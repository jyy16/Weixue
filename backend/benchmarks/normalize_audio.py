"""Normalize the teammate voice-set WAVs to 16 kHz mono PCM.

The WAVs under backend/data/sample/ have a non-standard header (data chunk
size = 0xFFFFFFFF) and are 40 kHz stereo. This tool reads the PCM data
directly (stdlib only, no ffmpeg), mixes stereo to mono, resamples to
16 kHz and writes a valid WAV next to the original as ``*.16k.wav`` — the
canonical input format of qwen_asr / paraformer.

Usage:
    python backend/benchmarks/normalize_audio.py [--dir backend/data/sample]
"""

from __future__ import annotations

import argparse
import array
import struct
import wave
from pathlib import Path

MAX_DATA_SIZE = 0xFFFFFFFF


class NormalizeError(RuntimeError):
    pass


def _parse_wav(path: Path):
    """Return (fmt_bytes, data_bytes); tolerates a broken data-size header."""
    with open(path, "rb") as f:
        head = f.read(12)
        if len(head) < 12 or head[:4] != b"RIFF" or head[8:12] != b"WAVE":
            raise NormalizeError("不是 RIFF/WAVE 文件")
        fmt = None
        data_offset = None
        data_size = None
        f.seek(12)
        while True:
            hdr = f.read(8)
            if len(hdr) < 8:
                break
            cid, size = struct.unpack("<4sI", hdr)
            if cid == b"fmt ":
                fmt = f.read(size)
                if len(fmt) < 16:
                    raise NormalizeError("fmt 块不完整")
            elif cid == b"data":
                data_offset = f.tell()
                data_size = size
                break
            else:
                f.seek(size + (size & 1), 1)
        if fmt is None or data_offset is None:
            raise NormalizeError("缺少 fmt/data 块")
        f.seek(data_offset)
        if data_size == MAX_DATA_SIZE or data_offset + data_size > path.stat().st_size:
            data = f.read()  # 头里长度无效时读到 EOF
        else:
            data = f.read(data_size)
    return fmt, data


def _resample_40000_to_16000(mono: array.array) -> array.array:
    """40 kHz -> 16 kHz: boxcar-5 low-pass + decimate (-> 8k), then linear x2."""
    n5 = len(mono) // 5
    dec = array.array("h", [0]) * n5
    for k in range(n5):
        i = 5 * k
        dec[k] = (mono[i] + mono[i + 1] + mono[i + 2] + mono[i + 3] + mono[i + 4]) // 5
    out = array.array("h", [0]) * (2 * n5)
    for i in range(n5 - 1):
        a = dec[i]
        b = dec[i + 1]
        out[2 * i] = a
        out[2 * i + 1] = (a + b) >> 1
    out[2 * n5 - 2] = dec[n5 - 1]
    return out


def _resample_linear(mono: array.array, in_rate: int, out_rate: int) -> array.array:
    step = in_rate / out_rate
    out_len = int(len(mono) / step)
    out = array.array("h", [0]) * out_len
    for i in range(out_len):
        x = i * step
        k = int(x)
        frac = x - k
        if k + 1 < len(mono):
            out[i] = int(mono[k] * (1.0 - frac) + mono[k + 1] * frac)
        else:
            out[i] = mono[k]
    return out


def normalize(src: Path, dst: Path) -> float:
    """Convert src to a valid 16 kHz mono s16le WAV at dst; returns duration."""
    fmt, data = _parse_wav(src)
    audio_format, channels, rate, _br, _align, bits = struct.unpack("<HHIIHH", fmt[:16])
    if audio_format != 1 or bits != 16:
        raise NormalizeError(
            f"仅支持 16-bit PCM（当前 format={audio_format}, bits={bits}）"
        )
    usable = len(data) - (len(data) % (channels * 2))
    if usable <= 0:
        raise NormalizeError("无音频数据")

    arr = array.array("h")
    arr.frombytes(data[:usable])
    n = len(arr) - (len(arr) % channels)
    arr = arr[:n]

    if channels == 2:
        left = arr[0::2]
        right = arr[1::2]
        probe = min(20000, len(left))
        diff = sum(
            abs(int(a) - int(b)) for a, b in zip(left[:probe], right[:probe])
        )
        if diff / max(1, probe) < 300:
            mono = left  # 双声道内容一致（TTS 常见），直接取左声道
        else:
            mono = array.array(
                "h", ((int(a) + int(b)) >> 1 for a, b in zip(left, right))
            )
    else:
        mono = arr

    if rate != 16000:
        mono = (
            _resample_40000_to_16000(mono)
            if rate == 40000
            else _resample_linear(mono, rate, 16000)
        )

    with wave.open(str(dst), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(mono.tobytes())
    return len(mono) / 16000.0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dir",
        default=str(Path(__file__).resolve().parent.parent / "data" / "sample"),
        help="存放原始 wav 的目录（默认 backend/data/sample）",
    )
    parser.add_argument("--dry-run", action="store_true", help="只列出待处理文件")
    args = parser.parse_args(argv)

    src_dir = Path(args.dir)
    wavs = sorted(p for p in src_dir.glob("*.wav") if not p.name.endswith(".16k.wav"))
    if not wavs:
        print(f"{src_dir} 下没有可处理的 wav")
        return 1

    rows = []
    for src in wavs:
        dst = src.with_name(src.stem + ".16k.wav")
        if args.dry_run:
            rows.append((src.name, dst.name, "-", "-"))
            continue
        try:
            dur = normalize(src, dst)
            rows.append(
                (
                    src.name,
                    dst.name,
                    f"{dur:.1f}s",
                    f"{dst.stat().st_size / 1024:.0f}KB",
                )
            )
            print(f"OK  {src.name} -> {dst.name}（{dur:.1f}s）")
        except NormalizeError as exc:
            print(f"FAIL {src.name}: {exc}")

    print("\n共 %d 个文件" % len(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

