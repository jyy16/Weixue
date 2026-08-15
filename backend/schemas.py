"""Pydantic schemas for API request/response validation."""

from __future__ import annotations
from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime


# ── Course (was Assignment) ─────────────────────────────────

class CourseBase(BaseModel):
    title: str
    class_name: str
    grade_level: int

class CourseCreate(CourseBase):
    pass

class CourseOut(CourseBase):
    id: int
    created_at: datetime
    topic_count: int = 0
    student_count: int = 0
    class Config:
        from_attributes = True


# ── DebateTopic (was Question) ──────────────────────────────

class DebateTopicBase(BaseModel):
    title: str
    topic_type: str = "dilemma"
    cognitive_tier: str = "developing"
    stimulus_material: str = ""
    reference_arguments: list[str] = Field(default_factory=list)
    max_score: int = 10

class DebateTopicCreate(DebateTopicBase):
    rubric_template_id: Optional[int] = None

class DebateTopicUpdate(BaseModel):
    title: Optional[str] = None
    topic_type: Optional[str] = None
    cognitive_tier: Optional[str] = None
    stimulus_material: Optional[str] = None
    reference_arguments: Optional[list[str]] = None
    max_score: Optional[int] = None
    order: Optional[int] = None

class DebateTopicOut(DebateTopicBase):
    id: int
    course_id: int
    rubric_template_id: Optional[int] = None
    order: int
    class Config:
        from_attributes = True


# ── Student ─────────────────────────────────────────────────

class StudentBase(BaseModel):
    name: str
    grade: int
    phone: str = ""

class StudentCreate(StudentBase):
    feishu_open_id: str = ""

class StudentUpdate(BaseModel):
    name: Optional[str] = None
    grade: Optional[int] = None
    phone: Optional[str] = None
    feishu_open_id: Optional[str] = None

class StudentBatchCreate(BaseModel):
    students: list[StudentCreate] = Field(default_factory=list)

class StudentOut(StudentBase):
    id: int
    course_id: int
    cognitive_tier: str = ""
    comment_draft: str = ""
    feishu_open_id: str = ""
    comment_delivery_status: str = "not_sent"
    comment_delivery_error: str = ""
    comment_delivered_at: Optional[datetime] = None
    class Config:
        from_attributes = True


# ── StudentResponse (was Submission) ────────────────────────

class DimensionScore(BaseModel):
    """Single dimension evaluation result."""
    dimension: str          # e.g. "position", "material"（五维度：立意/选材/结构/语言/视角）
    rating: str             # A+/A/A-/B+/B/B-
    evidence: str = ""      # text evidence from student response
    reasoning: str = ""     # why this rating was given

class AssessmentResult(BaseModel):
    """Full AI assessment result for one response."""
    dimension_scores: dict[str, str] = Field(default_factory=dict)
    # {"position": "A", "material": "B+", "structure": "B", "language": "A-", "perspective": "B+"}
    confidence: str = "uncertain"
    reasoning: dict = Field(default_factory=dict)
    extracted_features: dict = Field(default_factory=dict)
    bonus_flags: list[str] = Field(default_factory=list)
    note: str = ""
    suggested_tags: list[str] = Field(default_factory=list)

class StudentResponseOut(BaseModel):
    id: int
    student_id: int
    topic_id: int

    raw_text: str = ""
    cleaned_text: str = ""
    source: str = "manual"
    audio_recording_id: Optional[int] = None
    segment_start_ms: Optional[int] = None
    segment_end_ms: Optional[int] = None

    ai_dimension_scores: Optional[dict] = None
    ai_confidence: str = "uncertain"
    ai_reasoning: dict = Field(default_factory=dict)
    ai_extracted_features: dict = Field(default_factory=dict)
    ai_bonus_flags: list = Field(default_factory=list)
    ai_note: str = ""
    ai_suggested_tags: list = Field(default_factory=list)
    dialogue_finished: Optional[str] = None

    teacher_dimension_scores: Optional[dict] = None
    teacher_confidence_override: Optional[str] = None
    teacher_tags: list = Field(default_factory=list)
    teacher_note: str = ""
    teacher_reviewed: bool = False
    teacher_rating: str = ""
    processing_status: str = "not_started"

    class Config:
        from_attributes = True


class AudioImportOut(StudentResponseOut):
    """Audio import result plus the transcript produced for this upload.

    ``raw_text`` may contain all accumulated live-dialogue rounds, while this
    field always contains only the just-transcribed recording so the student
    UI can render the new bubble without guessing.
    """
    transcript: str = ""

class TeacherReview(BaseModel):
    """Teacher overrides AI assessment on specific dimensions."""
    dimension_scores: Optional[dict[str, str]] = None
    confidence_override: Optional[str] = None
    tags: list[str] = Field(default_factory=list)
    note: str = ""
    rating: Optional[str] = None


class QuickRatingUpdate(BaseModel):
    """On-the-spot live rating saved BEFORE the AI assessment push.

    Only records the teacher's instant judgment (teacher_rating / note); it
    does NOT mark the response as reviewed or create calibration records --
    the formal five-dimension review still happens later in the grading page.
    """
    rating: str = ""        # good / guide / echo（空串清除）
    note: str = ""


# ── AI Companion (live-class dialogue) ───────────────────────

class CompanionTurnCreate(BaseModel):
    """Append a turn to the companion dialogue of a response."""
    role: str                      # student / ai_suggestion / teacher
    content: str
    turn_type: str = ""            # scaffold / counter_example / elicitation / echo_risk / note


class CompanionTurnOut(BaseModel):
    id: int
    response_id: int
    role: str
    content: str
    turn_type: str = ""
    created_at: datetime

    class Config:
        from_attributes = True


class StatusUpdate(BaseModel):
    """Advance the live-class processing pipeline of a response."""
    status: str                    # not_started / recording / submitted / processing / processed


class SuggestTurnOut(BaseModel):
    """AI scaffolding-question suggestion + echo detection result."""
    questions: list[str] = Field(default_factory=list)
    scaffold_status: str = "ok"    # ok / continue / echo_risk
    echo_risk: bool = False
    note: str = ""

class TextImportRequest(BaseModel):
    """Manual transcript paste from the recording entry page."""
    student_id: int
    topic_id: int
    text: str
    source: str = "manual"   # manual / student_device / teacher


# ── RubricTemplate ──────────────────────────────────────────

class RubricTemplateOut(BaseModel):
    id: int
    cognitive_tier: str
    grade_range: str
    active_dimensions: list[str]
    dimension_weights: dict[str, float]
    rubric_definitions: dict
    negative_indicators: dict
    prompt_template: str
    class Config:
        from_attributes = True


# ── CalibrationRecord ───────────────────────────────────────

class CalibrationModification(BaseModel):
    dimension: str
    from_rating: str
    to_rating: str
    reason: str = ""

class CalibrationRecordOut(BaseModel):
    id: int
    response_id: int
    teacher_id: str = "default"
    ai_original_scores: dict
    teacher_final_scores: dict
    modifications: list[CalibrationModification] = Field(default_factory=list)
    note: str = ""
    created_at: datetime
    class Config:
        from_attributes = True

class CalibrationRecordCreate(BaseModel):
    response_id: int
    teacher_id: str = "default"
    ai_original_scores: dict
    teacher_final_scores: dict
    modifications: list[CalibrationModification] = Field(default_factory=list)
    note: str = ""


# ── Comment ─────────────────────────────────────────────────

class CommentRequest(BaseModel):
    student_id: int

class CommentOut(BaseModel):
    draft: str

class CommentSaveRequest(BaseModel):
    student_id: int
    draft: str

class CommentSendRequest(CommentSaveRequest):
    pass

class CommentSendOut(BaseModel):
    ok: bool
    student_id: int
    status: str
    message: str

class BatchCommentOut(BaseModel):
    results: list[dict] = Field(default_factory=list)
    # [{"student_id": 1, "student_name": "小雨", "draft": "...", "error": null}, ...]


# ── Analytics ───────────────────────────────────────────────

class TopicAnalytics(BaseModel):
    topic_id: int
    title: str
    topic_type: str
    cognitive_tier: str
    avg_dimension_scores: dict[str, float] = Field(default_factory=dict)
    # {"position": 0.75, "material": 0.62, ...}
    weak_dimensions: list[str] = Field(default_factory=list)
    low_students: list[str] = Field(default_factory=list)
    error_tags: list[dict] = Field(default_factory=list)


# ── DimensionTag ────────────────────────────────────────────

class TagOut(BaseModel):
    id: int
    name: str
    source: str
    use_count: int
    topic_ids: list = Field(default_factory=list)
    class Config:
        from_attributes = True

class TagUpdate(BaseModel):
    name: Optional[str] = None

class TagMerge(BaseModel):
    keep_id: int
    merge_ids: list[int]


# ── PrepPlan (备课辅助讲评计划) ──────────────────────────────

class PrepPlanOut(BaseModel):
    course_id: int
    lesson_plan: list[int] = Field(default_factory=list)
    notes: dict[str, str] = Field(default_factory=dict)
    confirmed: bool = False
    summary: dict = Field(default_factory=dict)
    updated_at: Optional[datetime] = None
    class Config:
        from_attributes = True


class PrepPlanUpdate(BaseModel):
    lesson_plan: list[int] = Field(default_factory=list)
    notes: dict[str, str] = Field(default_factory=dict)
    confirmed: bool = False


class PrepPlanPushOut(BaseModel):
    ok: bool
    status: str = ""
    message: str = ""


class PrepInsightsOut(BaseModel):
    """Deterministic per-course prep insights (no LLM, always available)."""
    course_id: int
    participation: dict = Field(default_factory=dict)
    tier_summary: dict = Field(default_factory=dict)
    highlights: list[dict] = Field(default_factory=list)
    topic_highlights: list[dict] = Field(default_factory=list)
    problem_patterns: list[dict] = Field(default_factory=list)
    top_tags: list[dict] = Field(default_factory=list)
    quick_rating_counts: dict = Field(default_factory=dict)


class PrepSummaryUpdate(BaseModel):
    """Teacher edits to an AI summary (topic_id=None ⇒ class-level)."""
    topic_id: Optional[int] = None
    overview: Optional[str] = None
    problems: Optional[str] = None
    suggestions: Optional[str] = None


# ── ASR settings ───────────────────────────────────────────

class ASRProviderInfo(BaseModel):
    id: str
    label: str
    ready: bool = False
    reason: str = ""

class ASRSettingOut(BaseModel):
    provider: str
    model: str = ""
    api_key_configured: bool = False
    providers: list[ASRProviderInfo] = Field(default_factory=list)
    demo: bool = False
    demo_data_present: bool = False

class ASRSettingUpdate(BaseModel):
    provider: str


class SystemModeOut(BaseModel):
    """Backend capability matrix used by the frontend 演示/真实 switch."""
    mode: str = "backend"
    demo_course_present: bool = False
    asr_provider: str = "mock"
    asr_ready: bool = False
    llm_configured: bool = False
    feishu_ready: bool = False
    bitable_ready: bool = False


class SystemModeAction(BaseModel):
    action: str   # enter_demo | enter_real


# ── In-app settings (LLM / ASR / Feishu / Bitable) ──────────

class SettingsItem(BaseModel):
    value: str = ""
    has_value: bool = False
    secret: bool = False


class SettingsOut(BaseModel):
    items: dict[str, SettingsItem] = Field(default_factory=dict)
    llm_configured: bool = False
    asr_configured: bool = False
    feishu_configured: bool = False
    bitable_configured: bool = False


class SettingsUpdate(BaseModel):
    settings: dict[str, str] = Field(default_factory=dict)
