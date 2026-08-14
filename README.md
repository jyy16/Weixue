# 维学思辨星 — 少儿思辨能力认知自适应评估系统

面向 1-7 年级思辨课堂的 AI 辅助评估系统。基于 Kuhn（1999）认识论发展模型，为不同认知梯段的学生匹配差异化评估维度，让 AI 承担多维度评分和评语草稿等机械劳动，教师专注于教学判断和个性化反馈。

**在线演示**：[https://l-i-t-t-l-e-l-i-u.github.io/Weixue/](https://l-i-t-t-l-e-l-i-u.github.io/Weixue/)

## 问题与立场

当前 AI 教育产品普遍追求"全自动批改"——AI 读学生作答、AI 打分、AI 写评语，教师沦为旁观者。但研究表明，AI 过度接管评价环节会削弱学生的深度学习（CEPR, 26,811 名学生样本），更关键的是：思辨能力的发展本身就不是一个可以全自动量化的过程。

我们的立场是 **AI 退后一步**：AI 负责结构化分析和草稿生成，在每个主观评价节点设置决策门，最终判断权始终在教师手中。这不是"人机协作"的套话——系统的每一个设计决策都围绕这个原则展开。

## 三个核心创新

### 1. 认知梯度 Rubric（Cognitive Gradient Rubric）

抛弃"所有学生用同一套评分标准"的粗暴做法。基于 Kuhn（1999）的 Realist → Absolutist → Multiplist → Evaluativist 四阶段认识论模型，将 1-7 年级划分为三个认知梯段，每个梯段匹配不同的评估维度和行为锚点：

| 梯段 | 年级 | 核心维度 | 理论依据 |
|------|------|----------|----------|
| 基础层 | 1-2 年级 | 清晰性、解释力、证据意识 | Byrnes & Dunbar (2014)：CT 前技能阶段 |
| 发展层 | 3-5 年级 | 清晰性、相关性、因果推理、证据使用 | Absolutist → Multiplist 过渡期 |
| 进阶层 | 6-7 年级 | 清晰性、相关性、论证质量、深度广度、反思调节 | Multiplist → Evaluativist 过渡期 |

每个维度下设 A+/A/A-/B+/B/B- 六级行为锚点，由 `rubric_loader.py` 在评估时动态组装为完整 prompt。论证质量维度引入 McNeill CER 框架 + Osborne 5 级评分，rebuttal 缺失硬性限制不超过 B+。

### 2. 双层评估流水线（Two-Layer Pipeline）

借鉴 NLP 数据清洗的 decoupling 思想，将评估拆分为两个解耦阶段：

- **Layer 1 — 文本清洗**：去除口语填充词（"嗯""那个""就是"）、修复错别字、规范化标点，产出 `cleaned_text`。保留原始 `raw_text` 供教师回溯。
- **Layer 2 — 维度评估**：基于清洗后的文本，调用认知梯度 rubric 进行多维度评分、推理链生成、特征提取和标签推荐。

这种解耦使每一层可以独立迭代——未来接入语音识别或 OCR 时只需替换 Layer 1，评估逻辑完全不受影响。

### 3. 教师校准记忆（Teacher Calibration Memory）

AI 评估不应该"忘掉"教师的修正偏好。每当教师在批改页覆盖 AI 评分时，系统自动记录差异（AI 原评分 → 教师终评分 + 理由），存入 `calibration_records` 表。下次评估新回答时，`rubric_loader` 从数据库中取最近 10 条校准记录，以紧凑格式注入 LLM prompt 的 few-shot 区域：

```
校准1  AI评分：清晰性A、解释力A-、证据意识B+
       教师修正：清晰性B+、解释力A-、证据意识A-
       教师理由：表达流畅但观点不够明确

校准2  AI评分：论证质量A-、深度广度B+、反思调节B+
       教师修正：论证质量B、深度广度B、反思调节B
       教师理由：缺乏证据支撑，多为个人断言
```

这不是量化蒸馏（"平均上调 0.5 级"对 LLM 没有意义），而是将教师的判断模式以自然语言形式传递给 AI，使评分倾向逐步向教师靠拢。

## 功能模块

- **班级管理**：管理大页下三个独立子页——辩题管理（增删改/排序）、学生管理（单个/批量添加）、录音录入（上传音频或粘贴转写文本），支持辩题↔学生双向查看与移除。
- **音频转写**：可插拔 ASR（演示 mock / 百炼 qwen_asr / OpenAI / DashScope），前端可在演示与真实转写间一键切换，课堂录音自动转写进入评估流水线。
- **智能评估**：AI 按认知梯度 rubric 批量评估，每个维度给出六级评级 + 推理链 + 建议标签。教师逐份审阅，可覆盖任意维度的评分。
- **合格线（教师口径）**：按学生年级分档——1-3 年级 ≥ 2.5（B+，"敢说、说清楚"），4-6 年级及以上 ≥ 3.0（A-，"观点明确、有依据、能换角度"）。课堂模式、智能评估、备课辅助与学情报告统一按学生各自年级的合格线判断达标。
- **评语生成**：基于教师确认的评分、标签和批注，LLM 生成个性化评语草稿。教师编辑后发送，支持批量生成。
- **备课辅助**：按辩题聚合评估数据，识别薄弱维度与低分学生；提供全班总体情况、
  优质发言、分题分析（每题可单独生成 AI 总结，生成后教师可编辑）、AI 总体总结
  （未配置 LLM 时自动用规则模板）；讲评计划可导出 Markdown/JSON、同步飞书多维表格、
  推送机器人卡片。
- **学情报告**：班级层面的多维分析报告，含各学生综合得分、维度雷达图和 Top 标签统计。
- **标签库**：管理评估标签，支持合并、重命名、删除。标签来源分为教研预设（base）、AI 生成（ai_new）和教师手动添加（teacher）。

## 技术栈

| 层次 | 技术 |
|------|------|
| 后端 | FastAPI + Uvicorn + SQLAlchemy |
| 数据库 | SQLite |
| LLM | OpenAI SDK（兼容 DashScope / DeepSeek / OpenAI） |
| ASR | 可插拔（mock / qwen_asr / OpenAI / DashScope），详见 [docs/音频录入与转写.md](./docs/音频录入与转写.md) |
| 前端 | React 18 + Vite + Zustand + Tailwind CSS |
| 部署 | GitHub Pages（纯前端 demo 模式）/ FastAPI + 前端静态托管 |

## 项目结构

```
Weixue/
├── backend/
│   ├── main.py                # FastAPI 装配（挂载路由 + 生命周期 + 静态托管）
│   ├── start.py               # 一键启动：uvicorn + 自动拉起飞书长连接 ws_listener
│   ├── api/                   # 路由模块（按领域拆分，由原 main.py 重构而来）
│   │   ├── state.py           # 运行时单例（llm/evaluator/companion/feishu）与共享工具
│   │   ├── settings.py        # 设置 / ASR / 模式切换
│   │   ├── courses.py         # 课程 / 辩题 / 学生 / 作答
│   │   ├── assessment.py      # 评估 / 批改 / 校准记录
│   │   ├── companion.py       # 课堂伴学对话
│   │   ├── recordings.py      # 音频 / 文本录入
│   │   ├── comments.py        # 评语生成 / 保存 / 发送
│   │   ├── prep.py            # 备课辅助（讲评计划 / AI 总结）
│   │   ├── tags.py            # 标签库
│   │   └── reports.py         # 学情报告
│   ├── database.py            # SQLAlchemy 数据模型
│   ├── schemas.py             # Pydantic 请求/响应模型
│   ├── seed.py                # 演示数据填充
│   ├── asr.py                 # 音频转写抽象层（mock / qwen_asr / openai / dashscope）
│   ├── audio_utils.py         # 音频预处理（ffmpeg 转 16k 单声道 WAV / 示例音频生成）
│   ├── tests/                 # 自动化测试（ASR 导入生命周期 / 批量评估流程 / 多维表格同步）
│   ├── grading/
│   │   ├── evaluator.py       # 双层流水线（Layer1 文本清洗 + Layer2 维度评估）
│   │   ├── rubric_loader.py   # Rubric 模板加载 + prompt 组装 + 校准注入
│   │   └── llm.py             # LLM 客户端适配器
│   ├── feishu/                # 飞书集成（多维表格 / 机器人 / 长连接）
│   ├── export_demo_data.py    # 导出演示数据到前端 demo 模式
│   ├── restore_demo_state.py  # 从 demo-data.json 快照恢复教师批改状态
│   ├── requirements.txt       # 运行依赖
│   ├── requirements-dev.txt   # 开发/测试依赖（含 pytest）
│   └── data/                  # SQLite 数据库
├── frontend/
│   ├── src/
│   │   ├── App.jsx            # 根组件（管理 + 5 个功能 Tab）
│   │   ├── api/
│   │   │   ├── client.js      # API 客户端（自动切换 demo/真实模式）
│   │   │   └── demoClient.js  # 纯前端 demo 数据源
│   │   ├── pages/             # 管理大页（辩题/学生/录音）+ 功能页面
│   │   └── stores/            # Zustand 状态管理
│   └── package.json
├── docs/
│   ├── 飞书集成技术方案.md            # 飞书集成设计与实施路线
│   ├── 音频录入与转写.md              # 音频录入与转写技术文档
│   ├── 技术实现章节素材.md            # 参赛方案"技术实现"章节素材（供统稿）
│   ├── 现场伴学设计与前端重构方案_v1.md  # 课堂伴学/学生端设计方案
│   ├── 飞书教师与学生_open_id_配置与验收.md
│   └── old/                         # 历史/过时文档归档（开题报告、验收记录等）
├── papers/                    # 核心参考文献
│   ├── Kuhn_1999_*.pdf        # 认识论发展阶段模型
│   ├── Byrnes_Dunbar_2014_*.pdf  # CT 前技能与认知发展
│   ├── McNeill_2011_*.pdf     # CER 框架与科学论证
│   └── Osborne_2004_*.pdf     # Toulmin 论证分析框架
```

## 快速开始

### 方式1：在线演示（无需部署）

直接访问 [GitHub Pages](https://l-i-t-t-l-e-l-i-u.github.io/Weixue/)，所有演示数据已内嵌在前端中。



### 方式2：开发环境

```bash
# 后端（一键启动：自动填充演示数据 + 启动 API + 自动拉起飞书长连接 ws_listener）
cd backend
pip install -r requirements.txt
编辑 .env 填入 LLM API Key
python start.py --reload      # http://127.0.0.1:8000（ws_listener 日志见 ws_listener.log）

# 只启动 API、不启动飞书长连接
python start.py --no-listener

# 前端（另开终端）
cd frontend
npm install
npm run dev                   # http://localhost:5173（自动代理到后端）

# 后端测试（需要 dev 依赖，须在 backend/ 目录下运行）
cd backend
pip install -r requirements-dev.txt
python -m pytest tests -q
```

`.env` 配置项：

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `LLM_PROVIDER` | LLM 提供商 | `dashscope` |
| `LLM_API_KEY` | API Key | — |
| `LLM_MODEL` | 模型名称 | 按提供商自动匹配 |
| `LLM_BASE_URL` | API 地址（可选） | — |
| `ASR_PROVIDER` | 音频转写提供商 | `mock`（演示保底）/ `qwen_asr`（推荐）/ `openai` / `dashscope` |
| `ASR_MODEL` | 转写模型名称 | 按提供商自动匹配 |
| `ASR_API_KEY` | 转写 API Key（留空复用 `LLM_API_KEY`） | — |

### 飞书接入（多维表格 + 机器人）

复制 `.env.example` 为 `.env`，填入企业自建应用凭证：

```env
FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=xxx
```

启动后端后，`GET /api/health` 检查数据库、飞书鉴权与多维表格状态。

用 `python start.py` 一键启动时，`ws_listener`（飞书长连接，接收卡片按钮回调与机器人消息）会自动以子进程方式拉起，日志写入 `ws_listener.log`；若已有监听进程在运行则不会重复启动（飞书会把回调分发到所有活跃连接，重复监听会导致卡片点击时好时坏）。也可用 `python start.py --no-listener` 只跑 API。

**思辨星助教**：飞书机器人支持教师直接发消息交互——`讲课计划`（推送讲评计划卡）、`小雨的评语`（推送评语确认卡）、`查 大伟`（学生摘要）、以及校准记忆/批量评估等答疑。默认只响应 `FEISHU_TEACHER_OPEN_ID` 绑定的教师；如需队友一起测试，在 `.env` 加 `FEISHU_ASSISTANT_OPEN_IDS=ou_xxx,ou_yyy`（逗号分隔）后重启后端。

**多维表格（已联调，2026-08-10）**：评估完成 / 教师保存后，本地数据单向同步到多维表格（班级 / 辩题 / 学生 / 评估记录四张表），本地库是唯一事实源，表格只作展示与审阅面。应用凭证就位后一键引导：

```bash
cd backend
python -m feishu.bootstrap_base   # 建 base + 4 表 + 写回 .env + 建字段选项 + 全量同步（幂等可重跑）
```

`FEISHU_BITABLE_APP_TOKEN` 与 `FEISHU_BITABLE_TABLE_IDS` 由引导脚本自动写回 `.env`；未配置时同步保持 `deferred`，不会伪装为已接通，`GET /api/feishu/bitable/status` 可查看实时状态。联调细节与踩坑记录见 [docs/old/多维表格联调记录_2026-08-10.md](./docs/old/多维表格联调记录_2026-08-10.md)。

### 更新纯前端 demo 数据

在运行 `python seed.py` 后执行：

```bash
cd backend
python export_demo_data.py
```

脚本会把当前 SQLite 数据导出到 `frontend/src/demo-data.json`。前后端统一采用 A+/A/A-/B+/B/B- 六级评分口径。

### 演示模式 / 真实模式一键切换

同一个前端构建支持运行时切换数据源，无需重新构建：

- **演示模式**：使用内嵌 `demo-data.json`（GitHub Pages / 断网演示可用），无需后端；
- **真实模式**：连接 FastAPI 后端（`/api`，可用 `VITE_API_BASE` 覆盖）；
- **自动模式（默认）**：启动时探测 `/api/health`，可达则真实、否则演示。

页面右上角有「演示 / 真实」切换按钮；模式选择保存在浏览器 `localStorage`。后端能力矩阵见
`GET /api/settings/mode`，`POST /api/settings/mode`（`enter_demo` / `enter_real`）可一键生成或清除
带标记的演示课程（只动 `demo_course_id` 标记的演示数据，不碰真实课程）。

### 部署到 GitHub Pages

```bash
cd frontend
npm install -D gh-pages
npx cross-env VITE_DEMO_MODE=true VITE_BASE_PATH=/Weixue/ npx vite build
npx gh-pages -d dist
```

`VITE_DEMO_MODE=true` 只把**初始默认**设为演示模式（静态托管推荐）；部署后仍可在
页面右上角一键切到真实模式。两种模式同一构建产物，API 层按运行时模式自动分发。

## 参考文献

- Kuhn, D. (1999). A developmental model of critical thinking. *Educational Researcher*, 28(2), 28-46.
- Byrnes, J. P., & Dunbar, K. N. (2014). The nature and development of critical-analytic thinking. *Educational Psychology Review*, 26(4), 477-493.
- McNeill, K. L. (2011). Elementary students' views of explanation, argumentation, and evidence. *Journal of Research in Science Teaching*, 48(7), 775-803.
- Osborne, J., Erduran, S., & Simon, S. (2004). Enhancing the quality of argumentation in school science. *Journal of Research in Science Teaching*, 41(10), 994-1020.
