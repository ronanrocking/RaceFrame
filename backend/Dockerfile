# syntax=docker/dockerfile:1.7
FROM python:3.14-slim-bookworm@sha256:86f975aca15cf04a40b399eebede9aea7c82eae084d1f1a0a6ef6bcaae871a30 AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN python -m venv /opt/venv

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

LABEL org.opencontainers.image.title="RaceFrame backend" \
      org.opencontainers.image.description="RaceFrame FastAPI service" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.source="https://github.com/ronanrocking/RaceFrame"

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONHASHSEED=random \
    HOME=/home/raceframe

RUN groupadd --gid "${APP_GID}" raceframe \
    && useradd --uid "${APP_UID}" --gid "${APP_GID}" --create-home --shell /usr/sbin/nologin raceframe \
    && rm -rf /usr/local/lib/python3.12/site-packages/pip* /usr/local/bin/pip*

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY --chown=${APP_UID}:${APP_GID} app ./app
COPY --chown=${APP_UID}:${APP_GID} alembic.ini ./alembic.ini
COPY --chown=${APP_UID}:${APP_GID} migrations ./migrations

USER ${APP_UID}:${APP_GID}

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/livez', timeout=2).read()"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2", "--limit-concurrency", "100", "--backlog", "256", "--timeout-keep-alive", "5", "--no-server-header"]
