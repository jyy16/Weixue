"""In-app settings, ASR provider and system-mode endpoints."""

import json
import os

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

import settings_store
from asr import ASRClient
from database import Course, SystemSetting, get_db
from feishu.bitable import BitableService
from feishu.sync import bitable_is_configured, bitable_status
from grading.llm import LLMClient
from schemas import (
    ASRSettingOut, ASRSettingUpdate, SettingsItem, SettingsOut, SettingsUpdate,
    SystemModeAction, SystemModeOut,
)

from . import state


router = APIRouter(tags=["settings"])

@router.get("/api/settings/asr", response_model=ASRSettingOut)
def get_asr_settings(db: Session = Depends(get_db)):
    """Current ASR mode (mock vs real provider) and per-provider readiness."""
    return state.build_asr_settings(db)

@router.post("/api/settings/asr", response_model=ASRSettingOut)
def set_asr_settings(body: ASRSettingUpdate, db: Session = Depends(get_db)):
    """Persist the ASR provider selection (mock | openai | dashscope).

    Real providers must not share the database with demo/seed data: switching
    to openai/dashscope purges the marked demo course, and switching back to
    mock re-seeds it when the database is otherwise empty.
    """
    provider = body.provider.strip().lower()
    if provider not in ASRClient.SUPPORTED_PROVIDERS:
        raise HTTPException(
            400, f"invalid ASR provider: {provider} (allowed: {ASRClient.SUPPORTED_PROVIDERS})"
        )
    if provider != "mock":
        state.purge_demo_data(db)
    elif provider == "mock":
        state.seed_demo_if_empty(db)
    row = db.get(SystemSetting, "asr_provider")
    if row is None:
        row = SystemSetting(key="asr_provider", value=provider)
        db.add(row)
    else:
        row.value = provider
    db.commit()
    return state.build_asr_settings(db)

@router.get("/api/settings/mode", response_model=SystemModeOut)
def get_system_mode(db: Session = Depends(get_db)):
    """Capability matrix for the frontend mode switch (no secrets)."""
    asr_settings = state.build_asr_settings(db)
    current = asr_settings.provider
    asr_ready = next(
        (p.ready for p in asr_settings.providers if p.id == current),
        False,
    )
    config = state.feishu_client.config
    return SystemModeOut(
        demo_course_present=asr_settings.demo_data_present,
        asr_provider=current,
        asr_ready=asr_ready,
        llm_configured=bool(os.getenv("LLM_API_KEY", "").strip()),
        feishu_ready=bool(config.is_configured and config.teacher_open_id),
        bitable_ready=bitable_status(config).get("mode") == "ready",
    )

@router.post("/api/settings/mode", response_model=dict)
def set_system_mode(body: SystemModeAction, db: Session = Depends(get_db)):
    """One-click backend actions for the mode switch.

    enter_demo: seed the demo course (only when the DB has no courses, so real
                teacher data is never overwritten; the frontend demo mode has
                embedded data anyway).
    enter_real: purge the marked demo course (never touches real courses).
    """
    action = body.action.strip().lower()
    if action == "enter_demo":
        if db.query(Course).count() > 0:
            return {
                "ok": True,
                "action": action,
                "seeded": False,
                "message": "数据库已有课程，演示数据未重新生成（演示模式前端已内置数据）。",
            }
        state.seed_demo_if_empty(db)
        return {
            "ok": True,
            "action": action,
            "seeded": True,
            "message": "演示课程已生成，可切换前端为演示模式开始演示。",
        }
    if action == "enter_real":
        result = state.purge_demo_data(db)
        return {
            "ok": True,
            "action": action,
            **result,
            "message": "演示课程已清除" if result.get("purged") else "无演示课程可清除。",
        }
    raise HTTPException(400, "invalid action (enter_demo|enter_real)")

def _bitable_ids_configured(values: dict) -> bool:
    raw = values.get("feishu_bitable_table_ids") or ""
    if not raw:
        return False
    try:
        ids = json.loads(raw)
    except (TypeError, ValueError):
        return False
    return bool(isinstance(ids, dict) and ids.get("responses"))

def _build_settings_out(db: Session) -> SettingsOut:
    values = settings_store.get_all(db)
    items: dict[str, SettingsItem] = {}
    for key, value in values.items():
        secret = key in settings_store.SECRET_KEYS
        items[key] = SettingsItem(
            value=(settings_store.mask(value) if secret else value),
            has_value=bool(value),
            secret=secret,
        )
    asr_provider = values.get("asr_provider") or "mock"
    asr_configured = asr_provider == "mock" or bool(
        values.get("asr_api_key") or values.get("llm_api_key")
    )
    return SettingsOut(
        items=items,
        llm_configured=bool(values.get("llm_api_key")),
        asr_configured=asr_configured,
        feishu_configured=bool(values.get("feishu_app_id") and values.get("feishu_app_secret")),
        bitable_configured=bool(values.get("feishu_bitable_app_token")) and _bitable_ids_configured(values),
    )

@router.get("/api/settings", response_model=SettingsOut)
def get_settings(db: Session = Depends(get_db)):
    """Return the current in-app settings (secrets masked, no raw values)."""
    return _build_settings_out(db)

@router.put("/api/settings", response_model=SettingsOut)
def update_settings(body: SettingsUpdate, db: Session = Depends(get_db)):
    """Persist settings and hot-reload the runtime (LLM / ASR / Feishu)."""
    settings_store.update(db, body.settings)
    state.reload_runtime_settings(db)
    return _build_settings_out(db)

async def _test_llm(db: Session) -> dict:
    values = settings_store.get_all(db)
    if not values.get("llm_api_key"):
        return {"ok": False, "detail": "未配置 LLM API Key"}
    client = LLMClient(
        provider=values.get("llm_provider") or None,
        api_key=values.get("llm_api_key"),
        model=values.get("llm_model") or None,
        base_url=values.get("llm_base_url") or None,
    )
    if not client.model:
        return {"ok": False, "detail": "未配置 LLM 模型"}
    try:
        out = await client.chat(
            [{"role": "user", "content": "请只回复两个字：正常"}],
            temperature=0,
            max_tokens=16,
            timeout=30,
        )
        return {"ok": True, "detail": f"连接成功，模型返回：{out[:80]}"}
    except Exception as exc:  # noqa: BLE001 - surfaced as a user-friendly detail
        return {"ok": False, "detail": f"连接失败：{exc}"}

def _test_asr(db: Session) -> dict:
    values = settings_store.get_all(db)
    provider = values.get("asr_provider") or "mock"
    if provider == "mock":
        return {"ok": True, "detail": "演示转写（mock）无需配置"}
    key = values.get("asr_api_key") or values.get("llm_api_key")
    model = values.get("asr_model")
    if not key:
        return {"ok": False, "detail": "未配置 ASR API Key（且未复用 LLM Key）"}
    if not model:
        return {"ok": False, "detail": "未配置 ASR 模型"}
    if provider == "dashscope":
        import importlib.util
        if importlib.util.find_spec("dashscope") is None:
            return {"ok": False, "detail": "未安装 dashscope SDK（pip install dashscope）"}
    return {"ok": True, "detail": f"{provider} 配置已就绪（未做真实音频转写测试）"}

async def _test_feishu_bot() -> dict:
    status = await state.feishu_client.health_check()
    if status.get("status") == "auth_ok":
        return {"ok": True, "detail": "飞书鉴权成功（tenant_access_token 可获取）"}
    return {"ok": False, "detail": status.get("message") or "飞书未配置或鉴权失败"}

async def _test_bitable() -> dict:
    if not bitable_is_configured(state.feishu_client.config):
        return {"ok": False, "detail": "未配置多维表格（App Token / 表格 ID）"}
    try:
        service = BitableService(state.feishu_client)
        tables = await service.list_tables()
        items = (tables or {}).get("items") or []
        return {"ok": True, "detail": f"多维表格可访问，共 {len(items)} 张表"}
    except Exception as exc:  # noqa: BLE001 - surfaced as a user-friendly detail
        return {"ok": False, "detail": f"多维表格访问失败：{exc}"}

@router.post("/api/settings/test/{section}")
async def test_settings_section(section: str, db: Session = Depends(get_db)):
    """Test one settings section against its live provider (no secrets returned)."""
    key = section.strip().lower()
    if key == "llm":
        return await _test_llm(db)
    if key == "asr":
        return _test_asr(db)
    if key in ("feishu", "feishu_bot", "bot"):
        return await _test_feishu_bot()
    if key in ("feishu_bitable", "bitable"):
        return await _test_bitable()
    raise HTTPException(400, f"unknown settings section: {section}")
