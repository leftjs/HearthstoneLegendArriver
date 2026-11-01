"""古神（Old God）随从的技能识别。

古神随从和泰坦一样不能攻击，改成「点自己场上的古神 → 弹出四张技能牌（和抉择/
发现同一套居中卡牌布局）→ 选技能 →（技能需要目标时）再点目标」。区别只有一条：
泰坦一回合只能用一张技能牌，古神只要技能还没用过、法力够，**同一回合里可以连着
选多次**——所以执行完一次不能把整条链路当成「已消费」，而是按技能名逐条判重。

盒子对这类牌给的指令长这样（实测 2026-10-24 00:37，克苏恩）：

    操作1号位随从攻击
    克苏恩，帝国之眼
    目标是对方2号位
    小鬼奶奶
    选择卡牌
    克苏恩的凝视

它没有「操作N号位古神」这种句式，用的是「操作N号位随从攻击」+「选择卡牌 技能名」，
所以**只要攻击指令后面跟着「选择卡牌」就一定是古神技能**（真攻击永远不会带选择）。

技能名 → 面板下标的映射不靠猜：古神的四张技能子牌 id 是「主牌 id + t + 数字」，
数字升序就是面板从左到右的槽位（BE_013t1..t4 / BE_009t1..t4 / BE_784t1..t4 /
恩佐斯是 t3..t6）。实测印证：

* Power.log 里完整四选一时 ``subOption 0..3`` 的顺序就是 愤怒/领域/凝视/仆从；
* 技能结算的 ``BLOCK_START ... SubOption=N`` 用的正是槽位号（0 基），
  与 ``ACTIVATABLE_MINION_ABILITY_USED_{N+1}`` 一一对应。

注意 ``subOption`` 的序号在**有技能已用过**时会变（不可用的被挪到末尾），所以它
不能直接当点击下标；槽位只能按这里的静态表来。
"""

import json
import re
from functools import lru_cache
from pathlib import Path


# 古神固定四张技能牌（12 张实测：每张古神都是 4 个技能子牌）。
OLD_GOD_ABILITY_COUNT = 4

# 古神牌的关键字行就是文本开头这个加粗标记。
_OLD_GOD_MARK = "<b>古神</b>"

# 技能子牌：「主牌 id + t + 槽位数字」，如 BE_013t3。
_ABILITY_CARD_ID = re.compile(r"^(?P<parent>.+?)t(?P<slot>\d+)$")


@lru_cache(maxsize=1)
def _cards():
    # 只读项目本地的卡牌数据，绝不在点击路径里联网。
    with (Path(__file__).resolve().parents[2] / 'cards.json').open(
            encoding='utf8') as source:
        return json.load(source)


@lru_cache(maxsize=1)
def old_god_card_ids():
    """带「古神」关键字的随从卡号（BE_009/013/017/784 等）。

    只认文本**开头**的 ``<b>古神</b>``：「抽一张古神牌」这类只是提到古神的牌
    （如 BE_703 德雷阿佳丝）不算，它们没有技能面板。
    """
    return frozenset(
        card['id'] for card in _cards()
        if card.get('type') == 'MINION'
        and (card.get('text') or '').lstrip().startswith(_OLD_GOD_MARK))


@lru_cache(maxsize=None)
def old_god_ability_cards(card_id):
    """某张古神的技能子牌，按面板槽位顺序返回 ``((技能名, 子牌卡号), ...)``。

    同名的孪生子牌（如克苏恩 BE_013t6 是 t3「凝视」的复制）只保留槽位小的那张；
    去重后仍按 id 数字升序，就是面板从左到右的顺序。
    """
    abilities = []
    for card in _cards():
        match = _ABILITY_CARD_ID.match(card.get('id') or '')
        if (match is None or match.group('parent') != card_id
                or card.get('type') != 'SPELL'):
            continue
        abilities.append((int(match.group('slot')), card.get('name') or '',
                          card['id']))
    abilities.sort()
    seen = set()
    unique = []
    for _slot, name, ability_card_id in abilities:
        if not name or name in seen:
            continue
        seen.add(name)
        unique.append((name, ability_card_id))
    return tuple(unique[:OLD_GOD_ABILITY_COUNT])


@lru_cache(maxsize=None)
def old_god_ability_layout(card_id):
    """``{技能名: 面板下标}``；不是古神/没有技能子牌的卡返回空表。"""
    return {name: index
            for index, (name, _card_id) in enumerate(
                old_god_ability_cards(card_id))}


def ability_used_tag(index):
    """「第 index 个技能已用过」的实体标签名（Power.log 里就是这个写法）。"""
    return f"ACTIVATABLE_MINION_ABILITY_USED_{index + 1}"


def ability_used(entity, index):
    """这个技能是否已经用过（古神技能每局每个只能用一次）。

    两种实体形状都认：
      * 日志实体（``CardEntity``）：直接查 ``ACTIVATABLE_MINION_ABILITY_USED_n``；
      * 界面实体（``StrategyMinion``）：没有 tag 表，读 ``used_ability_slots``
        （由 ``log_state`` 把同一批标签翻译过来）。
    完全没有这些信息时（测试桩）视为没用过，交给点击与结果校验兜底。
    """
    if index is None:
        return False
    used = getattr(entity, 'used_ability_slots', None)
    if used is not None:
        try:
            return index in used
        except TypeError:
            return False
    query = getattr(entity, 'query_tag', None)
    if query is None:
        return False
    try:
        return str(query(ability_used_tag(index))) == "1"
    except Exception:                            # noqa: BLE001
        return False
