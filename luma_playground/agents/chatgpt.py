# SPDX-License-Identifier: Apache-2.0
"""Sign in with ChatGPT, so Ari can run on the person's own ChatGPT plan.

This follows OpenAI's documentation for open-source and locally run apps
(developers.openai.com/siwc, "Registration and sign-in", "Accounts and
sessions", "Token reference", "Models and inference", "Errors and recovery",
"Preview limitations", read 9 Oct 2026):

- **Registration happens during the first sign-in.** The first authorize
  request uses ``client_id=dynamic_agent_client`` with ``agent_name_hint`` and a
  stable ``ext_agent_host_id``; the callback brings back the issued client id,
  which is kept with that account and used from then on. No client secret, no
  partner key.
- **OAuth with PKCE (S256) and a loopback redirect** on
  ``http://127.0.0.1:<port>/auth/callback`` (never ``localhost``), with fresh
  ``state``, ``nonce`` and verifier per attempt.
- **Tokens live only in the OS keyring** (libsecret's ``secret-tool`` on Luma,
  the macOS keychain), or, as a last resort, a 0600 file with a warning.
- **The ID token is checked** (RS256 signature against the issuer's JWKS,
  issuer, audience, expiry, nonce). Plan use needs the
  ``chatgpt.tokens.use.direct`` scope in the grant; a sign-in without it is
  kept, but plan use is off until the person allows it.
- **Refresh** with ``grant_type=refresh_token``: refresh tokens rotate, so each
  replacement is saved, and refreshes are serialised.
- **Inference** is ``POST https://api.openai.com/v1/responses`` with
  ``store: false`` and ``stream: true``, the whole history in ``input`` and the
  system prompt in ``instructions``; function tools are supported.

It's on unless turned off (``PLAYGROUND_SIWC=0``, or ``siwc = false`` under
``[features]`` in ``~/.config/playground/features.toml``); off, Ari's ChatGPT
connection says "coming soon" and nothing here runs.

No GTK imports. Tests use a fake OAuth and Responses server on localhost.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
import fcntl
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import secrets
import shutil
import socketserver
import subprocess
import sys
import threading
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from typing import Any, Callable

from . import acp
from ..core import paths

log = logging.getLogger(__name__)

ISSUER = "https://auth.openai.com"
AUTHORIZE_PATH = "/api/accounts/authorize"
TOKEN_PATH = "/api/accounts/oauth/token"
DISCOVERY_PATH = "/.well-known/openid-configuration"
API_BASE = "https://api.openai.com/v1"
RESOURCE = "https://api.openai.com/v1"
PLAN_SCOPE = "chatgpt.tokens.use.direct"
SCOPES = f"openid profile email offline_access resource.invoke {PLAN_SCOPE}"
DYNAMIC_CLIENT_ID = "dynamic_agent_client"
AGENT_NAME = "Luma Playground"
CALLBACK_PATH = "/auth/callback"
USAGE_URL = "https://chatgpt.com/settings/usage"
FEATURE_ENV = "PLAYGROUND_SIWC"
ISSUER_ENV = "PLAYGROUND_SIWC_ISSUER"      # tests and development only: a local fake issuer
STORE_ENV = "PLAYGROUND_TOKEN_STORE"       # tests and development only: "file" keeps the keyring untouched
KEYRING_APP = "org.projectluma.Playground"
KEYCHAIN_SERVICE = "org.projectluma.Playground.chatgpt"
REFRESH_MARGIN_S = 300
SIGN_IN_TIMEOUT_S = 10 * 60

COMING_SOON = "Ari on your ChatGPT plan is coming soon. For now, use a local model or an API key."
BUTTON_LABEL = "Continue with ChatGPT"
PLAN_LABEL = "Using ChatGPT plan"
FILE_WARNING = ("This computer's keyring isn't available, so Playground keeps your ChatGPT sign-in in a private "
                "file that only you can read. Setting up the keyring keeps it safer.")

TERMINAL_REFRESH_ERRORS = {"invalid_grant", "invalid_refresh_token", "token_expired", "refresh_token_expired",
                           "refresh_token_invalidated", "refresh_token_reused"}


# ── Errors, in plain words ─────────────────────────────────────────────────

class ChatGPTError(acp.AcpError):
    """Something about Sign in with ChatGPT. ``str()`` is for people."""


class SignInNeeded(acp.AuthRequired):
    """There's no usable sign-in: the runner shows the sign-in card."""

    def __init__(self, message: str = "Ari needs you to sign in with ChatGPT first.", **kw: Any) -> None:
        super().__init__(message, **kw)


class TokenRejected(SignInNeeded):
    """The API turned the access token down (HTTP 401)."""


class PlanUsageOff(acp.AuthRequired):
    """Signed in, but plan use wasn't allowed. Re-running sign-in with the plan scope fixes it."""

    reason = "plan-off"


class SignInDeclined(ChatGPTError):
    reason = "declined"


def enabled() -> bool:
    """On unless turned off (PLAYGROUND_SIWC=0, or siwc = false under [features]): Playground is open source
    and runs on the person's computer, the case OpenAI's open-source flow is for, as Luma's Ari does."""
    value = os.environ.get(FEATURE_ENV, "").strip().lower()
    if value in ("1", "true", "on", "yes"):
        return True
    if value in ("0", "false", "off", "no"):
        return False
    try:
        with open(paths.config_dir() / "features.toml", "rb") as handle:
            return bool((tomllib.load(handle).get("features") or {}).get("siwc", True))
    except (OSError, ValueError):
        return True


@dataclass
class Issuer:
    """Where sign-in happens. Always OpenAI's, except a local fake in tests."""
    issuer: str = ISSUER

    @classmethod
    def default(cls) -> "Issuer":
        override = os.environ.get(ISSUER_ENV, "").strip().rstrip("/")
        if override and (override.startswith("http://127.0.0.1:") or override.startswith("https://")):
            return cls(override)
        return cls()

    @property
    def authorize_url(self) -> str:
        return self.issuer + AUTHORIZE_PATH

    @property
    def token_url(self) -> str:
        return self.issuer + TOKEN_PATH

    @property
    def discovery_url(self) -> str:
        return self.issuer + DISCOVERY_PATH


def status(account: str, store: "TokenStore | None" = None) -> dict:
    """What the picker shows for an Ari-on-ChatGPT connection.

    ``state``: "coming-soon" (flag off), "signed-out", "plan-off" (signed in,
    plan use not allowed) or "signed-in". Never includes a token."""
    base = {"label": PLAN_LABEL, "button": BUTTON_LABEL, "manage_usage_url": USAGE_URL}
    if not enabled():
        return {**base, "state": "coming-soon", "text": COMING_SOON}
    store = store or TokenStore()
    record = store.get(account) or {}
    out = {**base, "email": record.get("email", ""), "storage": store.backend, "warning": store.warning}
    if not record.get("refresh_token") and not record.get("access_token"):
        return {**out, "state": "signed-out", "text": "Sign in with your ChatGPT account to use your plan here."}
    if not record.get("plan_usage"):
        return {**out, "state": "plan-off",
                "text": "You're signed in, but using your ChatGPT plan here isn't allowed yet. "
                        "Sign in again and allow it, or use an API key instead."}
    return {**out, "state": "signed-in", "text": "Eligible usage in this app uses your ChatGPT plan."}


# ── Small helpers ──────────────────────────────────────────────────────────

def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def pkce_pair() -> tuple[str, str]:
    """A fresh (verifier, S256 challenge)."""
    verifier = b64url(secrets.token_bytes(48))
    return verifier, b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def host_id() -> str:
    """This computer's stable ``ext_agent_host_id`` (``urn:uuid:...``). Not a secret."""
    path = paths.config_dir() / "chatgpt-host.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8")).get("ext_agent_host_id")
        if isinstance(value, str) and value.startswith("urn:uuid:"):
            return value
    except (OSError, ValueError, AttributeError):
        pass
    value = f"urn:uuid:{uuid.uuid4()}"
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_private(path, json.dumps({"ext_agent_host_id": value}))
    return value


def _write_private(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _post_form(url: str, form: dict, opener: Callable, timeout: float = 30.0) -> tuple[int, dict]:
    """POST a form; returns (status, JSON body). Network failures raise OSError/URLError."""
    request = urllib.request.Request(url, data=urllib.parse.urlencode(form).encode("ascii"), method="POST",
                                     headers={"Content-Type": "application/x-www-form-urlencoded",
                                              "Accept": "application/json", "User-Agent": "Luma-Playground"})
    try:
        with opener(request, timeout=timeout) as response:
            raw = response.read(1 << 20)
            code = getattr(response, "status", 200)
    except urllib.error.HTTPError as error:
        code = error.code
        try:
            raw = error.read(1 << 16)
        finally:
            error.close()
    try:
        body = json.loads(raw.decode("utf-8") or "{}") if raw else {}
    except ValueError:
        body = {}
    return code, body if isinstance(body, dict) else {}


def _get_json(url: str, opener: Callable, timeout: float = 15.0, token: str = "") -> Any:
    headers = {"Accept": "application/json", "User-Agent": "Luma-Playground"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with opener(urllib.request.Request(url, headers=headers), timeout=timeout) as response:
        return json.loads(response.read(4 << 20).decode("utf-8"))


def _error_code(body: dict) -> str:
    error = body.get("error")
    if isinstance(error, dict):
        return str(error.get("code") or error.get("type") or "")
    return str(error or body.get("code") or "")


# ── ID token checks (RS256, standard library only) ─────────────────────────

_SHA256_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")


def _rs256_ok(signing_input: bytes, signature: bytes, n: int, e: int) -> bool:
    """RSASSA-PKCS1-v1_5 with SHA-256."""
    size = (n.bit_length() + 7) // 8
    if len(signature) != size:
        return False
    decoded = pow(int.from_bytes(signature, "big"), e, n).to_bytes(size, "big")
    digest = _SHA256_PREFIX + hashlib.sha256(signing_input).digest()
    expected = b"\x00\x01" + b"\xff" * (size - len(digest) - 3) + b"\x00" + digest
    return hmac.compare_digest(decoded, expected)


def verify_id_token(token: str, *, jwks: dict, issuer: str, audience: str, nonce: str,
                    now: float | None = None, leeway: float = 120.0) -> dict:
    """Check an ID token and return its claims, or raise ChatGPTError."""
    try:
        head_b64, body_b64, sig_b64 = token.split(".")
        header = json.loads(b64url_decode(head_b64))
        claims = json.loads(b64url_decode(body_b64))
        signature = b64url_decode(sig_b64)
    except (ValueError, AttributeError) as error:
        raise ChatGPTError("ChatGPT's sign-in reply couldn't be read.", detail=str(error)) from error
    if header.get("alg") != "RS256":
        raise ChatGPTError("ChatGPT's sign-in reply wasn't signed the way Playground expects.",
                           detail=f"alg={header.get('alg')}")
    keys = [k for k in (jwks.get("keys") or []) if isinstance(k, dict) and k.get("kty") == "RSA"]
    if header.get("kid"):
        keys = [k for k in keys if k.get("kid") == header["kid"]] or keys
    signing_input = f"{head_b64}.{body_b64}".encode("ascii")
    good = False
    for key in keys:
        try:
            n = int.from_bytes(b64url_decode(key["n"]), "big")
            e = int.from_bytes(b64url_decode(key["e"]), "big")
        except (KeyError, ValueError):
            continue
        if _rs256_ok(signing_input, signature, n, e):
            good = True
            break
    if not good:
        raise ChatGPTError("ChatGPT's sign-in reply didn't check out, so Playground didn't use it.",
                           detail="bad signature")
    now = time.time() if now is None else now
    aud = claims.get("aud")
    auds = aud if isinstance(aud, list) else [aud]
    problems = []
    if claims.get("iss") != issuer:
        problems.append("issuer")
    if audience not in auds:
        problems.append("audience")
    if not isinstance(claims.get("exp"), (int, float)) or claims["exp"] < now - leeway:
        problems.append("expired")
    if not hmac.compare_digest(str(claims.get("nonce") or ""), nonce):
        problems.append("nonce")
    if not claims.get("sub"):
        problems.append("subject")
    if problems:
        raise ChatGPTError("ChatGPT's sign-in reply didn't check out, so Playground didn't use it.",
                           detail="id token: " + ", ".join(problems))
    return claims


# ── Where tokens live: the keyring, never a project ───────────────────────

class TokenStore:
    """One record per account (connection id), as JSON, in the OS keyring.

    ``backend``: "secret-tool" (libsecret, Luma), "keychain" (macOS) or "file"
    (a 0600 file under Playground's data folder, with ``warning`` set)."""

    _warned = False

    def __init__(self, backend: str = "auto", *, runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
                 folder: str | os.PathLike | None = None) -> None:
        self.run = runner
        self.folder = Path(folder) if folder else paths.data_dir() / "chatgpt"
        if backend == "auto":
            backend = os.environ.get(STORE_ENV, "").strip() or "auto"
            if backend not in ("auto", "secret-tool", "keychain", "file"):
                backend = "auto"
        if backend == "auto":
            if sys.platform.startswith("linux") and shutil.which("secret-tool"):
                backend = "secret-tool"
            elif sys.platform == "darwin" and shutil.which("security"):
                backend = "keychain"
            else:
                backend = "file"
        self.backend = backend
        self.warning = FILE_WARNING if backend == "file" else ""
        if self.warning and not TokenStore._warned:
            TokenStore._warned = True
            log.warning("ChatGPT sign-in is kept in a 0600 file: no keyring on this computer")

    @staticmethod
    def _check(account: str) -> str:
        if not paths.is_valid_id(account):
            raise ValueError(f"not an account id: {account!r}")
        return account

    def _attrs(self, account: str) -> list[str]:
        return ["app", KEYRING_APP, "kind", "chatgpt", "account", account]

    def get(self, account: str) -> dict | None:
        account = self._check(account)
        text = ""
        if self.backend == "secret-tool":
            result = self.run(["secret-tool", "lookup", *self._attrs(account)], capture_output=True, text=True,
                              timeout=15)
            text = (result.stdout or "").strip() if result.returncode == 0 else ""
        elif self.backend == "keychain":
            result = self.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", account, "-w"],
                              capture_output=True, text=True, timeout=15)
            text = (result.stdout or "").strip() if result.returncode == 0 else ""
            if text and not text.startswith("{"):
                try:
                    text = bytes.fromhex(text).decode("utf-8")   # data that the tool printed as hex
                except ValueError:
                    text = ""
        else:
            try:
                text = (self.folder / f"{account}.json").read_text(encoding="utf-8")
            except OSError:
                text = ""
        if not text:
            return None
        try:
            data = json.loads(text)
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def put(self, account: str, record: dict) -> None:
        account = self._check(account)
        payload = json.dumps(record, ensure_ascii=True, separators=(",", ":"))
        if self.backend == "secret-tool":
            result = self.run(["secret-tool", "store", "--label", f"Playground: ChatGPT sign-in ({account})",
                               *self._attrs(account)], input=payload, capture_output=True, text=True, timeout=15)
        elif self.backend == "keychain":
            # Through ``security -i`` so the secret is never on a command line other programs can see.
            command = (f"add-generic-password -U -s {KEYCHAIN_SERVICE} -a {account} "
                       f"-X {payload.encode('utf-8').hex()}\n")
            result = self.run(["security", "-i"], input=command, capture_output=True, text=True, timeout=15)
        else:
            self.folder.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(self.folder, 0o700)
            except OSError:
                pass
            _write_private(self.folder / f"{account}.json", payload)
            return
        if result.returncode != 0:
            raise ChatGPTError("Playground couldn't save your ChatGPT sign-in in the keyring.",
                               detail=(result.stderr or "")[-300:])

    def delete(self, account: str) -> None:
        account = self._check(account)
        if self.backend == "secret-tool":
            self.run(["secret-tool", "clear", *self._attrs(account)], capture_output=True, text=True, timeout=15)
        elif self.backend == "keychain":
            self.run(["security", "delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", account],
                     capture_output=True, text=True, timeout=15)
        else:
            try:
                (self.folder / f"{account}.json").unlink()
            except FileNotFoundError:
                pass


_KEEP_AFTER_SIGN_OUT = ("issuer", "subject", "email", "client_id", "ext_agent_host_id")


def _without_tokens(record: dict) -> dict:
    """What stays after signing out: the account and its issued client id, for next time."""
    return {k: record[k] for k in _KEEP_AFTER_SIGN_OUT if k in record}


# ── The browser sign-in ────────────────────────────────────────────────────

class _QuickServer(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self) -> None:   # no reverse DNS lookup (slow on a Mac with no network)
        socketserver.TCPServer.server_bind(self)
        self.server_name = "127.0.0.1"
        self.server_port = self.server_address[1]


_DONE_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Playground</title>
<style>body{{font:16px/1.5 system-ui,sans-serif;margin:15vh auto;max-width:28em;padding:0 1em;color:#222}}</style>
</head><body><h1>{title}</h1><p>{text}</p></body></html>"""


@dataclass
class Account:
    account: str
    email: str
    subject: str
    client_id: str
    plan_usage: bool


class SignInFlow:
    """One "Continue with ChatGPT": loopback listener, browser, callback, code exchange,
    ID token checks, then the record goes into the keyring. ``run()`` blocks; call it
    off the UI thread. ``url`` is the address the browser was sent to (for a "copy
    link" fallback), set once ``run()`` has started."""

    def __init__(self, account: str, *, store: TokenStore | None = None, issuer: Issuer | None = None,
                 opener: Callable = urllib.request.urlopen, open_browser: Callable[[str], Any] = webbrowser.open,
                 timeout: float = SIGN_IN_TIMEOUT_S, agent_name: str = AGENT_NAME,
                 clock: Callable[[], float] = time.time) -> None:
        self.account = TokenStore._check(account)
        self.store = store or TokenStore()
        self.issuer = issuer or Issuer.default()
        self.opener = opener
        self.open_browser = open_browser
        self.timeout = timeout
        self.agent_name = agent_name
        self.clock = clock
        self.url = ""
        self._got = threading.Event()
        self._query: dict[str, str] = {}
        self._server: _QuickServer | None = None

    def cancel(self) -> None:
        self._query = {"error": "cancelled"}
        self._got.set()

    def _listen(self) -> int:
        flow = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def do_GET(self) -> None:  # noqa: N802
                parsed = urllib.parse.urlsplit(self.path)
                if parsed.path != CALLBACK_PATH or flow._got.is_set():
                    self.send_error(404)
                    return
                query = {k: v[-1] for k, v in urllib.parse.parse_qs(parsed.query).items()}
                if query.get("error"):
                    title, text = "Not signed in", "Nothing was changed. You can close this tab."
                else:
                    title, text = "You're signed in", "You can close this tab and go back to Playground."
                page = _DONE_PAGE.format(title=title, text=text).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(page)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(page)
                flow._query = query
                flow._got.set()

        self._server = _QuickServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.1}, name="pg-siwc-callback",
                         daemon=True).start()
        return self._server.server_port

    def run(self) -> Account:
        prior = self.store.get(self.account) or {}
        client_id = str(prior.get("client_id") or DYNAMIC_CLIENT_ID)
        registering = client_id == DYNAMIC_CLIENT_ID
        state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
        verifier, challenge = pkce_pair()
        ext_host = str(prior.get("ext_agent_host_id") or host_id())
        port = self._listen()   # before the browser opens
        redirect_uri = f"http://127.0.0.1:{port}{CALLBACK_PATH}"
        try:
            params = {"client_id": client_id, "response_type": "code", "redirect_uri": redirect_uri,
                      "scope": SCOPES, "resource": RESOURCE, "state": state, "nonce": nonce,
                      "code_challenge": challenge, "code_challenge_method": "S256", "ext_agent_host_id": ext_host}
            if registering:
                params["agent_name_hint"] = self.agent_name
            else:
                if prior.get("id_token"):
                    params["id_token_hint"] = prior["id_token"]
                if prior.get("email"):
                    params["login_hint"] = prior["email"]
            self.url = self.issuer.authorize_url + "?" + urllib.parse.urlencode(params)
            try:
                opened = self.open_browser(self.url)
            except Exception:  # noqa: BLE001
                opened = False
            if opened is False:
                log.info("couldn't open a browser for ChatGPT sign-in")
            if not self._got.wait(self.timeout):
                raise ChatGPTError("Signing in took too long, so Playground stopped waiting. Try again.")
            query = dict(self._query)
        finally:
            if self._server is not None:
                self._server.shutdown()
                self._server.server_close()
        if query.get("error") == "cancelled":
            raise SignInDeclined("Signing in was cancelled.")
        if not hmac.compare_digest(str(query.get("state") or ""), state):
            raise ChatGPTError("That sign-in didn't match the one Playground started, so it wasn't used. Try again.")
        if query.get("error"):
            if query["error"] == "access_denied":
                raise SignInDeclined("You chose not to sign in. Nothing was changed.")
            raise ChatGPTError("ChatGPT couldn't finish signing in. Try again.",
                               detail=f"{query.get('error')}: {query.get('error_description', '')}"[:300])
        issued = str(query.get("client_id") or "")
        if registering:
            if not issued:
                raise ChatGPTError("ChatGPT didn't finish setting Playground up. Try again.",
                                   detail="registration callback without client_id")
            client_id = issued
            # Keep the issued id straight away: if the next step fails, the next try reuses it.
            self.store.put(self.account, {**_without_tokens(prior), "client_id": client_id,
                                          "ext_agent_host_id": ext_host, "issuer": self.issuer.issuer})
        elif issued and issued != client_id:
            raise ChatGPTError("ChatGPT answered for a different setup than this one, so Playground didn't use it.",
                               detail="client_id mismatch on reauthorization")
        code = str(query.get("code") or "")
        if not code:
            raise ChatGPTError("ChatGPT didn't send a sign-in code. Try again.")
        try:
            status_code, tokens = _post_form(self.issuer.token_url, {
                "grant_type": "authorization_code", "client_id": client_id, "code": code,
                "code_verifier": verifier, "redirect_uri": redirect_uri, "resource": RESOURCE}, self.opener)
        except (OSError, urllib.error.URLError) as error:
            raise ChatGPTError("Playground couldn't reach ChatGPT to finish signing in. Check the internet "
                               "connection and try again.", detail=str(error), reason="offline") from error
        if status_code != 200 or not tokens.get("access_token"):
            if _error_code(tokens) == "invalid_grant":
                raise ChatGPTError("That sign-in expired before Playground could finish it. Try again.",
                                   detail="invalid_grant")
            raise ChatGPTError("ChatGPT didn't finish signing in. Try again.",
                               detail=f"token endpoint {status_code} {_error_code(tokens)}")
        try:
            discovery = _get_json(self.issuer.discovery_url, self.opener)
            jwks = _get_json(str(discovery["jwks_uri"]), self.opener)
        except (OSError, ValueError, KeyError, urllib.error.URLError) as error:
            raise ChatGPTError("Playground couldn't check the sign-in with ChatGPT. Try again.",
                               detail=str(error)) from error
        claims = verify_id_token(str(tokens.get("id_token") or ""), jwks=jwks, issuer=self.issuer.issuer,
                                 audience=client_id, nonce=nonce, now=self.clock())
        subject = str(claims["sub"])
        if prior.get("subject") and prior["subject"] != subject:
            raise ChatGPTError("You signed in with a different ChatGPT account from the one this connection uses. "
                               "Add it as a new connection instead.", detail="subject mismatch")
        scopes = str(tokens.get("scope") or "").split()
        now = self.clock()
        record = {
            "issuer": self.issuer.issuer, "subject": subject, "email": str(claims.get("email") or ""),
            "client_id": client_id, "ext_agent_host_id": ext_host, "id_token": tokens.get("id_token"),
            "access_token": tokens["access_token"], "refresh_token": tokens.get("refresh_token") or "",
            "token_type": tokens.get("token_type") or "Bearer",
            "expires_at": now + float(tokens.get("expires_in") or 3600), "scopes": scopes,
            "plan_usage": PLAN_SCOPE in scopes, "saved_at": now,
        }
        if tokens.get("earliest_refresh_at"):
            record["earliest_refresh_at"] = tokens["earliest_refresh_at"]
        self.store.put(self.account, record)
        return Account(self.account, record["email"], subject, client_id, record["plan_usage"])


# ── Using and refreshing the tokens ────────────────────────────────────────

class TokenManager:
    """Hands Ari a fresh access token for one account, refreshing near expiry.
    Refreshes are serialised within the app and across processes (a lock file),
    because each refresh token can be used only once."""

    _locks: dict[str, threading.Lock] = {}
    _locks_guard = threading.Lock()

    def __init__(self, account: str, *, store: TokenStore | None = None, issuer: Issuer | None = None,
                 opener: Callable = urllib.request.urlopen, clock: Callable[[], float] = time.time,
                 margin_s: float = REFRESH_MARGIN_S) -> None:
        self.account = TokenStore._check(account)
        self.store = store or TokenStore()
        self.issuer = issuer or Issuer.default()
        self.opener = opener
        self.clock = clock
        self.margin_s = margin_s
        with self._locks_guard:
            self._lock = self._locks.setdefault(self.account, threading.Lock())

    def record(self) -> dict:
        return self.store.get(self.account) or {}

    def signed_in(self) -> bool:
        record = self.record()
        return bool(record.get("refresh_token") or record.get("access_token"))

    def plan_usage(self) -> bool:
        return bool(self.record().get("plan_usage"))

    def _file_lock(self):
        folder = paths.data_dir() / "chatgpt"
        folder.mkdir(parents=True, exist_ok=True)
        handle = open(folder / f"{self.account}.lock", "a+")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return handle

    def access_token(self, force_refresh: bool = False) -> str:
        with self._lock:
            handle = self._file_lock()
            try:
                record = self.record()   # read again under the lock: another process may have refreshed
                if not record.get("access_token") and not record.get("refresh_token"):
                    raise SignInNeeded()
                if not record.get("plan_usage"):
                    raise PlanUsageOff("You're signed in to ChatGPT, but using your plan here isn't allowed yet. "
                                       "Sign in again and allow it, or use an API key instead.")
                fresh = float(record.get("expires_at") or 0) - self.margin_s > self.clock()
                if fresh and not force_refresh and record.get("access_token"):
                    return str(record["access_token"])
                return self._refresh(record)
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()

    def _refresh(self, record: dict) -> str:
        if not record.get("refresh_token") or not record.get("client_id"):
            raise SignInNeeded("Your ChatGPT sign-in has ended. Sign in again to keep using your plan.")
        try:
            code, body = _post_form(self.issuer.token_url, {
                "grant_type": "refresh_token", "client_id": record["client_id"],
                "refresh_token": record["refresh_token"], "resource": RESOURCE}, self.opener)
        except (OSError, urllib.error.URLError) as error:
            raise ChatGPTError("Ari can't reach ChatGPT. Check the internet connection, then try again.",
                               detail=str(error), reason="offline") from error
        if code == 200 and body.get("access_token"):
            now = self.clock()
            record = dict(record)
            record["access_token"] = body["access_token"]
            if body.get("refresh_token"):
                record["refresh_token"] = body["refresh_token"]   # rotated: the old one is spent
            record["expires_at"] = now + float(body.get("expires_in") or 3600)
            if body.get("scope"):
                record["scopes"] = str(body["scope"]).split()
                record["plan_usage"] = PLAN_SCOPE in record["scopes"]
            if body.get("id_token"):
                record["id_token"] = body["id_token"]
            record["saved_at"] = now
            self.store.put(self.account, record)
            return str(record["access_token"])
        error = _error_code(body)
        if error in TERMINAL_REFRESH_ERRORS or code in (400, 401) and not error:
            self.store.put(self.account, _without_tokens(record))
            raise SignInNeeded("Your ChatGPT sign-in has ended. Sign in again to keep using your plan.",
                               detail=f"refresh: {code} {error}")
        if error == "invalid_client":
            raise ChatGPTError("Playground's ChatGPT setup isn't working. Sign in again, and if that doesn't help, "
                               "remove the connection and add it again.", detail="refresh: invalid_client")
        raise ChatGPTError("ChatGPT is busy right now. Try again in a little while.",
                           detail=f"refresh: {code} {error}", reason="busy")

    def sign_out(self) -> bool:
        """Revoke the refresh token and forget the tokens; keep the account and client id
        for next time. Returns False when OpenAI couldn't confirm the revocation (the
        person can disconnect Playground in ChatGPT's settings)."""
        with self._lock:
            record = self.record()
            confirmed = True
            token = record.get("refresh_token") or record.get("access_token")
            if token and record.get("client_id"):
                confirmed = False
                try:
                    endpoint = _get_json(self.issuer.discovery_url, self.opener).get("revocation_endpoint")
                    if endpoint:
                        code, _ = _post_form(str(endpoint), {
                            "token": token, "client_id": record["client_id"],
                            "token_type_hint": "refresh_token" if record.get("refresh_token") else "access_token"},
                            self.opener)
                        confirmed = code == 200
                except (OSError, ValueError, urllib.error.URLError):
                    confirmed = False
            self.store.put(self.account, _without_tokens(record))
            return confirmed


# ── The Responses API on the person's plan ────────────────────────────────

def list_models(token: str, *, base_url: str = API_BASE, opener: Callable = urllib.request.urlopen) -> list[dict]:
    """The models this account may use, in the server's order: [{slug, name}]."""
    try:
        data = _get_json(f"{base_url.rstrip('/')}/models", opener, token=token)
    except urllib.error.HTTPError as error:
        raise plain_http_error(error.code, "") from error
    except (OSError, urllib.error.URLError) as error:
        raise plain_network_error(error) from error
    out = []
    if isinstance(data.get("models"), list):
        for item in data["models"]:
            if isinstance(item, dict) and item.get("slug") and item.get("visibility", "list") == "list":
                out.append({"slug": str(item["slug"]), "name": str(item.get("display_name") or item["slug"])})
    elif isinstance(data.get("data"), list):
        out = [{"slug": str(i["id"]), "name": str(i["id"])} for i in data["data"] if isinstance(i, dict) and i.get("id")]
    return out


def default_model(token: str, *, base_url: str = API_BASE, opener: Callable = urllib.request.urlopen) -> str:
    models = list_models(token, base_url=base_url, opener=opener)
    if not models:
        raise ChatGPTError("Your ChatGPT plan doesn't offer any models to Playground right now.")
    return models[0]["slug"]


def responses_body(model: str, messages: list[dict], tools: list[dict]) -> dict:
    """A Responses request from Ari's OpenAI-chat-shaped history: system text goes in
    ``instructions`` (system items are refused on this route), tool calls and their
    results become ``function_call`` / ``function_call_output`` items."""
    instructions: list[str] = []
    items: list[dict] = []
    for message in messages:
        role = message.get("role")
        if role == "system":
            instructions.append(str(message.get("content") or ""))
        elif role == "user":
            items.append({"role": "user", "content": str(message.get("content") or "")})
        elif role == "assistant":
            if message.get("content"):
                items.append({"role": "assistant", "content": str(message["content"])})
            for call in message.get("tool_calls") or []:
                fn = call.get("function") or {}
                args = fn.get("arguments")
                items.append({"type": "function_call", "call_id": str(call.get("id") or ""),
                              "name": str(fn.get("name") or ""),
                              "arguments": args if isinstance(args, str) else json.dumps(args or {})})
        elif role == "tool":
            items.append({"type": "function_call_output", "call_id": str(message.get("tool_call_id") or ""),
                          "output": str(message.get("content") or "")})
    body: dict[str, Any] = {"model": model, "input": items, "store": False, "stream": True}
    if instructions:
        body["instructions"] = "\n\n".join(instructions)
    converted = []
    for tool in tools or []:
        fn = tool.get("function") if tool.get("type") == "function" else None
        if isinstance(fn, dict):
            converted.append({"type": "function", "name": fn.get("name"), "description": fn.get("description", ""),
                              "parameters": fn.get("parameters") or {"type": "object", "properties": {}}})
    if converted:
        body["tools"] = converted
        body["tool_choice"] = "auto"
    return body


def responses_request(base_url: str, token: str, body: dict) -> urllib.request.Request:
    return urllib.request.Request(f"{base_url.rstrip('/')}/responses", data=json.dumps(body).encode("utf-8"),
                                  method="POST", headers={"Content-Type": "application/json",
                                                          "Accept": "text/event-stream",
                                                          "Authorization": f"Bearer {token}",
                                                          "User-Agent": "Luma-Playground-Ari"})


def plain_error(code: str, message: str = "", param: str = "", http: int = 0) -> acp.AcpError:
    """OpenAI's documented plan-usage errors, in words, with the documented recovery."""
    if code == "subscription_sharing_usage_limit_exceeded" or http == 429:
        return ChatGPTError("You've reached the limit for using your ChatGPT plan in Playground for now. "
                            "Review your plan or this app's limit in ChatGPT settings.",
                            reason="usage-limit", data={"manage_usage": USAGE_URL}, detail=f"{code} {message}")
    if code == "subscription_sharing_user_not_eligible":
        return ChatGPTError("This ChatGPT account can't use its plan in other apps. Use an API key or a local "
                            "model instead.", reason="not-eligible", detail=code)
    if code in ("subscription_sharing_usage_unavailable", "subscription_sharing_user_unavailable") or http == 503:
        return ChatGPTError("ChatGPT is busy right now. Try again in a little while.", reason="busy",
                            detail=f"{code} {message}")
    if code == "subscription_sharing_unsupported_capability":
        return ChatGPTError("ChatGPT didn't accept part of Ari's request" + (f" ({param})" if param else "") + ".",
                            detail=f"{code} {param} {message}")
    if code in ("subscription_sharing_invalid_user",) or http == 401:
        return TokenRejected("Ari needs you to sign in with ChatGPT again.", detail=f"{code} {message}")
    if code.startswith("chatpass_v2_") or code == "subscription_sharing_route_not_supported":
        return ChatGPTError("Playground's ChatGPT setup isn't right, so the request was turned down.",
                            detail=f"{code} {message}")
    if http == 403:
        return ChatGPTError("ChatGPT turned that request down. Using your plan this way may not be offered "
                            "where you are.", detail=f"{code} {message}")
    return ChatGPTError("ChatGPT ran into a problem with that request." if not message or len(message) > 200
                        else f"ChatGPT said: {message}", detail=f"{http} {code} {message}")


def plain_http_error(http: int, detail: str) -> acp.AcpError:
    code, message, param = "", "", ""
    try:
        error = json.loads(detail).get("error") or {}
        if isinstance(error, dict):
            code, message, param = str(error.get("code") or ""), str(error.get("message") or ""), \
                str(error.get("param") or "")
    except (ValueError, AttributeError):
        pass
    return plain_error(code, message, param, http)


def plain_network_error(error: Exception) -> acp.AcpError:
    return ChatGPTError("Ari can't reach ChatGPT. Check the internet connection, then try again.",
                        detail=str(error), reason="offline")


def read_responses_stream(lines, on_text: Callable[[str], None], cancelled: Callable[[], bool]) -> dict:
    """Read a Responses SSE stream. Success only on ``response.completed`` (or
    ``response.incomplete``); a failure arriving mid-stream raises its plain error."""
    content: list[str] = []
    calls: dict[str, dict] = {}
    order: list[str] = []
    finish = ""
    usage: dict = {}
    data_lines: list[str] = []

    def handle(event: dict) -> bool:
        nonlocal finish, usage
        kind = event.get("type", "")
        if kind == "response.output_text.delta":
            text = event.get("delta") or ""
            if text:
                content.append(text)
                on_text(text)
        elif kind in ("response.output_item.added", "response.output_item.done"):
            item = event.get("item") or {}
            if item.get("type") == "function_call":
                key = str(item.get("id") or item.get("call_id") or len(order))
                if key not in calls:
                    calls[key] = {"id": "", "name": "", "arguments": ""}
                    order.append(key)
                slot = calls[key]
                slot["id"] = str(item.get("call_id") or slot["id"])
                slot["name"] = str(item.get("name") or slot["name"])
                if kind.endswith(".done") and isinstance(item.get("arguments"), str):
                    slot["arguments"] = item["arguments"]
        elif kind == "response.function_call_arguments.delta":
            key = str(event.get("item_id") or "")
            if key in calls:
                calls[key]["arguments"] += str(event.get("delta") or "")
        elif kind == "response.completed":
            raw = (event.get("response") or {}).get("usage") or {}
            usage = {"prompt_tokens": int(raw.get("input_tokens") or 0),
                     "completion_tokens": int(raw.get("output_tokens") or 0)}
            finish = "tool_calls" if calls else "stop"
            return True
        elif kind == "response.incomplete":
            details = (event.get("response") or {}).get("incomplete_details") or {}
            finish = "length" if "token" in str(details.get("reason") or "") else "stop"
            return True
        elif kind in ("response.failed", "error"):
            error = (event.get("response") or {}).get("error") if kind == "response.failed" else event
            error = error if isinstance(error, dict) else {}
            inner = error.get("error") if isinstance(error.get("error"), dict) else error
            raise plain_error(str(inner.get("code") or ""), str(inner.get("message") or ""),
                              str(inner.get("param") or ""))
        return False

    for raw in lines:
        if cancelled():
            raise InterruptedError
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n") if isinstance(raw, bytes) else str(raw).rstrip("\r\n")
        if line.startswith("data:"):
            data_lines.append(line[5:].strip())
            continue
        if line.strip() or not data_lines:
            continue
        payload, data_lines = "\n".join(data_lines), []
        if payload == "[DONE]":
            break
        try:
            event = json.loads(payload)
        except ValueError:
            continue
        if isinstance(event, dict) and handle(event):
            break
    else:
        if data_lines:
            try:
                event = json.loads("\n".join(data_lines))
                if isinstance(event, dict):
                    handle(event)
            except ValueError:
                pass
    if not finish:
        raise ChatGPTError("ChatGPT stopped answering partway through. Try again.", detail="stream ended early")
    tool_calls = [{"id": calls[k]["id"] or f"call_{uuid.uuid4().hex[:8]}", "name": calls[k]["name"],
                   "arguments": calls[k]["arguments"]} for k in order if calls[k]["name"]]
    return {"content": "".join(content), "tool_calls": tool_calls, "finish_reason": finish, "usage": usage}
