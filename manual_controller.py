"""Command-line control for mulligan and player-turn actions."""

from dataclasses import dataclass, replace
import json
import os
import re
import sys
import threading
import time
from typing import Callable, Optional, Union


def _clean_card_text(text):
    """去掉 HTML 标签并折叠空白，方便按文本规则匹配卡牌效果。"""
    return re.sub(r"\s+", "", re.sub(r"<[^>]+>", "", text or ""))


def _is_hand_pick_battlecry(card):
    """规则：可收集的随从，战吼效果是“从自己手牌中选一张牌”。

    HSAng 的“目标是我方X号位”对两类随从都可能是：从手牌里挑一张（本白名单，
    会弹手牌选择框），或指定我方场上一名随从（如佐拉复制回手，目前不支持，
    安全拒绝）。为免把后者误判成手牌选择，文本里选的是场上随从、或是随机/检视
    这类不弹手牌选择框的，一律不收录。
    """
    if card.get("type") != "MINION" or not card.get("collectible"):
        return False
    text = _clean_card_text(card.get("text"))
    if "选择" not in text or not ("手牌中" in text or "你的手牌" in text):
        return False
    if ("选择一个随从" in text or "选择一个友方随从" in text
            or "选择一条友方的龙" in text or "选择一个敌方随从" in text
            or "检视" in text):
        return False
    if ("随机选择" in text or "手牌中随机" in text
            or "随机从你的手牌" in text or "从中随机" in text
            or "随机一个随从" in text or "随机将你手牌" in text):
        return False
    return True


def _generate_friendly_hand_target_card_ids():
    """按上述规则从 cards.json 生成名单；每次生成完打印命中的卡及 id。

    读的是随仓库分发的本地 cards.json（与 __file__ 同目录），缺失/损坏时退回
    内置名单且不联网下载，避免 import 阶段卡在网络上。
    """
    fallback = frozenset({"CATA_490", "CATA_563"})
    cards_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "cards.json")
    try:
        with open(cards_path, "r", encoding="utf-8") as cards_file:
            cards = json.load(cards_file)
    except Exception as exc:
        print(f"[手牌目标] 读取 {cards_path} 失败，退回内置名单："
              f"{type(exc).__name__}: {exc}")
        return fallback
    matched = sorted(
        (card["id"], card["name"]) for card in cards
        if _is_hand_pick_battlecry(card))
    if not matched:
        print("[手牌目标] 按规则过滤结果为空，退回内置名单")
        return fallback
    print(f"[手牌目标] 由 cards.json 生成手牌目标随从白名单"
          f"（{len(matched)} 张）：")
    for card_id, name in matched:
        print(f"[手牌目标]   {card_id} {name}")
    return frozenset(card_id for card_id, _ in matched)


FRIENDLY_HAND_TARGET_CARD_IDS = _generate_friendly_hand_target_card_ids()

# 抉择(choose-one)法术：点手牌 -> 点棋盘中央把牌举起后，分支卡才居中弹出。
# 点分支前的等动画时长(秒)。比普通随从战吼的目标等待略长，给弹出动画留时间。
CHOOSE_ONE_PANEL_DELAY = 0.8


class GlobalHotkeyInput:
    """Collect numeric commands globally while Hearthstone stays focused."""

    def __init__(self, keyboard_module, output_func=print,
                 shutdown_event=None, wait_timeout=0.05):
        self.keyboard = keyboard_module
        self.output = output_func
        self.shutdown_event = shutdown_event or threading.Event()
        self.wait_timeout = wait_timeout

    @staticmethod
    def _cancel_code(prompt: str) -> str:
        return "99" if "99取消" in prompt else "0"

    def __call__(self, prompt: str) -> str:
        buffer = []
        completed = threading.Event()
        result = {"value": ""}
        handles = []

        def show_buffer():
            current = "".join(buffer) or "（空）"
            self.output(f"当前输入：{current}")

        def append_digit(digit):
            buffer.append(digit)
            show_buffer()

        def backspace():
            if buffer:
                buffer.pop()
            show_buffer()

        def submit():
            result["value"] = "".join(buffer)
            completed.set()

        def cancel():
            result["value"] = self._cancel_code(prompt)
            self.output(f"已取消，提交 {result['value']}")
            completed.set()

        try:
            self.output(prompt)
            self.output(
                "炉石保持前台：直接按数字，Enter确认，Backspace删除，Esc取消。")
            for digit in "0123456789":
                handles.append(self.keyboard.add_hotkey(
                    digit, lambda value=digit: append_digit(value),
                    suppress=True))
            handles.append(self.keyboard.add_hotkey(
                "enter", submit, suppress=True))
            handles.append(self.keyboard.add_hotkey(
                "backspace", backspace, suppress=True))
            handles.append(self.keyboard.add_hotkey(
                "esc", cancel, suppress=True))

            while not completed.wait(self.wait_timeout):
                if self.shutdown_event.is_set():
                    raise KeyboardInterrupt
            return result["value"]
        finally:
            for handle in handles:
                try:
                    self.keyboard.remove_hotkey(handle)
                except Exception:
                    pass


def cancellable_console_input(
    prompt: str,
    stop_event: threading.Event,
    key_available=None,
    read_key=None,
    sleep_func=time.sleep,
    write_func=None,
) -> str:
    """Read a Windows console line while allowing Ctrl+Q to cancel it."""
    if key_available is None or read_key is None:
        import msvcrt
        key_available = msvcrt.kbhit
        read_key = msvcrt.getwch
    if write_func is None:
        write_func = lambda text: (sys.stdout.write(text), sys.stdout.flush())

    write_func(prompt)
    chars = []
    while not stop_event.is_set():
        if not key_available():
            sleep_func(0.05)
            continue
        char = read_key()
        if char in ("\r", "\n"):
            write_func("\n")
            return "".join(chars)
        if char == "\003":
            raise KeyboardInterrupt
        if char == "\b":
            if chars:
                chars.pop()
                write_func("\b \b")
            continue
        if char in ("\x00", "\xe0"):
            read_key()
            continue
        if char.isprintable():
            chars.append(char)
            write_func(char)
    raise KeyboardInterrupt


@dataclass(frozen=True)
class Target:
    side: str
    kind: str
    index: Optional[int]
    entity_id: Optional[str] = None


@dataclass(frozen=True)
class PlayCardAction:
    hand_index: int
    card_id: str
    cardtype: str
    gap_index: Optional[int] = None
    target: Optional[Target] = None
    hand_entity_id: Optional[str] = None
    # 抉择(choose-one)法术：打出后要在居中弹出面板里选一张分支卡。
    # sub_option_index 是面板里从左到右的号位(0 起)，sub_option_count 是分支总数
    # (即弹出几张)，二者都非 None 时走 play_choose_one_spell 而非普通法术施放。
    sub_option_index: Optional[int] = None
    sub_option_count: Optional[int] = None
    turn_number: Optional[int] = None


@dataclass(frozen=True)
class TradeCardAction:
    hand_index: int
    card_id: str
    cardtype: str
    hand_entity_id: Optional[str] = None
    turn_number: Optional[int] = None


@dataclass(frozen=True)
class HeroPowerAction:
    target: Optional[Target] = None
    turn_number: Optional[int] = None


@dataclass(frozen=True)
class DiscoverChoiceAction:
    choice_index: int
    choice_count: int
    turn_number: Optional[int] = None


@dataclass(frozen=True)
class UseLocationAction:
    location_index: int
    card_id: str
    location_entity_id: Optional[str] = None
    target: Optional[Target] = None
    turn_number: Optional[int] = None


@dataclass(frozen=True)
class AttackAction:
    attacker: Target
    target: Target
    turn_number: Optional[int] = None


@dataclass(frozen=True)
class LaunchStarshipAction:
    starship_index: int
    card_id: str
    starship_entity_id: Optional[str] = None
    turn_number: Optional[int] = None


@dataclass(frozen=True)
class EndTurnAction:
    turn_number: Optional[int] = None


@dataclass(frozen=True)
class RefreshAction:
    turn_number: Optional[int] = None


@dataclass(frozen=True)
class TimelineAction:
    """点 HSAng 左下时间线提示：回溯(undo) / 维持(keep)。

    HSAng 认为上一手操作失误时会在推荐区弹出“回溯/维持”；OCR 到“回溯”点
    (351,805)，OCR 到“维持”点 (582,805)。该操作只影响 HSAng 侧 UI，不改动
    Power.log，因此 postcondition 是哨兵值 timeline_clicked（由 flow 特判）。
    """
    choice: str  # "undo" 或 "keep"
    turn_number: Optional[int] = None


@dataclass(frozen=True)
class ActionExecutionResult:
    executed: bool
    message: str
    recovery_needed: bool = False


ManualAction = Union[
    PlayCardAction,
    TradeCardAction,
    HeroPowerAction,
    DiscoverChoiceAction,
    UseLocationAction,
    AttackAction,
    LaunchStarshipAction,
    EndTurnAction,
    RefreshAction,
    TimelineAction,
]


class ClickExecutor:
    """Translate validated actions into the project's click primitives."""

    def __init__(self, click_module=None, focus_func=None, sleep_func=None,
                 action_context=None):
        if click_module is None:
            import click as click_module
        self.click = click_module
        if action_context is None:
            namespace = getattr(click_module, "__dict__", {})
            action_context = namespace.get("hearthstone_action_session")
        if action_context is None:
            from contextlib import nullcontext
            action_context = nullcontext
        self.action_context = action_context
        if sleep_func is None:
            import time
            sleep_func = time.sleep
        self.sleep = sleep_func

    def _safe_action(self, action):
        with self.action_context():
            try:
                return action()
            except Exception:
                try:
                    self.click.cancel_click()
                except Exception:
                    pass
                raise

    def _click_target(self, target: Target, my_count: int, oppo_count: int):
        if target.side == "friendly":
            if target.kind == "hero":
                self.click.choose_my_hero()
            else:
                self.click.choose_my_minion(target.index, my_count)
        elif target.kind == "hero":
            self.click.choose_oppo_hero()
        else:
            self.click.choose_opponent_minion(target.index, oppo_count)

    def play_minion(self, hand_index, hand_count, gap_index, minion_count,
                    oppo_minion_count, target):
        return self._safe_action(lambda: self._play_minion(
            hand_index, hand_count, gap_index, minion_count,
            oppo_minion_count, target))

    def _play_minion(self, hand_index, hand_count, gap_index, minion_count,
                     oppo_minion_count, target):
        if minion_count >= 7:
            self.click.drag_card_to_board_entity(
                hand_index, hand_count, gap_index, minion_count)
        else:
            self.click.choose_card(hand_index, hand_count)
            self.click.put_minion(gap_index, minion_count)
        # Let the board fan-out settle briefly before clicking the target.
        if target is not None:
            # Supported battlecry hand targets appear later than ordinary
            # minion targets, so wait for that UI to settle.
            is_friendly_hand_target = (
                target.side == "friendly" and target.kind == "hand")
            self.sleep(0.8 if is_friendly_hand_target else 0.3)
        if target is not None:
            adjusted = target
            if target.side == "friendly" and target.kind == "hand":
                adjusted_index = (target.index - 1
                                  if hand_index < target.index
                                  else target.index)
                self.click.choose_card(adjusted_index, hand_count - 1)
            elif (target.side == "friendly" and target.kind == "minion"
                  and target.index >= gap_index):
                adjusted = Target(
                    target.side, target.kind, target.index + 1,
                    target.entity_id)
                self._click_target(
                    adjusted, minion_count + 1, oppo_minion_count)
            else:
                self._click_target(
                    adjusted, minion_count + 1, oppo_minion_count)
        self.click.cancel_click()

    def play_spell(self, hand_index, hand_count, target, my_count,
                   oppo_count):
        return self._safe_action(lambda: self._play_spell(
            hand_index, hand_count, target, my_count, oppo_count))

    def _play_spell(self, hand_index, hand_count, target, my_count,
                    oppo_count):
        if target is None:
            if hand_count <= 2:
                # With a nearly empty hand, the fan re-centers noticeably.
                # Let the cards reach their final positions before selecting.
                self.sleep(0.3)
                self.click.choose_card(hand_index, hand_count)
                self.click.click_middle()
            else:
                self.click.choose_and_use_spell(hand_index, hand_count)
        else:
            self.click.choose_card(hand_index, hand_count)
            self._click_target(target, my_count, oppo_count)
        self.click.cancel_click()

    def play_choose_one_spell(self, hand_index, hand_count, option_index,
                              option_count):
        return self._safe_action(lambda: self._play_choose_one_spell(
            hand_index, hand_count, option_index, option_count))

    def _play_choose_one_spell(self, hand_index, hand_count, option_index,
                               option_count):
        # 抉择法术不能像普通法术那样"选牌后点中央施放"——点中央只把牌举起，
        # 分支卡才居中弹出；再点目标分支卡才算真正施放。
        if hand_count <= 2:
            # 手牌很少时扇形会明显重排，先等牌到位再选(与普通法术一致)。
            self.sleep(0.3)
        self.click.choose_card(hand_index, hand_count)
        self.click.click_middle()
        self.sleep(CHOOSE_ONE_PANEL_DELAY)
        self.click.choose_discover_card(option_index, option_count)

    def play_location(self, hand_index, hand_count, gap_index,
                      board_slot_count):
        return self._safe_action(lambda: self._play_location(
            hand_index, hand_count, gap_index, board_slot_count))

    def _play_location(self, hand_index, hand_count, gap_index,
                       board_slot_count):
        self.click.choose_card(hand_index, hand_count)
        self.click.put_minion(gap_index, board_slot_count)
        self.click.cancel_click()

    def use_location(self, location_screen_index, board_slot_count, target,
                     my_board_count, oppo_board_count):
        return self._safe_action(lambda: self._use_location(
            location_screen_index, board_slot_count, target,
            my_board_count, oppo_board_count))

    def _use_location(self, location_screen_index, board_slot_count, target,
                      my_board_count, oppo_board_count):
        self.click.choose_my_board_entity(
            location_screen_index, board_slot_count)
        if target is not None:
            self.sleep(0.3)
            self._click_target(target, my_board_count, oppo_board_count)
        self.click.cancel_click()

    def launch_starship(self, starship_screen_index, board_count):
        return self._safe_action(lambda: self._launch_starship(
            starship_screen_index, board_count))

    def _launch_starship(self, starship_screen_index, board_count):
        self.click.choose_my_board_entity(
            starship_screen_index, board_count)
        self.click.click_launch_starship()

    def play_weapon(self, hand_index, hand_count):
        return self._safe_action(
            lambda: self._play_weapon(hand_index, hand_count))

    def _play_weapon(self, hand_index, hand_count):
        self.click.choose_and_use_spell(hand_index, hand_count)
        self.click.cancel_click()

    def trade_card(self, hand_index, hand_count):
        return self._safe_action(
            lambda: self._trade_card(hand_index, hand_count))

    def _trade_card(self, hand_index, hand_count):
        self.click.choose_card(hand_index, hand_count)
        self.click.drag_card_to_deck()

    def use_hero_power(self, target=None, my_count=0, oppo_count=0):
        return self._safe_action(lambda: self._use_hero_power(
            target, my_count, oppo_count))

    def _use_hero_power(self, target, my_count, oppo_count):
        if target is None:
            self.click.use_skill_no_point()
            return
        self.click.click_skill()
        self._click_target(target, my_count, oppo_count)
        self.click.cancel_click()

    def choose_discover_card(self, choice_index, choice_count):
        return self._safe_action(
            lambda: self.click.choose_discover_card(
                choice_index, choice_count))

    def attack(self, attacker, target, my_count, oppo_count):
        return self._safe_action(
            lambda: self._attack(attacker, target, my_count, oppo_count))

    def _attack(self, attacker, target, my_count, oppo_count):
        if attacker.kind == "minion":
            if target.kind == "minion":
                self.click.minion_beat_minion(
                    attacker.index, my_count, target.index, oppo_count)
            else:
                self.click.minion_beat_hero(attacker.index, my_count)
        elif target.kind == "minion":
            self.click.hero_beat_minion(target.index, oppo_count)
        else:
            self.click.hero_beat_hero()

    def end_turn(self):
        return self._safe_action(self._end_turn)

    def _end_turn(self):
        self.click.end_turn()

    def timeline_click(self, choice):
        return self._safe_action(lambda: self._timeline_click(choice))

    def _timeline_click(self, choice):
        if choice == "undo":
            self.click.click_timeline_undo()
        else:
            self.click.click_timeline_keep()


class ManualController:
    def __init__(
        self,
        input_func: Callable[[str], str] = input,
        output_func: Callable[[str], None] = print,
        executor=None,
    ):
        self.input = input_func
        self.output = output_func
        self.executor = executor

    def _read_int(self, prompt: str) -> Optional[int]:
        try:
            return int(self.input(prompt).strip())
        except (ValueError, TypeError):
            self.output("无效输入，请输入一个整数。")
            return None

    @staticmethod
    def mulligan_is_current(original_state, fresh_state) -> bool:
        """Ensure hand positions still refer to the cards shown to the user."""
        original_ids = [
            (card.card_id, getattr(card, "entity_id", None))
            for card in original_state.my_hand_cards
        ]
        fresh_ids = [
            (card.card_id, getattr(card, "entity_id", None))
            for card in fresh_state.my_hand_cards
        ]
        return (not getattr(fresh_state, "is_end", False)
                and getattr(fresh_state, "game_num_turns_in_play", 0) == 0
                and original_ids == fresh_ids)

    def choose_mulligan(self, state) -> list[int]:
        replaceable = [
            card for card in state.my_hand_cards
            if not getattr(card, "is_coin", False)
        ]
        self.output("=== 起手留牌 ===")
        for index, card in enumerate(replaceable, start=1):
            self.output(f"[{index}] {card.name}  费用: {card.current_cost}")

        chosen: list[int] = []
        while True:
            value = self._read_int("输入要替换的手牌编号，输入 0 停止并确认：")
            if value is None:
                continue
            if value == 0:
                return chosen
     

            replaceable = [
                index for index, card in enumerate(state.my_hand_cards)
                if not getattr(card, "is_coin", False)]
            if not 1 <= value <= len(replaceable):
                self.output("无效手牌编号，请重新输入。")
                continue
            slot = value - 1
            if slot in chosen:
                self.output("该手牌已经选择，请重新输入。")
                continue
            chosen.append(slot)

    def prompt_turn_action(self, state):
        while True:
            self.output("可执行操作：")
            self.output("  0：刷新战局状态")
            self.output("  1-10：选择手牌")
            self.output("  11：使用英雄技能")
            self.output("  12：随从攻击")
            self.output("  13：结束回合")
            self.output("  14：使用地标")
            self.output("  15：发现选择")
            value = self._read_int("请输入操作编号：")
            if value is None:
                continue
            if value == 0:
                return RefreshAction()
            if value == 11:
                return HeroPowerAction()
            if value == 12:
                action = self._prompt_attack(state)
                if action is None:
                    continue
                return action
            if value == 13:
                return EndTurnAction()
            if value == 14:
                locations = getattr(state, "my_locations", [])
                if not locations:
                    self.output("没有可用的地标。")
                    continue
                for index, location in enumerate(locations, start=1):
                    self.output(f"[{index}] {location.name}")
                while True:
                    location_number = self._read_int(
                        "选择要使用的地标编号，99 取消：")
                    if location_number is None:
                        continue
                    if location_number == 99:
                        break
                    if 1 <= location_number <= len(locations):
                        location = locations[location_number - 1]
                        target = self._read_target(state)
                        if target == "cancel":
                            break
                        return UseLocationAction(
                            location_index=location_number - 1,
                            card_id=location.card_id,
                            location_entity_id=getattr(
                                location, "entity_id", None),
                            target=target,
                        )
                    self.output("无效地标编号，请重新输入。")
                continue
            if value == 15:
                choice_count = getattr(
                    state, "discover_choice_count", 0)
                if choice_count not in (1, 2, 3, 4):
                    self.output("当前没有发现选择。")
                    continue
                while True:
                    choice_index = self._read_int(
                        f"选择发现选项（1-{choice_count}，99 取消）：")
                    if choice_index is None:
                        continue
                    if choice_index == 99:
                        break
                    if 1 <= choice_index <= choice_count:
                        return DiscoverChoiceAction(
                            choice_index - 1, choice_count)
                    self.output("无效选项编号，请重新输入。")
                continue
            if not 1 <= value <= len(state.my_hand_cards):
                self.output("无效操作编号，请重新输入。")
                continue

            hand_index = value - 1
            selected = state.my_hand_cards[hand_index]
            cardtype = selected.cardtype
            if cardtype == "MINION":
                board_slot_count = getattr(
                    state, "my_board_slot_num", len(state.my_minions))
                while True:
                    gap = self._read_int(
                        f"随从落点（1-{board_slot_count + 1}，99取消）："
                    )
                    if gap is None:
                        continue
                    if gap == 99:
                        break
                    if 1 <= gap <= board_slot_count + 1:
                        target = self._read_target(state)
                        if target == "cancel":
                            break
                        return PlayCardAction(
                            hand_index, selected.card_id, cardtype,
                            gap_index=gap - 1, target=target,
                            hand_entity_id=getattr(
                                selected, "entity_id", None),
                        )
                    self.output("无效落点编号，请重新输入。")
                continue
            if cardtype == "LOCATION":
                board_slot_count = getattr(
                    state, "my_board_slot_num", len(state.my_minions))
                while True:
                    gap = self._read_int(
                        f"地标落点（1-{board_slot_count + 1}，99取消）："
                    )
                    if gap is None:
                        continue
                    if gap == 99:
                        break
                    if 1 <= gap <= board_slot_count + 1:
                        return PlayCardAction(
                            hand_index, selected.card_id, cardtype,
                            gap_index=gap - 1,
                            hand_entity_id=getattr(
                                selected, "entity_id", None),
                        )
                    self.output("无效落点编号，请重新输入。")
                continue
            if cardtype == "SPELL":
                target = self._read_target(state)
                if target == "cancel":
                    continue
                return PlayCardAction(
                    hand_index, selected.card_id, cardtype, target=target,
                    hand_entity_id=getattr(selected, "entity_id", None),
                )
            if cardtype == "WEAPON":
                return PlayCardAction(
                    hand_index, selected.card_id, cardtype,
                    hand_entity_id=getattr(selected, "entity_id", None))

            self.output(f"暂不支持卡牌类型：{cardtype}")

    def _prompt_attack(self, state):
        if not state.my_minions:
            self.output("己方场上没有随从可以攻击。")
            return None
        for index, minion in enumerate(state.my_minions, start=1):
            self.output(f"[{index}] {minion.name}")
        while True:
            attacker_index = self._read_int("选择攻击者编号，99 取消：")
            if attacker_index is None:
                continue
            if attacker_index == 99:
                return None
            if 1 <= attacker_index <= len(state.my_minions):
                attacker = Target("friendly", "minion", attacker_index - 1)
                target = self._read_target(state)
                if target == "cancel":
                    return None
                return AttackAction(attacker, target)
            self.output("无效攻击者编号，请重新输入。")

    def _read_target(self, state):
        self.output("目标编号：")
        self.output("  0：无目标 | 8/18：敌方英雄 | 9：我方英雄")
        self.output("  1-7：敌方随从 | 10-16：我方随从 | 99：取消")
        while True:
            value = self._read_int("请输入目标编号：")
            if value is None:
                continue
            if value == 0:
                return None
            if value in (8, 18):
                return Target("enemy", "hero", None)
            if value == 9:
                return Target("friendly", "hero", None)
            if 1 <= value <= 7:
                index = value - 1
                if index < len(state.oppo_minions):
                    return Target(
                        "enemy", "minion", index,
                        getattr(state.oppo_minions[index],
                                "entity_id", None))
                return Target("enemy", "minion", index)
            if 10 <= value <= 16:
                index = value - 10
                if index < len(state.my_minions):
                    return Target(
                        "friendly", "minion", index,
                        getattr(state.my_minions[index],
                                "entity_id", None))
                return Target("friendly", "minion", index)
            if value == 99:
                return "cancel"
            self.output("无效目标编号，请重新输入。")



    @staticmethod
    def _target_exists(target: Optional[Target], state) -> bool:
        if target is None:
            return True
        if target.kind == "hero":
            entity = (state.my_hero if target.side == "friendly"
                      else state.oppo_hero if target.side == "enemy"
                      else None)
            return (entity is not None
                    and (target.entity_id is None
                         or getattr(entity, "entity_id", None)
                         == target.entity_id))
        if target.kind == "hand":
            if target.side != "friendly" or target.index is None:
                return False
            collection = state.my_hand_cards
            if not 0 <= target.index < len(collection):
                return False
            return (target.entity_id is None
                    or getattr(collection[target.index], "entity_id", None)
                    == target.entity_id)
        if target.kind != "minion" or target.index is None:
            return False
        collection = (state.my_minions if target.side == "friendly"
                      else state.oppo_minions if target.side == "enemy"
                      else [])
        if not 0 <= target.index < len(collection):
            return False
        return (target.entity_id is None
                or getattr(collection[target.index], "entity_id", None)
                == target.entity_id)

    def _reject(self, message: str) -> ActionExecutionResult:
        self.output(message)
        return ActionExecutionResult(False, message)

    @staticmethod
    def _board_slot_count(state, side: str) -> int:
        if side == "friendly":
            return getattr(state, "my_board_slot_num", len(state.my_minions))
        return getattr(
            state, "oppo_board_slot_num", len(state.oppo_minions))

    @staticmethod
    def _target_for_click(target: Optional[Target], state) -> Optional[Target]:
        if target is None or target.kind != "minion" or target.index is None:
            return target
        collection = (state.my_minions if target.side == "friendly"
                      else state.oppo_minions)
        entity = collection[target.index]
        zone_pos = getattr(entity, "zone_pos", 0)
        screen_index = zone_pos - 1 if zone_pos > 0 else target.index
        return Target(
            target.side, target.kind, screen_index, target.entity_id)

    @staticmethod
    def bind_to_turn(action: ManualAction, state) -> ManualAction:
        return replace(
            action, turn_number=state.game_num_turns_in_play)

    def execute(self, action: ManualAction, state) -> ActionExecutionResult:
        try:
            return self._execute(action, state)
        except Exception as exc:
            message = f"鼠标操作失败，未执行或无法确认：{exc}"
            self.output(message)
            return ActionExecutionResult(False, message, recovery_needed=True)

    def _execute(self, action: ManualAction, state) -> ActionExecutionResult:
        if isinstance(action, RefreshAction):
            return ActionExecutionResult(False, "已刷新战局状态。")
        if not getattr(state, "is_my_turn", False):
            return self._reject("当前不是己方回合，未执行操作。")
        if (action.turn_number is not None
                and action.turn_number != state.game_num_turns_in_play):
            return self._reject("回合已经变化，旧操作未执行。")
        if self.executor is None:
            return self._reject("没有可用的鼠标执行器。")

        if isinstance(action, TradeCardAction):
            if not 0 <= action.hand_index < len(state.my_hand_cards):
                return self._reject("手牌位置已经失效，未执行交易。")
            selected = state.my_hand_cards[action.hand_index]
            if (selected.card_id != action.card_id
                    or selected.cardtype != action.cardtype):
                return self._reject("手牌内容已经变化，未执行交易。")
            if (action.hand_entity_id is not None
                    and getattr(selected, "entity_id", None)
                    != action.hand_entity_id):
                return self._reject("手牌对象已经变化，未执行交易。")
            self.executor.trade_card(
                action.hand_index, len(state.my_hand_cards))
            return ActionExecutionResult(True, f"已交易手牌：{selected.name}")

        if isinstance(action, PlayCardAction):
            if not 0 <= action.hand_index < len(state.my_hand_cards):
                return self._reject("手牌位置已经失效，未执行操作。")
            selected = state.my_hand_cards[action.hand_index]
            if (selected.card_id != action.card_id
                    or selected.cardtype != action.cardtype):
                return self._reject("手牌内容已经变化，未执行操作。")
            if (action.hand_entity_id is not None
                    and getattr(selected, "entity_id", None)
                    != action.hand_entity_id):
                return self._reject("手牌对象已经变化，未执行操作。")
            is_hand_target = (
                action.target is not None and action.target.kind == "hand")
            if is_hand_target:
                if (selected.card_id not in FRIENDLY_HAND_TARGET_CARD_IDS
                        or action.target.side != "friendly"
                        or action.target.index is None):
                    return self._reject(
                        "该随从不能选择手牌目标，未执行操作。")
                if (action.target.index == action.hand_index
                        or not self._target_exists(action.target, state)):
                    return self._reject(
                        "手牌目标已经失效，未执行操作。")
            elif not self._target_exists(action.target, state):
                return self._reject("目标已经不存在，未执行操作。")

            if action.cardtype == "MINION":
                my_board_count = self._board_slot_count(state, "friendly")
                oppo_board_count = self._board_slot_count(state, "enemy")
                if action.gap_index is None or not (
                        0 <= action.gap_index <= my_board_count):
                    return self._reject("随从落点已经失效，未执行操作。")
                self.executor.play_minion(
                    action.hand_index, len(state.my_hand_cards),
                    action.gap_index, my_board_count,
                    oppo_board_count,
                    self._target_for_click(action.target, state),
                )
            elif action.cardtype == "SPELL":
                if action.sub_option_index is not None:
                    if (action.target is not None
                            or action.sub_option_count is None
                            or action.sub_option_count not in (1, 2, 3, 4)
                            or not 0 <= action.sub_option_index
                            < action.sub_option_count):
                        return self._reject(
                            "抉择法术分支参数无效，未执行操作。")
                    self.executor.play_choose_one_spell(
                        action.hand_index, len(state.my_hand_cards),
                        action.sub_option_index, action.sub_option_count)
                else:
                    self.executor.play_spell(
                        action.hand_index, len(state.my_hand_cards),
                        self._target_for_click(action.target, state),
                        self._board_slot_count(state, "friendly"),
                        self._board_slot_count(state, "enemy"),
                    )
            elif action.cardtype == "WEAPON":
                self.executor.play_weapon(action.hand_index,
                                          len(state.my_hand_cards))
            elif action.cardtype == "HERO":
                self.executor.play_spell(
                    action.hand_index, len(state.my_hand_cards), None,
                    self._board_slot_count(state, "friendly"),
                    self._board_slot_count(state, "enemy"),
                )
            elif action.cardtype == "LOCATION":
                my_board_count = self._board_slot_count(state, "friendly")
                if my_board_count >= 7:
                    return self._reject("己方场上已满，未执行操作。")
                if action.target is not None:
                    return self._reject("地标牌不应指定目标，未执行操作。")
                if action.gap_index is None or not (
                        0 <= action.gap_index <= my_board_count):
                    return self._reject("地标落点已经失效，未执行操作。")
                self.executor.play_location(
                    action.hand_index, len(state.my_hand_cards),
                    action.gap_index, my_board_count,
                )
            else:
                return self._reject(f"不支持卡牌类型：{action.cardtype}")
            return ActionExecutionResult(True, f"已执行手牌：{selected.name}")

        if isinstance(action, HeroPowerAction):
            if action.target is None:
                self.executor.use_hero_power()
            else:
                if not self._target_exists(action.target, state):
                    return self._reject(
                        "英雄技能目标已经不存在，未执行操作。")
                self.executor.use_hero_power(
                    self._target_for_click(action.target, state),
                    self._board_slot_count(state, "friendly"),
                    self._board_slot_count(state, "enemy"),
                )
            return ActionExecutionResult(True, "已使用英雄技能。")

        if isinstance(action, DiscoverChoiceAction):
            if (action.choice_count not in (1, 2, 3, 4)
                    or not 0 <= action.choice_index < action.choice_count):
                return self._reject("发现选项位置已经失效，未执行操作。")
            self.executor.choose_discover_card(
                action.choice_index, action.choice_count)
            return ActionExecutionResult(
                True, f"已选择发现选项：{action.choice_index + 1}号位")

        if isinstance(action, UseLocationAction):
            locations = getattr(state, "my_locations", [])
            if not 0 <= action.location_index < len(locations):
                return self._reject("地标位置已经失效，未执行操作。")
            location = locations[action.location_index]
            if location.card_id != action.card_id:
                return self._reject("地标内容已经变化，未执行操作。")
            if (action.location_entity_id is not None
                    and getattr(location, "entity_id", None)
                    != action.location_entity_id):
                return self._reject("地标对象已经变化，未执行操作。")
            if not self._target_exists(action.target, state):
                return self._reject("地标目标已经不存在，未执行操作。")
            zone_pos = getattr(location, "zone_pos", 0)
            if zone_pos <= 0:
                return self._reject("地标战场位置未知，未执行操作。")
            my_board_count = self._board_slot_count(state, "friendly")
            oppo_board_count = self._board_slot_count(state, "enemy")
            if not 1 <= zone_pos <= my_board_count:
                return self._reject("地标战场位置已经失效，未执行操作。")
            self.executor.use_location(
                zone_pos - 1,
                my_board_count,
                self._target_for_click(action.target, state),
                my_board_count,
                oppo_board_count,
            )
            return ActionExecutionResult(True, f"已使用地标：{location.name}")

        if isinstance(action, LaunchStarshipAction):
            if not 0 <= action.starship_index < len(state.my_minions):
                return self._reject("星舰位置已经失效，未执行发射。")
            starship = state.my_minions[action.starship_index]
            if starship.card_id != action.card_id:
                return self._reject("星舰内容已经变化，未执行发射。")
            if (action.starship_entity_id is not None
                    and getattr(starship, "entity_id", None)
                    != action.starship_entity_id):
                return self._reject("星舰对象已经变化，未执行发射。")
            zone_pos = getattr(starship, "zone_pos", 0)
            board_count = self._board_slot_count(state, "friendly")
            if not 1 <= zone_pos <= board_count:
                return self._reject("星舰战场位置已经失效，未执行发射。")
            self.executor.launch_starship(zone_pos - 1, board_count)
            return ActionExecutionResult(True, "已发射星舰。")

        if isinstance(action, AttackAction):
            if not self._target_exists(action.attacker, state):
                return self._reject("攻击者已经不存在，未执行操作。")
            if not self._target_exists(action.target, state):
                return self._reject("攻击目标已经不存在，未执行操作。")

            self.executor.attack(
                self._target_for_click(action.attacker, state),
                self._target_for_click(action.target, state),
                self._board_slot_count(state, "friendly"),
                self._board_slot_count(state, "enemy"),
            )
            return ActionExecutionResult(True, "已执行攻击。")

        if isinstance(action, EndTurnAction):
            self.executor.end_turn()
            return ActionExecutionResult(True, "已请求结束回合。")

        if isinstance(action, TimelineAction):
            if action.choice not in ("undo", "keep"):
                return self._reject("未知的时间线操作，未执行。")
            self.executor.timeline_click(action.choice)
            return ActionExecutionResult(
                True, "已点击回溯。" if action.choice == "undo"
                else "已点击维持。")

        return self._reject("未知操作，未执行。")

    def run_turn_step(self, state, refresh_state) -> ActionExecutionResult:
        self.output(state.format_for_manual_control())
        action = self.prompt_turn_action(state)
        action = self.bind_to_turn(action, state)
        if isinstance(action, RefreshAction):
            refresh_state()
            return ActionExecutionResult(False, "已刷新战局状态。")

        fresh_state = refresh_state()
        if fresh_state is None:
            return self._reject("未能刷新日志状态，未执行操作。")
        return self.execute(action, fresh_state)

