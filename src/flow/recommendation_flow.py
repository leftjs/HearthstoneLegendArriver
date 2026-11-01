"""Execute exactly one validated recommendation and verify its result."""

from dataclasses import dataclass, replace
from enum import Enum
import time

from src.flow.hand_animation_delay import HandAnimationDelay
from src.recommendation_models import ActionKind
from src.safety.recommendation_validator import ConsumedActionStore


# 「回合号还没记录过」的哨兵：不能用 None，因为有些状态里回合号就是 None。
_UNSET_TURN = object()

PANEL_DIAG_LIMIT = 400

# 适配器在「手牌槽位与盒子文本对不上」时抛的诊断码（见
# recommendation_adapter._with_choose_one）。命中它说明我们手里的槽位/实体号
# 已经和真实手牌不一致，光等下一条 Power.log 增量救不回来——这里主动重建一次
# 手牌快照再适配一次（见 `_adapt_with_rebuild`）。
_REBUILD_ON_ADAPT_CODES = ("choose_one_card_unknown",)


def _raw_panel_diag(evidence):
    """面板**原始**文本（含标题行/牌名行）的单行速写。

    解析失败时进入日志的是归一化文本，牌名行已被剥掉；这里补上 OCR 原文，
    才能看出盒子当时到底写了什么。
    """
    raw_lines = (getattr(evidence, "raw_lines", ())
                 or getattr(evidence, "lines", ()))
    parts = [part for part in (str(getattr(line, "text", "") or "").strip()
                               for line in raw_lines) if part]
    text = " | ".join(parts) if parts else str(
        getattr(evidence, "raw_text", "") or "")
    text = " ".join(text.split())
    if not text:
        return "(empty)"
    if len(text) > PANEL_DIAG_LIMIT:
        text = text[:PANEL_DIAG_LIMIT] + "..."
    return text


def _panel_diag(evidence, proposed):
    """盒子面板的可读单行速写：优先用 OCR 原文（含牌名/标题），回退到解析文本。

    诊断串会原样进入浮窗日志（``retry:<诊断>``），所以压成单行并限长。
    """
    raw_lines = getattr(evidence, "raw_lines", ()) or getattr(
        evidence, "lines", ())
    parts = [getattr(line, "text", "") for line in raw_lines]
    parts = [part for part in (p.strip() for p in parts) if part]
    text = " | ".join(parts) if parts else getattr(
        evidence, "raw_text", "")
    text = " ".join(str(text or "").split())
    instruction = " ".join(
        str(getattr(proposed, "normalized_instruction", "") or "").split())
    if not text:
        text = instruction or "(empty)"
    if len(text) > PANEL_DIAG_LIMIT:
        text = text[:PANEL_DIAG_LIMIT] + "..."
    return text


class FlowStepStatus(str, Enum):
    EXECUTED = "executed"
    OBSERVE = "observe"
    RETRY = "retry"


class ResultStatus(str, Enum):
    COMPLETED = "completed"
    WAITING_RECOMMENDATION = "waiting_recommendation"
    RETRY = "retry"


@dataclass(frozen=True)
class FlowStepResult:
    status: FlowStepStatus
    diagnostics: str = ""


class RecommendationFlow:
    def __init__(self, capture, reader, parser, state_supplier, adapter,
                 validator, controller, sleep=time.sleep,
                 clock=time.monotonic,
                 result_timeout=5.0, stopped=lambda: False,
                 consumed=None, post_action_delay=0.0,
                 post_action_pause=None,
                 hand_animation_delay=None,
                 draw_extra_delay_per_card=0.0,
                 rebuild_state=None):
        self.capture = capture
        self.reader = reader
        self.parser = parser
        self.state_supplier = state_supplier
        self.adapter = adapter
        self.validator = validator
        self.controller = controller
        self.sleep = sleep
        self.clock = clock
        # Kept for constructor compatibility; result verification no longer
        # waits for a timeout window, it checks once and retries immediately.
        self.result_timeout = result_timeout
        self.stopped = stopped
        self.consumed = consumed or ConsumedActionStore()
        self.post_action_delay = post_action_delay
        # 「活人感」（可选）：盒子意见执行完后的随机延时 + 鼠标在手牌区悬停。
        # 返回 True 表示已接管本次延时，就不再叠加固定的 post_action_delay。
        # 只有走到这里的“识别盒子意见并执行完”的对局动作才会触发它。
        self.post_action_pause = post_action_pause
        # 「抽牌额外延时」（秒/张，网页「延时设置」可调）。判定看手牌入场数：
        #   * 每回合开始那一下的常规抽 1 张不算（那 1 张是免费的）；
        #   * 同一回合里再多抽的（比如开局一次抽 2 张）要算；
        #   * 回合内任何操作触发的抽牌，每张都算。
        self.draw_extra_delay_per_card = float(draw_extra_delay_per_card or 0.0)
        self._seen_entry_count = None
        # 免费那张只让给“我方这个回合还没执行过动作时”出现的第一次手牌增加，
        # 这样 Power.log 晚几拍、出牌抽到的牌先被看到时，也不会被误当成免费那张。
        self._entry_turn = _UNSET_TURN
        self._entry_turn_exempt_used = False
        self._acted_this_turn = False
        self.hand_animation_delay = (
            hand_animation_delay
            if hand_animation_delay is not None
            else HandAnimationDelay(clock=clock))
        # 「手牌槽位对不上」时的强制重建钩子：无参调用，返回 (state, revision)；
        # 未注入时退化为不重建（只保留诊断），保证测试与旧调用方行为不变。
        self.rebuild_state = rebuild_state
        self.waiting_instruction = None
        self.waiting_panel_hash = None
        self.waiting_turn_number = None
        self.waiting_action_kind = None

    def _adapt_with_rebuild(self, proposed, state, revision):
        """适配一次；命中「槽位对不上」时重建手牌快照再适配一次。

        只对 `_REBUILD_ON_ADAPT_CODES` 里的诊断码生效，且**每轮重试只重建一次**，
        避免在真卡手时把死循环变成"重建风暴"。重建拿不到新状态就退回原诊断。

        返回 ``(adapted, proposed, state, revision, error)``：适配失败时 ``adapted``
        为 None，``error`` 是最后一次的异常（已含重建结果）。

        **重建成功时必须把 proposed.log_revision 一起换新**：强制重建会调用
        ``update_log_state()`` 吃掉新的 Power.log 行，``log_state.revision`` 因此前进
        （实测 2026-09-27：7 → 42），而 ``proposed`` 还带着解析时的旧 revision。
        紧随其后的校验器会以 ``revision_changed:proposed=7:current=42`` 拒掉这一轮，
        重建就白做了（用户会看到"重建了但一下都没点"）。这里直接对齐 revision，
        让重建出来的适配结果当场可用。
        """
        try:
            return self.adapter(proposed, state), proposed, state, revision, None
        except Exception as exc:                # noqa: BLE001
            first_error = exc
        if not self._needs_rebuild(first_error) or self.rebuild_state is None:
            return None, proposed, state, revision, first_error
        try:
            rebuilt, rebuilt_revision = self.rebuild_state()
        except Exception:                       # noqa: BLE001
            return None, proposed, state, revision, first_error
        if rebuilt is None:
            return None, proposed, state, revision, first_error
        # 重建后状态是新的，proposed 的 revision 也要跟上（见上面的 docstring）。
        if rebuilt_revision is not None:
            proposed = replace(proposed, log_revision=rebuilt_revision)
        try:
            adapted = self.adapter(proposed, rebuilt)
        except Exception as exc:                # noqa: BLE001
            return None, proposed, rebuilt, rebuilt_revision, exc
        return adapted, proposed, rebuilt, rebuilt_revision, None

    @staticmethod
    def _needs_rebuild(exc):
        text = f"{type(exc).__name__}:{exc}"
        return any(code in text for code in _REBUILD_ON_ADAPT_CODES)

    def run_player_turn_step(self):
        try:
            state, revision = self.state_supplier()
            self.hand_animation_delay.observe(
                getattr(state, "hand_entry_count", 0))
            # 回合开始的常规抽 1 张不算；这里处理“抽了 2 张以上”和上一步动作
            # 触发、日志晚几拍才写出来的抽牌。
            self._account_hand_entries(state)
            if not state.is_my_turn:
                return FlowStepResult(FlowStepStatus.OBSERVE, "opponent_turn")
            if (self.waiting_turn_number is not None
                    and self.waiting_turn_number
                    != state.game_num_turns_in_play):
                self._clear_waiting()
            observed = {}
            def frame_supplier():
                observed["frame"] = self.capture.capture(ocr_panel_ok=True)
                return observed["frame"]
            evidence = self.reader.read(
                frame_supplier, self.capture.crop_recommendation)
            stable_frame = observed["frame"]
            if self.waiting_instruction is not None:
                self._clear_waiting()
            try:
                proposed = self.parser.parse(
                    evidence, state.game_num_turns_in_play, revision)
            except Exception as exc:
                # 解析失败时推荐文本从未打印过（print 在 parse 之后），
                # 这里把 OCR 原文（含牌名行）补进诊断。
                return FlowStepResult(
                    FlowStepStatus.RETRY,
                    f"{type(exc).__name__}:{exc}"
                    f":raw_panel={_raw_panel_diag(evidence)}")
            if proposed.action == ActionKind.MULLIGAN:
                return FlowStepResult(
                    FlowStepStatus.OBSERVE,
                    "stale_mulligan_recommendation")
            adapted, proposed, state, revision, adapt_error = (
                self._adapt_with_rebuild(proposed, state, revision))
            if adapt_error is not None:
                # 适配失败时盒子原文从未打印过（print 在适配之后），补进诊断：
                # 卡手排查需要知道「盒子到底说了什么」。
                return FlowStepResult(
                    FlowStepStatus.RETRY,
                    f"{type(adapt_error).__name__}:{adapt_error}"
                    f":panel={_panel_diag(evidence, proposed)}")
            fresh_state, fresh_revision = self._wait_for_hand_animation()
            current_frame = self.capture.capture(ocr_panel_ok=True)
            current_evidence = self.reader.read_frame(
                current_frame, self.capture.crop_recommendation)
            if (current_evidence.normalized_text
                    != evidence.normalized_text):
                return FlowStepResult(
                    FlowStepStatus.RETRY, "recommendation_changed")
            validation = self.validator.validate(
                proposed, adapted, stable_frame, current_frame,
                current_evidence,
                fresh_state, fresh_revision, "PLAYER_TURN", self.consumed,
                self.stopped())
            if not validation.accepted:
                return FlowStepResult(FlowStepStatus.RETRY, validation.code)
            adapted, key = validation.value
            print(f"[推荐] {proposed.normalized_instruction}")
            note = getattr(adapted, "note", "")
            if note:
                # 面板与日志状态不一致时的单行提示（如手牌类型错位）：让日志里
                # 直接看出「为什么这一手这么点」，而不是等事后翻 Power.log。
                print(f"[提示] {note}")
            print("[执行] 开始点击。")
            result = self.controller.execute(adapted.manual_action, fresh_state)
            if not result.executed or result.recovery_needed:
                return FlowStepResult(FlowStepStatus.RETRY,
                                      "execution_failed")
            # 本回合已经动过手：之后再出现的手牌增加都是“操作触发的抽牌”，
            # 不能再当成回合开始那张免费的。
            self._acted_this_turn = True
            # 操作结束后延时再开始下一轮截图+OCR（盒子更新面板留时间）。
            # 通过 controller.output 推送延时行，让浮窗底部计时表显示该延时。
            # 「活人感」开启时由 post_action_pause 接管（随机 0.5~3s + 手牌悬停），
            # 此时不再叠加固定延时。
            paused = False
            if self.post_action_pause is not None:
                try:
                    paused = bool(self.post_action_pause())
                except Exception:
                    paused = False
            if not paused and self.post_action_delay:
                output = getattr(self.controller, "output", None)
                if output is not None:
                    try:
                        output(
                            f"[SYS] 操作结束，延时 "
                            f"{self.post_action_delay:.1f}s 后继续")
                    except Exception:
                        pass
                self.sleep(self.post_action_delay)
            # 动作执行后立刻查一次手牌有没有变多（这个操作触发了抽牌）：
            # 查到了就按张数追加延时；还没写进日志就留到下一步再查。
            # 关闭该功能（0）时不做这次额外读取，保持原有节奏。
            if self.draw_extra_delay_per_card > 0:
                self._account_hand_entries(self.state_supplier()[0])
            verified = self._verify_result(
                proposed, adapted, current_frame,
                fresh_state, fresh_revision)
            if verified == ResultStatus.COMPLETED:
                self.consumed.mark_consumed(key)
                return FlowStepResult(FlowStepStatus.EXECUTED)
            if verified == ResultStatus.WAITING_RECOMMENDATION:
                # A discover click is not complete while Power.log still
                # exposes a choice UI.  Keep it retryable until that UI
                # closes; all other confirmed actions retain duplicate
                # execution protection. 泰坦技能同理：点完泰坦/技能/目标后
                # 不再重复点，等盒子更新推荐（兵主这类还会弹出「发现」框）。
                # 古神技能也留着重试：万一这一下没点出去（面板还没弹开），
                # 下一轮同一个技能还能再点一次；已经用过的技能由适配器按
                # ACTIVATABLE_MINION_ABILITY_USED_n 拦下，不会重复释放。
                if proposed.action not in (ActionKind.CHOOSE_DISCOVER,
                                           ActionKind.TITAN_ABILITY,
                                           ActionKind.OLD_GOD_ABILITY):
                    self.consumed.mark_consumed(key)
                self.waiting_instruction = proposed.normalized_instruction
                self.waiting_panel_hash = current_frame.perceptual_hash
                self.waiting_turn_number = proposed.turn_number
                self.waiting_action_kind = proposed.action
                return FlowStepResult(
                    FlowStepStatus.OBSERVE,
                    "waiting_recommendation_update")
            # 执行成功但结果没被确认：把盒子原文一并带出，便于事后判断是
            # 「点击没生效」还是「盒子给的动作本身无法完成」。
            return FlowStepResult(
                FlowStepStatus.RETRY,
                f"result_not_confirmed:panel={_panel_diag(evidence, proposed)}")
        except Exception as exc:
            return FlowStepResult(
                FlowStepStatus.RETRY,
                f"{type(exc).__name__}:{exc}")

    def _wait_for_hand_animation(self):
        """等待手牌入场动画完成，再取最新状态做校验（上游性能调优逻辑）。"""
        while True:
            state, revision = self.state_supplier()
            self.hand_animation_delay.observe(
                getattr(state, "hand_entry_count", 0))
            remaining = self.hand_animation_delay.remaining()
            if remaining <= 0:
                return state, revision
            self.sleep(remaining)

    # -------------------------------------------------- 抽牌额外延时（回合内抽牌）
    @staticmethod
    def _hand_entry_count(state):
        """手牌入场计数（Power.log 里 ZONE→HAND 的累计次数）。"""
        try:
            return int(getattr(state, "hand_entry_count", 0) or 0)
        except (TypeError, ValueError):
            return 0

    def _account_hand_entries(self, state):
        """我方回合手牌变多 = 抽到牌 → 每张追加「抽牌额外延时」。

        规则（按需求）：
          * 每回合开始那一下的常规抽 1 张不算（本回合我方还没动作过时让掉 1 张）；
          * 同一回合里再多抽的（开局一次抽 2 张）算；
          * 回合内任何操作触发的抽牌，每张都算。
        对手回合的抽牌不归我们延时；换局导致计数清零时重新开始记账。
        该功能关闭（0 秒/张）时直接不记账。
        """
        if self.draw_extra_delay_per_card <= 0:
            return 0
        count = self._hand_entry_count(state)
        previous = self._seen_entry_count
        self._seen_entry_count = count

        turn = getattr(state, "game_num_turns_in_play", None)
        if self._entry_turn is _UNSET_TURN or turn != self._entry_turn:
            # 新回合（或新的一局）：免费那张与“本回合动作记录”都重新开始。
            self._entry_turn = turn
            self._entry_turn_exempt_used = False
            self._acted_this_turn = False

        if previous is None or count == previous:
            return 0
        if count < previous:                     # 换局/计数清零
            return 0
        if not getattr(state, "is_my_turn", False):
            return 0

        gained = count - previous
        if not self._entry_turn_exempt_used and not self._acted_this_turn:
            # 我方本回合还没动作过 → 这次增加是回合开始的常规抽牌，让掉 1 张。
            self._entry_turn_exempt_used = True
            gained -= 1
        if gained <= 0:
            return 0
        extra = gained * self.draw_extra_delay_per_card
        if extra <= 0:
            return gained
        self._announce(f"[SYS] 抽到 {gained} 张牌，额外延时 {extra:.1f}s 后继续")
        self.sleep(extra)
        return gained

    def _announce(self, text):
        """把延时说明同时打到控制台与浮窗（浮窗按「延时」关键字收）。"""
        try:
            print(text)
        except Exception:
            pass
        output = getattr(self.controller, "output", None)
        if output is None:
            return
        try:
            output(text)
        except Exception:
            pass

    def _frame_for(self, frame_id):
        # Capture implementations may retain the stable reader's current frame.
        current = getattr(self.capture, "last_frame", None)
        if current is not None and current.frame_id == frame_id:
            return current
        # Tests and simple suppliers can provide a fresh identity-equivalent frame.
        return type("FrameProxy", (), {
            "frame_id": frame_id, "desktop_size": (1920, 1080), "dpi": 96,
            "window_handle": 1, "foreground": True, "panel_visible": True,
        })()

    def _verify_result(self, proposed, adapted, before_frame, before_state,
                       before_revision):
        """Check the result once right after executing; no timeout waiting.

        A changed recommendation text confirms completion. A discover
        action or a satisfied log postcondition waits for the next
        recommendation. Anything else returns RETRY immediately so the
        caller re-executes the same action with fresh evidence instead of
        waiting for a result window.
        """
        before_instruction = proposed.normalized_instruction
        after_state, after_revision = self.state_supplier()
        log_changed = (after_revision > before_revision
                       and self._postcondition(
                           adapted.postcondition, before_state, after_state,
                           adapted.source_entity_id,
                           adapted.target_entity_id,
                           getattr(adapted.manual_action, "ability_index",
                                   None)))
        recommendation_changed = False
        try:
            after_frame = self.capture.capture(ocr_panel_ok=True)
            if after_frame.exact_hash != before_frame.exact_hash:
                supplied = False
                def frame_supplier():
                    nonlocal supplied
                    if not supplied:
                        supplied = True
                        return after_frame
                    return self.capture.capture(ocr_panel_ok=True)
                after_evidence = self.reader.read(
                    frame_supplier, self.capture.crop_recommendation)
                recommendation_changed = (
                    after_evidence.normalized_text != before_instruction)
        except Exception:
            recommendation_changed = False
        if recommendation_changed:
            return ResultStatus.COMPLETED
        if proposed.action in (ActionKind.TIMELINE_UNDO,
                               ActionKind.TIMELINE_KEEP):
            # 回溯/维持只影响 HSAng 侧 UI，不改动 Power.log：面板文本变了才算
            # 完成；没变也不能立刻 RETRY 重击（会二次撤销/误操作）。返回
            # WAITING 让上层标记已消费并等待盒子更新推荐。
            return ResultStatus.WAITING_RECOMMENDATION
        if proposed.action == ActionKind.CHOOSE_DISCOVER:
            return ResultStatus.WAITING_RECOMMENDATION
        if proposed.action == ActionKind.TITAN_ABILITY:
            # 「点泰坦→选技能→（可选）点目标」是一次性三步操作，绝不重复点：
            # 技能结算后盒子会给出下一步推荐（兵主还有「发现对应符文的牌」），
            # 所以这里统一按“等盒子更新推荐”处理。
            return ResultStatus.WAITING_RECOMMENDATION
        if proposed.action == ActionKind.OLD_GOD_ABILITY:
            # 古神一回合能连着选多次：只要这次技能真的结算了（日志里已有痕迹）
            # 就当场算完成并消费掉，好让盒子紧接着给出的**下一个技能**能立刻执行；
            # 日志还没写出来时按“等盒子更新”处理，不重复点同一张技能牌
            # （重复点会被适配器按 ACTIVATABLE_MINION_ABILITY_USED_n 拦下）。
            if log_changed:
                return ResultStatus.COMPLETED
            return ResultStatus.WAITING_RECOMMENDATION
        if log_changed:
            return ResultStatus.WAITING_RECOMMENDATION
        return ResultStatus.RETRY

    def _clear_waiting(self):
        self.waiting_instruction = None
        self.waiting_panel_hash = None
        self.waiting_turn_number = None
        self.waiting_action_kind = None

    @staticmethod
    def _hash_distance(left, right):
        if not left or not right or len(left) != len(right):
            return float("inf")
        try:
            return sum(
                (int(a, 16) ^ int(b, 16)).bit_count()
                for a, b in zip(left, right))
        except ValueError:
            return 0 if left == right else float("inf")

    @staticmethod
    def _postcondition(kind, before, after, source_entity_id=None,
                       target_entity_id=None, ability_slot=None):
        if kind == "turn_changed":
            return (after.game_num_turns_in_play != before.game_num_turns_in_play
                    or not after.is_my_turn)
        if kind == "hand_card_left":
            return source_entity_id not in {
                getattr(card, "entity_id", None)
                for card in after.my_hand_cards}
        if kind == "hero_power_changed":
            return (getattr(after.my_hero_power, "exhausted", 0)
                    != getattr(before.my_hero_power, "exhausted", 0)
                    or after.my_last_mana != before.my_last_mana)
        if kind == "choice_resolved":
            if (getattr(before, "discover_choice_count", None) in (1, 2, 3, 4)
                    and getattr(after, "discover_choice_count", None) is None):
                return True
            before_hand = tuple(
                getattr(card, "entity_id", None)
                for card in before.my_hand_cards)
            after_hand = tuple(
                getattr(card, "entity_id", None)
                for card in after.my_hand_cards)
            return before_hand != after_hand
        if kind == "location_changed":
            old = RecommendationFlow._entity_by_id(
                before.my_locations, source_entity_id)
            new = RecommendationFlow._entity_by_id(
                after.my_locations, source_entity_id)
            return new is None or (
                getattr(new, "action_cooldown", 0)
                != getattr(old, "action_cooldown", 0)
                or getattr(new, "damage", 0) != getattr(old, "damage", 0))
        if kind == "starship_launched":
            old = RecommendationFlow._entity_by_id(
                before.my_minions, source_entity_id)
            new = RecommendationFlow._entity_by_id(
                after.my_minions, source_entity_id)
            return old is not None and (
                new is None
                or getattr(new, "card_id", None)
                != getattr(old, "card_id", None))
        if kind == "titan_ability_used":
            # 泰坦技能：泰坦本体会被标记为已行动（EXHAUSTED 0→1）、可能掉血/
            # 加血；技能还会改场面/手牌（邪恶符文召唤、鲜血符文消灭目标），
            # 兵主这类结算完立刻弹出「发现」框（GENERAL choice 出现）。
            # 任意一条成立就认为技能确实打出去了。
            old_titan = RecommendationFlow._entity_by_id(
                before.my_minions, source_entity_id)
            new_titan = RecommendationFlow._entity_by_id(
                after.my_minions, source_entity_id)
            if old_titan is not None and (
                    new_titan is None
                    or getattr(new_titan, "exhausted", 0)
                    != getattr(old_titan, "exhausted", 0)
                    or getattr(new_titan, "damage", 0)
                    != getattr(old_titan, "damage", 0)
                    or getattr(new_titan, "max_health", 0)
                    != getattr(old_titan, "max_health", 0)):
                return True
            if (getattr(after, "discover_choice_count", None)
                    != getattr(before, "discover_choice_count", None)):
                return True
            if (RecommendationFlow._board_signature(after)
                    != RecommendationFlow._board_signature(before)):
                return True
            return (tuple(getattr(card, "entity_id", None)
                          for card in after.my_hand_cards)
                    != tuple(getattr(card, "entity_id", None)
                             for card in before.my_hand_cards))
        if kind == "old_god_ability_used":
            # 古神技能：用过的技能会打上 ACTIVATABLE_MINION_ABILITY_USED_{槽位+1}
            # （Power.log 实测，2026-10-24 00:38/00:40 克苏恩）。这一条最直接；
            # 拿不到标签时（测试桩/日志只读了一半）再退回场面、手牌、水晶、
            # 发现框这些旁证——只要有一条变了就认为技能确实打出去了。
            old_god = RecommendationFlow._entity_by_id(
                before.my_minions, source_entity_id)
            new_god = RecommendationFlow._entity_by_id(
                after.my_minions, source_entity_id)
            if old_god is None:
                return False
            if new_god is None:
                return True
            if RecommendationFlow._ability_flags_changed(old_god, new_god):
                return True
            if ability_slot is not None and ability_slot in (
                    getattr(new_god, "used_ability_slots", None) or ()):
                return True
            if ability_slot is not None and str(
                    RecommendationFlow._query_tag(
                        new_god,
                        f"ACTIVATABLE_MINION_ABILITY_USED_{ability_slot + 1}")
            ) == "1":
                return True
            if getattr(new_god, "exhausted", 0) != getattr(
                    old_god, "exhausted", 0):
                return True
            if getattr(new_god, "damage", 0) != getattr(old_god, "damage", 0):
                return True
            if getattr(new_god, "max_health", 0) != getattr(
                    old_god, "max_health", 0):
                return True
            if getattr(new_god, "attack", 0) != getattr(old_god, "attack", 0):
                return True
            if (getattr(after, "discover_choice_count", None)
                    != getattr(before, "discover_choice_count", None)):
                return True
            if (getattr(after, "my_last_mana", None)
                    is not None
                    and getattr(after, "my_last_mana", None)
                    != getattr(before, "my_last_mana", None)):
                return True
            if (RecommendationFlow._board_signature(after)
                    != RecommendationFlow._board_signature(before)):
                return True
            return (tuple(getattr(card, "entity_id", None)
                          for card in after.my_hand_cards)
                    != tuple(getattr(card, "entity_id", None)
                             for card in before.my_hand_cards))
        if kind == "target_chosen":
            # 指向选择：点完目标后 TARGET 选择框会关闭（实测 08:57:50 → 08:58:31）。
            # 这一条最可靠；拿不到时再看目标有没有掉血/死亡，最后退回场面/手牌变化。
            if (getattr(before, "target_choice_open", False)
                    and not getattr(after, "target_choice_open", False)):
                return True
            if target_entity_id is not None:
                old_target = (
                    RecommendationFlow._entity_by_id(
                        before.oppo_minions, target_entity_id)
                    or RecommendationFlow._entity_by_id(
                        before.my_minions, target_entity_id))
                new_target = (
                    RecommendationFlow._entity_by_id(
                        after.oppo_minions, target_entity_id)
                    or RecommendationFlow._entity_by_id(
                        after.my_minions, target_entity_id))
                if old_target is not None and (
                        new_target is None
                        or getattr(new_target, "damage", 0)
                        != getattr(old_target, "damage", 0)
                        or getattr(new_target, "max_health", 0)
                        != getattr(old_target, "max_health", 0)):
                    return True
            if (RecommendationFlow._board_signature(after)
                    != RecommendationFlow._board_signature(before)):
                return True
            return (tuple(getattr(card, "entity_id", None)
                          for card in after.my_hand_cards)
                    != tuple(getattr(card, "entity_id", None)
                             for card in before.my_hand_cards))
        if kind == "combat_state_changed":
            old_source = RecommendationFlow._entity_by_id(
                before.my_minions, source_entity_id)
            new_source = RecommendationFlow._entity_by_id(
                after.my_minions, source_entity_id)
            old_target = RecommendationFlow._entity_by_id(
                before.oppo_minions, target_entity_id)
            new_target = RecommendationFlow._entity_by_id(
                after.oppo_minions, target_entity_id)
            return (new_source is None or new_target is None
                    or getattr(new_source, "exhausted", 0)
                    != getattr(old_source, "exhausted", 0)
                    or getattr(new_target, "damage", 0)
                    != getattr(old_target, "damage", 0))
        if kind == "hero_combat_state_changed":
            old_hero = getattr(before, "my_hero", None)
            new_hero = getattr(after, "my_hero", None)
            old_target = RecommendationFlow._entity_by_id(
                before.oppo_minions, target_entity_id)
            new_target = RecommendationFlow._entity_by_id(
                after.oppo_minions, target_entity_id)
            return (new_hero is None or new_target is None
                    or getattr(new_hero, "exhausted", 0)
                    != getattr(old_hero, "exhausted", 0)
                    or getattr(new_hero, "damage", 0)
                    != getattr(old_hero, "damage", 0)
                    or getattr(new_target, "damage", 0)
                    != getattr(old_target, "damage", 0))
        return False

    @staticmethod
    def _entity_by_id(collection, entity_id):
        return next((entity for entity in collection
                     if getattr(entity, "entity_id", None) == entity_id), None)

    @staticmethod
    def _query_tag(entity, name):
        query = getattr(entity, "query_tag", None)
        if query is None:
            return "0"
        try:
            return str(query(name))
        except Exception:                        # noqa: BLE001
            return "0"

    @staticmethod
    def _ability_flags_changed(old, new):
        """实体的「技能已用」标记有没有变化。

        Power.log 里这类标记有两种写法：通用的
        ``ACTIVATABLE_MINION_ABILITY_USED_n`` 和卡牌专属的
        ``CTHUN_ABILITYn_USED``（写在玩家实体上），所以按名字里是否含
        ``ABILITY_USED`` / ``_ABILITY`` 一起比。
        """
        old_tags = getattr(old, "tag_dict", None) or {}
        new_tags = getattr(new, "tag_dict", None) or {}
        keys = {key for key in set(old_tags) | set(new_tags)
                if "ABILITY_USED" in str(key) or "_ABILITY" in str(key)}
        return any(old_tags.get(key) != new_tags.get(key) for key in keys)

    @staticmethod
    def _board_signature(state):
        """场面指纹：随从顺序 + 每个随从的 id/伤害/生命上限。"""
        return tuple(
            (getattr(entity, "entity_id", None),
             getattr(entity, "damage", 0),
             getattr(entity, "max_health", 0))
            for entity in (tuple(getattr(state, "my_minions", ()))
                           + tuple(getattr(state, "oppo_minions", ()))))
