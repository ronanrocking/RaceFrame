from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from starlette.requests import Request

from app import main as main_module


def browser_request(path: str, query_string: str = "") -> Request:
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": query_string.encode("ascii"),
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


def test_event_search_results_only_render_on_the_explicit_results_view(monkeypatch: pytest.MonkeyPatch) -> None:
    event = SimpleNamespace(id="event-id", name="Test Event")
    search_results = Mock(return_value=(None, [], 0))
    monkeypatch.setattr(main_module, "get_published_event", lambda _db, _event_id: event)
    monkeypatch.setattr(main_module, "authorized_search_results", search_results)

    clean_response = main_module.user_event_search_page(browser_request("/user/events/event-id"), "event-id", Mock())

    assert clean_response.status_code == 200
    search_results.assert_not_called()

    results_response = main_module.user_event_search_page(
        browser_request("/user/events/event-id", "view=results"), "event-id", Mock()
    )

    assert results_response.status_code == 200
    search_results.assert_called_once()


def test_admin_dashboard_counts_uploaded_photos_not_processing_jobs(monkeypatch: pytest.MonkeyPatch) -> None:
    class DashboardDb:
        def scalar(self, statement):
            assert statement.get_final_froms()[0].name == "photos"
            return 7

    monkeypatch.setattr(main_module, "list_events", lambda _db: [])

    response = main_module.admin_dashboard(browser_request("/admin"), DashboardDb())

    assert response.status_code == 200
    assert b">7<" in response.body


def test_participant_shell_hides_internal_navigation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main_module, "list_user_events", lambda _db: [])

    response = main_module.user_event_list_page(browser_request("/user"), Mock())
    body = response.body.decode("utf-8")

    assert 'class="site-brand" href="/user"' in body
    assert 'href="/admin">Admin</a>' not in body
    assert 'href="/upload">Upload</a>' not in body
    assert "Choose your race to find your photos." in body
    assert "site-marquee" not in body


def test_participant_event_list_renders_thumbnail_and_placeholder(monkeypatch: pytest.MonkeyPatch) -> None:
    events = [
        SimpleNamespace(event=SimpleNamespace(id="with-photo", name="City Race", event_date=None, location=None, thumbnail_object_key="events/photo.jpg")),
        SimpleNamespace(event=SimpleNamespace(id="without-photo", name="Trail Race", event_date=None, location=None, thumbnail_object_key=None)),
    ]
    monkeypatch.setattr(main_module, "list_user_events", lambda _db: events)

    response = main_module.user_event_list_page(browser_request("/user"), Mock())
    body = response.body.decode("utf-8")

    assert 'src="/user/events/with-photo/thumbnail"' in body
    assert 'aria-label="Event photo not available"' in body


def test_upload_shell_uses_task_specific_navigation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main_module, "list_published_events", lambda _db: [])

    response = main_module.photographer_event_list_page(browser_request("/upload"), Mock())
    body = response.body.decode("utf-8")

    assert 'class="site-brand" href="/upload"' in body
    assert 'href="/user">Find Photos</a>' not in body
    assert "Choose a published event to start an upload." in body
