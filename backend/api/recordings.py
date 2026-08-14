"""Audio/text import and response deletion endpoints."""

import os
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session

from asr import ASRClient, ASRError
from database import (
    AudioRecording, CompanionTurn, DebateTopic, Student, StudentResponse, get_db,
)
from schemas import StudentResponseOut, TextImportRequest

from . import state


router = APIRouter(tags=["recordings"])

def _ensure_first_student_turn(db: Session, resp, content: str) -> None:
    """Record the first oral round as a CompanionTurn.

    The live classroom/assessment reads the dialogue via companion_turns;
    without this, the student's initial answer only lived in raw_text and
    disappeared from the dialogue timeline (and from the student window's
    3s poll). Subsequent rounds go through append_companion_turn.
    """
    if not content or not str(content).strip():
        return
    if resp.id is None:
        # 新建的作答还没落库（import_text 未 flush）：先持久化拿到
        # response_id，否则 CompanionTurn 外键会是 None 触发非空约束错误。
        db.flush()
    if any(t.role == "student" for t in (resp.companion_turns or [])):
        return
    db.add(
        CompanionTurn(
            response_id=resp.id,
            role="student",
            content=str(content).strip(),
            turn_type="",
        )
    )

@router.post("/api/courses/{cid}/audio/import", response_model=StudentResponseOut)
async def import_audio(
    cid: int,
    student_id: int = Form(...),
    topic_id: int = Form(...),
    file: UploadFile = File(...),
    source: str = Form("audio"),
    db: Session = Depends(get_db),
):
    """Upload classroom audio, transcribe via ASR (mock / dashscope / openai),
    store the transcript as raw_text, and reset stale assessment results."""
    student = db.query(Student).get(student_id)
    topic = db.query(DebateTopic).get(topic_id)
    if not student or student.course_id != cid or not topic or topic.course_id != cid:
        raise HTTPException(400, "student/topic not found in course")
    if source not in {"audio", "student_device", "teacher"}:
        raise HTTPException(400, f"invalid source: {source}")

    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in state.ALLOWED_AUDIO_EXT:
        raise HTTPException(
            400, f"unsupported audio type: {ext} (allowed: {sorted(state.ALLOWED_AUDIO_EXT)})"
        )

    safe_name = (
        f"{cid}_{student_id}_{topic_id}_"
        f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:6]}{ext}"
    )
    dest = os.path.join(state.UPLOAD_DIR, safe_name)
    with open(dest, "wb") as fh:
        fh.write(await file.read())

    # Transcribe before touching the database: a failed transcription must not
    # leave an empty StudentResponse, an AudioRecording row, or an orphan file.
    try:
        transcript = await ASRClient(provider=state.get_asr_provider(db)).transcribe(dest)
    except ASRError as exc:
        state._remove_audio_file(dest)
        raise HTTPException(502, f"转写失败：{exc}")
    except Exception as exc:
        state._remove_audio_file(dest)
        raise HTTPException(500, f"转写异常：{exc}")

    resp = (
        db.query(StudentResponse)
        .filter(
            StudentResponse.student_id == student_id,
            StudentResponse.topic_id == topic_id,
        )
        .first()
    )
    if resp is None:
        resp = StudentResponse(
            student_id=student_id, topic_id=topic_id, raw_text="", source=source
        )
        db.add(resp)

    # Re-uploading for the same student×topic replaces the old recording —
    # remove both its DB row and its physical file so nothing is orphaned.
    if resp.audio_recording_id:
        old = db.get(AudioRecording, resp.audio_recording_id)
        if old:
            state._remove_audio_file(old.file_path)
            db.delete(old)

    recording = AudioRecording(course_id=cid, topic_id=topic.id, file_path=dest)
    db.add(recording)
    db.flush()
    resp.audio_recording_id = recording.id

    # A new transcript invalidates the previous assessment
    resp.raw_text = transcript
    _ensure_first_student_turn(db, resp, transcript)
    resp.cleaned_text = ""
    resp.source = source
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
    db.commit()
    db.refresh(resp)
    return resp

@router.post("/api/courses/{cid}/responses/text", response_model=StudentResponseOut)
async def import_text(
    cid: int,
    body: TextImportRequest,
    db: Session = Depends(get_db),
):
    """Manual transcript paste (source='manual') — same reset semantics as audio import."""
    student = db.query(Student).get(body.student_id)
    topic = db.query(DebateTopic).get(body.topic_id)
    if not student or student.course_id != cid or not topic or topic.course_id != cid:
        raise HTTPException(400, "student/topic not found in course")
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(400, "text cannot be empty")

    resp = (
        db.query(StudentResponse)
        .filter(
            StudentResponse.student_id == body.student_id,
            StudentResponse.topic_id == body.topic_id,
        )
        .first()
    )
    if resp is None:
        resp = StudentResponse(
            student_id=body.student_id, topic_id=body.topic_id,
            raw_text=text, source=body.source or "manual",
        )
        db.add(resp)

    # New content invalidates the previous assessment
    resp.raw_text = text
    _ensure_first_student_turn(db, resp, text)
    resp.cleaned_text = ""
    resp.source = body.source or "manual"
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
    db.commit()
    db.refresh(resp)
    return resp

@router.delete("/api/responses/{rid}")
def delete_response(rid: int, db: Session = Depends(get_db)):
    """Delete a single student response — removes the student from that topic.
    Cascades calibration records and the linked audio recording row + file."""
    resp = db.query(StudentResponse).get(rid)
    if not resp:
        raise HTTPException(404, "Response not found")
    if resp.audio_recording_id:
        rec = db.get(AudioRecording, resp.audio_recording_id)
        if rec:
            state._remove_audio_file(rec.file_path)
            db.delete(rec)
    db.delete(resp)  # cascades calibrations via the relationship
    db.commit()
    return {"ok": True, "response_id": rid}
