## 3. B（队长）工作要求 —— 伴学 + 飞书 P0 + 前端体验 + 材料统筹

**目标（8/14 冻结前）**：飞书"评语从机器人出去"的叙事闭环成立，课堂模式可顺畅演示，参赛材料成稿。

### 任务与验收

1. **机器人卡片闭环（P0，当前是空壳）**
   - `POST /api/feishu/card`：校验 `X-Lark-Signature`，按按钮 action 分发——确认评分 / 修改 / 发送给学生，落库更新 `teacher_reviewed`、教师评分，并复用现有逻辑写校准记录；
   - 评语生成后调用 `BotService.send_card` 推卡片；`CommentsPage` 的"发送给学生"接通机器人，权限未就绪时明确显示"待联调"，不伪造成功；
   - **验收**：卡片可推送、按钮点击有真实反馈。
   - **状态（8/10）**：已实现。验签（sha1 + verification token）、解密、`review_confirm` 落库 + 校准记录（复用 `feishu/reviews.py`）、`request_change`/`send_comment` toast、`send_comment` 网页端推送卡片到教师 open_id。真实飞书回调需公网地址 + 应用权限后联调验证。
2. **事件订阅**：`/api/feishu/events` 分发 `im.message.receive_v1`（P1，时间不够则保持轮询兜底）。
   - **状态（8/10）**：已实现事件签名校验（sha256 + encrypt_key）与 `im.message.receive_v1` 轻量分发（含"评语/帮助"回复），`minutes.minute.generated_v1` 订阅留待妙记恢复。
3. **多维表格落地**：4 张表建表、单选选项自动创建验证、AI 字段捷径实测（结果决定 AI 叙事口径）、前端"飞书同步"状态展示（`GradingPage`）。
   - **状态（8/10）**：建表/选项自动化工具 `backend/feishu/schema.py`（`python -m feishu.schema --apply`，幂等）已实现并有测试；`GradingPage` 已加"飞书多维表格"状态卡（配置/绑定数/手动同步）。单选选项自动创建与 AI 字段捷径需真实租户联调验证。
4. **课堂模式 QA**：`LiveCockpit` / `StudentWindow` 全流程走查（开麦 → 提交 → 转写 → 追问 → 评级 → 确认），更新演示脚本 v2。
   - **状态（8/10）**：走查无致命问题；修复模拟录音未广播"正在发言"状态的小缺陷；演示脚本 v2 已更新（`docs/演示视频脚本_v2.md`）。
5. **家长报告前端页**：接口 `GET /api/students/{sid}/report` 已就绪，补页面与入口。
   - **状态（8/10）**：`ReportPage` 学生卡片新增"查看家长报告"入口 + 家长报告视图（能力画像/教师评语/给家长的建议），demo 与真实接口均接通。
6. **雷达图**：`ReportPage` 补班级维度雷达图（P1）。
   - **状态（8/10）**：班级思辨能力雷达图（纯 SVG，0-4 分维度均值）已加。
7. **评语模板正反馈改写**：`backend/grading/evaluator.py` 的评语口径细节。
   - **状态（8/10）**：`evaluator.py` 新增 `COMMENT_TONE_GUIDE` 正反馈口径，评语 prompt 引用；fallback 模板按认知层级给成长型建议，避免否定词。
8. **真实模式状态推送**：WebSocket/SSE 替换 BroadcastChannel（P1，时间够再做）。
   - **状态（8/10）**：未做，`statusBus` 注释保留替换方案；时间不够则维持 BroadcastChannel + 轮询兜底。
9. **材料统筹（非代码）**：参赛方案文档统稿成稿、PPT（大纲 v1 → 成品）、演示视频（脚本 v2 → 录制 → 剪辑）、开题报告更新；A 的技术素材由你整合。
   - **状态（8/10）**：演示脚本 v2 已成稿；参赛方案 / PPT / 视频 / 开题报告待 A 素材到位后统稿（非本次代码范围）。

### 文件边界

- **可碰**：`backend/feishu/**`、`backend/companion.py`、`backend/grading/evaluator.py`、`backend/main.py`（仅飞书/伴学/评语路由段）、前端 `LiveCockpit.jsx` / `StudentWindow.jsx` / `statusBus.js` / `gradingStore.js` / `CommentsPage.jsx` / `GradingPage.jsx` / `ReportPage.jsx` / `App.jsx`、`frontend/src/api/client.js` + `demoClient.js`（**只在文件末尾追加**）、`backend/tests/test_companion*.py` / `backend/tests/test_feishu_card*.py`（新）、材料文档。
- **不碰**：`backend/asr.py`、`backend/audio_utils.py`、`frontend/src/pages/RecordingsManager.jsx`、`backend/seed.py` / `export_demo.py` / `restore_demo_state.py`、`.github/`、`.gitignore`、`.env.example`、`docs/音频录入与转写.md`。
- 参赛方案文档由你统稿，A 以素材形式交付。

### 完成标准（DoD）

- [x] 卡片回调闭环：签名校验 + 按钮分发 + 落库 + 校准记录
- [x] 多维表格写入 + 前端同步状态可见（字段/选项自动化待真实租户联调验证）
- [x] 课堂模式脚本走查无致命问题
- [x] 家长报告页 + 雷达图可用
- [ ] 参赛方案 / PPT / 视频 / 开题报告成稿
- [x] 未授权飞书项全部如实标"待联调"
