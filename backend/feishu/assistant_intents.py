"""思辨星助教: keyword intent parsing + curated FAQ (pure logic, no I/O).

Kept free of Feishu/DB imports so it can be unit-tested without touching the
network or backend/.env. The executor (``feishu.assistant``) turns the parsed
intent into a card push / text reply.
"""

import re

HELP_MENU = (
    "思辨星助教：直接告诉我你想做什么\n"
    "・「讲课计划」→ 推送今天的讲评计划卡\n"
    "・「小雨的评语」→ 推送该生评语确认卡\n"
    "・「查 大伟」→ 学生情况摘要（评分/评级/投递状态）\n"
    "・「校准记忆是什么」「怎么批量评估」… → 答疑\n"
    "・「帮助」→ 显示本菜单"
)

FAQ = (
    (
        ("校准", "校准记忆", "老师改分"),
        "校准记忆：教师每次覆盖 AI 评分时，系统记录「AI 初评 → 教师终评 → 修改理由」；"
        "下次评估会把最近 10 条记录注入 AI 提示词，让 AI 初稿逐步贴近你的评价尺度——"
        "传递的是判断模式，不是简单调分。",
    ),
    (
        ("批量评估", "批量", "一起评估"),
        "批量评估：在「智能评估」页点击批量评估，系统按学生认知梯段逐条调用 AI 出初稿，"
        "可在评估页查看进度；完成后逐份确认即可。",
    ),
    (
        ("绑定", "open_id", "飞书账号", "学生绑定"),
        "绑定学生飞书：在学生管理里为每个学生填 feishu_open_id（或以手机号代替，"
        "系统会自动解析）；绑定后评语可直接私发学生。",
    ),
    (
        ("认知", "梯段", "标尺", "年级"),
        "认知梯度：1–2 年级评清晰性/解释力/证据意识，3–5 年级增加相关性/因果推理/证据使用，"
        "6–7 年级进一步评论证质量/深度广度/反思调节——不同年龄用不同标尺，"
        "孩子不会因超龄能力缺失被误判。",
    ),
    (
        ("发送给学生", "怎么发评语"),
        "评语：在「评语生成」页生成并保存草稿，点发送后机器人会把确认卡推给你；"
        "在卡上点「发送给学生」即私发学生，系统同步显示已送达。",
    ),
    (
        ("多维表格", "同步", "bitable"),
        "多维表格：评估完成/教师保存后自动同步到多维表格（班级/辩题/学生/评估记录），"
        "可在「飞书同步」卡片查看实时状态。",
    ),
)


def faq_answer(text: str) -> str:
    """Return the FAQ answer whose keywords appear in ``text``, else the menu."""
    for keywords, answer in FAQ:
        if any(k in text for k in keywords):
            return answer
    return HELP_MENU


def allowed_open_ids(teacher_open_id: str, extra: str = "") -> set:
    """Compute the set of open_ids the assistant may serve.

    ``extra`` is a comma-separated list (e.g. FEISHU_ASSISTANT_OPEN_IDS) so
    teammates can also chat with the bot during testing/demo.
    """
    allowed = set()
    if teacher_open_id and teacher_open_id.strip():
        allowed.add(teacher_open_id.strip())
    for item in (extra or "").split(","):
        item = item.strip()
        if item:
            allowed.add(item)
    return allowed


def parse_intent(text: str) -> dict:
    """Parse a teacher message into an action.

    Returns one of:
      {"action": "help"}
      {"action": "prep_plan"}
      {"action": "comment_card", "student": name}
      {"action": "comment_help"}
      {"action": "student_summary", "student": name}
      {"action": "faq"}
    """
    t = (text or "").strip()
    if not t:
        return {"action": "help"}
    if any(k in t for k in ("帮助", "菜单", "能做什么", "怎么用")):
        return {"action": "help"}
    if any(k in t for k in ("讲课计划", "备课计划", "讲评计划", "今天的计划")):
        return {"action": "prep_plan"}

    # 学生评语: "小雨的评语" / "评语 大伟"
    m = re.search(r"([\u4e00-\u9fa5]{1,4})的评语", t)
    if m:
        return {"action": "comment_card", "student": m.group(1)}
    m = re.search(r"评语\s*([\u4e00-\u9fa5]{1,4})", t)
    if m:
        return {"action": "comment_card", "student": m.group(1)}
    if "评语" in t:
        return {"action": "comment_help"}

    # 学生查询: "查 大伟" / "查一下 小明" / "大伟怎么样"
    m = re.search(r"(?:查一下|查询|看看|查查|查)\s*([\u4e00-\u9fa5]{1,4})", t)
    if m:
        return {"action": "student_summary", "student": m.group(1)}
    m = re.search(r"([\u4e00-\u9fa5]{2,4})(?:怎么样|的情况|的表现|的成绩|表现)", t)
    if m:
        return {"action": "student_summary", "student": m.group(1)}

    if any(k in t for entry in FAQ for k in entry[0]):
        return {"action": "faq"}
    return {"action": "help"}
