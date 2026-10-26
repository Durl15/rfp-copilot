import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


@dataclass(frozen=True)
class Settings:
    model_api_key: str
    meta_api_base: str
    meta_model: str
    civicprocurebot_api_base: str
    civicprocurebot_api_key: str
    sam_api_key: str
    db_path: Path


def get_settings() -> Settings:
    return Settings(
        model_api_key=_env("MODEL_API_KEY"),
        meta_api_base=_env("META_API_BASE", "https://api.meta.ai/v1").rstrip("/"),
        meta_model=_env("META_MODEL", "muse-spark-1.3"),
        civicprocurebot_api_base=_env("CIVICPROCUREBOT_API_BASE").rstrip("/"),
        civicprocurebot_api_key=_env("CIVICPROCUREBOT_API_KEY"),
        sam_api_key=_env("SAM_API_KEY"),
        db_path=Path(_env("DB_PATH") or ROOT / "data" / "rfp_copilot.sqlite3"),
    )
