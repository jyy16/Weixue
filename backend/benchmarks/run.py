"""CLI entry: run benchmark phases against a temporary server + dedicated DB."""

from __future__ import annotations

import argparse
import time
from datetime import datetime
from pathlib import Path

from .assess import run_assess
from .comments import run_comments
from .common import (
    BACKEND_DIR,
    DEFAULT_DB,
    DEFAULT_OUT_DIR,
    BenchServer,
    cleanup_run,
    ensure_env,
    env_stamp,
    export_teacher_references,
    seed_db,
    write_report,
)
from .accuracy import run_accuracy
from .calibration import run_calibration
from .transcribe import run_transcribe

COMMANDS = {
    "all": "完整基准：评语 → 转写 → 批量评估(含质量/成本) → 汇总报告",
    "transcribe": "仅语音转写（seed → 上传真实音频计时）",
    "comments": "仅评语生成（seed 已批改态 → 逐学生 + 批量）",
    "assess": "仅批量评估（seed → 补齐 27 条 → reset → assess → 质量/成本）",
    "accuracy": "转写准确率：语音集 18 条 + 示例音频 vs 参考文字稿（真实 ASR 调用）",
    "calibration": "校准记忆 A/B：无校准 vs 注入校准记录（9 条，两次评估）",
}


def _add_common(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--port", type=int, default=8788, help="临时 uvicorn 端口（默认 8788）")
    sp.add_argument(
        "--db", default=str(DEFAULT_DB),
        help="专用基准 SQLite 路径（默认 backend/data/benchmark.db，*.db 已被 gitignore）",
    )
    sp.add_argument("--out", default=str(DEFAULT_OUT_DIR), help="报告输出目录（默认 tmp/benchmark）")
    sp.add_argument("--clean", action="store_true", help="结束后删除基准库与本次上传音频")
    sp.add_argument("--timeout", type=float, default=2400.0, help="批量评估轮询超时秒数（默认 2400）")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="benchmark",
        description="维学思辨星性能基准。先用测试 API 更新 backend/.env"
                    "（LLM_BASE_URL / LLM_API_KEY / LLM_MODEL 等），再运行。",
    )
    sub = parser.add_subparsers(dest="cmd", required=True, metavar="{" + ",".join(COMMANDS) + "}")
    for name, help_ in COMMANDS.items():
        sp = sub.add_parser(name, help=help_)
        if name == "accuracy":
            sp.add_argument(
                "--dir",
                default=str(BACKEND_DIR / "data" / "sample"),
                help="语音集目录（默认 backend/data/sample）",
            )
            sp.add_argument("--limit", type=int, default=None, help="只测前 N 条（省额度）")
            sp.add_argument("--dry-run", action="store_true", help="只列配对，不发起 ASR 调用")
            sp.add_argument("--out", default=str(DEFAULT_OUT_DIR), help="报告输出目录")
        else:
            _add_common(sp)

    sub.choices["all"].add_argument("--skip-comments", action="store_true")
    sub.choices["all"].add_argument("--skip-transcribe", action="store_true")
    sub.choices["all"].add_argument("--skip-assess", action="store_true")
    sub.choices["transcribe"].add_argument(
        "--pure-asr", action="store_true",
        help="额外直接调用 ASRClient 计时纯转写耗时（多一次 ASR 调用）",
    )
    sub.choices["transcribe"].add_argument(
        "--allow-mock", action="store_true",
        help="ASR 为 mock 时也强制计时（数字无意义，慎用）",
    )
    sub.choices["transcribe"].add_argument(
        "--audio", default=None,
        help="用于转写计时的音频路径（默认 backend/data/sample_class_audio.wav；"
             "可用归一化后的 backend/data/sample/*.16k.wav）",
    )
    return parser


def main(argv=None) -> int:
    ensure_env()
    args = build_parser().parse_args(argv)
    if args.cmd == "accuracy":
        parts = {
            "meta": env_stamp(),
            "results": {
                "accuracy": run_accuracy(
                    sample_dir=Path(args.dir),
                    limit=args.limit,
                    dry_run=args.dry_run,
                )
            },
        }
        base = write_report(parts, Path(args.out), "accuracy")
        _print_summary(parts, base)
        return 0

    db_path = Path(args.db)
    out_dir = Path(args.out)
    server = BenchServer(args.port, db_path, out_dir)
    parts = {"meta": env_stamp(), "results": {}}
    t_start = time.perf_counter()

    try:
        _log(f"重建基准库（seed）→ {db_path}")
        seed_db(db_path)
        _log("基准库就绪，启动临时服务 …")
        with server:
            # Export teacher references immediately after seeding, before any
            # phase touches review state (comments/transcribe never touch the
            # 9 reviewed cells, but exporting first keeps the invariant clear).
            _log(f"服务就绪 {server.base_url}，导出教师参照 …")
            refs = export_teacher_references(db_path)
            _log(f"教师参照 {len(refs)} 条")

            if args.cmd in ("all", "comments") and not (
                args.cmd == "all" and args.skip_comments
            ):
                _log("阶段 1/3：评语生成（逐学生 9 次 + 批量 1 次）…")
                parts["results"]["comments"] = run_comments(server.base_url)
                _log("评语生成完成")

            if args.cmd in ("all", "transcribe") and not (
                args.cmd == "all" and args.skip_transcribe
            ):
                _log("阶段 2/3：语音转写计时（45s 示例音频）…")
                parts["results"]["transcribe"] = run_transcribe(
                    server.base_url,
                    allow_mock=getattr(args, "allow_mock", False),
                    pure_asr=getattr(args, "pure_asr", False),
                    audio=getattr(args, "audio", None),
                )
                _log("语音转写完成")

            if args.cmd in ("all", "assess") and not (
                args.cmd == "all" and args.skip_assess
            ):
                _log("阶段 3/3：批量评估 27 条（补齐文字稿 → reset → assess）…")
                parts["results"]["assess"] = run_assess(
                    server.base_url, db_path, refs=refs, timeout=args.timeout
                )
                _log("批量评估完成")

            if args.cmd == "calibration":
                _log("校准 A/B：第 1 轮无校准 → 注入校准记录 → 第 2 轮重跑 …")
                parts["results"]["calibration"] = run_calibration(
                    server.base_url, db_path, refs=refs, timeout=args.timeout
                )
                _log("校准 A/B 完成")

        base = write_report(parts, out_dir, f"bench-{args.cmd}")
        _print_summary(parts, base)
        _log(f"总耗时 {time.perf_counter() - t_start:.0f}s，报告：{base}.md / {base}.json")
        return 0
    finally:
        if getattr(args, "clean", False):
            cleanup_run(db_path)


def _log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def _print_summary(parts: dict, base: Path) -> None:
    meta = parts.get("meta", {})
    print("\n===== 基准结果摘要 =====")
    print(f"模型: {meta.get('llm_provider')} / {meta.get('llm_model')}  ({meta.get('llm_base_url')})")
    for key, section in parts.get("results", {}).items():
        d = section.get("data", {})
        if key == "transcribe":
            print(
                f"[转写] e2e {d.get('e2e_sec')}s | RTF {d.get('rtf')}× | "
                f"{d.get('chars')} 字"
            )
        elif key == "comments":
            print(
                f"[评语] 逐学生 {d.get('students')} 条共 {d.get('total_sec')}s"
                f"（{d.get('avg_sec')}s/条）| 批量 {d.get('batch_sec')}s"
            )
        elif key == "assess":
            print(
                f"[评估] {d.get('completed')}/{d.get('total')} 完成，"
                f"errors={d.get('errors')}，共 {d.get('total_sec')}s"
                f"（{d.get('avg_sec')}s/条）"
            )
            q = d.get("quality") or {}
            if q.get("compared"):
                print(
                    f"[质量] AI=教师 整体完全一致 {q.get('exact_all')}/{q.get('compared')}"
                    f"，至少一维一致 {q.get('any_match')}/{q.get('compared')}"
                )
        elif key == "accuracy":
            print(
                f"[准确率] {d.get('files')} 条，平均一致率（去标点）"
                f"{d.get('aggregate', {}).get('mean', 0):.1%}"
            )
        elif key == "calibration":
            a = d.get("run_a", {}).get("quality", {})
            b = d.get("run_b", {}).get("quality", {})
            print(
                f"[校准] 无校准 完全一致 {a.get('exact_all')}/{a.get('compared')} "
                f"-> 校准记忆 {b.get('exact_all')}/{b.get('compared')}；"
                f"45维一致 {a.get('dims_exact')}/{a.get('dims_total')} "
                f"-> {b.get('dims_exact')}/{b.get('dims_total')}"
            )
    print(f"报告: {base}.md  /  {base}.json")
