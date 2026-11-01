import sys

from log_op import *
from json_op import *
from strategy_entity import *
from print_info import *
import constants.constants
from src.game_state.old_god import OLD_GOD_ABILITY_COUNT, ability_used_tag
from src.game_state.titan import titan_card_ids

MY_NAME = constants.constants.YOUR_NAME
# 进程级：my_player_id 自愈交换的警告只打一次，避免读取历史日志刷屏。
_MY_PLAYER_SWAP_WARNED = False


def check_name():
    global MY_NAME
    if MY_NAME == "ChangeThis#54321":
        MY_NAME = input("请输入你的炉石用户名, 例子: \"为所欲为、异灵术#54321\" (不用输入引号!)\n").strip()


# 日志里用来表示“名字不可见”的占位名：不能拿它当玩家真名来比对。
_UNKNOWN_PLAYER_MARKERS = ("UNKNOWN",)


def player_name_check(state, my_name=None):
    """比对日志里的玩家名与配置的用户 ID，返回 None（暂无法判断）或判定结果。

    返回形如 ``{"config": 配置值, "players": {PlayerID: 名字}, "matched": bool}``。

    为什么要做这个校验：脚本完全靠昵称区分敌我（`MY_NAME in entity_string`），
    换战网账号后若忘记改「用户 ID」，整局都会被判成对手回合而不出牌，且日志里
    看不出任何异常。这里在拿到双方玩家名后比对一次，不匹配就交给上层提示。

    判定规则（保守，宁可不提示也不误报）：
      * 必须已经读到两个 PlayerID 的玩家名（日志成对给出，只读到一条时无法判断）；
      * 至少有一个名字是真实昵称（"UNKNOWN HUMAN PLAYER" 这类占位名不算）；
      * 配置昵称出现在任一真实昵称里即视为匹配（与解析层 `in` 的语义一致）。
    """
    name = (MY_NAME if my_name is None else my_name) or ""
    name = str(name).strip()
    players = {str(pid): str(n).strip()
               for pid, n in getattr(state, "player_names", {}).items()
               if str(n).strip()}
    if len(players) < 2:
        return None
    real_names = [n for n in players.values()
                  if not any(marker in n.upper()
                             for marker in _UNKNOWN_PLAYER_MARKERS)]
    if not real_names:
        return None
    target = name.lower()
    matched = bool(target) and any(target in n.lower() for n in real_names)
    return {"config": name, "players": players, "matched": matched}


class LogState:
    def __init__(self):
        self.session_id = "unknown-session"
        self.game_entity_id = 0
        self.player_id_map_dict = {}
        self.my_name = ""
        self.oppo_name = ""
        # PlayerID -> 日志里给出的完整玩家名（"xxx#12345"）。用于校验配置的
        # 「用户 ID」是否与当前账号一致（换号后忘改昵称会整局不出牌）。
        self.player_names = {}
        self.my_player_id = "0"
        self.oppo_player_id = "0"
        self.entity_dict = {}
        self.current_update_id = 0
        self.revision = 0
        self.game_generation = 0
        self.general_choice_id = None
        self.general_choice_type = None
        self.general_choice_player = None
        self.general_choice_indexes = set()
        self.general_choice_ready = False
        self.general_choice_source_card = ""
        self.general_choice_source_player = None
        self.power_options = {}
        self.current_power_option_id = None
        # 本局学到过的「卡牌 → {技能下标: 技能名}」：泰坦打出的那个回合服务器会
        # 把三张技能子牌（subOption）一起下发，下标即技能面板位置。之后泰坦留在
        # 场上、日志不再重发子选项时，仍能按盒子给的技能名点对位置。
        self.titan_ability_choices = {}
        # 本局学到过的「卡牌 → {子项名: 面板下标}」：抉择牌（以及泰坦技能）在还能
        # 打出的那段窗口里，DebugPrintOptions 会把 subOption 连下标一起下发。
        # 实测这个顺序对同一张牌是固定的（星辰坠落 每次都是 0=星辰领主、
        # 1=星辰漂流），所以记下来之后，即使盒子给推荐时子项已经不再下发，
        # 仍能按名字点对位置——不再受「推荐来得比子项晚」的时序影响。
        self.power_option_layouts = {}
        # 手牌构成变化诊断：每次我方手牌/场面构成变化记一行。
        # 实测 2026-09-23 13:40 手牌整体错位 1 位（进坟场的牌被当成了还在手牌），
        # 逐次记录就能看到那张"多出来的牌"是哪一步冒出来的。
        self._hand_diag_signature = None
        # 手牌入口计数（上游逻辑：用于手牌动画延迟判断）。
        self.hand_entry_count = 0
        # 开局生效的全局卡数量（BLOCK_START TRIGGER + START_OF_GAME_KEYWORD
        # + cardId 非空）。第一回合额外延时按此计数（每张 +2s，可配置）。
        self.start_of_game_card_count = 0
        # 本局是否已警告过 my_player_id 交换（flush 每局重置，避免刷屏）。
        self._swap_warned = False

    def __str__(self):
        res = \
            f"""GameState:
    game_entity_id: {self.game_entity_id}
    my_name: {self.my_name}
    oppo_name: {self.oppo_name}
    my_player_id: {self.my_player_id}
    oppo_player_id: {self.oppo_player_id}
    current_update_id: {self.current_update_id}
    entity_keys: {[list(self.entity_dict.keys())]}

"""
        key_list = list(self.entity_dict.keys())
        key_list.sort(key=int)

        for key in key_list:
            if key == self.game_entity_id:
                res += "GameState-"
            elif key == self.my_entity_id:
                res += "MyEntity-"
            elif key == self.oppo_entity_id:
                res += "OppoEntity-"
            res += f"[{str(key)}]\n"
            res += str(self.entity_dict[key])
            res += "\n"

        return res

    @property
    def is_end(self):
        return self.game_state == "COMPLETE"

    @property
    def current_update_entity(self):
        return self.entity_dict[self.current_update_id]

    @property
    def game_entity(self):
        return self.entity_dict[self.game_entity_id]

    @property
    # 这不是my_player_id, 而是指代表我这个玩家的那个entity的index
    def my_entity_id(self):
        return self.player_id_map_dict.get(self.my_player_id, 0)

    @property
    def my_entity(self):
        return self.entity_dict[self.my_entity_id]

    @property
    def oppo_entity_id(self):
        return self.player_id_map_dict.get(self.oppo_player_id, 0)

    @property
    def oppo_entity(self):
        return self.entity_dict[self.oppo_entity_id]

    @property
    def is_my_turn(self):
        return self.my_entity.query_tag("CURRENT_PLAYER") == "1"

    @property
    def my_last_mana(self):
        return self.my_entity.query_tag("RESOURCES") - \
               self.my_entity.query_tag("RESOURCES_USED")

    @property
    def game_step(self):
        return self.game_entity.query_tag("STEP")

    @property
    def game_state(self):
        return self.game_entity.query_tag("STATE")

    @property
    def game_num_turns_in_play(self):
        return int(self.game_entity.query_tag("NUM_TURNS_IN_PLAY"))

    @property
    def available(self):
        return self.game_entity_id != 0

    @property
    def discover_choice_count(self):
        # 只认 ChoiceType=GENERAL：「发现/抉择/拼装」这些面板是 GENERAL；
        # 指向选择（TARGET）虽然也用同一套 DebugPrintEntityChoices，但那是
        # 「选一个目标」，卡位完全不同（见 target_choice_open）。
        if (self.general_choice_type != "GENERAL"
                or not self.general_choice_ready
                or self.general_choice_player != self.my_player_id):
            return None
        count = len(self.general_choice_indexes)
        return count if 1 <= count <= 4 else None

    @property
    def target_choice_open(self):
        """游戏是否正等我方为一个效果选目标（ChoiceType=TARGET）。

        实测 2026-10-24 08:57:50（滑行者巨怪战吼选完「克苏恩的凝视」）：
        选择框立刻变成 ``ChoiceType=TARGET``，Source=滑行者巨怪(player=1)，
        实体是对方的两个随从；40 秒后用户手动点了目标才结算。
        """
        if (self.general_choice_type != "TARGET"
                or not self.general_choice_ready):
            return False
        source_player = self.general_choice_source_player
        if source_player is None:
            return False
        return str(source_player) == str(self.my_player_id)

    def clear_general_choice(self):
        self.general_choice_id = None
        self.general_choice_type = None
        self.general_choice_player = None
        self.general_choice_indexes = set()
        self.general_choice_ready = False
        self.general_choice_source_card = ""
        self.general_choice_source_player = None

    def flush(self):
        revision = self.revision
        game_generation = self.game_generation + 1
        session_id = self.session_id
        self.__init__()
        self.revision = revision
        self.game_generation = game_generation
        self.session_id = session_id

    def add_entity(self, entity_id, entity):
        # 整体替换，**不继承旧标签**。
        # 曾试过「把旧实体的标签并入新实体」，用来兜住"重发批次不含 ZONE"的情况，
        # 但那是错的：同一局里实体 ID 会被回收给别的牌（实测 2026-09-23 那局
        # id=61 先是 剧毒之种 FP1_019、之后又变成 天空之墙哨兵 CATA_565），
        # 合并会把上一张牌的区域/控制者等标签贴到新牌上，制造出新的错乱。
        # ZONE 一类身份标签必须一律以服务器重发的 tag= 为准。
        assert entity_id.isdigit()
        self.entity_dict[entity_id] = entity

    def set_game_entity(self, game_entity_id, game_entity):
        self.game_entity_id = game_entity_id
        self.add_entity(game_entity_id, game_entity)

    def fetch_game_entity(self):
        return self.entity_dict[self.game_entity_id]

    def add_player_entity(self, player_entity_id, player_id, player_entity):
        self.add_entity(player_entity_id, player_entity)
        self.player_id_map_dict[player_id] = player_entity_id

    def is_my_entity(self, entity):
        return entity.query_tag("CONTROLLER") == self.my_player_id


class Entity:
    def __init__(self):
        self.tag_dict = {}

    def __str__(self):
        res = ""
        for key, value in self.tag_dict.items():
            res += f"\t{key}: {value}\n"
        return res

    def set_tag(self, tag, val):
        self.tag_dict[tag] = val

    def query_tag(self, tag, default_val="0"):
        return self.tag_dict.get(tag, default_val)

    @property
    def cardtype(self):
        return self.query_tag("CARDTYPE")

    @property
    def zone(self):
        return self.query_tag("ZONE")

    @property
    def used_ability_slots(self):
        """已经用掉的技能槽位（0 基），来自 ACTIVATABLE_MINION_ABILITY_USED_n。

        古神/泰坦这类「点开面板选技能」的随从，服务器会在用掉某个技能后给本体
        打上这个标签（实测 2026-10-24 克苏恩：用完凝视/仆从分别置
        ACTIVATABLE_MINION_ABILITY_USED_3/4=1）。古神技能每局每个只能用一次，
        适配器靠它拦住「同一张技能牌被重复点」。
        """
        used = set()
        for slot in range(1, OLD_GOD_ABILITY_COUNT + 1):
            if str(self.query_tag(ability_used_tag(slot - 1))) == "1":
                used.add(slot - 1)
        return frozenset(used)


class GameEntity(Entity):
    pass


class PlayerEntity(Entity):
    pass


class CardEntity(Entity):
    def __init__(self, card_id):
        super().__init__()
        self.card_id = card_id

    def __str__(self):
        return "cardID: " + self.card_id + "\n" + \
               "name: " + self.name + "\n" + \
               super().__str__()

    def generate_strategy_entity(self, log_state, entity_id=None):
        if self.cardtype == "MINION":
            return StrategyMinion(
                entity_id=entity_id,
                card_id=self.card_id,
                zone=self.query_tag("ZONE"),
                zone_pos=int(self.query_tag("ZONE_POSITION")),
                current_cost=int(self.query_tag("TAG_LAST_KNOWN_COST_IN_HAND")),
                overload=int(self.query_tag("OVERLOAD")),
                is_mine=log_state.is_my_entity(self),
                attack=int(self.query_tag("ATK")),
                max_health=int(self.query_tag("HEALTH")),
                damage=int(self.query_tag("DAMAGE")),
                taunt=int(self.query_tag("TAUNT")),
                divine_shield=int(self.query_tag("DIVINE_SHIELD")),
                stealth=int(self.query_tag("STEALTH")),
                windfury=int(self.query_tag("WINDFURY")),
                poisonous=int(self.query_tag("POISONOUS")),
                freeze=int(self.query_tag("FREEZE")),
                battlecry=int(self.query_tag("BATTLECRY")),
                spell_power=int(self.query_tag("SPELLPOWER")),
                not_targeted_by_spell=int(self.query_tag("CANT_BE_TARGETED_BY_SPELLS")),
                not_targeted_by_power=int(self.query_tag("CANT_BE_TARGETED_BY_HERO_POWERS")),
                charge=int(self.query_tag("CHARGE")),
                rush=int(self.query_tag("RUSH")),
                attackable_by_rush=int(self.query_tag("ATTACKABLE_BY_RUSH")),
                frozen=int(self.query_tag("FROZEN")),
                dormant=int(self.query_tag("DORMANT")),
                untouchable=int(self.query_tag("UNTOUCHABLE")),
                immune=int(self.query_tag("IMMUNE")),
                # -1代表标签缺失, 有两种情况会产生-1: 断线重连; 卡刚从手牌中被打出来
                exhausted=int(self.query_tag("EXHAUSTED")),
                cant_attack=int(self.query_tag("CANT_ATTACK")),
                num_turns_in_play=int(self.query_tag("NUM_TURNS_IN_PLAY")),
                used_ability_slots=self.used_ability_slots,
            )
        elif self.cardtype == "SPELL":
            return StrategySpell(
                entity_id=entity_id,
                card_id=self.card_id,
                zone=self.query_tag("ZONE"),
                zone_pos=int(self.query_tag("ZONE_POSITION")),
                current_cost=int(self.query_tag("TAG_LAST_KNOWN_COST_IN_HAND")),
                overload=int(self.query_tag("OVERLOAD")),
                is_mine=log_state.is_my_entity(self),
            )
        elif self.cardtype == "WEAPON":
            return StrategyWeapon(
                entity_id=entity_id,
                card_id=self.card_id,
                zone=self.query_tag("ZONE"),
                zone_pos=int(self.query_tag("ZONE_POSITION")),
                current_cost=int(self.query_tag("TAG_LAST_KNOWN_COST_IN_HAND")),
                overload=int(self.query_tag("OVERLOAD")),
                is_mine=log_state.is_my_entity(self),
                attack=int(self.query_tag("ATK")),
                durability=int(self.query_tag("DURABILITY")),
                damage=int(self.query_tag("DAMAGE")),
                windfury=int(self.query_tag("WINDFURY")),
            )
        elif self.cardtype == "LOCATION":
            return StrategyLocation(
                entity_id=entity_id,
                card_id=self.card_id,
                zone=self.query_tag("ZONE"),
                zone_pos=int(self.query_tag("ZONE_POSITION")),
                current_cost=int(self.query_tag("TAG_LAST_KNOWN_COST_IN_HAND")),
                overload=int(self.query_tag("OVERLOAD")),
                is_mine=log_state.is_my_entity(self),
                max_health=int(self.query_tag("HEALTH")),
                damage=int(self.query_tag("DAMAGE")),
                action_cooldown=int(
                    self.query_tag("LOCATION_ACTION_COOLDOWN")),
                exhausted=int(self.query_tag("EXHAUSTED")),
                just_played=int(self.query_tag("JUST_PLAYED")),
            )
        elif self.cardtype == "HERO":
            return StrategyHero(
                entity_id=entity_id,
                card_id=self.card_id,
                zone=self.query_tag("ZONE"),
                zone_pos=int(self.query_tag("ZONE_POS")),
                current_cost=int(self.query_tag("TAG_LAST_KNOWN_COST_IN_HAND")),
                overload=int(self.query_tag("OVERLOAD")),
                is_mine=log_state.is_my_entity(self),
                max_health=int(self.query_tag("HEALTH")),
                damage=int(self.query_tag("DAMAGE")),
                stealth=int(self.query_tag("STEALTH")),
                immune=int(self.query_tag("IMMUNE")),
                not_targeted_by_spell=int(self.query_tag("CANT_BE_TARGETED_BY_SPELLS")),
                not_targeted_by_power=int(self.query_tag("CANT_BE_TARGETED_BY_HERO_POWERS")),
                armor=int(self.query_tag("ARMOR")),
                attack=int(self.query_tag("ATK")),
                exhausted=int(self.query_tag("EXHAUSTED")),
                frozen=int(self.query_tag("FROZEN")),
                cant_attack=int(self.query_tag("CANT_ATTACK")),
            )
        elif self.cardtype == "HERO_POWER":
            return StrategyHeroPower(
                entity_id=entity_id,
                card_id=self.card_id,
                zone=self.query_tag("ZONE"),
                zone_pos=int(self.query_tag("ZONE_POS")),
                current_cost=int(self.query_tag("TAG_LAST_KNOWN_COST_IN_HAND")),
                overload=int(self.query_tag("OVERLOAD")),
                is_mine=log_state.is_my_entity(self),
                exhausted=int(self.query_tag("EXHAUSTED")),
            )
        else:
            return None

    @property
    def name(self):
        return query_json_dict(self.card_id)

    def update_card_id(self, card_id):
        self.card_id = card_id


def update_state(state, line_info_container):
    if line_info_container.line_type == LOG_LINE_CREATE_GAME:
        # sys_print("Read in new game and flush state")
        state.flush()

    if line_info_container.line_type == LOG_LINE_BLOCK_START_TRIGGER:
        # 一张开局生效的全局卡事件。Entity 括号里 cardId 可能当时为空，
        # 但实体 id 已在 SHOW_ENTITY 时注册过 card_id。据此解析真实卡牌，
        # 只有 card_id 非空才计为一张生效卡（排除无卡牌的通用机制触发）。
        entity = state.entity_dict.get(
            line_info_container.info_dict["entity_id"])
        card_id = getattr(entity, "card_id", "") if entity is not None else ""
        if card_id:
            state.start_of_game_card_count += 1

    if line_info_container.line_type == LOG_LINE_POWER_OPTIONS_START:
        state.power_options = {}
        state.current_power_option_id = None
    if line_info_container.line_type == LOG_LINE_POWER_OPTION:
        info = line_info_container.info_dict
        state.current_power_option_id = info.get("entity_id")
        if state.current_power_option_id is not None:
            state.power_options[state.current_power_option_id] = {
                **info, "choices": {}}

    if line_info_container.line_type == LOG_LINE_POWER_SUBOPTION:
        parent = state.power_options.get(state.current_power_option_id)
        if parent is not None:
            info = line_info_container.info_dict
            parent["choices"][info["index"]] = dict(info)
            _record_titan_ability(state, parent, info)
            _record_power_option_layout(state, parent, info)

    if line_info_container.line_type == LOG_LINE_GENERAL_CHOICE_START:
        state.clear_general_choice()
        state.general_choice_id = line_info_container.info_dict["choice_id"]
        state.general_choice_type = line_info_container.info_dict.get(
            "choice_type")

    if line_info_container.line_type == LOG_LINE_GENERAL_CHOICE_SOURCE:
        if state.general_choice_id is not None:
            info = line_info_container.info_dict
            card_id = info.get("card_id") or ""
            entity = state.entity_dict.get(info.get("entity_id") or "")
            state.general_choice_source_card = card_id
            state.general_choice_source_player = info.get("player")

    if line_info_container.line_type == LOG_LINE_GENERAL_CHOICE_ENTITY:
        if state.general_choice_id is not None:
            player = line_info_container.info_dict["player"]
            if state.general_choice_type == "TARGET":
                # 指向选择的实体是「可以点的目标」（对方随从/英雄），它们的
                # player 是原主，不能拿来判断「这是不是我方的选择框」——那一条
                # 由 Source 行的 player 判定（见 target_choice_open）。
                state.general_choice_indexes.add(
                    line_info_container.info_dict["index"])
            elif state.general_choice_player in (None, player):
                state.general_choice_player = player
                state.general_choice_indexes.add(
                    line_info_container.info_dict["index"])
            else:
                state.clear_general_choice()

    if line_info_container.line_type == LOG_LINE_GENERAL_CHOICE_READY:
        if (state.general_choice_id
                == line_info_container.info_dict["choice_id"]):
            state.general_choice_ready = True

    if line_info_container.line_type == LOG_LINE_GENERAL_CHOICE_RESOLVED:
        if (state.general_choice_id
                == line_info_container.info_dict["choice_id"]):
            state.clear_general_choice()

    if line_info_container.line_type == LOG_LINE_GAME_ENTITY:
        game_entity = GameEntity()
        game_entity_id = line_info_container.info_dict["entity"]

        state.current_update_id = game_entity_id
        state.add_entity(game_entity_id, game_entity)
        state.game_entity_id = game_entity_id

    if line_info_container.line_type == LOG_LINE_PLAYER_ENTITY:
        player_entity = PlayerEntity()
        player_entity_id = line_info_container.info_dict["entity"]
        player_id = line_info_container.info_dict["player"]

        state.current_update_id = player_entity_id
        state.add_player_entity(player_entity_id, player_id, player_entity)

    if line_info_container.line_type == LOG_LINE_FULL_ENTITY:
        card_id = line_info_container.info_dict["card"]
        card_entity_id = line_info_container.info_dict["entity"]
        card_entity = CardEntity(card_id)

        state.current_update_id = card_entity_id
        state.add_entity(card_entity_id, card_entity)

    if line_info_container.line_type == LOG_LINE_FULL_ENTITY_PLAYER:
        # GAME_RESET(回溯)里玩家实体也是 FULL_ENTITY - Updating <裸名> CardID=,
        # 且不带括号 -> parse_line 解析不到实体 id。玩家实体本身不用重建(开局已由
        # PLAYER 行注册), 只需把 current_update_id 指向该玩家, 让它紧随的 tag= 块
        # (RESOURCES/RESOURCES_USED/CURRENT_PLAYER 等)能写到正确的玩家实体上。
        player_name = line_info_container.info_dict["name"]
        bare = player_name.split('#')[0].strip()
        entity_id = None
        if bare:
            for cand in (MY_NAME, state.my_name):
                if cand and cand.split('#')[0].strip() == bare:
                    entity_id = state.my_entity_id
                    break
            if entity_id is None:
                entity_id = state.oppo_entity_id
        if entity_id in state.entity_dict:
            state.current_update_id = entity_id

    if line_info_container.line_type == LOG_LINE_SHOW_ENTITY:
        card_id = line_info_container.info_dict["card"]
        card_entity_id = line_info_container.info_dict["entity"]

        card_entity = state.entity_dict[card_entity_id]
        card_entity.update_card_id(card_id)
        state.current_update_id = card_entity_id

    if line_info_container.line_type == LOG_LINE_CHANGE_ENTITY:
        card_id = line_info_container.info_dict["card"]
        card_entity_id = line_info_container.info_dict["entity"]

        card_entity = state.entity_dict[card_entity_id]
        card_entity.update_card_id(card_id)
        state.current_update_id = card_entity_id

    if line_info_container.line_type == LOG_LINE_TAG_CHANGE:
        entity_string = line_info_container.info_dict["entity"]

        # 情形一 "TAG_CHANGE Entity=GameEntity"
        if entity_string == "GameEntity":
            entity_id = state.game_entity_id

        # 情形二 "TAG_CHANGE Entity=Example#51234"
        elif not entity_string.isdigit():
            # 关于为什么用 "in" 而非 "==", 因为我总是懒得输入后面的数字
            if MY_NAME in entity_string:
                entity_id = state.my_entity_id
                if entity_string != state.my_name:
                    state.my_name = entity_string
            else:
                entity_id = state.oppo_entity_id
                if entity_string != state.oppo_name:
                    state.oppo_name = entity_string

            assert int(entity_id) <= 3

        # 情形三 "TAG_CHANGE Entity=[entityName=UNKNOWN ENTITY [cardType=INVALID] id=14 ...]"
        # 此时的EntityId已经被提取出来了
        else:
            entity_id = entity_string

        if entity_id not in state.entity_dict:
            warn_print(f"Invalid entity_id: {entity_id}")
            warn_print(f"Current line container: {line_info_container}")
            return False

        tag = line_info_container.info_dict["tag"]
        value = line_info_container.info_dict["value"]

        entity = state.entity_dict[entity_id]
        _record_friendly_hand_entry(state, entity, tag, value)
        entity.set_tag(tag, value)

    if line_info_container.line_type == LOG_LINE_TAG:
        tag = line_info_container.info_dict["tag"]
        value = line_info_container.info_dict["value"]

        # 在对战的一开始的时候, 对手的任何牌对你都是不可见的.
        # 故而在日志中发现的第一个不是英雄, 不是英雄技能, 而且
        # 你知道它的 CardID 的 Entity 一定是你的牌. 利用这
        # 张牌确定双方的 PlayerID
        if state.my_player_id == "0":
            if tag == "CARDTYPE" \
               and value not in ["HERO", "HERO_POWER", "PLAYER", "GAME"] \
               and "CONTROLLER" in state.current_update_entity.tag_dict:
                state.my_player_id = state.current_update_entity.query_tag("CONTROLLER")
                # 双方PlayerID, 一个是1, 一个是2
                state.oppo_player_id = str(3 - int(state.my_player_id))
                # debug_print(f"my_player_id: {state.my_player_id}")

        entity = state.current_update_entity
        _record_friendly_hand_entry(state, entity, tag, value)
        entity.set_tag(tag, value)

    if line_info_container.line_type == LOG_LINE_PLAYER_ID:
        player_id = line_info_container.info_dict["player"]
        player_name = line_info_container.info_dict["name"]
        if player_name:
            state.player_names[str(player_id)] = player_name.strip()

        # 我发现用这里的信息很不靠谱, 正常情况下的两个player_name
        # 应该对手的是"UNKNOWN HUMAN PLAYER", 你的是自己的用户名,
        # 但有时两个都是"UNKNOWN HUMAN PLAYER", 有时又都是已知.
        # 所以只拿来做校验

        # 下面这种情况明显是发生了错误. 一般会出现在在对战过程中关闭炉石
        # 再重新启动炉石. 此时在构建过程中看到的第一个确切的卡可能是对手
        # 场上的怪而非我自己的手牌, 进而误判 my_player_id
        if MY_NAME and MY_NAME in player_name:
            # 日志明确给出“我”的名字：直接确立己方 PlayerID，
            # 不再依赖“第一张卡”的 CONTROLLER 推断（那会因先读到对手怪而反）。
            try:
                my_pid = str(player_id)
                oppo_pid = str(3 - int(my_pid))
            except (TypeError, ValueError):
                my_pid = oppo_pid = None
            if my_pid and (state.my_player_id != my_pid
                           or state.oppo_player_id != oppo_pid):
                state.my_player_id = my_pid
                state.oppo_player_id = oppo_pid
        elif state.my_player_id != "0" and \
                player_id == state.oppo_player_id and MY_NAME in player_name:
            global _MY_PLAYER_SWAP_WARNED
            # 兜底：名字没能用于确立（如名字不含我），但发现“对手位是我名字”时交换。
            if not _MY_PLAYER_SWAP_WARNED:
                warn_print("my_player_id may be wrong")
                _MY_PLAYER_SWAP_WARNED = True
            state.my_player_id, state.oppo_player_id = \
                state.oppo_player_id, state.my_player_id

    state.revision += 1
    _emit_hand_diag_if_changed(state)
    return True


def _hand_snapshot(state):
    """我方手牌/场面的单行速写：``槽位:卡号(eid)``，按 zone_pos 排序。

    ``LogState`` 上并没有 ``my_hand_cards``（那是 ``StrategyState`` 的字段），
    所以这里直接扫 ``entity_dict``，按 CONTROLLER/ZONE 自己算——否则诊断会空转。
    """
    hand = []
    board = []
    for entity_id, entity in (getattr(state, "entity_dict", None) or {}).items():
        if not hasattr(entity, "query_tag"):
            continue
        if not state.is_my_entity(entity):
            continue
        zone = entity.query_tag("ZONE")
        try:
            pos = int(entity.query_tag("ZONE_POSITION"))
        except (TypeError, ValueError):
            pos = 0
        card = entity.card_id or "?"
        row = f"{pos}:{card}(eid={entity_id})"
        if zone == "HAND":
            hand.append((pos, row))
        elif zone == "PLAY" and entity.cardtype in ("MINION", "LOCATION"):
            board.append((pos, row))
    hand.sort()
    board.sort()
    return (f"hand[{len(hand)}]=" + ", ".join(r[1] for r in hand)
            + " | board[" + str(len(board)) + "]="
            + ", ".join(r[1] for r in board))


def _emit_hand_diag_if_changed(state):
    """手牌/场面构成变化时记一行，便于定位「多出来/少了的那张牌」。

    卡牌进 GRAVEYARD 却被算成还在手牌时，手牌张数会整体错位，盒子说的号位就
    对不上（实测 2026-09-23 13:40：盒子说 3 号位法术，我们取到 2 号位的随从）。
    逐次记录构成变化，就能看到那张牌是在哪一步被错误地留在手牌里的。
    """
    try:
        snapshot = _hand_snapshot(state)
    except Exception:
        return
    if snapshot == getattr(state, "_hand_diag_signature", None):
        return
    state._hand_diag_signature = snapshot
    try:
        info_print(f"[手牌] {snapshot}")
    except Exception:
        pass


def _record_friendly_hand_entry(state, entity, tag, value):
    """Count transitions into the known-friendly HAND state."""
    if state.my_player_id == "0":
        return
    old_zone = entity.query_tag("ZONE")
    old_controller = entity.query_tag("CONTROLLER")
    new_zone = value if tag == "ZONE" else old_zone
    new_controller = value if tag == "CONTROLLER" else old_controller
    was_friendly_hand = (
        old_zone == "HAND" and old_controller == state.my_player_id)
    is_friendly_hand = (
        new_zone == "HAND" and new_controller == state.my_player_id)
    if is_friendly_hand and not was_friendly_hand:
        state.hand_entry_count += 1


def _record_titan_ability(state, parent, info):
    """记住泰坦技能子牌的名字与面板位置（subOption 下标即点击位置）。

    只记录「自己场上/手牌」的泰坦（player 必须与 my_player_id 一致），避免把
    对手或其它玩家的技能顺序学进自己的兜底表；也只有父选项确实是泰坦牌（或子牌
    card_id 以父牌 card_id 开头）时才记录。这份记录只在 Power.log 之后不再重发
    子选项时给泰坦识别兜底，不参与正常解析。
    """
    parent_card = (parent or {}).get("card_id") or ""
    name = info.get("name")
    sub_card = info.get("card_id") or ""
    if not parent_card or not name:
        return
    if parent.get("player") != state.my_player_id:
        return
    if not (sub_card.startswith(parent_card) or _is_titan_card(parent_card)):
        return
    state.titan_ability_choices.setdefault(parent_card, {})[
        info["index"]] = name


def _record_power_option_layout(state, parent, info):
    """记住「卡牌 → {子项名: 面板下标}」，给抉择牌用。

    subOption 的下标就是面板位置，而且对同一张牌是固定的（实测 星辰坠落 每次
    都是 0=星辰领主、1=星辰漂流）。但子项只在「这张牌当前可打出」的那段窗口里
    下发；盒子给推荐时往往已经不再下发（2026-09-22 13:46 就是这样卡住的）。
    记下这一份之后，适配器就不必依赖「推荐来时子项还在」。

    同一份顺序会**同时记在主牌和每个子牌的卡号下**：主牌可能有多个版本号
    （如 CATA_153 与 NEW1_007 之争），盒子/状态解析出的卡号未必是我们学到时
    用的那一个，只按主牌记会查不到（2026-09-23 13:40 就是这样卡了 19 次）。

    只记录自己的牌：日志描述符里的 ``player=`` 是原主，抢来的随从会不一致，
    所以再允许「实体在己方手牌/场上」这一条。
    """
    parent_card = (parent or {}).get("card_id") or ""
    name = info.get("name")
    index = info.get("index")
    if not parent_card or not name or index is None:
        return
    if (parent.get("player") != state.my_player_id
            and not _is_my_entity(state, parent.get("entity_id"))):
        return
    # 先在父牌下累积**完整**的顺序表（子项是逐条到达的），再整体同步到子牌键。
    # 若直接把单条记到子牌键下，子牌键就只有一个条目，len() 当选项总数会算错。
    table = state.power_option_layouts.setdefault(parent_card, {})
    table[name] = index
    sub_card = info.get("card_id") or ""
    if sub_card and sub_card != parent_card:
        state.power_option_layouts[sub_card] = dict(table)
    for key in list(state.power_option_layouts):
        if key == parent_card:
            continue
        sub_table = state.power_option_layouts[key]
        if sub_table and set(sub_table) <= set(table):
            state.power_option_layouts[key] = dict(table)


def _is_my_entity(state, entity_id):
    """实体是否在己方手牌/场上（用于抢来的随从：player= 是原主）。"""
    if entity_id is None:
        return False
    for collection in ("my_hand_cards", "my_minions", "my_locations"):
        for entity in (getattr(state, collection, None) or ()):
            if str(getattr(entity, "entity_id", None)) == str(entity_id):
                return True
    return False


def _is_titan_card(card_id):
    try:
        return card_id in titan_card_ids()
    except Exception:
        # 本地卡牌数据缺失/损坏时只影响这份兜底记录，绝不打断日志解析。
        return False


if __name__ == "__main__":
    log_iter = log_iter_func(HEARTHSTONE_LOG_ROOT)
    log_container = next(log_iter)
    temp_state = LogState()

    for x in log_container.message_list:
        # print(x)
        update_state(temp_state, x)
    print(temp_state)
