# 性能基准（benchmark）

用真实 LLM / ASR 链路量化三个核心环节，产出可直接贴进参赛方案「量化收益」的数字：

| 环节 | 指标 | 对应接口 |
|---|---|---|
| 语音转写 | 端到端墙钟、实时率 RTF、产出字数 | `POST /api/courses/1/audio/import`（45s 示例音频） |
| 批量评估 | 27 条总耗时、秒/条、成功/错误/调用数 | `POST /api/courses/1/assess` + 轮询 `assessment-progress` |
| 评语生成 | 9 条逐学生 + 批量耗时、字数合规 | `POST /api/courses/1/comments`、`/comments/batch` |
| 质量 | AI 初评 vs 教师终评五维度一致率 | 评估后离线对比 seed 的 9 条已批改作答 |
| 成本 | token 粗估（输入/输出字符） | 估算；实际以测试 API 控制台为准 |

## 运行前

1. 把 `backend/.env` 换成测试 API 配置（`LLM_BASE_URL` / `LLM_API_KEY` /
   `LLM_MODEL`；`ASR_PROVIDER` 保持 `qwen_asr`，`ASR_MODEL=qwen3-asr-flash`）。
2. 确认后端虚拟环境可用（`backend\.venv\Scripts\python.exe`）。

## 运行

```powershell
# 完整基准（评语 → 转写 → 批量评估 → 质量/成本 → 报告），约 10 分钟真实调用
backend\.venv\Scripts\python.exe backend\benchmark.py all

# 只跑某一环节
backend\.venv\Scripts\python.exe backend\benchmark.py transcribe
backend\.venv\Scripts\python.exe backend\benchmark.py comments
backend\.venv\Scripts\python.exe backend\benchmark.py assess
```

可选参数（见 `--help`）：

- `--port 8788`：临时 uvicorn 端口，冲突时更换。
- `--out tmp/benchmark`：报告输出目录。
- `--clean`：结束后删除基准库与本次上传的音频。
- `--timeout 2400`：批量评估轮询超时秒数。
- `transcribe --pure-asr`：额外直接调用 ASRClient 计时纯转写耗时（多一次 ASR 调用）。

产物：`tmp/benchmark/bench-<命令>-<时间戳>.md`（人类可读）与同名 `.json`（完整数据）。
关键报告已快照归档至 `docs/benchmarks/`（含索引与口径说明），提交方案时引用归档路径。

## 口径说明（写进方案时保持一致）

- **27 条 = 9 条种子作答 + 18 条语音集文字稿**：`seed.py` 只含 9 条作答
  （每生 1 题，即教师已批改的那 9 条）；其余 18 个单元格由
  `backend/data/sample/NN_s<学生>_t<辩题>_*.txt` 的文字稿补齐（8/11 压测同一批
  模拟发言）。`all` 流程中「学生1 × 辩题2」会被真实音频转写结果替换。
  文字稿缺失时脚本会报错列出缺口，不会循环复用文本。
- **质量对比**：以 seed 的 9 条教师已批改作答为参照，比较评估后 AI 初评与
  教师终评的五维度一致率；转写单元格（学生 1 × 辩题 2）刻意避开这 9 条。
- **成本**：脚本只做字符级粗估（中文≈1 token），真实费用以测试 API 控制台为准。
- **旧数提醒**：8/11 的 515s≈19s/条 是 qwen-plus 时期的数字，本轮模型见报告头部，
  两批数字不要混用。
- **语音集 wav**：`backend/data/sample/*.wav` 为非标准 WAV 头（data 长度字段为
  0xFFFFFFFF、40kHz 双声道），已用 `backend/benchmarks/normalize_audio.py`
  归一化为 `*.16k.wav`（16kHz 单声道，纯标准库实现，无需 ffmpeg）。
  转写计时默认仍用标准 45s/16k 单声道的 `sample_class_audio.wav`；想用队友
  语音计时可加 `transcribe --audio backend/data/sample/01_s1_t2_小雨.16k.wav`。
- ASR 为 `mock` 时脚本会拒绝计时（耗时无意义），除非显式 `--allow-mock`。

## 实测账单记录（2026-08-14）

- DeepSeek 官方账单：**89 请求 / 161,653 token / ¥0.18**（含此前两次失败尝试的调用；
  单次完整成功跑 `all` 约 45 次调用 ≈ ¥0.15，即 27 条评估 + 9 条评语 ≈ ¥0.15/节课）。
- ASR（qwen3-asr-flash）走百炼控制台计费，未含在 DeepSeek 账单内。
- 字符粗估约比实际低 27%（实际约 1.25 token/字符），后续可直接用本账单当成本基准。

## 企业确认基线（2026-08-14）

- 教师撰写一条评语：**3–5 分钟/生（企业确认）**；对应实测 AI 草稿 **5.4s/条**（批量 6.1s/条），
  草稿阶段提速约 **33–56 倍**。
- 待确认：课堂转写整理基线（方案当前按 30–60 分钟/节课，来源为需求会口径，未经企业背书）。

## 教师侧手测（2026-08-14）

- 人工批改 1 条作答：约 **3 分钟**（单人秒表实测，建议补测 2–3 条取平均）。
- AI 初稿审阅确认：**≤1 分钟/条** → 教师批改时间节省约 **67%**。
- 全流程单条：AI 生成 26.4s + 教师确认 ≤60s ≈ **86s**，对比人工 180s 约 2 倍提速。
- 评语定稿耗时未测（建议顺手测 3 条）。

## 转写准确率记录（2026-08-14）

- 样本：18 条队友语音 + 3 条豆包儿童音色 + 示例音频 = 22 条，共 592.4s 音频，
  ASR 总耗时 58.8s（qwen3-asr-flash 真实调用）。
- 平均逐字一致率（去标点）**99.0%**，中位 **100%**，最低 **91.4%**。
- 儿童音色样本 91.4%–97.0%，差异主要为语气词插入（嗯/呢/吧/呀/啦）与个别同音字，语义可读。
- 运行：`python backend\benchmark.py accuracy`（需联网；`--limit N` 省额度，`--dry-run` 只列配对）。

## 校准记忆 A/B 记录（2026-08-15）

- 同 9 条作答跑两轮：A 无校准（冷启动）→ 从 A 轮观测偏差造 5 条模拟教师校准记录 → B 重跑。
- **45 维一致率 27% → 40%**；AI 偏高次数 **30 → 22**（收敛 27%）；视角维度 56% → 89%。
- 0/9 整体完全一致未变；结构/语言维度仍低（11%），说明 5 条样本只够部分收敛，不是真实教师验证。
- 代价：B 轮耗时 304s vs A 轮 181s（提示词变长，约 +68% 耗时与 token）。
- 完整报告：`docs/benchmarks/bench-calibration-20260815-004213.md`。
