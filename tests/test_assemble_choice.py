import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import patch

import click as hearthstone_click
from log_op import parse_line
from log_state import LogState, update_state
from manual_controller import (
    ActionExecutionResult, AssembleChoiceAction, ClickExecutor,
    DiscoverChoiceAction, ManualController,
)
from src.game_state.assemble import is_assemble_card, is_assemble_choice
from src.game_state.recommendation_adapter import adapt_action
from src.parser.recommendation_parser import (
    RecommendationParseError, RecommendationParser,
)
from src.recommendation_models import ActionKind, OcrEvidence


# 旧版盒子文案（2026-10-24 06:49 截图）：拼装面板上盒子只能说「选择我方3号位卡牌」。
BOX_TEXT = "选择我方3号位卡牌\n伦萨克大王"
# 新版盒子文案（2026-10-29 23:01 截图）：直接给「卡位+词条号」的点击序列。
BOX_TEXT_NEW = "进行拼装【右2中1】\n鸭妈妈"


def evidence(text):
    return OcrEvidence(
        frame_id="frame", created_at=0.0, lines=(),
        normalized_text=text, confidence=0.95,
        backend="test", preprocessing="test")


class AssembleChoiceTests(unittest.TestCase):
    # ------------------------------------------------------------ 日志识别
    def feed(self, state, text):
        parsed = parse_line("D 06:33:58.5951622 " + text)
        self.assertIsNotNone(parsed, text)
        update_state(state, parsed)

    def choice(self, source_card="BE_114", count=4):
        """实测形态：拼装的选择框有 4 个「半张」候选实体。"""
        state = LogState()
        state.my_player_id = "1"
        self.feed(state, "GameState.DebugPrintEntityChoices() - "
                         "id=8 Player=TestUser#12345 TaskList=500 "
                         "ChoiceType=GENERAL CountMin=1 CountMax=1")
        self.feed(state, "GameState.DebugPrintEntityChoices() -   "
                         f"Source=[entityName=暗影塑形 id=105 zone=HAND "
                         f"zonePos=3 cardId={source_card} player=1]")
        for index in range(count):
            self.feed(state,
                      "GameState.DebugPrintEntityChoices() - "
                      f"  Entities[{index}]=[entityName=伦萨克大王 "
                      f"id={176 + index} zone=SETASIDE zonePos=0 "
                      f"cardId=CS3_025 player=1]")
        self.feed(state, "ChoiceCardMgr.WaitThenShowChoices() - id=8 BEGIN")
        return state

    def test_source_line_is_parsed(self):
        parsed = parse_line(
            "D 06:33:58.5951622 GameState.DebugPrintEntityChoices() -   "
            "Source=[entityName=暗影塑形 id=105 zone=HAND zonePos=3 "
            "cardId=BE_114 player=1]")
        self.assertIsNotNone(parsed)
        self.assertEqual("105", parsed.info_dict["entity_id"])
        self.assertEqual("BE_114", parsed.info_dict["card_id"])

    def test_assemble_choice_is_detected_and_cleared(self):
        state = self.choice()
        self.assertEqual(4, state.discover_choice_count)
        self.assertTrue(state.general_choice_assemble)
        self.assertEqual("BE_114", state.general_choice_source_card)
        # 非拼装来源（普通发现）不能被误判。
        ordinary = self.choice(source_card="JAIL_EVENT_100")
        self.assertFalse(ordinary.general_choice_assemble)
        # 选完就清掉，避免下一轮误判。
        self.feed(state, "GameState.SendChoices() - id=8 ChoiceType=GENERAL")
        self.assertFalse(state.general_choice_assemble)
        self.assertIsNone(state.discover_choice_count)

    def test_card_table_marks_assemble_cards(self):
        self.assertTrue(is_assemble_card("BE_114"))   # 暗影塑形
        self.assertTrue(is_assemble_card("BE_044"))   # F3TCH3R寻回机
        self.assertFalse(is_assemble_card("CS3_025"))  # 伦萨克大王（候选）
        self.assertFalse(is_assemble_card("BE_013"))  # 古神

    # ------------------------------------------------------------ 适配
    def parse(self, text=BOX_TEXT, turn_number=13, revision=7):
        return RecommendationParser().parse(
            evidence(text), turn_number, revision)

    def state(self, assemble=True, count=4):
        return SimpleNamespace(
            game_num_turns_in_play=13, is_my_turn=True, my_player_id="1",
            my_minions=[], oppo_minions=[], my_locations=[], oppo_locations=[],
            my_hand_cards=[], my_hero=None, oppo_hero=None,
            discover_choice_count=count, assemble_choice=assemble,
            power_options={}, titan_ability_choices={},
        )

    def test_box_text_parses_as_discover(self):
        proposed = self.parse()
        self.assertEqual(ActionKind.CHOOSE_DISCOVER, proposed.action)
        self.assertEqual(3, proposed.source.index)
        # 盒子附带的牌名行（伦萨克大王）会被规范化丢掉。
        self.assertEqual("选择我方3号位卡牌", proposed.normalized_instruction)

    def test_assemble_panel_ignores_box_index(self):
        adapted = adapt_action(self.parse(), self.state())
        self.assertIsInstance(adapted.manual_action, AssembleChoiceAction)
        self.assertEqual("choice_resolved", adapted.postcondition)
        # 旧文案没有卡位信息 → picks 为空，走默认「右卡第 2 条」。
        self.assertEqual((), adapted.manual_action.picks)

    def test_normal_discover_panel_keeps_its_layout(self):
        adapted = adapt_action(self.parse(), self.state(assemble=False))
        self.assertIsInstance(adapted.manual_action, DiscoverChoiceAction)
        self.assertEqual(2, adapted.manual_action.choice_index)
        self.assertEqual(4, adapted.manual_action.choice_count)

    # ------------------------------------------------------------ 新文案：卡位+词条号
    def test_new_box_text_parses_pick_sequence(self):
        proposed = self.parse(BOX_TEXT_NEW)
        self.assertEqual(ActionKind.CHOOSE_DISCOVER, proposed.action)
        self.assertEqual((("右", 2), ("中", 1)), proposed.assemble_picks)
        # 牌名说明行（鸭妈妈）照旧被规范化丢掉，动作行原样保留。
        self.assertEqual("进行拼装【右2中1】", proposed.normalized_instruction)

    def test_new_box_text_needs_no_log_state(self):
        # 盒子点名了卡位，就不该再依赖日志里的候选实体数/拼装标记。
        adapted = adapt_action(
            self.parse(BOX_TEXT_NEW), self.state(assemble=False, count=None))
        self.assertIsInstance(adapted.manual_action, AssembleChoiceAction)
        self.assertEqual((("右", 2), ("中", 1)),
                         adapted.manual_action.picks)
        # 流程层会把 note 打成一行 [提示]，日志里能直接看出按什么点的。
        self.assertIn("右2中1", adapted.note)

    def test_bracket_variants_and_split_lines(self):
        # OCR 把【】读成 [] 也要认。
        self.assertEqual(
            (("右", 2), ("中", 1)),
            self.parse("进行拼装[右2中1]").assemble_picks)
        # 括号里的内容被切到单独一行也要认。
        self.assertEqual(
            (("左", 1),), self.parse("进行拼装\n【左1】").assemble_picks)
        # 单条词条（只有一个卡位）同样可用。
        self.assertEqual(
            (("左", 1),), self.parse("进行拼装【左1】").assemble_picks)

    def test_missing_or_invalid_picks_are_rejected(self):
        with self.assertRaises(RecommendationParseError) as missing:
            self.parse("进行拼装\n鸭妈妈")
        self.assertIn("assemble_pick_required", str(missing.exception))
        with self.assertRaises(RecommendationParseError) as out_of_range:
            self.parse("进行拼装【右3】")
        self.assertIn("assemble_entry_out_of_range", str(out_of_range.exception))
        with self.assertRaises(RecommendationParseError) as garbage:
            self.parse("进行拼装【随便点】")
        self.assertIn("assemble_pick_unsupported", str(garbage.exception))

    # ------------------------------------------------------------ 点击
    def test_click_sequence_is_second_card_then_confirm(self):
        clicks = []
        with patch.object(hearthstone_click, "left_click",
                          side_effect=lambda x, y: clicks.append((x, y))), \
                patch.object(hearthstone_click, "right_click"), \
                patch.object(hearthstone_click, "rand_sleep"):
            hearthstone_click.choose_assemble_option()
        # 旧文案默认：右侧候选卡第 2 条词条 → 拼装确认。
        self.assertEqual([(1336, 600), (930, 790)], clicks)

    def test_click_sequence_follows_box_pick_order(self):
        clicks = []
        with patch.object(hearthstone_click, "left_click",
                          side_effect=lambda x, y: clicks.append((x, y))), \
                patch.object(hearthstone_click, "right_click"), \
                patch.object(hearthstone_click, "rand_sleep"):
            hearthstone_click.choose_assemble_entries(
                (("右", 2), ("中", 1)))
        # 盒子顺序：右卡第 2 条 → 中卡第 1 条 → 拼装确认。
        self.assertEqual([(1336, 600), (916, 481), (930, 790)], clicks)

    def test_controller_executes_assemble_choice(self):
        events = []
        executor = ClickExecutor(click_module=hearthstone_click,
                                 action_context=nullcontext,
                                 sleep_func=lambda _: None)
        with patch.object(hearthstone_click, "choose_assemble_option",
                          side_effect=lambda: events.append("assemble")):
            result = ManualController(
                executor=executor, output_func=lambda _: None).execute(
                    AssembleChoiceAction(turn_number=13), self.state())
        self.assertTrue(result.executed, result.message)
        self.assertEqual(["assemble"], events)
        self.assertIn("拼装", result.message)

    def test_controller_passes_box_picks(self):
        seen = []
        executor = ClickExecutor(click_module=hearthstone_click,
                                 action_context=nullcontext,
                                 sleep_func=lambda _: None)
        with patch.object(hearthstone_click, "choose_assemble_entries",
                          side_effect=lambda picks: seen.append(tuple(picks))):
            result = ManualController(
                executor=executor, output_func=lambda _: None).execute(
                    AssembleChoiceAction(picks=(("右", 2), ("中", 1))),
                    self.state())
        self.assertTrue(result.executed, result.message)
        self.assertEqual([(("右", 2), ("中", 1))], seen)
        self.assertIn("右2中1", result.message)

    def test_assemble_entry_coordinates_cover_three_cards(self):
        self.assertEqual((516, 481), hearthstone_click.assemble_entry_xy("左", 1))
        self.assertEqual((516, 600), hearthstone_click.assemble_entry_xy("左", 2))
        self.assertEqual((916, 481), hearthstone_click.assemble_entry_xy("中", 1))
        self.assertEqual((916, 600), hearthstone_click.assemble_entry_xy("中", 2))
        self.assertEqual((1336, 481), hearthstone_click.assemble_entry_xy("右", 1))
        self.assertEqual((1336, 600), hearthstone_click.assemble_entry_xy("右", 2))


class _NullExecutor:
    def __getattr__(self, name):
        raise AssertionError(f"不应调用鼠标执行器：{name}")


if __name__ == "__main__":
    unittest.main()
