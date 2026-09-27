"""`refresh_snapshot(force=True)` 必须无视缓存 key 重建 StrategyState。

这是 `choose_one_card_unknown` 触发重建那条链路的地基：适配器报错时 flow 调
`rebuild_snapshot_for_retry()` → `refresh_snapshot(force=True)`，它**不能**因为
`(game_generation, revision)` 没变就把旧缓存对象还回来——那个对象的
`my_hand_cards` 正是不可信的东西。

测试用真 FSM_action 模块 + 替身 StrategyState，只观察「有没有重新构造」。
"""
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, ".")

import FSM_action


class ForceSnapshotRebuildTests(unittest.TestCase):
    def setUp(self):
        self.built = []
        self._orig_strategy_state = FSM_action.StrategyState
        self._orig_log_state = FSM_action.log_state
        self._orig_log_iter = FSM_action.log_iter
        self._orig_cache_key = FSM_action._snapshot_cache_key
        self._orig_cache = FSM_action._snapshot_cache
        self._orig_update = FSM_action.update_log_state

        built = self.built

        class FakeStrategyState:
            def __init__(self, log_state):
                built.append(log_state)
                self.log_revision = None
                self.my_hand_cards = []

        FSM_action.StrategyState = FakeStrategyState
        # 假 log_state：只需能被 FakeStrategyState 接收
        FSM_action.log_state = SimpleNamespace(
            game_generation=0, revision=11, entity_dict={})

        def fake_update():
            return True

        FSM_action.update_log_state = fake_update
        FSM_action._snapshot_cache_key = None
        FSM_action._snapshot_cache = None

    def tearDown(self):
        FSM_action.StrategyState = self._orig_strategy_state
        FSM_action.log_state = self._orig_log_state
        FSM_action.log_iter = self._orig_log_iter
        FSM_action._snapshot_cache_key = self._orig_cache_key
        FSM_action._snapshot_cache = self._orig_cache
        FSM_action.update_log_state = self._orig_update

    def test_unchanged_revision_reuses_cache(self):
        first = FSM_action.refresh_snapshot()
        again = FSM_action.refresh_snapshot()

        self.assertEqual(1, len(self.built), "key 没变时不该重建")
        self.assertIs(first, again)

    def test_force_rebuilds_even_when_revision_is_unchanged(self):
        first = FSM_action.refresh_snapshot()
        forced = FSM_action.refresh_snapshot(force=True)

        self.assertEqual(2, len(self.built), "force 必须重新构造一次")
        self.assertIsNot(first, forced)

    def test_retry_hook_returns_fresh_state_and_revision(self):
        FSM_action.refresh_snapshot()

        state, revision = FSM_action.rebuild_snapshot_for_retry()

        self.assertEqual(2, len(self.built), "钩子必须走 force 路径")
        self.assertIs(FSM_action._snapshot_cache, state)
        self.assertEqual(11, revision)
        self.assertEqual(11, state.log_revision)

    def test_retry_hook_reports_failure_as_none(self):
        FSM_action.update_log_state = lambda: False

        state, revision = FSM_action.rebuild_snapshot_for_retry()

        self.assertIsNone(state)
        self.assertIsNone(revision)

    def test_forced_snapshot_becomes_the_new_cache(self):
        FSM_action.refresh_snapshot()
        forced = FSM_action.refresh_snapshot(force=True)

        # 下一次普通调用应命中刚 force 出来的这一份，而不是再建一份
        again = FSM_action.refresh_snapshot()

        self.assertIs(forced, again)
        self.assertEqual(2, len(self.built))


if __name__ == "__main__":
    unittest.main()
