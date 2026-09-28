"""
Fetch content from the RMS Supabase DB (read-only, via the REST API) and turn it into a Title.

content_type -> table:
    summary -> summaries,  bite -> bites,  journey -> journeys
A content_id matches either the human source_id (e.g. "SUM-737") or the uuid primary key.
Credentials come from config/.env (RMS_SUPABASE_API_URL + service-role secret); never logged.
"""
from __future__ import annotations

import os

import requests

from content.kitab_loader import Title, title_from_record

TABLE = {"summary": "summaries", "bite": "bites", "journey": "journeys"}
_FIELDS = "id,source_id,title,author,content,cover,tags"


def _base_and_headers() -> tuple[str, dict]:
    from config import _load_env
    _load_env()
    base = os.environ["RMS_SUPABASE_API_URL"].rstrip("/")  # already ends with /rest/v1
    key = os.environ["RMS_SUPABASE_SERVICE_ROLE_SECRET"]
    return base, {"apikey": key, "Authorization": f"Bearer {key}"}


def fetch_record(content_id: str, content_type: str) -> dict:
    if content_type not in TABLE:
        raise ValueError(f"content_type must be one of {list(TABLE)}, got {content_type!r}")
    base, headers = _base_and_headers()
    table = TABLE[content_type]
    # match source_id first (human id), then fall back to the uuid primary key
    for column in ("source_id", "id"):
        r = requests.get(f"{base}/{table}", headers=headers,
                         params={column: f"eq.{content_id}", "select": _FIELDS, "limit": 1}, timeout=30)
        r.raise_for_status()
        rows = r.json()
        if rows:
            return rows[0]
    raise LookupError(f"{content_type} {content_id!r} not found in RMS")


def fetch_title(content_id: str, content_type: str) -> Title:
    return title_from_record(fetch_record(content_id, content_type))
