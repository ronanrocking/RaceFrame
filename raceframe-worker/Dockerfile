# syntax=docker/dockerfile:1.7
FROM python:3.14-slim-bookworm@sha256:86f975aca15cf04a40b399eebede9aea7c82eae084d1f1a0a6ef6bcaae871a30 AS builder

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential cmake \
    && rm -rf /var/lib/apt/lists/* \
    && python -m venv /opt/venv

COPY requirements-build.lock /tmp/requirements-build.lock
COPY requirements.lock /tmp/requirements.lock
RUN /opt/venv/bin/python -m pip install --require-hashes --requirement /tmp/requirements-build.lock \
    && /opt/venv/bin/python -m pip install --no-build-isolation --require-hashes \
        --requirement /tmp/requirements.lock \
    && /opt/venv/bin/python -m pip check \
    && /opt/venv/bin/python -m pip uninstall --yes pip setuptools wheel

FROM python:3.14-slim-bookworm@sha256:86f975aca15cf04a40b399eebede9aea7c82eae084d1f1a0a6ef6bcaae871a30 AS runtime

ARG APP_UID=10001
ARG APP_GID=10001
ARG VCS_REF=unknown
ARG BUILD_DATE=unknown

LABEL org.opencontainers.image.title="RaceFrame worker" \
      org.opencontainers.image.description="RaceFrame OCR and face-processing worker" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.source="https://github.com/ronanrocking/RaceFrame"

ENV DEBIAN_FRONTEND=noninteractive \
    PATH="/opt/venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONHASHSEED=random \
    HOME=/home/raceframe

RUN apt-get update \
    && apt-get install -y --no-install-recommends libglib2.0-0 libgl1 libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid "${APP_GID}" raceframe \
    && useradd --uid "${APP_UID}" --gid "${APP_GID}" --create-home --shell /usr/sbin/nologin raceframe \
    && install -d -o "${APP_UID}" -g "${APP_GID}" /home/raceframe/.insightface \
    && rm -rf /usr/local/lib/python3.11/site-packages/pip* /usr/local/bin/pip*

WORKDIR /worker

COPY --from=builder /opt/venv /opt/venv
COPY --chown=${APP_UID}:${APP_GID} worker.py ./worker.py

USER ${APP_UID}:${APP_GID}

HEALTHCHECK --interval=60s --timeout=5s --start-period=180s --retries=3 \
    CMD ["python", "-c", "import os; os.kill(1, 0)"]

CMD ["python", "worker.py"]
