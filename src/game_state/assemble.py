"""拼装（Assemble）选择面板的识别。

「拼装」是 BE（黑暗帝国的统治）的关键词：卡牌/战吼触发拼装时，服务器会弹一个
``GENERAL`` 选择框，界面标题是「选择一项替换」。截图实测（2026-10-24 06:49）：
中间一张大卡是拼装结果预览（只有一段文字），左右两张候选卡各拆成上下两段
「词条」（都带绿光、可点），下面还有「隐藏」「拼装」两个按钮。

盒子**旧版**文案和「发现」一样：

    选择我方3号位卡牌
    伦萨克大王

这套信息对不上屏幕卡位——实测 2026-10-24 06:33:58 的那次拼装：

* 日志里的选择框有 **4 个实体**，是两个候选各自的「半张/词条」实体
  （``CRAFTABLE_FIRST_HALF_DBID`` / ``CRAFTABLE_SECOND_HALF_DBID``），
  且同一候选的两条在列表里**顺序和屏幕上下相反**，
* 屏幕上只有 **2 张候选卡**（另有一张结果预览），

所以「N 号位」既不是屏幕卡位、也不是词条顺序。机器人按「四选一」布局点
(1150,500) 连点 10 次（06:34:01~06:34:32），游戏毫无反应，最后靠用户手动点掉。

盒子 **2026-10-29 更新后**直接给点击序列（实测 23:01 截图）：

    进行拼装【右2中1】
    鸭妈妈

「右2中1」= 右卡第 2 条词条 → 中间结果卡第 1 条词条，按盒子给的顺序点完再点
「拼装」确认（解析在 ``RecommendationParser._parse_assemble``，坐标在
``click.ASSEMBLE_ENTRY_X``）。也就是说现在**照盒子点**，只有旧文案才退回默认的
「点第二张候选卡的第二条词条」。
"""

import json
from functools import lru_cache
from pathlib import Path


# 拼装牌/拼装来源的文本里一定带这个关键词（"<b>拼装</b>一张随从牌" 等）。
_ASSEMBLE_MARK = "拼装"

# 拼装目标身上的标签（实测 暗影塑形 BE_114 对目标 175 打了 ASSEMBLE_TARGET=175）。
_ASSEMBLE_TARGET_TAG = "ASSEMBLE_TARGET"


@lru_cache(maxsize=1)
def _cards():
    # 只读项目本地的卡牌数据，绝不在点击路径里联网。
    with (Path(__file__).resolve().parents[2] / 'cards.json').open(
            encoding='utf8') as source:
        return json.load(source)


@lru_cache(maxsize=1)
def _assemble_card_ids():
    return frozenset(
        card['id'] for card in _cards()
        if _ASSEMBLE_MARK in (card.get('text') or ''))


def is_assemble_card(card_id):
    """这张牌的文本里带「拼装」（打出/战吼会弹拼装面板）。"""
    return bool(card_id) and card_id in _assemble_card_ids()


def is_assemble_choice(card_id, source_entity=None):
    """当前这个选择框是不是拼装面板。

    判据（任一成立）：
      * 发起者的卡牌文本带「拼装」（暗影塑形、F3TCH3R寻回机…）；
      * 发起者身上有 ``ASSEMBLE_TARGET`` 标签（日志里给拼装来源打的标记）。
    """
    if is_assemble_card(card_id):
        return True
    query = getattr(source_entity, 'query_tag', None)
    if query is None:
        return False
    try:
        return str(query(_ASSEMBLE_TARGET_TAG)) not in ("", "0")
    except Exception:                            # noqa: BLE001
        return False
