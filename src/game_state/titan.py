"""泰坦（Titan）随从的技能识别。

泰坦随从不能攻击，改成「点自己场上的泰坦 → 弹出三张技能牌（和抉择/发现同一套
居中卡牌面板）→ 选技能 →（技能需要目标时）再点目标」。
盒子对这类牌给的指令是「操作N号位泰坦 + 技能名」，因此执行前必须把技能名解析成
技能面板上的下标：

  1. 首选 Power.log 的 option/subOption（泰坦打出的那个回合，服务器会把三张技能
     子牌一起下发，和抉择的 power_options 同源），下标就是面板从左到右的位置；
  2. 其次用本局学到过的「卡牌 → 技能名顺序」（见 log_state 的 titan_ability_choices），
     应对泰坦是上回合留在场上、日志不再重发子选项的情况；
  3. 最后才是这里手写的静态兜底表（只收录实测日志确认过的泰坦）。

任何一步都无法唯一确定时都必须抛错重试，绝不猜着点。
"""

import json
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def titan_card_ids():
    # 只读项目本地的卡牌数据，绝不在点击路径里联网。
    with (Path(__file__).resolve().parents[2] / 'cards.json').open(encoding='utf8') as source:
        return frozenset(card['id'] for card in json.load(source)
                         if 'TITAN' in card.get('mechanics', []))


# 泰坦技能面板固定是三张技能牌。12 张泰坦实测每张都是三个技能（额外的同前缀
# 子牌是武器/衍生随从/附魔，不会出现在技能面板里），所以只读到一两张子选项就
# 说明这次日志读取不完整——必须报错重试，不能按错误的卡位去点。
TITAN_ABILITY_COUNT = 3


# card_id -> 技能名（顺序即技能面板从左到右的位置，下标可直接用于点击）。
# 仅在 Power.log 既没有当前选项、也没学到本局选项时兜底使用。
# TTN_737 兵主：实测 Power.log 的 subOption 0/1/2 = 鲜血符文/冰霜符文/邪恶符文。
TITAN_ABILITY_LAYOUTS = {
    'TTN_737': ('鲜血符文', '冰霜符文', '邪恶符文'),
}


def static_ability_choice(card_id, name):
    """静态兜底：返回 ``(下标, 技能数量)``；未收录或名字对不上返回 None。"""
    layout = TITAN_ABILITY_LAYOUTS.get(card_id)
    if not layout or name not in layout:
        return None
    return layout.index(name), len(layout)
