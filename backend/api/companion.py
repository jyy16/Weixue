"""Live-classroom companion dialogue and feedback endpoints."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database import CompanionTurn, StudentResponse, get_db
from schemas import (
    CompanionTurnCreate, CompanionTurnOut, StudentResponseOut, SuggestTurnOut,
)

from . import state


router = APIRouter(tags=["companion"])

@router.get("/api/companion/{rid}", response_model=list[CompanionTurnOut])
def get_companion_turns(rid: int, db: Session = Depends(get_db)):
    """Return the full dialogue history of a response (teacher/student both read it)."""
    resp = db.query(StudentResponse).get(rid)
    if not resp:
        raise HTTPException(404, "Response not found")
    return resp.companion_turns

@router.post("/api/responses/{rid}/turns", response_model=StudentResponseOut)
def append_companion_turn(rid: int, body: CompanionTurnCreate, db: Session = Depends(get_db)):
    """Append a turn (student answer / adopted AI suggestion / teacher question)."""
    resp = db.query(StudentResponse).get(rid)
    if not resp:
        raise HTTPException(404, "Response not found")
    content = (body.content or "").strip()
    if not content:
        raise HTTPException(400, "content cannot be empty")

    turn = CompanionTurn(
        response_id=rid,
        role=body.role,
        content=content,
        turn_type=body.turn_type or "",
    )
    db.add(turn)

    if body.role == "student":
        # A new oral round extends the raw answer and invalidates stale assessment.
        student_rounds = sum(1 for t in (resp.companion_turns or []) if t.role == "student") + 1
        prev = (resp.raw_text or "").strip()
        resp.raw_text = (prev + "\n" + content).strip() if prev else content
        resp.cleaned_text = ""
        resp.ai_dimension_scores = None
        resp.ai_confidence = "uncertain"
        resp.ai_reasoning = {}
        resp.ai_extracted_features = {}
        resp.ai_note = ""
        resp.ai_suggested_tags = []
        resp.teacher_dimension_scores = None
        resp.teacher_confidence_override = None
        resp.teacher_tags = []
        resp.teacher_note = ""
        resp.teacher_reviewed = False
        resp.teacher_rating = ""
        resp.processing_status = "submitted"
        # 答满 3 轮自动视为对话结束（与学生端 MAX_ROUNDS 一致）
        if student_rounds >= 3:
            resp.dialogue_finished = resp.dialogue_finished or "auto"

    db.commit()
    db.refresh(resp)
    return resp

@router.post("/api/responses/{rid}/dialogue-finish", response_model=StudentResponseOut)
def finish_dialogue(rid: int, body: dict, db: Session = Depends(get_db)):
    """Persist that the dialogue was ended by student or teacher (survives refresh)."""
    resp = db.query(StudentResponse).get(rid)
    if not resp:
        raise HTTPException(404, "Response not found")
    by = (body or {}).get("by", "student")
    if by not in {"student", "teacher"}:
        by = "student"
    resp.dialogue_finished = by
    db.commit()
    db.refresh(resp)
    return resp

@router.post("/api/companion/{rid}/suggest-turn", response_model=SuggestTurnOut)
async def suggest_companion_turn(rid: int, db: Session = Depends(get_db)):
    """AI scaffolding-question suggestions + echo detection for one response."""
    resp = db.query(StudentResponse).get(rid)
    if not resp:
        raise HTTPException(404, "Response not found")
    if not (resp.raw_text or "").strip():
        raise HTTPException(400, "response has no text yet")

    topic = resp.topic
    turns = resp.companion_turns or []
    # 多轮音频/文本追加后，raw_text 是各轮累积结果；"最新回答"应只取最近
    # 一轮学生发言，完整对话历史仍通过 turns 传给模型。
    latest_student = next(
        (t for t in reversed(turns) if t.role == "student"), None
    )
    result = await state.companion.suggest_turn(
        response_text=(latest_student.content if latest_student else resp.raw_text) or "",
        turns=turns,
        topic_title=topic.title if topic else "",
        stimulus_material=topic.stimulus_material or "" if topic else "",
        student_grade=resp.student.grade if resp.student else 4,
    )
    return SuggestTurnOut(**result)

_FLASH_FEEDBACK_FALLBACK = "你把自己的想法说出来啦，真棒！"

@router.post("/api/companion/{rid}/feedback")
async def flash_feedback(rid: int, db: Session = Depends(get_db)):
    """Short, warm, score-free flash-point feedback for the student (LLM + fallback)."""
    resp = db.query(StudentResponse).get(rid)
    if not resp:
        raise HTTPException(404, "Response not found")
    text = (resp.raw_text or "").strip()
    if not text:
        return {"feedback": _FLASH_FEEDBACK_FALLBACK}

    dialogue_lines = []
    for t in (resp.companion_turns or []):
        who = {"student": "学生", "teacher": "老师", "ai_suggestion": "AI"}.get(t.role, t.role)
        dialogue_lines.append(f"{who}：{t.content}")
    dialogue_block = "\n".join(dialogue_lines[-8:])

    prompt = (
        "你是一位温暖、懂孩子的思辨课老师。请根据下面的学生口述和对话，"
        "用 1-2 句话直接对孩子说话（用“你”），肯定 TA 做得好的地方（发现闪光点）。\n"
        "硬性要求：\n"
        "- 只提对话中真实出现的表现，不要编造孩子没说过的话\n"
        "- 语气温暖、具体，避免空泛套话\n"
        "- 不出现分数、等级、排名，不出现“不足/待提升/较差”等负面词\n"
        "- 不超过 40 个字\n\n"
        f"学生口述：\n{text}\n\n"
        f"对话记录：\n{dialogue_block or '（无）'}"
    )
    try:
        feedback = await state.llm.chat(
            [
                {"role": "system", "content": "你是一位给小学生写鼓励反馈的老师，只说肯定的话。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.7,
            max_tokens=200,
            timeout=30,
        )
        feedback = (feedback or "").strip().strip('"')
        if not feedback:
            feedback = _FLASH_FEEDBACK_FALLBACK
    except Exception:
        feedback = _FLASH_FEEDBACK_FALLBACK
    return {"feedback": feedback}
