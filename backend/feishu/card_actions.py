"""Shared dispatch for interactive-card button actions.

Both delivery channels route here so they cannot drift:
- HTTP callback: POST /api/feishu/card (feishu.routes.feishu_card)
- WebSocket long connection: card.action.trigger (feishu.ws_listener)

The input is the button's ``value`` dict; the output is the raw response dict
(e.g. {"toast": {...}}) that each channel serializes its own way (JSON body for
HTTP, P2CardActionTriggerResponse for the long connection).
"""

from typing import Callable, Optional

from sqlalchemy.orm import Session

from database import PrepPlan, Student, StudentResponse

from .reviews import apply_teacher_review


def dispatch_card_action(
    db: Session,
    value: dict,
    schedule_sync: Optional[Callable[[int], None]] = None,
    schedule_comment_delivery: Optional[Callable[[int, str], None]] = None,
) -> dict:
    """Dispatch one card button action by its ``value["action"]`` name."""
    if not isinstance(value, dict):
        value = {}
    action_name = str(value.get("action") or "")

    if action_name == "review_confirm":
        return _card_review_confirm(db, value, schedule_sync)
    if action_name == "request_change":
        return {
            "toast": {
                "type": "info",
                "content": "请在网页端“批改”页调整评分与评语",
            }
        }
    if action_name == "send_comment":
        return _card_send_comment(db, value, schedule_comment_delivery)
    if action_name == "prep_confirm":
        return _card_prep_confirm(db, value)
    return {"toast": {"type": "warning", "content": "未知操作，请升级应用"}}


def _card_review_confirm(
    db: Session, value: dict, schedule_sync: Optional[Callable[[int], None]]
) -> dict:
    """Confirm an AI review from a card button: persist review + calibration."""
    try:
        rid = int(value.get("response_id") or 0)
    except (TypeError, ValueError):
        return {"toast": {"type": "error", "content": "卡片参数无效"}}
    resp = db.get(StudentResponse, rid)
    if not resp:
        return {"toast": {"type": "error", "content": "作答记录不存在"}}

    apply_teacher_review(
        db,
        resp,
        dimension_scores=value.get("dimension_scores")
        or resp.teacher_dimension_scores
        or resp.ai_dimension_scores,
        confidence_override=value.get("confidence_override")
        or resp.teacher_confidence_override,
        tags=(
            value.get("tags")
            if value.get("tags") is not None
            else resp.teacher_tags or resp.ai_suggested_tags
        ),
        note=value.get("note")
        if value.get("note") is not None
        else resp.teacher_note
        or "",
        rating=value.get("rating") or resp.teacher_rating or "",
    )
    db.commit()
    if schedule_sync is not None:
        schedule_sync(rid)
    return {"toast": {"type": "success", "content": "评分已确认，校准记录已保存"}}


def _card_send_comment(
    db: Session,
    value: dict,
    schedule_comment_delivery: Optional[Callable[[int, str], None]],
) -> dict:
    """Validate and atomically reserve a comment delivery for one student."""
    try:
        student_id = int(value.get("student_id") or 0)
    except (TypeError, ValueError):
        return {"toast": {"type": "error", "content": "卡片参数无效"}}

    student = db.get(Student, student_id)
    if not student:
        return {"toast": {"type": "error", "content": "学生不存在"}}
    try:
        course_id = int(value.get("course_id") or 0)
    except (TypeError, ValueError):
        course_id = 0
    if course_id and student.course_id != course_id:
        return {"toast": {"type": "error", "content": "卡片与学生信息不匹配"}}
    if not (student.feishu_open_id or "").strip() and not (student.phone or "").strip():
        return {
            "toast": {
                "type": "warning",
                "content": f"{student.name}尚未绑定飞书账号或手机号，请先在学生管理中绑定",
            }
        }
    if not (student.comment_draft or "").strip():
        return {
            "toast": {"type": "warning", "content": f"{student.name}暂无可发送的评语"}
        }
    if schedule_comment_delivery is None:
        return {
            "toast": {
                "type": "warning",
                "content": "发送服务未启动，评语仍保存在系统中",
            }
        }

    import hashlib

    draft_hash = hashlib.sha256(student.comment_draft.strip().encode("utf-8")).hexdigest()
    card_hash = str(value.get("comment_hash") or "").strip()
    if card_hash and card_hash != draft_hash:
        return {
            "toast": {
                "type": "warning",
                "content": "评语已在网页端修改，请重新推送确认卡后再发送",
            }
        }
    if (
        student.comment_delivery_hash == draft_hash
        and student.comment_delivery_status == "delivered"
    ):
        return {
            "toast": {"type": "info", "content": f"这份评语已发送给{student.name}"}
        }
    if (
        student.comment_delivery_hash == draft_hash
        and student.comment_delivery_status == "sending"
    ):
        return {
            "toast": {"type": "info", "content": f"正在发送给{student.name}，请勿重复点击"}
        }

    # Conditional UPDATE makes duplicate clicks safe even when two callbacks
    # arrive at almost the same time.
    reserved = (
        db.query(Student)
        .filter(
            Student.id == student.id,
            ~(
                (Student.comment_delivery_hash == draft_hash)
                & Student.comment_delivery_status.in_(["sending", "delivered"])
            ),
        )
        .update(
            {
                Student.comment_delivery_status: "sending",
                Student.comment_delivery_hash: draft_hash,
                Student.comment_delivery_error: "",
                Student.comment_delivered_at: None,
            },
            synchronize_session=False,
        )
    )
    db.commit()
    if not reserved:
        db.expire_all()
        current = db.get(Student, student.id)
        if current and current.comment_delivery_status == "delivered":
            content = f"这份评语已发送给{student.name}"
        else:
            content = f"正在发送给{student.name}，请勿重复点击"
        return {"toast": {"type": "info", "content": content}}
    try:
        schedule_comment_delivery(student.id, draft_hash)
    except Exception as exc:
        student = db.get(Student, student.id)
        if student and student.comment_delivery_hash == draft_hash:
            student.comment_delivery_status = "failed"
            student.comment_delivery_error = str(exc)[:500]
            db.commit()
        return {
            "toast": {
                "type": "error",
                "content": "发送任务启动失败，评语仍保存在系统中",
            }
        }
    return {
        "toast": {
            "type": "success",
            "content": f"已提交发送给{student.name}，投递结果将记录在学生管理中",
        }
    }


def _card_prep_confirm(db: Session, value: dict) -> dict:
    """Confirm the lesson-prep plan from a card button (teacher decision gate)."""
    try:
        cid = int(value.get("course_id") or 0)
    except (TypeError, ValueError):
        return {"toast": {"type": "error", "content": "卡片参数无效"}}
    plan = db.query(PrepPlan).filter(PrepPlan.course_id == cid).first()
    if not plan:
        return {"toast": {"type": "error", "content": "讲评计划不存在，请先在网页端生成"}}
    if not plan.lesson_plan:
        return {"toast": {"type": "warning", "content": "讲评计划还没有选辩题"}}
    plan.confirmed = True
    db.commit()
    return {"toast": {"type": "success", "content": "讲评计划已确认"}}
