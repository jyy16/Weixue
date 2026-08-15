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
from schemas import AudioImportOut, StudentResponseOut, TextImportRequest

from . import state


router = APIRouter(tags=["recordings"])

def _store_student_transcript(resp, content: str, *, append: bool) -> None:
    """Persist one transcript with explicit replace/append semantics.

    Management-page imports replace the whole answer and its dialogue. Live
    student recordings append a new student turn and extend ``raw_text`` so
    polling and the final assessment both retain every round.
    """
    text = str(content or "").strip()
    if not text:
        return

    if append:
        previous = (resp.raw_text or "").strip()
        resp.raw_text = f"{previous}\n{text}" if previous else text
    else:
        resp.raw_text = text
        resp.companion_turns.clear()

    resp.companion_turns.append(
        CompanionTurn(role="student", content=text, turn_type="")
    )

@router.post("/api/courses/{cid}/audio/import", response_model=AudioImportOut)
async def import_audio(
    cid: int,
    student_id: int = Form(...),
    topic_id: int = Form(...),
    file: UploadFile = File(...),
    source: str = Form("audio"),
    response_id: int | None = Form(None),
    db: Session = Depends(get_db),
):
    """Upload classroom audio, transcribe via ASR (mock / dashscope / openai),
    and reset stale assessment results.

    With ``response_id`` the transcript is appended as a new live-dialogue
    student turn. Without it, the upload keeps the management-page behavior
    of replacing the existing answer for the same student and topic.
    """
    student = db.query(Student).get(student_id)
    topic = db.query(DebateTopic).get(topic_id)
    if not student or student.course_id != cid or not topic or topic.course_id != cid:
        raise HTTPException(400, "student/topic not found in course")
    if source not in {"audio", "student_device", "teacher"}:
        raise HTTPException(400, f"invalid source: {source}")

    append_round = response_id is not None
    if append_round:
        resp = db.get(StudentResponse, response_id)
        if (
            not resp
            or resp.student_id != student_id
            or resp.topic_id != topic_id
        ):
            raise HTTPException(400, "response does not match student/topic")
    else:
        resp = (
            db.query(StudentResponse)
            .filter(
                StudentResponse.student_id == student_id,
                StudentResponse.topic_id == topic_id,
            )
            .first()
        )

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
    # 上传目录可能不存在（例如全新克隆的仓库），先建目录再写文件。
    os.makedirs(state.UPLOAD_DIR, exist_ok=True)
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

    # A new transcript invalidates the previous assessment. Live rounds append
    # to the aggregate answer; management re-uploads replace it.
    _store_student_transcript(resp, transcript, append=append_round)
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
    return AudioImportOut.model_validate(resp).model_copy(
        update={"transcript": transcript}
    )

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

    # Manual imports replace the current answer. Subsequent student-window text
    # rounds use /responses/{rid}/turns and therefore keep append semantics.
    _store_student_transcript(resp, text, append=False)
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
