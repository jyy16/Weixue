"""Export SQLite demo data to frontend/src/demo-data.json for GitHub Pages demo mode.

Usage:
    python export_demo_data.py [--course 1] [--output ../frontend/src/demo-data.json]

The frontend (src/api/demoClient.js) expects JSON columns to be serialized as
JSON strings (it re-parses them), so this script re-serializes them on export.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from database import (  # noqa: E402
    CalibrationRecord,
    Course,
    DebateTopic,
    DimensionTag,
    SessionLocal,
    Student,
    StudentResponse,
    init_db,
)

JSON_FIELDS = {
    "topics": ["reference_arguments"],
    "responses": [
        "ai_dimension_scores",
        "ai_reasoning",
        "ai_extracted_features",
        "ai_suggested_tags",
        "teacher_dimension_scores",
        "teacher_tags",
    ],
    "tags": ["topic_ids"],
    "calibrations": ["ai_original_scores", "teacher_final_scores", "modifications"],
}


def row_dict(obj, kind: str) -> dict:
    d = {c.name: getattr(obj, c.name) for c in obj.__table__.columns}
    for field in JSON_FIELDS.get(kind, []):
        if d.get(field) is not None:
            d[field] = json.dumps(d[field], ensure_ascii=False)
    for key, value in list(d.items()):
        if value is not None and hasattr(value, "isoformat"):
            d[key] = value.isoformat()
    return d


def dump_course(cid: int) -> dict:
    db = SessionLocal()
    try:
        course = db.get(Course, cid)
        if not course:
            raise SystemExit(f"course {cid} not found")
        topics = (
            db.query(DebateTopic)
            .filter(DebateTopic.course_id == cid)
            .order_by(DebateTopic.order)
            .all()
        )
        students = db.query(Student).filter(Student.course_id == cid).all()
        student_ids = [s.id for s in students]
        responses = (
            db.query(StudentResponse)
            .filter(StudentResponse.student_id.in_(student_ids))
            .order_by(StudentResponse.id)
            .all()
        )
        tags = (
            db.query(DimensionTag)
            .filter(DimensionTag.course_id == cid)
            .order_by(DimensionTag.id)
            .all()
        )
        response_ids = [r.id for r in responses]
        calibrations = (
            db.query(CalibrationRecord)
            .filter(CalibrationRecord.response_id.in_(response_ids))
            .order_by(CalibrationRecord.created_at.desc())
            .all()
        )
        return {
            "courses": [row_dict(course, "courses")],
            "topics": [row_dict(t, "topics") for t in topics],
            "students": [row_dict(s, "students") for s in students],
            "responses": [row_dict(r, "responses") for r in responses],
            "tags": [row_dict(t, "tags") for t in tags],
            "calibrations": [row_dict(c, "calibrations") for c in calibrations],
        }
    finally:
        db.close()


def main() -> None:
    default_out = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..",
        "frontend",
        "src",
        "demo-data.json",
    )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--course", type=int, default=None, help="course id (default: first)")
    parser.add_argument("--output", default=default_out, help="output json path")
    args = parser.parse_args()

    init_db()
    db = SessionLocal()
    try:
        cid = args.course
        if cid is None:
            first = db.query(Course).order_by(Course.id).first()
            if not first:
                raise SystemExit("no course found - run seed.py first")
            cid = first.id
    finally:
        db.close()

    data = dump_course(cid)
    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    print(f"Exported course {cid} -> {os.path.abspath(args.output)} ({os.path.getsize(args.output)} bytes)")


if __name__ == "__main__":
    main()
