"""Feishu long-connection (WebSocket) listener.

Receives card button callbacks and bot messages WITHOUT a public callback
URL: Feishu delivers the new card-platform interactive callback as a
``card.action.trigger`` event and bot messages as ``im.message.receive_v1``
events, both pushed over the app's outbound WebSocket channel (works from
localhost / behind NAT).

Run (from backend/):
    python -m feishu.ws_listener

Console prerequisites (open.feishu.cn -> this app):
- 事件与回调 -> 订阅方式: "使用长连接接收事件"
- 事件订阅: im.message.receive_v1（接收消息）, card.action.trigger（卡片回传）
"""

import asyncio
import json
import sys
import threading

import lark_oapi as lark
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    CallBackToast,
    P2CardActionTrigger,
    P2CardActionTriggerResponse,
)

from database import SessionLocal

from .assistant import run_assistant_blocking
from .bot import BotService
from .card_actions import dispatch_card_action
from .comment_delivery import deliver_student_comment
from .client import FeishuClient, FeishuConfig
from .sync import BitableSyncer

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _sync_response_blocking(response_id: int) -> None:
    """Fire-and-forget Bitable sync in a worker thread with its own event
    loop (the SDK's loop stays free to ack the callback quickly)."""

    async def _run() -> None:
        config = FeishuConfig()
        client = FeishuClient(config)
        db = SessionLocal()
        try:
            syncer = BitableSyncer(client, config)
            if syncer.available:
                await syncer.sync_response(db, response_id)
        except Exception:
            # Sync failures must never break the card interaction.
            pass
        finally:
            db.close()
            await client.close()

    asyncio.run(_run())


def _schedule_sync(response_id: int) -> None:
    threading.Thread(
        target=_sync_response_blocking, args=(response_id,), daemon=True
    ).start()


def _deliver_comment_blocking(student_id: int, draft_hash: str) -> None:
    asyncio.run(deliver_student_comment(student_id, draft_hash))


def _schedule_comment_delivery(student_id: int, draft_hash: str) -> None:
    threading.Thread(
        target=_deliver_comment_blocking,
        args=(student_id, draft_hash),
        daemon=True,
    ).start()


def on_card_action(data: P2CardActionTrigger) -> P2CardActionTriggerResponse:
    """card.action.trigger: a button on one of our interactive cards was
    clicked. Shares dispatch with the HTTP /api/feishu/card endpoint."""
    value = {}
    if data.event and data.event.action and isinstance(data.event.action.value, dict):
        value = data.event.action.value

    db = SessionLocal()
    try:
        result = dispatch_card_action(
            db,
            value,
            schedule_sync=_schedule_sync,
            schedule_comment_delivery=_schedule_comment_delivery,
        )
    except Exception:
        result = {"toast": {"type": "error", "content": "处理失败，请到网页端操作"}}
    finally:
        db.close()

    response = P2CardActionTriggerResponse()
    toast = (result or {}).get("toast") or {}
    if toast:
        # SDK model classes take a single dict, NOT keyword arguments.
        response.toast = CallBackToast(
            {"type": toast.get("type"), "content": toast.get("content")}
        )
    return response


def on_message_receive(data: P2ImMessageReceiveV1) -> None:
    """im.message.receive_v1: route teacher text to the 思辨星助教 dispatcher
    (shared with the HTTP /api/feishu/events endpoint)."""
    event = data.event if data else None
    message = event.message if event else None
    if message is None or not message.message_id:
        return
    if message.message_type != "text":
        return
    text = ""
    try:
        text = json.loads(message.content or "{}").get("text", "")
    except (TypeError, ValueError):
        text = ""
    sender = ""
    if event and event.sender and event.sender.sender_id:
        sender = event.sender.sender_id.open_id or ""
    threading.Thread(
        target=run_assistant_blocking,
        args=(sender, text, message.message_id),
        daemon=True,
    ).start()


def main() -> None:
    config = FeishuConfig()
    if not config.is_configured:
        print("未配置 FEISHU_APP_ID / FEISHU_APP_SECRET，请先填写 backend/.env")
        sys.exit(1)

    handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_card_action_trigger(on_card_action)
        .register_p2_im_message_receive_v1(on_message_receive)
        .build()
    )
    client = lark.ws.Client(
        config.app_id,
        config.app_secret,
        event_handler=handler,
        log_level=lark.LogLevel.INFO,
    )
    print("维学思辨星 · 飞书长连接监听启动中……")
    print("  卡片按钮回调: card.action.trigger")
    print("  机器人消息:   im.message.receive_v1")
    print("（保持本窗口运行；Ctrl+C 退出）")
    client.start()


if __name__ == "__main__":
    main()
