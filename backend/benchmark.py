"""维学思辨星性能基准入口。

用法（仓库根目录，后端虚拟环境）：
    backend\\.venv\\Scripts\\python.exe backend\\benchmark.py all

先用测试 API 更新 backend/.env（LLM_BASE_URL / LLM_API_KEY / LLM_MODEL，
ASR_PROVIDER 保持 qwen_asr），再运行。详细说明见 backend/benchmarks/README.md。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from benchmarks.run import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())

