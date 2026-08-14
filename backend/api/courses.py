"""Course / topic / student / response listing endpoints."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from database import Course, DebateTopic, Student, StudentResponse, get_db
from schemas import (
    CourseCreate, CourseOut, DebateTopicCreate, DebateTopicOut,
    DebateTopicUpdate, StudentBatchCreate, StudentCreate, StudentOut,
    StudentResponseOut, StudentUpdate,
)

from . import state


router = APIRouter(tags=["courses"])

@router.get("/api/courses", response_model=list[CourseOut])
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

@router.get("/api/courses/{cid}", response_model=CourseOut)
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

@router.post("/api/courses", response_model=CourseOut)
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

@router.get("/api/courses/{cid}/topics", response_model=list[DebateTopicOut])
def list_topics(cid: int, db: Session = Depends(get_db)):
    topics = db.query(DebateTopic).filter(DebateTopic.course_id == cid).order_by(DebateTopic.order).all()
    return topics

@router.post("/api/courses/{cid}/topics", response_model=DebateTopicOut)
def create_topic(cid: int, body: DebateTopicCreate, db: Session = Depends(get_db)):
    if not db.query(Course).get(cid):
        raise HTTPException(404, "Course not found")
    max_order = db.query(func.max(DebateTopic.order)).filter(DebateTopic.course_id == cid).scalar() or 0
    t = DebateTopic(course_id=cid, order=max_order + 1, **body.model_dump())
    db.add(t)
    db.commit()
    db.refresh(t)
    return t

@router.put("/api/topics/{tid}", response_model=DebateTopicOut)
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

@router.delete("/api/topics/{tid}")
def delete_topic(tid: int, db: Session = Depends(get_db)):
    t = db.query(DebateTopic).get(tid)
    if not t:
        raise HTTPException(404, "Topic not found")
    db.delete(t)
    db.commit()
    return {"ok": True, "topic_id": tid}

def _student_out(student: Student) -> StudentOut:
    return StudentOut(
        id=student.id,
        name=student.name,
        grade=student.grade,
        course_id=student.course_id,
        cognitive_tier=student.cognitive_tier,
        comment_draft=student.comment_draft or "",
        phone=student.phone or "",
        feishu_open_id=student.feishu_open_id or "",
        comment_delivery_status=student.comment_delivery_status or "not_sent",
        comment_delivery_error=student.comment_delivery_error or "",
        comment_delivered_at=student.comment_delivered_at,
    )

@router.get("/api/courses/{cid}/students", response_model=list[StudentOut])
def list_students(cid: int, db: Session = Depends(get_db)):
    students = db.query(Student).filter(Student.course_id == cid).all()
    return [_student_out(student) for student in students]

@router.post("/api/courses/{cid}/students", response_model=StudentOut)
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
        phone=(body.phone or "").strip(),
        feishu_open_id=feishu_open_id,
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    return _student_out(s)

@router.post("/api/courses/{cid}/students/batch", response_model=dict)
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
            phone=(item.phone or "").strip(),
            feishu_open_id=feishu_open_id,
        )
        db.add(st)
        db.flush()
        created.append(_student_out(st))
    db.commit()
    return {"created": created, "skipped": skipped}

@router.put("/api/students/{sid}", response_model=StudentOut)
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
            state._reset_comment_delivery(s)
    if body.phone is not None:
        s.phone = body.phone.strip()
    db.commit()
    db.refresh(s)
    return _student_out(s)

@router.delete("/api/students/{sid}")
def delete_student(sid: int, db: Session = Depends(get_db)):
    s = db.query(Student).get(sid)
    if not s:
        raise HTTPException(404, "Student not found")
    db.delete(s)
    db.commit()
    return {"ok": True, "student_id": sid}

@router.get("/api/courses/{cid}/responses", response_model=list[StudentResponseOut])
def list_responses(cid: int, student_id: Optional[int] = None, db: Session = Depends(get_db)):
    q = db.query(StudentResponse).join(Student).filter(Student.course_id == cid)
    if student_id:
        q = q.filter(StudentResponse.student_id == student_id)
    return q.all()

@router.get("/api/responses/{rid}", response_model=StudentResponseOut)
def get_response(rid: int, db: Session = Depends(get_db)):
    resp = db.query(StudentResponse).get(rid)
    if not resp:
        raise HTTPException(404, "Response not found")
    return resp
