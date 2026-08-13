"""FastAPI application — all routes for the critical thinking assessment system."""

import hashlib
import os
import re
from datetime import datetime
from typing import Optional
import uuid
from fastapi import FastAPI, Depends, HTTPException, BackgroundTasks, File, Form, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from sqlalchemy import func
import threading

from database import (
    get_db, init_db, SessionLocal,
    Course, DebateTopic, Student, StudentResponse,
    RubricTemplate, CalibrationRecord, DimensionTag, AudioRecording, CompanionTurn,
    SystemSetting, FeishuBinding, PrepPlan,
    get_cognitive_tier,
)
from schemas import (
    CourseCreate, CourseOut, DebateTopicCreate, DebateTopicOut,
    DebateTopicUpdate, StudentCreate, StudentUpdate, StudentBatchCreate,
    StudentOut, StudentResponseOut, TeacherReview, TextImportRequest,
    CommentRequest, CommentOut, CommentSaveRequest, CommentSendRequest, CommentSendOut,
    BatchCommentOut,
    TopicAnalytics, TagOut, TagUpdate, TagMerge,
    PrepPlanOut, PrepPlanUpdate, PrepPlanPushOut,
    PrepInsightsOut, PrepSummaryUpdate,
    RubricTemplateOut,
    CompanionTurnCreate, CompanionTurnOut, StatusUpdate, SuggestTurnOut,
    ASRProviderInfo, ASRSettingOut, ASRSettingUpdate,
    SystemModeOut, SystemModeAction,
)
from grading.evaluator import AssessmentEngine, COMMENT_TONE_GUIDE
from grading.llm import LLMClient
from grading.rubric_loader import RubricLoader
from companion import CompanionEngine
from feishu.routes import close_client as close_feishu_router_client
from feishu.routes import router as feishu_router
from asr import ASRClient, ASRError
from grading.ratings import rating_to_value, pass_line_for_grade, is_passing
from feishu import FeishuClient
from feishu.bot import BotService
from feishu.reviews import apply_teacher_review, sync_tags_to_library
from feishu.sync import BitableSyncer, bitable_status

app = FastAPI(title="思辨星 · 少儿思辨能力认知自适应评估系统", version="0.1.0")

# 企业加分项：命中“有自己 / 有新意”可提升一级评级（A → A+）
_BONUS_VALUES = {"有自己", "有新意"}


_QUICK_RATING_LABELS = {
    "good": "表达完整",
    "guide": "需引导",
    "echo": "复述/未表达",
}


def _upgrade_band(band: str, bonus_flags: list) -> str:
    """Upgrade a 综合评级 band by one step when enterprise bonus flags hit."""
    if band in {"良好", "待提升", "薄弱"} and any(
        b in _BONUS_VALUES for b in (bonus_flags or [])
    ):
        return {"良好": "优秀", "待提升": "良好", "薄弱": "待提升"}[band]
    return band


def _band_for_avg(avg: float, pass_line: float) -> str:
    """综合评级档位（与前端 bandFromAverage 口径一致）。"""
    if avg >= 3.5:
        return "优秀"
    if avg >= pass_line:
        return "良好"
    if avg >= 1.5:
        return "待提升"
    if avg > 0:
        return "薄弱"
    return ""

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

llm = LLMClient()
evaluator = AssessmentEngine(llm)
companion = CompanionEngine(llm)
feishu_client = FeishuClient.from_env()

app.include_router(feishu_router)

UPLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

ALLOWED_AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".webm", ".mp4", ".amr", ".wma", ".flac"}

ASR_PROVIDER_LABELS = {
    "mock": "演示转写（mock）",
    "qwen_asr": "百炼 qwen3-asr-flash（推荐）",
    "openai": "OpenAI 兼容（whisper）",
    "dashscope": "DashScope 百炼（paraformer）",
}


def get_asr_provider(db: Session) -> str:
    """Current ASR provider: DB setting first, then ASR_PROVIDER env, then mock."""
    row = db.get(SystemSetting, "asr_provider")
    if row and row.value.strip():
        return row.value.strip().lower()
    return (os.getenv("ASR_PROVIDER") or "mock").lower().strip()


def _asr_provider_info(provider: str, api_key_configured: bool) -> ASRProviderInfo:
    reason = ""
    if provider == "mock":
        ready = True
    elif provider == "qwen_asr":
        ready = bool(api_key_configured)
        if not ready:
            reason = "未配置 ASR_API_KEY / LLM_API_KEY"
    elif provider == "openai":
        ready = bool(api_key_configured)
        if not ready:
            reason = "未配置 ASR_API_KEY / LLM_API_KEY"
    elif provider == "dashscope":
        try:
            import importlib.util
            has_sdk = importlib.util.find_spec("dashscope") is not None
        except Exception:
            has_sdk = False
        ready = bool(api_key_configured) and has_sdk
        if not api_key_configured:
            reason = "未配置 ASR_API_KEY / LLM_API_KEY"
        elif not has_sdk:
            reason = "未安装 dashscope SDK（pip install dashscope）"
    else:
        ready = False
        reason = f"未知 provider: {provider}"
    return ASRProviderInfo(
        id=provider,
        label=ASR_PROVIDER_LABELS.get(provider, provider),
        ready=ready,
        reason=reason,
    )


def build_asr_settings(db: Session) -> ASRSettingOut:
    current = get_asr_provider(db)
    api_key_configured = bool(os.getenv("ASR_API_KEY") or os.getenv("LLM_API_KEY", ""))
    try:
        client = ASRClient(provider=current)
    except ASRError:
        # A bad env/DB value must not take down the settings endpoint.
        current = "mock"
        client = ASRClient(provider=current)
    demo_data_present = False
    marker = db.get(SystemSetting, "demo_course_id")
    if marker and marker.value.strip():
        try:
            demo_data_present = db.get(Course, int(marker.value.strip())) is not None
        except ValueError:
            demo_data_present = False
    return ASRSettingOut(
        provider=current,
        model=client.model,
        api_key_configured=api_key_configured,
        providers=[
            _asr_provider_info(p, api_key_configured)
            for p in ASRClient.SUPPORTED_PROVIDERS
        ],
        demo=False,
        demo_data_present=demo_data_present,
    )


def purge_demo_data(db: Session) -> dict:
    """Delete the seed/demo course (marked by seed.py) and all its content.

    Only the course recorded in system_settings['demo_course_id'] is touched,
    so real teacher data in other courses is never affected. Physical audio
    files of the demo recordings are removed too.
    """
    marker = db.get(SystemSetting, "demo_course_id")
    if not marker or not marker.value.strip():
        return {"purged": False}
    try:
        course_id = int(marker.value.strip())
    except ValueError:
        return {"purged": False}
    course = db.get(Course, course_id)
    if course is None:
        db.delete(marker)
        db.commit()
        return {"purged": False}

    student_ids = [
        s.id for s in db.query(Student).filter(Student.course_id == course_id).all()
    ]
    topic_ids = [
        t.id for t in db.query(DebateTopic).filter(DebateTopic.course_id == course_id).all()
    ]
    resp_ids: set[int] = set()
    if student_ids:
        resp_ids.update(
            r.id for r in db.query(StudentResponse)
            .filter(StudentResponse.student_id.in_(student_ids)).all()
        )
    if topic_ids:
        resp_ids.update(
            r.id for r in db.query(StudentResponse)
            .filter(StudentResponse.topic_id.in_(topic_ids)).all()
        )

    summary = {
        "purged": True,
        "course_id": course_id,
        "responses": len(resp_ids),
        "topics": len(topic_ids),
        "students": len(student_ids),
        "recordings": 0,
        "calibrations": 0,
        "turns": 0,
        "tags": 0,
    }

    if resp_ids:
        resp_list = list(resp_ids)
        summary["calibrations"] = (
            db.query(CalibrationRecord)
            .filter(CalibrationRecord.response_id.in_(resp_list))
            .delete(synchronize_session=False)
        )
        summary["turns"] = (
            db.query(CompanionTurn)
            .filter(CompanionTurn.response_id.in_(resp_list))
            .delete(synchronize_session=False)
        )
        db.query(StudentResponse).filter(StudentResponse.id.in_(resp_list)).delete(
            synchronize_session=False
        )

    recordings = (
        db.query(AudioRecording).filter(AudioRecording.course_id == course_id).all()
    )
    file_paths = [rec.file_path for rec in recordings]
    summary["recordings"] = len(recordings)
    for rec in recordings:
        db.delete(rec)

    if student_ids:
        db.query(Student).filter(Student.id.in_(student_ids)).delete(
            synchronize_session=False
        )
    if topic_ids:
        db.query(DebateTopic).filter(DebateTopic.id.in_(topic_ids)).delete(
            synchronize_session=False
        )
    summary["tags"] = (
        db.query(DimensionTag).filter(DimensionTag.course_id == course_id).delete(
            synchronize_session=False
        )
    )

    entity_pairs = [("course", course_id)]
    entity_pairs += [("topic", tid) for tid in topic_ids]
    entity_pairs += [("student", sid) for sid in student_ids]
    entity_pairs += [("response", rid) for rid in resp_ids]
    for entity_type, entity_id in entity_pairs:
        db.query(FeishuBinding).filter(
            FeishuBinding.entity_type == entity_type,
            FeishuBinding.entity_id == entity_id,
        ).delete(synchronize_session=False)

    db.delete(course)
    db.delete(marker)
    db.commit()

    for path in file_paths:
        _remove_audio_file(path)
    return summary


def seed_demo_if_empty(db: Session) -> bool:
    """Re-seed the demo course when the database has no courses at all."""
    if db.query(Course).count() > 0:
        return False
    import seed as seed_module
    seed_module.seed(force=False)
    return True

# Thread-safe assessment progress tracker
_assessment_progress = {}
_progress_lock = threading.Lock()


@app.on_event("startup")
def on_startup():
    init_db()


@app.on_event("shutdown")
async def on_shutdown():
    await feishu_client.close()
    await close_feishu_router_client()


@app.get("/api/health")
async def health_check():
    feishu = await feishu_client.health_check()
    return {
        "status": "ok" if feishu["status"] == "auth_ok" else "degraded",
        "database": "ready",
        "feishu": feishu,
        "bitable": bitable_status(feishu_client.config),
    }


@app.get("/api/settings/asr", response_model=ASRSettingOut)
def get_asr_settings(db: Session = Depends(get_db)):
    """Current ASR mode (mock vs real provider) and per-provider readiness."""
    return build_asr_settings(db)


@app.post("/api/settings/asr", response_model=ASRSettingOut)
def set_asr_settings(body: ASRSettingUpdate, db: Session = Depends(get_db)):
    """Persist the ASR provider selection (mock | openai | dashscope).

    Real providers must not share the database with demo/seed data: switching
    to openai/dashscope purges the marked demo course, and switching back to
    mock re-seeds it when the database is otherwise empty.
    """
    provider = body.provider.strip().lower()
    if provider not in ASRClient.SUPPORTED_PROVIDERS:
        raise HTTPException(
            400, f"invalid ASR provider: {provider} (allowed: {ASRClient.SUPPORTED_PROVIDERS})"
        )
    if provider != "mock":
        purge_demo_data(db)
    elif provider == "mock":
        seed_demo_if_empty(db)
    row = db.get(SystemSetting, "asr_provider")
    if row is None:
        row = SystemSetting(key="asr_provider", value=provider)
        db.add(row)
    else:
        row.value = provider
    db.commit()
    return build_asr_settings(db)


# ════════════════════════════════════════════════════════════
# System Mode (演示模式 / 真实模式 · 能力矩阵 + 演示数据动作)
# ════════════════════════════════════════════════════════════

@app.get("/api/settings/mode", response_model=SystemModeOut)
def get_system_mode(db: Session = Depends(get_db)):
    """Capability matrix for the frontend mode switch (no secrets)."""
    asr_settings = build_asr_settings(db)
    current = asr_settings.provider
    asr_ready = next(
        (p.ready for p in asr_settings.providers if p.id == current),
        False,
    )
    config = feishu_client.config
    return SystemModeOut(
        demo_course_present=asr_settings.demo_data_present,
        asr_provider=current,
        asr_ready=asr_ready,
        llm_configured=bool(os.getenv("LLM_API_KEY", "").strip()),
        feishu_ready=bool(config.is_configured and config.teacher_open_id),
        bitable_ready=bitable_status(config).get("mode") == "ready",
    )


@app.post("/api/settings/mode", response_model=dict)
def set_system_mode(body: SystemModeAction, db: Session = Depends(get_db)):
    """One-click backend actions for the mode switch.

    enter_demo: seed the demo course (only when the DB has no courses, so real
                teacher data is never overwritten; the frontend demo mode has
                embedded data anyway).
    enter_real: purge the marked demo course (never touches real courses).
    """
    action = body.action.strip().lower()
    if action == "enter_demo":
        if db.query(Course).count() > 0:
            return {
                "ok": True,
                "action": action,
                "seeded": False,
                "message": "数据库已有课程，演示数据未重新生成（演示模式前端已内置数据）。",
            }
        seed_demo_if_empty(db)
        return {
            "ok": True,
            "action": action,
            "seeded": True,
            "message": "演示课程已生成，可切换前端为演示模式开始演示。",
        }
    if action == "enter_real":
        result = purge_demo_data(db)
        return {
            "ok": True,
            "action": action,
            **result,
            "message": "演示课程已清除" if result.get("purged") else "无演示课程可清除。",
        }
    raise HTTPException(400, "invalid action (enter_demo|enter_real)")


# ════════════════════════════════════════════════════════════
# Courses
# ════════════════════════════════════════════════════════════

@app.get("/api/courses", response_model=list[CourseOut])
def list_courses(db: Session = Depends(get_db)):
    courses = db.query(Course).all()
    result = []
    for c in courses:
        tc = db.query(DebateTopic).filter(DebateTopic.course_id == c.id).count()
        sc = db.query(Student).filter(Student.course_id == c.id).count()
        result.append(CourseOut(
            id=c.id, title=c.title, class_name=c.class_name,
            grade_level=c.grade_level, created_at=c.created_at,
            topic_count=tc, student_count=sc,
        ))
    return result


@app.get("/api/courses/{cid}", response_model=CourseOut)
def get_course(cid: int, db: Session = Depends(get_db)):
    c = db.query(Course).get(cid)
    if not c:
        raise HTTPException(404, "Course not found")
    tc = db.query(DebateTopic).filter(DebateTopic.course_id == cid).count()
    sc = db.query(Student).filter(Student.course_id == cid).count()
    return CourseOut(
        id=c.id, title=c.title, class_name=c.class_name,
        grade_level=c.grade_level, created_at=c.created_at,
        topic_count=tc, student_count=sc,
    )


@app.post("/api/courses", response_model=CourseOut)
def create_course(body: CourseCreate, db: Session = Depends(get_db)):
    """Create a brand-new course — starts empty (no topics/students)."""
    c = Course(**body.model_dump())
    db.add(c)
    db.commit()
    db.refresh(c)
    return CourseOut(
        id=c.id, title=c.title, class_name=c.class_name,
        grade_level=c.grade_level, created_at=c.created_at,
        topic_count=0, student_count=0,
    )


# ════════════════════════════════════════════════════════════
# Debate Topics
# ════════════════════════════════════════════════════════════

@app.get("/api/courses/{cid}/topics", response_model=list[DebateTopicOut])
def list_topics(cid: int, db: Session = Depends(get_db)):
    topics = db.query(DebateTopic).filter(DebateTopic.course_id == cid).order_by(DebateTopic.order).all()
    return topics


@app.post("/api/courses/{cid}/topics", response_model=DebateTopicOut)
def create_topic(cid: int, body: DebateTopicCreate, db: Session = Depends(get_db)):
    if not db.query(Course).get(cid):
        raise HTTPException(404, "Course not found")
    max_order = db.query(func.max(DebateTopic.order)).filter(DebateTopic.course_id == cid).scalar() or 0
    t = DebateTopic(course_id=cid, order=max_order + 1, **body.model_dump())
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


@app.put("/api/topics/{tid}", response_model=DebateTopicOut)
def update_topic(tid: int, body: DebateTopicUpdate, db: Session = Depends(get_db)):
    t = db.query(DebateTopic).get(tid)
    if not t:
        raise HTTPException(404, "Topic not found")
    for field, value in body.model_dump(exclude_unset=True).items():
        if value is not None:
            setattr(t, field, value)
    db.commit()
    db.refresh(t)
    return t


@app.delete("/api/topics/{tid}")
def delete_topic(tid: int, db: Session = Depends(get_db)):
    t = db.query(DebateTopic).get(tid)
    if not t:
        raise HTTPException(404, "Topic not found")
    db.delete(t)
    db.commit()
    return {"ok": True, "topic_id": tid}


# ════════════════════════════════════════════════════════════
# Students
# ════════════════════════════════════════════════════════════

def _student_out(student: Student) -> StudentOut:
    return StudentOut(
        id=student.id,
        name=student.name,
        grade=student.grade,
        course_id=student.course_id,
        cognitive_tier=student.cognitive_tier,
        comment_draft=student.comment_draft or "",
        feishu_open_id=student.feishu_open_id or "",
        comment_delivery_status=student.comment_delivery_status or "not_sent",
        comment_delivery_error=student.comment_delivery_error or "",
        comment_delivered_at=student.comment_delivered_at,
    )


def _reset_comment_delivery(student: Student) -> None:
    """A changed draft must be explicitly sent and tracked as a new delivery."""
    student.comment_delivery_status = "not_sent"
    student.comment_delivery_hash = ""
    student.comment_delivery_error = ""
    student.comment_delivered_at = None

@app.get("/api/courses/{cid}/students", response_model=list[StudentOut])
def list_students(cid: int, db: Session = Depends(get_db)):
    students = db.query(Student).filter(Student.course_id == cid).all()
    return [_student_out(student) for student in students]


@app.post("/api/courses/{cid}/students", response_model=StudentOut)
def create_student(cid: int, body: StudentCreate, db: Session = Depends(get_db)):
    if not db.query(Course).get(cid):
        raise HTTPException(404, "Course not found")
    feishu_open_id = body.feishu_open_id.strip()
    if feishu_open_id and not feishu_open_id.startswith("ou_"):
        raise HTTPException(400, "飞书 open_id 格式不正确，应以 ou_ 开头")
    s = Student(
        course_id=cid,
        name=body.name,
        grade=body.grade,
        feishu_open_id=feishu_open_id,
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    return _student_out(s)


@app.post("/api/courses/{cid}/students/batch", response_model=dict)
def create_students_batch(cid: int, body: StudentBatchCreate, db: Session = Depends(get_db)):
    """Batch-create students from the homework-entry panel ("姓名,年级" per line).
    Students with the same name in this course are skipped."""
    if not db.query(Course).get(cid):
        raise HTTPException(404, "Course not found")
    created, skipped = [], []
    for item in body.students:
        name = (item.name or "").strip()
        if not name:
            continue
        existing = (
            db.query(Student)
            .filter(Student.course_id == cid, Student.name == name)
            .first()
        )
        if existing:
            skipped.append(existing.name)
            continue
        feishu_open_id = item.feishu_open_id.strip()
        if feishu_open_id and not feishu_open_id.startswith("ou_"):
            raise HTTPException(
                400, f"{name} 的飞书 open_id 格式不正确，应以 ou_ 开头"
            )
        st = Student(
            course_id=cid,
            name=name,
            grade=item.grade,
            feishu_open_id=feishu_open_id,
        )
        db.add(st)
        db.flush()
        created.append(_student_out(st))
    db.commit()
    return {"created": created, "skipped": skipped}


@app.put("/api/students/{sid}", response_model=StudentOut)
def update_student(sid: int, body: StudentUpdate, db: Session = Depends(get_db)):
    s = db.query(Student).get(sid)
    if not s:
        raise HTTPException(404, "Student not found")
    if body.name is not None and body.name.strip():
        s.name = body.name.strip()
    if body.grade is not None:
        s.grade = body.grade
    if body.feishu_open_id is not None:
        feishu_open_id = body.feishu_open_id.strip()
        if feishu_open_id and not feishu_open_id.startswith("ou_"):
            raise HTTPException(400, "飞书 open_id 格式不正确，应以 ou_ 开头")
        if feishu_open_id != (s.feishu_open_id or ""):
            s.feishu_open_id = feishu_open_id
            _reset_comment_delivery(s)
    db.commit()
    db.refresh(s)
    return _student_out(s)


@app.delete("/api/students/{sid}")
def delete_student(sid: int, db: Session = Depends(get_db)):
    s = db.query(Student).get(sid)
    if not s:
        raise HTTPException(404, "Student not found")
    db.delete(s)
    db.commit()
    return {"ok": True, "student_id": sid}


# ════════════════════════════════════════════════════════════
# Student Responses & Assessment
# ════════════════════════════════════════════════════════════

@app.get("/api/courses/{cid}/responses", response_model=list[StudentResponseOut])
def list_responses(cid: int, student_id: Optional[int] = None, db: Session = Depends(get_db)):
    q = db.query(StudentResponse).join(Student).filter(Student.course_id == cid)
    if student_id:
        q = q.filter(StudentResponse.student_id == student_id)
    return q.all()


@app.get("/api/responses/{rid}", response_model=StudentResponseOut)
def get_response(rid: int, db: Session = Depends(get_db)):
    resp = db.query(StudentResponse).get(rid)
    if not resp:
        raise HTTPException(404, "Response not found")
    return resp


@app.post("/api/courses/{cid}/assess")
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
    with _progress_lock:
        if _assessment_progress.get(cid, {}).get("active"):
            raise HTTPException(409, "Assessment already in progress")
        _assessment_progress[cid] = {
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
                    with _progress_lock:
                        _assessment_progress[cid]["completed"] += 1
                        _assessment_progress[cid]["skipped"] += 1
                    continue
                if resp.ai_dimension_scores is not None and resp.ai_confidence != "uncertain":
                    with _progress_lock:
                        _assessment_progress[cid]["completed"] += 1
                        _assessment_progress[cid]["skipped"] += 1
                    continue

                raw_text = resp.raw_text or ""
                if not raw_text.strip():
                    with _progress_lock:
                        _assessment_progress[cid]["completed"] += 1
                        _assessment_progress[cid]["skipped"] += 1
                    continue  # empty response, skip without sending to AI

                try:
                    # Get 10 most recent calibration records (no tier filter)
                    cal_records = loader.get_calibration_records(
                        teacher_id="default",
                        limit=10,
                    )

                    result = await evaluator.assess(
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

                    with _progress_lock:
                        _assessment_progress[cid]["completed"] += 1
                        _assessment_progress[cid]["llm_calls"] += 1

                except Exception as e:
                    resp.cleaned_text = ""
                    resp.ai_dimension_scores = None
                    resp.ai_confidence = "uncertain"
                    resp.ai_reasoning = {}
                    resp.ai_extracted_features = {}
                    resp.ai_suggested_tags = []
                    resp.ai_note = f"AI评估异常：{e}"
                    db.commit()
                    with _progress_lock:
                        _assessment_progress[cid]["completed"] += 1
                        _assessment_progress[cid]["errors"] += 1
    finally:
        with _progress_lock:
            _assessment_progress[cid]["active"] = False
        try:
            syncer = BitableSyncer(feishu_client)
            if syncer.available:
                await syncer.sync_course(db, cid)
        except Exception:
            # Bitable sync must never break the assessment background task.
            pass
        db.close()


@app.get("/api/courses/{cid}/assessment-progress")
def assessment_progress(cid: int):
    """Poll assessment progress. Frontend calls this every 500ms."""
    with _progress_lock:
        p = _assessment_progress.get(cid, {
            "completed": 0, "total": 0, "active": False,
            "errors": 0, "llm_calls": 0, "skipped": 0,
        })
    return p


@app.post("/api/courses/{cid}/reset")
def reset_course(cid: int, db: Session = Depends(get_db)):
    """Reset all assessment data for this course."""
    with _progress_lock:
        if _assessment_progress.get(cid, {}).get("active"):
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

    with _progress_lock:
        _assessment_progress.pop(cid, None)

    db.commit()
    return {"ok": True, "responses_reset": len(responses)}


@app.post("/api/courses/{cid}/responses/clear")
def clear_course_responses(cid: int, db: Session = Depends(get_db)):
    """Delete every StudentResponse of the course (speech + dialogue +
    assessment + recordings), keeping students/topics/tags intact.

    Used by the classroom debug "清除发言" button so a new round of speaking
    can start from a clean slate.
    """
    if not db.get(Course, cid):
        raise HTTPException(404, "Course not found")
    with _progress_lock:
        if _assessment_progress.get(cid, {}).get("active"):
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
            _remove_audio_file(rec.file_path)
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

    with _progress_lock:
        _assessment_progress.pop(cid, None)
    db.commit()
    return {"ok": True, "responses_cleared": len(resp_ids)}


@app.post("/api/responses/{rid}/review", response_model=StudentResponseOut)
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
        syncer = BitableSyncer(feishu_client)
        if syncer.available:
            await syncer.sync_response(db, response_id)
    except Exception:
        # Sync failures are reported via /api/feishu/bitable/status only.
        pass
    finally:
        db.close()


# ════════════════════════════════════════════════════════════
# AI Companion (live-classroom dialogue + status pipeline)
# ════════════════════════════════════════════════════════════

@app.get("/api/companion/{rid}", response_model=list[CompanionTurnOut])
def get_companion_turns(rid: int, db: Session = Depends(get_db)):
    """Return the full dialogue history of a response (teacher/student both read it)."""
    resp = db.query(StudentResponse).get(rid)
    if not resp:
        raise HTTPException(404, "Response not found")
    return resp.companion_turns


@app.post("/api/responses/{rid}/turns", response_model=StudentResponseOut)
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


@app.post("/api/responses/{rid}/dialogue-finish", response_model=StudentResponseOut)
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


@app.post("/api/companion/{rid}/suggest-turn", response_model=SuggestTurnOut)
async def suggest_companion_turn(rid: int, db: Session = Depends(get_db)):
    """AI scaffolding-question suggestions + echo detection for one response."""
    resp = db.query(StudentResponse).get(rid)
    if not resp:
        raise HTTPException(404, "Response not found")
    if not (resp.raw_text or "").strip():
        raise HTTPException(400, "response has no text yet")

    topic = resp.topic
    result = await companion.suggest_turn(
        response_text=resp.raw_text or "",
        turns=resp.companion_turns,
        topic_title=topic.title if topic else "",
        stimulus_material=topic.stimulus_material or "" if topic else "",
        student_grade=resp.student.grade if resp.student else 4,
    )
    return SuggestTurnOut(**result)


_FLASH_FEEDBACK_FALLBACK = "你把自己的想法说出来啦，真棒！"


@app.post("/api/companion/{rid}/feedback")
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
        feedback = await llm.chat(
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


@app.patch("/api/responses/{rid}/status", response_model=StudentResponseOut)
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


@app.post("/api/responses/{rid}/assess", response_model=StudentResponseOut)
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
        result = await evaluator.assess_combined(
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


# ════════════════════════════════════════════════════════════
# Audio import (ASR pipeline)
# ════════════════════════════════════════════════════════════


def _remove_audio_file(file_path: str) -> None:
    """Best-effort removal of an uploaded audio file (never raises)."""
    try:
        if file_path and os.path.isfile(file_path):
            os.remove(file_path)
    except OSError:
        pass


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


@app.post("/api/courses/{cid}/audio/import", response_model=StudentResponseOut)
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
    if ext not in ALLOWED_AUDIO_EXT:
        raise HTTPException(
            400, f"unsupported audio type: {ext} (allowed: {sorted(ALLOWED_AUDIO_EXT)})"
        )

    safe_name = (
        f"{cid}_{student_id}_{topic_id}_"
        f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:6]}{ext}"
    )
    dest = os.path.join(UPLOAD_DIR, safe_name)
    with open(dest, "wb") as fh:
        fh.write(await file.read())

    # Transcribe before touching the database: a failed transcription must not
    # leave an empty StudentResponse, an AudioRecording row, or an orphan file.
    try:
        transcript = await ASRClient(provider=get_asr_provider(db)).transcribe(dest)
    except ASRError as exc:
        _remove_audio_file(dest)
        raise HTTPException(502, f"转写失败：{exc}")
    except Exception as exc:
        _remove_audio_file(dest)
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
            _remove_audio_file(old.file_path)
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


@app.post("/api/courses/{cid}/responses/text", response_model=StudentResponseOut)
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


@app.delete("/api/responses/{rid}")
def delete_response(rid: int, db: Session = Depends(get_db)):
    """Delete a single student response — removes the student from that topic.
    Cascades calibration records and the linked audio recording row + file."""
    resp = db.query(StudentResponse).get(rid)
    if not resp:
        raise HTTPException(404, "Response not found")
    if resp.audio_recording_id:
        rec = db.get(AudioRecording, resp.audio_recording_id)
        if rec:
            _remove_audio_file(rec.file_path)
            db.delete(rec)
    db.delete(resp)  # cascades calibrations via the relationship
    db.commit()
    return {"ok": True, "response_id": rid}


# ════════════════════════════════════════════════════════════
# Comments
# ════════════════════════════════════════════════════════════

@app.post("/api/courses/{cid}/comments", response_model=CommentOut)
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
    except Exception as e:
        # Fallback to template if LLM fails
        draft = _fallback_comment(student, topic_data, dim_labels)

    # Generating again is an explicit request for a new delivery item. Reset
    # the old result even if the model happens to produce identical text;
    # otherwise the UI would incorrectly keep showing the previous delivery as
    # covering this newly generated draft.
    student.comment_draft = draft
    _reset_comment_delivery(student)
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


@app.post("/api/courses/{cid}/comments/save")
def save_comment_draft(cid: int, body: CommentSaveRequest, db: Session = Depends(get_db)):
    """Save a comment draft for a student."""
    student = db.query(Student).get(body.student_id)
    if not student or student.course_id != cid:
        raise HTTPException(404, "Student not found")
    if body.draft != (student.comment_draft or ""):
        student.comment_draft = body.draft
        _reset_comment_delivery(student)
    db.commit()
    return {"ok": True, "student_id": body.student_id}


@app.post("/api/courses/{cid}/comments/send", response_model=CommentSendOut)
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
        _reset_comment_delivery(student)
    db.commit()

    status = "saved_pending_delivery"
    message = "评语已保存并标记待发送。"

    config = feishu_client.config
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
            await BotService(feishu_client).send_card(config.teacher_open_id, card)
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


@app.post("/api/courses/{cid}/comments/batch", response_model=BatchCommentOut)
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
            # Batch regeneration has the same semantics as regenerating one
            # student: every successful result starts a new delivery cycle,
            # even if the model happens to return identical text.
            student.comment_draft = draft
            _reset_comment_delivery(student)
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


# ════════════════════════════════════════════════════════════
# Teacher Calibration Records (for display)
# ════════════════════════════════════════════════════════════

@app.get("/api/courses/{cid}/calibrations")
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


# ════════════════════════════════════════════════════════════
# Lesson Prep Analytics
# ════════════════════════════════════════════════════════════

def _prep_topic_rows(cid: int, db: Session) -> list[dict]:
    """Per-topic prep aggregates shared by /prep, plan cards and Bitable sync."""
    topics = db.query(DebateTopic).filter(DebateTopic.course_id == cid).order_by(DebateTopic.order).all()
    students = db.query(Student).filter(Student.course_id == cid).all()

    result = []
    for topic in topics:
        dim_entries: dict[str, list[tuple[float, int]]] = {}
        weak_students = []
        tag_counts = {}

        for st in students:
            resp = db.query(StudentResponse).filter(
                StudentResponse.student_id == st.id,
                StudentResponse.topic_id == topic.id,
            ).first()
            if not resp:
                continue

            scores = resp.teacher_dimension_scores or resp.ai_dimension_scores
            conf = resp.teacher_confidence_override or resp.ai_confidence
            if conf == "uncertain" and not resp.teacher_dimension_scores:
                continue

            if scores:
                student_values = []
                for dim, rating in scores.items():
                    val = rating_to_value(rating)
                    if val is None:
                        continue
                    dim_entries.setdefault(dim, []).append((val, st.grade))
                    student_values.append(val)
                student_avg = sum(student_values) / len(student_values) if student_values else 0

                # 合格线按学生自己的年级：低年级 ≥2.5，高年级 ≥3.0。
                if student_values and not is_passing(st.grade, student_avg):
                    weak_students.append(f"{st.name}({student_avg:.1f})")

                tags = resp.teacher_tags or resp.ai_suggested_tags or []
                for tag in tags:
                    tag_counts[tag] = tag_counts.get(tag, 0) + 1

        avg_dim_scores = {}
        weak_dimensions = []
        for dim, entries in dim_entries.items():
            avg = sum(v for v, _ in entries) / len(entries)
            avg_dim_scores[dim] = round(avg, 2)
            # 维度“薄弱” = 该维度低于各自年级合格线的学生占比 ≥ 40%。
            below = sum(1 for v, g in entries if not is_passing(g, v))
            if below / len(entries) >= 0.4:
                weak_dimensions.append(dim)

        result.append({
            "topic_id": topic.id,
            "title": topic.title,
            "topic_type": topic.topic_type,
            "cognitive_tier": topic.cognitive_tier,
            "avg_dimension_scores": avg_dim_scores,
            "weak_dimensions": weak_dimensions,
            "low_students": weak_students,
            "error_tags": [
                {"tag": t, "count": c}
                for t, c in sorted(tag_counts.items(), key=lambda x: -x[1])
            ],
        })

    result.sort(
        key=lambda x: min(x["avg_dimension_scores"].values())
        if x["avg_dimension_scores"] else 5
    )
    return result


@app.get("/api/courses/{cid}/prep", response_model=list[TopicAnalytics])
def prep_analytics(cid: int, db: Session = Depends(get_db)):
    """Aggregate assessment results per topic for lesson prep."""
    return _prep_topic_rows(cid, db)


# ════════════════════════════════════════════════════════════
# Lesson Prep Plan (备课辅助 · 讲评计划)
# ════════════════════════════════════════════════════════════

@app.get("/api/courses/{cid}/prep/plan", response_model=PrepPlanOut)
def get_prep_plan(cid: int, db: Session = Depends(get_db)):
    """Return the saved lesson-prep plan (an empty draft when none exists)."""
    if not db.get(Course, cid):
        raise HTTPException(404, "Course not found")
    plan = db.query(PrepPlan).filter(PrepPlan.course_id == cid).first()
    return plan if plan else PrepPlanOut(course_id=cid)


@app.put("/api/courses/{cid}/prep/plan", response_model=PrepPlanOut)
async def save_prep_plan(
    cid: int,
    body: PrepPlanUpdate,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Create/update the lesson-prep plan.

    Topic order, notes and the confirmed flag are teacher decisions only — AI
    never overwrites them. Saving also refreshes the Bitable plan row when the
    sync is configured (fire-and-forget, never blocks the save).
    """
    course = db.get(Course, cid)
    if not course:
        raise HTTPException(404, "Course not found")
    topic_ids = {t.id for t in course.topics}
    invalid = [tid for tid in body.lesson_plan if tid not in topic_ids]
    if invalid:
        raise HTTPException(400, f"辩题不属于当前班级: {invalid}")

    plan = db.query(PrepPlan).filter(PrepPlan.course_id == cid).first()
    if plan is None:
        plan = PrepPlan(course_id=cid)
        db.add(plan)
    plan.lesson_plan = list(body.lesson_plan)
    plan.notes = {str(k): v for k, v in (body.notes or {}).items()}
    plan.confirmed = bool(body.confirmed)
    plan.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(plan)

    background_tasks.add_task(_sync_prep_plan_after_save, cid)
    return plan


async def _sync_prep_plan_after_save(course_id: int) -> None:
    """Fire-and-forget Bitable sync after the teacher saves/confirms a plan."""
    db = SessionLocal()
    try:
        syncer = BitableSyncer(feishu_client)
        if syncer.available:
            await syncer.sync_prep_plan(db, course_id)
    except Exception:
        # Sync failures are visible via /api/feishu/bitable/status only.
        pass
    finally:
        db.close()


_PREP_DIM_LABELS = {
    "position": "立意", "material": "选材", "structure": "结构",
    "language": "语言", "perspective": "视角",
}
_PREP_TIER_LABELS = {
    "basic": "基础层（1-2年级）",
    "developing": "发展层（3-5年级）",
    "advancing": "进阶层（6-7年级）",
}

_SUMMARY_LABEL_PREFIXES = (
    "总体情况", "本题总体情况", "总体总结", "本题总结", "总体",
    "普遍/突出问题", "普遍与突出问题", "普遍、突出问题", "普遍突出问题", "问题",
    "讲评建议", "建议",
)
_NUM_PREFIX_RE = re.compile(r"^\s*\d+[\.、．)）]\s*")
_BULLET_PREFIX_RE = re.compile(r"^\s*[-•*]\s+")


def _strip_summary_label(line: str) -> str:
    """Strip leading labels like '总体情况：' / '1. ' / '- ' from one line."""
    line = _BULLET_PREFIX_RE.sub("", line)
    line = _NUM_PREFIX_RE.sub("", line)
    line = line.strip()
    for prefix in _SUMMARY_LABEL_PREFIXES:
        if line.startswith(prefix):
            rest = line[len(prefix):]
            if rest and rest[0] in "：:，,。.":
                return rest[1:].lstrip()
            if rest.startswith(" "):
                return rest.lstrip()
    return line


def _normalize_summary_field(text, bullets: bool) -> str:
    """Normalize an LLM summary field for display/export/card.

    - Removes '总体情况：' / '1.' / '- ' prefixes (LLM output is unstable).
    - bullets=True ⇒ one '- ' item per line (Feishu/export render properly).
    - bullets=False ⇒ a single flowing paragraph (no stray newlines).
    """
    if not text:
        return ""
    cleaned = []
    for raw in str(text).splitlines():
        line = _strip_summary_label(raw)
        if line:
            cleaned.append(line)
    if bullets:
        return "\n".join(f"- {ln}" for ln in cleaned)
    return "".join(cleaned)


def _build_prep_plan_card_content(course, plan, rows: dict, insights: dict | None = None) -> str:
    """Markdown body for the Feishu lesson-plan card."""
    lines = [f"**班级**：{course.class_name}（{course.grade_level}年级）"]
    lines.append(f"**状态**：{'已确认' if plan.confirmed else '草稿'}")

    # 确定性总体统计（与页面同源，供教师核对 LLM 文本中的数字）
    if insights:
        p = insights["participation"]
        stats = [
            f"参评 {p['students_answered']}/{p['students_total']} 人",
            f"作答 {p['responses_total']} 份",
        ]
        if p.get("class_avg"):
            stats.append(f"班级均分 {p['class_avg']}")
        lines.append(f"**总体统计**：{' · '.join(stats)}")
        tier = insights["tier_summary"]
        if tier:
            lines.append(
                "分梯段：" + "；".join(
                    f"{_PREP_TIER_LABELS.get(k, k)} {v['students']}人/均分{v['avg_score']}"
                    for k, v in tier.items()
                )
            )
        if insights["top_tags"]:
            lines.append(
                "全课高频标签：" + "、".join(
                    f"{t['tag']}({t['count']})" for t in insights["top_tags"][:5]
                )
            )
        lines.append("")

    summary = plan.summary or {}
    if summary.get("overview"):
        lines.append("")
        lines.append("**总体情况**")
        lines.append(str(summary["overview"]))
    if summary.get("problems"):
        lines.append("")
        lines.append("**普遍/突出问题**")
        lines.append(str(summary["problems"]))

    lines.append("")
    lines.append("**讲评顺序**")
    if not plan.lesson_plan:
        lines.append("（暂无辩题）")
    for idx, tid in enumerate(plan.lesson_plan or [], start=1):
        row = rows.get(tid) or {}
        title = row.get("title") or f"辩题#{tid}"
        lines.append(f"{idx}. **{title}**")
        dims = row.get("avg_dimension_scores") or {}
        if dims:
            lines.append(
                "   - 维度均分：" + "；".join(
                    f"{_PREP_DIM_LABELS.get(d, d)}：{v}" for d, v in dims.items()
                )
            )
        weak = row.get("weak_dimensions") or []
        if weak:
            lines.append(
                "   - 薄弱：" + "、".join(
                    _PREP_DIM_LABELS.get(d, d) for d in weak
                )
            )
        low = row.get("low_students") or []
        if low:
            lines.append(f"   - 低分学生：{'、'.join(low)}")
        if not dims and not weak and not low:
            lines.append("   - 暂无评估数据，可先完成本题作答与评估")
        lines.append("")

    if insights and insights["highlights"]:
        lines.append("**优质发言**")
        for h in insights["highlights"][:3]:
            bonus = f"（{'、'.join(h['bonus_flags'])}）" if h["bonus_flags"] else ""
            lines.append(f"- {h['student_name']}《{h['topic_title']}》均分{h['avg']}{bonus}")
        lines.append("")

    notes = [(int(k), v) for k, v in (plan.notes or {}).items() if v]
    if notes:
        lines.append("**教师备注**")
        for tid, note in sorted(notes):
            title = (rows.get(tid) or {}).get("title") or f"辩题#{tid}"
            lines.append(f"- {title}：{note}")
        lines.append("")

    topic_summaries = (summary or {}).get("topics") or {}
    for tid in (plan.lesson_plan or []):
        ts = topic_summaries.get(str(tid)) or {}
        if not ts.get("overview"):
            continue
        title = (rows.get(tid) or {}).get("title") or f"辩题#{tid}"
        lines.append(f"**本题总结 · {title}**")
        lines.append(str(ts["overview"]))
    return "\n".join(lines)


@app.post("/api/courses/{cid}/prep/plan/push", response_model=PrepPlanPushOut)
async def push_prep_plan(cid: int, db: Session = Depends(get_db)):
    """Push the saved lesson-prep plan as a Feishu interactive card to the
    teacher — the same bot channel as the comment-confirmation cards."""
    course = db.get(Course, cid)
    if not course:
        raise HTTPException(404, "Course not found")
    plan = db.query(PrepPlan).filter(PrepPlan.course_id == cid).first()
    if plan is None or not plan.lesson_plan:
        raise HTTPException(400, "请先保存讲评计划再推送")

    config = feishu_client.config
    if not config.is_configured or not config.teacher_open_id:
        return PrepPlanPushOut(
            ok=True,
            status="pending_delivery",
            message="讲评计划已保存；飞书机器人发送通道待联调（未配置 FEISHU_TEACHER_OPEN_ID）。",
        )

    rows = {r["topic_id"]: r for r in _prep_topic_rows(cid, db)}
    insights = _prep_insights(cid, db)
    content = _build_prep_plan_card_content(course, plan, rows, insights)
    card = BotService.build_prep_plan_card(
        title=f"思辨星 · {course.class_name} 讲评计划",
        content=content,
        course_id=cid,
        change_url=f"{config.web_base_url}/?tab=prep",
    )
    try:
        await BotService(feishu_client).send_card(config.teacher_open_id, card)
    except Exception as exc:  # noqa: BLE001 - report honestly, keep the plan saved
        return PrepPlanPushOut(
            ok=False,
            status="error",
            message=f"飞书机器人推送失败（{exc}），请稍后重试。",
        )
    return PrepPlanPushOut(
        ok=True,
        status="delivered",
        message="讲评计划已推送到飞书机器人卡片，可在飞书中确认或点击跳转网页调整。",
    )


# ════════════════════════════════════════════════════════════
# Prep Insights & AI Summary (备课辅助 · 优质发言 / 普遍问题 / 总体情况)
# ════════════════════════════════════════════════════════════

def _prep_insights(cid: int, db: Session) -> dict:
    """Deterministic, always-available prep insights (no LLM call).

    - participation: who answered, how many, per-topic counts
    - tier_summary:  per cognitive tier students/avg/weak count
    - highlights:    top responses (avg ≥ grade pass line + 0.5), ≤2/topic, ≤6
    - problem_patterns: dimension × students below their own grade pass line
    - top_tags:      most-used teacher/AI tags across the class
    """
    topics = (
        db.query(DebateTopic)
        .filter(DebateTopic.course_id == cid)
        .order_by(DebateTopic.order)
        .all()
    )
    students = db.query(Student).filter(Student.course_id == cid).all()
    responses = (
        db.query(StudentResponse)
        .join(Student, StudentResponse.student_id == Student.id)
        .filter(Student.course_id == cid)
        .all()
    )
    resp_by_pair = {(r.student_id, r.topic_id): r for r in responses}
    resp_by_topic: dict[int, list] = {}
    for r in responses:
        resp_by_topic.setdefault(r.topic_id, []).append(r)

    # Participation
    participation = {
        "students_total": len(students),
        "students_answered": len({r.student_id for r in responses}),
        "responses_total": len(responses),
        "per_topic": [],
    }
    for t in topics:
        topic_resps = resp_by_topic.get(t.id, [])
        passing = 0
        topic_quick = {"good": 0, "guide": 0, "echo": 0}
        for r in topic_resps:
            if r.teacher_rating in topic_quick:
                topic_quick[r.teacher_rating] += 1
            scores = r.teacher_dimension_scores or r.ai_dimension_scores
            conf = r.teacher_confidence_override or r.ai_confidence
            if conf == "uncertain" and not r.teacher_dimension_scores:
                continue
            vals = [
                v for v in (rating_to_value(x) for x in (scores or {}).values())
                if v is not None
            ]
            avg = sum(vals) / len(vals) if vals else 0
            if vals and is_passing(r.student.grade, avg):
                passing += 1
        participation["per_topic"].append({
            "topic_id": t.id,
            "title": t.title,
            "responses": len(topic_resps),
            "reviewed": sum(1 for r in topic_resps if r.teacher_reviewed),
            "passing": passing,
            "quick_ratings": topic_quick,
        })

    # Tier summary
    tier_raw: dict[str, dict] = {}
    student_avgs: list[float] = []
    student_grades: list[int] = []
    for st in students:
        entry = tier_raw.setdefault(
            st.cognitive_tier, {"students": 0, "scores": [], "weak_students": 0}
        )
        entry["students"] += 1
        vals = []
        for r in responses:
            if r.student_id != st.id:
                continue
            scores = r.teacher_dimension_scores or r.ai_dimension_scores
            conf = r.teacher_confidence_override or r.ai_confidence
            if conf == "uncertain" and not r.teacher_dimension_scores:
                continue
            if scores:
                vals.extend(
                    v for v in (rating_to_value(x) for x in scores.values()) if v is not None
                )
        avg = sum(vals) / len(vals) if vals else 0
        if vals and not is_passing(st.grade, avg):
            entry["weak_students"] += 1
        if vals:
            entry["scores"].append(avg)
            student_avgs.append(avg)
            student_grades.append(st.grade)
    tier_summary = {
        tier: {
            "students": v["students"],
            "avg_score": round(sum(v["scores"]) / len(v["scores"]), 2)
            if v["scores"] else 0,
            "weak_students": v["weak_students"],
        }
        for tier, v in tier_raw.items()
    }
    participation["class_avg"] = (
        round(sum(student_avgs) / len(student_avgs), 2) if student_avgs else 0
    )
    participation["pass_count"] = sum(
        1 for avg, g in zip(student_avgs, student_grades) if is_passing(g, avg)
    )
    participation["pass_rate"] = (
        round(
            participation["pass_count"] / len(student_avgs), 2
        )
        if student_avgs else 0
    )

    # Highlights: strong answers worth praising in class
    candidates = []
    for r in responses:
        scores = r.teacher_dimension_scores or r.ai_dimension_scores
        conf = r.teacher_confidence_override or r.ai_confidence
        if conf == "uncertain" and not r.teacher_dimension_scores:
            continue
        if not scores:
            continue
        vals = [rating_to_value(x) for x in scores.values()]
        vals = [v for v in vals if v is not None]
        avg = sum(vals) / len(vals) if vals else 0
        # 优质发言 = 高于该生年级合格线至少半档（1-3年级 ≥3.0，4-6年级 ≥3.5）。
        if not vals or avg < pass_line_for_grade(r.student.grade) + 0.5:
            continue
        topic = next((t for t in topics if t.id == r.topic_id), None)
        if topic is None:
            continue
        candidates.append({
            "topic_id": topic.id,
            "topic_title": topic.title,
            "student_id": r.student_id,
            "student_name": r.student.name,
            "grade": r.student.grade,
            "text": (r.cleaned_text or r.raw_text or "")[:160],
            "scores": scores,
            "avg": round(avg, 2),
            "bonus_flags": r.ai_bonus_flags or [],
            "tags": (r.teacher_tags or r.ai_suggested_tags or [])[:4],
        })
    candidates.sort(key=lambda x: -x["avg"])
    per_topic_count: dict[int, int] = {}
    highlights = []
    for h in candidates:
        if per_topic_count.get(h["topic_id"], 0) >= 2:
            continue
        per_topic_count[h["topic_id"]] = per_topic_count.get(h["topic_id"], 0) + 1
        highlights.append(h)
        if len(highlights) >= 6:
            break

    # Per-topic highlights (for the 分题分析 section): ≤2 per topic, ≤12 total.
    topic_highlights: list[dict] = []
    per_topic_count = {}
    for h in candidates:
        if per_topic_count.get(h["topic_id"], 0) >= 2:
            continue
        per_topic_count[h["topic_id"]] = per_topic_count.get(h["topic_id"], 0) + 1
        topic_highlights.append(h)
        if len(topic_highlights) >= 12:
            break

    # Problem patterns: which dimensions are weak, and how many students/topics
    dim_students: dict[str, set] = {}
    dim_topics: dict[str, set] = {}
    for topic in topics:
        for st in students:
            r = resp_by_pair.get((st.id, topic.id))
            if not r:
                continue
            scores = r.teacher_dimension_scores or r.ai_dimension_scores
            conf = r.teacher_confidence_override or r.ai_confidence
            if conf == "uncertain" and not r.teacher_dimension_scores:
                continue
            if not scores:
                continue
            for dim, rating in scores.items():
                val = rating_to_value(rating)
                if val is None or is_passing(st.grade, val):
                    continue
                dim_students.setdefault(dim, set()).add(st.id)
                dim_topics.setdefault(dim, set()).add(topic.id)
    problem_patterns = [
        {
            "dimension": dim,
            "label": _PREP_DIM_LABELS.get(dim, dim),
            "students_affected": len(dim_students[dim]),
            "topics_affected": len(dim_topics[dim]),
        }
        for dim in dim_students
    ]
    problem_patterns.sort(key=lambda x: -x["students_affected"])

    # Top tags across the class
    tag_counts: dict[str, int] = {}
    for r in responses:
        for tag in (r.teacher_tags or r.ai_suggested_tags or []):
            tag_counts[tag] = tag_counts.get(tag, 0) + 1
    top_tags = [
        {"tag": t, "count": c}
        for t, c in sorted(tag_counts.items(), key=lambda x: -x[1])[:8]
    ]

    # 课堂即时评级（教师第一印象，绿/黄/红三档）——与五维度评分互补。
    quick_rating_counts = {"good": 0, "guide": 0, "echo": 0}
    for r in responses:
        if r.teacher_rating in quick_rating_counts:
            quick_rating_counts[r.teacher_rating] += 1

    return {
        "course_id": cid,
        "participation": participation,
        "tier_summary": tier_summary,
        "highlights": highlights,
        "topic_highlights": topic_highlights,
        "problem_patterns": problem_patterns,
        "top_tags": top_tags,
        "quick_rating_counts": quick_rating_counts,
    }


@app.get("/api/courses/{cid}/prep/insights", response_model=PrepInsightsOut)
def prep_insights(cid: int, db: Session = Depends(get_db)):
    """Deterministic prep insights: participation / highlights / problems."""
    if not db.get(Course, cid):
        raise HTTPException(404, "Course not found")
    return _prep_insights(cid, db)


def _build_summary_digest(course, insights: dict) -> str:
    """Compact class digest fed to the LLM (or used by the template fallback)."""
    p = insights["participation"]
    lines = [f"班级：{course.class_name}（{course.grade_level}年级）"]
    lines.append(
        "合格线：1-3年级 ≥2.5（B+），4-6年级及以上 ≥3.0（A-），"
        "按学生各自年级判断达标。"
    )
    lines.append(
        f"参评：{p['students_answered']}/{p['students_total']}人，"
        f"共{p['responses_total']}份作答。"
    )
    quick = insights.get("quick_rating_counts") or {}
    if quick and any(quick.values()):
        lines.append(
            "课堂即时评级（教师第一印象）：" + "、".join(
                f"{_QUICK_RATING_LABELS[key]}{quick.get(key, 0)}人"
                for key in ("good", "guide", "echo")
                if quick.get(key)
            )
        )
    tier = insights["tier_summary"]
    if tier:
        parts = []
        for key, label in (
            ("basic", "基础层"),
            ("developing", "发展层"),
            ("advancing", "进阶层"),
        ):
            t = tier.get(key)
            if t:
                parts.append(f"{label}{t['students']}人均分{t['avg_score']}")
        lines.append("分梯段：" + "；".join(parts))
    weak = insights["problem_patterns"][:3]
    if weak:
        lines.append(
            "主要薄弱维度：" + "；".join(
                f"{w['label']}（{w['students_affected']}人/{w['topics_affected']}题）"
                for w in weak
            )
        )
    if insights["highlights"]:
        lines.append(
            "优秀示例：" + "；".join(
                f"{h['student_name']}《{h['topic_title']}》均分{h['avg']}"
                for h in insights["highlights"][:3]
            )
        )
    if insights["top_tags"]:
        lines.append(
            "高频标签：" + "、".join(
                f"{t['tag']}({t['count']})" for t in insights["top_tags"][:5]
            )
        )
    return "\n".join(lines)


def _template_prep_summary(insights: dict) -> dict:
    """Deterministic fallback when the LLM is unavailable (no API key / error)."""
    p = insights["participation"]
    tier = insights["tier_summary"]
    tier_text = "；".join(
        f"{_PREP_TIER_LABELS.get(k, k)} {v['students']}人、均分{v['avg_score']}"
        for k, v in tier.items()
    )
    overview = (
        f"全班 {p['students_answered']} 名学生共提交 {p['responses_total']} 份作答，"
        f"整体参与度良好。"
        + (f"各认知梯段表现：{tier_text}。" if tier_text else "")
        + "建议在讲评课上先肯定整体亮点，再针对薄弱维度做引导。"
    )
    quick = insights.get("quick_rating_counts") or {}
    if quick and any(quick.values()):
        overview = (
            f"全班 {p['students_answered']} 名学生共提交 {p['responses_total']} 份作答，"
            "整体参与度良好。"
            "课堂即时评级（教师第一印象）："
            + "、".join(
                f"{_QUICK_RATING_LABELS[key]}{quick.get(key, 0)}人"
                for key in ("good", "guide", "echo")
                if quick.get(key)
            )
            + "。"
            + (f"各认知梯段表现：{tier_text}。" if tier_text else "")
            + "建议在讲评课上先肯定整体亮点，再针对薄弱维度做引导。"
        )
    weak = insights["problem_patterns"]
    if weak:
        problems = "\n".join(
            f"- {w['label']}维度偏弱，影响 {w['students_affected']} 名学生、"
            f"{w['topics_affected']} 道辩题。"
            for w in weak[:4]
        )
    else:
        problems = "- 当前未发现低于合格线的维度，可进入拓展与深度追问。"
    suggestions = (
        "- 讲评时先展示 1-2 份优质发言，说明好在哪里（结构或证据）；\n"
        "- 针对最弱的 1-2 个维度设计当堂小练习，例如补充证据或换位思考；\n"
        "- 关注低分学生名单，安排小组互评或个别追问。"
    )
    return {
        "overview": overview,
        "problems": problems,
        "suggestions": suggestions,
        "generated_by": "template",
    }


async def _generate_prep_summary_llm(digest: str) -> dict | None:
    """LLM narrative summary; returns None on any failure (caller falls back)."""
    prompt = (
        "你是一位经验丰富的思辨课教师，正在为下一次讲评课备课。\n"
        "以下是评估系统导出的班级数据摘要：\n\n"
        f"{digest}\n\n"
        "请基于数据输出三段内容，严格返回 JSON（不要 Markdown 代码块）：\n"
        '{"overview": "总体情况：3-5句话，先肯定整体亮点，再客观说明现状，不点名不评价个人", '
        '"problems": "普遍/突出问题：2-4条，每条一句话，指出维度与涉及人数，不点名学生", '
        '"suggestions": "讲评建议：2-3条具体可执行的课堂安排"}\n\n'
        "【硬性要求】\n"
        "- 只能陈述数据摘要中出现的数字和事实，不得推测趋势、原因或动机；\n"
        "- overview/problems/suggestions 正文不要重复'总体情况''问题''建议'等标题字样；\n"
        "- problems 与 suggestions 每一条独立成行，不要编号。"
    )
    try:
        llm = LLMClient()
        result = await llm.chat_json(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.4,
            max_tokens=1200,
        )
        if not isinstance(result, dict):
            return None
        text = {
            k: str(result.get(k) or "").strip()
            for k in ("overview", "problems", "suggestions")
        }
        if not any(text.values()):
            return None
        return {**text, "generated_by": "llm"}
    except Exception:  # noqa: BLE001 - any LLM failure falls back to template
        return None


@app.post("/api/courses/{cid}/prep/summary")
async def generate_prep_summary(cid: int, db: Session = Depends(get_db)):
    """Generate (and persist) the AI narrative summary for the prep plan.

    Deterministic insights feed the prompt; the result is stored on PrepPlan
    so the Feishu card / export / Bitable can reuse it. LLM failures fall back
    to a data-driven template — the page never blocks on the LLM.
    """
    course = db.get(Course, cid)
    if not course:
        raise HTTPException(404, "Course not found")
    insights = _prep_insights(cid, db)
    digest = _build_summary_digest(course, insights)
    summary = await _generate_prep_summary_llm(digest)
    if summary is None:
        summary = _template_prep_summary(insights)
    summary = {
        **summary,
        "overview": _normalize_summary_field(summary.get("overview"), False),
        "problems": _normalize_summary_field(summary.get("problems"), True),
        "suggestions": _normalize_summary_field(summary.get("suggestions"), True),
    }

    plan = db.query(PrepPlan).filter(PrepPlan.course_id == cid).first()
    if plan is None:
        plan = PrepPlan(course_id=cid)
        db.add(plan)
    plan.summary = {**summary, "generated_at": datetime.utcnow().isoformat()}
    db.commit()
    return plan.summary


def _build_topic_summary_digest(row: dict, highlights: list[dict]) -> str:
    """Per-topic digest fed to the LLM (or the template fallback)."""
    lines = [f"辩题：{row['title']}"]
    dims = "、".join(
        f"{_PREP_DIM_LABELS.get(d, d)}{v}"
        for d, v in (row.get("avg_dimension_scores") or {}).items()
    )
    if dims:
        lines.append(f"各维度均分：{dims}")
    if row.get("weak_dimensions"):
        lines.append(
            "薄弱维度：" + "、".join(
                _PREP_DIM_LABELS.get(d, d) for d in row["weak_dimensions"]
            )
        )
    if row.get("low_students"):
        lines.append("低分学生：" + "、".join(row["low_students"][:5]))
    if row.get("error_tags"):
        lines.append(
            "本题高频标签：" + "、".join(
                f"{t['tag']}({t['count']})" for t in row["error_tags"][:4]
            )
        )
    if highlights:
        lines.append(
            "优秀示例：" + "；".join(
                f"{h['student_name']}均分{h['avg']}" for h in highlights[:2]
            )
        )
    return "\n".join(lines)


def _template_topic_summary(row: dict, highlights: list[dict]) -> dict:
    """Deterministic per-topic fallback when the LLM is unavailable."""
    dims = "、".join(
        f"{_PREP_DIM_LABELS.get(d, d)}{v}"
        for d, v in (row.get("avg_dimension_scores") or {}).items()
    ) or "暂无评分"
    weak = row.get("weak_dimensions") or []
    problems = (
        "\n".join(f"- {_PREP_DIM_LABELS.get(d, d)}维度偏弱。" for d in weak)
        if weak else "- 未发现低于合格线的维度，可进入拓展追问。"
    )
    hl = highlights[:2]
    if hl:
        suggestions = (
            f"- 讲评时可展示 {'、'.join(h['student_name'] for h in hl)} 的优质发言。\n"
            "- 针对薄弱维度设计当堂小练习。"
        )
    else:
        suggestions = (
            "- 先带学生重新梳理本题的立场与理由，再逐步补充证据；\n"
            "- 可用反例启发学生换位思考。"
        )
    return {
        "overview": f"本题各维度均分：{dims}。",
        "problems": problems,
        "suggestions": suggestions,
        "generated_by": "template",
    }


async def _generate_topic_summary_llm(digest: str) -> dict | None:
    prompt = (
        "你是一位经验丰富的思辨课教师，正在为下一次讲评课备课。\n"
        "以下是评估系统导出的某一道辩题的班级数据：\n\n"
        f"{digest}\n\n"
        "请基于数据输出三段内容，严格返回 JSON（不要 Markdown 代码块）：\n"
        '{"overview": "本题总体情况：2-4句话，先肯定亮点，再客观说明现状，不点名不评价个人", '
        '"problems": "本题普遍/突出问题：1-3条，每条一句话，指出维度或具体短板，不点名学生", '
        '"suggestions": "本题讲评建议：2-3条具体可执行的课堂安排"}\n\n'
        "【硬性要求】\n"
        "- 只能陈述数据摘要中出现的数字和事实，不得推测趋势、原因或动机；\n"
        "- overview/problems/suggestions 正文不要重复'总体情况''问题''建议'等标题字样；\n"
        "- problems 与 suggestions 每一条独立成行，不要编号。"
    )
    try:
        llm = LLMClient()
        result = await llm.chat_json(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.4,
            max_tokens=1000,
        )
        if not isinstance(result, dict):
            return None
        text = {
            k: str(result.get(k) or "").strip()
            for k in ("overview", "problems", "suggestions")
        }
        if not any(text.values()):
            return None
        return {**text, "generated_by": "llm"}
    except Exception:  # noqa: BLE001 - any LLM failure falls back to template
        return None


@app.post("/api/courses/{cid}/prep/topics/{tid}/summary")
async def generate_topic_summary(cid: int, tid: int, db: Session = Depends(get_db)):
    """Generate (and persist) the per-topic AI summary for one debate topic."""
    if not db.get(Course, cid):
        raise HTTPException(404, "Course not found")
    row = next(
        (r for r in _prep_topic_rows(cid, db) if r["topic_id"] == tid),
        None,
    )
    if row is None:
        raise HTTPException(404, "Topic not found in course")
    insights = _prep_insights(cid, db)
    hl = [h for h in insights["topic_highlights"] if h["topic_id"] == tid][:2]
    digest = _build_topic_summary_digest(row, hl)
    summary = await _generate_topic_summary_llm(digest)
    if summary is None:
        summary = _template_topic_summary(row, hl)
    summary = {
        **summary,
        "overview": _normalize_summary_field(summary.get("overview"), False),
        "problems": _normalize_summary_field(summary.get("problems"), True),
        "suggestions": _normalize_summary_field(summary.get("suggestions"), True),
    }

    plan = db.query(PrepPlan).filter(PrepPlan.course_id == cid).first()
    if plan is None:
        plan = PrepPlan(course_id=cid)
        db.add(plan)
    base = dict(plan.summary or {})
    topics_map = dict(base.get("topics") or {})
    topics_map[str(tid)] = {
        **summary,
        "generated_at": datetime.utcnow().isoformat(),
    }
    base["topics"] = topics_map
    plan.summary = base
    db.commit()
    return topics_map[str(tid)]


@app.put("/api/courses/{cid}/prep/summary")
def save_prep_summary(
    cid: int,
    body: PrepSummaryUpdate,
    db: Session = Depends(get_db),
):
    """Persist teacher edits to an AI summary (class-level or per-topic).

    The AI draft stays the suggestion; the edited text is what exports and the
    Feishu card use. `edited=True` marks a human-touched summary.
    """
    if not db.get(Course, cid):
        raise HTTPException(404, "Course not found")
    plan = db.query(PrepPlan).filter(PrepPlan.course_id == cid).first()
    if plan is None:
        plan = PrepPlan(course_id=cid)
        db.add(plan)

    fields = ("overview", "problems", "suggestions")
    if body.topic_id:
        tid = body.topic_id
        row = next(
            (r for r in _prep_topic_rows(cid, db) if r["topic_id"] == tid),
            None,
        )
        if row is None:
            raise HTTPException(404, "Topic not found in course")
        base = dict(plan.summary or {})
        topics_map = dict(base.get("topics") or {})
        cur = dict(topics_map.get(str(tid)) or {})
        for k in fields:
            v = getattr(body, k, None)
            if v is not None:
                cur[k] = v
        cur["edited"] = True
        topics_map[str(tid)] = cur
        base["topics"] = topics_map
        plan.summary = base
    else:
        base = dict(plan.summary or {})
        for k in fields:
            v = getattr(body, k, None)
            if v is not None:
                base[k] = v
        base["edited"] = True
        plan.summary = base
    db.commit()
    return plan.summary


# ════════════════════════════════════════════════════════════
# Tags
# ════════════════════════════════════════════════════════════

@app.get("/api/courses/{cid}/tags", response_model=list[TagOut])
def list_tags(cid: int, db: Session = Depends(get_db)):
    tags = db.query(DimensionTag).filter(DimensionTag.course_id == cid).order_by(DimensionTag.use_count.desc()).all()
    return tags


@app.post("/api/courses/{cid}/tags", response_model=TagOut)
def create_tag(cid: int, name: str, source: str = "base", db: Session = Depends(get_db)):
    existing = db.query(DimensionTag).filter(DimensionTag.course_id == cid, DimensionTag.name == name).first()
    if existing:
        return existing
    t = DimensionTag(course_id=cid, name=name, source=source)
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


@app.put("/api/tags/{tid}", response_model=TagOut)
def update_tag(tid: int, body: TagUpdate, db: Session = Depends(get_db)):
    tag = db.query(DimensionTag).get(tid)
    if not tag:
        raise HTTPException(404, "Tag not found")
    if body.name is not None:
        tag.name = body.name
    db.commit()
    db.refresh(tag)
    return tag


@app.post("/api/tags/merge", response_model=TagOut)
def merge_tags(body: TagMerge, db: Session = Depends(get_db)):
    keep = db.query(DimensionTag).get(body.keep_id)
    if not keep:
        raise HTTPException(404, "Keep tag not found")

    for mid in body.merge_ids:
        if mid == body.keep_id:
            continue
        merge_tag = db.query(DimensionTag).get(mid)
        if not merge_tag:
            continue
        keep.use_count += merge_tag.use_count
        tids = list(set((keep.topic_ids or []) + (merge_tag.topic_ids or [])))
        keep.topic_ids = tids
        # Update responses referencing the merged tag
        responses = db.query(StudentResponse).all()
        for resp in responses:
            if merge_tag.name in (resp.teacher_tags or []):
                resp.teacher_tags = [keep.name if t == merge_tag.name else t for t in resp.teacher_tags]
        db.delete(merge_tag)

    db.commit()
    db.refresh(keep)
    return keep


@app.delete("/api/tags/{tid}")
def delete_tag(tid: int, db: Session = Depends(get_db)):
    tag = db.query(DimensionTag).get(tid)
    if not tag:
        raise HTTPException(404, "Tag not found")
    db.delete(tag)
    db.commit()
    return {"ok": True}


# ════════════════════════════════════════════════════════════
# Report (class-level analytics)
# ════════════════════════════════════════════════════════════

@app.get("/api/courses/{cid}/report")
def class_report(cid: int, db: Session = Depends(get_db)):
    """Full class report: per-topic stats, per-student scores, top dimension tags."""
    topics = db.query(DebateTopic).filter(DebateTopic.course_id == cid).order_by(DebateTopic.order).all()
    students = db.query(Student).filter(Student.course_id == cid).all()

    # Per-topic
    topic_stats = []
    for topic in topics:
        dim_scores = {}
        uncertain = 0
        for st in students:
            resp = db.query(StudentResponse).filter(
                StudentResponse.student_id == st.id,
                StudentResponse.topic_id == topic.id,
            ).first()
            if not resp:
                continue
            conf = resp.teacher_confidence_override or resp.ai_confidence
            scores = resp.teacher_dimension_scores or resp.ai_dimension_scores
            if conf == "uncertain" and not resp.teacher_dimension_scores:
                uncertain += 1
                continue
            if scores:
                for dim, rating in scores.items():
                    value = rating_to_value(rating)
                    if value is None:
                        continue
                    if dim not in dim_scores:
                        dim_scores[dim] = []
                    dim_scores[dim].append(value)

        avg_dims = {d: round(sum(v) / len(v), 2) for d, v in dim_scores.items()} if dim_scores else {}
        topic_stats.append({
            "topic_id": topic.id, "title": topic.title,
            "cognitive_tier": topic.cognitive_tier,
            "avg_dimension_scores": avg_dims,
            "uncertain": uncertain,
        })

    # Per-student
    student_stats = []
    class_dims = {}
    class_quick = {"good": 0, "guide": 0, "echo": 0}
    for st in students:
        all_vals = []
        unc = 0
        bonus_flags = []
        quick_counts = {"good": 0, "guide": 0, "echo": 0}
        for topic in topics:
            resp = db.query(StudentResponse).filter(
                StudentResponse.student_id == st.id,
                StudentResponse.topic_id == topic.id,
            ).first()
            if not resp:
                continue
            if resp.teacher_rating in quick_counts:
                quick_counts[resp.teacher_rating] += 1
                class_quick[resp.teacher_rating] += 1
            conf = resp.teacher_confidence_override or resp.ai_confidence
            scores = resp.teacher_dimension_scores or resp.ai_dimension_scores
            bonus_flags.extend(resp.ai_bonus_flags or [])
            if conf == "uncertain" and not resp.teacher_dimension_scores:
                unc += 1
            elif scores:
                for dim, rating in scores.items():
                    value = rating_to_value(rating)
                    if value is not None:
                        all_vals.append(value)
                        class_dims.setdefault(dim, []).append(value)

        avg_score = sum(all_vals) / len(all_vals) if all_vals else 0
        pass_line = pass_line_for_grade(st.grade)
        student_stats.append({
            "student_id": st.id, "name": st.name, "grade": st.grade,
            "cognitive_tier": st.cognitive_tier,
            "avg_score": round(avg_score, 2),
            "pass_line": pass_line,
            "passing": avg_score >= pass_line,
            "uncertain": unc,
            "bonus_flags": sorted(set(bonus_flags)),
            "quick_ratings": quick_counts,
        })

    # Top tags
    tags = db.query(DimensionTag).filter(
        DimensionTag.course_id == cid, DimensionTag.use_count > 0
    ).order_by(
        DimensionTag.use_count.desc()
    ).limit(10).all()
    top_tags = [{"name": t.name, "count": t.use_count, "source": t.source} for t in tags]

    # Class average
    all_student_avgs = [s["avg_score"] for s in student_stats if s["avg_score"] > 0]
    class_avg = sum(all_student_avgs) / len(all_student_avgs) if all_student_avgs else 0
    pass_count = sum(1 for s in student_stats if s["passing"])
    assessed = sum(1 for s in student_stats if s["avg_score"] > 0)

    return {
        "class_avg": round(class_avg, 2),
        "student_count": len(students),
        "pass_count": pass_count,
        "pass_rate": round(pass_count / assessed, 2) if assessed else 0,
        "class_dim_avg": {
            d: round(sum(v) / len(v), 2) for d, v in class_dims.items()
        },
        "topic_stats": topic_stats,
        "student_stats": student_stats,
        "top_tags": top_tags,
        "quick_rating_counts": class_quick,
    }


@app.get("/api/students/{sid}/report")
def student_report(sid: int, db: Session = Depends(get_db)):
    """Parent-facing report endpoint (interface reserved; no frontend yet).

    Returns a structured per-student report using the enterprise five-dimension
    language so a future parent page / Feishu bot can consume it directly.
    """
    student = db.query(Student).get(sid)
    if not student:
        raise HTTPException(404, "Student not found")

    response = (
        db.query(StudentResponse)
        .filter(StudentResponse.student_id == sid)
        .order_by(StudentResponse.id.desc())
        .first()
    )
    if not response:
        return {
            "student_id": sid,
            "name": student.name,
            "grade": student.grade,
            "has_report": False,
            "dimensions": {},
            "teacher_comment": "",
            "rating": "",
            "next_steps": [],
        }

    scores = response.teacher_dimension_scores or response.ai_dimension_scores or {}
    dim_labels = {
        "position": "立意（观点鲜明）", "material": "选材（言之有物）",
        "structure": "结构（条理清晰）", "language": "语言（用词准确）",
        "perspective": "视角（换位思考）",
        # Legacy keys (for older records)
        "clarity": "立意（观点鲜明）", "interpretation": "立意（观点鲜明）",
        "evidence_awareness": "选材（言之有物）", "evidence_use": "选材（言之有物）",
        "relevance": "结构（条理清晰）", "inference": "结构（条理清晰）",
        "argument_evaluation": "结构（条理清晰）", "depth_breadth": "视角（换位思考）",
        "self_regulation": "视角（换位思考）",
    }
    dimensions = {}
    score_values = []
    for dim, rating in scores.items():
        label = dim_labels.get(dim, dim)
        dimensions[label] = rating
        value = rating_to_value(rating)
        if value is not None:
            score_values.append(value)
    avg_score = sum(score_values) / len(score_values) if score_values else 0
    pass_line = pass_line_for_grade(student.grade)

    band = _upgrade_band(_band_for_avg(avg_score, pass_line), response.ai_bonus_flags or [])
    return {
        "student_id": sid,
        "name": student.name,
        "grade": student.grade,
        "avg_score": round(avg_score, 2),
        "pass_line": pass_line,
        "passing": bool(score_values) and avg_score >= pass_line,
        "has_report": True,
        "topic_title": response.topic.title if response.topic else "",
        "dimensions": dimensions,
        "teacher_comment": response.teacher_note or "",
        "rating": band,
        "quick_rating": response.teacher_rating or "",
        "bonus_flags": response.ai_bonus_flags or [],
        "reviewed": response.teacher_reviewed,
        "next_steps": [
            "下节课重点关注"
            + (_QUICK_RATING_LABELS.get(response.teacher_rating or "", "本次表达"))
            + "对应的引导方向"
        ],
    }


# ════════════════════════════════════════════════════════════
# Rubric Templates (read-only)
# ════════════════════════════════════════════════════════════

@app.get("/api/rubric-templates", response_model=list[RubricTemplateOut])
def list_rubric_templates(db: Session = Depends(get_db)):
    return db.query(RubricTemplate).all()


# ════════════════════════════════════════════════════════════
# Serve built frontend (production mode)
# ════════════════════════════════════════════════════════════

_frontend_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "frontend", "dist")

if os.path.isdir(_frontend_dir):
    @app.get("/")
    def _serve_index():
        return FileResponse(os.path.join(_frontend_dir, "index.html"))

    # Must come AFTER all other routes
    app.mount("/assets", StaticFiles(directory=os.path.join(_frontend_dir, "assets")), name="static-assets")

    @app.get("/{full_path:path}")
    def _spa_fallback(full_path: str):
        """SPA fallback: serve index.html for any non-API route."""
        return FileResponse(os.path.join(_frontend_dir, "index.html"))
