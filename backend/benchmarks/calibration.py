"""A/B calibration benchmark: cold start vs teacher calibration memory.

Runs the same 9 teacher-reviewed responses through assessment twice:
  Run A: no calibration records (cold start)
  Run B: after injecting curated calibration records built from Run A's
         observed AI->teacher mismatches (simulated teacher corrections)

Compares AI-vs-teacher agreement between the two runs to show whether the
calibration memory pulls the AI draft toward the teacher's scale.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta

import httpx

from .common import DIMENSIONS, open_db, read_responses

LEVEL = {"A+": 0, "A": 1, "A-": 2, "B+": 3, "B": 4, "B-": 5}
DIM_LABELS = {
    "position": "立意",
    "material": "选材",
    "structure": "结构",
    "language": "语言",
    "perspective": "视角",
}


def run_calibration(
    base_url: str,
    db_path,
    refs: list[dict],
    timeout: float = 2400.0,
) -> dict:
    refs_by_id = {r["response_id"]: r for r in refs}

    with httpx.Client(timeout=300) as client:
        _wipe_calibration(db_path)
        print("  第 1 轮（无校准 / 冷启动）：reset -> assess 9 条 …", flush=True)
        run_a = _assess_round(client, base_url, db_path, timeout)
        rows_a = read_responses(db_path)
        quality_a = _quality_report(refs, {r["response_id"]: r for r in rows_a})

        curated = _build_curated_records(refs, rows_a, max_records=5)
        _insert_calibration(db_path, curated)
        print(
            f"  已注入 {len(curated)} 条校准记录（取自第 1 轮观测偏差）",
            flush=True,
        )
        print("  第 2 轮（校准记忆）：reset -> assess 9 条 …", flush=True)
        run_b = _assess_round(client, base_url, db_path, timeout)
        rows_b = read_responses(db_path)
        quality_b = _quality_report(refs, {r["response_id"]: r for r in rows_b})

    qa, qb = quality_a, quality_b
    tables = [
        {
            "caption": "校准 A/B（9 条作答，同一教师参照）",
            "headers": ["指标", "A 无校准", "B 校准记忆"],
            "rows": [
                [
                    "9 条整体完全一致",
                    f"{qa['exact_all']}/{qa['compared']}",
                    f"{qb['exact_all']}/{qb['compared']}",
                ],
                [
                    "45 维完全一致",
                    f"{qa['dims_exact']}/{qa['dims_total']}"
                    f"（{qa['dims_exact'] / qa['dims_total']:.0%}）",
                    f"{qb['dims_exact']}/{qb['dims_total']}"
                    f"（{qb['dims_exact'] / qb['dims_total']:.0%}）",
                ],
                [
                    "≤1 级占比（45 维）",
                    f"{qa['within1']}/{qa['dims_total']}"
                    f"（{qa['within1'] / qa['dims_total']:.0%}）",
                    f"{qb['within1']}/{qb['dims_total']}"
                    f"（{qb['within1'] / qb['dims_total']:.0%}）",
                ],
                ["AI 偏高次数", qa["ai_higher"], qb["ai_higher"]],
                ["AI 偏低次数", qa["ai_lower"], qb["ai_lower"]],
                ["单轮总耗时（秒）", f"{run_a['total_sec']:.1f}", f"{run_b['total_sec']:.1f}"],
                ["错误/跳过", f"{run_a['errors']}/{run_a['skipped']}", f"{run_b['errors']}/{run_b['skipped']}"],
            ],
        },
        {
            "caption": "分维度一致率（45 维）",
            "headers": ["维度", "A 无校准", "B 校准记忆"],
            "rows": [
                [
                    DIM_LABELS.get(d, d),
                    f"{qa['per_dim'][d]['match']}/{qa['per_dim'][d]['total']}"
                    f"（{qa['per_dim'][d]['match'] / qa['per_dim'][d]['total']:.0%}）",
                    f"{qb['per_dim'][d]['match']}/{qb['per_dim'][d]['total']}"
                    f"（{qb['per_dim'][d]['match'] / qb['per_dim'][d]['total']:.0%}）",
                ]
                for d in DIMENSIONS
            ],
        },
        {
            "caption": "注入的校准记录（模拟教师修正）",
            "headers": ["学生", "AI 初评", "教师终评", "修正维度数"],
            "rows": [
                [
                    refs_by_id[c["response_id"]]["student_name"],
                    _fmt_scores(c["ai_original_scores"]),
                    _fmt_scores(c["teacher_final_scores"]),
                    len(c["modifications"]),
                ]
                for c in curated
            ],
        },
    ]
    notes = [
        "口径：同 9 条教师已批改作答，deepseek-v4-flash，同日两次评估；参照为团队演示教师终评（模拟教师）。",
        "校准记录来自第 1 轮实际观测的 AI->教师偏差（AI 初评 -> 教师终评 + 理由），模拟教师逐条修正后的沉淀。",
        "本实验证明校准机制会响应教师修正（few-shot 收敛），不是真实教师验证结论。",
    ]
    return {
        "title": "校准记忆 A/B",
        "notes": notes,
        "tables": tables,
        "data": {
            "run_a": {"sec": run_a["total_sec"], "quality": quality_a},
            "run_b": {"sec": run_b["total_sec"], "quality": quality_b},
            "calibration_records": curated,
        },
    }


def _assess_round(
    client: httpx.Client,
    base_url: str,
    db_path,
    timeout: float,
) -> dict:
    reset = client.post(f"{base_url}/api/courses/1/reset")
    if reset.status_code != 200:
        raise RuntimeError(f"reset 失败 HTTP {reset.status_code}: {reset.text[:300]}")
    started = client.post(f"{base_url}/api/courses/1/assess")
    if started.status_code != 200:
        raise RuntimeError(f"assess 启动失败 HTTP {started.status_code}: {started.text[:300]}")
    total = started.json().get("total") or 0
    if total != 9:
        raise RuntimeError(f"期望 9 条，实际 {total} 条")

    t0 = time.perf_counter()
    last = 0
    final = None
    while True:
        elapsed = time.perf_counter() - t0
        prog = client.get(f"{base_url}/api/courses/1/assessment-progress").json()
        completed = prog.get("completed") or 0
        if completed != last:
            last = completed
            if completed % 3 == 0 or completed >= total:
                print(f"    进度 {completed}/{total}（{elapsed:.0f}s）", flush=True)
        if not prog.get("active") and completed >= total:
            final = prog
            break
        if elapsed > timeout:
            raise RuntimeError(f"评估超时（>{timeout:.0f}s），已完成 {completed}/{total}")
        time.sleep(0.5)
    return {
        "total_sec": round(time.perf_counter() - t0, 3),
        "errors": final.get("errors") or 0,
        "skipped": final.get("skipped") or 0,
    }


def _wipe_calibration(db_path) -> None:
    db = open_db(db_path)
    try:
        from database import CalibrationRecord

        db.query(CalibrationRecord).delete()
        db.commit()
    finally:
        db.close()
        db.get_bind().dispose()


def _insert_calibration(db_path, records: list[dict]) -> None:
    db = open_db(db_path)
    try:
        from database import CalibrationRecord

        for i, rec in enumerate(records):
            db.add(
                CalibrationRecord(
                    response_id=rec["response_id"],
                    teacher_id="default",
                    ai_original_scores=rec["ai_original_scores"],
                    teacher_final_scores=rec["teacher_final_scores"],
                    modifications=rec["modifications"],
                    note=rec["note"],
                    created_at=datetime.utcnow() + timedelta(seconds=i),  # 控制注入顺序
                )
            )
        db.commit()
    finally:
        db.close()
        db.get_bind().dispose()


def _build_curated_records(
    refs: list[dict],
    rows: list[dict],
    max_records: int = 5,
) -> list[dict]:
    rows_by_id = {r["response_id"]: r for r in rows}
    candidates = []
    for ref in refs:
        row = rows_by_id.get(ref["response_id"])
        if not row:
            continue
        ai = row.get("ai_dimension_scores") or {}
        teacher = ref.get("teacher_dimension_scores") or {}
        mods = []
        for d in DIMENSIONS:
            if d in ai and d in teacher and ai[d] != teacher[d]:
                mods.append(
                    {
                        "dimension": d,
                        "from_rating": ai[d],
                        "to_rating": teacher[d],
                        "reason": _reason(d, ai[d], teacher[d]),
                    }
                )
        if mods:
            candidates.append((ref, ai, teacher, mods))
    candidates.sort(key=lambda c: -len(c[3]))
    out = []
    for ref, ai, teacher, mods in candidates[:max_records]:
        out.append(
            {
                "response_id": ref["response_id"],
                "ai_original_scores": ai,
                "teacher_final_scores": teacher,
                "modifications": mods,
                "note": "模拟教师校准：按本机构口径复核后修正（A/B 演示）。",
            }
        )
    return out


def _reason(dim: str, ai: str, teacher: str) -> str:
    label = DIM_LABELS.get(dim, dim)
    direction = "偏高" if LEVEL.get(ai, 3) < LEVEL.get(teacher, 3) else "偏低"
    return f"教师复核：{label}维度 AI 初评{ai}{direction}，学生表现更符合{teacher}级描述，按机构口径修正"


def _fmt_scores(scores: dict) -> str:
    return "、".join(
        f"{DIM_LABELS.get(d, d)}{v}" for d, v in (scores or {}).items()
    ) or "无"


def _quality_report(refs: list[dict], rows_by_id: dict[int, dict]) -> dict:
    per_dim = {d: {"match": 0, "total": 0} for d in DIMENSIONS}
    exact_all = 0
    dims_exact = 0
    dims_total = 0
    within1 = 0
    ai_higher = 0
    ai_lower = 0
    compared = 0
    for ref in refs:
        row = rows_by_id.get(ref["response_id"])
        if not row:
            continue
        ai = row.get("ai_dimension_scores") or {}
        teacher = ref.get("teacher_dimension_scores") or {}
        if not ai:
            continue
        compared += 1
        hits = []
        for d in DIMENSIONS:
            if d not in teacher or d not in ai:
                continue
            per_dim[d]["total"] += 1
            dims_total += 1
            if teacher[d] == ai[d]:
                per_dim[d]["match"] += 1
                dims_exact += 1
                hits.append(True)
            else:
                hits.append(False)
                diff = abs(LEVEL.get(ai[d], 3) - LEVEL.get(teacher[d], 3))
                if diff <= 1:
                    within1 += 1
                if LEVEL.get(ai[d], 3) < LEVEL.get(teacher[d], 3):
                    ai_higher += 1
                else:
                    ai_lower += 1
        if hits and all(hits):
            exact_all += 1
    return {
        "compared": compared,
        "exact_all": exact_all,
        "dims_exact": dims_exact,
        "dims_total": dims_total,
        "within1": within1,
        "ai_higher": ai_higher,
        "ai_lower": ai_lower,
        "per_dim": per_dim,
    }
