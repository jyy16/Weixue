"""Shared runtime state and helpers for the API routers.

The LLM/evaluator/companion/Feishu singletons live here so that
reload_runtime_settings() can rebuild them and every router observes the
change through ``state.llm`` / ``state.feishu_client`` at call time.
"""

import os
import threading

from sqlalchemy.orm import Session

import settings_store
from asr import ASRClient, ASRError
from companion import CompanionEngine
from database import (
    AudioRecording, CalibrationRecord, CompanionTurn, Course, DebateTopic,
    DimensionTag, FeishuBinding, Student, StudentResponse, SystemSetting,
)
from feishu import FeishuClient
from feishu.routes import reload_config as reload_feishu_config
from grading.evaluator import AssessmentEngine
from grading.llm import LLMClient
from schemas import ASRProviderInfo, ASRSettingOut


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

llm = LLMClient()

evaluator = AssessmentEngine(llm)

companion = CompanionEngine(llm)

feishu_client = FeishuClient.from_env()

def reload_runtime_settings(db: Session) -> dict:
    """Push DB-backed settings into os.environ and rebuild runtime singletons."""
    global llm, evaluator, companion, feishu_client
    settings = settings_store.get_all(db)
    settings_store.push_to_env(settings)
    llm = LLMClient()
    evaluator = AssessmentEngine(llm)
    companion = CompanionEngine(llm)
    feishu_client = FeishuClient.from_env()
    reload_feishu_config()
    return settings

UPLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads")

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

_assessment_progress = {}

_progress_lock = threading.Lock()

def _reset_comment_delivery(student: Student) -> None:
    """A changed draft must be explicitly sent and tracked as a new delivery."""
    student.comment_delivery_status = "not_sent"
    student.comment_delivery_hash = ""
    student.comment_delivery_error = ""
    student.comment_delivered_at = None

def _remove_audio_file(file_path: str) -> None:
    """Best-effort removal of an uploaded audio file (never raises)."""
    try:
        if file_path and os.path.isfile(file_path):
            os.remove(file_path)
    except OSError:
        pass
