"""Load the teammate voice-set transcripts for the 27-response matrix.

Files live in backend/data/sample/ and follow the naming convention:
    NN_s<student_id>_t<topic_id>_<name>.txt

These are the 18 simulated speeches used in the 8/11 stress test; together
with the 9 seeded answers they form the full 9 students x 3 topics = 27
response matrix. Teammates can edit the .txt files and rerun the benchmark.
"""

from __future__ import annotations

import re
from pathlib import Path

from .common import BACKEND_DIR

SAMPLE_DIR = BACKEND_DIR / "data" / "sample"
NAME_RE = re.compile(r"^\d+_s(\d+)_t(\d+)_(.+)\.txt$")


def load_transcripts(sample_dir: Path = SAMPLE_DIR) -> dict[tuple[int, int], str]:
    """Return {(student_id, topic_id): text} from the sample transcripts."""
    result: dict[tuple[int, int], str] = {}
    if not sample_dir.exists():
        return result
    for path in sorted(sample_dir.glob("*.txt")):
        m = NAME_RE.match(path.name)
        if not m:
            continue
        text = path.read_text(encoding="utf-8").strip()
        if text:
            result[(int(m.group(1)), int(m.group(2)))] = text
    return result

