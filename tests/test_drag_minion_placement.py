import unittest
from unittest.mock import patch

import click as hearthstone_click


class RecordingMouse:
    def __init__(self):
        self.events = []

    @property
    def position(self):
        return None

    @position.setter
    def position(self, value):
        self.events.append(("position", value))

    def press(self, _button):
        self.events.append(("press",))

    def release(self, _button):
        self.events.append(("release",))


class DragMinionToGapTests(unittest.TestCase):
    def _drag(self, **kwargs):
        mouse = RecordingMouse()
        with (
            patch.object(hearthstone_click, "Controller", return_value=mouse),
            patch.object(hearthstone_click, "rand_sleep"),
        ):
            hearthstone_click.drag_card_to_gap(**kwargs)
        return mouse.events

    def test_press_hold_drag_to_recommended_gap_then_release(self):
        # 5 个随从、落点第 6 号位(gap 5)：从手牌按住，抬离，斜移到
        # x=1310 的空隙落点，按住一小段再松开——不是“点两下”。
        events = self._drag(
            card_index=1,
            card_num=2,
            gap_index=5,
            minion_num=5,
        )

        self.assertEqual([
            ("position", (980, 1000)),
            ("press",),
            ("position", (980, 850)),
            ("position", (1310, 600)),
            ("release",),
        ], events)

    def test_empty_board_first_minion_drops_center_gap(self):
        # 空场首只随从：gap 0 落在中场，与 put_minion 坐标一致。
        events = self._drag(
            card_index=0,
            card_num=1,
            gap_index=0,
            minion_num=0,
        )

        self.assertEqual([
            ("position", (885, 1000)),
            ("press",),
            ("position", (885, 850)),
            ("position", (960, 600)),
            ("release",),
        ], events)


if __name__ == "__main__":
    unittest.main()
