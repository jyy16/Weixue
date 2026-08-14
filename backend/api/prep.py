"""Lesson-prep analytics, plan, insights and AI summary endpoints."""

import re
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session

from database import (
    Course, DebateTopic, PrepPlan, SessionLocal, Student, StudentResponse, get_db,
)
from feishu.bot import BotService
from feishu.sync import BitableSyncer
from grading.llm import LLMClient
from grading.ratings import is_passing, pass_line_for_grade, rating_to_value
from schemas import (
    PrepInsightsOut, PrepPlanOut, PrepPlanPushOut, PrepPlanUpdate,
    PrepSummaryUpdate, TopicAnalytics,
)

from . import state


router = APIRouter(tags=["prep"])

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

@router.get("/api/courses/{cid}/prep", response_model=list[TopicAnalytics])
def prep_analytics(cid: int, db: Session = Depends(get_db)):
    """Aggregate assessment results per topic for lesson prep."""
    return _prep_topic_rows(cid, db)

@router.get("/api/courses/{cid}/prep/plan", response_model=PrepPlanOut)
def get_prep_plan(cid: int, db: Session = Depends(get_db)):
    """Return the saved lesson-prep plan (an empty draft when none exists)."""
    if not db.get(Course, cid):
        raise HTTPException(404, "Course not found")
    plan = db.query(PrepPlan).filter(PrepPlan.course_id == cid).first()
    return plan if plan else PrepPlanOut(course_id=cid)

@router.put("/api/courses/{cid}/prep/plan", response_model=PrepPlanOut)
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
        syncer = BitableSyncer(state.feishu_client)
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

@router.post("/api/courses/{cid}/prep/plan/push", response_model=PrepPlanPushOut)
async def push_prep_plan(cid: int, db: Session = Depends(get_db)):
    """Push the saved lesson-prep plan as a Feishu interactive card to the
    teacher — the same bot channel as the comment-confirmation cards."""
    course = db.get(Course, cid)
    if not course:
        raise HTTPException(404, "Course not found")
    plan = db.query(PrepPlan).filter(PrepPlan.course_id == cid).first()
    if plan is None or not plan.lesson_plan:
        raise HTTPException(400, "请先保存讲评计划再推送")

    config = state.feishu_client.config
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
        await BotService(state.feishu_client).send_card(config.teacher_open_id, card)
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

@router.get("/api/courses/{cid}/prep/insights", response_model=PrepInsightsOut)
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
                f"{state._QUICK_RATING_LABELS[key]}{quick.get(key, 0)}人"
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
                f"{state._QUICK_RATING_LABELS[key]}{quick.get(key, 0)}人"
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

@router.post("/api/courses/{cid}/prep/summary")
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

@router.post("/api/courses/{cid}/prep/topics/{tid}/summary")
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

@router.put("/api/courses/{cid}/prep/summary")
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
