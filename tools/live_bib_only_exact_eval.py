from __future__ import annotations

import json
import uuid

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config import settings
from app.db import SessionLocal
from app.face import (
    cosine_similarity,
    create_face_search_results,
    dedupe_seed_embeddings,
    extract_match_candidates,
    face_seed_quality,
    is_dominant_face,
    normalize_embedding_payload,
    normalize_match_token,
    select_cluster_seed_embeddings,
)
from app.models import Event, FaceSearchResult, FaceSearchSession, Participant, Photo, PhotoFaceDetection
from app.models import utc_now


EVENT_ID = uuid.UUID("97f47890-d71b-4b8c-be3d-9b0fd07ea364")


def bib_sort_key(participant: Participant) -> tuple[int, str]:
    try:
        return int(participant.bib_number), participant.bib_number
    except ValueError:
        return 10**9, participant.bib_number


def exact_bib_values(detections, bib_number: str) -> tuple[str, ...]:
    target = normalize_match_token(bib_number)
    if not target:
        return ()

    matched: list[str] = []
    seen: set[str] = set()
    for detection in detections:
        for candidate in extract_match_candidates(detection.detected_text):
            if candidate == target and detection.detected_text not in seen:
                seen.add(detection.detected_text)
                matched.append(detection.detected_text)
    return tuple(matched[:3])


def classify_face_score(score: float | None) -> str:
    if score is None:
        return "none"
    if score >= settings.face_strong_similarity_threshold:
        return "strong"
    if score >= settings.face_medium_similarity_threshold:
        return "medium"
    if score >= settings.face_candidate_similarity_threshold:
        return "weak"
    return "none"


def select_exact_bib_seed_embeddings(session, *, event_id: uuid.UUID, participant: Participant) -> list[list[float]]:
    photos = (
        session.execute(
            select(Photo)
            .where(Photo.event_id == event_id)
            .options(selectinload(Photo.detections), selectinload(Photo.face_detections))
            .order_by(Photo.created_at.desc())
        )
        .scalars()
        .unique()
        .all()
    )

    seed_embeddings: list[list[float]] = []
    cluster_faces: list[PhotoFaceDetection] = []
    for photo in photos:
        if not exact_bib_values(photo.detections, participant.bib_number):
            continue

        good_faces = sorted(
            [face for face in photo.face_detections if normalize_embedding_payload(face.embedding_json)],
            key=face_seed_quality,
            reverse=True,
        )
        if not good_faces:
            continue
        if len(good_faces) == 1:
            seed_embeddings.append(good_faces[0].embedding_json)
        elif is_dominant_face(good_faces):
            seed_embeddings.append(good_faces[0].embedding_json)
        else:
            cluster_faces.extend(good_faces[:4])

        if len(seed_embeddings) >= 3:
            return dedupe_seed_embeddings(seed_embeddings)[:3]

    seed_embeddings.extend(select_cluster_seed_embeddings(cluster_faces))
    return dedupe_seed_embeddings(seed_embeddings)[:3]


def apply_exact_reinforcement(session, *, search_session: FaceSearchSession) -> int:
    if search_session.participant_id is None:
        return 0
    participant = session.get(Participant, search_session.participant_id)
    if participant is None:
        return 0

    created_total = 0
    reinforced_detection_ids: set[uuid.UUID] = set()
    for _round in range(2):
        candidates = (
            session.execute(
                select(FaceSearchResult)
                .where(FaceSearchResult.search_session_id == search_session.id)
                .order_by(FaceSearchResult.similarity_score.desc())
            )
            .scalars()
            .all()
        )
        created_this_round = 0
        for result in candidates:
            if created_total >= settings.face_reinforcement_max_embeddings:
                return created_total

            photo = session.get(Photo, result.photo_id)
            detection = session.get(PhotoFaceDetection, result.photo_face_detection_id)
            if photo is None or detection is None:
                continue
            if not exact_bib_values(photo.detections, participant.bib_number):
                continue
            if result.similarity_score < settings.face_reinforcement_similarity_threshold:
                continue

            embedding = normalize_embedding_payload(detection.embedding_json)
            if not embedding or detection.id in reinforced_detection_ids:
                continue

            create_face_search_results(
                session,
                event_id=search_session.event_id,
                search_session_id=search_session.id,
                embedding=embedding,
            )
            reinforced_detection_ids.add(detection.id)
            created_total += 1
            created_this_round += 1

        if created_this_round == 0:
            break

    return created_total


def create_exact_bib_session(session, *, event_id: uuid.UUID, participant: Participant) -> tuple[FaceSearchSession, int, int]:
    search_session = FaceSearchSession(event_id=event_id, participant_id=participant.id, status="processing")
    session.add(search_session)
    session.flush()

    seed_embeddings = select_exact_bib_seed_embeddings(session, event_id=event_id, participant=participant)
    for embedding in seed_embeddings[:3]:
        create_face_search_results(
            session,
            event_id=event_id,
            search_session_id=search_session.id,
            embedding=embedding,
        )

    reinforced_count = 0
    if seed_embeddings:
        reinforced_count = apply_exact_reinforcement(session, search_session=search_session)

    search_session.status = "completed"
    search_session.finished_at = utc_now()
    session.flush()
    return search_session, len(seed_embeddings), reinforced_count


def list_exact_result_filenames(session, *, event_id: uuid.UUID, participant: Participant, search_session: FaceSearchSession) -> list[str]:
    result_rows = (
        session.execute(
            select(FaceSearchResult).where(
                FaceSearchResult.search_session_id == search_session.id,
                FaceSearchResult.event_id == event_id,
            )
        )
        .scalars()
        .all()
    )

    best_face_score_by_photo: dict[uuid.UUID, float] = {}
    for result in result_rows:
        existing = best_face_score_by_photo.get(result.photo_id)
        if existing is None or result.similarity_score > existing:
            best_face_score_by_photo[result.photo_id] = result.similarity_score

    photos = (
        session.execute(
            select(Photo)
            .where(Photo.event_id == event_id)
            .options(selectinload(Photo.detections))
            .order_by(Photo.created_at.desc())
        )
        .scalars()
        .unique()
        .all()
    )

    filenames: set[str] = set()
    for photo in photos:
        face_score = best_face_score_by_photo.get(photo.id)
        face_strength = classify_face_score(face_score)
        has_exact_bib = bool(exact_bib_values(photo.detections, participant.bib_number))
        if face_strength == "strong" or has_exact_bib:
            filenames.add(photo.file_name)
    return sorted(filenames)


def main() -> None:
    with SessionLocal() as session:
        event = session.get(Event, EVENT_ID)
        if event is None:
            raise SystemExit(f"Event not found: {EVENT_ID}")

        participants = (
            session.execute(
                select(Participant)
                .where(Participant.event_id == EVENT_ID)
                .order_by(Participant.bib_number.asc())
            )
            .scalars()
            .all()
        )
        participants = sorted(participants, key=bib_sort_key)

        results = []
        created_sessions: list[FaceSearchSession] = []
        try:
            for participant in participants:
                try:
                    search_session, seed_count, reinforced_count = create_exact_bib_session(
                        session,
                        event_id=EVENT_ID,
                        participant=participant,
                    )
                    created_sessions.append(search_session)
                    images = list_exact_result_filenames(
                        session,
                        event_id=EVENT_ID,
                        participant=participant,
                        search_session=search_session,
                    )
                    results.append(
                        {
                            "bib_number": participant.bib_number,
                            "participant_id": str(participant.id),
                            "full_name": participant.full_name,
                            "face_session_id": str(search_session.id),
                            "seed_count": seed_count,
                            "reinforced_count": reinforced_count,
                            "images": images,
                        }
                    )
                except Exception as exc:  # noqa: BLE001
                    results.append(
                        {
                            "bib_number": participant.bib_number,
                            "participant_id": str(participant.id),
                            "full_name": participant.full_name,
                            "error": str(exc),
                            "images": [],
                        }
                    )
            print(json.dumps(results, indent=2))
        finally:
            for search_session in created_sessions:
                session.delete(search_session)
            session.commit()


if __name__ == "__main__":
    main()
