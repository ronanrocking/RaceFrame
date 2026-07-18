from __future__ import annotations

import hmac
import logging
from contextlib import asynccontextmanager
from datetime import date
from datetime import datetime, timezone
from functools import partial
from html import escape
from pathlib import Path
from urllib.parse import urlencode
from uuid import uuid4

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.orm import Session
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.concurrency import run_in_threadpool

from .admin import (
    EVENT_STATUSES,
    acquire_admin_lock,
    add_participant,
    create_event,
    delete_all_participants,
    delete_event,
    delete_participant,
    force_admin_lock,
    generate_unique_slug,
    get_event,
    get_participant,
    list_events,
    list_participants,
    parse_participant_file,
    update_event,
    update_participant,
    upsert_participants,
)
from .audit_log import append_admin_audit
from .config import settings
from .db import Base, SessionLocal, engine, get_db
from .face import create_bib_only_face_search_session, ingest_face_search_upload
from .health import application_ready
from .models import FaceSearchJob, Participant, PhotoJob
from .metrics import metrics_response, require_metrics_token
from .observability import RequestContextMiddleware, configure_logging
from .participant_lookup import normalize_name_lookup
from .photographer import (
    build_event_photo_stats,
    count_event_participants,
    delete_all_event_photos,
    delete_photo,
    generate_photo_access_url,
    get_event_photo,
    get_published_event,
    ingest_photo_upload,
    list_face_search_photo_items,
    list_published_events,
    list_user_events,
    normalize_match_token,
    rebuild_event_photo_matches,
    safe_photo_access_url,
)
from .rate_limits import hit_persistent_limit
from .worker_api import router as worker_router
from .uploads import read_upload_limited
from .web_security import (
    BrowserSecurityMiddleware,
    RequestBodyLimitMiddleware,
    client_ip,
    is_production,
    issue_search_capability,
    owner_binding_hash,
    request_search_capability,
    require_browser_csrf,
    secure_cookies,
    set_search_capability_cookie,
    template_security_context,
    validate_production_configuration,
)


BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(
    directory=str(BASE_DIR / "templates"),
    context_processors=[template_security_context],
)
ADMIN_SESSION_COOKIE = "raceframe_admin_session"
logger = logging.getLogger(__name__)
configure_logging(json_logs=is_production())


@asynccontextmanager
async def application_lifespan(_app: FastAPI):
    validate_production_configuration()
    if not is_production():
        await run_in_threadpool(Base.metadata.create_all, bind=engine)
    yield


app = FastAPI(
    title=settings.app_name,
    docs_url=None if is_production() else "/docs",
    redoc_url=None if is_production() else "/redoc",
    openapi_url=None if is_production() else "/openapi.json",
    lifespan=application_lifespan,
)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
app.include_router(worker_router)


@app.middleware("http")
async def admin_session_middleware(request: Request, call_next):
    request_path = request.scope.get("path", "")
    if not request_path.startswith("/admin") or request_path == "/admin/session/takeover":
        return await call_next(request)

    session_id = request.cookies.get(ADMIN_SESSION_COOKIE) or str(uuid4())
    if not await run_in_threadpool(try_acquire_admin_session, session_id):
        return HTMLResponse(
            "<h1>Admin panel in use</h1>"
            "<p>Another admin session is currently active. "
            "If that session is stale, you can take over from this browser.</p>"
            "<form method='post' action='/admin/session/takeover'>"
            f"<input type='hidden' name='csrf_token' value='{escape(request.state.csrf_token)}'>"
            "<p><button type='submit'>Take Over Admin Session</button></p>"
            "</form>",
            status_code=409,
        )

    response = await call_next(request)
    if request.cookies.get(ADMIN_SESSION_COOKIE) != session_id:
        response.set_cookie(
            ADMIN_SESSION_COOKIE,
            session_id,
            max_age=30 * 60,
            path="/admin",
            secure=secure_cookies(),
            httponly=True,
            samesite="strict",
        )
    return response


def try_acquire_admin_session(session_id: str) -> bool:
    session: OrmSession = SessionLocal()
    try:
        return acquire_admin_lock(session, session_id=session_id)
    finally:
        session.close()


@app.post("/admin/session/takeover", dependencies=[Depends(require_browser_csrf)])
def admin_session_takeover(request: Request) -> RedirectResponse:
    session_id = request.cookies.get(ADMIN_SESSION_COOKIE) or str(uuid4())
    session: OrmSession = SessionLocal()
    try:
        force_admin_lock(session, session_id=session_id)
        append_admin_audit(session, request, action="admin.session.takeover", target_type="admin_session")
    finally:
        session.close()

    response = RedirectResponse(url="/admin?message=Admin+session+taken+over.&level=success", status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        ADMIN_SESSION_COOKIE,
        session_id,
        max_age=30 * 60,
        path="/admin",
        secure=secure_cookies(),
        httponly=True,
        samesite="strict",
    )
    return response


# Register request hardening after the function middleware so trusted-host checking is
# outermost and the browser context/security headers still wrap admin early responses.
app.add_middleware(RequestBodyLimitMiddleware)
app.add_middleware(BrowserSecurityMiddleware)
app.add_middleware(RequestContextMiddleware)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.allowed_hosts))


def redirect_with_message(
    path: str,
    *,
    message: str,
    level: str = "success",
    status_code: int = status.HTTP_303_SEE_OTHER,
) -> RedirectResponse:
    query = urlencode({"message": message, "level": level})
    separator = "&" if "?" in path else "?"
    return RedirectResponse(url=f"{path}{separator}{query}", status_code=status_code)


def _aware_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def bind_search_capability(request: Request, db: Session, *, event, search_session):
    capability = issue_search_capability(
        session_id=str(search_session.id),
        event_id=str(event.id),
        visitor_id=request.state.visitor_id,
    )
    search_session.expires_at = capability.expires_at
    search_session.capability_hash = capability.capability_hash
    search_session.owner_binding_hash = capability.owner_hash
    db.commit()
    return capability


def authorized_search_results(request: Request, db: Session, *, event):
    capability = request_search_capability(request, event_id=str(event.id))
    if capability is None:
        return None, []

    search_session, results = list_face_search_photo_items(
        db,
        event=event,
        face_session_id=capability.session_id,
    )
    if search_session is None:
        return None, []
    expires_at = getattr(search_session, "expires_at", None)
    stored_capability_hash = getattr(search_session, "capability_hash", None)
    stored_owner_hash = getattr(search_session, "owner_binding_hash", None)
    if (
        expires_at is None
        or _aware_utc(expires_at) <= datetime.now(timezone.utc)
        or not stored_capability_hash
        or not stored_owner_hash
        or not hmac.compare_digest(stored_capability_hash, capability.capability_hash)
        or not hmac.compare_digest(stored_owner_hash, capability.owner_hash)
        or not hmac.compare_digest(stored_owner_hash, owner_binding_hash(request.state.visitor_id))
    ):
        return None, []
    return search_session, results


async def redirect_with_search_capability(
    request: Request,
    db: Session,
    *,
    event,
    search_session,
    message: str,
    level: str,
) -> RedirectResponse:
    capability = await run_in_threadpool(
        partial(bind_search_capability, request, db, event=event, search_session=search_session)
    )
    response = redirect_with_message(f"/user/events/{event.id}", message=message, level=level)
    set_search_capability_cookie(response, event_id=str(event.id), capability=capability)
    return response


def ensure_job_capacity(
    db: Session,
    *,
    model,
    limit: int,
    expected_new_jobs: int,
    detail: str,
) -> None:
    backlog = db.scalar(select(func.count()).select_from(model).where(model.status.in_(("queued", "processing")))) or 0
    if backlog + expected_new_jobs > limit:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=detail,
            headers={"Retry-After": "30"},
        )


def import_participant_records(db: Session, *, event, file_name: str, content: bytes):
    rows = parse_participant_file(file_name, content)
    result = upsert_participants(db, event=event, rows=rows)
    rebuild_event_photo_matches(db, event=event)
    return result


def serialize_photo_upload_result(result) -> dict[str, object | None]:
    latest_job = result.photo.jobs[0] if result.photo and result.photo.jobs else None
    return {
        "file_name": result.file_name,
        "success": result.success,
        "message": result.message,
        "photo_id": str(result.photo.id) if result.photo else None,
        "photo_status": result.photo.status if result.photo else None,
        "job_status": latest_job.status if latest_job else None,
        "error_message": latest_job.error_message if latest_job else None,
    }


async def enforce_persistent_search_limits(request: Request, *, event_id: str) -> None:
    checks = (
        ("public-search-visitor-minute", request.state.visitor_id, 3, 60),
        ("public-search-visitor-hour", request.state.visitor_id, 30, 60 * 60),
        ("public-search-ip", client_ip(request), 300, 10 * 60),
        ("public-search-event", str(event_id), 500, 10 * 60),
    )
    try:
        for bucket, key, limit, window_seconds in checks:
            retry_after = await run_in_threadpool(
                partial(
                    hit_persistent_limit,
                    bucket=bucket,
                    key=key,
                    limit=limit,
                    window_seconds=window_seconds,
                )
            )
            if retry_after is not None:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Too many searches. Please wait before trying again.",
                    headers={"Retry-After": str(retry_after)},
                )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("Persistent rate limiter failed", exc_info=exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Search admission is temporarily unavailable.",
            headers={"Retry-After": "30"},
        ) from exc


async def enforce_persistent_upload_limits(request: Request, *, event_id: str, file_count: int) -> None:
    """Reserve bounded upload capacity across every web process.

    The IP limit is deliberately generous for venue NATs; event and global
    buckets remain effective if an automated client rotates cookies or IPs.
    """
    checks = (
        ("photo-upload-visitor", request.state.visitor_id, 300, 10 * 60),
        ("photo-upload-ip", client_ip(request), 1_000, 10 * 60),
        ("photo-upload-event", str(event_id), 2_000, 10 * 60),
        ("photo-upload-global", "all", 5_000, 10 * 60),
    )
    try:
        for bucket, key, limit, window_seconds in checks:
            retry_after = await run_in_threadpool(
                partial(
                    hit_persistent_limit,
                    bucket=bucket,
                    key=key,
                    limit=limit,
                    window_seconds=window_seconds,
                    units=file_count,
                )
            )
            if retry_after is not None:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Upload rate limit exceeded. Please pause before retrying.",
                    headers={"Retry-After": str(retry_after)},
                )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("Persistent upload rate limiter failed", exc_info=exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Upload admission is temporarily unavailable.",
            headers={"Retry-After": "30"},
        ) from exc


def render_event_form(
    request: Request,
    *,
    page_title: str,
    submit_label: str,
    form_action: str,
    event: object | None,
    participant_count: int = 0,
    participants: list[Participant] | None = None,
    photo_count: int = 0,
    photo_items: list | None = None,
    photo_stats: dict[str, int | float] | None = None,
    auto_slug: str | None = None,
    error_message: str | None = None,
    form_values: dict[str, str] | None = None,
) -> HTMLResponse:
    context = {
        "request": request,
        "title": f"RaceFrame {page_title}",
        "page_title": page_title,
        "submit_label": submit_label,
        "form_action": form_action,
        "status_options": EVENT_STATUSES,
        "event": event,
        "participant_count": participant_count,
        "participants": participants or [],
        "photo_count": photo_count,
        "photo_items": photo_items or [],
        "photo_stats": photo_stats or {},
        "auto_slug": auto_slug,
        "error_message": error_message,
        "form_values": form_values or {},
    }
    return templates.TemplateResponse(request, "admin_event_form.html", context)


def parse_event_date(raw_value: str) -> date | None:
    raw_value = raw_value.strip()
    return date.fromisoformat(raw_value) if raw_value else None


def event_participants(session: Session, event_id) -> list[Participant]:
    return list_participants(session, event_id=event_id)


def render_existing_event_form(
    request: Request,
    *,
    event,
    db: Session,
    error_message: str | None = None,
    form_values: dict[str, str] | None = None,
) -> HTMLResponse:
    participants = event_participants(db, event.id)
    participant_count = len(participants)
    photo_stats = build_event_photo_stats(db, event_id=event.id)
    auto_slug = generate_unique_slug(
        db,
        name=(form_values or {}).get("name", event.name) if event else "",
        exclude_event_id=str(event.id),
    )
    return render_event_form(
        request,
        page_title="Edit Event",
        submit_label="Save Changes",
        form_action=f"/admin/events/{event.id}/edit",
        event=event,
        participant_count=participant_count,
        participants=participants,
        photo_count=int(photo_stats["total"]),
        photo_stats=photo_stats,
        auto_slug=auto_slug,
        error_message=error_message,
        form_values=form_values,
    )


@app.get("/", response_class=JSONResponse)
async def root() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health", response_class=JSONResponse, include_in_schema=False)
@app.get("/livez", response_class=JSONResponse, include_in_schema=False)
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/ready", response_class=JSONResponse, include_in_schema=False)
@app.get("/readyz", response_class=JSONResponse)
def readiness(db: Session = Depends(get_db)) -> Response:
    if not application_ready(db):
        return JSONResponse({"status": "unavailable"}, status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
    return JSONResponse({"status": "ready"})


@app.get(
    "/internal/metrics",
    include_in_schema=False,
    dependencies=[Depends(require_metrics_token)],
)
def prometheus_metrics(db: Session = Depends(get_db)) -> Response:
    return metrics_response(db)


@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    context = {
        "request": request,
        "title": "RaceFrame Admin Dashboard",
        "events": list_events(db),
        "message": request.query_params.get("message"),
        "message_level": request.query_params.get("level", "success"),
    }
    return templates.TemplateResponse(request, "admin_dashboard.html", context)


@app.get("/admin/events/new", response_class=HTMLResponse)
def new_event_page(request: Request) -> HTMLResponse:
    return render_event_form(
        request,
        page_title="Create Event",
        submit_label="Create Event",
        form_action="/admin/events/new",
        event=None,
        auto_slug="Will be generated from the event name when you save.",
    )


@app.get("/upload", response_class=HTMLResponse)
def photographer_event_list_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    context = {
        "request": request,
        "title": "RaceFrame Photographer Upload",
        "events": list_published_events(db),
        "message": request.query_params.get("message"),
        "message_level": request.query_params.get("level", "success"),
        "nav_home_url": "/upload",
        "nav_home_label": "Published Events",
    }
    return templates.TemplateResponse(request, "upload_events.html", context)


@app.get("/user", response_class=HTMLResponse)
def user_event_list_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    context = {
        "request": request,
        "title": "RaceFrame User Search",
        "events": list_user_events(db),
        "nav_home_url": "/user",
        "nav_home_label": "Find Photos",
    }
    return templates.TemplateResponse(request, "user_event_list.html", context)


@app.get("/user/events/{event_id}", response_class=HTMLResponse)
def user_event_search_page(
    request: Request,
    event_id: str,
    db: Session = Depends(get_db),
) -> Response:
    event = get_published_event(db, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="Published event not found.")

    if "face_session" in request.query_params or "q" in request.query_params:
        return RedirectResponse(url=f"/user/events/{event.id}", status_code=status.HTTP_303_SEE_OTHER)

    search_term = ""
    results = []
    matched_participants = []
    face_search_session, face_search_results = authorized_search_results(request, db, event=event)
    if face_search_session is not None and face_search_session.participant is not None:
        search_term = face_search_session.participant.bib_number

    context = {
        "request": request,
        "title": f"Search Photos - {event.name}",
        "event": event,
        "search_term": search_term,
        "results": results,
        "face_search_session": face_search_session,
        "face_search_results": face_search_results,
        "matched_participants": matched_participants,
        "message": request.query_params.get("message"),
        "message_level": request.query_params.get("level", "success"),
        "download_all_url": None,
        "nav_home_url": "/user",
        "nav_home_label": "Find Photos",
    }
    return templates.TemplateResponse(request, "user_event_search.html", context)


@app.post("/user/events/{event_id}/selfies", dependencies=[Depends(require_browser_csrf)])
async def user_upload_selfies_by_search(
    request: Request,
    event_id: str,
    participant_query: str = Form(""),
    selfie_files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    event = await run_in_threadpool(get_published_event, db, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="Published event not found.")
    await enforce_persistent_search_limits(request, event_id=str(event.id))

    query = participant_query.strip()
    submitted_files = [selfie_file for selfie_file in selfie_files if selfie_file.filename and selfie_file.filename.strip()]
    if not query:
        return redirect_with_message(
            f"/user/events/{event.id}",
            message="Enter your bib number or participant name and upload at least one selfie.",
            level="error",
        )
    if not submitted_files:
        return redirect_with_message(
            f"/user/events/{event.id}",
            message="Choose at least one face photo before uploading.",
            level="error",
        )
    if len(submitted_files) > settings.max_selfie_batch_files:
        raise HTTPException(status_code=413, detail="Too many selfie files in one search.")

    participant = await run_in_threadpool(partial(find_event_participant, db, event_id=event.id, query=query))
    if participant is None:
        return redirect_with_message(
            f"/user/events/{event.id}",
            message="No unique exact participant match was found. Enter your exact bib number.",
            level="error",
        )

    await run_in_threadpool(
        partial(
            ensure_job_capacity,
            db,
            model=FaceSearchJob,
            limit=settings.max_face_search_backlog,
            expected_new_jobs=len(submitted_files),
            detail="Search processing is temporarily full. Please retry shortly.",
        )
    )

    search_queued_count = 0
    failed_notes: list[str] = []
    face_search_session = None
    for selfie_file in submitted_files:
        content = await read_upload_limited(selfie_file, settings.max_selfie_upload_bytes)
        search_result = await run_in_threadpool(
            partial(
                ingest_face_search_upload,
                db,
                event=event,
                participant=participant,
                search_session=face_search_session,
                file_name=selfie_file.filename or "face-photo",
                content_type=selfie_file.content_type or "",
                content=content,
            )
        )
        if search_result.search_session is not None:
            face_search_session = search_result.search_session
        if search_result.success:
            search_queued_count += 1
        else:
            failed_notes.append(search_result.message)

    message = f"Queued {search_queued_count} temporary search selfie{'' if search_queued_count == 1 else 's'}."
    if failed_notes:
        message = f"{message} {' | '.join(failed_notes[:2])}"

    if face_search_session is None:
        return redirect_with_message(
            f"/user/events/{event.id}",
            message="Search could not be created. Please retry.",
            level="error",
        )
    return await redirect_with_search_capability(
        request,
        db,
        event=event,
        search_session=face_search_session,
        message=message,
        level="error" if failed_notes and not search_queued_count else "success",
    )


@app.post("/user/events/{event_id}/bib-only", dependencies=[Depends(require_browser_csrf)])
async def user_bib_only_search(
    request: Request,
    event_id: str,
    participant_query: str = Form(""),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    event = await run_in_threadpool(get_published_event, db, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="Published event not found.")
    await enforce_persistent_search_limits(request, event_id=str(event.id))

    query = participant_query.strip()
    if not query:
        return redirect_with_message(
            f"/user/events/{event.id}",
            message="Enter your bib number or participant name before using bib-only search.",
            level="error",
        )

    participant = await run_in_threadpool(partial(find_event_participant, db, event_id=event.id, query=query))
    if participant is None:
        return redirect_with_message(
            f"/user/events/{event.id}",
            message="No unique exact participant match was found. Enter your exact bib number.",
            level="error",
        )

    face_search_session, seed_count = await run_in_threadpool(
        partial(
            create_bib_only_face_search_session,
            db,
            event_id=event.id,
            participant=participant,
        )
    )
    message = (
        "Started bib-only reinforced search. "
        f"Temporary face seeds found: {seed_count}."
    )
    return await redirect_with_search_capability(
        request,
        db,
        event=event,
        search_session=face_search_session,
        message=message,
        level="success" if seed_count else "error",
    )


def find_event_participant(db: Session, *, event_id, query: str) -> Participant | None:
    normalized_bib = normalize_match_token(query)
    exact_bib_matches = list(
        db.execute(
            select(Participant)
            .where(Participant.event_id == event_id, Participant.bib_lookup == normalized_bib)
            .limit(2)
        )
        .scalars()
    )
    if len(exact_bib_matches) == 1:
        return exact_bib_matches[0]

    normalized_name = normalize_name_lookup(query)
    exact_name_matches = list(
        db.execute(
            select(Participant)
            .where(Participant.event_id == event_id, Participant.name_lookup == normalized_name)
            .limit(2)
        ).scalars()
    )
    return exact_name_matches[0] if len(exact_name_matches) == 1 else None


@app.get("/user/events/{event_id}/photos/{photo_id}/download")
def user_download_photo(request: Request, event_id: str, photo_id: str, db: Session = Depends(get_db)) -> Response:
    event = get_published_event(db, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="Published event not found.")

    _, authorized_items = authorized_search_results(request, db, event=event)
    if not any(str(item.photo.id) == str(photo_id) for item in authorized_items):
        raise HTTPException(status_code=404, detail="Photo not found.")

    photo = get_event_photo(db, event_id=event.id, photo_id=photo_id)
    if photo is None:
        raise HTTPException(status_code=404, detail="Photo not found.")

    try:
        access_url = generate_photo_access_url(photo.original_object_key, download_name=photo.file_name)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to create a photo download URL", exc_info=exc)
        raise HTTPException(status_code=503, detail="Photo download is temporarily unavailable.") from exc
    if not access_url:
        raise HTTPException(status_code=503, detail="Photo download is temporarily unavailable.")

    return RedirectResponse(url=access_url, status_code=status.HTTP_303_SEE_OTHER)


@app.get("/user/events/{event_id}/download-all")
def user_download_all_photos(
    event_id: str,
    q: str = "",
    db: Session = Depends(get_db),
) -> Response:
    raise HTTPException(status_code=410, detail="Download all is disabled for temporary hybrid searches.")


@app.get("/upload/events/{event_id}", response_class=HTMLResponse)
def photographer_event_upload_page(request: Request, event_id: str, db: Session = Depends(get_db)) -> HTMLResponse:
    event = get_published_event(db, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="Published event not found.")

    context = {
        "request": request,
        "title": f"Upload Photos - {event.name}",
        "event": event,
        "participant_count": count_event_participants(db, event_id=event.id),
        "photo_stats": build_event_photo_stats(db, event_id=event.id),
        "message": request.query_params.get("message"),
        "message_level": request.query_params.get("level", "success"),
        "nav_home_url": "/upload",
        "nav_home_label": "Published Events",
    }
    return templates.TemplateResponse(request, "upload_event_detail.html", context)


@app.post("/upload/events/{event_id}", dependencies=[Depends(require_browser_csrf)])
async def photographer_upload_action(
    request: Request,
    event_id: str,
    photo_files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
) -> Response:
    event = await run_in_threadpool(get_published_event, db, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="Published event not found.")

    wants_json = (
        request.headers.get("x-requested-with") == "XMLHttpRequest"
        or "application/json" in request.headers.get("accept", "")
    )

    submitted_files = [photo_file for photo_file in photo_files if photo_file.filename and photo_file.filename.strip()]
    if not submitted_files:
        message = "Choose one or more image files before uploading."
        if wants_json:
            return JSONResponse(
                {
                    "message": message,
                    "processed_count": 0,
                    "queued_count": 0,
                    "failed_count": 0,
                    "results": [],
                },
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        return redirect_with_message(f"/upload/events/{event.id}", message=message, level="error")
    if len(submitted_files) > settings.max_photo_batch_files:
        raise HTTPException(status_code=413, detail="Too many photo files in one upload request.")

    await enforce_persistent_upload_limits(
        request,
        event_id=str(event.id),
        file_count=len(submitted_files),
    )

    await run_in_threadpool(
        partial(
            ensure_job_capacity,
            db,
            model=PhotoJob,
            limit=settings.max_photo_job_backlog,
            expected_new_jobs=len(submitted_files) * 2,
            detail="Photo processing is temporarily full. Please retry after queued work finishes.",
        )
    )
    request_idempotency_key = request.headers.get("idempotency-key", "").strip()
    if len(request_idempotency_key) > 120:
        raise HTTPException(status_code=400, detail="Idempotency-Key is too long.")

    queued_count = 0
    failed_count = 0
    failure_notes: list[str] = []
    upload_results: list[dict[str, object | None]] = []

    for index, photo_file in enumerate(submitted_files):
        content = await read_upload_limited(photo_file, settings.max_photo_upload_bytes)
        idempotency_key = request_idempotency_key
        if idempotency_key and len(submitted_files) > 1:
            idempotency_key = f"{idempotency_key}:{index}"
        result = await run_in_threadpool(
            partial(
                ingest_photo_upload,
                db,
                event=event,
                file_name=photo_file.filename or "photo",
                content_type=photo_file.content_type or "",
                content=content,
                idempotency_key=idempotency_key or None,
            )
        )
        upload_results.append(await run_in_threadpool(serialize_photo_upload_result, result))
        if result.success:
            queued_count += 1
        else:
            failed_count += 1
            failure_notes.append(f"{result.file_name}: {result.message}")

    message = f"Uploaded {len(submitted_files)} photo(s). Queued {queued_count}, failed {failed_count}."
    if failure_notes:
        message = f"{message} {' | '.join(failure_notes[:3])}"
    if queued_count:
        await run_in_threadpool(
            partial(
                append_admin_audit,
                db,
                request,
                action="photo.upload",
                target_type="event",
                target_id=event.id,
                event_id=event.id,
                metadata={
                    "submitted_count": len(submitted_files),
                    "queued_count": queued_count,
                    "failed_count": failed_count,
                },
            )
        )

    if wants_json:
        return JSONResponse(
            {
                "message": message,
                "processed_count": len(submitted_files),
                "queued_count": queued_count,
                "failed_count": failed_count,
                "results": upload_results,
            },
            status_code=status.HTTP_202_ACCEPTED if queued_count else status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    return redirect_with_message(
        f"/upload/events/{event.id}",
        message=message,
        level="error" if failed_count else "success",
    )


@app.post(
    "/admin/events/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_browser_csrf)],
)
def create_event_action(
    request: Request,
    name: str = Form(...),
    event_date: str = Form(""),
    location: str = Form(""),
    status_value: str = Form("draft", alias="status"),
    db: Session = Depends(get_db),
):
    normalized_name = name.strip()
    normalized_location = location.strip() or None

    form_values = {
        "name": normalized_name,
        "event_date": event_date,
        "location": location,
        "status": status_value,
    }

    if not normalized_name:
        return render_event_form(
            request,
            page_title="Create Event",
            submit_label="Create Event",
            form_action="/admin/events/new",
            event=None,
            auto_slug="Will be generated from the event name when you save.",
            error_message="Event name is required.",
            form_values=form_values,
        )

    if status_value not in EVENT_STATUSES:
        return render_event_form(
            request,
            page_title="Create Event",
            submit_label="Create Event",
            form_action="/admin/events/new",
            event=None,
            auto_slug=generate_unique_slug(db, name=normalized_name or "event"),
            error_message="Choose a valid status.",
            form_values=form_values,
        )

    try:
        parsed_date = parse_event_date(event_date)
    except ValueError:
        return render_event_form(
            request,
            page_title="Create Event",
            submit_label="Create Event",
            form_action="/admin/events/new",
            event=None,
            auto_slug=generate_unique_slug(db, name=normalized_name or "event"),
            error_message="Event date must use the YYYY-MM-DD format.",
            form_values=form_values,
        )

    normalized_slug = generate_unique_slug(db, name=normalized_name)

    event = create_event(
        db,
        name=normalized_name,
        slug=normalized_slug,
        event_date=parsed_date,
        location=normalized_location,
        status=status_value,
    )
    append_admin_audit(
        db,
        request,
        action="event.create",
        target_type="event",
        target_id=event.id,
        event_id=event.id,
    )
    return redirect_with_message(f"/admin/events/{event.id}/edit", message="Event created.")


@app.get("/admin/events/{event_id}/edit", response_class=HTMLResponse)
def edit_event_page(request: Request, event_id: str, db: Session = Depends(get_db)) -> HTMLResponse:
    event = get_event(db, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="Event not found.")
    response = render_existing_event_form(request, event=event, db=db)
    response.context["message"] = request.query_params.get("message")
    response.context["message_level"] = request.query_params.get("level", "success")
    return response


@app.post(
    "/admin/events/{event_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_browser_csrf)],
)
def edit_event_action(
    request: Request,
    event_id: str,
    name: str = Form(...),
    event_date: str = Form(""),
    location: str = Form(""),
    status_value: str = Form("draft", alias="status"),
    db: Session = Depends(get_db),
):
    event = get_event(db, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="Event not found.")

    normalized_name = name.strip()
    normalized_location = location.strip() or None
    form_values = {
        "name": normalized_name,
        "event_date": event_date,
        "location": location,
        "status": status_value,
    }

    if not normalized_name:
        return render_existing_event_form(request, event=event, db=db, error_message="Event name is required.", form_values=form_values)

    if status_value not in EVENT_STATUSES:
        return render_existing_event_form(request, event=event, db=db, error_message="Choose a valid status.", form_values=form_values)

    try:
        parsed_date = parse_event_date(event_date)
    except ValueError:
        return render_existing_event_form(
            request,
            event=event,
            db=db,
            error_message="Event date must use the YYYY-MM-DD format.",
            form_values=form_values,
        )

    normalized_slug = generate_unique_slug(db, name=normalized_name, exclude_event_id=str(event.id))

    update_event(
        db,
        event=event,
        name=normalized_name,
        slug=normalized_slug,
        event_date=parsed_date,
        location=normalized_location,
        status=status_value,
    )
    append_admin_audit(
        db,
        request,
        action="event.update",
        target_type="event",
        target_id=event.id,
        event_id=event.id,
    )
    return redirect_with_message(f"/admin/events/{event.id}/edit", message="Event updated.")


@app.post("/admin/events/{event_id}/delete", dependencies=[Depends(require_browser_csrf)])
def delete_event_action(request: Request, event_id: str, db: Session = Depends(get_db)) -> RedirectResponse:
    event = get_event(db, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="Event not found.")

    deleted_photo_count = delete_all_event_photos(db, event=event)
    deleted_event_id = event.id
    delete_event(db, event=event)
    append_admin_audit(
        db,
        request,
        action="event.delete",
        target_type="event",
        target_id=deleted_event_id,
        metadata={"deleted_photo_count": deleted_photo_count},
    )
    return redirect_with_message("/admin", message="Event deleted.")


@app.post(
    "/admin/events/{event_id}/participants/upload",
    dependencies=[Depends(require_browser_csrf)],
)
async def upload_participants_action(
    request: Request,
    event_id: str,
    participant_file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    event = await run_in_threadpool(get_event, db, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="Event not found.")

    file_name = participant_file.filename or ""
    if not file_name:
        return redirect_with_message(
            f"/admin/events/{event.id}/edit",
            message="Choose a participant file before uploading.",
            level="error",
        )

    content = await read_upload_limited(participant_file, settings.max_participant_upload_bytes)
    try:
        result = await run_in_threadpool(
            partial(import_participant_records, db, event=event, file_name=file_name, content=content)
        )
    except ValueError as exc:
        return redirect_with_message(
            f"/admin/events/{event.id}/edit",
            message=str(exc),
            level="error",
        )
    await run_in_threadpool(
        partial(
            append_admin_audit,
            db,
            request,
            action="participant.import",
            target_type="event",
            target_id=event.id,
            event_id=event.id,
            metadata={
                "inserted_count": result.inserted_rows,
                "updated_count": result.updated_rows,
                "skipped_count": result.skipped_rows,
            },
        )
    )

    message = (
        f"Participants imported. Added {result.inserted_rows}, updated {result.updated_rows}, "
        f"skipped {result.skipped_rows}."
    )
    return redirect_with_message(f"/admin/events/{event.id}/edit", message=message)


@app.post("/admin/events/{event_id}/participants/add", dependencies=[Depends(require_browser_csrf)])
def add_participant_action(
    request: Request,
    event_id: str,
    bib_number: str = Form(...),
    full_name: str = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    event = get_event(db, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="Event not found.")

    try:
        participant = add_participant(db, event=event, bib_number=bib_number, full_name=full_name)
    except ValueError as exc:
        return redirect_with_message(f"/admin/events/{event.id}/edit", message=str(exc), level="error")

    rebuild_event_photo_matches(db, event=event)
    append_admin_audit(
        db,
        request,
        action="participant.create",
        target_type="participant",
        target_id=participant.id,
        event_id=event.id,
    )
    return redirect_with_message(f"/admin/events/{event.id}/edit", message="Participant added.")


@app.post(
    "/admin/events/{event_id}/participants/{participant_id}/update",
    dependencies=[Depends(require_browser_csrf)],
)
def update_participant_action(
    request: Request,
    event_id: str,
    participant_id: str,
    bib_number: str = Form(...),
    full_name: str = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    event = get_event(db, event_id)
    participant = get_participant(db, participant_id)
    if event is None or participant is None or participant.event_id != event.id:
        raise HTTPException(status_code=404, detail="Participant not found.")

    try:
        update_participant(db, participant=participant, bib_number=bib_number, full_name=full_name)
    except ValueError as exc:
        return redirect_with_message(f"/admin/events/{event.id}/edit", message=str(exc), level="error")

    rebuild_event_photo_matches(db, event=event)
    append_admin_audit(
        db,
        request,
        action="participant.update",
        target_type="participant",
        target_id=participant.id,
        event_id=event.id,
    )
    return redirect_with_message(f"/admin/events/{event.id}/edit", message="Participant updated.")


@app.post(
    "/admin/events/{event_id}/participants/{participant_id}/delete",
    dependencies=[Depends(require_browser_csrf)],
)
def delete_participant_action(
    request: Request,
    event_id: str,
    participant_id: str,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    event = get_event(db, event_id)
    participant = get_participant(db, participant_id)
    if event is None or participant is None or participant.event_id != event.id:
        raise HTTPException(status_code=404, detail="Participant not found.")

    deleted_participant_id = participant.id
    delete_participant(db, participant=participant)
    rebuild_event_photo_matches(db, event=event)
    append_admin_audit(
        db,
        request,
        action="participant.delete",
        target_type="participant",
        target_id=deleted_participant_id,
        event_id=event.id,
    )
    return redirect_with_message(f"/admin/events/{event.id}/edit", message="Participant deleted.")


@app.post(
    "/admin/events/{event_id}/participants/delete-all",
    dependencies=[Depends(require_browser_csrf)],
)
def delete_all_participants_action(
    request: Request,
    event_id: str,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    event = get_event(db, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="Event not found.")

    deleted_count = delete_all_participants(db, event=event)
    rebuild_event_photo_matches(db, event=event)
    append_admin_audit(
        db,
        request,
        action="participant.delete_all",
        target_type="event",
        target_id=event.id,
        event_id=event.id,
        metadata={"deleted_count": deleted_count},
    )
    return redirect_with_message(
        f"/admin/events/{event.id}/edit",
        message=f"Deleted {deleted_count} participant record{'s' if deleted_count != 1 else ''}.",
    )


@app.post(
    "/admin/events/{event_id}/photos/{photo_id}/delete",
    dependencies=[Depends(require_browser_csrf)],
)
def delete_photo_action(
    request: Request,
    event_id: str,
    photo_id: str,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    event = get_event(db, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="Event not found.")

    photo = get_event_photo(db, event_id=event.id, photo_id=photo_id)
    if photo is None:
        raise HTTPException(status_code=404, detail="Photo not found.")

    deleted_photo_id = photo.id
    delete_photo(db, photo=photo)
    append_admin_audit(
        db,
        request,
        action="photo.delete",
        target_type="photo",
        target_id=deleted_photo_id,
        event_id=event.id,
    )
    return redirect_with_message(f"/admin/events/{event.id}/edit", message="Photo deleted.")


@app.get("/admin/events/{event_id}/photos/{photo_id}", response_class=HTMLResponse)
def admin_photo_detail_page(
    request: Request,
    event_id: str,
    photo_id: str,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    event = get_event(db, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="Event not found.")

    photo = get_event_photo(db, event_id=event.id, photo_id=photo_id)
    if photo is None:
        raise HTTPException(status_code=404, detail="Photo not found.")

    context = {
        "request": request,
        "title": f"Photo Details - {photo.file_name}",
        "event": event,
        "photo": photo,
        "photo_url": safe_photo_access_url(photo.original_object_key),
        "nav_home_url": f"/admin/events/{event.id}/edit",
        "nav_home_label": "Event",
    }
    return templates.TemplateResponse(request, "admin_photo_detail.html", context)


@app.post(
    "/admin/events/{event_id}/photos/delete-all",
    dependencies=[Depends(require_browser_csrf)],
)
def delete_all_event_photos_action(
    request: Request,
    event_id: str,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    event = get_event(db, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="Event not found.")

    deleted_count = delete_all_event_photos(db, event=event)
    append_admin_audit(
        db,
        request,
        action="photo.delete_all",
        target_type="event",
        target_id=event.id,
        event_id=event.id,
        metadata={"deleted_count": deleted_count},
    )
    return redirect_with_message(
        f"/admin/events/{event.id}/edit",
        message=f"Deleted {deleted_count} photo record{'s' if deleted_count != 1 else ''}.",
    )
