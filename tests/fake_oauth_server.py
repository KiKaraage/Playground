# SPDX-License-Identifier: Apache-2.0
"""A fake "Sign in with ChatGPT" issuer for tests: discovery, JWKS, authorize,
token (code and refresh grants, with rotation), revocation. Standard library only.

    issuer = FakeIssuer()
    url = issuer.start()                 # http://127.0.0.1:<port>
    browser = issuer.browser             # use as open_browser: "the person" approves in the browser
    ...
    issuer.stop()

It signs ID tokens with a small RSA key made at start (RS256), checks PKCE
(S256), the loopback redirect, the client id it issued, and the resource, and
records every request in ``issuer.log``. Switches: ``deny`` (the person says
no), ``grant_plan`` (False leaves out chatgpt.tokens.use.direct),
``wrong_state``, ``bad_signature``, ``fail_refresh`` (an error code).
"""
from __future__ import annotations

import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import random
import secrets
import socketserver
import threading
import time
import urllib.parse
import urllib.request

PLAN_SCOPE = "chatgpt.tokens.use.direct"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _is_probable_prime(n: int, rounds: int = 24) -> bool:
    if n < 4:
        return n in (2, 3)
    for p in (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37):
        if n % p == 0:
            return n == p
    d, r = n - 1, 0
    while d % 2 == 0:
        d //= 2
        r += 1
    rng = random.Random(n)
    for _ in range(rounds):
        a = rng.randrange(2, n - 2)
        x = pow(a, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(r - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def _prime(bits: int) -> int:
    while True:
        candidate = secrets.randbits(bits) | (1 << (bits - 1)) | 1
        if _is_probable_prime(candidate):
            return candidate


class RsaKey:
    """A test-only RSA key (1024 bits; never for real use)."""

    def __init__(self, bits: int = 1024) -> None:
        e = 65537
        while True:
            p, q = _prime(bits // 2), _prime(bits // 2)
            phi = (p - 1) * (q - 1)
            if p != q and phi % e:
                break
        self.n, self.e = p * q, e
        self.d = pow(e, -1, phi)
        self.kid = secrets.token_hex(4)

    def jwk(self) -> dict:
        size = (self.n.bit_length() + 7) // 8
        return {"kty": "RSA", "kid": self.kid, "alg": "RS256", "use": "sig",
                "n": _b64(self.n.to_bytes(size, "big")), "e": _b64(self.e.to_bytes(3, "big"))}

    def sign(self, claims: dict) -> str:
        head = _b64(json.dumps({"alg": "RS256", "typ": "JWT", "kid": self.kid}).encode())
        body = _b64(json.dumps(claims).encode())
        size = (self.n.bit_length() + 7) // 8
        digest = bytes.fromhex("3031300d060960864801650304020105000420") + hashlib.sha256(f"{head}.{body}".encode()).digest()
        padded = b"\x00\x01" + b"\xff" * (size - len(digest) - 3) + b"\x00" + digest
        signature = pow(int.from_bytes(padded, "big"), self.d, self.n).to_bytes(size, "big")
        return f"{head}.{body}.{_b64(signature)}"


class _QuickServer(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        self.server_name = "127.0.0.1"
        self.server_port = self.server_address[1]


_KEYS: list[RsaKey] = []


def _keys() -> tuple[RsaKey, RsaKey]:
    """Two keys per test run (making them is the slow part)."""
    while len(_KEYS) < 2:
        _KEYS.append(RsaKey())
    return _KEYS[0], _KEYS[1]


class FakeIssuer:
    def __init__(self) -> None:
        self.key, self.other_key = _keys()
        self.log: list[tuple[str, dict]] = []
        self.deny = False
        self.grant_plan = True
        self.wrong_state = False
        self.bad_signature = False
        self.fail_refresh = ""
        self.subject = "user-123"
        self.email = "nick@example.com"
        self.issued_client = "oaiapp_" + secrets.token_hex(6)
        self.codes: dict[str, dict] = {}
        self.refresh_tokens: dict[str, str] = {}   # refresh token -> client id (current ones only)
        self.revoked: list[str] = []
        self.url = ""
        self._server: _QuickServer | None = None

    # The person's browser: follow the authorize URL to the loopback callback.
    def browser(self, url: str) -> bool:
        def go() -> None:
            try:
                with urllib.request.urlopen(url, timeout=10) as response:
                    response.read()
            except Exception:  # noqa: BLE001 - the flow under test reports the outcome
                pass
        threading.Thread(target=go, daemon=True).start()
        return True

    def start(self) -> str:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:
                pass

            def _json(self, status: int, payload: dict) -> None:
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:  # noqa: N802
                parsed = urllib.parse.urlsplit(self.path)
                query = {k: v[-1] for k, v in urllib.parse.parse_qs(parsed.query).items()}
                fake.log.append((parsed.path, query))
                if parsed.path == "/.well-known/openid-configuration":
                    self._json(200, {"issuer": fake.url, "authorization_endpoint": fake.url + "/api/accounts/authorize",
                                     "token_endpoint": fake.url + "/api/accounts/oauth/token",
                                     "jwks_uri": fake.url + "/jwks", "revocation_endpoint": fake.url + "/revoke"})
                elif parsed.path == "/jwks":
                    self._json(200, {"keys": [fake.key.jwk()]})
                elif parsed.path == "/api/accounts/authorize":
                    self._authorize(query)
                else:
                    self._json(404, {"error": "not_found"})

            def _authorize(self, q: dict) -> None:
                redirect = q.get("redirect_uri", "")
                back: dict[str, str] = {"state": "not-yours" if fake.wrong_state else q.get("state", "")}
                if fake.deny:
                    back["error"] = "access_denied"
                else:
                    code = secrets.token_urlsafe(16)
                    registering = q.get("client_id") == "dynamic_agent_client"
                    fake.codes[code] = {"challenge": q.get("code_challenge"), "redirect_uri": redirect,
                                        "nonce": q.get("nonce"), "client_id": fake.issued_client}
                    back["code"] = code
                    scopes = q.get("scope", "").split()
                    if not fake.grant_plan:
                        scopes = [s for s in scopes if s != PLAN_SCOPE]
                    back["scope"] = " ".join(scopes)
                    if registering:
                        back["client_id"] = fake.issued_client
                self.send_response(302)
                self.send_header("Location", redirect + "?" + urllib.parse.urlencode(back))
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                form = {k: v[-1] for k, v in urllib.parse.parse_qs(self.rfile.read(length).decode()).items()}
                fake.log.append((self.path, form))
                if self.path == "/revoke":
                    fake.revoked.append(form.get("token", ""))
                    fake.refresh_tokens.pop(form.get("token", ""), None)
                    self.send_response(200)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if self.path != "/api/accounts/oauth/token":
                    self._json(404, {"error": "not_found"})
                    return
                if form.get("resource") != "https://api.openai.com/v1":
                    self._json(400, {"error": "invalid_target"})
                    return
                if form.get("grant_type") == "authorization_code":
                    grant = fake.codes.pop(form.get("code", ""), None)
                    if grant is None:
                        self._json(400, {"error": "invalid_grant"})
                        return
                    challenge = _b64(hashlib.sha256(form.get("code_verifier", "").encode()).digest())
                    if challenge != grant["challenge"] or form.get("redirect_uri") != grant["redirect_uri"] \
                            or form.get("client_id") != grant["client_id"]:
                        self._json(400, {"error": "invalid_grant"})
                        return
                    self._json(200, fake._tokens(grant["client_id"], grant["nonce"]))
                    return
                if form.get("grant_type") == "refresh_token":
                    if fake.fail_refresh:
                        self._json(400, {"error": fake.fail_refresh})
                        return
                    token = form.get("refresh_token", "")
                    if fake.refresh_tokens.get(token) != form.get("client_id"):
                        self._json(400, {"error": "refresh_token_reused"})
                        return
                    del fake.refresh_tokens[token]   # rotation: each one works once
                    self._json(200, fake._tokens(form["client_id"], None))
                    return
                self._json(400, {"error": "unsupported_grant_type"})

        self._server = _QuickServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_port}"
        threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True,
                         name="fake-issuer").start()
        return self.url

    def _tokens(self, client_id: str, nonce: str | None) -> dict:
        refresh = "rt_" + secrets.token_hex(8)
        self.refresh_tokens[refresh] = client_id
        scopes = "openid profile email offline_access resource.invoke" + (f" {PLAN_SCOPE}" if self.grant_plan else "")
        out = {"access_token": "at_" + secrets.token_hex(8), "refresh_token": refresh, "token_type": "Bearer",
               "expires_in": 3600, "scope": scopes}
        if nonce is not None:
            now = int(time.time())
            claims = {"iss": self.url, "aud": client_id, "sub": self.subject, "email": self.email, "nonce": nonce,
                      "iat": now, "exp": now + 3600}
            out["id_token"] = (self.other_key if self.bad_signature else self.key).sign(claims)
        return out

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
