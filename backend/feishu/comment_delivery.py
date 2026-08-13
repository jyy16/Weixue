"""Reliable, per-student Feishu delivery for teacher-approved comments."""

import asyncio
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.exc import OperationalError

from database import SessionLocal, Student

from .bot import BotService
from .client import FeishuClient, FeishuConfig


def _read_delivery_snapshot(
    student_id: int,
    expected_hash: str,
) -> Optional[tuple[str, str, str, str]]:
    """Read the reserved delivery data in a short-lived synchronous session."""
    db = SessionLocal()
    try:
        student = db.get(Student, student_id)
        if not student:
            return None
        if (
            student.comment_delivery_status != "sending"
            or student.comment_delivery_hash != expected_hash
        ):
            return None
        return (
            (student.feishu_open_id or "").strip(),
            (student.comment_draft or "").strip(),
            student.name,
            (student.phone or "").strip(),
        )
    finally:
        db.close()


def _persist_delivery_result_once(
    student_id: int,
    expected_hash: str,
    *,
    status: str,
    error: str,
    delivered_at: Optional[datetime],
) -> bool:
    db = SessionLocal()
    try:
        updated = (
            db.query(Student)
            .filter(
                Student.id == student_id,
                Student.comment_delivery_status == "sending",
                Student.comment_delivery_hash == expected_hash,
            )
            .update(
                {
                    Student.comment_delivery_status: status,
                    Student.comment_delivery_error: error[:500],
                    Student.comment_delivered_at: delivered_at,
                },
                synchronize_session=False,
            )
        )
        db.commit()
        return bool(updated)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _persist_resolved_open_id_once(
    student_id: int,
    expected_hash: str,
    open_id: str,
) -> bool:
    """Persist a phone-resolved open_id without overwriting a newer attempt."""
    db = SessionLocal()
    try:
        updated = (
            db.query(Student)
            .filter(
                Student.id == student_id,
                Student.comment_delivery_status == "sending",
                Student.comment_delivery_hash == expected_hash,
            )
            .update({Student.feishu_open_id: open_id}, synchronize_session=False)
        )
        db.commit()
        return bool(updated)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


async def _persist_delivery_result(
    student_id: int,
    expected_hash: str,
    *,
    status: str,
    error: str = "",
    delivered_at: Optional[datetime] = None,
    attempts: int = 3,
) -> bool:
    """Persist a result without overwriting a newer draft/delivery attempt.

    SQLite permits one writer at a time. Its connection timeout handles normal
    contention; these short retries cover the remaining narrow lock window.
    """
    for attempt in range(attempts):
        try:
            # SQLite and SQLAlchemy are synchronous. Offload the whole
            # transaction so lock waits cannot block Feishu's event loop.
            return await asyncio.to_thread(
                _persist_delivery_result_once,
                student_id,
                expected_hash,
                status=status,
                error=error,
                delivered_at=delivered_at,
            )
        except OperationalError as exc:
            if "database is locked" not in str(exc).lower() or attempt == attempts - 1:
                raise
        await asyncio.sleep(0.15 * (attempt + 1))
    return False


async def deliver_student_comment(
    student_id: int,
    expected_hash: str,
    client: Optional[FeishuClient] = None,
) -> None:
    """Deliver the reserved draft and persist success/failure for UI feedback.

    ``expected_hash`` prevents a queued task from sending a draft that the
    teacher edited after clicking the card button.
    """
    owns_client = client is None
    if client is None:
        client = FeishuClient(FeishuConfig())

    try:
        # Read the reserved delivery snapshot, then close the session before
        # the network request. This avoids holding a SQLite transaction or
        # connection while waiting for Feishu.
        snapshot = await asyncio.to_thread(
            _read_delivery_snapshot,
            student_id,
            expected_hash,
        )
        if snapshot is None:
            return
        open_id, comment, name, phone = snapshot

        if not open_id and phone:
            try:
                mobile = phone
                if mobile.isdigit() and len(mobile) == 11 and mobile.startswith("1"):
                    mobile = "+86" + mobile
                result = await client.request(
                    "POST",
                    "/contact/v3/users/batch_get_id",
                    params={"user_id_type": "open_id"},
                    json_body={"mobiles": [mobile]},
                )
                user_list = (result or {}).get("user_list") or []
                open_id = next(
                    (
                        str(item.get("user_id") or "")
                        for item in user_list
                        if item.get("user_id")
                    ),
                    "",
                )
                if open_id:
                    try:
                        await asyncio.to_thread(
                            _persist_resolved_open_id_once,
                            student_id,
                            expected_hash,
                            open_id,
                        )
                    except Exception:
                        pass  # best effort: delivery proceeds with the resolved id
            except Exception as exc:  # noqa: BLE001 - surfaced as delivery error
                await _persist_delivery_result(
                    student_id,
                    expected_hash,
                    status="failed",
                    error=f"根据手机号解析飞书账号失败：{exc}",
                )
                return

        if not open_id or not comment:
            await _persist_delivery_result(
                student_id,
                expected_hash,
                status="failed",
                error="学生账号未绑定或评语为空",
            )
            return

        card = BotService.build_student_comment_card(
            student_name=name,
            comment=comment,
        )
        try:
            await BotService(client).send_card(open_id, card)
        except Exception as exc:
            await _persist_delivery_result(
                student_id,
                expected_hash,
                status="failed",
                error=str(exc),
            )
        else:
            await _persist_delivery_result(
                student_id,
                expected_hash,
                status="delivered",
                delivered_at=datetime.now(timezone.utc),
            )
    finally:
        if owns_client:
            await client.close()
