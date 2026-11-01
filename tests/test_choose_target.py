import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import patch

import click as hearthstone_click
from log_op import parse_line
from log_state import LogState, update_state
from manual_controller import (
    ActionExecutionResult, ChooseTargetAction, ClickExecutor,
    DiscoverChoiceAction, ManualController,
)
from src.game_state.recommendation_adapter import (
    RecommendationStateError, adapt_action,
)
from src.parser.recommendation_parser import RecommendationParser
from src.recommendation_models import ActionKind, OcrEvidence


# 实测盒子文案（2026-10-24 08:57:50）：滑行者巨怪战吼选完「克苏恩的凝视」后，
# 选择框变成 ChoiceType=TARGET，盒子接着说「选择对方2号位卡牌」。
BOX_TEXT = "选择对方2号位卡牌\n小鬼奶奶"


def evidence(text):
    return OcrEvidence(
        frame_id="frame", created_at=0.0, lines=(),
        normalized_text=text, confidence=0.95,
        backend="test", preprocessing="test")


def minion(card_id, entity_id, zone_pos, name="随从", **overrides):
    values = dict(card_id=card_id, entity_id=entity_id, zone_pos=zone_pos,
                  name=name, cardtype="MINION", damage=0, max_health=6,
                  exhausted=0, attack=2)
    values.update(overrides)
    return SimpleNamespace(**values)


class ChooseTargetTests(unittest.TestCase):
    # ------------------------------------------------------------ 语法
    def parse(self, text, turn_number=22, revision=7):
        return RecommendationParser().parse(
            evidence(text), turn_number, revision)

    def test_enemy_card_target_is_parsed(self):
        proposed = self.parse(BOX_TEXT)
        self.assertEqual(ActionKind.CHOOSE_TARGET, proposed.action)
        self.assertEqual("board_slot", proposed.target.kind)
        self.assertEqual("enemy", proposed.target.owner)
        self.assertEqual(2, proposed.target.index)
        # 牌名说明行（小鬼奶奶）被规范化丢弃，动作行必须保留——否则 OCR 的
        # 稳定读取会把整个面板判成「没读到字」（2026-10-24 08:57 卡了 42 秒）。
        self.assertEqual("选择对方2号位卡牌",
                         proposed.normalized_instruction)

    def test_normalize_keeps_target_action_line(self):
        text = RecommendationParser.normalize_action_text(
            "打法参考A\n选择对方2号位卡牌\n小鬼奶奶")
        self.assertEqual("选择对方2号位卡牌", text)

    def test_enemy_hero_target_is_parsed(self):
        proposed = self.parse("选择对方英雄")
        self.assertEqual(ActionKind.CHOOSE_TARGET, proposed.action)
        self.assertEqual("hero", proposed.target.kind)
        self.assertEqual("enemy", proposed.target.owner)

    def test_own_card_choice_stays_a_discover(self):
        proposed = self.parse("选择我方3号位卡牌\n伦萨克大王")
        self.assertEqual(ActionKind.CHOOSE_DISCOVER, proposed.action)

    # ------------------------------------------------------------ 日志状态
    def feed(self, state, text):
        parsed = parse_line("D 08:57:50.4586738 " + text)
        self.assertIsNotNone(parsed, text)
        update_state(state, parsed)

    def target_choice(self, source_player="1", ready=True):
        state = LogState()
        state.my_player_id = "1"
        self.feed(state, "GameState.DebugPrintEntityChoices() - "
                         "id=17 Player=TestUser#12345 TaskList= "
                         "ChoiceType=TARGET CountMin=1 CountMax=1")
        self.feed(state, "GameState.DebugPrintEntityChoices() -   "
                         "Source=[entityName=滑行者巨怪 id=35 zone=PLAY "
                         f"zonePos=1 cardId=BE_204 player={source_player}]")
        self.feed(state, "GameState.DebugPrintEntityChoices() - "
                         "  Entities[0]=[entityName=小鬼奶奶 id=173 zone=PLAY "
                         "zonePos=2 cardId=JAIL_399t1 player=2]")
        self.feed(state, "GameState.DebugPrintEntityChoices() - "
                         "  Entities[1]=[entityName=晶石魔像 id=56 zone=PLAY "
                         "zonePos=1 cardId=BE_861 player=2]")
        if ready:
            self.feed(state, "ChoiceCardMgr.WaitThenShowChoices() - "
                             "id=17 BEGIN")
        return state

    def test_target_choice_is_tracked_and_not_counted_as_discover(self):
        state = self.target_choice()
        self.assertTrue(state.target_choice_open)
        self.assertIsNone(state.discover_choice_count)
        self.feed(state, "GameState.SendChoices() - id=17 ChoiceType=TARGET")
        self.assertFalse(state.target_choice_open)
        # 对手的指向选择不能当成我方在选目标。
        self.assertFalse(self.target_choice(source_player="2").target_choice_open)

    def general_choice(self):
        state = LogState()
        state.my_player_id = "1"
        self.feed(state, "GameState.DebugPrintEntityChoices() - "
                         "id=8 Player=TestUser#12345 TaskList=500 "
                         "ChoiceType=GENERAL CountMin=1 CountMax=1")
        self.feed(state, "GameState.DebugPrintEntityChoices() -   "
                         "Source=[entityName=暗影塑形 id=105 zone=HAND "
                         "zonePos=3 cardId=BE_114 player=1]")
        for index in range(4):
            self.feed(state, "GameState.DebugPrintEntityChoices() - "
                             f"  Entities[{index}]=[entityName=克苏恩的凝视 "
                             f"id={251 + index} zone=SETASIDE zonePos=0 "
                             "cardId=BE_013t3 player=1]")
        self.feed(state, "ChoiceCardMgr.WaitThenShowChoices() - id=8 BEGIN")
        return state

    def test_general_choice_still_counts_as_discover(self):
        state = self.general_choice()
        self.assertEqual(4, state.discover_choice_count)
        self.assertFalse(state.target_choice_open)

    # ------------------------------------------------------------ 适配
    def state(self, **overrides):
        values = dict(
            game_num_turns_in_play=22, is_my_turn=True, my_player_id="1",
            my_minions=[minion("BE_204", "35", 1, "滑行者巨怪")],
            oppo_minions=[minion("BE_861", "56", 1, "晶石魔像"),
                          minion("JAIL_399t1", "173", 2, "小鬼奶奶")],
            my_locations=[], oppo_locations=[], my_hand_cards=[],
            my_hero=SimpleNamespace(entity_id="78"), oppo_hero=SimpleNamespace(entity_id="80"),
            discover_choice_count=None, target_choice_open=True,
            assemble_choice=False, power_options={},
            titan_ability_choices={},
        )
        values.update(overrides)
        return SimpleNamespace(**values)

    def test_enemy_card_target_is_adapted(self):
        adapted = adapt_action(self.parse(BOX_TEXT), self.state())
        manual = adapted.manual_action
        self.assertIsInstance(manual, ChooseTargetAction)
        self.assertEqual("enemy", manual.target.side)
        self.assertEqual("minion", manual.target.kind)
        self.assertEqual(1, manual.target.index)      # 对方2号位 → collection 1
        self.assertEqual("173", adapted.target_entity_id)
        self.assertEqual("target_chosen", adapted.postcondition)

    def test_out_of_range_target_is_rejected(self):
        with self.assertRaisesRegex(RecommendationStateError,
                                    "board_slot_out_of_range"):
            adapt_action(self.parse("选择对方5号位卡牌"), self.state())

    def test_friendly_target_is_adapted_when_no_discover_panel(self):
        # 指向选择里盒子说「选择我方1号位卡牌」= 点我方场面第 1 个。
        adapted = adapt_action(
            self.parse("选择我方1号位卡牌\n滑行者巨怪"),
            self.state(target_choice_open=True))
        self.assertIsInstance(adapted.manual_action, ChooseTargetAction)
        self.assertEqual("friendly", adapted.manual_action.target.side)
        self.assertEqual("35", adapted.target_entity_id)

    def test_discover_panel_is_not_hijacked_by_target_mode(self):
        adapted = adapt_action(
            self.parse("选择我方2号位卡牌\n克苏恩的凝视"),
            self.state(discover_choice_count=4, target_choice_open=True))
        self.assertIsInstance(adapted.manual_action, DiscoverChoiceAction)
        self.assertEqual(1, adapted.manual_action.choice_index)

    # ------------------------------------------------------------ 执行
    def test_controller_clicks_the_target(self):
        state = self.state()
        adapted = adapt_action(self.parse(BOX_TEXT), state)
        events = []
        executor = ClickExecutor(click_module=hearthstone_click,
                                 action_context=nullcontext,
                                 sleep_func=lambda _: None)
        with patch.object(hearthstone_click, "choose_opponent_minion",
                          side_effect=lambda i, n: events.append(("target", i, n))):
            result = ManualController(
                executor=executor, output_func=lambda _: None).execute(
                    adapted.manual_action, state)
        self.assertTrue(result.executed, result.message)
        self.assertEqual([("target", 1, 2)], events)
        self.assertIn("已选择目标", result.message)

    def test_stale_target_is_rejected(self):
        state = self.state()
        adapted = adapt_action(self.parse(BOX_TEXT), state)
        state.oppo_minions = state.oppo_minions[:1]
        result = ManualController(executor=NullExecutor()).execute(
            adapted.manual_action, state)
        self.assertFalse(result.executed)
        self.assertIn("选择目标已经不存在", result.message)

    # ------------------------------------------------------------ 结果判定
    def test_postcondition_detects_target_choice_closed(self):
        class Flow:
            _postcondition = staticmethod(
                __import__("src.flow.recommendation_flow",
                           fromlist=["RecommendationFlow"]).RecommendationFlow._postcondition)
        before = SimpleNamespace(target_choice_open=True, my_minions=[],
                                 oppo_minions=[minion("JAIL_399t1", "173", 2,
                                                      damage=0)],
                                 my_hand_cards=[])
        after = SimpleNamespace(target_choice_open=False, my_minions=[],
                                oppo_minions=[minion("JAIL_399t1", "173", 2,
                                                     damage=5)],
                                my_hand_cards=[])
        self.assertTrue(Flow._postcondition(
            "target_chosen", before, after, None, "173"))
        self.assertFalse(Flow._postcondition(
            "target_chosen", before, before, None, "173"))


class NullExecutor:
    def __getattr__(self, name):
        raise AssertionError(f"不应调用鼠标执行器：{name}")


if __name__ == "__main__":
    unittest.main()