from __future__ import annotations

import contextvars
import json
import logging
import re
import time
import uuid
from datetime import datetime, timezone

from prometheus_client import Counter, Histogram
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response


request_id_context: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="")
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{8,128}$")
HTTP_REQUESTS = Counter(
    "raceframe_http_requests_total",
    "HTTP requests completed by route, method, and status.",
    ("method", "route", "status"),
)
HTTP_REQUEST_DURATION = Histogram(
    "raceframe_http_request_duration_seconds",
    "HTTP request duration by route and method.",
    ("method", "route"),
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60),
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = request_id_context.get()
        if request_id:
            payload["request_id"] = request_id
        for key in ("event", "job_id", "task_id", "photo_id", "search_session_id", "maintenance"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


def configure_logging(*, json_logs: bool) -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not root.handlers:
        root.addHandler(logging.StreamHandler())
    formatter: logging.Formatter
    if json_logs:
        formatter = JsonFormatter()
    else:
        formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    for handler in root.handlers:
        handler.setFormatter(formatter)


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        supplied_id = request.headers.get("x-request-id", "")
        request_id = supplied_id if REQUEST_ID_PATTERN.fullmatch(supplied_id) else uuid.uuid4().hex
        token = request_id_context.set(request_id)
        request.state.request_id = request_id
        started = time.perf_counter()
        logger = logging.getLogger("raceframe.request")
        try:
            response = await call_next(request)
        except Exception:
            logger.exception(
                "request_failed",
                extra={"event": {"method": request.method, "path": request.scope.get("path", "")}},
            )
            raise
        else:
            elapsed_seconds = time.perf_counter() - started
            route = request.scope.get("route")
            route_path = getattr(route, "path", None) or "unmatched"
            if route_path != "/internal/metrics":
                HTTP_REQUESTS.labels(
                    method=request.method,
                    route=route_path,
                    status=str(response.status_code),
                ).inc()
                HTTP_REQUEST_DURATION.labels(method=request.method, route=route_path).observe(elapsed_seconds)
            response.headers["X-Request-ID"] = request_id
            logger.info(
                "request_completed",
                extra={
                    "event": {
                        "method": request.method,
                        "path": request.scope.get("path", ""),
                        "status": response.status_code,
                        "duration_ms": round(elapsed_seconds * 1_000, 2),
                    }
                },
            )
            return response
        finally:
            request_id_context.reset(token)
