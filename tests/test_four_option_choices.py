import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import patch

import click as hearthstone_click
from log_op import parse_line
from log_state import LogState, update_state
from manual_controller import ClickExecutor, ManualController
from src.flow.recommendation_flow import RecommendationFlow
from src.game_state.recommendation_adapter import adapt_action, RecommendationStateError
from src.parser.recommendation_parser import RecommendationParser


class FourOptionChoiceTests(unittest.TestCase):
    def feed(self, state, text):
        parsed = parse_line("D 21:29:00.0000000 " + text)
        self.assertIsNotNone(parsed, text)
        update_state(state, parsed)

    def choice(self, count, player="1"):
        state = LogState()
        state.my_player_id = "1"
        self.feed(state, "GameState.DebugPrintEntityChoices() - id=8 Player=1 ChoiceType=GENERAL")
        for index in range(count):
            self.feed(state,
                "GameState.DebugPrintEntityChoices() - "
                f"Entities[{index}]=[entityName=选项 id={100 + index} "
                f"zone=SETASIDE zonePos=0 cardId=OPTION_{index} player={player}]")
        self.feed(state, "ChoiceCardMgr.WaitThenShowChoices() - id=8 BEGIN")
        return state

    def proposed(self, index, alternate=False):
        text = (f"选第{index}个选项" if alternate
                else f"选择我方{index}号位卡牌")
        return RecommendationParser().parse(SimpleNamespace(
            frame_id="frame", normalized_text="打法参考A\n" + text,
            confidence=.99), turn_number=3, log_revision=7)

    def test_four_choices_from_log_reach_correct_screen_coordinates(self):
        for index, x in enumerate((400, 775, 1150, 1525), start=1):
            with self.subTest(index=index):
                log = self.choice(4)
                self.assertEqual(4, log.discover_choice_count)
                state = SimpleNamespace(game_num_turns_in_play=3, is_my_turn=True,
                    discover_choice_count=log.discover_choice_count)
                adapted = adapt_action(self.proposed(index), state)
                controller = ManualController(output_func=lambda _: None,
                    executor=ClickExecutor(click_module=hearthstone_click,
                        action_context=nullcontext))
                with patch.object(hearthstone_click, "left_click") as click, \
                        patch.object(hearthstone_click, "rand_sleep"):
                    result = controller.execute(adapted.manual_action, state)
                self.assertTrue(result.executed, result.message)
                click.assert_called_once_with(x, 500)

    def test_spoken_option_text_accepts_fourth_choice(self):
        proposed = self.proposed(4, alternate=True)
        self.assertEqual(4, proposed.source.index)
        self.assertEqual("discover_slot", proposed.source.kind)

    def test_legacy_layouts_keep_their_coordinates(self):
        for count, positions, y in (
                (1, (960,), 540), (2, (760, 1160), 500),
                (3, (560, 960, 1360), 500)):
            for index, x in enumerate(positions):
                with self.subTest(count=count, index=index), \
                        patch.object(hearthstone_click, "left_click") as click, \
                        patch.object(hearthstone_click, "rand_sleep"):
                    hearthstone_click.choose_discover_card(index, count)
                    click.assert_called_once_with(x, y)

    def test_fourth_option_cannot_be_selected_on_three_option_screen(self):
        with self.assertRaisesRegex(RecommendationStateError, "discover_slot_out_of_range"):
            adapt_action(self.proposed(4), SimpleNamespace(
                game_num_turns_in_play=3, discover_choice_count=3))

    def test_resolving_four_choices_without_drawing_a_card_is_detected(self):
        log = self.choice(4)
        before = SimpleNamespace(discover_choice_count=log.discover_choice_count,
                                 my_hand_cards=[])
        self.feed(log, "GameState.SendChoices() - id=8 ChoiceType=GENERAL")
        after = SimpleNamespace(discover_choice_count=log.discover_choice_count,
                                my_hand_cards=[])
        self.assertTrue(RecommendationFlow._postcondition("choice_resolved", before, after))

    def test_next_three_option_choice_does_not_reuse_four_option_layout(self):
        log = self.choice(4)
        self.assertEqual(4, log.discover_choice_count)
        self.feed(log, "GameState.DebugPrintEntityChoices() - id=9 Player=1 ChoiceType=GENERAL")
        self.assertIsNone(log.discover_choice_count)
        for index in range(3):
            self.feed(log, "GameState.DebugPrintEntityChoices() - "
                f"Entities[{index}]=[entityName=选项 id={200 + index} "
                "zone=SETASIDE zonePos=0 cardId=OPTION player=1]")
        self.feed(log, "ChoiceCardMgr.WaitThenShowChoices() - id=9 BEGIN")
        self.assertEqual(3, log.discover_choice_count)

    def test_enemy_and_unsupported_choice_counts_are_not_exposed(self):
        self.assertIsNone(self.choice(4, player="2").discover_choice_count)
        self.assertIsNone(self.choice(5).discover_choice_count)
