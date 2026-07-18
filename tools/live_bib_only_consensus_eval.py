from __future__ import annotations

import json
import math
import re
import uuid

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config import settings
from app.db import SessionLocal
from app.face import (
    cosine_similarity,
    create_face_search_results,
    dedupe_seed_embeddings,
    face_seed_quality,
    is_dominant_face,
    normalize_embedding_payload,
)
from app.models import Event, FaceSearchResult, FaceSearchSession, Participant, Photo, PhotoFaceDetection
from app.models import utc_now


EVENT_ID = uuid.UUID("97f47890-d71b-4b8c-be3d-9b0fd07ea364")
ASCII_TOKEN_PATTERN = re.compile(r"[A-Z0-9]+")
ASCII_DIGIT_PATTERN = re.compile(r"[0-9]+")
BIB_ONLY_SEED_PHOTO_LIMIT = int(getattr(settings, "bib_only_seed_photo_limit", 10))
BIB_ONLY_SEED_CLUSTER_MIN_PHOTOS = int(getattr(settings, "bib_only_seed_cluster_min_photos", 2))
BIB_ONLY_SEED_CLUSTER_MAJORITY_RATIO = float(getattr(settings, "bib_only_seed_cluster_majority_ratio", 0.60))
BIB_ONLY_SEED_CLUSTER_LEAD_RATIO = float(getattr(settings, "bib_only_seed_cluster_lead_ratio", 1.50))


def bib_sort_key(participant: Participant) -> tuple[int, str]:
    try:
        return int(participant.bib_number), participant.bib_number
    except ValueError:
        return 10**9, participant.bib_number


def normalize_ascii_token(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "", value).upper()


def extract_ascii_candidates(value: str) -> list[str]:
    normalized_value = value.upper()
    tokens = ASCII_TOKEN_PATTERN.findall(normalized_value)
    raw_candidate = normalize_ascii_token(value)
    if raw_candidate:
        tokens.insert(0, raw_candidate)
    tokens.extend(ASCII_DIGIT_PATTERN.findall(normalized_value))

    seen: set[str] = set()
    candidates: list[str] = []
    for token in tokens:
        normalized = normalize_ascii_token(token)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        candidates.append(normalized)
    return candidates


def exact_bib_values(detections, bib_number: str) -> tuple[str, ...]:
    target = normalize_ascii_token(bib_number)
    if not target:
        return ()

    matched: list[str] = []
    seen: set[str] = set()
    for detection in detections:
        for candidate in extract_ascii_candidates(detection.detected_text):
            if candidate != target or detection.detected_text in seen:
                continue
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


def cluster_seed_faces(faces: list[PhotoFaceDetection]) -> list[list[PhotoFaceDetection]]:
    clusters: list[list[PhotoFaceDetection]] = []
    for face in faces:
        embedding = normalize_embedding_payload(face.embedding_json)
        if not embedding:
            continue
        best_cluster = None
        best_score = 0.0
        for cluster in clusters:
            score = max(cosine_similarity(embedding, other.embedding_json) for other in cluster)
            if score > best_score:
                best_score = score
                best_cluster = cluster
        if best_cluster is not None and best_score >= settings.face_reinforcement_similarity_threshold:
            best_cluster.append(face)
        else:
            clusters.append([face])
    return clusters


def average_face_embeddings(faces: list[PhotoFaceDetection]) -> list[float]:
    embeddings = [normalize_embedding_payload(face.embedding_json) for face in faces]
    embeddings = [embedding for embedding in embeddings if embedding]
    if not embeddings:
        return []
    dimension = len(embeddings[0])
    if any(len(embedding) != dimension for embedding in embeddings):
        return []

    averaged = [sum(embedding[index] for embedding in embeddings) / len(embeddings) for index in range(dimension)]
    norm = math.sqrt(sum(value * value for value in averaged))
    if norm <= 0:
        return []
    return [value / norm for value in averaged]


def select_consensus_seed_embeddings(
    faces: list[PhotoFaceDetection],
    *,
    support_photo_count: int,
) -> list[list[float]]:
    if not faces or support_photo_count <= 0:
        return []

    clusters = cluster_seed_faces(faces)
    eligible_clusters = []
    for cluster in clusters:
        photo_ids = {face.photo_id for face in cluster}
        if len(photo_ids) < BIB_ONLY_SEED_CLUSTER_MIN_PHOTOS:
            continue
        avg_quality = sum(face_seed_quality(face) for face in cluster) / len(cluster)
        eligible_clusters.append((len(photo_ids), avg_quality, cluster))

    if not eligible_clusters:
        return []

    eligible_clusters.sort(key=lambda item: (item[0], item[1]), reverse=True)
    best_photo_count, _best_quality, best_cluster = eligible_clusters[0]
    required_support = max(
        BIB_ONLY_SEED_CLUSTER_MIN_PHOTOS,
        math.ceil(support_photo_count * BIB_ONLY_SEED_CLUSTER_MAJORITY_RATIO),
    )
    if best_photo_count < required_support:
        return []

    if len(eligible_clusters) > 1:
        second_photo_count = eligible_clusters[1][0]
        has_clear_count_lead = best_photo_count >= second_photo_count + 2
        has_clear_ratio_lead = best_photo_count >= math.ceil(second_photo_count * BIB_ONLY_SEED_CLUSTER_LEAD_RATIO)
        if not has_clear_count_lead and not has_clear_ratio_lead:
            return []

    best_faces = sorted(best_cluster, key=face_seed_quality, reverse=True)
    embeddings = [average_face_embeddings(best_faces)]
    embeddings.extend(face.embedding_json for face in best_faces[:2])
    return dedupe_seed_embeddings(embeddings)[:3]


def select_consensus_bib_seed_embeddings(session, *, event_id: uuid.UUID, participant: Participant) -> list[list[float]]:
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

    candidate_faces: list[PhotoFaceDetection] = []
    exact_bib_photo_count = 0
    usable_face_photo_count = 0
    single_photo_single_face_embedding: list[float] | None = None
    for photo in photos:
        if not exact_bib_values(photo.detections, participant.bib_number):
            continue

        exact_bib_photo_count += 1
        if exact_bib_photo_count > BIB_ONLY_SEED_PHOTO_LIMIT:
            break

        good_faces = sorted(
            [face for face in photo.face_detections if normalize_embedding_payload(face.embedding_json)],
            key=face_seed_quality,
            reverse=True,
        )
        if not good_faces:
            continue
        usable_face_photo_count += 1
        if len(good_faces) == 1:
            candidate_faces.append(good_faces[0])
            if exact_bib_photo_count == 1:
                single_photo_single_face_embedding = normalize_embedding_payload(good_faces[0].embedding_json)
        elif is_dominant_face(good_faces):
            candidate_faces.append(good_faces[0])
        else:
            candidate_faces.extend(good_faces[:4])

    consensus_embeddings = select_consensus_seed_embeddings(
        candidate_faces,
        support_photo_count=usable_face_photo_count,
    )
    if consensus_embeddings:
        return consensus_embeddings

    if exact_bib_photo_count == 1 and usable_face_photo_count == 1 and single_photo_single_face_embedding:
        return [single_photo_single_face_embedding]
    return []


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


def create_consensus_bib_session(session, *, event_id: uuid.UUID, participant: Participant) -> tuple[FaceSearchSession, int, int]:
    search_session = FaceSearchSession(event_id=event_id, participant_id=participant.id, status="processing")
    session.add(search_session)
    session.flush()

    seed_embeddings = select_consensus_bib_seed_embeddings(session, event_id=event_id, participant=participant)
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


def list_consensus_result_filenames(session, *, event_id: uuid.UUID, participant: Participant, search_session: FaceSearchSession) -> list[str]:
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
                    search_session, seed_count, reinforced_count = create_consensus_bib_session(
                        session,
                        event_id=EVENT_ID,
                        participant=participant,
                    )
                    created_sessions.append(search_session)
                    images = list_consensus_result_filenames(
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
