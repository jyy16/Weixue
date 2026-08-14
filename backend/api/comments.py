"""Comment draft generation, saving, sending and batch endpoints."""

import hashlib

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database import DebateTopic, Student, StudentResponse, get_db
from feishu.bot import BotService
from grading.evaluator import COMMENT_TONE_GUIDE
from grading.llm import LLMClient
from schemas import (
    BatchCommentOut, CommentOut, CommentRequest, CommentSaveRequest,
    CommentSendOut, CommentSendRequest,
)

from . import state


router = APIRouter(tags=["comments"])

@router.post("/api/courses/{cid}/comments", response_model=CommentOut)
async def generate_comment(cid: int, body: CommentRequest, db: Session = Depends(get_db)):
    """Generate a personalized comment draft using LLM, incorporating teacher tags & notes."""
    student = db.query(Student).get(body.student_id)
    if not student or student.course_id != cid:
        raise HTTPException(404, "Student not found")

    topics = db.query(DebateTopic).filter(DebateTopic.course_id == cid).order_by(DebateTopic.order).all()
    responses = db.query(StudentResponse).filter(
        StudentResponse.student_id == body.student_id
    ).all()
    resp_map = {r.topic_id: r for r in responses}

    dim_labels = {
        "position": "立意（观点鲜明）", "material": "选材（言之有物）",
        "structure": "结构（条理清晰）", "language": "语言（用词准确）",
        "perspective": "视角（换位思考）",
    }
    tier_labels = {"basic": "低年级（1-2年级）", "developing": "中年级（3-5年级）", "advancing": "高年级（6-7年级）"}

    # Collect per-topic teacher data
    topic_data = []
    reviewed_count = 0
    for topic in topics:
        r = resp_map.get(topic.id)
        if not r or not r.raw_text or not r.raw_text.strip():
            continue

        scores = r.teacher_dimension_scores or r.ai_dimension_scores
        is_reviewed = r.teacher_reviewed or False
        if is_reviewed:
            reviewed_count += 1

        score_parts = []
        if scores:
            for dim, rating in scores.items():
                label = dim_labels.get(dim, dim)
                score_parts.append(f"{label}: {rating}")

        tags = r.teacher_tags or r.ai_suggested_tags or []
        note = r.teacher_note or ""
        bonus = r.ai_bonus_flags or []

        topic_data.append({
            "order": topic.order,
            "title": topic.title,
            "scores": "、".join(score_parts) if score_parts else "无评分",
            "tags": tags,
            "note": note,
            "bonus": bonus,
            "reviewed": is_reviewed,
            "raw_text_preview": (r.raw_text[:80] + "...") if len(r.raw_text) > 80 else r.raw_text,
        })

    if reviewed_count == 0:
        return CommentOut(draft=f"提示：{student.name}同学尚无教师批改记录。请先在「评分」页面完成至少一个辩题的教师批改，再生成评语。")

    # Build LLM prompt
    topic_summaries = []
    for td in topic_data:
        lines = [f"辩题{td['order']}：{td['title']}"]
        lines.append(f"  评分：{td['scores']}")
        if td['tags']:
            lines.append(f"  教师选用标签：{'、'.join(td['tags'])}")
        if td['bonus']:
            lines.append(f"  加分项：{'、'.join(td['bonus'])}（已按规则升级评级）")
        if td['note']:
            lines.append(f"  教师批注：{td['note']}")
        if not td['reviewed']:
            lines.append("  （此题仅AI评分，教师未批改）")
        topic_summaries.append("\n".join(lines))

    prompt = (
        f"你是一位经验丰富的思辨课教师，正在为{student.name}同学（{tier_labels.get(student.cognitive_tier, '')}）撰写期末评语。\n\n"
        f"以下是{student.name}在各辩题中的表现数据和你的批改记录：\n\n"
        + "\n\n".join(topic_summaries)
        + "\n\n" + COMMENT_TONE_GUIDE
        + "\n请撰写一段150-250字的个性化评语，要求：\n"
        "1. 用温暖但专业的语气，直接对学生说话（用'你'而非'该生'）\n"
        "2. 具体引用教师选用的标签和批注中的观察（这些是你的第一手判断，优先使用）\n"
        "3. 先肯定亮点（结合具体辩题表现），再给出1-2个'可以更…'式的成长方向\n"
        "4. 给出一个具体的下一步建议\n"
        "5. 不要用模板化的开头（如'在本次课程中'），直接进入个性化内容\n"
        "6. 不要列出所有维度的分数，而是用自然语言描述表现\n"
    )

    try:
        llm = LLMClient()
        draft = await llm.chat(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
            max_tokens=600,
        )
        draft = draft.strip()
        if not draft:
            draft = _fallback_comment(student, topic_data, dim_labels)
    except Exception:
        # Fallback to template if LLM fails
        draft = _fallback_comment(student, topic_data, dim_labels)

    # Generating again is an explicit request for a new delivery item. Reset
    # the old result even if the model happens to produce identical text;
    # otherwise the UI would incorrectly keep showing the previous delivery as
    # covering this newly generated draft.
    student.comment_draft = draft
    state._reset_comment_delivery(student)
    db.commit()

    return CommentOut(draft=draft)

def _fallback_comment(student, topic_data, dim_labels):
    """Template fallback when LLM is unavailable."""
    name = student.name
    parts = [f"{name}同学在本次思辨课中表现积极。"]

    reviewed_topics = [t for t in topic_data if t['reviewed']]
    all_tags = []
    all_notes = []
    for t in reviewed_topics:
        all_tags.extend(t['tags'])
        if t['note']:
            all_notes.append(t['note'])

    if all_tags:
        unique_tags = list(dict.fromkeys(all_tags))[:4]
        parts.append(f"根据教师观察，你在「{'」「'.join(unique_tags)}」等方面有所体现。")

    if all_notes:
        parts.append(f"教师特别提到：{all_notes[0]}")

    if student.cognitive_tier == "basic":
        parts.append(
            "接下来可以试着把想法大声说完整，比如用“因为…所以…”把理由讲给老师和同学听。"
        )
    elif student.cognitive_tier == "advancing":
        parts.append(
            "接下来可以挑战更难的辩题：先说出自己的理由，再想想对方可能会怎么说，然后试着回应对方。"
        )
    else:
        parts.append(
            "接下来可以试着在表达观点时多举一个具体的例子，让理由更有画面感；"
            "也可以听听不同角度的想法，看看有没有新的可能。"
        )

    return "\n\n".join(parts)

@router.post("/api/courses/{cid}/comments/save")
def save_comment_draft(cid: int, body: CommentSaveRequest, db: Session = Depends(get_db)):
    """Save a comment draft for a student."""
    student = db.query(Student).get(body.student_id)
    if not student or student.course_id != cid:
        raise HTTPException(404, "Student not found")
    if body.draft != (student.comment_draft or ""):
        student.comment_draft = body.draft
        state._reset_comment_delivery(student)
    db.commit()
    return {"ok": True, "student_id": body.student_id}

@router.post("/api/courses/{cid}/comments/send", response_model=CommentSendOut)
async def send_comment(
    cid: int, body: CommentSendRequest, db: Session = Depends(get_db)
):
    """Save the final comment and, when Feishu credentials are ready, push an
    interactive confirmation card to the teacher via the bot."""
    student = db.query(Student).get(body.student_id)
    if not student or student.course_id != cid:
        raise HTTPException(404, "Student not found")
    if not body.draft.strip():
        raise HTTPException(400, "Comment draft is empty")
    final_draft = body.draft.strip()
    if final_draft != (student.comment_draft or ""):
        student.comment_draft = final_draft
        state._reset_comment_delivery(student)
    db.commit()

    status = "saved_pending_delivery"
    message = "评语已保存并标记待发送。"

    config = state.feishu_client.config
    if config.is_configured and config.teacher_open_id:
        try:
            # Pick the latest teacher-reviewed response for the card context.
            main_resp = next(
                (
                    r
                    for r in sorted(
                        student.responses, key=lambda r: r.id, reverse=True
                    )
                    if r.teacher_reviewed
                ),
                None,
            )
            card = BotService.build_comment_card(
                title=f"思辨星 · {student.name} 评语确认",
                content=_build_comment_card_content(student, main_resp, body.draft),
                course_id=cid,
                student_id=student.id,
                response_id=main_resp.id if main_resp else 0,
                comment_hash=hashlib.sha256(
                    body.draft.strip().encode("utf-8")
                ).hexdigest(),
                change_url=f"{config.web_base_url}/?tab=comments",
            )
            await BotService(state.feishu_client).send_card(config.teacher_open_id, card)
            status = "delivered"
            message = (
                "评语已保存，并已通过飞书机器人推送评语卡片；"
                "在飞书中点击卡片按钮即可确认评分或发送。"
            )
        except Exception as exc:
            message = (
                f"评语已保存；飞书机器人推送暂不可用（{exc}），"
                "已保留待重试。"
            )
    else:
        message = (
            "评语已保存并标记待发送；飞书机器人发送通道待联调"
            "（未配置 FEISHU_TEACHER_OPEN_ID），评语不会丢失。"
        )
    return CommentSendOut(
        ok=True,
        student_id=body.student_id,
        status=status,
        message=message,
    )

def _build_comment_card_content(student, response, draft: str) -> str:
    """Markdown body for the Feishu comment card."""
    dim_labels = {
        "position": "立意（观点鲜明）",
        "material": "选材（言之有物）",
        "structure": "结构（条理清晰）",
        "language": "语言（用词准确）",
        "perspective": "视角（换位思考）",
    }
    lines = [f"**学生**：{student.name}（{student.cognitive_tier}）"]
    if response is not None and response.topic:
        lines.append(f"**辩题**：{response.topic.title}")
    lines.append("")
    lines.append(f"**评语**\n{draft}")
    if response is not None:
        scores = response.teacher_dimension_scores or response.ai_dimension_scores
        if scores:
            score_parts = [
                f"{dim_labels.get(dim, dim)}：{rating}"
                for dim, rating in scores.items()
            ]
            lines.append(f"\n**评分摘要**：{'、'.join(score_parts)}")
        bonus = response.ai_bonus_flags or []
        if bonus:
            lines.append(f"**加分项**：{'、'.join(bonus)}（已按规则升级评级）")
        tags = response.teacher_tags or response.ai_suggested_tags or []
        if tags:
            lines.append(f"**亮点标签**：{'、'.join(tags)}")
        if response.teacher_note:
            lines.append(f"**教师备注**：{response.teacher_note}")
    return "\n".join(lines)

@router.post("/api/courses/{cid}/comments/batch", response_model=BatchCommentOut)
async def batch_generate_comments(cid: int, db: Session = Depends(get_db)):
    """Generate comments for all students who have at least one teacher-reviewed topic."""
    students = db.query(Student).filter(Student.course_id == cid).all()
    topics = db.query(DebateTopic).filter(DebateTopic.course_id == cid).order_by(DebateTopic.order).all()

    dim_labels = {
        "position": "立意（观点鲜明）", "material": "选材（言之有物）",
        "structure": "结构（条理清晰）", "language": "语言（用词准确）",
        "perspective": "视角（换位思考）",
    }
    tier_labels = {"basic": "低年级（1-2年级）", "developing": "中年级（3-5年级）", "advancing": "高年级（6-7年级）"}

    results = []
    llm = LLMClient()

    for student in students:
        responses = db.query(StudentResponse).filter(
            StudentResponse.student_id == student.id
        ).all()
        resp_map = {r.topic_id: r for r in responses}

        # Check if any topic is teacher-reviewed
        reviewed_count = 0
        topic_summaries = []
        for topic in topics:
            r = resp_map.get(topic.id)
            if not r or not r.raw_text or not r.raw_text.strip():
                continue
            is_reviewed = r.teacher_reviewed or False
            if is_reviewed:
                reviewed_count += 1

            scores = r.teacher_dimension_scores or r.ai_dimension_scores
            score_parts = []
            if scores:
                for dim, rating in scores.items():
                    label = dim_labels.get(dim, dim)
                    score_parts.append(f"{label}: {rating}")
            tags = r.teacher_tags or r.ai_suggested_tags or []
            note = r.teacher_note or ""
            bonus = r.ai_bonus_flags or []

            lines = [f"辩题{topic.order}：{topic.title}"]
            lines.append(f"  评分：{'、'.join(score_parts) if score_parts else '无评分'}")
            if tags:
                lines.append(f"  教师选用标签：{'、'.join(tags)}")
            if bonus:
                lines.append(f"  加分项：{'、'.join(bonus)}（已按规则升级评级）")
            if note:
                lines.append(f"  教师批注：{note}")
            if not is_reviewed:
                lines.append("  （此题仅AI评分，教师未批改）")
            topic_summaries.append("\n".join(lines))

        if reviewed_count == 0:
            results.append({
                "student_id": student.id,
                "student_name": student.name,
                "draft": "",
                "error": "无教师批改记录，跳过",
            })
            continue

        prompt = (
            f"你是一位经验丰富的思辨课教师，正在为{student.name}同学（{tier_labels.get(student.cognitive_tier, '')}）撰写期末评语。\n\n"
            f"以下是{student.name}在各辩题中的表现数据和你的批改记录：\n\n"
            + "\n\n".join(topic_summaries)
            + "\n\n请撰写一段150-250字的个性化评语，要求：\n"
            "1. 用温暖但专业的语气，直接对学生说话（用'你'而非'该生'）\n"
            "2. 具体引用教师选用的标签和批注中的观察（这些是你的第一手判断，优先使用）\n"
            "3. 先肯定亮点（结合具体辩题表现），再指出1-2个提升方向\n"
            "4. 给出一个具体的下一步建议\n"
            "5. 不要用模板化的开头（如'在本次课程中'），直接进入个性化内容\n"
            "6. 不要列出所有维度的分数，而是用自然语言描述表现\n"
        )

        try:
            draft = await llm.chat(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7,
                max_tokens=600,
            )
            draft = draft.strip()
            if not draft:
                raise ValueError("LLM 返回空内容")
            # Batch regeneration always starts a new delivery cycle, even if
            # the model happens to return identical text.
            student.comment_draft = draft
            state._reset_comment_delivery(student)
            db.commit()
            results.append({
                "student_id": student.id,
                "student_name": student.name,
                "draft": draft,
                "error": None,
            })
        except Exception as e:
            results.append({
                "student_id": student.id,
                "student_name": student.name,
                "draft": "",
                "error": str(e),
            })

    return BatchCommentOut(results=results)
