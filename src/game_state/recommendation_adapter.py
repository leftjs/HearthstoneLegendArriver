"""Map display slots in HSAng instructions to identity-bound manual actions."""

from dataclasses import dataclass, replace

from manual_controller import (
    ChooseTargetAction,
    AttackAction, DiscoverChoiceAction, EndTurnAction, HeroPowerAction,
    FRIENDLY_HAND_TARGET_CARD_IDS, LaunchStarshipAction, PlayCardAction,
    Target, TimelineAction, TitanAbilityAction, TradeCardAction,
    UseLocationAction,
)
from src.recommendation_models import ActionKind
from src.game_state.choose_one import choose_one_card_ids
from src.game_state.titan import (
    TITAN_ABILITY_COUNT, static_ability_choice, titan_card_ids,
)


class RecommendationStateError(ValueError):
    pass


@dataclass(frozen=True)
class BoardEntry:
    kind: str
    collection_index: int
    entity: object


@dataclass(frozen=True)
class AdaptedAction:
    manual_action: object
    source_entity_id: str | None
    target_entity_id: str | None
    postcondition: str
    # 单行诊断：适配时发现「面板」与「日志状态」不一致（例如手牌类型/顺序错位）时，
    # 由流程层打进日志。空串表示没有异常。
    note: str = ""


def _entity_label(entry):
    entity = entry.entity
    return (f"{entry.kind}#{getattr(entity, 'card_id', None) or '?'}"
            f"(eid={getattr(entity, 'entity_id', None) or '?'},"
            f"zp={getattr(entity, 'zone_pos', None) or '?'})")


def _board_snapshot(entries):
    """场面速写：出错时把冲突实体一起带进诊断，便于事后定位。

    诊断串会原样进入浮窗日志（``retry:<消息>``），所以必须保持单行且简短。
    """
    if not entries:
        return "empty"
    return "[" + ", ".join(_entity_label(entry) for entry in entries) + "]"


def ordered_board(state, side):
    minions = state.my_minions if side == "friendly" else state.oppo_minions
    locations = (getattr(state, "my_locations", []) if side == "friendly"
                 else getattr(state, "oppo_locations", []))
    entries = [BoardEntry("minion", i, entity)
               for i, entity in enumerate(minions)]
    entries.extend(BoardEntry("location", i, entity)
                   for i, entity in enumerate(locations))
    positions = [getattr(entry.entity, "zone_pos", 0) for entry in entries]
    if any(position <= 0 for position in positions):
        raise RecommendationStateError(
            f"board_position_unknown:{side}:{_board_snapshot(entries)}")
    if len(set(positions)) != len(positions):
        duplicates = sorted({position for position in positions
                             if positions.count(position) > 1})
        raise RecommendationStateError(
            f"duplicate_board_position:{side}:dup={duplicates}:"
            f"{_board_snapshot(entries)}")
    return tuple(sorted(entries, key=lambda entry: entry.entity.zone_pos))


def board_slot(state, side, one_based_index):
    board = ordered_board(state, side)
    if not 1 <= one_based_index <= len(board):
        raise RecommendationStateError(
            f"board_slot_out_of_range:{side}:asked={one_based_index}:"
            f"len={len(board)}:{_board_snapshot(board)}")
    return board[one_based_index - 1]


def adapt_action(proposed, state):
    if proposed.turn_number != state.game_num_turns_in_play:
        raise RecommendationStateError("turn_changed")
    if proposed.action == ActionKind.PLAY_CARD:
        adapted = _adapt_play_card(proposed, state)
        card = state.my_hand_cards[proposed.source.index - 1]
        return _with_choose_one(proposed, state, adapted, card)
    if proposed.action == ActionKind.TRADE_CARD:
        return _adapt_trade_card(proposed, state)
    if proposed.action == ActionKind.USE_HERO_POWER:
        adapted = _adapt_hero_power(proposed, state)
        return _with_choose_one(
            proposed, state, adapted, getattr(state, "my_hero_power", None))
    if proposed.action == ActionKind.ATTACK:
        return _adapt_attack(proposed, state)
    if proposed.action == ActionKind.USE_LOCATION:
        entry = board_slot(state, "friendly", proposed.source.index)
        if entry.kind != "location":
            raise RecommendationStateError("source_not_location")
        location = entry.entity
        target = None
        target_id = None
        if proposed.target is not None:
            side = proposed.target.owner
            if side not in {"friendly", "enemy"}:
                raise RecommendationStateError(
                    "location_target_unsupported")
            if proposed.target.kind == "hero":
                hero = (getattr(state, "my_hero", None)
                        if side == "friendly"
                        else getattr(state, "oppo_hero", None))
                if hero is None:
                    raise RecommendationStateError(
                        "location_target_missing")
                target_id = getattr(hero, "entity_id", None)
                target = Target(side, "hero", None, target_id)
            elif proposed.target.kind == "board_slot":
                target_entry = board_slot(
                    state, side, proposed.target.index)
                if target_entry.kind != "minion":
                    raise RecommendationStateError("target_not_minion")
                target_id = getattr(target_entry.entity, "entity_id", None)
                target = Target(
                    side, "minion", target_entry.collection_index,
                    target_id)
            else:
                raise RecommendationStateError(
                    "location_target_unsupported")
        manual = UseLocationAction(
            entry.collection_index, location.card_id,
            getattr(location, "entity_id", None), target=target)
        return AdaptedAction(manual, getattr(location, "entity_id", None),
                             target_id, "location_changed")
    if proposed.action == ActionKind.CHOOSE_DISCOVER:
        choice_count = getattr(state, "discover_choice_count", None)
        if choice_count not in (1, 2, 3, 4):
            if getattr(state, "target_choice_open", False):
                # 没有 GENERAL 选择框，但游戏正等我方选目标：盒子这时说的
                # 「选择我方N号位卡牌」指的是**我方场上**第 N 个随从。
                return _adapt_friendly_target(proposed, state)
            raise RecommendationStateError(
                f"discover_choice_count_unavailable:count={choice_count}")
        if (proposed.source is None
                or proposed.source.kind != "discover_slot"
                or proposed.source.owner != "friendly"
                or not 1 <= proposed.source.index <= choice_count):
            raise RecommendationStateError(
                f"discover_slot_out_of_range:count={choice_count}:"
                f"kind={getattr(proposed.source, 'kind', None)}:"
                f"owner={getattr(proposed.source, 'owner', None)}:"
                f"index={getattr(proposed.source, 'index', None)}")
        return AdaptedAction(
            DiscoverChoiceAction(
                proposed.source.index - 1, choice_count),
            None, None, "choice_resolved")
    if proposed.action == ActionKind.CHOOSE_TARGET:
        return _adapt_choose_target(proposed, state)
    if proposed.action == ActionKind.TITAN_ABILITY:
        return _adapt_titan_ability(proposed, state)
    if proposed.action == ActionKind.END_TURN:
        return AdaptedAction(EndTurnAction(), None, None, "turn_changed")
    if proposed.action == ActionKind.TIMELINE_UNDO:
        # 回溯：撤销 HSAng 时间线里上一步操作，纯 HSAng 侧 UI，不动 Power.log。
        return AdaptedAction(
            TimelineAction("undo"), None, None, "timeline_clicked")
    if proposed.action == ActionKind.TIMELINE_KEEP:
        # 维持：保留当前操作、关掉 HSAng 的撤销提示，同样不改 Power.log。
        return AdaptedAction(
            TimelineAction("keep"), None, None, "timeline_clicked")
    raise RecommendationStateError("unsupported_action")


def _adapt_choose_target(proposed, state):
    """「选择对方N号位卡牌 / 选择对方英雄」= 效果正在等我方点目标。

    实测 2026-10-24 08:57:50（滑行者巨怪战吼选完「克苏恩的凝视」）：选择框变成
    ``ChoiceType=TARGET``，实体是对方的两个随从，盒子说「选择对方2号位卡牌」，
    号位对应的是**场面位置**（对方2号位 = 小鬼奶奶），不是选择框下标。
    """
    if proposed.target is None:
        raise RecommendationStateError("choice_target_required")
    target, target_id = _board_target(state, proposed.target, "choice")
    return AdaptedAction(ChooseTargetAction(target), None, target_id,
                         "target_chosen")


def _adapt_friendly_target(proposed, state):
    """指向选择里盒子的「选择我方N号位卡牌」= 点我方场面第 N 个随从。"""
    index = getattr(getattr(proposed, "source", None), "index", None)
    if index is None:
        raise RecommendationStateError("choice_target_required")
    entry = board_slot(state, "friendly", index)
    if entry.kind != "minion":
        raise RecommendationStateError("target_not_minion")
    entity_id = getattr(entry.entity, "entity_id", None)
    return AdaptedAction(
        ChooseTargetAction(Target("friendly", "minion",
                                  entry.collection_index, entity_id)),
        None, entity_id, "target_chosen")


def _with_choose_one(proposed, state, adapted, source):
    """把盒子给的抉择名解析成「点面板第几个」。

    盒子文本里出现「选择卡牌 + 选项名」，本身就证明这是一次抉择——判据不需要
    卡表。真正缺的只是**点面板第几个**，它来自引擎的 ``subOption`` 下标：

      1. 当前 Power.log 里这张牌的子项（最权威，还带可用性 error）；
      2. 否则用本局学到过的顺序（子项只在牌「当前可打出」时下发，盒子给推荐时
         往往已经不再下发；实测同一张牌的顺序固定，学到一次就够）。

    两处都拿不到才报错重试，绝不盲点。
    """
    name = getattr(proposed, "choice_name", None)
    if name is None or source is None:
        return adapted
    if source.card_id in titan_card_ids():
        # 泰坦从手牌打出时，盒子给的是「打出N号位随从 + 选择卡牌 + 技能名」，
        # 和抉择同形：落地后服务器弹出的就是三张技能牌，选完才轮到目标。
        index, count = _titan_ability_choice(
            state, source.card_id, getattr(source, "entity_id", None), name)
        return replace(adapted, manual_action=replace(
            adapted.manual_action, choose_one=DiscoverChoiceAction(index, count)))
    if (isinstance(adapted.manual_action, PlayCardAction)
            and adapted.manual_action.cardtype not in {"SPELL", "MINION"}):
        raise RecommendationStateError(
            f"choose_one_cardtype_unsupported:card={source.card_id}"
            f":type={adapted.manual_action.cardtype}:name={name}")
    entity_id = getattr(source, "entity_id", None)
    detail = f"card={source.card_id}:eid={entity_id}:name={name}"
    choices = _power_option_choices(state, source.card_id, entity_id)
    if choices:
        index, count = _choose_one_from_choices(choices, name)
        return replace(adapted, manual_action=replace(
            adapted.manual_action, choose_one=DiscoverChoiceAction(index, count)))
    learned = _learned_choice(state, source.card_id, name)
    if learned is not None:
        index, count = learned
        return replace(adapted, manual_action=replace(
            adapted.manual_action, choose_one=DiscoverChoiceAction(index, count)))
    if source.card_id in choose_one_card_ids():
        # 卡表认得它是抉择牌，但这次日志里没有可用的子项：多半只读了一半，
        # 报错重试即可自愈。
        raise RecommendationStateError(
            f"choose_one_options_unavailable:{detail}"
            f":learned={_learned_labels(state, source.card_id)}")
    # 盒子的选项名没有对应的状态依据，卡表也不认识这张牌：这是识别缺口，
    # 宁可报错重试也不盲点——盲点只会点开选择框然后卡住（连点）。
    raise RecommendationStateError(f"choose_one_card_unknown:{detail}")


def _learned_labels(state, card_id):
    """学过的顺序，按 ``下标:名字`` 拼成诊断串（表里存的是 {下标: 名字}）。"""
    layouts = getattr(state, "power_option_layouts", None) or {}
    table = layouts.get(card_id) or {}
    return _choices_brief(table)


def _learned_choice(state, card_id, name):
    """用本局学过的顺序解析 ``(下标, 选项数量)``；没有则 None。

    ``power_option_layouts`` 里存的是「子项名 → 面板下标」。历史上泰坦的
    ``titan_ability_choices`` 是反过来的 ``{下标: 名字}``，为兼容两种形状，
    这里两种都认。
    """
    layouts = getattr(state, "power_option_layouts", None) or {}
    table = layouts.get(card_id) or {}
    if not table:
        return None
    # {名字: 下标}
    if name in table and isinstance(table[name], int):
        count = len(table)
        if count in (1, 2, 3, 4) and all(
                isinstance(v, int) for v in table.values()):
            return table[name], count
    # {下标: 名字}
    count = len(table)
    if count not in (1, 2, 3, 4):
        return None
    matches = [str(index) for index, option_name in table.items()
               if option_name == name]
    if len(matches) != 1:
        return None
    try:
        return int(matches[0]), count
    except ValueError:
        return None


def _choose_one_from_choices(choices, name):
    """在 option/subOption 子项表里按名字唯一定位 ``(下标, 选项数量)``。

    失败一律带上盒子给的名字和整张选项表：``choose_one_name_not_unique``
    在 2026-09-21 07:51 卡了 112 次，当时日志里只有错误码，无法判断是哪个
    选项重名。
    """
    count = len(choices)
    if count not in (1, 2, 3, 4) or set(choices) != set(range(count)):
        raise RecommendationStateError(
            f"choose_one_options_incomplete:name={name}"
            f":choices={_choices_brief(choices)}")
    matches = [index for index, choice in choices.items()
               if choice.get("name") == name]
    if len(matches) != 1:
        raise RecommendationStateError(
            f"choose_one_name_not_unique:name={name}"
            f":indices={matches}:choices={_choices_brief(choices)}")
    index = matches[0]
    if choices[index].get("error") not in {"NONE", "REQ_TARGET_TO_PLAY"}:
        raise RecommendationStateError(
            f"choose_one_option_unavailable:name={name}:index={index}"
            f":error={choices[index].get('error')}"
            f":choices={_choices_brief(choices)}")
    return index, count


def _adapt_titan_ability(proposed, state):
    """「操作N号位泰坦」+「选择卡牌 技能名」+（可选）目标 的执行计划。

    点击顺序与盒子文字顺序无关：先点自己场上的泰坦弹出技能面板，再点技能，
    最后（技能需要目标时）点目标。技能面板与发现/抉择共用居中卡牌布局，
    所以技能下标沿用 DiscoverChoiceAction 的点击方式。
    """
    entry = board_slot(state, "friendly", proposed.source.index)
    if entry.kind != "minion":
        raise RecommendationStateError("source_not_minion")
    titan = entry.entity
    name = getattr(proposed, "choice_name", None)
    if not name:
        raise RecommendationStateError("titan_ability_name_required")
    index, count = _titan_ability_choice(
        state, titan.card_id, getattr(titan, "entity_id", None), name)
    target = None
    target_id = None
    if proposed.target is not None:
        target, target_id = _titan_target(state, proposed.target)
    manual = TitanAbilityAction(
        entry.collection_index, titan.card_id, index, count,
        ability_name=name, target=target,
        titan_entity_id=getattr(titan, "entity_id", None))
    return AdaptedAction(manual, getattr(titan, "entity_id", None),
                         target_id, "titan_ability_used")


def _choices_brief(options):
    """技能/子项表的单行速写：``下标:名字(error)``。"""
    if not options:
        return "-"
    parts = []
    for index in sorted(options):
        option = options[index]
        if isinstance(option, dict):
            label = f"{index}:{option.get('name') or '?'}"
            error = option.get("error")
            if error and error != "NONE":
                label += f"({error})"
        else:
            label = f"{index}:{option}"
        parts.append(label)
    return "[" + ", ".join(parts) + "]"


def _titan_ability_choice(state, card_id, entity_id, name):
    """把盒子给的泰坦技能名解析成 ``(面板下标, 技能数量)``。

    依次尝试：本次 Power.log 的 option/subOption → 本局学到过的技能顺序 →
    静态兜底表。每一步都要求技能面板是完整的三张（见 TITAN_ABILITY_COUNT）：
    不完整的来源直接跳过（多半是日志只读到一半），全都对不上就报错重试，
    绝不猜着点。

    所有失败都把「盒子给的名字 + 三个来源里实际有什么」带进诊断：泰坦卡手时
    日志里只有错误码，无法判断是名字对不上、日志没读全，还是场面已变。
    """
    current = _power_option_choices(state, card_id, entity_id)
    current_index = _titan_index_of(current, name)
    if current_index is not None and _complete_titan_options(current):
        if current[current_index].get("error") not in {"NONE",
                                                       "REQ_TARGET_TO_PLAY"}:
            raise RecommendationStateError(
                f"titan_ability_unavailable:card={card_id}:eid={entity_id}:"
                f"name={name}:current={_choices_brief(current)}")
        return current_index, len(current)
    learned = (getattr(state, "titan_ability_choices", None)
               or {}).get(card_id) or {}
    learned_index = _titan_index_of(learned, name)
    if learned_index is not None and _complete_titan_options(learned):
        return learned_index, len(learned)
    static = static_ability_choice(card_id, name)
    if static is not None:
        return static
    detail = (f"card={card_id}:eid={entity_id}:name={name}"
              f":current={_choices_brief(current)}"
              f":learned={_choices_brief(learned)}")
    if current_index is not None or learned_index is not None:
        raise RecommendationStateError(
            f"titan_ability_options_incomplete:{detail}")
    raise RecommendationStateError(
        f"titan_ability_options_unavailable:{detail}")


def _titan_index_of(options, name):
    """技能名在选项表里的唯一下标；没有则 None，出现多个直接报错。

    options 既可能是 Power.log 的 ``{下标: {"name": ...}}``，也可能是本局学到的
    ``{下标: 技能名}``。
    """
    matches = [index for index, option in (options or {}).items()
               if (option.get("name") if isinstance(option, dict) else option)
               == name]
    if len(matches) > 1:
        # 带上重名与全部候选：同名技能出现两次时，日志里必须能看出重复的是谁。
        raise RecommendationStateError(
            f"titan_ability_name_not_unique:name={name}"
            f":indices={matches}:options={_choices_brief(options)}")
    return matches[0] if matches else None


def _complete_titan_options(options):
    """技能面板必须是完整的三张：不完整一律当作日志没读全。"""
    return bool(options) and len(options) == TITAN_ABILITY_COUNT and (
        set(options) == set(range(TITAN_ABILITY_COUNT)))


def _is_my_entity_id(state, entity_id):
    """实体是否在己方手牌/场上（控制权判定，不看 ``player=`` 原主字段）。"""
    if entity_id is None:
        return False
    collections = (
        getattr(state, "my_hand_cards", ()),
        getattr(state, "my_minions", ()),
        getattr(state, "my_locations", ()),
    )
    return any(
        str(getattr(entity, "entity_id", None)) == str(entity_id)
        for collection in collections for entity in (collection or ()))


def _power_option_choices(state, card_id, entity_id):
    """当前 Power.log 里这张牌的可选子项（没有/不是自己控制的牌时返回 None）。

    控制权用「实体在不在己方手牌/场上」判定，**不能**用选项里的 ``player=``：
    那是牌的**原主**（owner），抢过来的随从（如「控制权变更」偷来的泰坦）原主
    仍是对手，但 CONTROLLER 已经是自己——按原主判定会把这类牌全部拒掉，
    表现就是 titan_ability_options_unavailable 卡死。
    """
    option = (getattr(state, "power_options", None) or {}).get(entity_id)
    if option is None or option.get("card_id") != card_id:
        return None
    if not _is_my_entity_id(state, entity_id):
        return None
    return option.get("choices") or None


def _board_target(state, ref, prefix):
    """技能目标（随从或英雄），返回 ``(Target, entity_id)``。

    ``prefix`` 只用于诊断码前缀（``titan`` / ``old_god``），便于从日志一眼看出
    是哪种牌卡住了。
    """
    side = ref.owner
    if side not in {"friendly", "enemy"}:
        raise RecommendationStateError(f"{prefix}_target_unsupported")
    if ref.kind == "hero":
        hero = (getattr(state, "my_hero", None) if side == "friendly"
                else getattr(state, "oppo_hero", None))
        if hero is None:
            raise RecommendationStateError(f"{prefix}_target_missing")
        entity_id = getattr(hero, "entity_id", None)
        return Target(side, "hero", None, entity_id), entity_id
    if ref.kind == "board_slot":
        entry = board_slot(state, side, ref.index)
        if entry.kind != "minion":
            raise RecommendationStateError("target_not_minion")
        entity_id = getattr(entry.entity, "entity_id", None)
        return Target(side, "minion", entry.collection_index, entity_id), entity_id
    raise RecommendationStateError(f"{prefix}_target_unsupported")


def _titan_target(state, ref):
    """泰坦技能的目标（随从或英雄），返回 ``(Target, entity_id)``。"""
    return _board_target(state, ref, "titan")


def _hand_snapshot(cards):
    """手牌速写：诊断里带上牌名与实体号，定位槽位错乱用。"""
    if not cards:
        return "empty"
    return "[" + ", ".join(
        f"{i + 1}:{getattr(card, 'card_id', None) or '?'}"
        f"(eid={getattr(card, 'entity_id', None) or '?'})"
        for i, card in enumerate(cards)) + "]"


def _hand_slot_error(where, asked, cards):
    return RecommendationStateError(
        f"hand_slot_out_of_range:{where}:asked={asked}:"
        f"len={len(cards)}:{_hand_snapshot(cards)}")


def _adapt_play_card(proposed, state):
    index = proposed.source.index - 1
    if not 0 <= index < len(state.my_hand_cards):
        raise _hand_slot_error("play", proposed.source.index,
                               state.my_hand_cards)
    card = state.my_hand_cards[index]
    # 出牌行为（随从落点 / 法术要不要点目标 / 武器直接使用）一律按**面板**说的
    # 类型来执行：盒子读的是游戏里的真实手牌，而日志状态里手牌顺序会错位——
    # 实测 2026-10-29 22:35：「打出1号位法术 潮起潮落 / 目标是对方1号位」被执行成
    # 了「点手牌 → 点屏幕中间 → 右键取消」，因为状态把 1 号槽认成了英雄牌
    # （死亡之翼），走的是英雄牌分支，目标被丢掉。
    # 状态里的卡号/实体号只用于「这一手还是不是同一张牌」的校验。
    cardtype = proposed.card_type or getattr(card, "cardtype", None)
    note = ""
    if (proposed.card_type and getattr(card, "cardtype", None)
            and proposed.card_type != card.cardtype):
        note = (f"手牌类型与面板不一致：面板={proposed.card_type} "
                f"状态={card.cardtype} 槽位={index + 1}"
                f"（{getattr(card, 'card_id', None)}），按面板执行")
    gap = None
    manual_target = None
    target_id = None
    if cardtype in {"MINION", "LOCATION"}:
        board_count = len(ordered_board(state, "friendly"))
        gap = (board_count if proposed.destination is None
               else proposed.destination.index - 1)
        if not 0 <= gap <= board_count:
            raise RecommendationStateError("minion_destination_out_of_range")
    if proposed.target is not None:
        if (card.card_id in FRIENDLY_HAND_TARGET_CARD_IDS
                and proposed.target.owner == "friendly"
                and proposed.target.kind in {"hand_slot", "board_slot"}):
            target_index = proposed.target.index - 1
            if not 0 <= target_index < len(state.my_hand_cards):
                raise RecommendationStateError("hand_target_out_of_range")
            if target_index == index:
                raise RecommendationStateError("hand_target_is_source")
            target_card = state.my_hand_cards[target_index]
            target_id = getattr(target_card, "entity_id", None)
            manual_target = Target(
                "friendly", "hand", target_index, target_id)
        elif proposed.card_type in {"SPELL", "MINION"}:
            target_error = ("spell_target" if proposed.card_type == "SPELL"
                            else "minion_target")
            if proposed.target.owner not in {"friendly", "enemy"}:
                raise RecommendationStateError(f"{target_error}_unsupported")
            if proposed.target.kind == "hero":
                hero = (getattr(state, "my_hero", None)
                        if proposed.target.owner == "friendly"
                        else getattr(state, "oppo_hero", None))
                if hero is None:
                    raise RecommendationStateError(f"{target_error}_missing")
                target_id = getattr(hero, "entity_id", None)
                manual_target = Target(
                    proposed.target.owner, "hero", None, target_id)
            elif proposed.target.kind == "board_slot":
                target = board_slot(
                    state, proposed.target.owner, proposed.target.index)
                if target.kind != "minion":
                    raise RecommendationStateError("target_not_minion")
                target_id = getattr(target.entity, "entity_id", None)
                manual_target = Target(
                    proposed.target.owner, "minion",
                    target.collection_index, target_id)
            else:
                raise RecommendationStateError(f"{target_error}_unsupported")
        else:
            raise RecommendationStateError("targeted_action_unsupported")
    manual = PlayCardAction(
        index, card.card_id, cardtype, gap_index=gap,
        target=manual_target,
        hand_entity_id=getattr(card, "entity_id", None))
    return AdaptedAction(
        manual, getattr(card, "entity_id", None), target_id,
                         "hand_card_left", note=note)


def _adapt_trade_card(proposed, state):
    index = proposed.source.index - 1
    if not 0 <= index < len(state.my_hand_cards):
        raise _hand_slot_error("trade", proposed.source.index,
                               state.my_hand_cards)
    card = state.my_hand_cards[index]
    entity_id = getattr(card, "entity_id", None)
    manual = TradeCardAction(
        index, card.card_id, card.cardtype, hand_entity_id=entity_id)
    return AdaptedAction(
        manual, entity_id, None, "hand_card_left")


def _adapt_hero_power(proposed, state):
    power = getattr(state, "my_hero_power", None)
    target = None
    target_id = None
    if proposed.target is not None:
        side = proposed.target.owner
        if side not in {"friendly", "enemy"}:
            raise RecommendationStateError("hero_power_target_unsupported")
        if proposed.target.kind == "hero":
            hero = (getattr(state, "my_hero", None)
                    if side == "friendly"
                    else getattr(state, "oppo_hero", None))
            if hero is None:
                raise RecommendationStateError("hero_power_target_missing")
            target_id = getattr(hero, "entity_id", None)
            target = Target(side, "hero", None, target_id)
        elif proposed.target.kind == "board_slot":
            entry = board_slot(state, side, proposed.target.index)
            if entry.kind != "minion":
                raise RecommendationStateError("target_not_minion")
            target_id = getattr(entry.entity, "entity_id", None)
            target = Target(
                side, "minion", entry.collection_index, target_id)
        else:
            raise RecommendationStateError("hero_power_target_unsupported")
    return AdaptedAction(
        HeroPowerAction(target=target),
        getattr(power, "entity_id", None), target_id,
        "hero_power_changed")


def _adapt_attack(proposed, state):
    if proposed.source.kind == "hero":
        hero = getattr(state, "my_hero", None)
        if hero is None:
            raise RecommendationStateError("friendly_hero_missing")
        source_target = Target(
            "friendly", "hero", None, getattr(hero, "entity_id", None))
        source_id = getattr(hero, "entity_id", None)
    else:
        source = board_slot(state, "friendly", proposed.source.index)
        if source.kind != "minion":
            raise RecommendationStateError("source_not_minion")
        source_id = getattr(source.entity, "entity_id", None)
        if source.entity.card_id == "SC_999t":
            manual = LaunchStarshipAction(
                source.collection_index,
                source.entity.card_id,
                source_id,
            )
            return AdaptedAction(
                manual, source_id, None, "starship_launched")
        source_target = Target(
            "friendly", "minion", source.collection_index, source_id)
    if proposed.target is None:
        raise RecommendationStateError("attack_target_required")
    if proposed.target.kind == "hero":
        hero = getattr(state, "oppo_hero", None)
        if hero is None:
            raise RecommendationStateError("enemy_hero_missing")
        target_id = getattr(hero, "entity_id", None)
        manual_target = Target("enemy", "hero", None, target_id)
    else:
        target = board_slot(state, "enemy", proposed.target.index)
        if target.kind != "minion":
            raise RecommendationStateError("target_not_minion")
        target_id = getattr(target.entity, "entity_id", None)
        manual_target = Target("enemy", "minion", target.collection_index,
                               target_id)
    postcondition = ("hero_combat_state_changed"
                     if proposed.source.kind == "hero"
                     else "combat_state_changed")
    return AdaptedAction(AttackAction(source_target, manual_target),
                         source_id, target_id, postcondition)
