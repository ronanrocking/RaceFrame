from __future__ import annotations

from datetime import date
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
from .config import settings
from .db import Base, SessionLocal, engine, get_db
from .face import create_bib_only_face_search_session, ingest_face_search_upload
from .models import Participant
from .photographer import (
    count_event_participants,
    download_photo_bytes,
    delete_all_event_photos,
    delete_photo,
    get_event_photo,
    get_published_event,
    ingest_photo_upload,
    list_event_photo_items,
    list_face_search_photo_items,
    list_published_events,
    list_user_events,
    normalize_match_token,
    rebuild_event_photo_matches,
    safe_photo_access_url,
)
from .worker_api import router as worker_router


BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
ADMIN_SESSION_COOKIE = "raceframe_admin_session"

app = FastAPI(title=settings.app_name)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
app.include_router(worker_router)


@app.on_event("startup")
def startup() -> None:
    Base.metadata.create_all(bind=engine)


@app.middleware("http")
async def admin_session_middleware(request: Request, call_next):
    if not request.url.path.startswith("/admin") or request.url.path == "/admin/session/takeover":
        return await call_next(request)

    session_id = request.cookies.get(ADMIN_SESSION_COOKIE) or str(uuid4())
    session: OrmSession = SessionLocal()
    try:
        if not acquire_admin_lock(session, session_id=session_id):
            return HTMLResponse(
                "<h1>Admin panel in use</h1>"
                "<p>Another admin session is currently active. "
                "If that session is stale, you can take over from this browser.</p>"
                "<form method='post' action='/admin/session/takeover'>"
                "<p><button type='submit'>Take Over Admin Session</button></p>"
                "</form>",
                status_code=409,
            )
    finally:
        session.close()

    response = await call_next(request)
    if request.cookies.get(ADMIN_SESSION_COOKIE) != session_id:
        response.set_cookie(
            ADMIN_SESSION_COOKIE,
            session_id,
            httponly=True,
            samesite="lax",
        )
    return response


@app.post("/admin/session/takeover")
async def admin_session_takeover(request: Request) -> RedirectResponse:
    session_id = request.cookies.get(ADMIN_SESSION_COOKIE) or str(uuid4())
    session: OrmSession = SessionLocal()
    try:
        force_admin_lock(session, session_id=session_id)
    finally:
        session.close()

    response = RedirectResponse(url="/admin?message=Admin+session+taken+over.&level=success", status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        ADMIN_SESSION_COOKIE,
        session_id,
        httponly=True,
        samesite="lax",
    )
    return response


def redirect_with_message(path: str, *, message: str, level: str = "success", status_code: int = status.HTTP_303_SEE_OTHER) -> RedirectResponse:
    query = urlencode({"message": message, "level": level})
    separator = "&" if "?" in path else "?"
    return RedirectResponse(url=f"{path}{separator}{query}", status_code=status_code)


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
    photo_stats: dict[str, int] | None = None,
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
    return templates.TemplateResponse("admin_event_form.html", context)


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
    photo_items = list_event_photo_items(db, event_id=event.id)
    photo_stats = build_photo_stats(photo_items)
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
        photo_count=len(photo_items),
        photo_items=photo_items,
        photo_stats=photo_stats,
        auto_slug=auto_slug,
        error_message=error_message,
        form_values=form_values,
    )


def build_photo_stats(photo_items: list) -> dict[str, int]:
    stats = {
        "total": len(photo_items),
        "uploaded": 0,
        "processing": 0,
        "ready": 0,
        "failed": 0,
        "queued_jobs": 0,
        "processing_jobs": 0,
        "completed_jobs": 0,
        "failed_jobs": 0,
    }
    for item in photo_items:
        photo_status = item.photo.status
        if photo_status in stats:
            stats[photo_status] += 1
        for job in item.photo.jobs:
            key = f"{job.status}_jobs"
            if key in stats:
                stats[key] += 1
    return stats


@app.get("/", response_class=JSONResponse)
async def root() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health", response_class=JSONResponse)
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    context = {
        "request": request,
        "title": "RaceFrame Admin Dashboard",
        "events": list_events(db),
        "message": request.query_params.get("message"),
        "message_level": request.query_params.get("level", "success"),
    }
    return templates.TemplateResponse("admin_dashboard.html", context)


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
    return templates.TemplateResponse("upload_events.html", context)


@app.get("/user", response_class=HTMLResponse)
def user_event_list_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    context = {
        "request": request,
        "title": "RaceFrame User Search",
        "events": list_user_events(db),
        "nav_home_url": "/user",
        "nav_home_label": "Find Photos",
    }
    return templates.TemplateResponse("user_event_list.html", context)


@app.get("/user/events/{event_id}", response_class=HTMLResponse)
def user_event_search_page(
    request: Request,
    event_id: str,
    q: str = "",
    face_session: str = "",
    db: Session = Depends(get_db),
) -> HTMLResponse:
    event = get_published_event(db, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="Published event not found.")

    search_term = q.strip()
    results = []
    matched_participants = []

    face_search_session = None
    face_search_results = []
    if face_session.strip():
        face_search_session, face_search_results = list_face_search_photo_items(
            db,
            event=event,
            face_session_id=face_session.strip(),
        )

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
    return templates.TemplateResponse("user_event_search.html", context)


@app.post("/user/events/{event_id}/participants/{participant_id}/selfies")
async def user_upload_selfies(
    event_id: str,
    participant_id: str,
    selfie_files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    event = get_published_event(db, event_id)
    participant = get_participant(db, participant_id)
    if event is None or participant is None or participant.event_id != event.id:
        raise HTTPException(status_code=404, detail="Participant not found for this event.")

    submitted_files = [selfie_file for selfie_file in selfie_files if selfie_file.filename and selfie_file.filename.strip()]
    if not submitted_files:
        return redirect_with_message(
            f"/user/events/{event.id}?{urlencode({'q': participant.bib_number})}",
            message="Choose at least one selfie before uploading.",
            level="error",
        )

    search_queued_count = 0
    failed_notes: list[str] = []
    face_search_session = None
    for selfie_file in submitted_files[:5]:
        content = await selfie_file.read()
        search_result = ingest_face_search_upload(
            db,
            event=event,
            participant=participant,
            search_session=face_search_session,
            file_name=selfie_file.filename or "face-photo",
            content_type=selfie_file.content_type or "",
            content=content,
        )
        if search_result.search_session is not None:
            face_search_session = search_result.search_session
        if search_result.success:
            search_queued_count += 1
        else:
            failed_notes.append(f"{search_result.file_name}: {search_result.message}")

    message = f"Queued {search_queued_count} temporary search selfie{'' if search_queued_count == 1 else 's'}."
    if failed_notes:
        message = f"{message} {' | '.join(failed_notes[:2])}"

    redirect_params = {"q": participant.bib_number}
    if face_search_session is not None:
        redirect_params["face_session"] = str(face_search_session.id)
    return redirect_with_message(
        f"/user/events/{event.id}?{urlencode(redirect_params)}",
        message=message,
        level="error" if failed_notes and not search_queued_count else "success",
    )


@app.post("/user/events/{event_id}/selfies")
async def user_upload_selfies_by_search(
    event_id: str,
    participant_query: str = Form(""),
    selfie_files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    event = get_published_event(db, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="Published event not found.")

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
            f"/user/events/{event.id}?{urlencode({'q': query})}",
            message="Choose at least one face photo before uploading.",
            level="error",
        )

    participant = find_event_participant(db, event_id=event.id, query=query)
    if participant is None:
        return redirect_with_message(
            f"/user/events/{event.id}?{urlencode({'q': query})}",
            message="No participant matched that bib or name. Check the spelling or bib number.",
            level="error",
        )

    search_queued_count = 0
    failed_notes: list[str] = []
    face_search_session = None
    for selfie_file in submitted_files[:5]:
        content = await selfie_file.read()
        search_result = ingest_face_search_upload(
            db,
            event=event,
            participant=participant,
            search_session=face_search_session,
            file_name=selfie_file.filename or "face-photo",
            content_type=selfie_file.content_type or "",
            content=content,
        )
        if search_result.search_session is not None:
            face_search_session = search_result.search_session
        if search_result.success:
            search_queued_count += 1
        else:
            failed_notes.append(f"{search_result.file_name}: {search_result.message}")

    message = f"Queued {search_queued_count} temporary search selfie{'' if search_queued_count == 1 else 's'} for {participant.bib_number} - {participant.full_name}."
    if failed_notes:
        message = f"{message} {' | '.join(failed_notes[:2])}"

    redirect_params = {"q": participant.bib_number}
    if face_search_session is not None:
        redirect_params["face_session"] = str(face_search_session.id)
    return redirect_with_message(
        f"/user/events/{event.id}?{urlencode(redirect_params)}",
        message=message,
        level="error" if failed_notes and not search_queued_count else "success",
    )


@app.post("/user/events/{event_id}/bib-only")
async def user_bib_only_search(
    event_id: str,
    participant_query: str = Form(""),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    event = get_published_event(db, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="Published event not found.")

    query = participant_query.strip()
    if not query:
        return redirect_with_message(
            f"/user/events/{event.id}",
            message="Enter your bib number or participant name before using bib-only search.",
            level="error",
        )

    participant = find_event_participant(db, event_id=event.id, query=query)
    if participant is None:
        return redirect_with_message(
            f"/user/events/{event.id}?{urlencode({'q': query})}",
            message="No participant matched that bib or name. Check the spelling or bib number.",
            level="error",
        )

    face_search_session, seed_count = create_bib_only_face_search_session(
        db,
        event_id=event.id,
        participant=participant,
    )
    message = (
        f"Started bib-only reinforced search for {participant.bib_number} - {participant.full_name}. "
        f"Temporary face seeds found: {seed_count}."
    )
    return redirect_with_message(
        f"/user/events/{event.id}?{urlencode({'q': participant.bib_number, 'face_session': str(face_search_session.id)})}",
        message=message,
        level="success" if seed_count else "error",
    )


def find_event_participant(db: Session, *, event_id, query: str) -> Participant | None:
    normalized_query = " ".join(query.lower().split())
    normalized_bib = normalize_match_token(query)
    candidates = (
        db.execute(
            select(Participant)
            .where(Participant.event_id == event_id)
            .order_by(Participant.full_name.asc())
        )
        .scalars()
        .all()
    )
    for candidate in candidates:
        if normalized_bib and normalize_match_token(candidate.bib_number) == normalized_bib:
            return candidate
        if normalized_query and normalized_query in " ".join(candidate.full_name.lower().split()):
            return candidate
    return None


@app.get("/user/events/{event_id}/photos/{photo_id}/download")
def user_download_photo(event_id: str, photo_id: str, db: Session = Depends(get_db)) -> Response:
    event = get_published_event(db, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="Published event not found.")

    photo = get_event_photo(db, event_id=event.id, photo_id=photo_id)
    if photo is None:
        raise HTTPException(status_code=404, detail="Photo not found.")

    try:
        photo_bytes = download_photo_bytes(photo.original_object_key)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"Photo download unavailable: {exc}") from exc

    return Response(
        content=photo_bytes,
        media_type=photo.content_type or "application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{photo.file_name}"',
        },
    )


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
        "photo_items": list_event_photo_items(db, event_id=event.id),
        "message": request.query_params.get("message"),
        "message_level": request.query_params.get("level", "success"),
        "nav_home_url": "/upload",
        "nav_home_label": "Published Events",
    }
    return templates.TemplateResponse("upload_event_detail.html", context)


@app.post("/upload/events/{event_id}")
async def photographer_upload_action(
    request: Request,
    event_id: str,
    photo_files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
) -> Response:
    event = get_published_event(db, event_id)
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
                    "ready_count": 0,
                    "failed_count": 0,
                    "results": [],
                },
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        return redirect_with_message(f"/upload/events/{event.id}", message=message, level="error")

    queued_count = 0
    failed_count = 0
    failure_notes: list[str] = []
    upload_results: list[dict[str, object | None]] = []

    for photo_file in submitted_files:
        content = await photo_file.read()
        result = ingest_photo_upload(
            db,
            event=event,
            file_name=photo_file.filename or "photo",
            content_type=photo_file.content_type or "",
            content=content,
        )
        latest_job = result.photo.jobs[0] if result.photo and result.photo.jobs else None
        upload_results.append(
            {
                "file_name": result.file_name,
                "success": result.success,
                "message": result.message,
                "photo_id": str(result.photo.id) if result.photo else None,
                "photo_url": safe_photo_access_url(result.photo.original_object_key) if result.photo else None,
                "photo_status": result.photo.status if result.photo else None,
                "job_status": latest_job.status if latest_job else None,
                "error_message": latest_job.error_message if latest_job else None,
            }
        )
        if result.success:
            queued_count += 1
        else:
            failed_count += 1
            failure_notes.append(f"{result.file_name}: {result.message}")

    message = f"Uploaded {len(submitted_files)} photo(s). Queued {queued_count}, failed {failed_count}."
    if failure_notes:
        message = f"{message} {' | '.join(failure_notes[:3])}"

    if wants_json:
        return JSONResponse(
            {
                "message": message,
                "processed_count": len(submitted_files),
                "ready_count": queued_count,
                "failed_count": failed_count,
                "results": upload_results,
            },
            status_code=status.HTTP_200_OK,
        )

    return redirect_with_message(
        f"/upload/events/{event.id}",
        message=message,
        level="error" if failed_count else "success",
    )


@app.post("/admin/events/new", response_class=HTMLResponse)
async def create_event_action(
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


@app.post("/admin/events/{event_id}/edit", response_class=HTMLResponse)
async def edit_event_action(
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
        return render_existing_event_form(request, event=event, db=db, error_message="Event date must use the YYYY-MM-DD format.", form_values=form_values)

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
    return redirect_with_message(f"/admin/events/{event.id}/edit", message="Event updated.")


@app.post("/admin/events/{event_id}/delete")
async def delete_event_action(event_id: str, db: Session = Depends(get_db)) -> RedirectResponse:
    event = get_event(db, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="Event not found.")

    delete_all_event_photos(db, event=event)
    delete_event(db, event=event)
    return redirect_with_message("/admin", message="Event deleted.")


@app.post("/admin/events/{event_id}/participants/upload")
async def upload_participants_action(
    event_id: str,
    participant_file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    event = get_event(db, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="Event not found.")

    file_name = participant_file.filename or ""
    if not file_name:
        return redirect_with_message(
            f"/admin/events/{event.id}/edit",
            message="Choose a participant file before uploading.",
            level="error",
        )

    content = await participant_file.read()
    try:
        rows = parse_participant_file(file_name, content)
        result = upsert_participants(db, event=event, rows=rows)
        rebuild_event_photo_matches(db, event=event)
    except ValueError as exc:
        return redirect_with_message(
            f"/admin/events/{event.id}/edit",
            message=str(exc),
            level="error",
        )

    message = (
        f"Participants imported. Added {result.inserted_rows}, updated {result.updated_rows}, "
        f"skipped {result.skipped_rows}."
    )
    return redirect_with_message(f"/admin/events/{event.id}/edit", message=message)


@app.post("/admin/events/{event_id}/participants/add")
async def add_participant_action(
    event_id: str,
    bib_number: str = Form(...),
    full_name: str = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    event = get_event(db, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="Event not found.")

    try:
        add_participant(db, event=event, bib_number=bib_number, full_name=full_name)
    except ValueError as exc:
        return redirect_with_message(f"/admin/events/{event.id}/edit", message=str(exc), level="error")

    rebuild_event_photo_matches(db, event=event)
    return redirect_with_message(f"/admin/events/{event.id}/edit", message="Participant added.")


@app.post("/admin/events/{event_id}/participants/{participant_id}/update")
async def update_participant_action(
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
    return redirect_with_message(f"/admin/events/{event.id}/edit", message="Participant updated.")


@app.post("/admin/events/{event_id}/participants/{participant_id}/delete")
async def delete_participant_action(
    event_id: str,
    participant_id: str,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    event = get_event(db, event_id)
    participant = get_participant(db, participant_id)
    if event is None or participant is None or participant.event_id != event.id:
        raise HTTPException(status_code=404, detail="Participant not found.")

    delete_participant(db, participant=participant)
    rebuild_event_photo_matches(db, event=event)
    return redirect_with_message(f"/admin/events/{event.id}/edit", message="Participant deleted.")


@app.post("/admin/events/{event_id}/participants/delete-all")
async def delete_all_participants_action(event_id: str, db: Session = Depends(get_db)) -> RedirectResponse:
    event = get_event(db, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="Event not found.")

    deleted_count = delete_all_participants(db, event=event)
    rebuild_event_photo_matches(db, event=event)
    return redirect_with_message(
        f"/admin/events/{event.id}/edit",
        message=f"Deleted {deleted_count} participant record{'s' if deleted_count != 1 else ''}.",
    )


@app.post("/admin/events/{event_id}/photos/{photo_id}/delete")
async def delete_photo_action(
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

    delete_photo(db, photo=photo)
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
    return templates.TemplateResponse("admin_photo_detail.html", context)


@app.post("/admin/events/{event_id}/photos/delete-all")
async def delete_all_event_photos_action(event_id: str, db: Session = Depends(get_db)) -> RedirectResponse:
    event = get_event(db, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="Event not found.")

    deleted_count = delete_all_event_photos(db, event=event)
    return redirect_with_message(
        f"/admin/events/{event.id}/edit",
        message=f"Deleted {deleted_count} photo record{'s' if deleted_count != 1 else ''}.",
    )
