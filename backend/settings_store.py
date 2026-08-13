"""Central runtime settings: DB (system_settings) first, then env fallback.

The in-app Settings page persists values into the ``system_settings`` table so
teachers can configure LLM / ASR / Feishu from the UI instead of editing
``backend/.env``. On save and at startup the values are pushed into
``os.environ`` so the existing env-driven clients (LLM / ASR / Feishu) keep
working without a deep refactor.

Keys are lowercase snake_case; each maps to an environment variable. Empty
string values are ignored on update so a masked secret is never overwritten.
"""

from __future__ import annotations

import os
from typing import Any

from sqlalchemy.orm import Session

from database import SystemSetting


# settings key -> environment variable name
SETTING_ENV: dict[str, str] = {
    "llm_provider": "LLM_PROVIDER",
    "llm_api_key": "LLM_API_KEY",
    "llm_model": "LLM_MODEL",
    "llm_base_url": "LLM_BASE_URL",
    "asr_provider": "ASR_PROVIDER",
    "asr_api_key": "ASR_API_KEY",
    "asr_model": "ASR_MODEL",
    "feishu_app_id": "FEISHU_APP_ID",
    "feishu_app_secret": "FEISHU_APP_SECRET",
    "feishu_base_url": "FEISHU_BASE_URL",
    "feishu_bitable_app_token": "FEISHU_BITABLE_APP_TOKEN",
    "feishu_bitable_table_ids": "FEISHU_BITABLE_TABLE_IDS",
    "feishu_verification_token": "FEISHU_VERIFICATION_TOKEN",
    "feishu_encrypt_key": "FEISHU_ENCRYPT_KEY",
    "feishu_teacher_open_id": "FEISHU_TEACHER_OPEN_ID",
    "feishu_web_base_url": "FEISHU_WEB_BASE_URL",
}

SECRET_KEYS = {
    "llm_api_key",
    "asr_api_key",
    "feishu_app_secret",
    "feishu_verification_token",
    "feishu_encrypt_key",
}


def get_setting(db: Session, key: str) -> str:
    """Return the effective value for one key: DB override, else env, else ''."""
    row = db.get(SystemSetting, key)
    if row is not None and row.value is not None and row.value.strip() != "":
        return row.value
    env_name = SETTING_ENV.get(key)
    if env_name:
        return (os.getenv(env_name) or "").strip()
    return ""


def get_all(db: Session) -> dict[str, str]:
    """Effective values for every known setting."""
    return {key: get_setting(db, key) for key in SETTING_ENV}


def update(db: Session, updates: dict[str, Any]) -> dict[str, str]:
    """Persist non-empty values from ``updates`` and return the new effective set.

    Empty / missing values are ignored, so a frontend that echoes a masked
    secret (or leaves a field blank) never wipes the real value.
    """
    for key, value in updates.items():
        if key not in SETTING_ENV:
            continue
        if value is None:
            continue
        text = str(value).strip()
        if text == "":
            continue
        row = db.get(SystemSetting, key)
        if row is None:
            db.add(SystemSetting(key=key, value=text))
        else:
            row.value = text
    db.commit()
    return get_all(db)


def push_to_env(settings: dict[str, str]) -> None:
    """Write effective settings into os.environ (keeps env-driven clients in sync)."""
    for key, value in settings.items():
        env_name = SETTING_ENV.get(key)
        if not env_name:
            continue
        if value:
            os.environ[env_name] = value
        else:
            os.environ.pop(env_name, None)


def mask(value: str) -> str:
    """Mask a secret for the settings API response."""
    if not value:
        return ""
    if len(value) <= 8:
        return "••••"
    return value[:4] + "••••••" + value[-4:]
