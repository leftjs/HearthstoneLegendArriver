"""Validate recommendation evidence immediately before execution."""

from src.recommendation_config import RecommendationConfig
from src.recommendation_models import ValidationResult


def _flat_text(text, limit=160):
    """面板文本压成单行，便于放进一行诊断日志。"""
    flat = " / ".join(
        line for line in str(text or "").splitlines() if line.strip())
    if not flat:
        return "(empty)"
    return flat if len(flat) <= limit else flat[:limit] + "..."


class ConsumedActionStore:
    def __init__(self):
        self._keys = set()

    @staticmethod
    def key_for(proposed, adapted):
        return (
            proposed.turn_number, proposed.log_revision,
            proposed.normalized_instruction, adapted.source_entity_id,
            adapted.target_entity_id,
        )

    @staticmethod
    def describe(key):
        """把消费键压成单行诊断串（排查 already_consumed 闭环用）。"""
        try:
            turn, revision, instruction, source, target = key
        except (TypeError, ValueError):
            return repr(key)
        flat = " / ".join(
            line for line in str(instruction or "").splitlines()
            if line.strip())
        return (f"turn={turn},rev={revision},src={source or '-'},"
                f"tgt={target or '-'},instr={flat or '-'}")

    def contains(self, key):
        return key in self._keys

    def mark_consumed(self, key):
        self._keys.add(key)


class RecommendationValidator:
    def __init__(self, config=None):
        self.config = config or RecommendationConfig()

    def validate(self, proposed, adapted, stable_frame, current_frame,
                 current_ocr, state, log_revision,
                 flow_state, consumed, stopped):
        # State/identity freshness must be checked before frame/control gates.
        if proposed.turn_number != state.game_num_turns_in_play:
            return ValidationResult.reject(
                f"turn_changed:proposed={proposed.turn_number}:"
                f"state={state.game_num_turns_in_play}")
        if proposed.log_revision != log_revision:
            return ValidationResult.reject(
                f"revision_changed:proposed={proposed.log_revision}:"
                f"current={log_revision}")
        if proposed.ocr_confidence < self.config.min_ocr_confidence:
            return ValidationResult.reject(
                f"ocr_confidence:{proposed.ocr_confidence:.2f}<"
                f"{self.config.min_ocr_confidence}")
        if proposed.semantic_confidence < 1.0:
            return ValidationResult.reject("semantic_confidence")
        if not getattr(state, "is_my_turn", False):
            return ValidationResult.reject("not_player_turn")
        if stable_frame.frame_id != proposed.frame_id:
            return ValidationResult.reject("stale_frame")
        if current_ocr.frame_id != current_frame.frame_id:
            return ValidationResult.reject("current_ocr_frame")
        if current_ocr.normalized_text != proposed.normalized_instruction:
            # 定位后到点击前面板文本变了：带上变化前后的文本便于复盘。
            return ValidationResult.reject(
                f"recommendation_changed:before={_flat_text(proposed.normalized_instruction)}"
                f":after={_flat_text(current_ocr.normalized_text)}")
        if current_frame.desktop_size != self.config.desktop_size:
            return ValidationResult.reject("desktop_size")
        if current_frame.dpi != self.config.desktop_dpi:
            return ValidationResult.reject("dpi")
        if not current_frame.window_handle or not current_frame.foreground:
            return ValidationResult.reject("window")
        if not current_frame.panel_visible:
            return ValidationResult.reject("recommendation_panel")
        if stopped:
            return ValidationResult.reject("stopped")
        if flow_state != "PLAYER_TURN":
            return ValidationResult.reject("flow_state")
        key = consumed.key_for(proposed, adapted)
        if consumed.contains(key):
            # 带上消费键：already_consumed 连环卡死时需要知道是哪条动作被判重。
            return ValidationResult.reject(
                f"already_consumed:{consumed.describe(key)}")
        return ValidationResult.allow((adapted, key))
