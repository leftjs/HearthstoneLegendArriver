import unittest
from contextlib import nullcontext
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import click as hearthstone_click
from log_op import parse_line
from log_state import LogState, update_state
from manual_controller import (
    ActionExecutionResult, ClickExecutor, DiscoverChoiceAction,
    ManualController, TitanAbilityAction,
)
from src.flow.recommendation_flow import (
    FlowStepStatus, RecommendationFlow, ResultStatus,
)
from src.game_state.recommendation_adapter import (
    RecommendationStateError, adapt_action,
)
from src.parser.recommendation_parser import (
    RecommendationParseError, RecommendationParser,
)
from src.recommendation_models import (
    ActionKind, FrameEvidence, OcrEvidence,
)
from src.safety.recommendation_validator import RecommendationValidator


# 用户给的盒子文案：操作1号位泰坦 / 兵主 / 目标是对方2号位 / 穆克拉先生 /
# 选择卡牌 / 鲜血符文。卡牌名与目标名是盒子附带的说明行，应被规范化丢弃。
BOX_TEXT = ("操作1号位泰坦\n兵主\n目标是对方2号位\n穆克拉先生\n"
            "选择卡牌\n鲜血符文")
NORMALIZED_TEXT = "操作1号位泰坦\n目标是对方2号位\n选择卡牌\n鲜血符文"


def frame(frame_id, exact_hash):
    return FrameEvidence(
        frame_id=frame_id,
        captured_at=0.0,
        desktop_size=(1920, 1080),
        dpi=96,
        window_handle=1,
        foreground=True,
        recommendation_roi=(7, 32, 278, 970),
        exact_hash=exact_hash,
        perceptual_hash="0" * 64,
        panel_visible=True,
    )


def evidence(frame_id, text):
    return OcrEvidence(
        frame_id=frame_id,
        created_at=0.0,
        lines=(),
        normalized_text=text,
        confidence=0.95,
        backend="test",
        preprocessing="test",
    )


class SamePanelCapture:
    """点击前后截图相同：用来验证「面板没更新时等盒子」而不是重复点击。"""

    def __init__(self):
        self.frames = iter((
            frame("stable", "same"),
            frame("current", "same"),
            frame("after", "same"),
        ))

    def capture(self, ocr_panel_ok=False):
        return next(self.frames)

    @staticmethod
    def crop_recommendation(current_frame):
        return current_frame


class FixedReader:
    def __init__(self, text=BOX_TEXT):
        # 与生产一致：reader 读取时会先做同一步文本规范化。
        self.text = RecommendationParser.normalize_action_text(text)

    def read(self, frame_supplier, _roi_supplier):
        return evidence(frame_supplier().frame_id, self.text)

    def read_frame(self, current_frame, _roi_supplier):
        return evidence(current_frame.frame_id, self.text)


class RecordingController:
    def __init__(self):
        self.actions = []

    def execute(self, action, _state):
        self.actions.append(action)
        return ActionExecutionResult(True, "executed")


def minion(card_id, entity_id, zone_pos, name="随从", **overrides):
    values = dict(card_id=card_id, entity_id=entity_id, zone_pos=zone_pos,
                  name=name, cardtype="MINION", damage=0, max_health=10,
                  exhausted=0, attack=0)
    values.update(overrides)
    return SimpleNamespace(**values)


class TitanAbilityTests(unittest.TestCase):
    # ------------------------------------------------------------ 日志工具
    def feed(self, log, text):
        parsed = parse_line(
            "D 23:39:00.2647248 GameState.DebugPrintOptions() - " + text)
        self.assertIsNotNone(parsed, text)
        update_state(log, parsed)

    def options(self, player="1", source="TTN_737", error="NONE",
                sub_error="NONE"):
        """真实 Power.log 形态：泰坦打出当回合主选项带三张技能子牌。"""
        log = LogState()
        log.my_player_id = "1"
        self.feed(log, "id=68")
        self.feed(log, "  option 2 type=POWER mainEntity=[entityName=兵主 "
                       f"id=27 zone=HAND zonePos=4 cardId={source} "
                       f"player={player}] error={error} errorParam=")
        choices = {
            "TTN_737": ((0, "鲜血符文", "TTN_737t"),
                        (1, "冰霜符文", "TTN_737t3"),
                        (2, "邪恶符文", "TTN_737t1")),
            "TTN_075": ((0, "元尊之力", "TTN_075t"),
                        (1, "远古知识", "TTN_075t2"),
                        (2, "无限潜能", "TTN_075t3")),
        }.get(source, ((0, "技能甲", "OTHER_1t"),))
        for index, name, card in choices:
            self.feed(log, f"    subOption {index} entity=[entityName={name} "
                           f"id={28 + index} zone=SETASIDE zonePos=0 "
                           f"cardId={card} player={player}] "
                           f"error={sub_error if index == 0 else 'NONE'} "
                           "errorParam=")
        return log

    def state(self, log=None, **overrides):
        my_minions = [minion("TTN_737", "27", 1, "兵主")]
        oppo_minions = [minion("TIME_609t2", "63", 1, "游侠新兵温蕾萨"),
                        minion("ETC_836", "64", 2, "穆克拉先生")]
        values = dict(
            game_num_turns_in_play=9,
            is_my_turn=True,
            my_player_id="1",
            my_minions=my_minions,
            oppo_minions=oppo_minions,
            my_locations=[],
            oppo_locations=[],
            my_hand_cards=[],
            my_hero=SimpleNamespace(entity_id="80", name="巫妖王"),
            oppo_hero=SimpleNamespace(entity_id="82", name="纳萨诺斯"),
            power_options=(log.power_options if log else {}),
            titan_ability_choices=(log.titan_ability_choices if log else {}),
            discover_choice_count=None,
            my_board_slot_num=len(my_minions),
            oppo_board_slot_num=len(oppo_minions),
        )
        values.update(overrides)
        return SimpleNamespace(**values)

    def parse(self, text, turn_number=9, revision=7):
        return RecommendationParser().parse(
            evidence("frame", text), turn_number, revision)

    # ------------------------------------------------------------ 语法解析
    def test_box_text_parses_to_titan_ability(self):
        proposed = self.parse(BOX_TEXT)
        self.assertEqual(ActionKind.TITAN_ABILITY, proposed.action)
        self.assertEqual("board_slot", proposed.source.kind)
        self.assertEqual("friendly", proposed.source.owner)
        self.assertEqual(1, proposed.source.index)
        self.assertEqual("board_slot", proposed.target.kind)
        self.assertEqual("enemy", proposed.target.owner)
        self.assertEqual(2, proposed.target.index)
        self.assertEqual("鲜血符文", proposed.choice_name)
        self.assertEqual(NORMALIZED_TEXT, proposed.normalized_instruction)

    def test_ability_without_target_is_allowed(self):
        proposed = self.parse("操作1号位泰坦\n兵主\n选择卡牌\n邪恶符文")
        self.assertEqual(ActionKind.TITAN_ABILITY, proposed.action)
        self.assertIsNone(proposed.target)
        self.assertEqual("邪恶符文", proposed.choice_name)

    def test_missing_ability_name_is_not_guessed(self):
        with self.assertRaisesRegex(RecommendationParseError,
                                    "titan_ability_name_required"):
            self.parse("操作1号位泰坦\n兵主")

    def test_other_actions_are_unaffected(self):
        proposed = self.parse("操作1号位随从攻击\n目标是对方2号位")
        self.assertEqual(ActionKind.ATTACK, proposed.action)

    # ------------------------------------------------------------ 技能下标
    def test_log_learning_records_titan_ability_order(self):
        log = self.options()
        self.assertEqual(
            {0: "鲜血符文", 1: "冰霜符文", 2: "邪恶符文"},
            log.titan_ability_choices["TTN_737"])
        # 敌人（或其它玩家）的子选项不应学到自己的表里。
        self.assertEqual({}, self.options(player="2").titan_ability_choices)

    def test_power_options_resolve_ability_index_and_target(self):
        adapted = adapt_action(self.parse(BOX_TEXT), self.state(self.options()))
        manual = adapted.manual_action
        self.assertIsInstance(manual, TitanAbilityAction)
        self.assertEqual(0, manual.ability_index)
        self.assertEqual(3, manual.ability_count)
        self.assertEqual("鲜血符文", manual.ability_name)
        self.assertEqual("TTN_737", manual.card_id)
        self.assertEqual("27", manual.titan_entity_id)
        self.assertEqual("27", adapted.source_entity_id)
        self.assertEqual("64", adapted.target_entity_id)
        self.assertEqual("titan_ability_used", adapted.postcondition)
        self.assertEqual("enemy", manual.target.side)
        self.assertEqual("minion", manual.target.kind)
        self.assertEqual(1, manual.target.index)

    def test_frost_rune_needs_no_target(self):
        text = "操作1号位泰坦\n兵主\n选择卡牌\n冰霜符文"
        adapted = adapt_action(self.parse(text), self.state(self.options()))
        self.assertEqual(1, adapted.manual_action.ability_index)
        self.assertIsNone(adapted.manual_action.target)
        self.assertIsNone(adapted.target_entity_id)

    def test_learned_order_is_used_when_log_has_no_suboptions(self):
        log = self.options()
        state = self.state()
        state.power_options = {}
        state.titan_ability_choices = log.titan_ability_choices
        adapted = adapt_action(self.parse(BOX_TEXT), state)
        self.assertEqual(0, adapted.manual_action.ability_index)
        self.assertEqual(3, adapted.manual_action.ability_count)

    def test_static_fallback_covers_primus(self):
        state = self.state()
        state.power_options = {}
        state.titan_ability_choices = {}
        adapted = adapt_action(self.parse(BOX_TEXT), state)
        self.assertEqual(0, adapted.manual_action.ability_index)
        self.assertEqual(3, adapted.manual_action.ability_count)

    def test_unknown_ability_or_titan_is_never_guessed(self):
        state = self.state()
        state.power_options = {}
        state.titan_ability_choices = {}
        with self.assertRaisesRegex(RecommendationStateError,
                                    "titan_ability_options_unavailable"):
            adapt_action(self.parse(
                "操作1号位泰坦\n兵主\n选择卡牌\n不存在的技能"), state)
        other = self.state()
        other.power_options = {}
        other.titan_ability_choices = {}
        other.my_minions[0].card_id = "ETC_836"
        with self.assertRaisesRegex(RecommendationStateError,
                                    "titan_ability_options_unavailable"):
            adapt_action(self.parse(BOX_TEXT), other)

    def test_unplayable_ability_is_rejected(self):
        log = self.options(sub_error="REQ_ENOUGH_MANA")
        with self.assertRaisesRegex(RecommendationStateError,
                                    "titan_ability_unavailable"):
            adapt_action(self.parse(BOX_TEXT), self.state(log))

    def test_partially_read_ability_panel_is_not_clicked(self):
        # 日志块只读到一张技能子牌（读取不完整）时，绝不能按 1 张牌的居中卡位点。
        norgannon_text = "操作1号位泰坦\n诺甘农\n选择卡牌\n元尊之力"
        log = self.options(source="TTN_075")
        log.power_options["27"]["choices"].pop(1)
        log.power_options["27"]["choices"].pop(2)
        state = self.state(log)
        state.my_minions[0].card_id = "TTN_075"
        state.titan_ability_choices = {}
        with self.assertRaisesRegex(RecommendationStateError,
                                    "titan_ability_options_incomplete"):
            adapt_action(self.parse(norgannon_text), state)
        # 本局学到的顺序不完整、卡牌又没有静态兜底表时同样只重试。
        partial = self.state()
        partial.power_options = {}
        partial.my_minions[0].card_id = "TTN_075"
        partial.titan_ability_choices = {"TTN_075": {0: "元尊之力"}}
        with self.assertRaisesRegex(RecommendationStateError,
                                    "titan_ability_options_incomplete"):
            adapt_action(self.parse(norgannon_text), partial)
        # 兵主有实测确认的静态兜底表：日志/学习不完整时仍然点对位置。
        primus = self.state()
        primus.power_options = {}
        primus.titan_ability_choices = {"TTN_737": {0: "鲜血符文"}}
        adapted = adapt_action(self.parse(BOX_TEXT), primus)
        self.assertEqual((0, 3), (adapted.manual_action.ability_index,
                                  adapted.manual_action.ability_count))

    def test_enemy_options_cannot_be_reused(self):
        # 对手的 option/subOption 绝不能拿来当自己的技能顺序：
        # 换成没有静态兜底的泰坦后必须直接报错重试。
        log = self.options(player="2", source="TTN_075")
        state = self.state(log)
        state.my_minions[0].card_id = "TTN_075"
        with self.assertRaisesRegex(RecommendationStateError,
                                    "titan_ability_options_unavailable"):
            adapt_action(self.parse(BOX_TEXT), state)

    def test_playing_titan_from_hand_selects_its_ability(self):
        log = self.options()
        hand_card = minion("TTN_737", "27", 4, "兵主")
        state = self.state(log, my_hand_cards=[hand_card])
        proposed = self.parse(
            "打出1号位随从\n兵主\n选择卡牌\n鲜血符文\n"
            "目标是对方2号位\n穆克拉先生")
        self.assertEqual(ActionKind.PLAY_CARD, proposed.action)
        adapted = adapt_action(proposed, state)
        self.assertEqual(DiscoverChoiceAction(0, 3),
                         adapted.manual_action.choose_one)
        self.assertEqual("64", adapted.target_entity_id)

    # ------------------------------------------------------------ 鼠标点击
    def test_click_order_is_titan_then_ability_then_target(self):
        log = self.options()
        state = self.state(log)
        # 场上再放一个随从：验证泰坦的点击位置按 ZONE_POSITION 算
        # （兵主在 1 号位 → 屏幕第 0 个卡位，总卡位数 2）。
        state.my_minions.append(minion("ETC_001", "28", 2, "跟班"))
        state.my_board_slot_num = 2
        adapted = adapt_action(self.parse(BOX_TEXT), state)
        events = []
        executor = ClickExecutor(click_module=hearthstone_click,
                                 action_context=nullcontext,
                                 sleep_func=lambda _: None)
        with patch.object(hearthstone_click, "cancel_click",
                          side_effect=lambda: events.append("cancel")), \
                patch.object(hearthstone_click, "choose_my_board_entity",
                             side_effect=lambda i, n: events.append(("titan", i, n))), \
                patch.object(hearthstone_click, "choose_titan_ability",
                             side_effect=lambda i, n: events.append(("ability", i, n))), \
                patch.object(hearthstone_click, "choose_opponent_minion",
                             side_effect=lambda i, n: events.append(("target", i, n))):
            result = ManualController(
                executor=executor, output_func=lambda _: None).execute(
                    adapted.manual_action, state)
        self.assertTrue(result.executed, result.message)
        self.assertEqual(
            ["cancel", ("titan", 0, 2), ("ability", 0, 3), ("target", 1, 2)],
            events)
        self.assertIn("鲜血符文", result.message)

    def test_stale_titan_or_ability_is_rejected_before_clicking(self):
        log = self.options()
        state = self.state(log)
        adapted = adapt_action(self.parse(BOX_TEXT), state)
        controller = ManualController(executor=NullExecutor(),
                                      output_func=lambda _: None)
        changed_card = replace(adapted.manual_action, card_id="OTHER")
        result = controller.execute(changed_card, state)
        self.assertFalse(result.executed)
        self.assertIn("泰坦内容已经变化", result.message)
        bad_index = replace(adapted.manual_action, ability_index=5)
        result = controller.execute(bad_index, state)
        self.assertFalse(result.executed)
        self.assertIn("泰坦技能选项已经失效", result.message)
        state.oppo_minions = state.oppo_minions[:1]
        result = controller.execute(adapted.manual_action, state)
        self.assertFalse(result.executed)
        self.assertIn("泰坦技能目标已经不存在", result.message)

    # ------------------------------------------------------------ 流程判定
    def test_flow_waits_for_box_update_without_repeating_the_click(self):
        log = self.options()
        before = self.state(log)
        after = self.state(log)
        after.my_minions[0].exhausted = 1
        states = iter(((before, 10), (before, 10), (after, 11)))
        controller = RecordingController()
        flow = RecommendationFlow(
            capture=SamePanelCapture(),
            reader=FixedReader(),
            parser=RecommendationParser(),
            state_supplier=lambda: next(states),
            adapter=adapt_action,
            validator=RecommendationValidator(),
            controller=controller,
            post_action_delay=0.0,
        )

        result = flow.run_player_turn_step()

        self.assertEqual(FlowStepStatus.OBSERVE, result.status)
        self.assertEqual("waiting_recommendation_update", result.diagnostics)
        self.assertEqual(1, len(controller.actions))
        self.assertIsInstance(controller.actions[0], TitanAbilityAction)
        # 泰坦技能不标记「已消费」：盒子还没刷新时允许下一轮重新决策。
        self.assertEqual(set(), flow.consumed._keys)

    def test_verify_result_waits_even_when_panel_is_unchanged(self):
        log = self.options()
        state = self.state(log)
        adapted = adapt_action(self.parse(BOX_TEXT), state)
        flow = RecommendationFlow(
            capture=nullcontext(), reader=FixedReader(),
            parser=RecommendationParser(),
            state_supplier=lambda: (state, 11), adapter=adapt_action,
            validator=RecommendationValidator(),
            controller=RecordingController())
        status = flow._verify_result(
            self.parse(BOX_TEXT), adapted, frame("before", "same"), state, 10)
        self.assertEqual(ResultStatus.WAITING_RECOMMENDATION, status)

    def test_postcondition_detects_used_ability(self):
        titan = minion("TTN_737", "27", 1, "兵主")
        before = SimpleNamespace(my_minions=[titan], oppo_minions=[],
                                 my_hand_cards=[], discover_choice_count=None)
        used = minion("TTN_737", "27", 1, "兵主", exhausted=1)
        after = SimpleNamespace(my_minions=[used], oppo_minions=[],
                                my_hand_cards=[], discover_choice_count=None)
        self.assertTrue(RecommendationFlow._postcondition(
            "titan_ability_used", before, after, "27", None))
        self.assertFalse(RecommendationFlow._postcondition(
            "titan_ability_used", before, before, "27", None))
        discover_open = SimpleNamespace(my_minions=[titan], oppo_minions=[],
                                        my_hand_cards=[],
                                        discover_choice_count=3)
        self.assertTrue(RecommendationFlow._postcondition(
            "titan_ability_used", before, discover_open, "27", None))
        killed_target = SimpleNamespace(
            my_minions=[titan], oppo_minions=[], my_hand_cards=[],
            discover_choice_count=None)
        with_target = SimpleNamespace(
            my_minions=[titan],
            oppo_minions=[minion("ETC_836", "64", 2, "穆克拉先生")],
            my_hand_cards=[], discover_choice_count=None)
        self.assertTrue(RecommendationFlow._postcondition(
            "titan_ability_used", with_target, killed_target, "27", "64"))


class NullExecutor:
    def __getattr__(self, name):
        raise AssertionError(f"不应调用鼠标执行器：{name}")


if __name__ == "__main__":
    unittest.main()
