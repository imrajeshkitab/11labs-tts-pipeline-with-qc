"""
Central config: loads env vars once, exposes typed settings.
Everything else in this package reads config through here — never `os.environ` directly —
so there's one place to change if env var names or loading strategy change later.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent  # fine-tuning-tts/
DATA_DIR = PACKAGE_ROOT / "data"

_ENV_LOADED = False


def _load_env() -> None:
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    try:
        from dotenv import load_dotenv
        # package-local .env first (self-contained/shared copy), then the parent project .env
        for candidate in (PACKAGE_ROOT / ".env", PROJECT_ROOT / ".env"):
            if candidate.exists():
                load_dotenv(candidate)
                break
    except ImportError:
        pass  # fall back to whatever is already in the process environment
    _ENV_LOADED = True


@dataclass(frozen=True)
class Settings:
    gemini_api_key: str
    eleven_labs_api_key: str
    voice_ids: dict[str, str] = field(default_factory=dict)

    normalization_cache_path: Path = DATA_DIR / "normalization_cache.json"
    pronunciation_dictionary_path: Path = DATA_DIR / "pronunciation_dictionary.json"

    gemini_model: str = "gemini-2.5-flash"


def load_settings() -> Settings:
    _load_env()
    eleven_key = os.environ.get("ELEVEN_LABS_API_KEY_ADMIN") or os.environ.get("ELEVEN_LABS_API_KEY")
    missing = [k for k, v in (("GEMINI_API_KEY", os.environ.get("GEMINI_API_KEY")),
                              ("ELEVEN_LABS_API_KEY_ADMIN", eleven_key)) if not v]
    if missing:
        raise RuntimeError(
            f"Missing required env var(s): {', '.join(missing)}. "
            f"Expected them in {PROJECT_ROOT / '.env'}"
        )

    voice_ids = {
        key: value
        for key, value in os.environ.items()
        if key.startswith("ancient_wisdom_speaker_")
    }

    return Settings(
        gemini_api_key=os.environ["GEMINI_API_KEY"],
        eleven_labs_api_key=eleven_key,
        voice_ids=voice_ids,
    )
