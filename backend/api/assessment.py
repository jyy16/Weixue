"""Batch/live assessment, review, reset and calibration endpoints."""

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session

from database import (
    AudioRecording, CalibrationRecord, CompanionTurn, Course, DebateTopic,
    DimensionTag, FeishuBinding, PrepPlan, SessionLocal, Student,
    StudentResponse, get_db,
)
from feishu.reviews import apply_teacher_review, sync_tags_to_library
from feishu.sync import BitableSyncer
from grading.rubric_loader import RubricLoader
from schemas import (
    QuickRatingUpdate, StatusUpdate, StudentResponseOut, TeacherReview,
)

from . import state


router = APIRouter(tags=["assessment"])

@router.post("/api/courses/{cid}/assess")
async def assess_course(cid: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """Kick off AI assessment for all student responses in a course."""
    course = db.query(Course).get(cid)
    if not course:
        raise HTTPException(404, "Course not found")

    topics = db.query(DebateTopic).filter(DebateTopic.course_id == cid).order_by(DebateTopic.order).all()
    students = db.query(Student).filter(Student.course_id == cid).all()

    if not topics or not students:
        raise HTTPException(400, "Need topics and students before assessment")

    # Count responses that need assessment (skip empty/unanswered)
    need_assessment = 0
    for student in students:
        for topic in topics:
            resp = db.query(StudentResponse).filter(
                StudentResponse.student_id == student.id,
                StudentResponse.topic_id == topic.id,
            ).first()
            if not resp:
                continue  # no response record = student didn't answer
            if not resp.raw_text or not resp.raw_text.strip():
                continue  # empty response = skip
            if resp.teacher_reviewed:
                continue
            if resp.ai_dimension_scores is not None and resp.ai_confidence != "uncertain":
                continue
            need_assessment += 1

    # Check-and-claim in ONE lock block: two separate blocks let concurrent
    # POSTs both pass the check and start duplicate assessment runs.
    with state._progress_lock:
        if state._assessment_progress.get(cid, {}).get("active"):
            raise HTTPException(409, "Assessment already in progress")
        state._assessment_progress[cid] = {
            "completed": 0, "total": need_assessment, "active": True,
            "errors": 0, "llm_calls": 0, "skipped": 0,
        }

    background_tasks.add_task(_run_assessment, cid, students, topics)
    return {"status": "started", "total": need_assessment, "need_assessment": need_assessment}

async def _run_assessment(cid: int, students, topics):
    """Background task: assess all student responses."""
    db = SessionLocal()
    loader = RubricLoader(db)

    try:
        for student in students:
            for topic in topics:
                resp = db.query(StudentResponse).filter(
                    StudentResponse.student_id == student.id,
                    StudentResponse.topic_id == topic.id,
                ).first()

                if not resp:
                    continue  # no response record = student didn't answer this topic, skip

                if resp.teacher_reviewed:
                    with state._progress_lock:
                        state._assessment_progress[cid]["completed"] += 1
                        state._assessment_progress[cid]["skipped"] += 1
                    continue
                if resp.ai_dimension_scores is not None and resp.ai_confidence != "uncertain":
                    with state._progress_lock:
                        state._assessment_progress[cid]["completed"] += 1
                        state._assessment_progress[cid]["skipped"] += 1
                    continue

                raw_text = resp.raw_text or ""
                if not raw_text.strip():
                    with state._progress_lock:
                        state._assessment_progress[cid]["completed"] += 1
                        state._assessment_progress[cid]["skipped"] += 1
                    continue  # empty response, skip without sending to AI

                try:
                    # Get 10 most recent calibration records (no tier filter)
                    cal_records = loader.get_calibration_records(
                        teacher_id="default",
                        limit=10,
                    )

                    result = await state.evaluator.assess(
                        rubric_loader=loader,
                        cognitive_tier=student.cognitive_tier,
                        topic_title=topic.title,
                        topic_type=topic.topic_type,
                        stimulus_material=topic.stimulus_material or "",
                        reference_arguments=topic.reference_arguments or [],
                        raw_text=raw_text,
                        student_grade=student.grade,
                        calibration_records=cal_records if cal_records else None,
                    )

                    resp.cleaned_text = result.get("cleaned_text", "")
                    resp.ai_dimension_scores = result.get("dimension_scores")
                    resp.ai_confidence = result.get("confidence", "uncertain")
                    resp.ai_reasoning = result.get("reasoning", {})
                    resp.ai_extracted_features = result.get("extracted_features", {})
                    resp.ai_bonus_flags = result.get("bonus_flags", [])
                    resp.ai_note = result.get("note", "")
                    resp.ai_suggested_tags = result.get("suggested_tags", [])

                    # Sync AI suggested tags to DimensionTag library
                    new_tags = result.get("suggested_tags", [])
                    if new_tags:
                        sync_tags_to_library(db, cid, new_tags, source="ai")

                    db.commit()

                    with state._progress_lock:
                        state._assessment_progress[cid]["completed"] += 1
                        state._assessment_progress[cid]["llm_calls"] += 1

                except Exception as e:
                    resp.cleaned_text = ""
                    resp.ai_dimension_scores = None
                    resp.ai_confidence = "uncertain"
                    resp.ai_reasoning = {}
                    resp.ai_extracted_features = {}
                    resp.ai_suggested_tags = []
                    resp.ai_note = f"AI评估异常：{e}"
                    db.commit()
                    with state._progress_lock:
                        state._assessment_progress[cid]["completed"] += 1
                        state._assessment_progress[cid]["errors"] += 1
    finally:
        with state._progress_lock:
            state._assessment_progress[cid]["active"] = False
        try:
            syncer = BitableSyncer(state.feishu_client)
            if syncer.available:
                await syncer.sync_course(db, cid)
        except Exception:
            # Bitable sync must never break the assessment background task.
            pass
        db.close()

@router.get("/api/courses/{cid}/assessment-progress")
def assessment_progress(cid: int):
    """Poll assessment progress. Frontend calls this every 500ms."""
    with state._progress_lock:
        p = state._assessment_progress.get(cid, {
            "completed": 0, "total": 0, "active": False,
            "errors": 0, "llm_calls": 0, "skipped": 0,
        })
    return p

@router.post("/api/courses/{cid}/reset")
def reset_course(cid: int, db: Session = Depends(get_db)):
    """Reset all assessment data for this course."""
    with state._progress_lock:
        if state._assessment_progress.get(cid, {}).get("active"):
            raise HTTPException(409, "评估进行中，请等待完成后再重置课程")

    responses = db.query(StudentResponse).join(Student).filter(
        Student.course_id == cid
    ).all()

    for resp in responses:
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
        resp.processing_status = "not_started"

    # Reset companion dialogue turns for the course.
    # NB: Query.delete() with join() raises InvalidRequestError in
    # SQLAlchemy 2.x — delete via a subquery of response ids instead.
    resp_ids = db.query(StudentResponse.id).join(Student).filter(
        Student.course_id == cid
    )
    db.query(CompanionTurn).filter(
        CompanionTurn.response_id.in_(resp_ids)
    ).delete(synchronize_session=False)

    # Reset tags: remove AI-new and teacher-created tags, reset base use_count
    db.query(DimensionTag).filter(
        DimensionTag.course_id == cid,
        DimensionTag.source.in_(["ai_new", "teacher"]),
    ).delete(synchronize_session=False)
    db.query(DimensionTag).filter(DimensionTag.course_id == cid).update(
        {"use_count": 0}, synchronize_session=False
    )

    with state._progress_lock:
        state._assessment_progress.pop(cid, None)

    db.commit()
    return {"ok": True, "responses_reset": len(responses)}

@router.post("/api/courses/{cid}/responses/clear")
def clear_course_responses(cid: int, db: Session = Depends(get_db)):
    """Delete every StudentResponse of the course (speech + dialogue +
    assessment + recordings), keeping students/topics/tags intact.

    Used by the classroom debug "清除发言" button so a new round of speaking
    can start from a clean slate.
    """
    if not db.get(Course, cid):
        raise HTTPException(404, "Course not found")
    with state._progress_lock:
        if state._assessment_progress.get(cid, {}).get("active"):
            raise HTTPException(409, "评估进行中，请等待完成后再清除")

    responses = (
        db.query(StudentResponse)
        .join(Student, StudentResponse.student_id == Student.id)
        .filter(Student.course_id == cid)
        .all()
    )
    if not responses:
        return {"ok": True, "responses_cleared": 0}

    resp_ids = [r.id for r in responses]
    db.query(CalibrationRecord).filter(
        CalibrationRecord.response_id.in_(resp_ids)
    ).delete(synchronize_session=False)
    db.query(CompanionTurn).filter(
        CompanionTurn.response_id.in_(resp_ids)
    ).delete(synchronize_session=False)

    rec_ids = [r.audio_recording_id for r in responses if r.audio_recording_id]
    if rec_ids:
        recordings = (
            db.query(AudioRecording).filter(AudioRecording.id.in_(rec_ids)).all()
        )
        for rec in recordings:
            state._remove_audio_file(rec.file_path)
            db.delete(rec)

    db.query(FeishuBinding).filter(
        FeishuBinding.entity_type == "response",
        FeishuBinding.entity_id.in_(resp_ids),
    ).delete(synchronize_session=False)
    # 备课辅助的讲评计划（顺序/备注/AI 总结）与作答强相关，一并清空。
    db.query(PrepPlan).filter(PrepPlan.course_id == cid).delete(
        synchronize_session=False
    )
    db.query(StudentResponse).filter(
        StudentResponse.id.in_(resp_ids)
    ).delete(synchronize_session=False)

    with state._progress_lock:
        state._assessment_progress.pop(cid, None)
    db.commit()
    return {"ok": True, "responses_cleared": len(resp_ids)}

@router.post("/api/responses/{rid}/review", response_model=StudentResponseOut)
def review_response(
    rid: int,
    body: TeacherReview,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Teacher reviews/overrides AI assessment on specific dimensions."""
    resp = db.query(StudentResponse).get(rid)
    if not resp:
        raise HTTPException(404, "Response not found")

    apply_teacher_review(
        db,
        resp,
        dimension_scores=body.dimension_scores,
        confidence_override=body.confidence_override,
        tags=body.tags,
        note=body.note,
        rating=body.rating or "",
    )

    db.commit()
    db.refresh(resp)
    background_tasks.add_task(_sync_response_after_review, rid)
    return resp

async def _sync_response_after_review(response_id: int):
    """Fire-and-forget Bitable sync after a teacher confirms a review."""
    db = SessionLocal()
    try:
        syncer = BitableSyncer(state.feishu_client)
        if syncer.available:
            await syncer.sync_response(db, response_id)
    except Exception:
        # Sync failures are reported via /api/feishu/bitable/status only.
        pass
    finally:
        db.close()


@router.post("/api/responses/{rid}/quick-rating", response_model=StudentResponseOut)
def save_quick_rating(
    rid: int,
    body: QuickRatingUpdate,
    db: Session = Depends(get_db),
):
    """Save the teacher's on-the-spot quick rating + note (before AI push).

    Unlike /review, this never marks the response teacher_reviewed and never
    creates calibration records; it only records the live-class judgment.
    """
    resp = db.query(StudentResponse).get(rid)
    if not resp:
        raise HTTPException(404, "Response not found")
    if body.rating not in {"", "good", "guide", "echo"}:
        raise HTTPException(400, "invalid rating (good|guide|echo)")
    resp.teacher_rating = body.rating
    resp.teacher_note = (body.note or "").strip()
    db.commit()
    db.refresh(resp)
    return resp


@router.patch("/api/responses/{rid}/status", response_model=StudentResponseOut)
def update_response_status(rid: int, body: StatusUpdate, db: Session = Depends(get_db)):
    """Advance the live-class status pipeline (adapter hook for student windows)."""
    resp = db.query(StudentResponse).get(rid)
    if not resp:
        raise HTTPException(404, "Response not found")
    allowed = {"not_started", "recording", "submitted", "processing", "processed"}
    if body.status not in allowed:
        raise HTTPException(400, f"invalid status: {body.status}")
    resp.processing_status = body.status
    db.commit()
    db.refresh(resp)
    return resp

@router.post("/api/responses/{rid}/assess", response_model=StudentResponseOut)
async def assess_one_response(rid: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """Live path: evaluate a single response (cleaning + evaluation in one call)."""
    resp = db.query(StudentResponse).get(rid)
    if not resp:
        raise HTTPException(404, "Response not found")
    raw_text = (resp.raw_text or "").strip()
    if not raw_text:
        raise HTTPException(400, "response has no text")

    resp.processing_status = "processing"
    db.commit()

    try:
        loader = RubricLoader(db)
        cal_records = loader.get_calibration_records(teacher_id="default", limit=10)
        student = resp.student
        topic = resp.topic
        result = await state.evaluator.assess_combined(
            rubric_loader=loader,
            cognitive_tier=student.cognitive_tier,
            topic_title=topic.title,
            topic_type=topic.topic_type,
            stimulus_material=topic.stimulus_material or "",
            reference_arguments=topic.reference_arguments or [],
            raw_text=raw_text,
            student_grade=student.grade,
            calibration_records=cal_records if cal_records else None,
            dialogue_turns=resp.companion_turns or None,
        )

        resp.cleaned_text = result.get("cleaned_text", "")
        resp.ai_dimension_scores = result.get("dimension_scores")
        resp.ai_confidence = result.get("confidence", "uncertain")
        resp.ai_reasoning = result.get("reasoning", {})
        resp.ai_extracted_features = result.get("extracted_features", {})
        resp.ai_bonus_flags = result.get("bonus_flags", [])
        resp.ai_note = result.get("note", "")
        resp.ai_suggested_tags = result.get("suggested_tags", [])
        new_tags = result.get("suggested_tags", [])
        if new_tags:
            sync_tags_to_library(db, topic.course_id, new_tags, source="ai")
        # assess_combined swallows LLM errors into a failure dict (no scores).
        # Keep the response retryable instead of masking it as "processed".
        resp.processing_status = "processed" if result.get("dimension_scores") else "submitted"
        db.commit()
    except Exception as e:
        resp.cleaned_text = ""
        resp.ai_dimension_scores = None
        resp.ai_confidence = "uncertain"
        resp.ai_reasoning = {}
        resp.ai_extracted_features = {}
        resp.ai_suggested_tags = []
        resp.ai_note = f"AI评估异常：{e}"
        resp.processing_status = "submitted"
        db.commit()

    db.refresh(resp)
    background_tasks.add_task(_sync_response_after_review, rid)
    return resp

@router.get("/api/courses/{cid}/calibrations")
def get_calibrations(cid: int, limit: int = 10, db: Session = Depends(get_db)):
    """Fetch recent teacher calibration records for display."""
    records = (
        db.query(CalibrationRecord)
        .join(StudentResponse)
        .join(Student)
        .filter(Student.course_id == cid)
        .order_by(CalibrationRecord.created_at.desc())
        .limit(limit)
        .all()
    )

    dim_labels = {
        "position": "立意（观点鲜明）", "material": "选材（言之有物）",
        "structure": "结构（条理清晰）", "language": "语言（用词准确）",
        "perspective": "视角（换位思考）",
        # Legacy keys (for older records)
        "clarity": "立意（观点鲜明）", "relevance": "结构（条理清晰）",
        "evidence_use": "选材（言之有物）", "inference": "结构（条理清晰）",
        "argument_evaluation": "结构（条理清晰）", "depth_breadth": "视角（换位思考）",
    }

    def format_scores(scores: dict) -> str:
        if not scores:
            return "无"
        parts = []
        for dim, rating in scores.items():
            label = dim_labels.get(dim, dim)
            parts.append(f"{label}{rating}")
        return "、".join(parts)

    result = []
    for rec in records:
        # Extract reasons from modifications
        reasons = []
        for m in (rec.modifications or []):
            if isinstance(m, dict):
                reason = m.get("reason", "")
                if reason:
                    reasons.append(reason)
        reason_str = "；".join(reasons) if reasons else rec.note

        result.append({
            "id": rec.id,
            "ai_scores": format_scores(rec.ai_original_scores or {}),
            "teacher_scores": format_scores(rec.teacher_final_scores or {}),
            "reason": reason_str or "",
            "created_at": rec.created_at.isoformat() if rec.created_at else "",
        })

    return {"total": len(records), "records": result}
