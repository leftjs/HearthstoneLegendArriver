import unittest
from contextlib import nullcontext
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import click as hearthstone_click
from log_state import CardEntity, LogState
from manual_controller import (
    ActionExecutionResult, ClickExecutor, DiscoverChoiceAction,
    ManualController, OldGodAbilityAction,
)
from src.flow.recommendation_flow import (
    FlowStepStatus, RecommendationFlow,
)
from src.game_state.old_god import (
    OLD_GOD_ABILITY_COUNT, ability_used, ability_used_tag,
    old_god_ability_cards, old_god_ability_layout, old_god_card_ids,
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


# 实测盒子文案（2026-10-24 00:37，克苏恩在场上，技能「克苏恩的凝视」指向对方2号位）。
BOX_TEXT = ("操作1号位随从攻击\n克苏恩，帝国之眼\n目标是对方2号位\n"
            "小鬼奶奶\n选择卡牌\n克苏恩的凝视")
NORMALIZED_TEXT = "操作1号位随从攻击\n目标是对方2号位\n选择卡牌\n克苏恩的凝视"


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


class TaggedMinion(SimpleNamespace):
    """带 Power.log 标签的随从实体（ACTIVATABLE_MINION_ABILITY_USED_n 等）。"""

    def __init__(self, tag_dict=None, **kwargs):
        super().__init__(**kwargs)
        self.tag_dict = dict(tag_dict or {})

    def query_tag(self, tag, default_val="0"):
        return self.tag_dict.get(tag, default_val)


def minion(card_id, entity_id, zone_pos, name="随从", **overrides):
    values = dict(card_id=card_id, entity_id=entity_id, zone_pos=zone_pos,
                  name=name, cardtype="MINION", damage=0, max_health=12,
                  exhausted=0, attack=12)
    values.update(overrides)
    return SimpleNamespace(**values)


class NullExecutor:
    def __getattr__(self, name):
        raise AssertionError(f"不应调用鼠标执行器：{name}")


class OldGodAbilityTests(unittest.TestCase):
    # ------------------------------------------------------------ 卡牌数据
    def test_card_table_finds_every_old_god_and_its_four_abilities(self):
        self.assertEqual({"BE_009", "BE_013", "BE_017", "BE_784"},
                         set(old_god_card_ids()))
        expected = {
            "BE_013": ("克苏恩的愤怒", "克苏恩的领域", "克苏恩的凝视",
                       "克苏恩的仆从"),
            "BE_009": ("尤格-萨隆的头脑", "尤格-萨隆的疯狂", "尤格-萨隆的崛起",
                       "尤格-萨隆的领域"),
            "BE_017": ("恩佐斯的召唤", "恩佐斯的操纵", "恩佐斯的大军",
                       "恩佐斯的领域"),
            "BE_784": ("亚煞极的凝视", "亚煞极的子嗣", "亚煞极的威力",
                       "亚煞极的领域"),
        }
        for card_id, names in expected.items():
            with self.subTest(card=card_id):
                self.assertEqual(
                    names, tuple(name for name, _ in
                                 old_god_ability_cards(card_id)))
                self.assertEqual(
                    {name: index for index, name in enumerate(names)},
                    old_god_ability_layout(card_id))
                self.assertEqual(OLD_GOD_ABILITY_COUNT,
                                 len(old_god_ability_layout(card_id)))
        # 只是「提到古神」的牌（亡语抽古神牌）不算古神，没有技能面板。
        self.assertEqual({}, old_god_ability_layout("BE_703"))

    # ------------------------------------------------------------ 语法解析
    def parse(self, text, turn_number=9, revision=7):
        return RecommendationParser().parse(
            evidence("frame", text), turn_number, revision)

    def test_box_text_parses_to_old_god_ability(self):
        proposed = self.parse(BOX_TEXT)
        self.assertEqual(ActionKind.OLD_GOD_ABILITY, proposed.action)
        self.assertEqual("board_slot", proposed.source.kind)
        self.assertEqual("friendly", proposed.source.owner)
        self.assertEqual(1, proposed.source.index)
        self.assertEqual("board_slot", proposed.target.kind)
        self.assertEqual("enemy", proposed.target.owner)
        self.assertEqual(2, proposed.target.index)
        self.assertEqual("克苏恩的凝视", proposed.choice_name)
        self.assertEqual(NORMALIZED_TEXT, proposed.normalized_instruction)

    def test_ability_without_target_is_allowed(self):
        # 实测 00:39/00:40 的「克苏恩的仆从」「克苏恩的愤怒」都不带目标行。
        proposed = self.parse(
            "操作1号位随从攻击\n克苏恩，帝国之眼\n选择卡牌\n克苏恩的仆从")
        self.assertEqual(ActionKind.OLD_GOD_ABILITY, proposed.action)
        self.assertIsNone(proposed.target)
        self.assertEqual("克苏恩的仆从", proposed.choice_name)

    def test_friendly_target_is_accepted(self):
        proposed = self.parse(
            "操作1号位随从攻击\n克苏恩，帝国之眼\n目标是我方2号位\n"
            "选择卡牌\n克苏恩的领域")
        self.assertEqual("friendly", proposed.target.owner)
        self.assertEqual(2, proposed.target.index)

    def test_plain_attack_is_unaffected(self):
        proposed = self.parse("操作1号位随从攻击\n目标是对方2号位")
        self.assertEqual(ActionKind.ATTACK, proposed.action)
        self.assertIsNone(proposed.choice_name)

    def test_missing_ability_name_is_not_guessed(self):
        with self.assertRaisesRegex(RecommendationParseError,
                                    "choose_one_name_required"):
            self.parse("操作1号位随从攻击\n克苏恩，帝国之眼\n选择卡牌")

    # ------------------------------------------------------------ 适配
    def state(self, **overrides):
        my_minions = [minion("BE_013", "24", 1, "克苏恩，帝国之眼")]
        oppo_minions = [minion("JAIL_399t1", "210", 1, "小鬼奶奶"),
                        minion("JAIL_399t1", "209", 2, "小鬼奶奶")]
        values = dict(
            game_num_turns_in_play=9,
            is_my_turn=True,
            my_player_id="1",
            my_minions=my_minions,
            oppo_minions=oppo_minions,
            my_locations=[],
            oppo_locations=[],
            my_hand_cards=[],
            my_hero=SimpleNamespace(entity_id="78", name="破天萨尔"),
            oppo_hero=SimpleNamespace(entity_id="80", name="古尔丹"),
            power_options={},
            titan_ability_choices={},
            discover_choice_count=None,
            my_board_slot_num=len(my_minions),
            oppo_board_slot_num=len(oppo_minions),
        )
        values.update(overrides)
        return SimpleNamespace(**values)

    def test_ability_name_maps_to_panel_slot(self):
        adapted = adapt_action(self.parse(BOX_TEXT), self.state())
        manual = adapted.manual_action
        self.assertIsInstance(manual, OldGodAbilityAction)
        self.assertEqual(2, manual.ability_index)      # 凝视是第 3 张
        self.assertEqual(4, manual.ability_count)
        self.assertEqual("克苏恩的凝视", manual.ability_name)
        self.assertEqual("BE_013", manual.card_id)
        self.assertEqual("24", manual.old_god_entity_id)
        self.assertEqual("24", adapted.source_entity_id)
        self.assertEqual("209", adapted.target_entity_id)
        self.assertEqual("old_god_ability_used", adapted.postcondition)
        self.assertEqual("enemy", manual.target.side)
        self.assertEqual("minion", manual.target.kind)
        self.assertEqual(1, manual.target.index)

    def test_each_ability_gets_its_own_slot(self):
        for name, slot in (("克苏恩的愤怒", 0), ("克苏恩的领域", 1),
                           ("克苏恩的仆从", 3)):
            with self.subTest(name=name):
                adapted = adapt_action(
                    self.parse(f"操作1号位随从攻击\n克苏恩\n选择卡牌\n{name}"),
                    self.state())
                self.assertEqual(slot, adapted.manual_action.ability_index)

    def test_unknown_ability_name_is_not_guessed(self):
        with self.assertRaisesRegex(RecommendationStateError,
                                    "old_god_ability_name_unknown"):
            adapt_action(self.parse(
                "操作1号位随从攻击\n克苏恩\n选择卡牌\n不存在的技能"),
                self.state())

    def test_non_old_god_minion_is_not_treated_as_old_god(self):
        state = self.state(my_minions=[minion("ETC_836", "64", 1, "穆克拉先生")])
        with self.assertRaisesRegex(RecommendationStateError,
                                    "old_god_card_unknown"):
            adapt_action(self.parse(BOX_TEXT), state)

    def test_already_used_ability_is_rejected_before_clicking(self):
        used = TaggedMinion(tag_dict={ability_used_tag(2): "1"},
                            **vars(minion("BE_013", "24", 1, "克苏恩")))
        state = self.state(my_minions=[used])
        with self.assertRaisesRegex(RecommendationStateError,
                                    "old_god_ability_already_used"):
            adapt_action(self.parse(BOX_TEXT), state)

    def test_used_ability_survives_the_log_to_strategy_translation(self):
        # 线上适配器拿到的是 StrategyMinion（没有 tag 表），它必须同样知道
        # 哪些技能已经用掉——否则同一张技能牌会被反复点。
        entity = CardEntity("BE_013")
        for tag, value in (("CARDTYPE", "MINION"), ("ZONE", "PLAY"),
                           ("ZONE_POSITION", "1"), ("CONTROLLER", "1"),
                           ("ATK", "12"), ("HEALTH", "12"),
                           ("EXHAUSTED", "1"),
                           (ability_used_tag(2), "1")):
            entity.set_tag(tag, value)
        log = LogState()
        log.my_player_id = "1"
        log.add_entity("24", entity)
        strategy = entity.generate_strategy_entity(log, "24")
        self.assertEqual(frozenset({2}), strategy.used_ability_slots)
        self.assertTrue(ability_used(strategy, 2))
        self.assertFalse(ability_used(strategy, 0))
        state = self.state(my_minions=[strategy])
        with self.assertRaisesRegex(RecommendationStateError,
                                    "old_god_ability_already_used"):
            adapt_action(self.parse(BOX_TEXT), state)

    def test_playing_old_god_from_hand_selects_the_ability(self):
        hand_card = minion("BE_013", "24", 4, "克苏恩，帝国之眼")
        state = self.state(my_hand_cards=[hand_card], my_minions=[])
        proposed = self.parse(
            "打出1号位随从\n克苏恩，帝国之眼\n选择卡牌\n克苏恩的仆从\n"
            "放置于我方1号位")
        self.assertEqual(ActionKind.PLAY_CARD, proposed.action)
        adapted = adapt_action(proposed, state)
        self.assertEqual(DiscoverChoiceAction(3, 4),
                         adapted.manual_action.choose_one)

    # ------------------------------------------------------------ 鼠标点击
    def test_four_ability_slots_click_the_centered_row(self):
        for index, x in enumerate((400, 775, 1150, 1525)):
            with self.subTest(index=index), \
                    patch.object(hearthstone_click, "left_click") as click, \
                    patch.object(hearthstone_click, "rand_sleep"):
                hearthstone_click.choose_old_god_ability(index, 4)
                click.assert_called_once_with(x, 500)

    def test_click_order_is_old_god_then_ability_then_target(self):
        state = self.state()
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
                             side_effect=lambda i, n: events.append(("god", i, n))), \
                patch.object(hearthstone_click, "choose_old_god_ability",
                             side_effect=lambda i, n: events.append(("ability", i, n))), \
                patch.object(hearthstone_click, "choose_opponent_minion",
                             side_effect=lambda i, n: events.append(("target", i, n))):
            result = ManualController(
                executor=executor, output_func=lambda _: None).execute(
                    adapted.manual_action, state)
        self.assertTrue(result.executed, result.message)
        self.assertEqual(
            ["cancel", ("god", 0, 2), ("ability", 2, 4), ("target", 1, 2)],
            events)
        self.assertIn("克苏恩的凝视", result.message)

    def test_real_click_coordinates_god_ability_target(self):
        # 端到端坐标：场上只有克苏恩（1 个卡位）→ 点 (960,600) 开面板；
        # 凝视是第 3 张 → 点四张布局的第 3 个卡位 (1150,500)；
        # 目标是对方 2 号位（共 2 个）→ (1030,400)。
        state = self.state()
        adapted = adapt_action(self.parse(BOX_TEXT), state)
        executor = ClickExecutor(click_module=hearthstone_click,
                                 action_context=nullcontext,
                                 sleep_func=lambda _: None)
        clicks = []
        with patch.object(hearthstone_click, "left_click",
                          side_effect=lambda x, y: clicks.append((x, y))), \
                patch.object(hearthstone_click, "right_click"), \
                patch.object(hearthstone_click, "rand_sleep"):
            result = ManualController(
                executor=executor, output_func=lambda _: None).execute(
                    adapted.manual_action, state)
        self.assertTrue(result.executed, result.message)
        self.assertEqual([(960, 600), (1150, 500), (1030, 400)], clicks)

    def test_stale_old_god_action_is_rejected_before_clicking(self):
        state = self.state()
        adapted = adapt_action(self.parse(BOX_TEXT), state)
        controller = ManualController(executor=NullExecutor(),
                                      output_func=lambda _: None)
        changed_card = replace(adapted.manual_action, card_id="OTHER")
        result = controller.execute(changed_card, state)
        self.assertFalse(result.executed)
        self.assertIn("古神内容已经变化", result.message)
        bad_index = replace(adapted.manual_action, ability_index=5)
        result = controller.execute(bad_index, state)
        self.assertFalse(result.executed)
        self.assertIn("古神技能选项已经失效", result.message)
        state.oppo_minions = state.oppo_minions[:1]
        result = controller.execute(adapted.manual_action, state)
        self.assertFalse(result.executed)
        self.assertIn("古神技能目标已经不存在", result.message)

    # ------------------------------------------------------------ 流程判定
    def test_postcondition_detects_used_ability_by_log_tag(self):
        before = self.state()
        after = self.state()
        after.my_minions = [TaggedMinion(
            tag_dict={ability_used_tag(2): "1"},
            **vars(before.my_minions[0]))]
        self.assertTrue(RecommendationFlow._postcondition(
            "old_god_ability_used", before, after, "24", "209", 2))
        self.assertFalse(RecommendationFlow._postcondition(
            "old_god_ability_used", before, before, "24", "209", 2))
        # 取不到标签时退回旁证：技能改场面/手牌/水晶也算打出去了。
        damaged = self.state()
        damaged.oppo_minions = [minion("JAIL_399t1", "209", 2, "小鬼奶奶",
                                       damage=5)]
        self.assertTrue(RecommendationFlow._postcondition(
            "old_god_ability_used", before, damaged, "24", "209", 2))
        # 线上状态（StrategyMinion）没有 tag 表，靠 used_ability_slots 判定。
        flagged = self.state()
        flagged.my_minions = [minion("BE_013", "24", 1, "克苏恩",
                                     used_ability_slots=frozenset({2}))]
        self.assertTrue(RecommendationFlow._postcondition(
            "old_god_ability_used", before, flagged, "24", None, 2))
        other_slot = self.state()
        other_slot.my_minions = [minion("BE_013", "24", 1, "克苏恩",
                                        used_ability_slots=frozenset({3}))]
        self.assertFalse(RecommendationFlow._postcondition(
            "old_god_ability_used", before, other_slot, "24", None, 2))

    def flow(self, states, text=BOX_TEXT):
        return RecommendationFlow(
            capture=SamePanelCapture(),
            reader=FixedReader(text),
            parser=RecommendationParser(),
            state_supplier=lambda: next(states),
            adapter=adapt_action,
            validator=RecommendationValidator(),
            controller=RecordingController(),
            post_action_delay=0.0,
        )

    def test_ability_resolved_is_completed_and_consumed(self):
        before = self.state()
        after = self.state()
        after.my_minions = [TaggedMinion(
            tag_dict={ability_used_tag(2): "1"},
            **vars(before.my_minions[0]))]
        flow = self.flow(iter(((before, 10), (before, 10), (after, 11))))
        result = flow.run_player_turn_step()
        self.assertEqual(FlowStepStatus.EXECUTED, result.status)
        self.assertEqual(1, len(flow.consumed._keys))

    def test_same_turn_second_ability_is_not_blocked(self):
        before = self.state()
        after = self.state()
        after.my_minions = [TaggedMinion(
            tag_dict={ability_used_tag(2): "1"},
            **vars(before.my_minions[0]))]
        flow = self.flow(iter(((before, 10), (before, 10), (after, 11))))
        self.assertEqual(FlowStepStatus.EXECUTED,
                         flow.run_player_turn_step().status)
        # 同一回合的第二个技能（换了一条推荐文本）：适配与查重都必须放行。
        second = RecommendationParser().parse(
            evidence("stable", "操作1号位随从攻击\n目标是对方2号位\n"
                               "选择卡牌\n克苏恩的仆从"),
            9, 11)
        adapted = adapt_action(second, after)
        result = RecommendationValidator().validate(
            second, adapted, frame("stable", "same"), frame("stable", "same"),
            evidence("stable", second.normalized_instruction), after, 11,
            "PLAYER_TURN", flow.consumed, False)
        self.assertTrue(result.accepted, result.code)

    def test_unconfirmed_ability_waits_without_repeating_the_click(self):
        state = self.state()
        states = iter(((state, 10), (state, 10), (state, 10)))
        flow = self.flow(states)
        result = flow.run_player_turn_step()
        self.assertEqual(FlowStepStatus.OBSERVE, result.status)
        self.assertEqual("waiting_recommendation_update", result.diagnostics)
        self.assertEqual(set(), flow.consumed._keys)

    def test_panel_with_several_abilities_is_parsed_as_old_god(self):
        # 盒子同时给 A/B 两条参考时只读 A：第二段（另一个技能）不参与解析。
        text = (BOX_TEXT + "\n打法参考B\n操作1号位随从攻击\n克苏恩\n"
                "选择卡牌\n克苏恩的仆从")
        proposed = self.parse(text)
        self.assertEqual("克苏恩的凝视", proposed.choice_name)


if __name__ == "__main__":
    unittest.main()
