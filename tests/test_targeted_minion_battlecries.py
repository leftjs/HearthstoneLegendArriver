"""随从指向类战吼（王室图书管理员等）的解析/适配/点击测试。

随从不一定只能带手牌目标：沉默/打伤害等战吼可指向敌方场上随从或英雄，
盒子文本形如「打出N号位随从\n目标是对方X号位随从」。这类推荐在打通前
会卡在 parser 的 MINION 分支报 targeted_action_unsupported。
"""

import unittest
from contextlib import nullcontext
from types import SimpleNamespace

from manual_controller import (
    ClickExecutor,
    ManualController,
    PlayCardAction,
    Target,
)
from src.game_state.recommendation_adapter import adapt_action
from src.parser.recommendation_parser import RecommendationParser
from src.recommendation_models import ActionKind, SlotRef


class RecordingClickModule:
    def __init__(self):
        self.events = []

    def choose_card(self, hand_index, hand_count):
        self.events.append(("choose_card", hand_index, hand_count))

    def put_minion(self, gap_index, board_count):
        self.events.append(("put_minion", gap_index, board_count))

    def drag_card_to_gap(self, hand_index, hand_count, gap_index,
                         board_count):
        self.events.append((
            "drag_card_to_gap",
            hand_index,
            hand_count,
            gap_index,
            board_count,
        ))

    def choose_opponent_minion(self, oppo_index, oppo_num):
        self.events.append(("choose_opponent_minion", oppo_index, oppo_num))

    def choose_oppo_hero(self):
        self.events.append(("choose_oppo_hero",))

    def cancel_click(self):
        self.events.append(("cancel_click",))


def _ocr(instruction):
    return SimpleNamespace(
        frame_id="frame-1",
        normalized_text=instruction,
        confidence=0.99,
    )


def _controller(clicks, sleeps):
    return ManualController(
        output_func=lambda _message: None,
        executor=ClickExecutor(
            click_module=clicks,
            sleep_func=sleeps.append,
            action_context=nullcontext,
        ),
    )


class TargetedMinionParserTests(unittest.TestCase):
    def setUp(self):
        self.parser = RecommendationParser()

    def _parse(self, instruction):
        return self.parser.parse(_ocr(instruction), turn_number=3, log_revision=7)

    def test_minion_targeting_enemy_board_minion(self):
        # 王室图书管理员：打出随从 + 目标是对方5号位随从 → 敌方场上5号位。
        proposed = self._parse("打出2号位随从\n目标是对方5号位随从")

        self.assertEqual(ActionKind.PLAY_CARD, proposed.action)
        self.assertEqual("MINION", proposed.card_type)
        self.assertEqual(SlotRef("hand_slot", "friendly", 2), proposed.source)
        self.assertEqual(
            SlotRef("board_slot", "enemy", 5), proposed.target)

    def test_minion_targeting_enemy_hero(self):
        proposed = self._parse("打出3号位随从\n目标是对方英雄")

        self.assertEqual(ActionKind.PLAY_CARD, proposed.action)
        self.assertEqual(SlotRef("hero", "enemy"), proposed.target)

    def test_minion_targeting_friendly_stays_hand_slot_grammar(self):
        # 己方N号位随从 保留旧语义交给适配层按卡牌 id 判定（手牌 vs 场上）。
        proposed = self._parse("打出1号位随从\n目标是我方2号位随从")

        self.assertEqual(ActionKind.PLAY_CARD, proposed.action)
        self.assertEqual(
            SlotRef("hand_slot", "friendly", 2), proposed.target)


class TargetedMinionAdapterTests(unittest.TestCase):
    @staticmethod
    def _minions(entity_ids):
        return [SimpleNamespace(
            card_id="MINION", cardtype="MINION",
            entity_id=eid, zone_pos=pos)
            for pos, eid in enumerate(entity_ids, start=1)]

    def test_enemy_board_minion_target_resolves_to_enemy_minion(self):
        cards = [
            SimpleNamespace(
                card_id="SPELL_1", cardtype="SPELL",
                entity_id="entity-s", name="法术一"),
            SimpleNamespace(
                card_id="CATA_999", cardtype="MINION",
                entity_id="entity-m", name="王室图书管理员"),
        ]
        state = SimpleNamespace(
            game_num_turns_in_play=3,
            is_my_turn=True,
            my_hand_cards=cards,
            my_minions=[],
            my_locations=[],
            oppo_minions=self._minions(
                ["e1", "e2", "e3", "e4", "e5"]),
            oppo_hero=SimpleNamespace(entity_id="e-hero"),
        )
        proposed = RecommendationParser().parse(
            _ocr("打出2号位随从\n目标是对方5号位随从"),
            turn_number=3, log_revision=7)

        adapted = adapt_action(proposed, state)

        manual = adapted.manual_action
        self.assertIsInstance(manual, PlayCardAction)
        self.assertEqual(1, manual.hand_index)
        self.assertEqual("CATA_999", manual.card_id)
        self.assertEqual(0, manual.gap_index)
        self.assertEqual(
            Target("enemy", "minion", 4, "e5"), manual.target)
        self.assertEqual("e5", adapted.target_entity_id)

    def test_enemy_hero_target_resolves_to_oppo_hero(self):
        state = SimpleNamespace(
            game_num_turns_in_play=3,
            my_hand_cards=[
                SimpleNamespace(
                    card_id="CATA_999", cardtype="MINION",
                    entity_id="entity-m", name="王室图书管理员"),
            ],
            my_minions=[],
            my_locations=[],
            oppo_minions=[],
            oppo_hero=SimpleNamespace(entity_id="e-hero"),
        )
        proposed = RecommendationParser().parse(
            _ocr("打出1号位随从\n目标是对方英雄"),
            turn_number=3, log_revision=7)

        adapted = adapt_action(proposed, state)

        self.assertEqual(
            Target("enemy", "hero", None, "e-hero"),
            adapted.manual_action.target)
        self.assertEqual("e-hero", adapted.target_entity_id)

    def test_friendly_target_on_non_whitelist_minion_means_own_board(self):
        # 非手牌选择白名单随从（如沉默）指向己方随从 → 己方场上 N 号位，不是手牌。
        state = SimpleNamespace(
            game_num_turns_in_play=3,
            my_hand_cards=[
                SimpleNamespace(
                    card_id="CATA_999", cardtype="MINION",
                    entity_id="entity-m", name="王室图书管理员"),
            ],
            my_minions=self._minions(["f1", "f2"]),
            my_locations=[],
            oppo_minions=self._minions(["e1"]),
            oppo_hero=SimpleNamespace(entity_id="e-hero"),
        )
        proposed = RecommendationParser().parse(
            _ocr("打出1号位随从\n目标是我方2号位随从"),
            turn_number=3, log_revision=7)

        adapted = adapt_action(proposed, state)

        self.assertEqual(
            Target("friendly", "minion", 1, "f2"),
            adapted.manual_action.target)


class TargetedMinionControllerTests(unittest.TestCase):
    def _state(self):
        return SimpleNamespace(
            game_num_turns_in_play=3,
            is_my_turn=True,
            my_hand_cards=[
                SimpleNamespace(
                    card_id="SPELL_1", cardtype="SPELL",
                    entity_id="entity-s", name="法术一"),
                SimpleNamespace(
                    card_id="CATA_999", cardtype="MINION",
                    entity_id="entity-m", name="王室图书管理员"),
            ],
            my_minions=[],
            my_locations=[],
            my_board_slot_num=0,
            oppo_minions=[
                SimpleNamespace(
                    card_id="M", cardtype="MINION",
                    entity_id=eid, zone_pos=pos)
                for pos, eid in enumerate(["e1", "e2", "e3", "e4", "e5"],
                                          start=1)
            ],
            oppo_board_slot_num=5,
        )

    def test_play_minion_then_click_enemy_target(self):
        proposed = RecommendationParser().parse(
            _ocr("打出2号位随从\n目标是对方5号位随从"),
            turn_number=3, log_revision=7)
        state = self._state()
        adapted = adapt_action(proposed, state)
        clicks = RecordingClickModule()
        sleeps = []

        result = _controller(clicks, sleeps).execute(
            adapted.manual_action, state)

        self.assertTrue(result.executed, result.message)
        self.assertEqual([
            ("drag_card_to_gap", 1, 2, 0, 0),
            ("choose_opponent_minion", 4, 5),
            ("cancel_click",),
        ], clicks.events)
        self.assertEqual([0.3], sleeps)

    def test_enemy_target_with_explicit_destination_drags_to_gap(self):
        # 王室图书管理员 + 「放置于我方6号位」：满编场上带落点。盒子推荐
        # 「打出2号位随从/目标是对方3号位/放置于我方6号位」。落牌必须是拖到
        # 空隙而不是点两下，否则指向战吼随从会弹回手牌。
        state = SimpleNamespace(
            game_num_turns_in_play=9,
            is_my_turn=True,
            my_hand_cards=[
                SimpleNamespace(
                    card_id="MINION_1", cardtype="MINION",
                    entity_id="h1", name="假随从"),
                SimpleNamespace(
                    card_id="CATA_999", cardtype="MINION",
                    entity_id="h2", name="王室图书管理员"),
            ],
            my_minions=[
                SimpleNamespace(
                    card_id="M", cardtype="MINION",
                    entity_id=fid, zone_pos=pos)
                for pos, fid in enumerate(
                    ["f1", "f2", "f3", "f4", "f5"], start=1)
            ],
            my_locations=[],
            my_board_slot_num=5,
            oppo_minions=[
                SimpleNamespace(
                    card_id="M", cardtype="MINION",
                    entity_id=eid, zone_pos=pos)
                for pos, eid in enumerate(
                    ["e1", "e2", "e3", "e4", "e5", "e6"], start=1)
            ],
            oppo_board_slot_num=6,
        )
        proposed = RecommendationParser().parse(
            _ocr("打出2号位随从\n目标是对方3号位随从\n放置于我方6号位"),
            turn_number=9, log_revision=7)
        adapted = adapt_action(proposed, state)
        clicks = RecordingClickModule()
        sleeps = []

        result = _controller(clicks, sleeps).execute(
            adapted.manual_action, state)

        self.assertTrue(result.executed, result.message)
        self.assertEqual(1, adapted.manual_action.hand_index)
        self.assertEqual(5, adapted.manual_action.gap_index)
        self.assertEqual(
            Target("enemy", "minion", 2, "e3"),
            adapted.manual_action.target)
        self.assertEqual([
            ("drag_card_to_gap", 1, 2, 5, 5),
            ("choose_opponent_minion", 2, 6),
            ("cancel_click",),
        ], clicks.events)
        self.assertEqual([0.3], sleeps)


if __name__ == "__main__":
    unittest.main()
