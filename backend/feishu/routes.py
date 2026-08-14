"""FastAPI routes exposing the Feishu integration (health, bitable, events, cards).

Endpoints:
- GET  /api/feishu/health              - config status (no secrets)
- GET  /api/feishu/bitable/status      - Bitable config + binding counts
- POST /api/feishu/bitable/sync        - manual one-way sync of a course
- POST /api/feishu/bitable/pull        - manual pull of teacher edits back
- POST /api/feishu/events              - event subscription callback (M4)
- POST /api/feishu/card                - interactive card callback (M4)
"""

import hashlib
import hmac
import json
from typing import Optional

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Request,
)
from sqlalchemy.orm import Session

from database import (
    FeishuBinding,
    SessionLocal,
    get_db,
)

from .assistant import run_assistant_async
from .bot import BotService
from .card_actions import dispatch_card_action
from .comment_delivery import deliver_student_comment
from .client import FeishuClient, FeishuConfig
from .sync import (
    BitableSyncer,
    TABLE_KEYS,
    bitable_is_configured,
    bitable_status,
)

router = APIRouter(prefix="/api/feishu", tags=["feishu"])

_feishu_config = FeishuConfig()
_client: Optional[FeishuClient] = None


def get_client() -> FeishuClient:
    global _client
    if _client is None:
        _client = FeishuClient(_feishu_config)
    return _client


def reload_config() -> None:
    """Rebuild the module-level Feishu config after an in-app settings change."""
    global _feishu_config, _client
    _feishu_config = FeishuConfig()
    _client = None


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.close()
        _client = None


async def _sync_response_after_review(response_id: int) -> None:
    """Fire-and-forget Bitable sync after a card callback confirms a review."""
    db = SessionLocal()
    try:
        syncer = BitableSyncer(get_client(), _feishu_config)
        if syncer.available:
            await syncer.sync_response(db, response_id)
    except Exception:
        # Sync failures are reported via /api/feishu/bitable/status only.
        pass
    finally:
        db.close()


def _verify_event_signature(
    raw_body: bytes, timestamp: str, nonce: str, signature: str
) -> bool:
    """sha256(timestamp + nonce + encrypt_key + raw_body) per official docs."""
    bs = (
        f"{timestamp}{nonce}{_feishu_config.encrypt_key}".encode("utf-8")
        + raw_body
    )
    return hmac.compare_digest(hashlib.sha256(bs).hexdigest(), signature or "")


@router.get("/health")
async def health():
    status = await get_client().health_check()
    return {
        "status": status["status"],
        "feishu": _feishu_config.summary(),
        "auth": status,
        "bitable": bitable_status(_feishu_config),
    }


@router.get("/bitable/status")
def bitable_status_endpoint(db: Session = Depends(get_db)):
    """Bitable configuration and how many local entities are bound so far."""
    result = bitable_status(_feishu_config)
    counts = {}
    for key in TABLE_KEYS:
        counts[key] = (
            db.query(FeishuBinding).filter(FeishuBinding.table_key == key).count()
        )
    result["bindings"] = counts
    return result


@router.post("/bitable/sync")
async def bitable_sync(body: dict, db: Session = Depends(get_db)):
    """Manually sync one course (course + topics + students + responses)."""
    if not bitable_is_configured(_feishu_config):
        raise HTTPException(
            503,
            "FEISHU_BITABLE_APP_TOKEN / FEISHU_BITABLE_TABLE_IDS not configured",
        )
    try:
        course_id = int(body.get("course_id") or 0)
    except (TypeError, ValueError):
        raise HTTPException(400, "course_id must be an integer")
    if course_id <= 0:
        raise HTTPException(400, "course_id is required")
    syncer = BitableSyncer(get_client(), _feishu_config)
    return await syncer.sync_course(db, course_id)


@router.post("/bitable/pull")
async def bitable_pull(body: dict, db: Session = Depends(get_db)):
    """Manually pull teacher-owned edits (教师评分/标签/批注/状态, 评语草稿)
    from Bitable back into the local DB for one course."""
    if not bitable_is_configured(_feishu_config):
        raise HTTPException(
            503,
            "FEISHU_BITABLE_APP_TOKEN / FEISHU_BITABLE_TABLE_IDS not configured",
        )
    try:
        course_id = int(body.get("course_id") or 0)
    except (TypeError, ValueError):
        raise HTTPException(400, "course_id must be an integer")
    if course_id <= 0:
        raise HTTPException(400, "course_id is required")
    syncer = BitableSyncer(get_client(), _feishu_config)
    return await syncer.pull_course(db, course_id)


@router.post("/events")
async def feishu_events(
    request: Request,
    background_tasks: BackgroundTasks,
):
    """Feishu event subscription callback.

    Handles url_verification (challenge), verifies the X-Lark-Signature when an
    Encrypt Key is configured, and dispatches im.message.receive_v1 to the bot.
    """
    raw_body = await request.body()
    timestamp = request.headers.get("X-Lark-Request-Timestamp", "")
    nonce = request.headers.get("X-Lark-Request-Nonce", "")
    signature = request.headers.get("X-Lark-Signature", "")
    if _feishu_config.encrypt_key and signature and not _verify_event_signature(
        raw_body, timestamp, nonce, signature
    ):
        raise HTTPException(401, "invalid event signature")
    try:
        body = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise HTTPException(400, "invalid event body")
    try:
        event = BotService.handle_event(_feishu_config, body)
    except ValueError as exc:
        raise HTTPException(403, str(exc))
    if "challenge" in event:
        return event
    if event.get("type") == "im.message.receive_v1":
        payload = event.get("event") or {}
        message = payload.get("message") or {}
        message_id = message.get("message_id", "")
        msg_type = message.get("message_type", "")
        text = ""
        if msg_type == "text":
            try:
                text = json.loads(message.get("content", "{}")).get("text", "")
            except (TypeError, ValueError):
                text = ""
        sender = ((payload.get("sender") or {}).get("sender_id") or {}).get(
            "open_id", ""
        )
        if message_id:
            background_tasks.add_task(run_assistant_async, sender, text, message_id)
    return {"code": 0, "msg": "ack"}


@router.post("/card")
async def feishu_card(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Interactive card callback (button clicks).

    Verifies `X-Lark-Signature` (sha1 over timestamp + nonce + verification
    token + raw body), decrypts when Encrypt Key is configured, checks the
    callback `header.token`, then delegates to the shared dispatcher (also used
    by the WebSocket long-connection listener in feishu.ws_listener).
    """
    raw_body = await request.body()
    timestamp = request.headers.get("X-Lark-Request-Timestamp", "")
    nonce = request.headers.get("X-Lark-Request-Nonce", "")
    signature = request.headers.get("X-Lark-Signature", "")
    if not BotService.verify_card_signature(
        _feishu_config, raw_body, timestamp, nonce, signature
    ):
        raise HTTPException(401, "invalid card callback signature")
    try:
        plaintext = BotService.decrypt_card_payload(_feishu_config, raw_body)
        payload = json.loads(plaintext.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise HTTPException(400, str(exc))

    header = payload.get("header") or {}
    if (
        _feishu_config.verification_token
        and header.get("token") != _feishu_config.verification_token
    ):
        raise HTTPException(403, "card callback token mismatch")

    event = payload.get("event") or {}
    action = event.get("action") or {}
    value = action.get("value") or {}
    return dispatch_card_action(
        db,
        value,
        schedule_sync=lambda rid: background_tasks.add_task(
            _sync_response_after_review, rid
        ),
        schedule_comment_delivery=lambda sid, draft_hash: background_tasks.add_task(
            deliver_student_comment, sid, draft_hash, get_client()
        ),
    )
