"""ASR accuracy benchmark: machine transcripts vs human reference texts.

Compares real qwen_asr output against the teammate voice-set reference
transcripts (backend/data/sample/*.16k.wav + matching *.txt) and, when the
benchmark DB exists, the built-in sample_class_audio.wav against
audio_utils.SAMPLE_SCRIPT (already transcribed during the `all` run).

Metric: character-level agreement via difflib, reported both with and without
punctuation (the primary figure is 去标点后的逐字一致率).

Usage:
    python backend/benchmark.py accuracy [--dir backend/data/sample]
                                         [--limit N] [--dry-run]
"""

from __future__ import annotations

import asyncio
import difflib
import re
import sys
import time
from pathlib import Path

from .common import (
    BACKEND_DIR,
    SAMPLE_AUDIO,
    open_db,
    stats,
    wav_duration,
)
from .sample_data import NAME_RE, SAMPLE_DIR

AUDIO_EXTS = {".wav", ".mp3", ".m4a", ".aac", ".ogg", ".webm", ".flac", ".amr"}


def strip_punct(text: str) -> str:
    return re.sub(
        r"[\s，。、；：？！“”‘’「」（）【】—…,.!?;:()]",
        "",
        text or "",
    )


def char_agreement(ref: str, hyp: str, normalize_punct: bool) -> float:
    if normalize_punct:
        ref, hyp = strip_punct(ref), strip_punct(hyp)
    if not ref:
        return 0.0
    return difflib.SequenceMatcher(None, ref, hyp).ratio()


def find_pairs(sample_dir: Path) -> list[tuple[Path, str]]:
    """Pair every audio file with its same-stem *.txt reference.

    Naming is free-form: ``foo.mp3`` ↔ ``foo.txt``. The normalized 16k wav is
    preferred when both ``foo.wav`` and ``foo.16k.wav`` exist for one reference.
    """
    refs = {
        p.name[:-4]: p.read_text(encoding="utf-8").strip()
        for p in sample_dir.rglob("*.txt")
    }

    by_base: dict[str, list[Path]] = {}
    for w in sample_dir.rglob("*"):
        if not w.is_file() or w.suffix.lower() not in AUDIO_EXTS:
            continue
        base = w.name[: -len(w.suffix)]
        if base.endswith(".16k"):
            base = base[:-4]
        by_base.setdefault(base, []).append(w)

    pairs = []
    for base, candidates in sorted(by_base.items()):
        ref = refs.get(base)
        if not ref:
            continue
        wav = next(
            (c for c in candidates if c.name.endswith(".16k.wav")),
            candidates[0],
        )
        pairs.append((wav, ref))
    return pairs


def _transcribe(path: Path) -> str:
    if str(BACKEND_DIR) not in sys.path:
        sys.path.insert(0, str(BACKEND_DIR))
    from asr import ASRClient

    return asyncio.run(ASRClient(provider="qwen_asr").transcribe(str(path)))


def _sample_audio_case() -> dict | None:
    """Free case: reuse the transcript from the `all` run (benchmark.db)."""
    db_path = BACKEND_DIR / "data" / "benchmark.db"
    if not db_path.exists():
        return None
    db = open_db(db_path)
    try:
        from database import StudentResponse

        row = db.query(StudentResponse).filter(StudentResponse.source == "audio").first()
        if not row or not row.raw_text:
            return None
        from audio_utils import SAMPLE_SCRIPT

        return {
            "file": SAMPLE_AUDIO.name,
            "audio_s": round(wav_duration(SAMPLE_AUDIO), 2),
            "ref_chars": len(strip_punct(SAMPLE_SCRIPT)),
            "hyp_chars": len(strip_punct(row.raw_text)),
            "agree_no_punct": round(char_agreement(SAMPLE_SCRIPT, row.raw_text, True), 4),
            "agree_raw": round(char_agreement(SAMPLE_SCRIPT, row.raw_text, False), 4),
            "asr_sec": None,
            "ref": SAMPLE_SCRIPT,
            "hyp": row.raw_text,
        }
    finally:
        db.close()


def _duration(path: Path) -> float | None:
    if path.suffix.lower() != ".wav":
        return None  # mp3/m4a 等无 ffprobe，不探测时长
    try:
        return wav_duration(path)
    except Exception:
        return None


def run_accuracy(
    sample_dir: Path = SAMPLE_DIR,
    limit: int | None = None,
    dry_run: bool = False,
) -> dict:
    pairs = find_pairs(sample_dir)
    if limit:
        pairs = pairs[:limit]
    if not pairs:
        raise RuntimeError(f"{sample_dir} 下没有可配对的 *.16k.wav + *.txt")

    rows = []
    total_audio_s = 0.0
    total_asr_s = 0.0
    for wav, ref in pairs:
        dur = _duration(wav)
        if dur:
            total_audio_s += dur
        row = {
            "file": wav.name,
            "audio_s": round(dur, 2) if dur else None,
            "ref_chars": len(strip_punct(ref)),
            "hyp_chars": None,
            "agree_no_punct": None,
            "agree_raw": None,
            "asr_sec": None,
        }
        if not dry_run:
            t0 = time.perf_counter()
            hyp = _transcribe(wav)
            asr_sec = time.perf_counter() - t0
            total_asr_s += asr_sec
            row.update(
                {
                    "hyp_chars": len(strip_punct(hyp)),
                    "agree_no_punct": round(char_agreement(ref, hyp, True), 4),
                    "agree_raw": round(char_agreement(ref, hyp, False), 4),
                    "asr_sec": round(asr_sec, 2),
                    "ref": ref,
                    "hyp": hyp,
                }
            )
        rows.append(row)

    sample_case = None if dry_run else _sample_audio_case()
    if sample_case:
        rows.append(sample_case)

    agree_vals = [r["agree_no_punct"] for r in rows if r["agree_no_punct"] is not None]
    agg = stats(agree_vals) if agree_vals else {"n": 0}

    tables = [
        {
            "caption": "逐文件转写准确率（qwen3-asr-flash）",
            "headers": ["文件", "音频(秒)", "参考字数", "转写字数", "一致率(去标点)", "一致率(含标点)", "ASR耗时(秒)"],
            "rows": [
                [
                    r["file"],
                    r["audio_s"] if r["audio_s"] is not None else "-",
                    r["ref_chars"],
                    r["hyp_chars"] if r["hyp_chars"] is not None else "-",
                    f"{r['agree_no_punct']:.1%}" if r["agree_no_punct"] is not None else "-",
                    f"{r['agree_raw']:.1%}" if r["agree_raw"] is not None else "-",
                    r["asr_sec"] if r["asr_sec"] is not None else "-",
                ]
                for r in rows
            ],
        },
        {
            "caption": "汇总",
            "headers": ["指标", "值"],
            "rows": [
                ["文件数", len(rows)],
                ["音频总时长（秒）", f"{total_audio_s:.1f}"],
                ["ASR 总耗时（秒）", f"{total_asr_s:.1f}" if not dry_run else "-"],
                ["平均一致率（去标点）", f"{agg.get('mean', 0):.1%}" if agg.get("n") else "-"],
                ["中位一致率（去标点）", f"{agg.get('median', 0):.1%}" if agg.get("n") else "-"],
                ["最低一致率（去标点）", f"{agg.get('min', 0):.1%}" if agg.get("n") else "-"],
            ],
        },
    ]
    notes = [
        "口径：字符级一致率（difflib），主指标为去标点后的逐字一致率；含标点为参考。",
        "覆盖场景：在线课堂单生独立录音（干净单说话人）。语音集与豆包儿童样本均为合成语音，"
        "覆盖儿童音色/发音差异；真实家庭环境杂音未覆盖，如需可后续补真实录音。",
    ]
    if dry_run:
        notes.append("--dry-run：仅列出配对与预计调用数，未发起 ASR 调用。")

    return {
        "title": "转写准确率",
        "notes": notes,
        "tables": tables,
        "data": {
            "dry_run": dry_run,
            "files": len(rows),
            "total_audio_s": round(total_audio_s, 1),
            "total_asr_s": round(total_asr_s, 1),
            "aggregate": agg,
            "rows": rows,
        },
    }
