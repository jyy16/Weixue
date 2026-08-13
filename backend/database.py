"""Database setup and SQLAlchemy ORM models.

Weixue critical thinking assessment system.
"""

import os
from datetime import datetime
from sqlalchemy import (
    create_engine, Column, Integer, String, Float, Boolean, Text,
    DateTime, ForeignKey, JSON, UniqueConstraint
)
from sqlalchemy.orm import sessionmaker, relationship, declarative_base

# WEIXUE_DB_PATH overrides the default SQLite location (useful for tests/temp DBs).
DB_PATH = os.getenv(
    "WEIXUE_DB_PATH",
    os.path.join(os.path.dirname(__file__), "data", "grading.db"),
)
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

# Background Feishu delivery and normal API requests may briefly write at the
# same time. Let SQLite wait for the current writer instead of immediately
# raising ``database is locked``; delivery-specific writes also retry below.
engine = create_engine(
    f"sqlite:///{DB_PATH}",
    echo=False,
    connect_args={"timeout": 10},
)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
Base = declarative_base()


# ── Cognitive tier helper ───────────────────────────────────

def get_cognitive_tier(grade: int) -> str:
    """Map grade (1-7) to cognitive tier based on Kuhn (1999) epistemological development.

    basic      (1-2年级): Absolutist — knowledge as direct copy of reality,
                           CT precursor skills (expression, simple causation)
    developing (3-5年级): Absolutist → Multiplist transition — can give reasons
                           but treats all opinions as equally valid
    advancing  (6-7年级): Multiplist → Evaluativist transition — can evaluate
                           argument quality and consider counter-arguments

    Reference: Kuhn, D. (1999). A Developmental Model of Critical Thinking.
    Educational Researcher, 28(2), 16-23.
    """
    if grade <= 2:
        return "basic"
    elif grade <= 5:
        return "developing"
    else:
        return "advancing"


# ── ORM Models ──────────────────────────────────────────────

class Course(Base):
    __tablename__ = "courses"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(200), nullable=False)
    class_name = Column(String(100), nullable=False)
    grade_level = Column(Integer, nullable=False)   # primary target grade (1-7)
    created_at = Column(DateTime, default=datetime.utcnow)

    topics = relationship("DebateTopic", back_populates="course",
                          cascade="all, delete-orphan", order_by="DebateTopic.order")
    students = relationship("Student", back_populates="course",
                            cascade="all, delete-orphan")
    prep_plan = relationship("PrepPlan", back_populates="course",
                             cascade="all, delete-orphan", uselist=False)


class DebateTopic(Base):
    __tablename__ = "debate_topics"

    id = Column(Integer, primary_key=True, index=True)
    course_id = Column(Integer, ForeignKey("courses.id"), nullable=False)
    title = Column(String(300), nullable=False)          # the debate question
    topic_type = Column(String(50), default="dilemma")   # dilemma / fact_opinion / causal
    cognitive_tier = Column(String(20), default="developing")
    stimulus_material = Column(Text, default="")         # passage, image description, etc.
    reference_arguments = Column(JSON, default=list)     # ["pro argument 1", ...]
    rubric_template_id = Column(Integer, ForeignKey("rubric_templates.id"), nullable=True)
    max_score = Column(Integer, default=10)
    order = Column(Integer, default=0)

    course = relationship("Course", back_populates="topics")
    rubric_template = relationship("RubricTemplate")
    responses = relationship("StudentResponse", back_populates="topic",
                             cascade="all, delete-orphan")


class Student(Base):
    __tablename__ = "students"

    id = Column(Integer, primary_key=True, index=True)
    course_id = Column(Integer, ForeignKey("courses.id"), nullable=False)
    name = Column(String(100), nullable=False)
    grade = Column(Integer, nullable=False)   # 1-7
    comment_draft = Column(Text, default="")  # saved comment draft
    phone = Column(String(30), default="")    # mobile for future Feishu push
    # The user's identity is scoped to the current Feishu app.  It is stored on
    # the student rather than in environment variables so each comment can be
    # routed to the matching recipient.
    feishu_open_id = Column(String(100), default="")
    comment_delivery_status = Column(String(20), default="not_sent")
    comment_delivery_hash = Column(String(64), default="")
    comment_delivery_error = Column(Text, default="")
    comment_delivered_at = Column(DateTime(timezone=True), nullable=True)

    course = relationship("Course", back_populates="students")
    responses = relationship("StudentResponse", back_populates="student",
                             cascade="all, delete-orphan")

    @property
    def cognitive_tier(self) -> str:
        return get_cognitive_tier(self.grade)


class StudentResponse(Base):
    __tablename__ = "student_responses"

    id = Column(Integer, primary_key=True, index=True)
    student_id = Column(Integer, ForeignKey("students.id"), nullable=False)
    topic_id = Column(Integer, ForeignKey("debate_topics.id"), nullable=False)

    # Raw and cleaned text
    raw_text = Column(Text, default="")       # original speech/writing (with noise)
    cleaned_text = Column(Text, default="")   # after cleaning stage
    source = Column(String(20), default="manual")  # manual / audio / student_device / teacher / asr
    audio_recording_id = Column(Integer, ForeignKey("audio_recordings.id"), nullable=True)
    segment_start_ms = Column(Integer, nullable=True)
    segment_end_ms = Column(Integer, nullable=True)

    # AI multi-dimensional assessment
    ai_dimension_scores = Column(JSON, nullable=True)
    # e.g. {"position": "A", "material": "B+", "structure": "B", "language": "A-", "perspective": "B+"}

    ai_confidence = Column(String(20), default="uncertain")
    # certain_good / certain_weak / uncertain

    ai_reasoning = Column(JSON, default=dict)
    # per-dimension reasoning chain:
    # {"position": {"evidence": "...", "reasoning": "...", "rating": "A"}, ...}

    ai_extracted_features = Column(JSON, default=dict)
    # {"arguments_count": 2, "counter_arguments": 0, "causal_connectors": ["因为","所以"], ...}

    ai_note = Column(Text, default="")
    ai_suggested_tags = Column(JSON, default=list)
    ai_bonus_flags = Column(JSON, default=list)
    # 对话生命周期：None=进行中；'student'/'teacher'=任一方主动结束；'auto'=答满 3 轮自动结束
    dialogue_finished = Column(String(20), nullable=True)

    # Teacher override (per-dimension)
    teacher_dimension_scores = Column(JSON, nullable=True)
    teacher_confidence_override = Column(String(20), nullable=True)
    teacher_tags = Column(JSON, default=list)
    teacher_note = Column(Text, default="")
    teacher_reviewed = Column(Boolean, default=False)
    teacher_rating = Column(String(20), default="")

    # Live-class companion pipeline status (student window drives the first
    # states; backend advances through processing/processed).
    processing_status = Column(String(20), default="not_started")
    # not_started / recording / submitted / processing / processed

    student = relationship("Student", back_populates="responses")
    topic = relationship("DebateTopic", back_populates="responses")
    calibrations = relationship("CalibrationRecord", back_populates="response",
                                cascade="all, delete-orphan")
    companion_turns = relationship("CompanionTurn", back_populates="response",
                                   cascade="all, delete-orphan",
                                   order_by="CompanionTurn.created_at")


class CompanionTurn(Base):
    """One turn of the AI-companion dialogue bound to a StudentResponse.

    role: student (oral answer) / ai_suggestion (scaffolding question
          suggested to the teacher) / teacher (question actually asked).
    turn_type: scaffold / counter_example / elicitation / echo_risk / note.
    The full dialogue history is injected into the evaluator prompt so the
    assessment can account for AI/teacher scaffolding and echo detection.
    """

    __tablename__ = "companion_turns"

    id = Column(Integer, primary_key=True, index=True)
    response_id = Column(Integer, ForeignKey("student_responses.id"), nullable=False)
    role = Column(String(20), nullable=False)
    content = Column(Text, default="")
    turn_type = Column(String(30), default="")
    created_at = Column(DateTime, default=datetime.utcnow)

    response = relationship("StudentResponse", back_populates="companion_turns")


class RubricTemplate(Base):
    """Cognitive-tier-specific rubric configuration.

    Each template defines which dimensions are active, their weights,
    behavioral anchor definitions, and negative indicators for the LLM.
    """
    __tablename__ = "rubric_templates"

    id = Column(Integer, primary_key=True, index=True)
    cognitive_tier = Column(String(20), nullable=False, unique=True)
    # basic / developing / advancing

    grade_range = Column(String(20), nullable=False)
    # e.g. "1-2", "3-5", "6-7"

    active_dimensions = Column(JSON, nullable=False)
    # ["position", "material", "structure", "language", "perspective"]

    dimension_weights = Column(JSON, nullable=False)
    # {"position": 0.2, "material": 0.2, "structure": 0.2, "language": 0.2, "perspective": 0.2}

    rubric_definitions = Column(JSON, nullable=False)
    # {"position": {"name": "立意（观点鲜明）", "description": "...", "levels": {"A": "...", ...}}, ...}

    negative_indicators = Column(JSON, nullable=False)
    # {"position": "无法辨识核心观点", ...}

    prompt_template = Column(Text, nullable=False)
    # LLM system prompt template for this tier

    created_at = Column(DateTime, default=datetime.utcnow)


class CalibrationRecord(Base):
    """Stores teacher corrections to AI assessments for feedback alignment.

    Each record captures one instance where the teacher modified AI-generated
    dimension scores. These records are retrieved as few-shot examples
    during subsequent assessments to align AI output with teacher preferences.
    """
    __tablename__ = "calibration_records"

    id = Column(Integer, primary_key=True, index=True)
    response_id = Column(Integer, ForeignKey("student_responses.id"), nullable=False)
    teacher_id = Column(String(50), default="default")

    ai_original_scores = Column(JSON, nullable=False)
    # {"position": "A", "material": "B+", ...}

    teacher_final_scores = Column(JSON, nullable=False)
    # {"position": "A", "material": "A", ...}

    modifications = Column(JSON, default=list)
    # [{"dimension": "material", "from": "B+", "to": "A", "reason": "..."}, ...]

    note = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.utcnow)

    response = relationship("StudentResponse", back_populates="calibrations")


class DimensionTag(Base):
    """Tags for categorizing critical thinking behaviors."""
    __tablename__ = "dimension_tags"

    id = Column(Integer, primary_key=True, index=True)
    course_id = Column(Integer, ForeignKey("courses.id"), nullable=False)
    name = Column(String(200), nullable=False)
    source = Column(String(20), default="base")   # base / ai_new
    use_count = Column(Integer, default=0)
    topic_ids = Column(JSON, default=list)


class AudioRecording(Base):
    """A raw audio file uploaded by the teacher.

    Scenario A (homework): one recording maps to one StudentResponse.
    Scenario B (classroom): one recording is segmented into many responses via
    StudentResponse.segment_start_ms / segment_end_ms.
    """

    __tablename__ = "audio_recordings"

    id = Column(Integer, primary_key=True, index=True)
    course_id = Column(Integer, ForeignKey("courses.id"), nullable=False)
    topic_id = Column(Integer, ForeignKey("debate_topics.id"), nullable=False)
    file_path = Column(String(500), default="")
    duration_ms = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class SystemSetting(Base):
    """Simple key-value settings persisted in the database.

    Used for runtime toggles such as the ASR provider selection, so a teacher
    can switch between mock (demo) and a real speech-recognition provider
    without editing environment variables or restarting the server.
    """

    __tablename__ = "system_settings"

    key = Column(String(80), primary_key=True)
    value = Column(Text, default="")


class PrepPlan(Base):
    """One teacher lesson-prep plan per course (workbench 备课辅助).

    lesson_plan: ordered topic ids the teacher chose to review in class.
    notes:       {topic_id: teacher's own teaching note} (string keys).
    confirmed:   teacher pressed "确认讲评计划" (the human decision gate).

    The plan is the teacher's draft/decision; AI only aggregates analytics.
    """
    __tablename__ = "prep_plans"

    id = Column(Integer, primary_key=True, index=True)
    course_id = Column(Integer, ForeignKey("courses.id"), nullable=False, unique=True)
    lesson_plan = Column(JSON, default=list)   # [topic_id, ...] in review order
    notes = Column(JSON, default=dict)         # {str(topic_id): note text}
    confirmed = Column(Boolean, default=False)
    summary = Column(JSON, default=dict)
    # {"overview": "...", "problems": "...", "suggestions": "...",
    #  "generated_by": "llm"|"template", "generated_at": iso}
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    course = relationship("Course", back_populates="prep_plan")


class FeishuBinding(Base):
    """Maps a local entity to a Feishu Bitable record (two-way sync).

    Kept on the local side so that subsequent syncs can batch_update the
    same remote row instead of creating duplicates, and so that pull-back
    can tell remote edits apart from our own pushes via last_synced_hash.
    entity_type is one of course / topic / student / response / prep_plan;
    table_key matches the Bitable table names used in FEISHU_BITABLE_TABLE_IDS.
    """

    __tablename__ = "feishu_bindings"
    __table_args__ = (
        UniqueConstraint(
            "entity_type", "entity_id", "table_key",
            name="uq_feishu_binding_entity",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    entity_type = Column(String(30), nullable=False)
    entity_id = Column(Integer, nullable=False)
    table_key = Column(String(30), nullable=False)
    remote_record_id = Column(String(120), nullable=False)
    # Two-way sync snapshot: hash of the teacher-owned fields as last seen,
    # used to detect remote edits without replaying our own pushes.
    last_synced_hash = Column(String(64), default="", nullable=False, server_default="")
    last_synced_at = Column(DateTime, nullable=True)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


# ── Helpers ─────────────────────────────────────────────────

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    Base.metadata.create_all(bind=engine)
    _migrate()


def _migrate():
    """Lightweight additive migrations for existing SQLite databases.

    SQLAlchemy create_all() does not alter existing tables, so additive columns
    on existing Student and StudentResponse tables are created here.
    """
    from sqlalchemy import text

    with engine.connect() as conn:
        student_cols = {
            row[1] for row in conn.execute(text("PRAGMA table_info(students)"))
        }
        if "feishu_open_id" not in student_cols:
            conn.execute(text("ALTER TABLE students ADD COLUMN feishu_open_id VARCHAR(100) DEFAULT ''"))
        if "comment_delivery_status" not in student_cols:
            conn.execute(text("ALTER TABLE students ADD COLUMN comment_delivery_status VARCHAR(20) DEFAULT 'not_sent'"))
        if "comment_delivery_hash" not in student_cols:
            conn.execute(text("ALTER TABLE students ADD COLUMN comment_delivery_hash VARCHAR(64) DEFAULT ''"))
        if "comment_delivery_error" not in student_cols:
            conn.execute(text("ALTER TABLE students ADD COLUMN comment_delivery_error TEXT DEFAULT ''"))
        if "comment_delivered_at" not in student_cols:
            conn.execute(text("ALTER TABLE students ADD COLUMN comment_delivered_at DATETIME"))
        if "phone" not in student_cols:
            conn.execute(text("ALTER TABLE students ADD COLUMN phone VARCHAR(30) DEFAULT ''"))

        cols = {row[1] for row in conn.execute(text("PRAGMA table_info(student_responses)"))}
        if "source" not in cols:
            conn.execute(text("ALTER TABLE student_responses ADD COLUMN source VARCHAR(20) DEFAULT 'manual'"))
        if "audio_recording_id" not in cols:
            conn.execute(text("ALTER TABLE student_responses ADD COLUMN audio_recording_id INTEGER REFERENCES audio_recordings(id)"))
        if "segment_start_ms" not in cols:
            conn.execute(text("ALTER TABLE student_responses ADD COLUMN segment_start_ms INTEGER"))
        if "segment_end_ms" not in cols:
            conn.execute(text("ALTER TABLE student_responses ADD COLUMN segment_end_ms INTEGER"))
        if "processing_status" not in cols:
            conn.execute(text("ALTER TABLE student_responses ADD COLUMN processing_status VARCHAR(20) DEFAULT 'not_started'"))
        if "teacher_rating" not in cols:
            conn.execute(text("ALTER TABLE student_responses ADD COLUMN teacher_rating VARCHAR(20) DEFAULT ''"))
        if "ai_bonus_flags" not in cols:
            conn.execute(text("ALTER TABLE student_responses ADD COLUMN ai_bonus_flags JSON DEFAULT '[]'"))
        if "dialogue_finished" not in cols:
            conn.execute(text("ALTER TABLE student_responses ADD COLUMN dialogue_finished VARCHAR(20)"))
        prep_cols = {
            row[1] for row in conn.execute(text("PRAGMA table_info(prep_plans)"))
        }
        if "summary" not in prep_cols:
            conn.execute(text("ALTER TABLE prep_plans ADD COLUMN summary JSON"))
        binding_cols = {
            row[1] for row in conn.execute(text("PRAGMA table_info(feishu_bindings)"))
        }
        if "last_synced_hash" not in binding_cols:
            conn.execute(text(
                "ALTER TABLE feishu_bindings ADD COLUMN last_synced_hash VARCHAR(64) DEFAULT '' NOT NULL"
            ))
        if "last_synced_at" not in binding_cols:
            conn.execute(text("ALTER TABLE feishu_bindings ADD COLUMN last_synced_at DATETIME"))
        # Enforce (entity_type, entity_id, table_key) uniqueness on pre-existing
        # databases (create_all adds it on fresh ones). Dedup first, keeping the
        # earliest row per group, then add the unique index.
        idx_rows = conn.execute(text("PRAGMA index_list(feishu_bindings)")).fetchall()
        if not any(row[2] for row in idx_rows):  # row[2] == unique flag
            conn.execute(text(
                "DELETE FROM feishu_bindings WHERE id NOT IN ("
                "SELECT MIN(id) FROM feishu_bindings "
                "GROUP BY entity_type, entity_id, table_key)"
            ))
            conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_feishu_binding_entity "
                "ON feishu_bindings (entity_type, entity_id, table_key)"
            ))
        conn.commit()
