from __future__ import annotations

from unittest.mock import Mock

import pytest
from starlette.requests import Request

from app import main as main_module


def browser_request(path: str) -> Request:
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "headers": [(b"host", b"testserver")],
            "client": ("127.0.0.1", 1234),
            "server": ("testserver", 80),
            "app": main_module.app,
        }
    )
    request.state.csrf_token = "test-csrf-token-that-is-long-enough"
    request.state.visitor_id = "test-visitor-token-that-is-long-enough"
    request.state.csp_nonce = "test-csp-nonce"
    return request


@pytest.mark.parametrize(
    ("path", "query_helper", "endpoint"),
    (
        ("/admin", "list_events", main_module.admin_dashboard),
        ("/upload", "list_published_events", main_module.photographer_event_list_page),
        ("/user", "list_user_events", main_module.user_event_list_page),
    ),
)
def test_top_level_html_routes_render_with_current_starlette_api(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    query_helper: str,
    endpoint,
) -> None:
    monkeypatch.setattr(main_module, query_helper, lambda _db: [])

    response = endpoint(browser_request(path), Mock())

    assert response.status_code == 200
    assert response.media_type == "text/html"
    assert b"<!doctype html>" in response.body.lower()
