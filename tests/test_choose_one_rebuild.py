"""`choose_one_card_unknown` 要触发一次 my_hand_cards 重建再适配。

背景（2026-09-23 13:39:59 实测）：盒子说「打出3号位法术 / 星辰坠落 / 选择卡牌 /
星辰漂流」，适配器把手牌 3 号位解析成 CATA_153（奥拉基尔），既不认识这张牌、
也没有「星辰漂流」的子项表 → 抛 choose_one_card_unknown，19 次重试全是同一个
错误、0 次点击。用户要求：命中这个错时也要强制重建一次手牌快照。

这里锁住三件事：
  1. 命中该诊断码时会调用重建钩子，且**每轮只调用一次**；
  2. 重建后用新 state 再适配一次，成功就照常执行；
  3. 不是这个诊断码（普通适配失败）不许触发重建。
"""
import unittest
from types import SimpleNamespace

from manual_controller import ActionExecutionResult, EndTurnAction
from src.flow.recommendation_flow import FlowStepStatus, RecommendationFlow
from src.game_state.recommendation_adapter import (
    RecommendationStateError, adapt_action)
from src.parser.recommendation_parser import RecommendationParser
from src.recommendation_models import FrameEvidence, OcrEvidence
from src.safety.recommendation_validator import RecommendationValidator


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
        confidence=0.90,
        backend="test",
        preprocessing="test",
    )


class SequencedCapture:
    def __init__(self):
        self.frames = iter((
            frame("stable", "before"),
            frame("current", "before"),
        ))

    def capture(self, ocr_panel_ok=False):
        return next(self.frames)

    @staticmethod
    def crop_recommendation(current_frame):
        return current_frame


class EndTurnReader:
    @staticmethod
    def read(frame_supplier, _roi_supplier):
        return evidence(frame_supplier().frame_id, "结束回合")

    @staticmethod
    def read_frame(current_frame, _roi_supplier):
        return evidence(current_frame.frame_id, "结束回合")


class RecordingController:
    def __init__(self):
        self.actions = []

    def execute(self, action, _state):
        self.actions.append(action)
        return ActionExecutionResult(True, "executed")


class RebuildOnUnknownChoiceTests(unittest.TestCase):
    def make_flow(self, adapter, rebuild_state, states):
        # states 可以是迭代器（旧用例），也可以是带状态的 supplier 函数
        # （需要「重建后 revision 才前进」这种按调用次数变化的行为）。
        supplier = states if callable(states) else (lambda: next(states))
        return RecommendationFlow(
            capture=SequencedCapture(),
            reader=EndTurnReader(),
            parser=RecommendationParser(),
            state_supplier=supplier,
            adapter=adapter,
            validator=RecommendationValidator(),
            controller=RecordingController(),
            rebuild_state=rebuild_state,
        )

    def test_unknown_choice_triggers_one_rebuild_then_succeeds(self):
        stale = SimpleNamespace(
            is_my_turn=True, game_num_turns_in_play=3, hand_entry_count=0)
        rebuilt = SimpleNamespace(
            is_my_turn=True, game_num_turns_in_play=3, hand_entry_count=0)
        calls = []
        seen_revisions = []

        def adapter(proposed, state):
            calls.append(state)
            seen_revisions.append(proposed.log_revision)
            if state is stale:
                raise RecommendationStateError(
                    "choose_one_card_unknown:card=CATA_153:eid=14:"
                    "name=星辰漂流")
            return adapt_action(proposed, state)

        rebuild_calls = []

        def rebuild_state():
            rebuild_calls.append(True)
            return rebuilt, 42

        # 真实情形：首次读到 revision 7；强制重建吃掉新日志行后 revision 前进到 42，
        # 之后供应商一直给 42。旧实现只换 state、proposed.log_revision 仍是 7，
        # 校验器会以 revision_changed:proposed=7:current=42 拒掉 → 重建白做、0 次点击。
        supplier_calls = []

        def supplier():
            supplier_calls.append(True)
            if len(supplier_calls) == 1:
                return stale, 7
            return rebuilt, 42

        flow = self.make_flow(adapter, rebuild_state, supplier)

        result = flow.run_player_turn_step()

        self.assertEqual(1, len(rebuild_calls), "每轮只允许重建一次")
        self.assertEqual([stale, rebuilt], calls, "重建后要用新 state 再适配")
        self.assertEqual([7, 42], seen_revisions,
                         "重建后 proposed.log_revision 必须换成新 revision，"
                         "否则会被 revision_changed 拒掉")
        self.assertNotIn("revision_changed", result.diagnostics)
        # 适配通过后确实点下去了（后面的 result_not_confirmed 属于另一条待修
        # 路径，与本测试无关）。
        self.assertEqual(1, len(flow.controller.actions))
        self.assertIsInstance(flow.controller.actions[0], EndTurnAction)

    def test_rebuild_revision_is_not_forced_when_unchanged(self):
        """重建没让 revision 前进时也要照常工作（避免引入 regression）。"""
        stale = SimpleNamespace(
            is_my_turn=True, game_num_turns_in_play=3, hand_entry_count=0)
        rebuilt = SimpleNamespace(
            is_my_turn=True, game_num_turns_in_play=3, hand_entry_count=0)
        seen = []

        def adapter(proposed, state):
            seen.append(proposed.log_revision)
            if state is stale:
                raise RecommendationStateError("choose_one_card_unknown:x")
            return adapt_action(proposed, state)

        states = iter(((stale, 7), (stale, 7), (rebuilt, 7)))
        flow = self.make_flow(adapter, lambda: (rebuilt, 7), states)

        result = flow.run_player_turn_step()

        self.assertEqual([7, 7], seen)
        self.assertNotIn("revision_changed", result.diagnostics)
        self.assertEqual(1, len(flow.controller.actions))

    def test_other_adapt_errors_do_not_rebuild(self):
        state = SimpleNamespace(
            is_my_turn=True, game_num_turns_in_play=3, hand_entry_count=0)
        rebuild_calls = []

        def adapter(_proposed, _state):
            raise RecommendationStateError("recommendation_changed")

        states = iter(((state, 7), (state, 7)))
        flow = self.make_flow(
            adapter, lambda: rebuild_calls.append(True), states)

        result = flow.run_player_turn_step()

        self.assertEqual(FlowStepStatus.RETRY, result.status)
        self.assertIn("recommendation_changed", result.diagnostics)
        self.assertEqual([], rebuild_calls, "其他错误不许触发重建")

    def test_rebuild_failure_falls_back_to_first_diagnostic(self):
        state = SimpleNamespace(
            is_my_turn=True, game_num_turns_in_play=3, hand_entry_count=0)
        calls = []

        def adapter(proposed, examined):
            calls.append(examined)
            raise RecommendationStateError(
                "choose_one_card_unknown:card=CATA_153:eid=14:name=星辰漂流")

        def rebuild_state():
            return None, None        # 拿不到新状态

        states = iter(((state, 7), (state, 7)))
        flow = self.make_flow(adapter, rebuild_state, states)

        result = flow.run_player_turn_step()

        self.assertEqual(FlowStepStatus.RETRY, result.status)
        self.assertIn("choose_one_card_unknown", result.diagnostics)
        self.assertEqual([state], calls, "重建失败就不该再适配一次")

    def test_no_rebuild_hook_keeps_old_behaviour(self):
        state = SimpleNamespace(
            is_my_turn=True, game_num_turns_in_play=3, hand_entry_count=0)
        calls = []

        def adapter(proposed, examined):
            calls.append(examined)
            raise RecommendationStateError(
                "choose_one_card_unknown:card=CATA_153:eid=14:name=星辰漂流")

        states = iter(((state, 7), (state, 7)))
        flow = self.make_flow(adapter, None, states)

        result = flow.run_player_turn_step()

        self.assertEqual(FlowStepStatus.RETRY, result.status)
        self.assertIn("choose_one_card_unknown", result.diagnostics)
        self.assertEqual([state], calls)


if __name__ == "__main__":
    unittest.main()
