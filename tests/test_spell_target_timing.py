"""指向类法术必须在「选牌」与「点目标」之间留出停顿。

2026-10-07 17:33 实测卡手：机器人连点 6 次
    [推荐] 打出1号位法术 / 妖术 / 目标是对方3号位 / 怨毒大厨
全部 result_not_confirmed，而 Power.log 那 27 秒里**一个 BlockType=PLAY 都没有**
（法力没扣、牌没离手、NUM_CARDS_PLAYED_THIS_TURN 始终 0），游戏只是一直停在
「等你点目标」；同一张牌、同一个目标手动点一次就成。

根因：`_play_spell` 的指向路径点完手牌**没有停顿**就去点目标，目标那一下早于
「指向模式」打开，被当成普通点击吞掉。同一文件里 `_play_minion` 早就为普通目标
留了 0.4s、为手牌目标留了 0.9s，指向类法术这条漏了。

这里锁住三件事：
  1. 「选牌 → 停顿 → 点目标」的顺序（本用例的核心断言）；
  2. 停顿发生在点目标**之前**而不是之后（顺序反了等于没修）；
  3. 不带目标的法术不会被塞进这个停顿（不引入多余延时）。
"""
import unittest
from contextlib import nullcontext

from manual_controller import ClickExecutor, Target


class RecordingClicks:
    """把 click 模块的调用按顺序记下来（真 module 太多副作用，用替身）。"""

    def __init__(self, events):
        self.events = events

    def choose_card(self, index, count):
        self.events.append(("choose_card", index, count))

    def choose_opponent_minion(self, index, count):
        self.events.append(("click_target_oppo_minion", index, count))

    def choose_my_minion(self, index, count):
        self.events.append(("click_target_my_minion", index, count))

    def cancel_click(self):
        self.events.append(("cancel",))

    def click_middle(self):
        self.events.append(("click_middle",))

    def choose_and_use_spell(self, index, count):
        self.events.append(("choose_and_use_spell", index, count))


class SpellTargetTimingTests(unittest.TestCase):
    def executor(self):
        events = []
        self.events = events
        self.sleeps = []
        # sleep 也记进同一个事件流，这样能直接断言它与点击的先后顺序。
        executor = ClickExecutor(
            click_module=RecordingClicks(events),
            sleep_func=lambda seconds: self.sleeps.append(seconds),
            action_context=nullcontext)
        # sleep 走的是 ClickExecutor.sleep（封装了 sleep_func），
        # 包一层把调用写进事件流，便于判定顺序。
        real_sleep = executor.sleep

        def traced_sleep(seconds):
            events.append(("sleep", seconds))
            real_sleep(seconds)

        executor.sleep = traced_sleep
        return executor

    def names(self):
        return [e[0] for e in self.events]

    def test_targeted_spell_pauses_between_card_and_target(self):
        executor = self.executor()
        target = Target("enemy", "minion", 2, "enemy-2")

        executor.play_spell(0, 9, target, 2, 4)

        names = self.names()
        self.assertIn("sleep", names, "选牌与点目标之间必须有停顿")
        i_card = names.index("choose_card")
        i_sleep = names.index("sleep")
        i_target = names.index("click_target_oppo_minion")
        self.assertLess(i_card, i_sleep,
                        "停顿必须发生在选牌之后")
        self.assertLess(i_sleep, i_target,
                        "停顿必须发生在点目标之前——顺序反了等于没修")
        # 点目标那一步的坐标参数仍要正确传递
        self.assertEqual(("click_target_oppo_minion", 2, 4),
                         self.events[i_target])

    def test_pause_is_before_target_not_after(self):
        """专门盯住「停顿在点目标之后」这种假修复。"""
        executor = self.executor()
        executor.play_spell(0, 9, Target("enemy", "minion", 2, "e2"), 2, 4)

        names = self.names()
        self.assertLess(names.index("sleep"),
                        names.index("click_target_oppo_minion"))

    def test_untargeted_spell_does_not_gain_the_pause(self):
        executor = self.executor()

        executor.play_spell(0, 5, None, 2, 4)

        self.assertNotIn("sleep", self.names(),
                         "不带目标的法术不该走这条路径的停顿")

    def test_friendly_minion_target_also_pauses(self):
        executor = self.executor()

        executor.play_spell(1, 9, Target("friendly", "minion", 1, "f1"), 3, 4)

        names = self.names()
        i_card = names.index("choose_card")
        i_sleep = names.index("sleep")
        i_target = names.index("click_target_my_minion")
        self.assertLess(i_card, i_sleep)
        self.assertLess(i_sleep, i_target)


if __name__ == "__main__":
    unittest.main()
