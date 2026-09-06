"""HSAng 时间线提示「回溯/维持」的解析、适配、点击与 flow 防重试测试。

回溯/维持只影响 HSAng 侧 UI、不改动 Power.log，所以 flow 必须对这两种动作
特判：面板没变时返回 WAITING（等待盒子更新）而不是 RETRY（会二次撤销/误点）。
"""

import unittest
from contextlib import nullcontext
from types import SimpleNamespace

from manual_controller import (
    ActionExecutionResult,
    ClickExecutor,
    ManualController,
    TimelineAction,
)
from src.flow.recommendation_flow import FlowStepStatus, RecommendationFlow
from src.game_state.recommendation_adapter import adapt_action
from src.parser.recommendation_parser import (
    RecommendationParseError,
    RecommendationParser,
)
from src.recommendation_models import (
    ActionKind,
    FrameEvidence,
    OcrEvidence,
)
from src.safety.recommendation_validator import RecommendationValidator


class TimelineParserTests(unittest.TestCase):
    def setUp(self):
        self.parser = RecommendationParser()

    def _ocr(self, instruction):
        return SimpleNamespace(
            frame_id="frame-1",
            normalized_text=instruction,
            confidence=0.99,
        )

    def _parse(self, instruction):
        return self.parser.parse(
            self._ocr(instruction), turn_number=3, log_revision=7)

    def test_undo_literal_parses_to_timeline_undo(self):
        for text in ("回溯", "打法参考A\n回溯"):
            with self.subTest(text=text):
                proposed = self._parse(text)
                self.assertEqual(ActionKind.TIMELINE_UNDO, proposed.action)
                self.assertEqual("回溯", proposed.normalized_instruction)

    def test_keep_literal_parses_to_timeline_keep(self):
        proposed = self._parse("维持")

        self.assertEqual(ActionKind.TIMELINE_KEEP, proposed.action)
        self.assertEqual("维持", proposed.normalized_instruction)

    def test_timeline_coexists_with_the_pending_play_line(self):
        # 弹框时上一句「打出N号位随从」还挂着、旁边带一行时间线字：
        # 那张卡上一步已打出并消费，这次只点时间线按钮，不再执行打出。
        for text, expected in (("打出1号位随从\n维持", ActionKind.TIMELINE_KEEP),
                               ("打出1号位随从\n回溯", ActionKind.TIMELINE_UNDO)):
            with self.subTest(text=text):
                proposed = self._parse(text)
                self.assertEqual(expected, proposed.action)

    def test_timeline_mixed_with_non_play_action_is_ambiguous(self):
        # 与打出以外的动作共存无法判定点哪个，报歧义走重试，绝不猜。
        for text in ("维持\n结束回合", "回溯\n锻造2号位卡牌",
                     "回溯\n维持", "维持\n打出1号位随从\n打出2号位随从"):
            with self.subTest(text=text):
                with self.assertRaisesRegex(
                        RecommendationParseError, "ambiguous_actions"):
                    self._parse(text)


class TimelineAdapterTests(unittest.TestCase):
    @staticmethod
    def _proposed(action_kind):
        text = {ActionKind.TIMELINE_UNDO: "回溯",
                ActionKind.TIMELINE_KEEP: "维持"}[action_kind]
        ocr = SimpleNamespace(
            frame_id="frame-1",
            normalized_text=text,
            confidence=0.99,
        )
        return RecommendationParser().parse(
            ocr, turn_number=3, log_revision=7)

    def _state(self):
        return SimpleNamespace(game_num_turns_in_play=3, is_my_turn=True)

    def test_undo_maps_to_timeline_action(self):
        adapted = adapt_action(
            self._proposed(ActionKind.TIMELINE_UNDO), self._state())

        self.assertIsInstance(adapted.manual_action, TimelineAction)
        self.assertEqual("undo", adapted.manual_action.choice)
        self.assertEqual("timeline_clicked", adapted.postcondition)
        self.assertIsNone(adapted.source_entity_id)
        self.assertIsNone(adapted.target_entity_id)

    def test_keep_maps_to_timeline_action(self):
        adapted = adapt_action(
            self._proposed(ActionKind.TIMELINE_KEEP), self._state())

        self.assertIsInstance(adapted.manual_action, TimelineAction)
        self.assertEqual("keep", adapted.manual_action.choice)
        self.assertEqual("timeline_clicked", adapted.postcondition)


class RecordingClickModule:
    def __init__(self):
        self.events = []

    def click_timeline_undo(self):
        self.events.append(("timeline_undo",))

    def click_timeline_keep(self):
        self.events.append(("timeline_keep",))


class TimelineControllerTests(unittest.TestCase):
    @staticmethod
    def _controller(clicks, sleeps):
        return ManualController(
            output_func=lambda _message: None,
            executor=ClickExecutor(
                click_module=clicks,
                sleep_func=sleeps.append,
                action_context=nullcontext,
            ),
        )

    def _state(self):
        return SimpleNamespace(is_my_turn=True, game_num_turns_in_play=3)

    def test_undo_click_executes_undo(self):
        clicks = RecordingClickModule()
        sleeps = []

        result = self._controller(clicks, sleeps).execute(
            TimelineAction("undo"), self._state())

        self.assertTrue(result.executed, result.message)
        self.assertIn("回溯", result.message)
        self.assertEqual([("timeline_undo",)], clicks.events)
        self.assertEqual([], sleeps)

    def test_keep_click_executes_keep(self):
        clicks = RecordingClickModule()
        sleeps = []

        result = self._controller(clicks, sleeps).execute(
            TimelineAction("keep"), self._state())

        self.assertTrue(result.executed, result.message)
        self.assertIn("维持", result.message)
        self.assertEqual([("timeline_keep",)], clicks.events)

    def test_unknown_choice_is_rejected_before_any_click(self):
        clicks = RecordingClickModule()
        sleeps = []

        result = self._controller(clicks, sleeps).execute(
            TimelineAction("rewind"), self._state())

        self.assertFalse(result.executed)
        self.assertEqual([], clicks.events)
        self.assertEqual([], sleeps)

    def test_not_my_turn_is_rejected_before_any_click(self):
        clicks = RecordingClickModule()
        sleeps = []

        result = self._controller(clicks, sleeps).execute(
            TimelineAction("undo"),
            SimpleNamespace(is_my_turn=False, game_num_turns_in_play=3))

        self.assertFalse(result.executed)
        self.assertEqual([], clicks.events)


class RecordingController:
    def __init__(self):
        self.actions = []

    def execute(self, action, _state):
        self.actions.append(action)
        return ActionExecutionResult(True, "executed")


class PinnedTimelineHarness:
    """Frames/reads 一直钉在同一句指令上，面板永不自行变化。

    这样流程只由 _verify_result 的时间线分支决定结果，不受 OCR 漂移干扰。
    """

    def __init__(self, instruction):
        self.instruction = instruction
        self._frame_no = 0

    def _frame(self, frame_id):
        return FrameEvidence(
            frame_id=frame_id,
            captured_at=0.0,
            desktop_size=(1920, 1080),
            dpi=96,
            window_handle=1,
            foreground=True,
            recommendation_roi=(7, 32, 278, 970),
            exact_hash="same",
            perceptual_hash="0" * 64,
            panel_visible=True,
        )

    def capture(self, ocr_panel_ok=False):
        self._frame_no += 1
        return self._frame(f"f{self._frame_no}")

    @staticmethod
    def crop_recommendation(current_frame):
        return current_frame

    def read(self, frame_supplier, _roi_supplier):
        current_frame = frame_supplier()
        return self._evidence(current_frame.frame_id)

    def read_frame(self, current_frame, _roi_supplier):
        return self._evidence(current_frame.frame_id)

    def _evidence(self, frame_id):
        return OcrEvidence(
            frame_id=frame_id,
            created_at=0.0,
            lines=(),
            normalized_text=self.instruction,
            confidence=0.90,
            backend="test",
            preprocessing="test",
        )


class TimelineFlowTests(unittest.TestCase):
    def _step(self, instruction):
        active = SimpleNamespace(
            is_my_turn=True, game_num_turns_in_play=3, hand_entry_count=0)
        harness = PinnedTimelineHarness(instruction)
        # 每次调用都要返回一个 (state, revision) 二元组；不能用生成器函数
        # （flow 每步会多次 state_supplier()，生成器会被解包错）。
        state_supplier = lambda: (active, 10)

        controller = RecordingController()
        flow = RecommendationFlow(
            capture=harness,
            reader=harness,
            parser=RecommendationParser(),
            state_supplier=state_supplier,
            adapter=adapt_action,
            validator=RecommendationValidator(),
            controller=controller,
        )
        return flow, controller

    def test_unchanged_panel_waits_instead_of_retrying(self):
        flow, controller = self._step("回溯")

        result = flow.run_player_turn_step()

        # 点完面板没变、Power.log 也没变：绝不能 RETRY（会二次撤销），
        # 应标记已消费并等待盒子更新下一条推荐。
        self.assertEqual(FlowStepStatus.OBSERVE, result.status)
        self.assertIn("waiting_recommendation_update", result.diagnostics)
        self.assertEqual(
            [TimelineAction("undo")], controller.actions)

    def test_identical_recommendation_is_not_executed_twice(self):
        flow, controller = self._step("回溯")
        first = flow.run_player_turn_step()
        self.assertEqual(FlowStepStatus.OBSERVE, first.status)

        second = flow.run_player_turn_step()

        # 同一指令、同一 turn/rev：已被消费去重，RETRY 而非再次点击。
        self.assertEqual(FlowStepStatus.RETRY, second.status)
        self.assertEqual([TimelineAction("undo")], controller.actions)

    def test_keep_choice_also_waits_after_single_click(self):
        flow, controller = self._step("维持")

        result = flow.run_player_turn_step()

        self.assertEqual(FlowStepStatus.OBSERVE, result.status)
        self.assertIn("waiting_recommendation_update", result.diagnostics)
        self.assertEqual(
            [TimelineAction("keep")], controller.actions)

    def test_pending_play_line_with_keep_only_clicks_keep_once(self):
        # 面板「打出…还挂着 + 维持」：打出上一步已消费，本次只点一次维持，
        # 不重打、也不因文字再次变化而重复点击。
        flow, controller = self._step("打出1号位随从\n维持")

        result = flow.run_player_turn_step()

        self.assertEqual(FlowStepStatus.OBSERVE, result.status)
        self.assertEqual([TimelineAction("keep")], controller.actions)

        second = flow.run_player_turn_step()
        self.assertEqual(FlowStepStatus.RETRY, second.status)
        self.assertEqual([TimelineAction("keep")], controller.actions)


if __name__ == "__main__":
    unittest.main()
