"""Speech transcription benchmark.

Measures the wall-clock of POST /api/courses/{cid}/audio/import (upload +
ASR + persist), derives the real-time factor (RTF = wall-clock / audio
duration) and reports transcript size. Optionally times the pure ASR call.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import httpx

from .common import BACKEND_DIR, SAMPLE_AUDIO, count_chars, wav_duration

# Import into a cell outside the 9 teacher-reviewed responses so the
# AI-vs-teacher quality comparison keeps an identical text basis.
AUDIO_CELL = {"student_id": 1, "topic_id": 2}


def run_transcribe(
    base_url: str,
    allow_mock: bool = False,
    pure_asr: bool = False,
    audio: str | Path | None = None,
) -> dict:
    audio = Path(audio) if audio else Path(SAMPLE_AUDIO)
    if not audio.exists():
        raise RuntimeError(f"示例音频不存在：{audio}")

    with httpx.Client(timeout=300) as client:
        asr_settings = client.get(f"{base_url}/api/settings/asr").json()
        provider = asr_settings.get("provider") or os.getenv("ASR_PROVIDER") or "mock"
        model = asr_settings.get("model") or os.getenv("ASR_MODEL") or ""
        if provider == "mock" and not allow_mock:
            raise RuntimeError(
                "当前 ASR provider 为 mock（不调用真实服务，耗时无意义）。"
                "请先在 backend/.env 配置 ASR_PROVIDER=qwen_asr（或 openai/dashscope），"
                "或加 --allow-mock 强制计时（数字无意义，慎用）。"
            )

        duration = wav_duration(audio)
        size_mb = audio.stat().st_size / 1024 / 1024

        print(
            f"  转写计时：{audio.name}（{duration:.1f}s / {size_mb:.2f}MB），"
            f"provider={provider}，等待返回…",
            flush=True,
        )
        t0 = time.perf_counter()
        with audio.open("rb") as fh:
            resp = client.post(
                f"{base_url}/api/courses/1/audio/import",
                data={**AUDIO_CELL, "source": "audio"},
                files={"file": (audio.name, fh, "audio/wav")},
            )
        e2e_sec = time.perf_counter() - t0
        if resp.status_code != 200:
            raise RuntimeError(f"音频导入失败 HTTP {resp.status_code}: {resp.text[:300]}")

        payload = resp.json()
        raw_text = payload.get("raw_text") or ""
        chars = count_chars(raw_text)
        rtf = e2e_sec / duration if duration else 0.0
        cps = chars / duration if duration else 0.0

        notes = [
            f"导入单元格：学生 {AUDIO_CELL['student_id']} × 辩题 {AUDIO_CELL['topic_id']}"
            "（避开 9 条已批改作答，保证后续质量对比口径一致）。",
            "RTF = 端到端墙钟 / 音频时长，可外推到整节课（30–60 分钟）。"
            "45s 儿童发言的合理字数区间约 110–180 字，用于核对转写完整性。",
        ]
        tables = [
            {
                "caption": "语音转写（端到端）",
                "headers": ["指标", "值"],
                "rows": [
                    ["ASR Provider", provider],
                    ["ASR Model", model],
                    ["音频文件", str(audio)],
                    ["音频时长（秒）", f"{duration:.2f}"],
                    ["文件大小（MB）", f"{size_mb:.2f}"],
                    ["端到端墙钟（秒）", f"{e2e_sec:.2f}"],
                    ["实时率 RTF（墙钟/音频时长）", f"{rtf:.3f}×"],
                    ["转写产出字数", chars],
                    ["语速（字/秒）", f"{cps:.1f}"],
                    ["作答来源", payload.get("source", "")],
                ],
            }
        ]

        if pure_asr:
            pure = _time_pure_asr(provider, audio)
            tables.append(
                {
                    "caption": "纯 ASR 调用耗时（不含上传/落库）",
                    "headers": ["指标", "值"],
                    "rows": [
                        ["耗时（秒）", f"{pure['sec']:.2f}"],
                        ["RTF", f"{pure['sec'] / duration:.3f}×"],
                        ["产出字数", pure["chars"]],
                    ],
                }
            )
            notes.append(
                "纯 ASR 为脚本直接调用 ASRClient（同 provider）计时，会额外产生一次 ASR 调用。"
            )

    return {
        "title": "语音转写",
        "notes": notes,
        "tables": tables,
        "data": {
            "provider": provider,
            "model": model,
            "audio": str(audio),
            "duration_s": round(duration, 3),
            "size_mb": round(size_mb, 3),
            "e2e_sec": round(e2e_sec, 3),
            "rtf": round(rtf, 4),
            "chars": chars,
            "chars_per_sec": round(cps, 3),
            "source": payload.get("source", ""),
            "raw_text": raw_text,
        },
    }


def _time_pure_asr(provider: str, audio: Path) -> dict:
    sys.path.insert(0, str(BACKEND_DIR))
    from asr import ASRClient

    async def _run():
        return await ASRClient(provider=provider).transcribe(str(audio))

    t0 = time.perf_counter()
    text = asyncio.run(_run())
    return {"sec": time.perf_counter() - t0, "chars": count_chars(text), "text": text}
