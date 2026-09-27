import unittest
from types import SimpleNamespace

from src.parser.recommendation_parser import (
    RecommendationParseError,
    RecommendationParser,
)
from src.recommendation_models import ActionKind, SlotRef


class HandDeckActionParserTests(unittest.TestCase):
    def setUp(self):
        self.parser = RecommendationParser()

    def _parse(self, instruction):
        ocr = SimpleNamespace(
            frame_id="frame-1",
            normalized_text=instruction,
            confidence=0.99,
        )
        try:
            return self.parser.parse(ocr, turn_number=3, log_revision=7)
        except RecommendationParseError as exc:
            self.fail(f"{instruction!r} should be accepted: {exc}")

    def test_forge_card_uses_existing_trade_action_for_requested_slot(self):
        proposed = self._parse("锻造3号位卡牌")

        self.assertEqual(ActionKind.TRADE_CARD, proposed.action)
        self.assertEqual(SlotRef("hand_slot", "friendly", 3), proposed.source)
        self.assertEqual("锻造3号位卡牌", proposed.normalized_instruction)

    def test_prepare_card_uses_existing_trade_action_for_requested_slot(self):
        proposed = self._parse("预备2号位卡牌")

        self.assertEqual(ActionKind.TRADE_CARD, proposed.action)
        self.assertEqual(SlotRef("hand_slot", "friendly", 2), proposed.source)
        self.assertEqual("预备2号位卡牌", proposed.normalized_instruction)


class FriendlySideWordingTests(unittest.TestCase):
    """盒子对「己方」的写法不统一，OCR 还会把「我」认成「已」。

    目标行一直有「我方/己方」两种写法，其它行原本只认「我方」。实测
    2026-09-21 04:59、2026-09-22 16:35/19:16 三次卡手就是「已方」导致
    `unsupported_spell_target` 无限重试。三种写法都必须接受。
    """

    SIDES = ("我方", "己方", "已方")

    def setUp(self):
        self.parser = RecommendationParser()

    def _parse(self, instruction):
        ocr = SimpleNamespace(frame_id="frame-1", normalized_text=instruction,
                              confidence=0.99)
        return self.parser.parse(ocr, turn_number=3, log_revision=7)

    def test_friendly_target_accepts_all_wordings(self):
        for side in self.SIDES:
            with self.subTest(side=side):
                proposed = self._parse(f"打出1号位法术\n目标是{side}5号位")
                self.assertEqual(
                    SlotRef("board_slot", "friendly", 5), proposed.target)

    def test_friendly_hero_target_accepts_all_wordings(self):
        for side in self.SIDES:
            with self.subTest(side=side):
                proposed = self._parse(f"打出1号位法术\n目标是{side}英雄")
                self.assertEqual(SlotRef("hero", "friendly"), proposed.target)

    def test_minion_destination_accepts_all_wordings(self):
        for side in self.SIDES:
            with self.subTest(side=side):
                proposed = self._parse(f"打出1号位随从\n放置于{side}3号位")
                self.assertEqual(
                    SlotRef("board_slot", "friendly", 3), proposed.destination)

    def test_hero_attack_accepts_all_wordings(self):
        for side in self.SIDES:
            with self.subTest(side=side):
                proposed = self._parse(
                    f"操作{side}英雄攻击\n目标是对方英雄")
                self.assertEqual(ActionKind.ATTACK, proposed.action)
                self.assertEqual(SlotRef("hero", "friendly"), proposed.source)

    def test_discover_selection_accepts_all_wordings(self):
        for side in self.SIDES:
            with self.subTest(side=side):
                proposed = self._parse(f"选择{side}2号位卡牌")
                self.assertEqual(ActionKind.CHOOSE_DISCOVER, proposed.action)
                self.assertEqual(2, proposed.source.index)

    def test_ocr_misread_char_is_reported_as_near_miss(self):
        """认不出的行若与某个合法句式只差一个字，诊断里直接给出错字。

        这样新增错字表时只收录**实测害过我们**的字，不必预先枚举
        「我/己/已/万」这类没有边界的字形混淆。
        （用「丁方」而非「我万」做样例：后者已在 _OCR_VARIANTS 里，会被直接纠正，
        不再走到失败路径。）
        """
        ocr = SimpleNamespace(
            frame_id="frame-1",
            normalized_text="打出2号位法术\n目标是丁方5号位",
            confidence=0.99)
        with self.assertRaises(RecommendationParseError) as ctx:
            self.parser.parse(ocr, turn_number=3, log_revision=7)
        message = str(ctx.exception)
        self.assertIn("near=", message)
        self.assertIn("丁→对", message)

    def test_valid_lines_do_not_produce_a_near_miss_hint(self):
        """合法行不该进 near 提示（否则日志会被误报刷屏）。

        用一个「合法目标行 + 一个真歧义」的面板触发失败：合法行必须被过滤掉，
        剩下的真歧义行也因为没有单字差异而不产生提示。
        """
        ocr = SimpleNamespace(
            frame_id="frame-1",
            normalized_text=("打出2号位法术\n目标是对方5号位\n目标是敌方1号位"),
            confidence=0.99)
        with self.assertRaises(RecommendationParseError) as ctx:
            self.parser.parse(ocr, turn_number=3, log_revision=7)
        self.assertNotIn("near=", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
