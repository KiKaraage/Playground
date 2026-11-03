# SPDX-License-Identifier: Apache-2.0
"""Running a goal's checks, and turning the results into Proof.

A check is a line under a story in the goal file (``  - check: ...``) or a
dict. ``parse_check`` reads the plain-language forms:

    run: npm test                         a shell command, judged by exit code
    `pytest -q` passes                    the same
    GET /health returns 200               an HTTP probe on the preview
    / shows "Join the waitlist"           status 200 and the text on the page
    https://example.com/ returns 200      an absolute address works too
    screenshot of /pricing                a shot of the preview (the UI takes it)
    A seat is taken exactly once          anything else waits for a person

and the dict forms ``{"kind": "shell", "command": ...}``,
``{"kind": "http", "path"|"url": ..., "status": 200, "contains": ...}``,
``{"kind": "screenshot", "path": "/"}``.

``run_checks`` returns the data of a ``proof`` card (see ARCHITECTURE.md):
``{"shots": [relpath], "checks": [{"text", "status", "evidence"}], ...}``.
Evidence (command output, response bodies, screenshots and ``proof.json``) is
kept under ``.playground/local/proof/<goal>/<run>/``; ``evidence`` in each
check is that project-relative path, and ``note`` says what happened in plain
words.

Threat notes
    * Check commands run in the project folder with the same scrubbed
      environment as the preview (no API keys, tokens or SSH agent), in their
      own process group, with a timeout, and output capped at 1 MB.
    * Screenshots come from a callback the UI injects; this module never
      touches the display.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time
from typing import Any, Callable, Iterable
import urllib.error
import urllib.parse
import urllib.request

from .model import Check, ThreadEntry, now_iso
from .paths import ProjectPaths, relative_to_project, require_id
from .preview import clean_env
from .store import ProjectStore, atomic_write_json

KINDS = ("shell", "http", "screenshot", "manual")
MAX_OUTPUT = 1024 * 1024
MAX_BODY = 256 * 1024
DEFAULT_TIMEOUT = 120.0

# The UI's screenshot hook: (url, out_path) -> truthy when the PNG was written.
ScreenshotFn = Callable[[str, Path], Any]


@dataclass
class CheckSpec:
    text: str
    kind: str = "manual"
    command: str = ""
    url: str = ""             # absolute URL, or a path on the preview ("/pricing")
    status: int | None = None
    contains: str = ""
    timeout: float = DEFAULT_TIMEOUT


_SHELL_PREFIX = re.compile(r"^(?:run|command|shell|cmd)\s*:\s*(?P<cmd>.+)$", re.I)
_SHELL_DOLLAR = re.compile(r"^\$\s+(?P<cmd>.+)$")
_SHELL_TICKS = re.compile(r"^`(?P<cmd>[^`]+)`\s*(?:passes|succeeds|works|runs|exits 0|is green)?\.?$", re.I)
_HTTP = re.compile(
    r"^(?:(?:GET|open|visit|load|fetch|http)\s*:?\s+)?(?P<url>https?://\S+|/\S*)"
    r"(?:\s+(?:returns|gives|responds(?: with)?|answers(?: with)?|is|->|→)\s*(?P<status>\d{3}))?"
    r"(?:\s*,?\s*(?:and\s+)?(?:shows|contains|says|includes|has)\s+[\"“'](?P<text>.+?)[\"”'])?\.?$",
    re.I)
_SHOT = re.compile(r"^(?:take\s+an?\s+)?(?:screenshot|screen\s*shot|shot|snapshot)(?:\s+of)?\s*:?\s*(?P<path>\S+)?\.?$",
                   re.I)


def parse_check(item: str | dict | Check) -> CheckSpec:
    """Read one check, in plain language or as a dict."""
    if isinstance(item, Check):
        item = item.text
    if isinstance(item, dict):
        kind = str(item.get("kind") or "manual")
        text = str(item.get("text") or item.get("command") or item.get("url") or item.get("path") or kind)
        status = item.get("status")
        return CheckSpec(text=text, kind=kind if kind in KINDS else "manual", command=str(item.get("command") or ""),
                         url=str(item.get("url") or item.get("path") or ""),
                         status=int(status) if isinstance(status, (int, str)) and str(status).isdigit() else None,
                         contains=str(item.get("contains") or ""),
                         timeout=float(item.get("timeout") or DEFAULT_TIMEOUT))
    text = " ".join(str(item or "").split())
    body = re.sub(r"^check\s*:\s*", "", text, flags=re.I)
    for pattern in (_SHELL_PREFIX, _SHELL_DOLLAR, _SHELL_TICKS):
        match = pattern.match(body)
        if match:
            return CheckSpec(text=text, kind="shell", command=match.group("cmd").strip())
    match = _SHOT.match(body)
    if match:
        return CheckSpec(text=text, kind="screenshot", url=match.group("path") or "/")
    match = _HTTP.match(body)
    if match and (match.group("status") or match.group("text") or re.match(r"^(GET|open|visit|load|fetch|http)\b",
                                                                           body, re.I)):
        return CheckSpec(text=text, kind="http", url=match.group("url"),
                         status=int(match.group("status")) if match.group("status") else None,
                         contains=match.group("text") or "")
    return CheckSpec(text=text, kind="manual")


# ───────────────────────────── running ─────────────────────────────

def _run_dir(project_dir: Path, goal_id: str | None) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    folder = ProjectPaths(project_dir).local_dir / "proof" / (require_id(goal_id) if goal_id else "checks") / stamp
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _rel(project_dir: Path, path: Path) -> str:
    return relative_to_project(project_dir, path)


def _run_shell(project_dir: Path, spec: CheckSpec, log_path: Path, env: dict[str, str]) -> tuple[str, str]:
    started = time.monotonic()
    with open(log_path, "wb") as log:
        log.write(f"$ {spec.command}\n".encode())
        log.flush()
        try:
            proc = subprocess.Popen(["/bin/sh", "-c", spec.command], cwd=str(project_dir), env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                    start_new_session=True)
        except OSError as exc:
            log.write(f"\n{exc}\n".encode())
            return "fail", f"Couldn't start the command: {exc}"
        written = 0
        timed_out = False
        assert proc.stdout is not None
        os.set_blocking(proc.stdout.fileno(), False)
        while True:
            chunk = proc.stdout.read(65536)
            if chunk:
                if written < MAX_OUTPUT:
                    log.write(chunk[: MAX_OUTPUT - written])
                    written += len(chunk)
                continue
            if proc.poll() is not None:
                rest = proc.stdout.read() or b""
                if written < MAX_OUTPUT:
                    log.write(rest[: MAX_OUTPUT - written])
                break
            if time.monotonic() - started > spec.timeout:
                timed_out = True
                for sig in (signal.SIGTERM, signal.SIGKILL):
                    try:
                        os.killpg(proc.pid, sig)
                    except (ProcessLookupError, PermissionError):
                        break
                    try:
                        proc.wait(timeout=3)
                        break
                    except subprocess.TimeoutExpired:
                        continue
                break
            time.sleep(0.05)
        proc.stdout.close()
        code = proc.wait()
        elapsed = time.monotonic() - started
        log.write(f"\n# exit {code} after {elapsed:.1f}s\n".encode())
    if timed_out:
        seconds = int(round(spec.timeout))
        return "fail", f"Took longer than {seconds} second{'' if seconds == 1 else 's'}, so it was stopped."
    if code == 0:
        return "pass", f"Passed in {elapsed:.1f}s."
    return "fail", f"Failed (exit code {code})."


def _resolve_url(spec: CheckSpec, preview_url: str | None) -> str | None:
    if spec.url.startswith(("http://", "https://")):
        return spec.url
    if not preview_url:
        return None
    base = preview_url if preview_url.endswith("/") else preview_url + "/"
    parts = urllib.parse.urlsplit(base)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, spec.url or "/", "", ""))


def _run_http(spec: CheckSpec, url: str, body_path: Path) -> tuple[str, str]:
    request = urllib.request.Request(url, headers={"User-Agent": "Playground checks"})
    try:
        with urllib.request.urlopen(request, timeout=min(spec.timeout, 30)) as response:  # noqa: S310
            status, body = int(response.status), response.read(MAX_BODY)
    except urllib.error.HTTPError as exc:
        with exc:
            status, body = int(exc.code), exc.read(MAX_BODY) if exc.fp else b""
    except (urllib.error.URLError, OSError, ValueError) as exc:
        body_path.write_text(f"GET {url}\n\n{exc}\n")
        return "fail", "Nothing answered at that address. Is the preview running?"
    body_path.write_bytes(f"GET {url}\nHTTP {status}\n\n".encode() + body)
    expected = spec.status
    if expected is not None and status != expected:
        return "fail", f"Answered {status}, expected {expected}."
    if expected is None and not 200 <= status < 400:
        return "fail", f"Answered {status}."
    if spec.contains:
        text = body.decode("utf-8", errors="replace")
        if spec.contains.lower() not in text.lower():
            return "fail", f"Answered {status}, but the page doesn't say “{spec.contains}”."
        return "pass", f"Answered {status} and shows “{spec.contains}”."
    return "pass", f"Answered {status}."


def run_checks(project_dir: Path | str, checks: Iterable[str | dict | Check], *, goal_id: str | None = None,
               preview_url: str | None = None, screenshot: ScreenshotFn | None = None,
               timeout: float | None = None, env: dict[str, str] | None = None) -> dict:
    """Run checks now. Returns the ``proof`` card data; evidence is on disk."""
    root = Path(project_dir).resolve()
    folder = _run_dir(root, goal_id)
    run_env = clean_env({**(env or {}), **({"PREVIEW_URL": preview_url} if preview_url else {})})
    results: list[dict] = []
    shots: list[str] = []
    started = now_iso()
    for index, item in enumerate(checks, start=1):
        spec = parse_check(item)
        if timeout is not None:
            spec.timeout = timeout
        record: dict[str, Any] = {"text": spec.text, "kind": spec.kind}
        try:
            if spec.kind == "shell":
                path = folder / f"check-{index:02d}.log"
                status, note = _run_shell(root, spec, path, run_env)
                record["evidence"] = _rel(root, path)
            elif spec.kind == "http":
                url = _resolve_url(spec, preview_url)
                if url is None:
                    status, note = "pending", "The preview isn't running, so this couldn't be checked yet."
                else:
                    path = folder / f"check-{index:02d}.http.txt"
                    status, note = _run_http(spec, url, path)
                    record["evidence"] = _rel(root, path)
            elif spec.kind == "screenshot":
                url = _resolve_url(spec, preview_url)
                if screenshot is None or url is None:
                    status, note = "pending", "A screenshot needs the preview open in Playground."
                else:
                    path = folder / f"shot-{index:02d}.png"
                    produced = screenshot(url, path)
                    if isinstance(produced, (str, Path)) and Path(produced).exists():
                        path = Path(produced)
                    if produced and path.exists() and path.stat().st_size > 0:
                        rel = _rel(root, path)
                        shots.append(rel)
                        record["evidence"] = rel
                        status, note = "pass", "Screenshot taken."
                    else:
                        status, note = "fail", "The screenshot couldn't be taken."
            else:
                status, note = "pending", "This one needs a person (or the agent) to look."
        except Exception as exc:  # noqa: BLE001 - one broken check must not hide the others
            status, note = "fail", f"Playground couldn't run this check: {exc}"
        record["status"] = status
        record["note"] = note
        results.append(record)
    passed = sum(1 for r in results if r["status"] == "pass")
    failed = sum(1 for r in results if r["status"] == "fail")
    data = {"shots": shots, "checks": results, "summary": summarize(results), "passed": passed,
            "failed": failed, "pending": len(results) - passed - failed, "started": started,
            "finished": now_iso(), "evidence_dir": _rel(root, folder)}
    if goal_id:
        data["goal_id"] = goal_id
    atomic_write_json(folder / "proof.json", data)
    return data


def summarize(results: list[dict]) -> str:
    total = len(results)
    if not total:
        return "There are no checks yet."
    passed = sum(1 for r in results if r["status"] == "pass")
    failed = sum(1 for r in results if r["status"] == "fail")
    if passed == total:
        return "All checks pass." if total > 1 else "The check passes."
    parts = [f"{passed} of {total} checks pass"]
    if failed:
        parts.append(f"{failed} failed")
    pending = total - passed - failed
    if pending:
        parts.append(f"{pending} waiting for a look")
    return ", ".join(parts) + "."


def proof_entry(data: dict, by: str = "playground") -> ThreadEntry:
    """A ``proof`` card for the goal's thread."""
    return ThreadEntry(kind="proof", role="system", text=data.get("summary", ""), data=data, by=by)


def run_goal_checks(project_dir: Path | str, goal_id: str, *, preview_url: str | None = None,
                    screenshot: ScreenshotFn | None = None, record: bool = True, by: str = "playground",
                    store: ProjectStore | None = None) -> dict:
    """Run every check in a goal, write statuses back to the goal, and add a Proof card.

    The goal file keeps one evidence per check; the thread gets the full card.
    """
    store = store or ProjectStore(project_dir)
    goal = store.read_goal(goal_id)
    checks = goal.checks
    if preview_url is None:
        state = store.read_preview_state()
        preview_url = state.get("url") if state.get("state") == "running" else None
    data = run_checks(project_dir, checks, goal_id=goal_id, preview_url=preview_url, screenshot=screenshot)
    if record:
        results = iter(data["checks"])

        def apply(g: Any) -> None:
            for story in g.stories:
                for check in story.checks:
                    result = next(results, None)
                    if result is None:
                        return
                    check.status = result["status"]
                    check.evidence = result.get("evidence") or None

        store.update_goal(goal_id, apply)
        store.append_thread(goal_id, proof_entry(data, by=by))
    return data


__all__ = ["CheckSpec", "parse_check", "run_checks", "run_goal_checks", "proof_entry", "summarize", "KINDS"]
