# 飞书教师与学生 open_id 配置与验收

本文用于帮助开发者和 PR 审阅者配置、检查“教师确认后，将评语定向发送给对应学生”的完整链路。

## 1. 两类 open_id 分别保存在哪里

| 对象 | 保存位置 | 用途 |
| --- | --- | --- |
| 教师 | 项目根目录 `.env` 的 `FEISHU_TEACHER_OPEN_ID` | 接收评语确认卡、讲评计划卡等教师端消息 |
| 学生 | 数据库中每条学生记录的 `feishu_open_id` 字段 | 接收该学生自己的最终评语 |

不要把所有学生共用一个环境变量。每个学生的 `open_id` 必须分别绑定到对应的学生记录。

> `open_id` 是“用户在某一个应用下的身份标识”。同一个用户在不同飞书应用下的 `open_id` 不同，因此教师和学生的 `open_id` 都必须使用本项目配置的 `FEISHU_APP_ID`、`FEISHU_APP_SECRET` 查询，不能从其他应用复制。

## 2. 飞书开放平台前置配置

在飞书开放平台进入本项目使用的企业自建应用，检查以下项目：

1. 已添加“机器人”应用能力。
2. 已开通应用身份权限：
   - “通过手机号或邮箱获取用户 ID”（`contact:user.id:readonly`）。这是
     `POST /contact/v3/users/batch_get_id` 的必需权限；
     `contact:user.base:readonly` 不是该接口当前文档列出的权限。
   - `im:message:send_as_bot`：以应用身份发送消息。
3. 应用的可用范围、通讯录数据权限范围同时包含测试教师和测试学生。
4. 修改权限、范围或机器人能力后，已经创建并发布新版本；正式企业中的变更还可能需要管理员审核。
5. 教师和学生是当前飞书租户中的成员，并且使用的手机号或邮箱与飞书账号资料一致。

飞书发送消息接口还要求收件人在机器人的可用范围内。仅打开 API 权限，但没有把学生加入应用可用范围，仍然无法查询或发送。

官方参考：

- [通过手机号或邮箱获取用户 ID](https://open.feishu.cn/document/server-docs/contact-v3/user/batch_get_id)
- [用户资源与 open_id 说明](https://open.feishu.cn/document/server-docs/contact-v3/user/field-overview)
- [发送消息](https://open.feishu.cn/document/server-docs/im-v1/message/create)

## 3. 配置应用凭据

在项目根目录创建 `.env`，不要将真实凭据写入 `.env.example`：

```powershell
cd D:\jyy\2026_summer\Weixue
Copy-Item backend\.env.example .env
```

至少填写：

```env
FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=xxx
FEISHU_TEACHER_OPEN_ID=
```

`.env` 已被 Git 忽略。不要在提交、PR 描述、聊天记录或截图中公开 `FEISHU_APP_SECRET`。

## 4. 查询教师和学生的 open_id

查询脚本必须从 `backend` 目录以模块方式运行：

```powershell
cd D:\jyy\2026_summer\Weixue\backend

# 按手机号查询；11 位中国大陆手机号会自动补 +86
python -m feishu.resolve_open_id --mobile 13800000000

# 也可以按用户资料中的邮箱查询（飞书官方说明：不支持企业邮箱字段）
python -m feishu.resolve_open_id --email student@example.com
```

可在一次调用中查询多个人：

```powershell
python -m feishu.resolve_open_id `
  --mobile 13800000000 `
  --mobile 13900000000 `
  --email teacher@example.com
```

成功时输出类似：

```text
找到：+8613800000000 -> ou_xxxxxxxxxxxxxxxxx
```

请记录“人员—`open_id`”对应关系，但不要把真实手机号或完整 `open_id` 提交到仓库。

## 5. 设置教师 open_id

把教师查询结果写入项目根目录 `.env`：

```env
FEISHU_TEACHER_OPEN_ID=ou_teacher_xxx
```

保存后重启后端。教师 `open_id` 的作用是接收待确认的卡片；它不会作为学生评语的最终收件人。

## 6. 在前端绑定学生 open_id

1. 启动后端和前端，并在页面右上角确认当前为“真实模式”，不是“演示模式”。
2. 打开“管理” → “学生管理”。
3. 找到目标学生，点击“编辑”。
4. 在“学生飞书 open_id（ou_...）”输入框中粘贴该学生的 `open_id`。
5. 点击“保存”。
6. 学生行应显示“飞书已绑定”；将输入框留空并保存可以解除绑定。

系统会拒绝不以 `ou_` 开头的值。绑定关系保存在本地数据库中，因此不能只修改前端演示数据，也不应把学生 `open_id` 写成全局环境变量。

审阅者也可以通过接口检查绑定结果：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/courses/<课程ID>/students |
  ConvertTo-Json -Depth 5
```

返回的目标学生记录中应包含非空的 `feishu_open_id`。

## 7. 启动真实推送所需进程

分别打开三个 PowerShell 窗口。

后端：

```powershell
cd D:\jyy\2026_summer\Weixue\backend
python start.py --reload
```

说明：`python start.py` 会同时启动 FastAPI 与飞书长连接监听器（ws_listener，日志见 `ws_listener.log`；已有监听实例时自动跳过，不会重复启动）。如需分开控制：

```powershell
python start.py --no-listener        # 只启动 API
python -m feishu.ws_listener         # 只启动飞书长连接（手动方式）
```

前端：

```powershell
cd D:\jyy\2026_summer\Weixue\frontend
npm run dev
```

`ws_listener` 必须保持运行（`python start.py` 一键启动时会自动拉起），用于接收教师点击飞书卡片按钮产生的回调。机器上只能保留一个本项目的监听进程；多个新旧监听器同时连接可能导致回调被旧进程接收，表现为“有时成功、有时失败”。启动脚本已做重复实例检测，手动启动前请先确认无残留监听进程。

可在 Windows 中检查监听进程：

```powershell
Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -match 'feishu\.ws_listener' } |
  Select-Object ProcessId, CommandLine
```

正常情况下只应看到一个有效监听进程，并且日志中有一条 `connected to wss://...`。

## 8. 端到端推送验收

本功能不是在网页第一次点击时直接绕过教师确认发送。完整流程如下：

1. 在前端“评语生成”页选择已经绑定飞书账号的学生。
2. 生成或编辑评语，点击“发送给学生”。
3. 后端保存最新评语，并向 `FEISHU_TEACHER_OPEN_ID` 发送一张教师确认卡。
4. 教师在飞书中检查学生姓名和评语，点击卡片中的“发送给学生”。
5. 长连接监听器收到按钮回调，根据卡片中的学生 ID 重新查询学生记录。
6. 后端把评语发送到该学生记录绑定的 `feishu_open_id`。
7. 回到“管理” → “学生管理”，刷新后检查投递状态：
   - “评语已送达”：飞书接口已确认发送成功。
   - “飞书发送失败”：查看页面记录的失败原因和后端日志。
   - “飞书发送中”：任务已经提交，等待接口返回。

建议测试时使用两个可以明确区分的飞书账号：教师只应收到确认卡，学生只应收到自己的最终评语。再绑定第二名学生做交叉测试，确认两名学生不会互相收到对方的内容。

## 9. 常见问题

### 9.1 查询结果只有手机号，没有 `user_id`

例如：

```json
{"user_list":[{"mobile":"+8613800000000"}]}
```

这通常表示手机号格式已被飞书接受，但应用无权返回该用户的 ID。依次检查：

1. 手机号是否就是该用户飞书账号当前绑定的号码，可改用用户资料中的邮箱查询交叉验证；飞书官方说明该接口不支持企业邮箱字段。
2. 用户是否与应用处于同一个企业/测试租户；普通外部联系人不能按当前通讯录查询流程处理。
3. 应用“可用范围”是否包含该用户或其部门。
4. 应用“通讯录数据权限范围”是否包含该用户。
5. `contact:user.id:readonly` 是否以“应用身份”开通。
6. 修改后是否发布了新版本并通过管理员审核。
7. 当前 `.env` 的 `FEISHU_APP_ID`、`FEISHU_APP_SECRET` 是否属于刚刚配置范围的同一个应用。

### 9.2 学生已经绑定，但发送失败

检查学生是否也在机器人的可用范围内、是否使用了当前应用查询出的 `open_id`，以及 `im:message:send_as_bot` 和机器人能力是否已经随新版本发布。具体错误会记录在学生的投递失败原因和后端日志中。

### 9.3 网页点击后教师没有收到卡片

检查：

- 页面是否处于真实模式。
- `FEISHU_TEACHER_OPEN_ID` 是否已配置，并且修改 `.env` 后是否重启后端。
- 教师是否在机器人可用范围内。
- 后端是否成功获取应用凭据、是否具备发送消息权限。

### 9.4 教师点击卡片后没有发送给学生

检查 `python -m feishu.ws_listener` 是否正在运行且只有一个实例（用 `python start.py` 启动时会自动拉起监听器）。修改监听代码、权限或事件配置后，需要停止旧进程并重新启动。

监听器日志中的 `processor not found` 如果对应 `im.message.message_read_v1` 或 `im.chat.access_event.bot_p2p_chat_entered_v1`，表示飞书投递了本项目没有注册处理器的附加事件，本身不等于评语发送失败。是否发送成功应以学生投递状态、发送接口响应和学生端实际收件为准。

### 9.5 卡片提示评语已修改或已经发送

系统会校验评语内容哈希，并阻止旧卡片发送已经变更的草稿；请从网页重新点击“发送给学生”，生成包含最新评语的确认卡。同一份评语成功投递后重复点击也会被拦截，避免学生收到重复消息。

## 10. PR 审阅检查清单

- [ ] `.env`、数据库文件和真实用户信息没有进入 Git 提交。
- [ ] 教师 `open_id` 只用于接收确认卡。
- [ ] 不同学生分别绑定了各自的 `feishu_open_id`。
- [ ] 教师和学生的 `open_id` 均由当前应用查询获得。
- [ ] 未绑定学生时，系统阻止发送并给出提示。
- [ ] 教师确认后，消息只到达卡片所指向的学生。
- [ ] 同一张卡重复点击不会重复投递。
- [ ] 修改评语后，旧卡片不能发送过期内容。
- [ ] 成功、失败和发送中状态能够在学生管理页查看。
- [ ] 只运行一个 `feishu.ws_listener` 实例（`start.py` 已自动检测防重复）。
