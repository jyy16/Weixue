"""Batch assessment benchmark.

Brings the benchmark DB to 9 students x 3 topics = 27 responses, resets the
course, runs POST /assess, polls assessment-progress to completion, then
computes AI-vs-teacher score agreement and a rough token estimate.
"""

from __future__ import annotations

import time

import httpx

from .common import (
    DIMENSIONS,
    estimate_assess_tokens,
    export_teacher_references,
    read_responses,
    stats,
)
from .sample_data import load_transcripts

EXPECTED_RESPONSES = 27


def run_assess(
    base_url: str,
    db_path,
    refs: list[dict] | None = None,
    timeout: float = 2400.0,
) -> dict:
    refs = refs if refs is not None else export_teacher_references(db_path)
    refs_by_id = {r["response_id"]: r for r in refs}

    with httpx.Client(timeout=300) as client:
        added = _ensure_27(client, base_url)

        reset = client.post(f"{base_url}/api/courses/1/reset")
        if reset.status_code != 200:
            raise RuntimeError(f"reset 失败 HTTP {reset.status_code}: {reset.text[:300]}")

        started = client.post(f"{base_url}/api/courses/1/assess")
        if started.status_code != 200:
            raise RuntimeError(f"assess 启动失败 HTTP {started.status_code}: {started.text[:300]}")
        start_payload = started.json()
        total = start_payload.get("total") or 0
        if total != EXPECTED_RESPONSES:
            raise RuntimeError(
                f"期望评估 {EXPECTED_RESPONSES} 条，实际 {total} 条"
                "（请确认 seed 与 27 条补齐逻辑，或检查是否有评估进行中）"
            )
        print(f"  评估已启动：{total} 条，轮询进度中（约 5–20s/条）…", flush=True)

        t0 = time.perf_counter()
        progress_log = [(0.0, 0)]
        last_completed = 0
        final = None
        while True:
            elapsed = time.perf_counter() - t0
            prog = client.get(f"{base_url}/api/courses/1/assessment-progress").json()
            completed = prog.get("completed") or 0
            if completed != last_completed:
                progress_log.append((round(elapsed, 3), completed))
                last_completed = completed
                if completed % 5 == 0 or completed >= total:
                    print(f"  评估进度 {completed}/{total}（已用 {elapsed:.0f}s）", flush=True)
            if not prog.get("active") and completed >= total:
                final = prog
                break
            if elapsed > timeout:
                raise RuntimeError(
                    f"评估超时（>{timeout:.0f}s），已完成 {completed}/{total}。"
                    f"见 uvicorn 日志排查。"
                )
            time.sleep(0.5)
        total_sec = time.perf_counter() - t0

        if progress_log[-1][1] != total:
            progress_log.append((round(total_sec, 3), total))

        item_secs = []
        for (t_prev, c_prev), (t_cur, c_cur) in zip(progress_log, progress_log[1:]):
            inc = c_cur - c_prev
            if inc > 0:
                item_secs.extend([(t_cur - t_prev) / inc] * inc)
        item_stats = stats(item_secs)

        rows = read_responses(db_path)
        rows_by_id = {r["response_id"]: r for r in rows}
        quality = _quality_report(refs, rows_by_id)

        conf_dist: dict[str, int] = {}
        for r in rows:
            conf_dist[r["ai_confidence"]] = conf_dist.get(r["ai_confidence"], 0) + 1
        success = sum(1 for r in rows if r["ai_dimension_scores"])
        errors = final.get("errors") or 0
        llm_calls = final.get("llm_calls") or 0
        skipped = final.get("skipped") or 0
        completed = final.get("completed") or 0

        cost = estimate_assess_tokens(rows)

    notes = [
        f"27 条 = 9 条种子作答 + 语音集文字稿补齐；本轮补建 {added} 条"
        "（backend/data/sample/NN_s*_t*.txt，8/11 压测同一批模拟发言）。",
        "种子作答保持原文（9 条教师已批改，质量对比口径一致）；"
        "all 流程中 1 条（学生1×辩题2）会被真实音频转写结果替换。",
        "评估为串行执行（后台任务逐条调用 LLM），平均秒/条即真实吞吐。",
        "8/11 的 515s≈19s/条 是 qwen-plus 时期的旧数；本轮模型/API 见报告头部，勿混用。",
    ]
    tables = [
        {
            "caption": "批量评估（27 条作答）",
            "headers": ["指标", "值"],
            "rows": [
                ["需要评估条数", total],
                ["完成条数", completed],
                ["成功（含评分）", success],
                ["LLM 调用次数", llm_calls],
                ["错误", errors],
                ["跳过", skipped],
                ["总耗时（秒）", f"{total_sec:.1f}"],
                ["平均（秒/条）", f"{total_sec / total:.1f}" if total else "N/A"],
                ["单条 min/中位/max（秒）", f"{item_stats['min']} / {item_stats['median']} / {item_stats['max']}"],
                ["单条 P95（秒）", item_stats["p95"]],
            ],
        },
        {
            "caption": "AI vs 教师评分一致率（9 条已批改作答）",
            "headers": ["维度", "可比条数", "一致条数", "一致率"],
            "rows": [
                [
                    {"position": "立意", "material": "选材", "structure": "结构",
                     "language": "语言", "perspective": "视角"}.get(d, d),
                    v["total"],
                    v["match"],
                    f"{v['match'] / v['total']:.0%}" if v["total"] else "N/A",
                ]
                for d, v in quality["per_dim"].items()
            ]
            + [
                ["整体五维完全一致", quality["compared"], quality["exact_all"],
                 f"{quality['exact_all'] / quality['compared']:.0%}" if quality["compared"] else "N/A"],
                ["至少一维一致", quality["compared"], quality["any_match"],
                 f"{quality['any_match'] / quality['compared']:.0%}" if quality["compared"] else "N/A"],
            ],
        },
        {
            "caption": "AI 置信度分布（27 条）",
            "headers": ["置信度", "条数"],
            "rows": [[k, v] for k, v in sorted(conf_dist.items())],
        },
        {
            "caption": "评估成本粗估（27 条）",
            "headers": ["项目", "值"],
            "rows": [
                ["输入字符（粗估）", cost["input_chars_est"]],
                ["输出字符（粗估）", cost["output_chars_est"]],
                ["token 粗估", cost["tokens_est"]],
                ["口径", cost["note"]],
            ],
        },
    ]

    return {
        "title": "批量评估",
        "notes": notes,
        "tables": tables,
        "data": {
            "added_27": added,
            "total": total,
            "completed": completed,
            "success": success,
            "llm_calls": llm_calls,
            "errors": errors,
            "skipped": skipped,
            "total_sec": round(total_sec, 3),
            "avg_sec": round(total_sec / total, 3) if total else None,
            "item_sec": item_stats,
            "quality": quality,
            "confidence_dist": conf_dist,
            "cost_estimate": cost,
        },
    }


def _ensure_27(client: httpx.Client, base_url: str) -> int:
    """Fill every missing student x topic cell so the course has 27 responses.

    Texts come from the voice-set transcripts; the 9 seed cells already exist
    in the database. Missing cells fail loudly instead of recycling text.
    """
    students = client.get(f"{base_url}/api/courses/1/students").json()
    topics = client.get(f"{base_url}/api/courses/1/topics").json()
    responses = client.get(f"{base_url}/api/courses/1/responses").json()
    existing = {(r["student_id"], r["topic_id"]) for r in responses}
    transcripts = load_transcripts()

    added = 0
    missing = []
    for st in students:
        for tp in topics:
            cell = (st["id"], tp["id"])
            if cell in existing:
                continue
            text = (transcripts.get(cell) or "").strip()
            if not text:
                missing.append(cell)
                continue
            resp = client.post(
                f"{base_url}/api/courses/1/responses/text",
                json={
                    "student_id": st["id"],
                    "topic_id": tp["id"],
                    "text": text,
                    "source": "manual",
                },
            )
            if resp.status_code != 200:
                raise RuntimeError(f"补建作答失败 HTTP {resp.status_code}: {resp.text[:300]}")
            added += 1
    if missing:
        raise RuntimeError(
            "以下单元格缺少作答文本（语音集文字稿未覆盖，请补写后重试）："
            + ", ".join(f"学生{s}×辩题{t}" for s, t in sorted(missing))
        )
    return added


def _quality_report(refs: list[dict], rows_by_id: dict[int, dict]) -> dict:
    per_dim = {d: {"match": 0, "total": 0} for d in DIMENSIONS}
    compared = 0
    exact_all = 0
    any_match = 0
    mismatches = []

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
            if teacher[d] == ai[d]:
                per_dim[d]["match"] += 1
                hits.append(True)
            else:
                hits.append(False)
                mismatches.append(
                    {
                        "response_id": ref["response_id"],
                        "student_name": ref.get("student_name", ""),
                        "dimension": d,
                        "ai": ai[d],
                        "teacher": teacher[d],
                    }
                )
        if hits and all(hits):
            exact_all += 1
        if any(hits):
            any_match += 1

    return {
        "compared": compared,
        "exact_all": exact_all,
        "any_match": any_match,
        "per_dim": per_dim,
        "mismatches": mismatches[:50],
    }
