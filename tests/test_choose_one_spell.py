import unittest
from contextlib import nullcontext
from types import SimpleNamespace

from log_state import CardEntity
from manual_controller import (
    CHOOSE_ONE_PANEL_DELAY,
    ClickExecutor,
    ManualController,
    PlayCardAction,
)
from src.game_state.recommendation_adapter import (
    RecommendationStateError,
    adapt_action,
)
from src.parser.recommendation_parser import RecommendationParser
from src.recommendation_models import ActionKind, SlotRef
from strategy import StrategyState


class RecordingClickModule:
    def __init__(self):
        self.events = []

    def choose_card(self, hand_index, hand_count):
        self.events.append(("choose_card", hand_index, hand_count))

    def click_middle(self):
        self.events.append(("click_middle",))

    def choose_discover_card(self, choice_index, choice_count):
        self.events.append(
            ("choose_discover_card", choice_index, choice_count))

    def cancel_click(self):
        self.events.append(("cancel_click",))


class ChooseOneParserTests(unittest.TestCase):
    def setUp(self):
        self.parser = RecommendationParser()

    def _parse(self, instruction):
        ocr = SimpleNamespace(
            frame_id="frame-1",
            normalized_text=instruction,
            confidence=0.99,
        )
        return self.parser.parse(ocr, turn_number=3, log_revision=7)

    def test_captures_by_name_choice_on_the_play_frame(self):
        proposed = self._parse(
            "打出4号位法术\n"
            "森林之灵\n"
            "选择卡牌\n"
            "群狼的力量")

        self.assertEqual(ActionKind.PLAY_CARD, proposed.action)
        self.assertEqual(SlotRef("hand_slot", "friendly", 4), proposed.source)
        self.assertEqual("SPELL", proposed.card_type)
        self.assertEqual("群狼的力量", proposed.choice_card_name)
        # 归一化文案=规范面板文本(reader 也是这套)，故包含选择行；去重/展示用它。
        self.assertEqual(
            "打出4号位法术\n选择卡牌\n群狼的力量",
            proposed.normalized_instruction)

    def test_captures_inline_colon_form(self):
        proposed = self._parse(
            "打出2号位法术\n选择卡牌：猎鹰的灵动")

        self.assertEqual(ActionKind.PLAY_CARD, proposed.action)
        self.assertEqual("猎鹰的灵动", proposed.choice_card_name)

    def test_plain_spell_play_has_no_choice(self):
        proposed = self._parse("打出3号位法术")

        self.assertEqual(ActionKind.PLAY_CARD, proposed.action)
        self.assertIsNone(proposed.choice_card_name)

    def test_normalize_keeps_choice_so_reader_pipeline_keeps_it(self):
        # 生产链路里 OCR reader 会先用 normalize_action_text 过滤面板文本，
        # 抉择分支名必须在这条规范文本里存活，parse() 才能再读到。
        canonical = RecommendationParser.normalize_action_text(
            "打出4号位法术\n"
            "森林之灵\n"
            "选择卡牌\n"
            "群狼的力量")

        self.assertEqual(
            "打出4号位法术\n选择卡牌\n群狼的力量", canonical)
        proposed = self._parse(canonical)
        self.assertEqual("群狼的力量", proposed.choice_card_name)

    def test_choose_header_without_action_line_is_not_a_play(self):
        # 若盒子只给 选择卡牌/名(没有打出行)，不应当被误当成出牌动作。
        ocr = SimpleNamespace(
            frame_id="frame-1",
            normalized_text="选择卡牌\n群狼的力量",
            confidence=0.99,
        )
        with self.assertRaises(Exception):
            self.parser.parse(ocr, turn_number=3, log_revision=7)


class ChooseOneAdapterTests(unittest.TestCase):
    @staticmethod
    def _hand_card(card_id, cardtype, entity_id):
        return SimpleNamespace(
            card_id=card_id, cardtype=cardtype,
            entity_id=entity_id, name="森林之灵")

    @staticmethod
    def _sub_options():
        return {
            "123": [
                SimpleNamespace(
                    entity_id="124", card_id="EDR_233a",
                    name="群狼的力量"),
                SimpleNamespace(
                    entity_id="125", card_id="EDR_233b",
                    name="猎鹰的灵动"),
            ],
        }

    def _state(self, cards):
        return SimpleNamespace(
            game_num_turns_in_play=3,
            my_hand_cards=cards,
            my_minions=[],
            my_locations=[],
            oppo_minions=[],
            sub_options_by_parent=self._sub_options(),
        )

    def _parse(self, instruction):
        ocr = SimpleNamespace(
            frame_id="frame-1",
            normalized_text=instruction,
            confidence=0.99,
        )
        return RecommendationParser().parse(ocr, turn_number=3, log_revision=7)

    def test_resolves_first_sub_option_to_leftmost_index(self):
        proposed = self._parse(
            "打出1号位法术\n选择卡牌\n群狼的力量")
        state = self._state([self._hand_card("EDR_233", "SPELL", "123")])

        adapted = adapt_action(proposed, state)

        self.assertEqual("123", adapted.source_entity_id)
        self.assertEqual("hand_card_left", adapted.postcondition)
        manual = adapted.manual_action
        self.assertIsInstance(manual, PlayCardAction)
        self.assertEqual(0, manual.sub_option_index)
        self.assertEqual(2, manual.sub_option_count)

    def test_resolves_second_sub_option_to_index_one(self):
        proposed = self._parse(
            "打出1号位法术\n选择卡牌\n猎鹰的灵动")
        state = self._state([self._hand_card("EDR_233", "SPELL", "123")])

        adapted = adapt_action(proposed, state)

        self.assertEqual(1, adapted.manual_action.sub_option_index)
        self.assertEqual(2, adapted.manual_action.sub_option_count)

    def test_unknown_choice_name_is_rejected(self):
        proposed = self._parse(
            "打出1号位法术\n选择卡牌\n不存在的分支")
        state = self._state([self._hand_card("EDR_233", "SPELL", "123")])

        with self.assertRaisesRegex(
                RecommendationStateError, "sub_option_not_found"):
            adapt_action(proposed, state)

    def test_missing_sub_options_is_rejected(self):
        proposed = self._parse(
            "打出1号位法术\n选择卡牌\n群狼的力量")
        state = self._state([self._hand_card("PLAIN_1", "SPELL", "999")])
        state.sub_options_by_parent = {}

        with self.assertRaisesRegex(
                RecommendationStateError, "sub_options_unavailable"):
            adapt_action(proposed, state)

    def test_non_spell_card_with_choice_is_rejected(self):
        proposed = self._parse(
            "打出1号位随从\n选择卡牌\n群狼的力量")
        state = self._state(
            [self._hand_card("EDR_233", "MINION", "123")])

        with self.assertRaisesRegex(
                RecommendationStateError, "choose_one_unsupported"):
            adapt_action(proposed, state)


class ChooseOneExecutorTests(unittest.TestCase):
    def _controller(self, clicks, sleeps):
        return ManualController(
            output_func=lambda _message: None,
            executor=ClickExecutor(
                click_module=clicks,
                sleep_func=sleeps.append,
                action_context=nullcontext,
            ),
        )

    def _hand_cards(self, count):
        return [
            SimpleNamespace(
                card_id="SPELL_1", cardtype="SPELL",
                entity_id=f"entity-{index}", name="占位")
            for index in range(count)
        ]

    def _state(self, cards):
        return SimpleNamespace(
            game_num_turns_in_play=3,
            is_my_turn=True,
            my_hand_cards=cards,
            my_minions=[],
            my_locations=[],
            my_board_slot_num=0,
            oppo_minions=[],
            oppo_board_slot_num=0,
        )

    def _action(self, hand_index, option_index, option_count,
                hand_entity_id="entity-3"):
        return PlayCardAction(
            hand_index=hand_index,
            card_id="EDR_233",
            cardtype="SPELL",
            hand_entity_id=hand_entity_id,
            sub_option_index=option_index,
            sub_option_count=option_count,
        )

    def test_executes_card_then_center_then_choice(self):
        cards = self._hand_cards(4)
        cards[3] = SimpleNamespace(
            card_id="EDR_233", cardtype="SPELL",
            entity_id="entity-3", name="森林之灵")
        state = self._state(cards)
        clicks = RecordingClickModule()
        sleeps = []

        result = self._controller(clicks, sleeps).execute(
            self._action(3, 0, 2), state)

        self.assertTrue(result.executed, result.message)
        self.assertEqual([
            ("choose_card", 3, 4),
            ("click_middle",),
            ("choose_discover_card", 0, 2),
        ], clicks.events)
        self.assertEqual([CHOOSE_ONE_PANEL_DELAY], sleeps)

    def test_small_hand_waits_for_fan_before_selecting(self):
        state = self._state(self._hand_cards(1))
        state.my_hand_cards[0] = SimpleNamespace(
            card_id="EDR_233", cardtype="SPELL",
            entity_id="entity-0", name="森林之灵")
        clicks = RecordingClickModule()
        sleeps = []

        result = self._controller(clicks, sleeps).execute(
            self._action(0, 1, 2, hand_entity_id="entity-0"), state)

        self.assertTrue(result.executed, result.message)
        self.assertEqual([0.3, CHOOSE_ONE_PANEL_DELAY], sleeps)

    def test_rejects_out_of_range_choice_index(self):
        state = self._state(self._hand_cards(4))
        clicks = RecordingClickModule()
        sleeps = []

        result = self._controller(clicks, sleeps).execute(
            self._action(3, 5, 2), state)

        self.assertFalse(result.executed)
        self.assertEqual([], clicks.events)
        self.assertEqual([], sleeps)


class ChooseOneStrategyStateTests(unittest.TestCase):
    @staticmethod
    def _spell_entity(entity_id, card_id, controller="2"):
        card = CardEntity(card_id)
        card.set_tag("CARDTYPE", "SPELL")
        card.set_tag("CONTROLLER", controller)
        card.set_tag("ZONE", "HAND")
        card.set_tag("ZONE_POSITION", "4")
        return card

    @staticmethod
    def _sub_entity(entity_id, card_id, parent, controller):
        card = CardEntity(card_id)
        card.set_tag("ZONE", "SETASIDE")
        card.set_tag("PARENT_CARD", parent)
        card.set_tag("CONTROLLER", controller)
        return card

    def _state(self, entity_dict):
        my_entity = SimpleNamespace(query_tag=lambda _tag: "0")
        return StrategyState(SimpleNamespace(
            is_end=False,
            is_my_turn=True,
            game_num_turns_in_play=3,
            my_entity=my_entity,
            discover_choice_count=None,
            hand_entry_count=0,
            start_of_game_card_count=0,
            entity_dict=entity_dict,
            my_player_id="2",
            is_my_entity=lambda entity: (
                entity.query_tag("CONTROLLER") == "2"),
        ))

    def test_builds_sorted_sub_options_by_parent(self):
        entity_dict = {
            # 故意乱序插入，验证按实体 id(=subOption 顺序)升序排列。
            "125": self._sub_entity("125", "EDR_233b", "123", "2"),
            "124": self._sub_entity("124", "EDR_233a", "123", "2"),
            "123": self._spell_entity("123", "EDR_233"),
            # 敌方手里的同款抉择：不该算进我方子选项。
            "500": self._sub_entity("500", "EDR_233a", "123", "1"),
            # 无父卡的衍生占位：不该算。
            "501": self._sub_entity("501", "TOKEN_1", "0", "2"),
        }

        state = self._state(entity_dict)

        children = state.sub_options_by_parent.get("123", ())
        self.assertEqual(2, len(children))
        self.assertEqual(
            ["EDR_233a", "EDR_233b"], [child.card_id for child in children])
        self.assertEqual(
            ["124", "125"], [child.entity_id for child in children])
        # 父卡仍是手牌里那张法术。
        self.assertEqual(
            ["123"], [card.entity_id for card in state.my_hand_cards])


if __name__ == "__main__":
    unittest.main()
