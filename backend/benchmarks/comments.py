"""Comment-draft generation benchmark (per-student POSTs + batch endpoint)."""

from __future__ import annotations

import time

import httpx

from .common import count_chars, estimate_comment_tokens, stats

TEMPLATE_OPENINGS = ("在本次课程中", "本次课程中", "本课程中", "在这学期中")
PLACEHOLDER_MARK = "尚无教师批改记录"


def _compliance(draft: str) -> dict:
    chars = count_chars(draft)
    return {
        "chars": chars,
        "in_band": 150 <= chars <= 250,
        "template_open": draft.startswith(TEMPLATE_OPENINGS),
        "has_you": "你" in draft,
        "placeholder": PLACEHOLDER_MARK in draft,
    }


def run_comments(base_url: str) -> dict:
    with httpx.Client(timeout=300) as client:
        students = client.get(f"{base_url}/api/courses/1/students").json()
        if not students:
            raise RuntimeError("课程 1 没有学生，无法测评语生成")

        per_student = []
        for s in students:
            t0 = time.perf_counter()
            resp = client.post(
                f"{base_url}/api/courses/1/comments", json={"student_id": s["id"]}
            )
            sec = time.perf_counter() - t0
            if resp.status_code != 200:
                raise RuntimeError(
                    f"评语生成失败（学生 {s['id']}）HTTP {resp.status_code}: {resp.text[:300]}"
                )
            draft = resp.json().get("draft") or ""
            per_student.append(
                {
                    "student_id": s["id"],
                    "student_name": s.get("name", ""),
                    "sec": round(sec, 3),
                    "draft": draft,
                    **_compliance(draft),
                }
            )

        t0 = time.perf_counter()
        resp = client.post(f"{base_url}/api/courses/1/comments/batch")
        batch_sec = time.perf_counter() - t0
        if resp.status_code != 200:
            raise RuntimeError(f"批量评语失败 HTTP {resp.status_code}: {resp.text[:300]}")
        batch_results = resp.json().get("results") or []
        batch_drafts = [r.get("draft") or "" for r in batch_results]
        batch_ok = [r for r in batch_results if not r.get("error") and r.get("draft")]

        sec_stats = stats(r["sec"] for r in per_student)
        total_sec = sum(r["sec"] for r in per_student)
        avg_sec = total_sec / len(per_student) if per_student else 0.0
        batch_per_item = batch_sec / len(batch_ok) if batch_ok else None
        in_band = sum(1 for r in per_student if r["in_band"])
        template_open = sum(1 for r in per_student if r["template_open"])
        placeholders = sum(1 for r in per_student if r["placeholder"])

        rows = [
            [
                r["student_name"],
                r["student_id"],
                f"{r['sec']:.2f}",
                r["chars"],
                "是" if r["in_band"] else "否",
                "是" if r["template_open"] else "否",
                "是" if r["has_you"] else "否",
            ]
            for r in per_student
        ]
        tables = [
            {
                "caption": "逐学生评语生成（POST /comments）",
                "headers": ["学生", "ID", "耗时(秒)", "字数", "150–250合规", "模板化开头", "含'你'"],
                "rows": rows,
            },
            {
                "caption": "汇总",
                "headers": ["指标", "值"],
                "rows": [
                    ["学生数", len(per_student)],
                    ["总耗时（秒）", f"{total_sec:.2f}"],
                    ["平均（秒/条）", f"{avg_sec:.2f}"],
                    ["单条分布 min/中位/max", f"{sec_stats['min']} / {sec_stats['median']} / {sec_stats['max']}"],
                    ["批量接口总耗时（秒）", f"{batch_sec:.2f}"],
                    ["批量折算（秒/条）", f"{batch_per_item:.2f}" if batch_per_item else "N/A"],
                    ["字数合规 150–250", f"{in_band}/{len(per_student)}"],
                    ["模板化开头", f"{template_open}/{len(per_student)}"],
                    ["占位（无批改记录）", f"{placeholders}/{len(per_student)}"],
                ],
            },
        ]

        cost = estimate_comment_tokens([r["draft"] for r in per_student])
        tables.append(
            {
                "caption": "评语成本粗估（9 条）",
                "headers": ["项目", "值"],
                "rows": [
                    ["输入字符（粗估）", cost["input_chars_est"]],
                    ["输出字符（粗估）", cost["output_chars_est"]],
                    ["token 粗估", cost["tokens_est"]],
                    ["口径", cost["note"]],
                ],
            }
        )

    notes = [
        "前置状态：seed 数据中每位学生至少 1 题已由教师批改（评语生成的前提）。",
        "逐学生与批量是两种调用方式，各产生 9 次 LLM 调用；批量接口内部为串行循环。",
        "字数 150–250 为目标区间；模板化开头与占位为质量检查项，非硬性失败。",
    ]

    return {
        "title": "评语生成",
        "notes": notes,
        "tables": tables,
        "data": {
            "students": len(per_student),
            "total_sec": round(total_sec, 3),
            "avg_sec": round(avg_sec, 3),
            "per_student_sec": sec_stats,
            "batch_sec": round(batch_sec, 3),
            "batch_per_item_sec": round(batch_per_item, 3) if batch_per_item else None,
            "in_band": in_band,
            "template_open": template_open,
            "placeholders": placeholders,
            "cost_estimate": cost,
            "per_student": per_student,
            "batch_drafts": batch_drafts,
        },
    }

