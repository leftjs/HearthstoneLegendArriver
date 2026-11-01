"""Deterministic recommendation grammar; never guesses key digits."""

import re
import time
import uuid

from src.recommendation_models import ActionKind, ProposedAction, SlotRef


class RecommendationParseError(ValueError):
    pass


# 诊断串里保留的面板文本长度上限（含换行）。解析失败时日志里只有错误码，没有
# 面板原文，事后无法判断是哪条推荐触发——这里把归一化文本一并带上，供排查。
_ACTION_TEXT_DIAG_LIMIT = 200
# 诊断用：把数字统一换成 '0' 再比对，号位差异就不参与判断。
_DIGIT_DIAG = str.maketrans("123456789", "000000000")
# 合法句式模板（数字已用 '0' 占位）。只服务「单字近似」诊断提示。
_TEMPLATE_PATTERNS = (
    "打出0号位随从", "打出0号位法术", "打出0号位武器", "打出0号位地标",
    "打出0号位英雄", "替换0号位卡牌", "交易0号位卡牌", "锻造0号位卡牌",
    "预备0号位卡牌", "放置于我方0号位", "放置于己方0号位", "操作0号位随从攻击",
    "操作0号位泰坦", "操作0号位地标", "操作我方英雄攻击", "操作己方英雄攻击",
    "目标是对方0号位", "目标是敌方0号位", "目标是己方0号位", "目标是我方0号位",
    "目标是对方英雄", "目标是敌方英雄", "目标是己方英雄", "目标是我方英雄",
    "选择我方0号位卡牌", "选择己方0号位卡牌", "选择对方0号位卡牌", "选择对方英雄",
)
def _digits_as_marker(line):
    """把数字统一替换成 '0'，用于忽略号位差异的比对。"""
    return re.sub(r"\d", "0", (line or "").translate(_DIGIT_DIAG))


_TEMPLATE_MARKERS = tuple(
    _digits_as_marker(template) for template in _TEMPLATE_PATTERNS)
_TEMPLATE_BY_MARKER = dict(zip(_TEMPLATE_MARKERS, _TEMPLATE_PATTERNS))


def _near_template(line):
    """行与某个合法句式**只差一个字符**时返回 ``疑似模板(错字→正字)``。

    这就是「OCR 认错一个字」的判据：用它把实际的错字摆到日志里，而不是靠我们
    去猜「我/己/已/万」这类没有边界的混淆。
    """
    marker = _digits_as_marker(line)
    if "0" not in marker:
        return ""
    for template in _TEMPLATE_MARKERS:
        if len(template) != len(marker):
            continue
        diff = [(a, b) for a, b in zip(marker, template) if a != b]
        if len(diff) != 1:
            continue
        wrong, right = diff[0]
        return f"{_TEMPLATE_BY_MARKER[template]}({wrong}→{right})"
    return ""


def _action_text_diag(text):
    """把归一化后的面板文本压成单行诊断串（保留全部行，仅替换换行）。"""
    normalized = " / ".join(
        line for line in (text or "").splitlines() if line.strip())
    if not normalized:
        return "(empty)"
    if len(normalized) > _ACTION_TEXT_DIAG_LIMIT:
        return normalized[:_ACTION_TEXT_DIAG_LIMIT] + "..."
    return normalized


class RecommendationParser:
    _mulligan = re.compile(r"^替换([1-9]\d*)号位卡牌$")
    _keep_all = "保留全部卡牌"
    _play = re.compile(r"^打出([1-9]\d*)号位(随从|法术|武器|地标|英雄)$")
    _trade = re.compile(r"^(?:交易|锻造|预备)([1-9]\d*)号位卡牌$")
    # 「己方」等写法统一由 _OCR_VARIANTS 归一化成「我方」，所以句式里只认
    # 「我方」即可：保持**单一来源**，避免两处各列一遍写法而对不上。
    _own = "我方"
    _destination = re.compile(rf"^放置于{_own}([1-9]\d*)号位$")
    _minion_attack = re.compile(r"^操作([1-9]\d*)号位随从攻击$")
    _titan_ability = re.compile(r"^操作([1-9]\d*)号位泰坦$")
    _hero_attack = re.compile(rf"^操作{_own}英雄攻击$")
    _target = re.compile(
        r"^目标是(?:对方|敌方)([1-9]\d*)号位(?:随从)?$")
    _friendly_hand_target = re.compile(
        rf"^目标是{_own}([1-9]\d*)号位(?:随从)?$")
    _enemy_hero_targets = {"目标是对方英雄", "目标是敌方英雄"}
    _friendly_hero_targets = {"目标是己方英雄", "目标是我方英雄"}
    _location = re.compile(r"^操作([1-9]\d*)号位地标$")
    _discover = re.compile(
        rf"^(?:选择{_own}([1-4])号位卡牌|选(?:择)?第([1-4])个选项)$")
    # 指向选择：效果选完后还要再点一个目标（如「克苏恩的凝视」「克苏恩的子嗣」
    # 这类古神衍生效果）。盒子这时给的是「选择对方2号位卡牌」——注意它是
    # **动作行**，不是「目标是…」修饰行；号位是对方场面的位置（不是选择框下标）。
    _choose_enemy_target = re.compile(
        r"^选择(?:对方|敌方)([1-9]\d*)号位(?:卡牌|随从)$")
    _choose_enemy_hero_targets = {"选择对方英雄", "选择敌方英雄"}
    _choose_own_hero_targets = {"选择我方英雄", "选择己方英雄"}
    # HSAng 时间线提示字：按钮文案可能是纯「回溯/维持」，也可能是带标题的
    # 「回溯时间线/维持时间线」（OCR 也可能把两者读成「回溯」+「时间线」两行，
    # 其中「时间线」非动作行会被过滤，只剩「回溯」）。
    _timeline = re.compile(r"^(回溯|维持)\s*(?:时间线)?$")
    _reference_a_headers = {"打法参考A", "打法参考Ａ"}
    _reference_b_headers = {"打法参考B", "打法参考Ｂ"}
    # OCR 错字表：只收录**实测真的害过我们**、或字形确实近似到会认错的组合。
    # 必须是**词组**（≥2 字）：单字全局替换会误伤卡名——实测卡名里含「术」738
    # 个、「木」98 个、「我」84 个、「矛」93 个、「已」32 个，字符级替换必错。
    #
    #  已方→我方    我/已 字形接近。实测 2026-09-21 04:59、2026-09-22 16:35/19:16
    #               三次卡手都是它：目标行匹配不上 → unsupported_spell_target
    #  対→对        対/对（日文汉字）。实测同批日志出现；独立成字，卡名里没有
    #  己方→我方    「己方」本身是合法写法，但面板两种写法混用；统一成「我方」
    #               让下游（消费键、文本比对）看到一致的串
    #  我万→我方    方/万 字形接近
    #  両→两        日文汉字「両」← 两
    #
    # 新增条目请附「实测时间 + 触发的错误码」。无法确定时**不要加**：解析失败的
    # 诊断里带 ``near=``，会自动指出「只差一个字」的候选与错字，照着加即可。
    _OCR_VARIANTS = (
        ("已方", "我方"),
        ("己方", "我方"),
        ("我万", "我方"),
        ("対", "对"),
        ("両", "两"),
    )

    @classmethod
    def _fix_ocr_variants(cls, line):
        """纠正 OCR 常见错字（字形相近），避免整条推荐无法解析。"""
        fixed = line
        for wrong, right in cls._OCR_VARIANTS:
            fixed = fixed.replace(wrong, right)
        return fixed

    @classmethod
    def normalize_action_text(cls, text):
        parser = cls()
        lines = parser._reference_a_lines(text or "")
        lines = [cls._fix_ocr_variants(line) for line in lines]
        retained = []
        for index, line in enumerate(lines):
            if (parser._is_action_line(line)
                    or parser._destination.fullmatch(line)
                    or parser._target.fullmatch(line)
                    or parser._friendly_hand_target.fullmatch(line)
                    or line in parser._enemy_hero_targets
                    or "目标" in line
                    or line == "选择卡牌"
                    or (index > 0 and lines[index - 1] == "选择卡牌")):
                retained.append(line)
        return "\n".join(retained)

    def parse(self, ocr, turn_number, log_revision):
        """解析面板文本；任何失败都附带面板原文 + 单字近似候选。

        失败消息形如 ``unsupported_spell_target:panel=打出2号位法术 / …``：
        错误码保持在最前面（现有断言与日志检索都按码匹配），面板文本只作后缀。

        额外带 ``near=`` —— 若有某一行「只差一个字符」就是合法句式，把它连同
        修正后的样子一起打出来。这样新增 OCR 错字表时**只需收录真正害过我们的
        字**，不必预先猜「我/己/已」这类没有边界的混淆。
        """
        action_text = self.normalize_action_text(ocr.normalized_text)
        try:
            return self._parse_action(action_text, ocr, turn_number,
                                      log_revision)
        except RecommendationParseError as exc:
            near = self._near_miss_hint(action_text)
            suffix = f":near={near}" if near else ""
            raise RecommendationParseError(
                f"{exc}:panel={_action_text_diag(action_text)}{suffix}"
            ) from None

    def _near_miss_hint(self, action_text):
        """找出「只差一个字符」就能匹配的合法句式；没有则返回空串。

        只用于诊断，不改变解析结果：把 OCR 实际认错的字摆到眼前，避免我们
        靠猜去扩错字表。合法句式由正则模板生成（数字位置用 ``0`` 占位、比较时
        再把输入里的数字替换掉），所以不用手写模板列表。
        """
        lines = self._lines(action_text)
        hints = []
        for line in lines:
            if self._is_known_line(line):
                continue
            template = _near_template(line)
            if template:
                hints.append(f"{line}→{template}")
        return " / ".join(hints[:3])

    def _is_known_line(self, line):
        """这一行本身已经能被现有句式识别：近似提示只报认不出的行。"""
        return bool(
            self._is_action_line(line)
            or self._destination.fullmatch(line)
            or self._target.fullmatch(line)
            or self._friendly_hand_target.fullmatch(line)
            or line in self._enemy_hero_targets
            or line in self._friendly_hero_targets
            or line == "选择卡牌")
    def _parse_action(self, action_text, ocr, turn_number, log_revision):
        lines = self._lines(action_text)
        if not lines:
            raise RecommendationParseError("empty_recommendation")
        action_lines = [line for line in lines if self._is_action_line(line)]
        if self._keep_all in action_lines:
            if len(action_lines) != 1:
                raise RecommendationParseError("ambiguous_actions")
            return self._build(
                ocr, turn_number, log_revision,
                ActionKind.MULLIGAN, mulligan_slots=())
        timeline = [line for line in action_lines
                    if self._timeline_of(line) is not None]
        if timeline:
            # HSAng 时间线提示出现时，面板上其它文字都是上一步已完成动作的残留
            # （盒子先给上一步的推荐、特效完成后弹时间线框、再追加一行时间线字
            # 告诉点哪个按钮）。弹框是模态的——不点掉它什么都做不了，所以共存
            # 内容一律忽略：出现「回溯」就点回溯、出现「维持」就点维持，不再要求
            # 共存的是不是打出/选择。唯一无法判定的是回溯/维持同屏时该点哪个。
            choices = {self._timeline_of(line) for line in timeline}
            if choices == {"undo", "keep"}:
                raise RecommendationParseError("ambiguous_actions")
            action = (ActionKind.TIMELINE_UNDO
                      if "undo" in choices else ActionKind.TIMELINE_KEEP)
            return self._build(ocr, turn_number, log_revision, action)
        mulligans = [self._mulligan.fullmatch(line) for line in action_lines]
        if action_lines and all(match is not None for match in mulligans):
            slots = tuple(sorted({int(match.group(1)) for match in mulligans}))
            return self._build(ocr, turn_number, log_revision,
                               ActionKind.MULLIGAN, mulligan_slots=slots)
        if len(action_lines) != 1:
            raise RecommendationParseError("ambiguous_actions")
        primary = action_lines[0]

        play = self._play.fullmatch(primary)
        if play:
            slot = int(play.group(1))
            card_type = {"随从": "MINION", "法术": "SPELL",
                         "武器": "WEAPON", "地标": "LOCATION",
                         "英雄": "HERO"}[play.group(2)]
            target = None
            if card_type == "SPELL":
                enemy_board_targets = [
                    match for line in lines
                    if (match := self._target.fullmatch(line))
                ]
                friendly_board_targets = [
                    match for line in lines
                    if (match := self._friendly_hand_target.fullmatch(line))
                ]
                enemy_hero_targets = sum(
                    line in self._enemy_hero_targets for line in lines)
                friendly_hero_targets = sum(
                    line in self._friendly_hero_targets for line in lines)
                target_lines = [line for line in lines if "目标" in line]
                if len(target_lines) > 1:
                    raise RecommendationParseError("ambiguous_target")
                target_count = (
                    len(enemy_board_targets) + len(friendly_board_targets)
                    + enemy_hero_targets + friendly_hero_targets)
                if target_lines and target_count != 1:
                    raise RecommendationParseError("unsupported_spell_target")
                if enemy_board_targets:
                    target = SlotRef(
                        "board_slot", "enemy",
                        int(enemy_board_targets[0].group(1)))
                elif friendly_board_targets:
                    target = SlotRef(
                        "board_slot", "friendly",
                        int(friendly_board_targets[0].group(1)))
                elif enemy_hero_targets:
                    target = SlotRef("hero", "enemy")
                elif friendly_hero_targets:
                    target = SlotRef("hero", "friendly")
            elif card_type == "MINION":
                # Card identity in the adapter disambiguates the special
                # battlecries whose friendly slot actually means a hand card.
                target = self._optional_board_or_hero_target(
                    lines, "unsupported_minion_target")
            else:
                self._reject_target_lines(lines)
            destinations = [self._destination.fullmatch(line) for line in lines]
            destinations = [match for match in destinations if match]
            if len(destinations) > 1:
                raise RecommendationParseError("ambiguous_destination")
            destination = (SlotRef("board_slot", "friendly",
                                   int(destinations[0].group(1)))
                           if destinations else None)
            return self._build(
                ocr, turn_number, log_revision, ActionKind.PLAY_CARD,
                source=SlotRef("hand_slot", "friendly", slot),
                destination=destination,
                target=target,
                card_type=card_type)

        trade = self._trade.fullmatch(primary)
        if trade:
            self._reject_target_lines(lines)
            return self._build(
                ocr, turn_number, log_revision, ActionKind.TRADE_CARD,
                source=SlotRef(
                    "hand_slot", "friendly", int(trade.group(1))))

        if primary == "使用英雄技能":
            target = self._optional_board_or_hero_target(
                lines, "unsupported_hero_power_target")
            return self._build(
                ocr, turn_number, log_revision, ActionKind.USE_HERO_POWER,
                source=SlotRef("hero_power", "friendly"), target=target)

        discover = self._discover.fullmatch(primary)
        if discover:
            self._reject_target_lines(lines)
            return self._build(
                ocr, turn_number, log_revision, ActionKind.CHOOSE_DISCOVER,
                source=SlotRef("discover_slot", "friendly",
                               int(discover.group(1) or discover.group(2))))

        enemy_choice = self._choose_enemy_target.fullmatch(primary)
        if (enemy_choice or primary in self._choose_enemy_hero_targets
                or primary in self._choose_own_hero_targets):
            # 「选择对方2号位卡牌」= 正在等目标：点对方场面第 N 个（N 是场面位置，
            # 不是选择框下标——实测 08:57:50 的选择框里实体顺序是 [2号位, 1号位]）。
            if enemy_choice is not None:
                target = SlotRef("board_slot", "enemy",
                                 int(enemy_choice.group(1)))
            elif primary in self._choose_enemy_hero_targets:
                target = SlotRef("hero", "enemy")
            else:
                target = SlotRef("hero", "friendly")
            return self._build(
                ocr, turn_number, log_revision, ActionKind.CHOOSE_TARGET,
                target=target)

        attack = self._minion_attack.fullmatch(primary)
        hero_attack = self._hero_attack.fullmatch(primary)
        if attack or hero_attack:
            targets = [self._target.fullmatch(line) for line in lines]
            targets = [match for match in targets if match]
            targets.extend(
                line for line in lines if line in self._enemy_hero_targets)
            if len(targets) > 1 or (not targets and hero_attack):
                raise RecommendationParseError("attack_target_required")
            source = (SlotRef("board_slot", "friendly", int(attack.group(1)))
                      if attack else SlotRef("hero", "friendly"))
            target = None
            if targets:
                target = (SlotRef("hero", "enemy")
                          if targets[0] in self._enemy_hero_targets
                          else SlotRef("board_slot", "enemy",
                                       int(targets[0].group(1))))
            return self._build(
                ocr, turn_number, log_revision, ActionKind.ATTACK,
                source=source, target=target)

        location = self._location.fullmatch(primary)
        if location:
            target = self._optional_board_or_hero_target(
                lines, "unsupported_location_target")
            return self._build(
                ocr, turn_number, log_revision, ActionKind.USE_LOCATION,
                source=SlotRef("board_slot", "friendly",
                               int(location.group(1))),
                target=target)

        titan = self._titan_ability.fullmatch(primary)
        if titan:
            # 泰坦：点自己场上的泰坦 → 技能面板里选「选择卡牌」后面那个技能名
            # →（技能需要目标时）点目标。技能名由 _build 统一取出。
            target = self._optional_board_or_hero_target(
                lines, "unsupported_titan_target")
            return self._build(
                ocr, turn_number, log_revision, ActionKind.TITAN_ABILITY,
                source=SlotRef("board_slot", "friendly",
                               int(titan.group(1))),
                target=target)

        if primary == "结束回合":
            self._reject_target_lines(lines)
            return self._build(
                ocr, turn_number, log_revision, ActionKind.END_TURN)
        raise RecommendationParseError("unsupported_recommendation")

    def _lines(self, text):
        translation = str.maketrans("０１２３４５６７８９", "0123456789")
        return [line.strip().translate(translation) for line in text.splitlines()
                if line.strip()]

    def _reference_a_lines(self, text):
        lines = self._lines(text)
        has_reference_a = any(
            line in self._reference_a_headers for line in lines)
        retained = []
        reading_a = not has_reference_a
        for line in lines:
            if line in self._reference_a_headers:
                reading_a = True
                continue
            if line in self._reference_b_headers:
                if reading_a:
                    break
                continue
            if reading_a:
                retained.append(line)
        return retained

    def _is_action_line(self, line):
        if self._timeline_of(line) is not None:
            return True
        return bool(self._mulligan.fullmatch(line) or self._play.fullmatch(line)
                    or self._trade.fullmatch(line)
                    or self._minion_attack.fullmatch(line)
                    or self._titan_ability.fullmatch(line)
                    or self._hero_attack.fullmatch(line)
                    or self._location.fullmatch(line)
                    or self._discover.fullmatch(line)
                    or self._choose_enemy_target.fullmatch(line)
                    or line in self._choose_enemy_hero_targets
                    or line in self._choose_own_hero_targets
                    or line in {self._keep_all, "使用英雄技能", "结束回合"})

    def _timeline_of(self, line):
        match = self._timeline.fullmatch(line)
        if match is None:
            return None
        return "undo" if match.group(1) == "回溯" else "keep"

    def _optional_board_or_hero_target(self, lines, unsupported_code):
        target_lines = [line for line in lines if "目标" in line]
        if len(target_lines) > 1:
            raise RecommendationParseError("ambiguous_target")
        if not target_lines:
            return None
        target_line = target_lines[0]
        enemy_board = self._target.fullmatch(target_line)
        friendly_board = self._friendly_hand_target.fullmatch(target_line)
        if target_line in self._enemy_hero_targets:
            return SlotRef("hero", "enemy")
        if target_line in self._friendly_hero_targets:
            return SlotRef("hero", "friendly")
        if enemy_board:
            return SlotRef(
                "board_slot", "enemy", int(enemy_board.group(1)))
        if friendly_board:
            return SlotRef(
                "board_slot", "friendly", int(friendly_board.group(1)))
        raise RecommendationParseError(unsupported_code)

    @staticmethod
    def _reject_target_lines(lines):
        if any("目标" in line for line in lines):
            raise RecommendationParseError("targeted_action_unsupported")

    @staticmethod
    def _build(ocr, turn_number, log_revision, action, **kwargs):
        action_text = RecommendationParser.normalize_action_text(
            ocr.normalized_text)
        lines = action_text.splitlines()
        markers = [i for i, line in enumerate(lines) if line == "选择卡牌"]
        choice_name = None
        if markers and action in (ActionKind.PLAY_CARD, ActionKind.USE_HERO_POWER,
                                  ActionKind.TITAN_ABILITY):
            if len(markers) != 1 or markers[0] + 1 >= len(lines):
                raise RecommendationParseError("choose_one_name_required")
            choice_name = lines[markers[0] + 1]
            if (RecommendationParser()._is_action_line(choice_name)
                    or "目标" in choice_name or choice_name.startswith("放置于")):
                raise RecommendationParseError("choose_one_name_required")
        if action == ActionKind.TITAN_ABILITY and choice_name is None:
            # 没有技能名就无法确定点哪张技能牌，宁可重试也不猜着点。
            raise RecommendationParseError("titan_ability_name_required")
        return ProposedAction(
            action_id=f"action-{uuid.uuid4()}", frame_id=ocr.frame_id,
            created_at=time.time(), turn_number=turn_number,
            log_revision=log_revision, raw_instruction=action_text,
            normalized_instruction=action_text, action=action,
            ocr_confidence=ocr.confidence, semantic_confidence=1.0,
            choice_name=choice_name,
            **kwargs)
