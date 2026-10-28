import logging
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").strip().upper() or "INFO")


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
    copilot_admin_key: str
    max_run_input_tokens: int
    price_in_per_mtok: float
    price_out_per_mtok: float


def get_settings() -> Settings:
    return Settings(
        model_api_key=_env("MODEL_API_KEY"),
        meta_api_base=_env("META_API_BASE", "https://api.meta.ai/v1").rstrip("/"),
        meta_model=_env("META_MODEL", "muse-spark-1.3"),
        civicprocurebot_api_base=_env("CIVICPROCUREBOT_API_BASE").rstrip("/"),
        civicprocurebot_api_key=_env("CIVICPROCUREBOT_API_KEY"),
        sam_api_key=_env("SAM_API_KEY"),
        db_path=Path(_env("DB_PATH") or ROOT / "data" / "rfp_copilot.sqlite3"),
        copilot_admin_key=_env("COPILOT_ADMIN_KEY"),
        max_run_input_tokens=int(_env("MAX_RUN_INPUT_TOKENS") or 60000),
        price_in_per_mtok=float(_env("PRICE_IN_PER_MTOK") or 0.50),
        price_out_per_mtok=float(_env("PRICE_OUT_PER_MTOK") or 2.00),
    )
