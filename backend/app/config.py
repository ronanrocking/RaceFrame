from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./raceframe.db")
    app_name: str = os.getenv("APP_NAME", "RaceFrame Backend")


settings = Settings()
