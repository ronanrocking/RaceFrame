from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import secrets
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Deque
from urllib.parse import urlsplit

from fastapi import Form, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .config import settings


logger = logging.getLogger(__name__)

PRODUCTION_ENVIRONMENTS = {"production", "prod", "staging"}
KNOWN_ENVIRONMENTS = PRODUCTION_ENVIRONMENTS | {"development", "dev", "test"}
CSRF_COOKIE = "raceframe_csrf"
VISITOR_COOKIE = "raceframe_visitor"
SEARCH_COOKIE_PREFIX = "raceframe_search_"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def is_production() -> bool:
    return settings.environment in PRODUCTION_ENVIRONMENTS


def secure_cookies() -> bool:
    return is_production() or bool(settings.public_origins) and all(
        origin.lower().startswith("https://") for origin in settings.public_origins
    )


def validate_production_configuration() -> None:
    """Refuse to boot with development defaults in a production environment."""

    if settings.environment not in KNOWN_ENVIRONMENTS:
        raise RuntimeError(f"Unknown APP_ENV value: {settings.environment!r}")
    if not is_production():
        looks_deployed = not settings.database_url.lower().startswith("sqlite") and bool(settings.r2_bucket_name)
        if looks_deployed and settings.environment in {"development", "dev"}:
            raise RuntimeError("APP_ENV must explicitly identify a deployed environment.")
        return

    problems: list[str] = []
    if settings.database_url.lower().startswith("sqlite"):
        problems.append("DATABASE_URL must use a production database, not SQLite")
    if len(settings.app_secret_key.encode("utf-8")) < 32:
        problems.append("RACEFRAME_SECRET_KEY must contain at least 32 bytes")
    if len(settings.worker_api_token.encode("utf-8")) < 32:
        problems.append("WORKER_API_TOKEN must contain at least 32 bytes")
    if len(settings.metrics_api_token.encode("utf-8")) < 32:
        problems.append("METRICS_API_TOKEN must contain at least 32 bytes")
    if not settings.allowed_hosts or any(
        "*" in host or "://" in host or "/" in host for host in settings.allowed_hosts
    ):
        problems.append("ALLOWED_HOSTS must contain explicit hostnames")
    if not settings.public_origins:
        problems.append("PUBLIC_ORIGINS must contain the public HTTPS origin")
    for origin in settings.public_origins:
        parsed = urlsplit(origin)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
        ):
            problems.append(f"PUBLIC_ORIGINS contains an invalid production origin: {origin!r}")
        elif parsed.hostname.lower() not in {host.lower() for host in settings.allowed_hosts}:
            problems.append(f"PUBLIC_ORIGINS host is missing from ALLOWED_HOSTS: {parsed.hostname!r}")
    r2_values = {
        "CLOUDFLARE_R2_ACCOUNT_ID": settings.r2_account_id,
        "CLOUDFLARE_R2_BUCKET_NAME": settings.r2_bucket_name,
        "CLOUDFLARE_R2_ENDPOINT": settings.r2_endpoint,
        "CLOUDFLARE_R2_ACCESS_KEY_ID": settings.r2_access_key_id,
        "CLOUDFLARE_R2_SECRET_ACCESS_KEY": settings.r2_secret_access_key,
    }
    missing_r2 = [name for name, value in r2_values.items() if not value]
    if missing_r2:
        problems.append(f"R2 configuration is incomplete: {', '.join(missing_r2)}")
    if settings.r2_endpoint and not settings.r2_endpoint.lower().startswith("https://"):
        problems.append("CLOUDFLARE_R2_ENDPOINT must use HTTPS")

    positive_limits = {
        "MAX_PHOTO_UPLOAD_BYTES": settings.max_photo_upload_bytes,
        "MAX_SELFIE_UPLOAD_BYTES": settings.max_selfie_upload_bytes,
        "MAX_PHOTO_BATCH_FILES": settings.max_photo_batch_files,
        "MAX_SELFIE_BATCH_FILES": settings.max_selfie_batch_files,
        "MAX_FACE_SEARCH_BACKLOG": settings.max_face_search_backlog,
        "MAX_PHOTO_JOB_BACKLOG": settings.max_photo_job_backlog,
        "SEARCH_CAPABILITY_TTL_SECONDS": settings.search_capability_ttl_seconds,
        "MAX_PHOTO_REQUEST_BYTES": settings.max_photo_request_bytes,
        "MAX_FORM_REQUEST_BYTES": settings.max_form_request_bytes,
        "WORKER_HEARTBEAT_STALE_SECONDS": settings.worker_heartbeat_stale_seconds,
        "WORKER_LEASE_SECONDS": settings.worker_lease_seconds,
        "WORKER_MAX_ATTEMPTS": settings.worker_max_attempts,
        "WORKER_RETRY_BASE_SECONDS": settings.worker_retry_base_seconds,
        "WORKER_RETRY_MAX_SECONDS": settings.worker_retry_max_seconds,
        "DELETION_TASK_RETENTION_DAYS": settings.deletion_task_retention_days,
        "WORKER_HEARTBEAT_RETENTION_DAYS": settings.worker_heartbeat_retention_days,
    }
    problems.extend(f"{name} must be positive" for name, value in positive_limits.items() if value <= 0)
    if not 30 <= settings.worker_lease_seconds <= 3_600:
        problems.append("WORKER_LEASE_SECONDS must be between 30 and 3600")
    if not 1 <= settings.worker_max_attempts <= 20:
        problems.append("WORKER_MAX_ATTEMPTS must be between 1 and 20")
    if not 1 <= settings.worker_retry_base_seconds <= 3_600:
        problems.append("WORKER_RETRY_BASE_SECONDS must be between 1 and 3600")
    if not settings.worker_retry_base_seconds <= settings.worker_retry_max_seconds <= 86_400:
        problems.append("WORKER_RETRY_MAX_SECONDS must be between the retry base and 86400")

    if problems:
        raise RuntimeError("Unsafe production configuration:\n- " + "\n- ".join(problems))


_EPHEMERAL_SECRET = secrets.token_bytes(32)


def _signing_key() -> bytes:
    configured = settings.app_secret_key.encode("utf-8")
    return configured or _EPHEMERAL_SECRET


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def secret_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def owner_binding_hash(visitor_id: str) -> str:
    return hmac.new(_signing_key(), visitor_id.encode("utf-8"), hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class SearchCapability:
    session_id: str
    event_id: str
    secret: str
    expires_at: datetime
    capability_hash: str
    owner_hash: str


def issue_search_capability(*, session_id: str, event_id: str, visitor_id: str) -> SearchCapability:
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=settings.search_capability_ttl_seconds)
    raw_secret = secrets.token_urlsafe(32)
    bound_owner_hash = owner_binding_hash(visitor_id)
    payload = {
        "sid": str(session_id),
        "eid": str(event_id),
        "cap": raw_secret,
        "exp": int(expires_at.timestamp()),
        "own": bound_owner_hash,
    }
    encoded_payload = _b64encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    signature = _b64encode(hmac.new(_signing_key(), encoded_payload.encode("ascii"), hashlib.sha256).digest())
    return SearchCapability(
        session_id=str(session_id),
        event_id=str(event_id),
        secret=f"{encoded_payload}.{signature}",
        expires_at=expires_at,
        capability_hash=secret_hash(raw_secret),
        owner_hash=bound_owner_hash,
    )


def decode_search_capability(value: str, *, event_id: str, visitor_id: str) -> SearchCapability | None:
    try:
        encoded_payload, supplied_signature = value.split(".", 1)
        expected_signature = _b64encode(
            hmac.new(_signing_key(), encoded_payload.encode("ascii"), hashlib.sha256).digest()
        )
        if not hmac.compare_digest(supplied_signature, expected_signature):
            return None
        payload = json.loads(_b64decode(encoded_payload))
        expires_at = datetime.fromtimestamp(int(payload["exp"]), tz=timezone.utc)
        if expires_at <= datetime.now(timezone.utc):
            return None
        if not hmac.compare_digest(str(payload["eid"]), str(event_id)):
            return None
        bound_owner_hash = owner_binding_hash(visitor_id)
        if not hmac.compare_digest(str(payload["own"]), bound_owner_hash):
            return None
        raw_secret = str(payload["cap"])
        session_id = str(payload["sid"])
    except (ValueError, TypeError, KeyError, json.JSONDecodeError, UnicodeError):
        return None

    return SearchCapability(
        session_id=session_id,
        event_id=str(event_id),
        secret=value,
        expires_at=expires_at,
        capability_hash=secret_hash(raw_secret),
        owner_hash=bound_owner_hash,
    )


def search_cookie_name(event_id: str) -> str:
    compact_id = "".join(character for character in str(event_id).lower() if character.isalnum())
    return f"{SEARCH_COOKIE_PREFIX}{compact_id[:40]}"


def set_search_capability_cookie(response: Response, *, event_id: str, capability: SearchCapability) -> None:
    response.set_cookie(
        search_cookie_name(event_id),
        capability.secret,
        max_age=settings.search_capability_ttl_seconds,
        expires=capability.expires_at,
        path=f"/user/events/{event_id}",
        secure=secure_cookies(),
        httponly=True,
        samesite="lax",
    )


def request_search_capability(request: Request, *, event_id: str) -> SearchCapability | None:
    visitor_id = getattr(request.state, "visitor_id", "")
    cookie_value = request.cookies.get(search_cookie_name(event_id), "")
    if not visitor_id or not cookie_value:
        return None
    return decode_search_capability(cookie_value, event_id=event_id, visitor_id=visitor_id)


def _normalized_origin(value: str) -> str | None:
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            return None
        default_port = 80 if parsed.scheme == "http" else 443
        port = parsed.port or default_port
    except ValueError:
        return None
    suffix = "" if port == default_port else f":{port}"
    return f"{parsed.scheme}://{parsed.hostname.lower()}{suffix}"


def _allowed_origins(request: Request) -> set[str]:
    origins = {origin for value in settings.public_origins if (origin := _normalized_origin(value))}
    if not is_production():
        host = request.headers.get("host", "")
        if host:
            origins.add(f"{request.url.scheme}://{host.lower()}")
    return origins


def _request_origin_is_allowed(request: Request) -> bool:
    """Validate a supplied browser origin while tolerating privacy-stripped headers.

    The double-submit CSRF token remains mandatory. Some browsers and privacy
    extensions omit both Origin and Referer; rejecting those requests makes every
    legitimate form action unusable without adding meaningful protection beyond
    the unguessable CSRF token.
    """
    source = request.headers.get("origin") or request.headers.get("referer")
    if source is None:
        return True
    supplied_origin = _normalized_origin(source)
    return supplied_origin is not None and supplied_origin in _allowed_origins(request)


async def require_browser_csrf(
    request: Request,
    csrf_token: str | None = Form(default=None),
    x_csrf_token: str | None = Header(default=None),
) -> None:
    """Double-submit CSRF protection plus strict Origin/Referer validation."""

    if request.method.upper() in SAFE_METHODS:
        return
    expected_token = request.cookies.get(CSRF_COOKIE, "")
    supplied_token = x_csrf_token or csrf_token or ""
    if not expected_token or not supplied_token or not hmac.compare_digest(expected_token, supplied_token):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid or missing CSRF token.")

    if not _request_origin_is_allowed(request):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Request origin is not allowed.")


class SlidingWindowLimiter:
    """Small process-local guard; edge/Redis limits should supplement it when horizontally scaled."""

    def __init__(self, *, max_keys: int = 50_000) -> None:
        self.max_keys = max_keys
        self._entries: OrderedDict[tuple[str, str], Deque[float]] = OrderedDict()
        self._lock = asyncio.Lock()

    async def hit(self, *, bucket: str, key: str, limit: int, window_seconds: int) -> int | None:
        now = time.monotonic()
        cutoff = now - window_seconds
        entry_key = (bucket, key)
        async with self._lock:
            timestamps = self._entries.setdefault(entry_key, deque())
            while timestamps and timestamps[0] <= cutoff:
                timestamps.popleft()
            self._entries.move_to_end(entry_key)
            if len(timestamps) >= limit:
                return max(1, int(window_seconds - (now - timestamps[0])) + 1)
            timestamps.append(now)
            while len(self._entries) > self.max_keys:
                self._entries.popitem(last=False)
        return None


class InFlightLimiter:
    def __init__(self) -> None:
        self._counts: dict[str, int] = {}
        self._lock = asyncio.Lock()

    async def enter(self, category: str, limit: int) -> bool:
        async with self._lock:
            current = self._counts.get(category, 0)
            if current >= limit:
                return False
            self._counts[category] = current + 1
            return True

    async def leave(self, category: str) -> None:
        async with self._lock:
            current = self._counts.get(category, 0)
            if current <= 1:
                self._counts.pop(category, None)
            else:
                self._counts[category] = current - 1


class _RequestBodyTooLarge(Exception):
    pass


class RequestBodyLimitMiddleware:
    """Enforce request limits while bytes arrive, before multipart data is spooled."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    def _limit_for(self, scope: Scope) -> int | None:
        if scope.get("type") != "http" or scope.get("method", "GET").upper() in SAFE_METHODS:
            return None
        path = scope.get("path", "")
        if path.startswith("/internal/worker/"):
            return None
        overhead = 1024 * 1024
        if path.startswith("/upload/events/"):
            aggregate = settings.max_photo_upload_bytes * settings.max_photo_batch_files + overhead
            return min(aggregate, settings.max_photo_request_bytes)
        if path.startswith("/user/events/") and path.endswith("/selfies"):
            return settings.max_selfie_upload_bytes * settings.max_selfie_batch_files + overhead
        if path.startswith("/admin/events/") and path.endswith("/participants/upload"):
            return settings.max_participant_upload_bytes + overhead
        return settings.max_form_request_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        limit = self._limit_for(scope)
        if limit is None:
            await self.app(scope, receive, send)
            return

        headers = {name.lower(): value for name, value in scope.get("headers", [])}
        declared_length = headers.get(b"content-length")
        if declared_length is not None:
            try:
                parsed_length = int(declared_length)
            except ValueError:
                await PlainTextResponse("Invalid Content-Length header.", status_code=400)(scope, receive, send)
                return
            if parsed_length < 0:
                await PlainTextResponse("Invalid Content-Length header.", status_code=400)(scope, receive, send)
                return
            if parsed_length > limit:
                await PlainTextResponse("Request body is too large.", status_code=413)(scope, receive, send)
                return

        consumed = 0

        async def limited_receive() -> Message:
            nonlocal consumed
            message = await receive()
            if message.get("type") == "http.request":
                consumed += len(message.get("body", b""))
                if consumed > limit:
                    raise _RequestBodyTooLarge
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _RequestBodyTooLarge:
            await PlainTextResponse("Request body is too large.", status_code=413)(scope, receive, send)


def _valid_browser_token(value: str) -> bool:
    return 20 <= len(value) <= 128 and all(character.isalnum() or character in "-_" for character in value)


def client_ip(request: Request) -> str:
    candidate = request.client.host if request.client else "unknown"
    if settings.trust_proxy_headers:
        candidate = request.headers.get("cf-connecting-ip") or request.headers.get("x-forwarded-for", "").split(",", 1)[0] or candidate
    try:
        return str(ipaddress.ip_address(candidate.strip()))
    except ValueError:
        return "unknown"


def _limit_response(request: Request, *, status_code: int, detail: str, retry_after: int) -> Response:
    headers = {"Retry-After": str(retry_after)}
    if "application/json" in request.headers.get("accept", ""):
        return JSONResponse({"detail": detail}, status_code=status_code, headers=headers)
    return PlainTextResponse(detail, status_code=status_code, headers=headers)


def _has_browser_admission_context(request: Request) -> bool:
    csrf_cookie = request.cookies.get(CSRF_COOKIE, "")
    return _valid_browser_token(csrf_cookie) and _request_origin_is_allowed(request)


class BrowserSecurityMiddleware(BaseHTTPMiddleware):
    def __init__(self, app) -> None:
        super().__init__(app)
        self.rate_limiter = SlidingWindowLimiter()
        self.in_flight = InFlightLimiter()

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        csrf_token = request.cookies.get(CSRF_COOKIE, "")
        visitor_id = request.cookies.get(VISITOR_COOKIE, "")
        new_csrf = not _valid_browser_token(csrf_token)
        new_visitor = not _valid_browser_token(visitor_id)
        if new_csrf:
            csrf_token = secrets.token_urlsafe(32)
        if new_visitor:
            visitor_id = secrets.token_urlsafe(24)

        request.state.csrf_token = csrf_token
        request.state.visitor_id = visitor_id
        request.state.csp_nonce = secrets.token_urlsafe(18)

        path = request.scope.get("path", "")
        category: str | None = None
        if (
            request.method == "POST"
            and _has_browser_admission_context(request)
            and path.startswith("/user/events/")
            and (
                path.endswith("/selfies") or path.endswith("/bib-only")
            )
        ):
            request_ip = client_ip(request)
            event_key = path.split("/", 4)[3] if len(path.split("/", 4)) > 3 else "unknown"
            checks = (
                ("public-search-visitor", visitor_id, 5, 60),
                ("public-search-ip", request_ip, 300, 600),
                ("public-search-event", event_key, 500, 600),
            )
            for bucket, key, limit, window in checks:
                retry_after = await self.rate_limiter.hit(
                    bucket=bucket, key=key, limit=limit, window_seconds=window
                )
                if retry_after is not None:
                    response = _limit_response(
                        request,
                        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                        detail="Too many searches. Please wait before trying again.",
                        retry_after=retry_after,
                    )
                    return self._finalize_response(request, response, csrf_token, visitor_id, new_csrf, new_visitor)
            category = "public-search"
            if not await self.in_flight.enter(category, 4):
                response = _limit_response(
                    request,
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Search capacity is temporarily full. Please retry shortly.",
                    retry_after=10,
                )
                return self._finalize_response(request, response, csrf_token, visitor_id, new_csrf, new_visitor)
        elif (
            request.method == "POST"
            and _has_browser_admission_context(request)
            and path.startswith("/upload/events/")
        ):
            retry_after = await self.rate_limiter.hit(
                bucket="photo-upload-visitor", key=visitor_id, limit=120, window_seconds=60
            )
            if retry_after is not None:
                response = _limit_response(
                    request,
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Upload rate limit exceeded. Please pause before retrying.",
                    retry_after=retry_after,
                )
                return self._finalize_response(request, response, csrf_token, visitor_id, new_csrf, new_visitor)
            category = "photo-upload"
            if not await self.in_flight.enter(category, 4):
                response = _limit_response(
                    request,
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Upload capacity is temporarily full. Please retry shortly.",
                    retry_after=10,
                )
                return self._finalize_response(request, response, csrf_token, visitor_id, new_csrf, new_visitor)

        try:
            response = await call_next(request)
        finally:
            if category is not None:
                await self.in_flight.leave(category)
        return self._finalize_response(request, response, csrf_token, visitor_id, new_csrf, new_visitor)

    def _finalize_response(
        self,
        request: Request,
        response: Response,
        csrf_token: str,
        visitor_id: str,
        new_csrf: bool,
        new_visitor: bool,
    ) -> Response:
        nonce = request.state.csp_nonce
        path = request.scope.get("path", "")
        if is_production() or path not in {"/docs", "/redoc", "/openapi.json"}:
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; "
                "base-uri 'self'; object-src 'none'; frame-ancestors 'none'; form-action 'self'; "
                f"script-src 'self' 'nonce-{nonce}'; "
                "style-src 'self' https://fonts.googleapis.com; img-src 'self' data: https:; "
                "font-src 'self' https://fonts.gstatic.com; connect-src 'self'; "
                "media-src 'none'; worker-src 'none'; manifest-src 'self'"
            )
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=()"
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        response.headers["X-Permitted-Cross-Domain-Policies"] = "none"
        if is_production():
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        if path.startswith(("/admin", "/upload", "/user")):
            response.headers["Cache-Control"] = "no-store, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"

        if new_csrf:
            response.set_cookie(
                CSRF_COOKIE,
                csrf_token,
                max_age=12 * 60 * 60,
                path="/",
                secure=secure_cookies(),
                httponly=True,
                samesite="strict",
            )
        if new_visitor:
            response.set_cookie(
                VISITOR_COOKIE,
                visitor_id,
                max_age=24 * 60 * 60,
                path="/",
                secure=secure_cookies(),
                httponly=True,
                samesite="lax",
            )
        return response


def template_security_context(request: Request) -> dict[str, str]:
    return {
        "csrf_token": getattr(request.state, "csrf_token", ""),
        "csp_nonce": getattr(request.state, "csp_nonce", ""),
    }
