"""Grounding guarantees for personalized comment prompts."""

import unittest
from types import SimpleNamespace

from api.comments import COMMENT_GROUNDING_SYSTEM_PROMPT, _build_comment_prompt


class CommentPromptTests(unittest.TestCase):
    def setUp(self):
        self.student = SimpleNamespace(
            name="小雨",
            cognitive_tier="basic",
        )
        self.tier_labels = {"basic": "低年级（1-2年级）"}
        self.topic_data = [{
            "order": 1,
            "title": "老鹰康复后应该回到野外还是留在动物园？",
            "scores": "立意（观点鲜明）: B-",
            "teacher_tags": [],
            "ai_tags": ["具象联想主导", "议题脱钩"],
            "note": "",
            "bonus": [],
            "reviewed": True,
            "raw_text": "老鹰有鹰角。长得不如原神。老鹰不会智斗，太逊了。",
        }]

    def test_prompt_contains_the_actual_answer_as_the_quote_source(self):
        prompt = _build_comment_prompt(
            self.student, self.topic_data, self.tier_labels
        )

        self.assertIn(self.topic_data[0]["raw_text"], prompt)
        self.assertIn("唯一可逐字引用的原文", prompt)
        self.assertIn("不得用引号制造原话", prompt)

    def test_ai_tags_are_not_presented_as_teacher_selected_evidence(self):
        prompt = _build_comment_prompt(
            self.student, self.topic_data, self.tier_labels
        )

        self.assertIn("教师已选标签：无", prompt)
        self.assertIn("AI建议标签（未经教师确认，不可当作事实）", prompt)
        self.assertIn("评分和标签只是概括", COMMENT_GROUNDING_SYSTEM_PROMPT)
        self.assertIn("禁止虚构学生的原话", COMMENT_GROUNDING_SYSTEM_PROMPT)
        self.assertIn("不得把跑题内容包装成", COMMENT_GROUNDING_SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()
