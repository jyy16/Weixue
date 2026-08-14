"""Unit tests for the 思辨星助教 intent parser (pure logic, no Feishu/DB)."""

from feishu.assistant_intents import (
    HELP_MENU,
    FAQ,
    allowed_open_ids,
    faq_answer,
    parse_intent,
)


def test_help_intents():
    for text in ("帮助", "菜单", "你能做什么", ""):
        assert parse_intent(text)["action"] == "help", text


def test_prep_plan_intent():
    for text in ("讲课计划", "备课计划", "帮我看看今天的讲课计划"):
        assert parse_intent(text)["action"] == "prep_plan", text


def test_comment_card_intent():
    assert parse_intent("小雨的评语") == {"action": "comment_card", "student": "小雨"}
    assert parse_intent("评语 大伟") == {"action": "comment_card", "student": "大伟"}
    assert parse_intent("评语") == {"action": "comment_help"}


def test_student_summary_intent():
    assert parse_intent("查 大伟") == {"action": "student_summary", "student": "大伟"}
    assert parse_intent("查一下 小明") == {"action": "student_summary", "student": "小明"}
    assert parse_intent("大伟怎么样")["action"] == "student_summary"
    assert parse_intent("大伟的表现")["action"] == "student_summary"


def test_faq_intent_and_answer():
    assert parse_intent("校准记忆是什么")["action"] == "faq"
    assert parse_intent("怎么批量评估")["action"] == "faq"
    answer = faq_answer("校准记忆是什么")
    assert "校准记忆" in answer and len(answer) > 10
    assert faq_answer("完全无关的一句话") == HELP_MENU
    assert FAQ  # 知识库非空


def test_unknown_falls_back_to_help():
    assert parse_intent("随便说点什么")["action"] == "help"


def test_allowed_open_ids():
    assert allowed_open_ids("ou_a") == {"ou_a"}
    assert allowed_open_ids("ou_a", "ou_b, ou_c") == {"ou_a", "ou_b", "ou_c"}
    assert allowed_open_ids("", "") == set()
    assert allowed_open_ids("", " ou_x , ") == {"ou_x"}
