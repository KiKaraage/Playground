# SPDX-License-Identifier: Apache-2.0
"""Playground's own Node.js, so nobody has to install one.

Claude, Codex and Gemini reach Playground through ACP adapters published on
npm. They need Node.js 22 or newer and npm. A good enough Node already on the
computer is used as is. Otherwise Playground downloads one pinned official
build from nodejs.org, checks it against the sha256 written below, and keeps it
in ``<data dir>/runtime/``. Nothing is installed system-wide and nothing needs
an administrator, on Luma, macOS or Windows.

To move to a newer Node, change ``NODE_VERSION`` and ``NODE_BUILDS`` together,
from ``https://nodejs.org/dist/v<version>/SHASUMS256.txt``.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from typing import Callable
import urllib.error
import urllib.request

from . import registry

MIN_NODE_MAJOR = 22   # claude-agent-acp's floor
NODE_VERSION = "24.21.0"   # LTS "Krypton", 7 Sep 2026
NODE_BUILDS = {
    "linux-x86_64": ("linux-x64.tar.xz", "fd8e59d5a511510f6a298afb548f18c7d2b1be404d8b4a27d94fbe49f56cb2d6"),
    "linux-aarch64": ("linux-arm64.tar.xz", "6ad1325edbdb5649c379b75a237147a666c95d4f9ae8d340fef2d1575d289ad2"),
    "darwin-aarch64": ("darwin-arm64.tar.gz", "bed7eea5325e1108f32ce5228ddd6a5f0f08a499ee42aa7442aea583702f6057"),
    "darwin-x86_64": ("darwin-x64.tar.gz", "1462cb3b3046b815cf8ea436d3da450ec1a9f11dac7e5a46b0ada5305d7e8097"),
    "windows-x86_64": ("win-x64.zip", "158f7685b44de51f6c0df1d153526cbcd3e1bc739a8dfc607721cef75de9e541"),
    "windows-aarch64": ("win-arm64.zip", "8779b1bde1d39f8d420e3b57aa657b39891af434d3de44a919044cec06785921"),
}
DIST_URL = "https://nodejs.org/dist"
# About what a person waits for, said before the download starts.
DOWNLOAD_MB = 30

Progress = Callable[[str, float | None], None]   # (plain words, 0..1 or None when unknown)

_install_lock = threading.Lock()


class SetupError(RuntimeError):
    pass


@dataclass
class Node:
    node: str        # the node program
    npm_cli: str     # npm-cli.js, run with node
    own: bool        # Playground's copy, not the computer's

    @property
    def bin_dir(self) -> str:
        return str(Path(self.node).parent)


def runtime_dir() -> Path:
    from ..core import paths
    return paths.data_dir() / "runtime"


def _build_name(plat: str) -> str:
    suffix = re.sub(r"\.(tar\.xz|tar\.gz|zip)$", "", NODE_BUILDS[plat][0])
    return f"node-v{NODE_VERSION}-{suffix}"


def _layout(root: Path, windows: bool) -> tuple[Path, Path]:
    if windows:
        return root / "node.exe", root / "node_modules" / "npm" / "bin" / "npm-cli.js"
    return root / "bin" / "node", root / "lib" / "node_modules" / "npm" / "bin" / "npm-cli.js"


def own_node(plat: str | None = None) -> Node | None:
    """Playground's downloaded Node, if it's there."""
    plat = plat or registry.platform_key()
    if plat not in NODE_BUILDS:
        return None
    node, npm = _layout(runtime_dir() / _build_name(plat), plat.startswith("windows"))
    if node.is_file() and os.access(node, os.X_OK) and npm.is_file():
        return Node(str(node), str(npm), True)
    return None


def _major(node: str) -> int:
    try:
        result = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=10,
                                stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return 0
    match = re.match(r"v(\d+)\.", result.stdout.strip())
    return int(match.group(1)) if result.returncode == 0 and match else 0


def system_node(path_env: str | None = None) -> Node | None:
    """A Node already on the computer, if it is new enough and has npm."""
    found = shutil.which("node", path=path_env)
    if not found:
        return None
    real = Path(os.path.realpath(found))
    candidates = [real.parent.parent / "lib" / "node_modules" / "npm" / "bin" / "npm-cli.js",   # Unix layout
                  real.parent / "node_modules" / "npm" / "bin" / "npm-cli.js"]                  # Windows layout
    npm = next((p for p in candidates if p.is_file()), None)
    if npm is None or _major(found) < MIN_NODE_MAJOR:
        return None
    return Node(found, str(npm), False)


def find_node(path_env: str | None = None) -> Node | None:
    """The Node agents run on: Playground's own when it has one (it is known good), else the computer's."""
    if os.environ.get("PLAYGROUND_SYSTEM_NODE_ONLY"):
        return system_node(path_env)
    return own_node() or system_node(path_env)


def ensure_node(*, path_env: str | None = None, progress: Progress | None = None, timeout: float = 300.0,
                opener=urllib.request.urlopen) -> Node:
    """A working Node, downloading Playground's own when the computer has none.

    Raises SetupError in plain words when it can't.
    """
    found = find_node(path_env)
    if found is not None:
        return found
    plat = registry.platform_key()
    if plat not in NODE_BUILDS:
        raise SetupError("Playground can’t run these agents on this kind of computer yet.")
    with _install_lock:
        found = own_node(plat)
        if found is not None:
            return found
        suffix, sha = NODE_BUILDS[plat]
        name = _build_name(plat)
        url = f"{DIST_URL}/v{NODE_VERSION}/node-v{NODE_VERSION}-{suffix}"
        base = runtime_dir()
        base.mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix=".node-", dir=str(base)))
        try:
            archive = work / f"download-{suffix}"
            _download(url, archive, sha, progress, timeout, opener)
            if progress:
                progress("Unpacking", None)
            unpacked = work / "unpacked"
            unpacked.mkdir()
            registry._extract(archive, url, unpacked)
            top = unpacked / name
            if not top.is_dir():
                raise SetupError("The Node.js download wasn’t laid out as expected, so Playground threw it away.")
            final = base / name
            if final.exists():
                shutil.rmtree(final)
            os.replace(top, final)
        except registry.RegistryError as error:
            raise SetupError(str(error)) from error
        except OSError as error:
            raise SetupError(_network_words(error)) from error
        finally:
            shutil.rmtree(work, ignore_errors=True)
        _remove_old_builds(base, name)
        found = own_node(plat)
        if found is None:
            raise SetupError("Node.js was downloaded but won’t start on this computer.")
        return found


def _download(url: str, dest: Path, sha256: str, progress: Progress | None, timeout: float, opener) -> None:
    digest = hashlib.sha256()
    request = urllib.request.Request(url, headers={"User-Agent": "Luma-Playground"})
    with opener(request, timeout=timeout) as response, open(dest, "wb") as out:
        total = int(response.headers.get("Content-Length") or 0) if getattr(response, "headers", None) else 0
        done = 0
        while True:
            chunk = response.read(1 << 18)
            if not chunk:
                break
            digest.update(chunk)
            out.write(chunk)
            done += len(chunk)
            if progress:
                progress("Downloading", done / total if total else None)
    if digest.hexdigest().lower() != sha256.lower():
        raise SetupError("The Node.js download didn’t match its checksum, so Playground threw it away.")


def _remove_old_builds(base: Path, keep: str) -> None:
    for child in base.glob("node-v*"):
        if child.name != keep and child.is_dir():
            shutil.rmtree(child, ignore_errors=True)


def _network_words(error: BaseException) -> str:
    return ("Playground couldn’t download what it needs. Check the internet connection and try again."
            if isinstance(error, (urllib.error.URLError, TimeoutError, ConnectionError)) else
            f"Playground couldn’t finish setting up ({error}).")

