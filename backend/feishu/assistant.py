"""思辨星助教 executor: turns a parsed teacher message into a real action.

Both the WebSocket long connection (``feishu.ws_listener``) and the HTTP event
callback (``feishu.routes.feishu_events``) funnel teacher text here so the bot
behaves identically on both channels. The bot's "hands" (comment cards, prep
plan cards, student delivery) are existing code; this module adds the intent
dispatch and knowledge-base answers.

Only the configured teacher (FEISHU_TEACHER_OPEN_ID) plus the optional
comma-separated FEISHU_ASSISTANT_OPEN_IDS allow-list are served; everyone else
is ignored to keep group chats quiet.
"""

import asyncio
import hashlib
import os
from typing import Optional

from database import Course, PrepPlan, SessionLocal, Student

from .assistant_intents import (
    HELP_MENU,
    allowed_open_ids,
    faq_answer,
    parse_intent,
)
from .bot import BotService
from .client import FeishuClient, FeishuConfig

# 演示/默认课程；生产环境应由教师↔课程绑定决定。
COURSE_ID = 1


def _find_student(db, name: Optional[str]):
    if not name:
        return None
    return (
        db.query(Student)
        .filter(Student.course_id == COURSE_ID, Student.name == name.strip())
        .first()
    )


async def _push_prep_plan(bot: BotService, teacher_open_id: str) -> None:
    # Lazy import: api.prep -> api.state -> feishu.routes would create an
    # import cycle if loaded at module import time.
    from api.prep import (
        _build_prep_plan_card_content,
        _prep_insights,
        _prep_topic_rows,
    )

    db = SessionLocal()
    try:
        course = db.get(Course, COURSE_ID)
        if course is None:
            await bot.send_text(teacher_open_id, "未找到演示课程，请先在网页端创建班级。")
            return
        plan = db.query(PrepPlan).filter(PrepPlan.course_id == COURSE_ID).first()
        if plan is None or not plan.lesson_plan:
            await bot.send_text(
                teacher_open_id,
                "还没有保存的讲课计划，请先在「备课辅助」页保存讲评顺序与备注。",
            )
            return
        rows = {r["topic_id"]: r for r in _prep_topic_rows(COURSE_ID, db)}
        insights = _prep_insights(COURSE_ID, db)
        content = _build_prep_plan_card_content(course, plan, rows, insights)
        config = FeishuConfig()
        card = BotService.build_prep_plan_card(
            title=f"思辨星 · {course.class_name} 讲评计划",
            content=content,
            course_id=COURSE_ID,
            change_url=f"{config.web_base_url}/?tab=prep",
        )
        await bot.send_card(teacher_open_id, card)
    finally:
        db.close()


async def _push_comment_card(
    bot: BotService,
    teacher_open_id: str,
    student_name: str,
) -> None:
    from api.comments import _build_comment_card_content

    db = SessionLocal()
    try:
        student = _find_student(db, student_name)
        if student is None:
            await bot.send_text(teacher_open_id, f"课程里没找到叫「{student_name}」的学生。")
            return
        if not (student.comment_draft or "").strip():
            await bot.send_text(
                teacher_open_id,
                f"{student.name} 还没有评语草稿，请先在「评语生成」页生成。",
            )
            return
        latest = next(
            (
                r
                for r in sorted(student.responses or [], key=lambda r: r.id, reverse=True)
                if r.teacher_reviewed
            ),
            None,
        )
        config = FeishuConfig()
        card = BotService.build_comment_card(
            title=f"思辨星 · {student.name} 评语确认",
            content=_build_comment_card_content(student, latest, student.comment_draft),
            course_id=COURSE_ID,
            student_id=student.id,
            response_id=latest.id if latest else 0,
            comment_hash=hashlib.sha256(
                student.comment_draft.strip().encode("utf-8")
            ).hexdigest(),
            change_url=f"{config.web_base_url}/?tab=comments",
        )
        await bot.send_card(teacher_open_id, card)
    finally:
        db.close()


async def _reply_student_summary(bot: BotService, message_id: str, student_name: str) -> None:
    db = SessionLocal()
    try:
        student = _find_student(db, student_name)
        if student is None:
            await bot.reply_text(message_id, f"课程里没找到叫「{student_name}」的学生。")
            return
        lines = [f"{student.name}（{student.grade}年级 · {student.cognitive_tier}）"]
        reviewed = next(
            (
                r
                for r in sorted(student.responses or [], key=lambda r: r.id, reverse=True)
                if r.teacher_reviewed or r.ai_dimension_scores
            ),
            None,
        )
        if reviewed is not None:
            scores = reviewed.teacher_dimension_scores or reviewed.ai_dimension_scores or {}
            if scores:
                lines.append("最新评估：" + "、".join(f"{d}:{v}" for d, v in scores.items()))
            if reviewed.teacher_rating:
                lines.append(f"课堂评级：{reviewed.teacher_rating}")
        if student.comment_draft:
            lines.append(f"评语草稿：{student.comment_draft[:60]}{'…' if len(student.comment_draft) > 60 else ''}")
        lines.append(f"评语投递：{student.comment_delivery_status or 'not_sent'}")
        await bot.reply_text(message_id, "\n".join(lines))
    finally:
        db.close()


async def _dispatch(client: FeishuClient, teacher_open_id: str, text: str, message_id: str) -> None:
    bot = BotService(client)
    intent = parse_intent(text)
    action = intent["action"]
    if action == "prep_plan":
        await _push_prep_plan(bot, teacher_open_id)
    elif action == "comment_card":
        await _push_comment_card(bot, teacher_open_id, intent.get("student") or "")
    elif action == "comment_help":
        await bot.reply_text(
            message_id,
            "想推送哪位学生的评语确认卡？直接说「小雨的评语」或「评语 大伟」。",
        )
    elif action == "student_summary":
        await _reply_student_summary(bot, message_id, intent.get("student") or "")
    elif action == "faq":
        await bot.reply_text(message_id, faq_answer(text))
    else:
        await bot.reply_text(message_id, HELP_MENU)


async def run_assistant_async(
    sender_open_id: str,
    text: str,
    message_id: str,
) -> None:
    """Async entry used by the HTTP event route (background task)."""
    config = FeishuConfig()
    allowed = allowed_open_ids(
        config.teacher_open_id,
        os.getenv("FEISHU_ASSISTANT_OPEN_IDS", ""),
    )
    if not allowed or (sender_open_id or "").strip() not in allowed:
        return
    client = FeishuClient(config)
    try:
        await _dispatch(client, (config.teacher_open_id or "").strip(), text, message_id)
    finally:
        await client.close()


def run_assistant_blocking(
    sender_open_id: str,
    text: str,
    message_id: str,
) -> None:
    """Blocking entry used by the ws_listener daemon thread."""
    try:
        asyncio.run(run_assistant_async(sender_open_id, text, message_id))
    except Exception:  # noqa: BLE001 - never let a reply break the listener
        pass
