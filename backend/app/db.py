from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, declarative_base, sessionmaker

from .config import settings


if settings.database_url.startswith("sqlite"):
    connect_args = {"check_same_thread": False}
    engine_options: dict[str, object] = {}
else:
    connect_args = {
        "connect_timeout": 5,
        "application_name": "raceframe-backend",
        "options": (
            "-c statement_timeout=30000 "
            "-c lock_timeout=5000 "
            "-c idle_in_transaction_session_timeout=30000"
        ),
    }
    engine_options = {
        "pool_size": 10,
        "max_overflow": 5,
        "pool_timeout": 10,
        "pool_recycle": 1_800,
    }

engine = create_engine(
    settings.database_url,
    future=True,
    pool_pre_ping=True,
    connect_args=connect_args,
    **engine_options,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
Base = declarative_base()


def get_db() -> Generator[Session, None, None]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
