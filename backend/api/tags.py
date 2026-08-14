"""Dimension-tag library endpoints."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database import DimensionTag, StudentResponse, get_db
from schemas import TagMerge, TagOut, TagUpdate


router = APIRouter(tags=["tags"])

@router.get("/api/courses/{cid}/tags", response_model=list[TagOut])
def list_tags(cid: int, db: Session = Depends(get_db)):
    tags = db.query(DimensionTag).filter(DimensionTag.course_id == cid).order_by(DimensionTag.use_count.desc()).all()
    return tags

@router.post("/api/courses/{cid}/tags", response_model=TagOut)
def create_tag(cid: int, name: str, source: str = "base", db: Session = Depends(get_db)):
    existing = db.query(DimensionTag).filter(DimensionTag.course_id == cid, DimensionTag.name == name).first()
    if existing:
        return existing
    t = DimensionTag(course_id=cid, name=name, source=source)
    db.add(t)
    db.commit()
    db.refresh(t)
    return t

@router.put("/api/tags/{tid}", response_model=TagOut)
def update_tag(tid: int, body: TagUpdate, db: Session = Depends(get_db)):
    tag = db.query(DimensionTag).get(tid)
    if not tag:
        raise HTTPException(404, "Tag not found")
    if body.name is not None:
        tag.name = body.name
    db.commit()
    db.refresh(tag)
    return tag

@router.post("/api/tags/merge", response_model=TagOut)
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

@router.delete("/api/tags/{tid}")
def delete_tag(tid: int, db: Session = Depends(get_db)):
    tag = db.query(DimensionTag).get(tid)
    if not tag:
        raise HTTPException(404, "Tag not found")
    db.delete(tag)
    db.commit()
    return {"ok": True}
