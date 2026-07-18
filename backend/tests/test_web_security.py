from __future__ import annotations

import unittest
from dataclasses import replace
from unittest.mock import patch

from fastapi import HTTPException
from starlette.requests import Request

from app.config import settings
from app.web_security import (
    RequestBodyLimitMiddleware,
    SlidingWindowLimiter,
    decode_search_capability,
    issue_search_capability,
    require_browser_csrf,
    validate_production_configuration,
)


class SearchCapabilityTests(unittest.TestCase):
    def test_capability_is_bound_to_event_and_visitor(self) -> None:
        capability = issue_search_capability(
            session_id="59bbc34f-6edf-4a73-96c6-72ed2500548a",
            event_id="05ea57cb-0dc7-4747-8b80-1d19bd313327",
            visitor_id="visitor-one-1234567890",
        )
        decoded = decode_search_capability(
            capability.secret,
            event_id=capability.event_id,
            visitor_id="visitor-one-1234567890",
        )
        self.assertIsNotNone(decoded)
        self.assertEqual(decoded.session_id, capability.session_id)
        self.assertEqual(decoded.capability_hash, capability.capability_hash)
        self.assertIsNone(
            decode_search_capability(
                capability.secret,
                event_id="f6b832ee-a0d3-41cb-8f38-97b4aadd08de",
                visitor_id="visitor-one-1234567890",
            )
        )
        self.assertIsNone(
            decode_search_capability(
                capability.secret,
                event_id=capability.event_id,
                visitor_id="visitor-two-1234567890",
            )
        )

    def test_tampered_capability_is_rejected(self) -> None:
        capability = issue_search_capability(
            session_id="59bbc34f-6edf-4a73-96c6-72ed2500548a",
            event_id="05ea57cb-0dc7-4747-8b80-1d19bd313327",
            visitor_id="visitor-one-1234567890",
        )
        tampered = f"{capability.secret[:-1]}{'A' if capability.secret[-1] != 'A' else 'B'}"
        self.assertIsNone(
            decode_search_capability(
                tampered,
                event_id=capability.event_id,
                visitor_id="visitor-one-1234567890",
            )
        )


class ProductionConfigurationTests(unittest.TestCase):
    def test_production_rejects_development_database_and_missing_secrets(self) -> None:
        unsafe = replace(
            settings,
            environment="production",
            database_url="sqlite:///./unsafe.db",
            app_secret_key="",
            worker_api_token="",
            allowed_hosts=("raceframe.example",),
            public_origins=("https://raceframe.example",),
        )
        with patch("app.web_security.settings", unsafe):
            with self.assertRaisesRegex(RuntimeError, "SQLite"):
                validate_production_configuration()


class SlidingWindowLimiterTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejects_after_limit(self) -> None:
        limiter = SlidingWindowLimiter(max_keys=10)
        self.assertIsNone(await limiter.hit(bucket="search", key="visitor", limit=2, window_seconds=60))
        self.assertIsNone(await limiter.hit(bucket="search", key="visitor", limit=2, window_seconds=60))
        self.assertGreaterEqual(
            await limiter.hit(bucket="search", key="visitor", limit=2, window_seconds=60),
            1,
        )


class CsrfTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def request(*, token: str, origin: str | None = "http://testserver") -> Request:
        headers = [
            (b"host", b"testserver"),
            (b"cookie", f"raceframe_csrf={token}".encode("ascii")),
        ]
        if origin is not None:
            headers.append((b"origin", origin.encode("ascii")))
        return Request(
            {
                "type": "http",
                "method": "POST",
                "scheme": "http",
                "path": "/admin/events/new",
                "query_string": b"",
                "headers": headers,
                "client": ("127.0.0.1", 1234),
                "server": ("testserver", 80),
            }
        )

    async def test_matching_double_submit_token_and_origin_pass(self) -> None:
        token = "csrf-token-that-is-long-enough-123456"
        await require_browser_csrf(self.request(token=token), csrf_token=token, x_csrf_token=None)

    async def test_bad_origin_is_rejected(self) -> None:
        token = "csrf-token-that-is-long-enough-123456"
        with self.assertRaises(HTTPException) as raised:
            await require_browser_csrf(
                self.request(token=token, origin="https://attacker.example"),
                csrf_token=token,
                x_csrf_token=None,
            )
        self.assertEqual(raised.exception.status_code, 403)

    async def test_missing_origin_with_valid_csrf_token_passes(self) -> None:
        token = "csrf-token-that-is-long-enough-123456"
        await require_browser_csrf(
            self.request(token=token, origin=None),
            csrf_token=token,
            x_csrf_token=None,
        )

    async def test_opaque_origin_is_rejected(self) -> None:
        token = "csrf-token-that-is-long-enough-123456"
        with self.assertRaises(HTTPException):
            await require_browser_csrf(
                self.request(token=token, origin="null"),
                csrf_token=token,
                x_csrf_token=None,
            )


class RequestBodyLimitMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def test_chunked_oversize_body_is_rejected(self) -> None:
        downstream_called = False

        async def downstream(scope, receive, send):
            nonlocal downstream_called
            downstream_called = True
            await receive()
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        middleware = RequestBodyLimitMiddleware(downstream)
        messages = [
            {
                "type": "http.request",
                "body": b"x" * (2 * 1024 * 1024 + 1),
                "more_body": False,
            }
        ]
        sent: list[dict] = []

        async def receive():
            return messages.pop(0)

        async def send(message):
            sent.append(message)

        await middleware(
            {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": "POST",
                "scheme": "https",
                "path": "/admin/events/new",
                "raw_path": b"/admin/events/new",
                "query_string": b"",
                "headers": [],
                "client": ("127.0.0.1", 1234),
                "server": ("testserver", 443),
            },
            receive,
            send,
        )

        self.assertTrue(downstream_called)
        self.assertEqual(sent[0]["status"], 413)


if __name__ == "__main__":
    unittest.main()
