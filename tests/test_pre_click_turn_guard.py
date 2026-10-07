"""点鼠标前的回合复核：动作已过期就绝不能点下去。

2026-10-07 20:45:38 实测卡手：
    [推荐] 打出4号位法术 / 妖术 / 目标是对方3号位 / 索利托斯，循环新生
机器人点完手牌又点目标（坐标都对、0.4s 停顿也在），但 Power.log 里**我们那张
妖术始终没有 PLAY 块**；而同一时刻的 options 块把我方英雄与随从全部标成
REQ_YOUR_TURN = 已经是对手回合。回合在「校验通过 → 真正点鼠标」之间结束，
于是一路空点到用户手动打出。

这里锁住三件事：
  1. 复核发现回合已变（turn_number 不同）→ 一次都不点；
  2. 复核发现已经不是自己回合 → 一次都不点；
  3. 一切都还正常时照常执行，不引入误拦。
"""
import unittest
from types import SimpleNamespace

from manual_controller import ActionExecutionResult, EndTurnAction
from src.flow.recommendation_flow import FlowStepStatus, RecommendationFlow
from src.parser.recommendation_parser import RecommendationParser
from src.recommendation_models import FrameEvidence, OcrEvidence
from src.safety.recommendation_validator import RecommendationValidator

from tests.test_choose_one_rebuild import (
    EndTurnReader, RecordingController, SequencedCapture)


def state(turn=3, my_turn=True):
    return SimpleNamespace(
        is_my_turn=my_turn, game_num_turns_in_play=turn, hand_entry_count=0)


class PreClickTurnGuardTests(unittest.TestCase):
    def build(self, validate_state, verify_state):
        """validate_state 供校验，verify_state 供点前复核（故意不同）。"""
        clicks = RecordingController()
        flow = RecommendationFlow(
            capture=SequencedCapture(),
            reader=EndTurnReader(),
            parser=RecommendationParser(),
            state_supplier=lambda: (validate_state, 7),
            adapter=lambda proposed, st: __import__(
                "src.game_state.recommendation_adapter",
                fromlist=["adapt_action"]).adapt_action(proposed, st),
            validator=RecommendationValidator(),
            controller=clicks,
            verify_state=lambda: (verify_state, 42),
        )
        return flow, clicks

    def test_turn_ended_before_click_blocks_the_click(self):
        flow, clicks = self.build(state(turn=3), state(turn=4))

        result = flow.run_player_turn_step()

        self.assertEqual(FlowStepStatus.RETRY, result.status)
        self.assertIn("turn_ended_before_click", result.diagnostics)
        self.assertEqual([], clicks.actions, "回合已结束就一次都不能点")

    def test_not_my_turn_before_click_blocks_the_click(self):
        flow, clicks = self.build(state(turn=3), state(turn=3, my_turn=False))

        result = flow.run_player_turn_step()

        self.assertEqual(FlowStepStatus.RETRY, result.status)
        self.assertIn("not_my_turn_before_click", result.diagnostics)
        self.assertEqual([], clicks.actions, "已经是对手回合，一次都不能点")

    def test_still_our_turn_still_clicks(self):
        flow, clicks = self.build(state(turn=3), state(turn=3))

        result = flow.run_player_turn_step()

        self.assertNotIn("before_click", result.diagnostics)
        self.assertEqual(1, len(clicks.actions), "一切正常时必须照常出牌")
        self.assertIsInstance(clicks.actions[0], EndTurnAction)


if __name__ == "__main__":
    unittest.main()
