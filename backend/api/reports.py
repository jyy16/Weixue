"""Class/student reports and rubric-template endpoints."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database import (
    DebateTopic, DimensionTag, RubricTemplate, Student, StudentResponse, get_db,
)
from grading.ratings import pass_line_for_grade, rating_to_value
from schemas import RubricTemplateOut

from . import state


router = APIRouter(tags=["reports"])

@router.get("/api/courses/{cid}/report")
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

@router.get("/api/students/{sid}/report")
def student_report(sid: int, db: Session = Depends(get_db)):
    """Parent-facing report endpoint (interface reserved; no frontend yet).

    Returns a structured per-student report using the enterprise five-dimension
    language so a future parent page / Feishu bot can consume it directly.
    """
    student = db.query(Student).get(sid)
    if not student:
        raise HTTPException(404, "Student not found")

    responses = (
        db.query(StudentResponse)
        .filter(StudentResponse.student_id == sid)
        .all()
    )
    if not responses:
        return {
            "student_id": sid,
            "name": student.name,
            "grade": student.grade,
            "has_report": False,
            "dimensions": {},
            "teacher_comment": "",
            "best_answer": None,
            "rating": "",
        }
    response = max(responses, key=lambda r: r.id)

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

    band = state._upgrade_band(state._band_for_avg(avg_score, pass_line), response.ai_bonus_flags or [])

    best_answer = None
    best_score = -1.0
    for r in responses:
        text = (r.cleaned_text or r.raw_text or "").strip()
        if not text:
            continue
        r_scores = r.teacher_dimension_scores or r.ai_dimension_scores or {}
        vals = [rating_to_value(v) for v in r_scores.values() if rating_to_value(v) is not None]
        if not vals:
            continue
        avg = sum(vals) / len(vals)
        if avg > best_score:
            best_score = avg
            best_answer = {
                "topic_title": r.topic.title if r.topic else "",
                "text": text,
                "score": round(avg, 2),
            }

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
        "teacher_comment": student.comment_draft or "",
        "best_answer": best_answer,
        "rating": band,
        "quick_rating": response.teacher_rating or "",
        "bonus_flags": response.ai_bonus_flags or [],
        "reviewed": response.teacher_reviewed,
    }

@router.get("/api/rubric-templates", response_model=list[RubricTemplateOut])
def list_rubric_templates(db: Session = Depends(get_db)):
    return db.query(RubricTemplate).all()
