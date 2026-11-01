import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import patch

import click as hearthstone_click
from manual_controller import ClickExecutor, ManualController
from src.flow.recommendation_flow import RecommendationFlow
from src.game_state.recommendation_adapter import (
    RecommendationStateError, adapt_action,
)
from src.parser.recommendation_parser import RecommendationParser
from src.recommendation_models import ActionKind, OcrEvidence


# 实测盒子文案（2026-10-29 22:35:01）：1 号位法术「潮起潮落」指向对方 1 号位。
BOX_TEXT = "打出1号位法术\n潮起潮落\n目标是对方1号位\n滑行者巨怪"
MINION_TEXT = "打出1号位随从\n托加\n放置于我方1号位"


def card(card_id, cardtype, entity_id, zone_pos, name="牌"):
    return SimpleNamespace(card_id=card_id, cardtype=cardtype,
                           entity_id=entity_id, zone_pos=zone_pos, name=name,
                           current_cost=2, is_coin=False)


def minion(card_id, entity_id, zone_pos, name="随从"):
    return SimpleNamespace(card_id=card_id, cardtype="MINION",
                           entity_id=entity_id, zone_pos=zone_pos, name=name,
                           damage=0, max_health=9, exhausted=0, attack=3)


class PanelCardTypeTests(unittest.TestCase):
    def parse(self, text, turn_number=13, revision=100):
        return RecommendationParser().parse(
            OcrEvidence("f", 0.0, (), text, 0.95, "test", "test"),
            turn_number, revision)

    def state(self, first_hand_card, **overrides):
        """first_hand_card 模拟「日志状态里 1 号槽认成了别的类型」。"""
        values = dict(
            game_num_turns_in_play=13, is_my_turn=True, my_player_id="2",
            my_hand_cards=[first_hand_card,
                           card("CATA_190h", "HERO", "57", 2, "灭世者死亡之翼")],
            my_minions=[minion("BE_204", "26", 1)],
            oppo_minions=[minion("BE_204", "26", 1),
                          minion("CATA_565t", "125", 3),
                          minion("CATA_565t", "128", 2)],
            my_locations=[], oppo_locations=[],
            my_hero=SimpleNamespace(entity_id="78", name="萨尔"),
            oppo_hero=SimpleNamespace(entity_id="76", name="阿兰娜"),
            discover_choice_count=None, target_choice_open=False,
            assemble_choice=False, power_options={},
            titan_ability_choices={},
        )
        values.update(overrides)
        return SimpleNamespace(**values)

    # ------------------------------------------------------------ 适配
    def test_panel_spell_wins_over_stale_hero_state(self):
        state = self.state(card("CATA_190h", "HERO", "57", 1,
                                "灭世者死亡之翼"))
        adapted = adapt_action(self.parse(BOX_TEXT), state)
        self.assertEqual("SPELL", adapted.manual_action.cardtype)
        self.assertIsNotNone(adapted.manual_action.target)
        self.assertEqual("enemy", adapted.manual_action.target.side)
        self.assertIn("手牌类型与面板不一致", adapted.note)
        self.assertIn("面板=SPELL", adapted.note)

    def test_panel_minion_wins_over_stale_spell_state(self):
        state = self.state(card("TIME_702", "SPELL", "114", 1, "潮起潮落"))
        adapted = adapt_action(self.parse(MINION_TEXT), state)
        self.assertEqual("MINION", adapted.manual_action.cardtype)
        self.assertEqual(0, adapted.manual_action.gap_index)
        self.assertIn("面板=MINION", adapted.note)

    def test_consistent_state_has_no_note(self):
        state = self.state(card("TIME_702", "SPELL", "114", 1, "潮起潮落"))
        adapted = adapt_action(self.parse(BOX_TEXT), state)
        self.assertEqual("", adapted.note)
        self.assertEqual(ActionKind.PLAY_CARD, self.parse(BOX_TEXT).action)

    # ------------------------------------------------------------ 执行
    def test_targeted_spell_clicks_hand_then_target_even_if_state_says_hero(self):
        # 就是出问题的那一手：以前会「点手牌 → 点中间 → 右键取消」，现在必须
        # 点手牌 → 点目标（对方 1 号位 = 共 3 个里的第 1 个 → x=820, y=400）。
        state = self.state(card("CATA_190h", "HERO", "57", 1))
        adapted = adapt_action(self.parse(BOX_TEXT), state)
        clicks = []
        executor = ClickExecutor(click_module=hearthstone_click,
                                 action_context=nullcontext,
                                 sleep_func=lambda _: None)
        with patch.object(hearthstone_click, "left_click",
                          side_effect=lambda x, y: clicks.append((x, y))), \
                patch.object(hearthstone_click, "right_click"), \
                patch.object(hearthstone_click, "rand_sleep"):
            result = ManualController(
                executor=executor, output_func=lambda _: None).execute(
                    adapted.manual_action, state)
        self.assertTrue(result.executed, result.message)
        self.assertIn((820, 400), clicks)
        self.assertNotIn((960, 500), clicks)

    def test_minion_play_keeps_its_board_destination(self):
        state = self.state(card("TIME_702", "SPELL", "114", 1))
        adapted = adapt_action(self.parse(MINION_TEXT), state)
        calls = []
        executor = ClickExecutor(click_module=hearthstone_click,
                                 action_context=nullcontext,
                                 sleep_func=lambda _: None)
        with patch.object(hearthstone_click, "choose_card",
                          side_effect=lambda i, n: calls.append(("hand", i, n))), \
                patch.object(hearthstone_click, "put_minion",
                             side_effect=lambda g, n: calls.append(("gap", g, n))), \
                patch.object(hearthstone_click, "cancel_click"):
            result = ManualController(
                executor=executor, output_func=lambda _: None).execute(
                    adapted.manual_action, state)
        self.assertTrue(result.executed, result.message)
        self.assertEqual([("hand", 0, 2), ("gap", 0, 1)], calls)

    def test_changed_card_is_still_rejected(self):
        state = self.state(card("CATA_190h", "HERO", "57", 1))
        adapted = adapt_action(self.parse(BOX_TEXT), state)
        state.my_hand_cards[0] = card("SOMETHING_ELSE", "SPELL", "57", 1)
        result = ManualController(executor=NullExecutor()).execute(
            adapted.manual_action, state)
        self.assertFalse(result.executed)
        self.assertIn("手牌内容已经变化", result.message)

    def test_flow_prints_the_mismatch_note(self):
        # 流程层把 note 打进日志（浮窗里能直接看到为什么这么点）。
        state = self.state(card("CATA_190h", "HERO", "57", 1))
        adapted = adapt_action(self.parse(BOX_TEXT), state)
        self.assertTrue(getattr(adapted, "note", ""))
        self.assertIn("[提示]", self._render_note(adapted))

    @staticmethod
    def _render_note(adapted):
        note = getattr(adapted, "note", "")
        return f"[提示] {note}" if note else ""


class NullExecutor:
    def __getattr__(self, name):
        raise AssertionError(f"不应调用鼠标执行器：{name}")


if __name__ == "__main__":
    unittest.main()
