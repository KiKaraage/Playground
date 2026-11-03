# SPDX-License-Identifier: Apache-2.0
"""Usage and cost for the services in a project's stack (``.playground/stack.toml``).

Each ``[[services]]`` entry names a ``usage_source`` adapter:

    cloudflare   R2 storage and Class A/B operations (GraphQL analytics API);
                 secrets: an API token (a name containing TOKEN) and the account
                 id (a name containing ACCOUNT, or ``account_id`` on the service);
                 optional ``bucket``
    aws          month-to-date cost from Cost Explorer through the ``aws`` CLI;
                 secrets: AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY (AWS_SESSION_TOKEN)
    resend       emails sent this month (Resend API); secret: the API key
    postgres     database size through ``psql``; secret: the connection URL
    http         up or down and latency for the service's ``url`` (or its live
                 environment when that is an address); no secrets
    manual       numbers that people or agents record (``record_usage``)

``refresh(project_dir)`` runs the adapters in a worker thread. A good reading
is appended to ``.playground/usage/services.jsonl`` as numbers only
(``{ts, service_id, period, metrics, cost, currency, source}``); what happened,
in plain words, goes to ``.playground/local/services-status.json``. Then
``usage.changed`` is published. ``money_summary`` is what the Usage & money
section shows: this month's cost against budget per service and in total, and
the trend.

Threat notes
    * Read-only. Adapters only ask; nothing here writes to a provider. Postgres
      sessions are opened with ``default_transaction_read_only=on``.
    * Credentials come from the vault, by the names listed on the service, and
      live only in memory for one request: in an HTTP header, or in the
      environment of one ``aws``/``psql`` child (never in argv, where any user
      can read them with ``ps``). Status text is scrubbed of every credential
      value before it is stored or shown.
    * Provider endpoints are constants in this module, never read from
      stack.toml. Otherwise anyone who can edit the synced project could point
      a vault token at their own server. Credentialed requests must be HTTPS
      (loopback is allowed, for tests) and refuse redirects, because urllib
      would carry the Authorization header to the redirect's host.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
import tomllib
from typing import Any, Callable, Iterable
import urllib.error
import urllib.parse
import urllib.request

from . import events
from .paths import ProjectPaths
from .model import ServiceUsage
from .store import ProjectStore, atomic_write_json, read_json

ADAPTERS = ("cloudflare", "aws", "resend", "postgres", "http", "manual")
KINDS = ("email", "database", "storage", "hosting", "auth", "payments", "analytics", "dns", "ai", "messaging", "other")

# Endpoints are fixed here on purpose (see the threat notes). Tests patch these.
CLOUDFLARE_GRAPHQL = "https://api.cloudflare.com/client/v4/graphql"
RESEND_API = "https://api.resend.com"
TIMEOUT = 20.0
RESEND_MAX_PAGES = 50

# Cloudflare R2 list prices (USD), for an estimate when the bill isn't available.
R2_STORAGE_PER_GB_MONTH = 0.015
R2_FREE_GB = 10
R2_CLASS_A_PER_MILLION = 4.50
R2_FREE_CLASS_A = 1_000_000
R2_CLASS_B_PER_MILLION = 0.36
R2_FREE_CLASS_B = 10_000_000
R2_CLASS_A = {"ListBuckets", "PutBucket", "ListObjects", "PutObject", "CopyObject", "CompleteMultipartUpload",
              "CreateMultipartUpload", "UploadPart", "UploadPartCopy", "ListMultipartUploads", "ListParts",
              "PutBucketEncryption", "PutBucketCors", "PutBucketLifecycleConfiguration", "PutBucketStorageClass",
              "LifecycleStorageTierTransition"}
R2_FREE_OPS = {"DeleteObject", "DeleteBucket", "AbortMultipartUpload"}

SecretLookup = Callable[[str], "str | None"]


@dataclass
class Reading:
    """What one adapter run found. Only ``snapshot()`` goes into the project's synced files."""
    service_id: str
    source: str
    ok: bool
    status: str                         # plain words
    period: str = ""
    metrics: dict[str, float] = field(default_factory=dict)
    cost: float | None = None
    currency: str = "USD"
    estimated: bool = False
    ts: str = ""

    def snapshot(self) -> ServiceUsage:
        metrics = {k: v for k, v in self.metrics.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
        if self.estimated:
            metrics["cost_estimated"] = 1
        return ServiceUsage(service_id=self.service_id, period=self.period, metrics=metrics,
                            cost=None if self.cost is None else round(float(self.cost), 4),
                            currency=self.currency, source=self.source, ts=self.ts)


class AdapterError(Exception):
    """A plain-words reason an adapter couldn't read usage."""


# ───────────────────────────── time ─────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def period_of(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m")


def month_start(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _previous_period(period: str, back: int = 1) -> str:
    year, month = (int(x) for x in period.split("-"))
    month -= back
    while month <= 0:
        month += 12
        year -= 1
    return f"{year:04d}-{month:02d}"


# ───────────────────────────── stack.toml ─────────────────────────────

def stack_path(project_dir: Path | str) -> Path:
    return ProjectPaths(project_dir).meta / "stack.toml"


def snapshots_path(project_dir: Path | str) -> Path:
    return ProjectPaths(project_dir).meta / "usage" / "services.jsonl"


def status_path(project_dir: Path | str) -> Path:
    return ProjectPaths(project_dir).local_dir / "services-status.json"


def list_services(project_dir: Path | str) -> list[dict]:
    """The ``[[services]]`` of stack.toml (through the store when it knows how)."""
    store = ProjectStore(project_dir)
    reader = getattr(store, "read_stack", None)
    if callable(reader):
        try:
            stack = reader()
            services = stack.get("services") if isinstance(stack, dict) else getattr(stack, "services", None)
            if services is not None:
                return [s if isinstance(s, dict) else s.to_dict() for s in services]
        except Exception:  # noqa: BLE001 - fall back to reading the file ourselves
            pass
    try:
        document = tomllib.loads(stack_path(project_dir).read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return []
    return [s for s in document.get("services") or [] if isinstance(s, dict) and s.get("id")]


def get_service(project_dir: Path | str, service_id: str) -> dict | None:
    return next((s for s in list_services(project_dir) if s.get("id") == service_id), None)


def _adapter_for(service: dict) -> str:
    source = str(service.get("usage_source") or "manual").lower()
    return source if source in ADAPTERS else "manual"


def _provider_name(service: dict) -> str:
    return str(service.get("provider") or service.get("name") or "this service")


# ───────────────────────────── credentials ─────────────────────────────

def vault_lookup(project_dir: Path | str) -> SecretLookup:
    """Secrets by name from the project vault. Never raises: a missing secret is None."""
    vault = None
    try:
        from . import security
        project_id = ProjectStore(project_dir).read_project().id
        if security.crypto_available()[0] and project_id:
            vault = security.Vault(project_dir, project_id)
            if not vault.path.exists():
                vault = None
    except Exception:  # noqa: BLE001
        vault = None

    def lookup(name: str) -> str | None:
        if vault is None:
            return None
        try:
            return vault.get(name)
        except Exception:  # noqa: BLE001 - not there, not a member, locked: all "no credential"
            return None

    return lookup


def _credentials(service: dict, lookup: SecretLookup) -> dict[str, str]:
    names = service.get("secrets") or []
    if isinstance(names, str):
        names = [names]
    found = {}
    for name in names:
        if isinstance(name, str) and re.match(r"^[A-Za-z_][A-Za-z0-9_.-]{0,127}$", name):
            value = lookup(name)
            if value:
                found[name] = value
    return found


def _pick(creds: dict[str, str], *needles: str) -> str | None:
    for name, value in creds.items():
        if any(n in name.upper() for n in needles):
            return value
    return None


def _missing(service: dict, what: str) -> AdapterError:
    return AdapterError(f"Add the {_provider_name(service)} {what} in Places → Vault to see live usage.")


def redact(text: str, creds: Iterable[str]) -> str:
    """Remove every credential value (and its URL-encoded form) from text meant for people."""
    for value in sorted({c for c in creds if c and len(c) >= 4}, key=len, reverse=True):
        for form in {value, urllib.parse.quote(value, safe=""), urllib.parse.quote_plus(value)}:
            text = text.replace(form, "•••")
    return text


# ───────────────────────────── HTTP ─────────────────────────────

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        raise AdapterError("The provider sent Playground somewhere else, so it stopped to keep the key safe.")


_OPENER = urllib.request.build_opener(_NoRedirect)


def _check_endpoint(url: str) -> None:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme == "https":
        return
    if parts.scheme == "http" and parts.hostname in ("127.0.0.1", "localhost", "::1"):
        return
    raise AdapterError("Playground only sends keys over a secure connection.")


def _request_json(url: str, *, token: str, method: str = "GET", body: dict | None = None,
                  provider: str = "the provider") -> Any:
    _check_endpoint(url)
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/json", "User-Agent": "Playground usage",
        **({"Content-Type": "application/json"} if data else {})})
    try:
        with _OPENER.open(request, timeout=TIMEOUT) as response:
            return json.loads(response.read(4 * 1024 * 1024) or b"null")
    except urllib.error.HTTPError as exc:
        with exc:
            if exc.code in (401, 403):
                raise AdapterError(f"{provider} didn't accept the key. Check it in Places → Vault, "
                                   "and that it can read usage.") from None
            if exc.code == 429:
                raise AdapterError(f"{provider} asked Playground to slow down. It will try again later.") from None
            raise AdapterError(f"{provider} answered with an error ({exc.code}).") from None
    except AdapterError:
        raise
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise AdapterError(f"Couldn't reach {provider}. Is this computer online?") from exc
    except ValueError as exc:
        raise AdapterError(f"{provider} sent something Playground couldn't read.") from exc


# ───────────────────────────── adapters ─────────────────────────────

def _cloudflare(service: dict, creds: dict[str, str], now: datetime) -> Reading:
    token = _pick(creds, "TOKEN", "API_KEY", "KEY")
    account = str(service.get("account_id") or "") or _pick(creds, "ACCOUNT")
    if not token:
        raise _missing(service, "token")
    if not account or not re.match(r"^[A-Za-z0-9]{8,64}$", account):
        raise _missing(service, "account id")
    start = month_start(now)
    variables: dict[str, Any] = {"accountTag": account, "start": _iso(start), "end": _iso(now)}
    bucket = str(service.get("bucket") or "")
    bucket_filter = ", bucketName: $bucket" if bucket else ""
    query = (
        "query R2Usage($accountTag: string!, $start: Time!, $end: Time!" + (", $bucket: string" if bucket else "")
        + ") { viewer { accounts(filter: {accountTag: $accountTag}) {"
        " storage: r2StorageAdaptiveGroups(limit: 10000, filter: {datetime_geq: $start, datetime_leq: $end"
        + bucket_filter + "}, orderBy: [datetime_DESC]) {"
        " max { payloadSize metadataSize objectCount } dimensions { bucketName datetime } }"
        " operations: r2OperationsAdaptiveGroups(limit: 10000, filter: {datetime_geq: $start, datetime_leq: $end"
        + bucket_filter + "}) { sum { requests } dimensions { actionType } } } } }")
    if bucket:
        variables["bucket"] = bucket
    reply = _request_json(CLOUDFLARE_GRAPHQL, token=token, method="POST",
                          body={"query": query, "variables": variables}, provider="Cloudflare")
    if not isinstance(reply, dict):
        raise AdapterError("Cloudflare sent something Playground couldn't read.")
    if reply.get("errors"):
        raise AdapterError("Cloudflare couldn't share R2 usage. The token may need the Account Analytics permission.")
    try:
        accounts = reply["data"]["viewer"]["accounts"]
    except (KeyError, TypeError):
        raise AdapterError("Cloudflare sent something Playground couldn't read.") from None
    if not accounts:
        raise AdapterError("Cloudflare doesn't know that account id, or the token can't see it.")
    account_data = accounts[0]
    latest: dict[str, dict] = {}
    for group in account_data.get("storage") or []:
        name = (group.get("dimensions") or {}).get("bucketName", "")
        when = (group.get("dimensions") or {}).get("datetime", "")
        if name not in latest or when > latest[name]["when"]:
            latest[name] = {"when": when, **(group.get("max") or {})}
    payload = sum(float(b.get("payloadSize") or 0) + float(b.get("metadataSize") or 0) for b in latest.values())
    objects = sum(int(b.get("objectCount") or 0) for b in latest.values())
    class_a = class_b = 0
    for group in account_data.get("operations") or []:
        action = (group.get("dimensions") or {}).get("actionType", "")
        requests = int((group.get("sum") or {}).get("requests") or 0)
        if action in R2_FREE_OPS:
            continue
        if action in R2_CLASS_A:
            class_a += requests
        else:
            class_b += requests
    storage_gb = payload / 1e9
    cost = (max(0.0, storage_gb - R2_FREE_GB) * R2_STORAGE_PER_GB_MONTH
            + max(0, class_a - R2_FREE_CLASS_A) / 1e6 * R2_CLASS_A_PER_MILLION
            + max(0, class_b - R2_FREE_CLASS_B) / 1e6 * R2_CLASS_B_PER_MILLION)
    return Reading(service["id"], "cloudflare", True,
                   f"{storage_gb:,.1f} GB stored · {class_a:,} writes and {class_b:,} reads this month.",
                   metrics={"storage_gb": round(storage_gb, 3), "objects": objects, "class_a_ops": class_a,
                            "class_b_ops": class_b, "buckets": len(latest)},
                   cost=round(cost, 2), estimated=True)


def _aws(service: dict, creds: dict[str, str], now: datetime) -> Reading:
    key_id = creds.get("AWS_ACCESS_KEY_ID") or _pick(creds, "ACCESS_KEY_ID")
    secret = creds.get("AWS_SECRET_ACCESS_KEY") or _pick(creds, "SECRET_ACCESS_KEY", "SECRET")
    if not key_id or not secret:
        raise _missing(service, "access keys")
    aws = shutil.which("aws")
    if not aws:
        raise AdapterError("To see AWS costs here, install the AWS command line tool (aws).")
    start = month_start(now).date()
    end = (now.astimezone(timezone.utc) + timedelta(days=1)).date()
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/tmp"),
           "AWS_ACCESS_KEY_ID": key_id, "AWS_SECRET_ACCESS_KEY": secret, "AWS_DEFAULT_REGION": "us-east-1",
           "AWS_PAGER": "", "AWS_CONFIG_FILE": os.devnull, "AWS_SHARED_CREDENTIALS_FILE": os.devnull}
    session = creds.get("AWS_SESSION_TOKEN") or _pick(creds, "SESSION_TOKEN")
    if session:
        env["AWS_SESSION_TOKEN"] = session
    argv = [aws, "ce", "get-cost-and-usage", "--time-period", f"Start={start},End={end}",
            "--granularity", "MONTHLY", "--metrics", "UnblendedCost", "--output", "json"]
    try:
        proc = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise AdapterError("AWS took too long to answer.") from None
    if proc.returncode != 0:
        err = proc.stderr or ""
        if any(w in err for w in ("AccessDenied", "UnrecognizedClient", "InvalidClientTokenId", "SignatureDoesNotMatch")):
            raise AdapterError("AWS didn't accept those keys, or they can't read Cost Explorer (ce:GetCostAndUsage).")
        if "Could not connect" in err or "EndpointConnectionError" in err:
            raise AdapterError("Couldn't reach AWS. Is this computer online?")
        raise AdapterError("AWS couldn't share costs right now.")
    try:
        result = json.loads(proc.stdout)
        total = (result["ResultsByTime"][0]["Total"]["UnblendedCost"])
        amount, unit = float(total["Amount"]), str(total.get("Unit") or "USD")
    except (ValueError, KeyError, IndexError, TypeError):
        raise AdapterError("AWS sent something Playground couldn't read.") from None
    return Reading(service["id"], "aws", True, f"{_money(amount, unit)} so far this month.",
                   metrics={"cost_to_date": round(amount, 4)}, cost=round(amount, 2), currency=unit[:8])


def _resend(service: dict, creds: dict[str, str], now: datetime) -> Reading:
    key = _pick(creds, "RESEND", "API_KEY", "KEY", "TOKEN") or (next(iter(creds.values())) if creds else None)
    if not key:
        raise _missing(service, "API key")
    start = month_start(now)
    sent = bounced = 0
    after = None
    capped = True
    for _ in range(RESEND_MAX_PAGES):
        query = {"limit": "100", **({"after": after} if after else {})}
        reply = _request_json(f"{RESEND_API}/emails?{urllib.parse.urlencode(query)}", token=key, provider="Resend")
        rows = reply.get("data") if isinstance(reply, dict) else None
        if not isinstance(rows, list):
            raise AdapterError("Resend sent something Playground couldn't read.")
        reached_older = False
        for row in rows:
            created = _parse_time(row.get("created_at"))
            if created is None:
                continue
            if created < start:
                reached_older = True
                break
            sent += 1
            if str(row.get("last_event") or "") in ("bounced", "complained", "failed"):
                bounced += 1
        if reached_older or not reply.get("has_more") or not rows:
            capped = False
            break
        after = rows[-1].get("id")
        if not after:
            capped = False
            break
    words = f"{sent:,}{'+' if capped else ''} emails sent this month"
    if bounced:
        words += f" · {bounced} bounced or failed"
    return Reading(service["id"], "resend", True, words + ".",
                   metrics={"emails_sent": sent, "emails_bounced": bounced, **({"partial": 1} if capped else {})})


def _postgres(service: dict, creds: dict[str, str], now: datetime) -> Reading:
    url = _pick(creds, "DATABASE_URL", "POSTGRES", "PG", "URL") or (next(iter(creds.values())) if creds else None)
    if not url:
        raise _missing(service, "database URL")
    psql = shutil.which("psql")
    if not psql:
        raise AdapterError("To see the database size here, install the PostgreSQL client (psql).")
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("postgres", "postgresql"):
        raise AdapterError("The database secret should be a postgres:// address.")
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/tmp"),
           "PGCONNECT_TIMEOUT": "10", "PGAPPNAME": "playground-usage",
           "PGOPTIONS": "-c default_transaction_read_only=on -c statement_timeout=15000"}
    if parts.hostname:
        env["PGHOST"] = parts.hostname
    if parts.port:
        env["PGPORT"] = str(parts.port)
    if parts.username:
        env["PGUSER"] = urllib.parse.unquote(parts.username)
    if parts.password:
        env["PGPASSWORD"] = urllib.parse.unquote(parts.password)
    database = urllib.parse.unquote(parts.path.lstrip("/"))
    if database:
        env["PGDATABASE"] = database
    options = urllib.parse.parse_qs(parts.query)
    if options.get("sslmode"):
        env["PGSSLMODE"] = options["sslmode"][0]
    sql = ("select pg_database_size(current_database()), "
           "(select count(*) from pg_stat_activity where datname = current_database())")
    try:
        proc = subprocess.run([psql, "-X", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-c", sql], env=env,
                              capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise AdapterError("The database took too long to answer.") from None
    if proc.returncode != 0:
        err = proc.stderr or ""
        if "password authentication failed" in err or "authentication" in err:
            raise AdapterError("The database didn't accept that password. Check the URL in Places → Vault.")
        if "could not connect" in err.lower() or "Connection refused" in err or "timeout" in err.lower():
            raise AdapterError("Couldn't reach the database. Is it running, and reachable from this computer?")
        raise AdapterError("The database couldn't share its size right now.")
    try:
        size_text, conns_text = proc.stdout.strip().splitlines()[0].split("|")
        size, conns = int(size_text), int(conns_text)
    except (ValueError, IndexError):
        raise AdapterError("The database sent something Playground couldn't read.") from None
    return Reading(service["id"], "postgres", True, f"{_bytes(size)} of data · {conns} connected now.",
                   metrics={"size_bytes": size, "size_gb": round(size / 1e9, 3), "connections": conns})


def _http(service: dict, creds: dict[str, str], now: datetime) -> Reading:
    url = str(service.get("url") or "")
    if not url:
        live = (service.get("environments") or {}).get("live") if isinstance(service.get("environments"), dict) else ""
        url = str(live or "")
    if not re.match(r"^https?://[^\s]+$", url):
        raise AdapterError("Add the address to check (url) to this service.")
    request = urllib.request.Request(url, headers={"User-Agent": "Playground status check"})
    started = time.monotonic()
    status = 0
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310 - no credentials sent
            status = int(response.status)
    except urllib.error.HTTPError as exc:
        with exc:
            status = int(exc.code)
    except (urllib.error.URLError, OSError, TimeoutError, ValueError):
        status = 0
    latency = round((time.monotonic() - started) * 1000)
    up = 200 <= status < 400
    words = (f"Up · answered in {latency} ms." if up else
             f"Down · answered {status}." if status else "Down · nothing answered.")
    return Reading(service["id"], "http", True, words,
                   metrics={"up": 1 if up else 0, "status": status, "latency_ms": latency})


def _manual(service: dict, creds: dict[str, str], now: datetime) -> Reading:
    raise AdapterError("These numbers are added by hand or by an agent.")


_ADAPTER_FUNCS = {"cloudflare": _cloudflare, "aws": _aws, "resend": _resend, "postgres": _postgres,
                  "http": _http, "manual": _manual}


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    text = re.sub(r"(\.\d{6})\d+", r"\1", text)            # trim nanoseconds
    text = re.sub(r"([+-]\d{2})$", r"\1:00", text)          # "+00" → "+00:00"
    try:
        dt = datetime.fromisoformat(text.replace(" ", "T", 1))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ───────────────────────────── running ─────────────────────────────

def fetch(service: dict, lookup: SecretLookup, now: datetime | None = None) -> Reading:
    """Run one service's adapter. Never raises; failures come back as a plain-words status."""
    now = now or _now()
    adapter = _adapter_for(service)
    creds = _credentials(service, lookup) if adapter not in ("http", "manual") else {}
    try:
        reading = _ADAPTER_FUNCS[adapter](service, creds, now)
    except AdapterError as exc:
        reading = Reading(str(service.get("id")), adapter, False, redact(str(exc), creds.values()))
    except Exception:  # noqa: BLE001 - an adapter bug must not leak details or stop the others
        reading = Reading(str(service.get("id")), adapter, False,
                          f"Playground couldn't read {_provider_name(service)}'s usage this time.")
    reading.status = redact(reading.status, creds.values())
    reading.ts = _iso(now)
    reading.period = period_of(now)
    if reading.ok and reading.cost is None:
        fixed = _num(service.get("monthly_cost"))
        if fixed is not None:
            reading.cost = fixed
    creds.clear()
    return reading


def _append_snapshot(project_dir: Path | str, snapshot: ServiceUsage, bus: events.EventBus | None = None) -> dict:
    """Through the store, which checks it's numbers only and announces usage.changed."""
    return ProjectStore(project_dir, bus=bus).append_service_usage(snapshot).to_dict()


def record_usage(project_dir: Path | str, service_id: str, metrics: dict[str, Any], *, cost: float | None = None,
                 currency: str = "USD", source: str = "manual", when: datetime | None = None,
                 bus: events.EventBus | None = None) -> dict:
    """Record numbers a person or an agent reported (the ``manual`` adapter). Numbers only."""
    if get_service(project_dir, service_id) is None:
        raise ValueError(f"There's no service called {service_id} in this project's stack.")
    clean = {str(k)[:40]: v for k, v in (metrics or {}).items()
             if isinstance(v, (int, float)) and not isinstance(v, bool)}
    now = when or _now()
    reading = Reading(service_id, source if source in ("manual", "agent", "person") else "manual", True,
                      "Recorded.", period=period_of(now), metrics=clean, cost=_num(cost), currency=str(currency)[:8],
                      ts=_iso(now))
    return _append_snapshot(project_dir, reading.snapshot(), bus)


def refresh_now(project_dir: Path | str, service_id: str | None = None, *, lookup: SecretLookup | None = None,
                now: datetime | None = None, bus: events.EventBus | None = None) -> list[Reading]:
    """Run the adapters here and now (``refresh`` calls this from a worker thread)."""
    services = list_services(project_dir)
    if service_id:
        services = [s for s in services if s.get("id") == service_id]
    lookup = lookup or vault_lookup(project_dir)
    readings = []
    statuses = read_json(status_path(project_dir)) or {}
    for service in services:
        reading = fetch(service, lookup, now)
        readings.append(reading)
        if reading.ok:
            _append_snapshot(project_dir, reading.snapshot(), bus)
        statuses[str(service["id"])] = {"ok": reading.ok, "status": reading.status, "checked": reading.ts,
                                        "source": reading.source}
    status_path(project_dir).parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(status_path(project_dir), statuses)
    (bus if bus is not None else events.bus).publish(events.USAGE_CHANGED, project=str(project_dir),
                                                     service_id=service_id or "")
    return readings


def refresh(project_dir: Path | str, service_id: str | None = None, *, lookup: SecretLookup | None = None,
            bus: events.EventBus | None = None,
            done: Callable[[list[Reading]], Any] | None = None) -> threading.Thread:
    """Refresh usage in a worker thread. Listen for ``usage.changed``, or pass ``done``."""
    def work() -> None:
        readings = refresh_now(project_dir, service_id, lookup=lookup, bus=bus)
        if done is not None:
            done(readings)

    thread = threading.Thread(target=work, name="services-refresh", daemon=True)
    thread.start()
    return thread


# ───────────────────────────── summaries ─────────────────────────────

def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _money(value: float, currency: str = "USD") -> str:
    symbol = {"USD": "$", "EUR": "€", "GBP": "£"}.get(currency, "")
    return f"{symbol}{value:,.2f}" if symbol else f"{value:,.2f} {currency}"


def _bytes(size: float) -> str:
    for unit, scale in (("TB", 1e12), ("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if size >= scale:
            return f"{size / scale:,.1f} {unit}"
    return f"{int(size)} bytes"


def snapshots(project_dir: Path | str, service_id: str | None = None) -> list[dict]:
    return [u.to_dict() for u in ProjectStore(project_dir).read_service_usage(service_id)]


def latest(project_dir: Path | str, service_id: str, period: str | None = None) -> dict | None:
    return _latest_in(snapshots(project_dir, service_id), period)


def _month_cost(rows: list[dict], month: str) -> tuple[float | None, dict | None]:
    """A month's cost so far, and the reading it rests on: the month's own snapshot when there is one,
    otherwise the sum of that month's daily snapshots (the latest one for each day)."""
    monthly = _latest_in(rows, month)
    if monthly is not None:
        return _num(monthly.get("cost")), monthly
    days: dict[str, dict] = {}
    for row in rows:
        period = str(row.get("period") or "")
        if len(period) == 10 and period.startswith(month + "-"):
            if period not in days or str(row.get("ts")) >= str(days[period].get("ts")):
                days[period] = row
    costs = [_num(row.get("cost")) for row in days.values()]
    known = [c for c in costs if c is not None]
    if not known:
        return None, None
    newest = max(days.values(), key=lambda row: str(row.get("ts")))
    return round(sum(known), 2), newest


def _latest_in(rows: list[dict], period: str | None = None) -> dict | None:
    best = None
    for row in rows:
        if period and row.get("period") != period:
            continue
        if best is None or str(row.get("ts")) >= str(best.get("ts")):
            best = row
    return best


def _trend_words(series: list[float | None]) -> str:
    known = [v for v in series if v is not None]
    if len(series) < 2 or series[-1] is None or series[-2] is None:
        return "" if len(known) < 2 else "Not enough months to compare yet."
    now, before = series[-1], series[-2]
    if before == 0:
        return "New this month." if now else "No cost yet."
    change = (now - before) / before * 100
    if abs(change) < 5:
        return "About the same as last month so far."
    return f"{'Up' if change > 0 else 'Down'} {abs(change):.0f}% on last month so far."


def money_summary(project_dir: Path | str, now: datetime | None = None, months: int = 6) -> dict:
    """This month's cost against budget, per service and in total, with a trend.

    Costs are month-to-date (the latest reading of each month). The trend is
    the last ``months`` months, oldest first.
    """
    now = now or _now()
    period = period_of(now)
    periods = [_previous_period(period, back) for back in range(months - 1, -1, -1)]
    statuses = read_json(status_path(project_dir)) or {}
    rows = []
    total_cost = 0.0
    total_budget = 0.0
    any_budget = False
    trend_total = [0.0] * len(periods)
    trend_known = [False] * len(periods)
    currencies = set()
    # usage/services.jsonl is read once here, not once per service and month (it was 42 reads for Kiln).
    by_service: dict[str, list[dict]] = {}
    for row in snapshots(project_dir):
        by_service.setdefault(str(row.get("service_id")), []).append(row)
    for service in list_services(project_dir):
        sid = str(service["id"])
        rows_for = by_service.get(sid, [])
        series: list[float | None] = []
        for index, p in enumerate(periods):
            cost, _row = _month_cost(rows_for, p)
            series.append(cost)
            if cost is not None:
                trend_total[index] += cost
                trend_known[index] = True
        current = _month_cost(rows_for, period)[1]
        cost = series[-1]
        if cost is None and _num(service.get("monthly_cost")) is not None:
            cost = _num(service.get("monthly_cost"))
        budget = _num(service.get("monthly_budget")) or None      # 0 means "no budget set"
        currency = str((current or {}).get("currency") or "USD")
        currencies.add(currency)
        if cost is not None:
            total_cost += cost
        if budget is not None:
            total_budget += budget
            any_budget = True
        status = statuses.get(sid, {})
        if cost is None:
            line = "No cost reported yet."
        elif budget:
            line = f"{_money(cost, currency)} of {_money(budget, currency)} budget"
            if cost > budget:
                line += f" · {_money(cost - budget, currency)} over"
        else:
            line = f"{_money(cost, currency)} so far this month"
        rows.append({
            "id": sid, "name": str(service.get("name") or sid), "provider": str(service.get("provider") or ""),
            "kind": str(service.get("kind") or "other"), "plan": str(service.get("plan") or ""),
            "source": _adapter_for(service), "cost": cost, "budget": budget, "currency": currency,
            "budget_used": round(cost / budget, 3) if (cost is not None and budget) else None,
            "over_budget": bool(cost is not None and budget is not None and cost > budget),
            "metrics": {k: v for k, v in ((current or {}).get("metrics") or {}).items() if k != "cost_estimated"},
            "estimated": bool(((current or {}).get("metrics") or {}).get("cost_estimated")),
            "updated": str((current or {}).get("ts") or ""), "status": str(status.get("status") or ""),
            "status_ok": status.get("ok"), "trend": series, "trend_words": _trend_words(series), "summary": line,
        })
    trend = [round(v, 2) if known else None for v, known in zip(trend_total, trend_known)]
    total_line = f"{_money(total_cost)} so far this month"
    if any_budget:
        total_line += f" of {_money(total_budget)} budget"
    return {"period": period, "periods": periods, "services": rows,
            "total": {"cost": round(total_cost, 2), "budget": round(total_budget, 2) if any_budget else None,
                      "currency": "USD" if currencies <= {"USD"} else "mixed",
                      "over_budget": any_budget and total_cost > total_budget,
                      "trend": trend, "trend_words": _trend_words(trend), "summary": total_line}}


__all__ = [
    "ADAPTERS", "KINDS", "Reading", "AdapterError", "list_services", "get_service", "vault_lookup", "fetch",
    "refresh", "refresh_now", "record_usage", "snapshots", "latest", "money_summary", "redact", "period_of",
]
