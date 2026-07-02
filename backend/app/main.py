from __future__ import annotations

from datetime import date
from pathlib import Path
from urllib.parse import urlencode
from uuid import uuid4

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
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
from .models import Participant


BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
ADMIN_SESSION_COOKIE = "raceframe_admin_session"

app = FastAPI(title=settings.app_name)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


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
    return RedirectResponse(url=f"{path}?{query}", status_code=status_code)


def render_event_form(
    request: Request,
    *,
    page_title: str,
    submit_label: str,
    form_action: str,
    event: object | None,
    participant_count: int = 0,
    participants: list[Participant] | None = None,
    auto_slug: str | None = None,
    error_message: str | None = None,
    form_values: dict[str, str] | None = None,
) -> HTMLResponse:
    context = {
        "request": request,
        "page_title": page_title,
        "submit_label": submit_label,
        "form_action": form_action,
        "status_options": EVENT_STATUSES,
        "event": event,
        "participant_count": participant_count,
        "participants": participants or [],
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
        auto_slug=auto_slug,
        error_message=error_message,
        form_values=form_values,
    )


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
    return redirect_with_message(f"/admin/events/{event.id}/edit", message="Participant deleted.")


@app.post("/admin/events/{event_id}/participants/delete-all")
async def delete_all_participants_action(event_id: str, db: Session = Depends(get_db)) -> RedirectResponse:
    event = get_event(db, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="Event not found.")

    deleted_count = delete_all_participants(db, event=event)
    return redirect_with_message(
        f"/admin/events/{event.id}/edit",
        message=f"Deleted {deleted_count} participant record{'s' if deleted_count != 1 else ''}.",
    )
