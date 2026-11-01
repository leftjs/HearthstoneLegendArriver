"""Immutable evidence and action protocol shared across pipeline layers."""

from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional


class ActionKind(str, Enum):
    MULLIGAN = "mulligan"
    PLAY_CARD = "play_card"
    TRADE_CARD = "trade_card"
    USE_HERO_POWER = "use_hero_power"
    ATTACK = "attack"
    USE_LOCATION = "use_location"
    CHOOSE_DISCOVER = "choose_discover"
    CHOOSE_TARGET = "choose_target"
    TITAN_ABILITY = "titan_ability"
    OLD_GOD_ABILITY = "old_god_ability"
    END_TURN = "end_turn"
    TIMELINE_UNDO = "timeline_undo"
    TIMELINE_KEEP = "timeline_keep"


@dataclass(frozen=True)
class SlotRef:
    kind: str
    owner: Optional[str] = None
    index: Optional[int] = None


@dataclass(frozen=True)
class FrameEvidence:
    frame_id: str
    captured_at: float
    desktop_size: tuple[int, int]
    dpi: int
    window_handle: int
    foreground: bool
    recommendation_roi: tuple[int, int, int, int]
    exact_hash: str
    perceptual_hash: str
    panel_visible: bool
    pixels: Any = None


@dataclass(frozen=True)
class OcrLine:
    text: str
    confidence: float
    box: tuple[tuple[float, float], ...] = ()


@dataclass(frozen=True)
class OcrEvidence:
    frame_id: str
    created_at: float
    lines: tuple[OcrLine, ...]
    normalized_text: str
    confidence: float
    backend: str
    preprocessing: str
    # 过滤前的完整面板行（含「打法参考A/B」标题与牌名说明行）。``lines`` 只保留
    # 复核通过的行，事后排查卡手时看不出盒子原文，所以另存一份。
    raw_lines: tuple[OcrLine, ...] = ()
    raw_text: str = ""


@dataclass(frozen=True)
class ProposedAction:
    action_id: str
    frame_id: str
    created_at: float
    turn_number: int
    log_revision: int
    raw_instruction: str
    normalized_instruction: str
    action: ActionKind
    source: Optional[SlotRef] = None
    destination: Optional[SlotRef] = None
    target: Optional[SlotRef] = None
    mulligan_slots: tuple[int, ...] = ()
    card_type: Optional[str] = None
    ocr_confidence: float = 0.0
    semantic_confidence: float = 0.0
    choice_name: Optional[str] = None
    # 拼装面板的点击序列：盒子新文案「进行拼装【右2中1】」直接给出「卡位+词条号」，
    # 元组按盒子给的先后顺序排列，元素形如 ``("右", 2)``。空元组表示盒子没说清
    # （旧文案），由适配器退回默认策略。
    assemble_picks: tuple[tuple[str, int], ...] = ()


@dataclass(frozen=True)
class ValidationResult:
    accepted: bool
    code: str
    diagnostics: tuple[str, ...] = ()
    value: Any = None

    @classmethod
    def allow(cls, value: Any) -> "ValidationResult":
        return cls(True, "accepted", value=value)

    @classmethod
    def reject(cls, code: str, *diagnostics: str) -> "ValidationResult":
        return cls(False, code, diagnostics)
