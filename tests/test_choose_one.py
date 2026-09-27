import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import patch

import click as clicks
from log_op import parse_line
from log_state import LogState, update_state
from manual_controller import ClickExecutor, ManualController, DiscoverChoiceAction
from src.game_state.recommendation_adapter import adapt_action, RecommendationStateError
from src.ocr.stable_reader import StableRecommendationReader
from src.parser.recommendation_parser import RecommendationParser
from src.recommendation_models import ActionKind, OcrEvidence, OcrLine


class ChooseOneTests(unittest.TestCase):
    def feed(self, log, text):
        parsed = parse_line('D 23:06:06.3682005 GameState.DebugPrintOptions() - ' + text)
        self.assertIsNotNone(parsed, text)
        update_state(log, parsed)

    def options(self, source='AV_205p', player='1', entity_id='152',
                choices=None):
        log = LogState()
        log.my_player_id = '1'
        self.feed(log, 'id=23')
        self.feed(log, f'  option 5 type=POWER mainEntity=[entityName=培育 id={entity_id} zone=PLAY zonePos=0 cardId={source} player={player}] error=NONE errorParam=')
        if choices is None:
            choices = {
                'EX1_154': ((0, '阳炎之怒', 'EX1_154a'), (1, '自然之怒', 'EX1_154b')),
                'EX1_165': ((0, '猎豹形态', 'EX1_165a'), (1, '熊形态', 'EX1_165b')),
            }.get(source, ((0, '山谷植根', 'AV_205pb'), (1, '冰雪绽放', 'AV_205a')))
        for i, name, card in choices:
            self.feed(log, f'    subOption {i} entity=[entityName={name} id={153+i} zone=SETASIDE zonePos=0 cardId={card} player={player}] error=NONE errorParam=')
        return log

    def state(self, log, source='AV_205p'):
        card = SimpleNamespace(entity_id='152', card_id=source, cardtype='SPELL', name='抉择牌')
        return SimpleNamespace(game_num_turns_in_play=3, is_my_turn=True,
            my_player_id='1', power_options=log.power_options,
            my_hero_power=card, my_hand_cards=[card], my_minions=[], my_locations=[],
            oppo_minions=[], oppo_locations=[], my_hero=SimpleNamespace(entity_id='hero'),
            oppo_hero=SimpleNamespace(entity_id='enemy'), discover_choice_count=None)

    def proposed(self, primary='使用英雄技能', name='冰雪绽放', target=''):
        text = f'打法参考A\n{primary}\n培育\n选择卡牌\n{name}\n{target}\n打法参考B\n结束回合'
        return RecommendationParser().parse(SimpleNamespace(frame_id='frame',
            normalized_text=text, confidence=.99), 3, 7)

    def test_named_hero_power_selects_right_option_before_target(self):
        state = self.state(self.options())
        adapted = adapt_action(self.proposed(target='目标是己方英雄'), state)
        events = []
        executor = ClickExecutor(click_module=clicks, action_context=nullcontext,
                                 sleep_func=lambda _: None)
        with patch.object(clicks, 'cancel_click', side_effect=lambda: events.append('cancel')), \
             patch.object(clicks, 'click_skill', side_effect=lambda: events.append('power')), \
             patch.object(clicks, 'choose_discover_card', side_effect=lambda i, n: events.append((i, n))), \
             patch.object(clicks, 'choose_my_hero', side_effect=lambda: events.append('target')):
            result = ManualController(executor=executor, output_func=lambda _: None).execute(adapted.manual_action, state)
        self.assertTrue(result.executed, result.message)
        self.assertEqual(['cancel', 'power', (1, 2), 'target', 'cancel'], events)

    def test_named_spell_selects_option_before_spell_target(self):
        log = self.options('EX1_154')
        state = self.state(log, 'EX1_154')
        action = adapt_action(self.proposed('打出1号位法术', name='自然之怒', target='目标是对方英雄'), state)
        events = []
        executor = ClickExecutor(click_module=clicks, action_context=nullcontext, sleep_func=lambda _: None)
        with patch.object(clicks, 'cancel_click', side_effect=lambda: events.append('cancel')), \
             patch.object(clicks, 'choose_card', side_effect=lambda *a: events.append('hand')), \
             patch.object(clicks, 'click_middle', side_effect=lambda: events.append('play')), \
             patch.object(clicks, 'choose_discover_card', side_effect=lambda i, n: events.append((i, n))), \
             patch.object(clicks, 'choose_oppo_hero', side_effect=lambda: events.append('target')):
            result = ManualController(executor=executor, output_func=lambda _: None).execute(action.manual_action, state)
        self.assertTrue(result.executed, result.message)
        self.assertEqual(['cancel', 'hand', 'play', (1, 2), 'target', 'cancel'], events)

    def test_choose_one_minion_keeps_placement_and_selects_form(self):
        state = self.state(self.options('EX1_165'), 'EX1_165')
        state.my_hand_cards[0].cardtype = 'MINION'
        action = adapt_action(self.proposed('打出1号位随从', name='熊形态'), state)
        events = []
        executor = ClickExecutor(click_module=clicks, action_context=nullcontext, sleep_func=lambda _: None)
        with patch.object(clicks, 'cancel_click', side_effect=lambda: events.append('cancel')), \
             patch.object(clicks, 'choose_card', side_effect=lambda *a: events.append('hand')), \
             patch.object(clicks, 'put_minion', side_effect=lambda i, n: events.append(('place', i, n))), \
             patch.object(clicks, 'choose_discover_card', side_effect=lambda i, n: events.append(('choice', i, n))):
            result = ManualController(executor=executor, output_func=lambda _: None).execute(action.manual_action, state)
        self.assertTrue(result.executed, result.message)
        self.assertEqual(['cancel', 'hand', ('place', 0, 0), ('choice', 1, 2), 'cancel'], events)

    def test_disabled_choice_keeps_its_layout_slot_but_cannot_be_selected(self):
        log = self.options()
        self.feed(log, '    subOption 0 entity=[entityName=山谷植根 id=153 zone=SETASIDE zonePos=0 cardId=AV_205pb player=1] error=REQ_ENOUGH_MANA errorParam=')
        manual = adapt_action(self.proposed(), self.state(log)).manual_action
        self.assertEqual(DiscoverChoiceAction(1, 2), manual.choose_one)
        with self.assertRaisesRegex(RecommendationStateError, 'choose_one_option_unavailable'):
            adapt_action(self.proposed(name='山谷植根'), self.state(log))

    def test_choice_resolves_from_state_when_card_table_lags(self):
        # WON_113 艾泽拉斯的勇士 实测：cards.json 里没有 CHOOSE_ONE 标签
        # （子牌 WON_113a/b = 为了联盟！/为了部落！），但 Power.log 的
        # option/subOption 把子项和面板下标一起给了。卡表过期不应该让自动化
        # 退化成「只点手牌、然后卡在等选择」，所以状态优先于卡表。
        log = self.options('WON_113')
        state = self.state(log, 'WON_113')
        state.my_hero_power = None
        adapted = adapt_action(
            self.proposed('打出1号位法术', name='冰雪绽放'), state)
        self.assertEqual(DiscoverChoiceAction(1, 2),
                         adapted.manual_action.choose_one)

    def test_learned_layout_resolves_when_live_suboptions_are_gone(self):
        """2026-09-22 13:46 的真实卡手：推荐来时子项已经不再下发。

        Power.log 实证（星辰坠落 id=110 NEW1_007）：
          13:45:27  option 4 … cardId=NEW1_007 error=NONE
                      subOption 0 = 星辰领主(NEW1_007b)
                      subOption 1 = 星辰漂流(NEW1_007a)
          13:45:38+ 之后所有 options 块子项数 = 0
          13:46:15  盒子才说「打出3号位法术/选择卡牌/星辰领主」→ 连卡 19 次

        子项只在牌「当前可打出」时下发，所以必须把学到的那份顺序留作兜底。
        """
        log = LogState()
        log.my_player_id = '1'
        self.feed(log, 'id=1')
        self.feed(log, '  option 4 type=POWER mainEntity=[entityName=星辰坠落 '
                       'id=110 zone=HAND zonePos=3 cardId=NEW1_007 player=1] '
                       'error=NONE errorParam=')
        self.feed(log, '    subOption 0 entity=[entityName=星辰领主 id=111 '
                       'zone=SETASIDE zonePos=0 cardId=NEW1_007b player=1] '
                       'error=NONE errorParam=')
        self.feed(log, '    subOption 1 entity=[entityName=星辰漂流 id=112 '
                       'zone=SETASIDE zonePos=0 cardId=NEW1_007a player=1] '
                       'error=NONE errorParam=')
        self.assertEqual({'星辰领主': 0, '星辰漂流': 1},
                         log.power_option_layouts['NEW1_007'])

        # 推荐来的时候，实时 power_options 里已经没有这张牌的子项了。
        spell = SimpleNamespace(entity_id='110', card_id='NEW1_007',
                                cardtype='SPELL', name='星辰坠落')
        state = SimpleNamespace(
            game_num_turns_in_play=3, is_my_turn=True, my_player_id='1',
            power_options={}, my_hero_power=None,
            my_hand_cards=[spell], my_minions=[], my_locations=[],
            oppo_minions=[], oppo_locations=[],
            my_hero=SimpleNamespace(entity_id='hero'),
            oppo_hero=SimpleNamespace(entity_id='enemy'),
            discover_choice_count=None,
            power_option_layouts=log.power_option_layouts)
        adapted = adapt_action(
            self.proposed('打出1号位法术', name='星辰领主'), state)
        self.assertEqual(DiscoverChoiceAction(0, 2),
                         adapted.manual_action.choose_one)

        # 名字对不上学过的选项时仍要报错，不盲点。
        with self.assertRaisesRegex(RecommendationStateError,
                                    'choose_one_options_unavailable'):
            adapt_action(
                self.proposed('打出1号位法术', name='不存在的选项'), state)

    def test_learned_layout_is_keyed_by_sub_card_too(self):
        """学到顺序要同时挂在子牌卡号下。

        2026-09-23 13:40 的卡手：盒子说「打出3号位法术/星辰坠落/选择卡牌/
        星辰漂流」，适配器拿到的 card_id 却是 CATA_153（另一个版本号），
        只按主牌卡号 NEW1_007 记录的表就查不到 → choose_one_card_unknown 卡 19 次。
        """
        log = LogState()
        log.my_player_id = '1'
        self.feed(log, 'id=1')
        self.feed(log, '  option 4 type=POWER mainEntity=[entityName=星辰坠落 '
                       'id=110 zone=HAND zonePos=3 cardId=NEW1_007 player=1] '
                       'error=NONE errorParam=')
        self.feed(log, '    subOption 0 entity=[entityName=星辰领主 id=111 '
                       'zone=SETASIDE zonePos=0 cardId=NEW1_007b player=1] '
                       'error=NONE errorParam=')
        self.feed(log, '    subOption 1 entity=[entityName=星辰漂流 id=112 '
                       'zone=SETASIDE zonePos=0 cardId=NEW1_007a player=1] '
                       'error=NONE errorParam=')

        self.assertEqual({'星辰领主': 0, '星辰漂流': 1},
                         log.power_option_layouts['NEW1_007'])
        # 子牌键下也要是**完整**的一份：否则 len() 当选项总数会算错。
        self.assertEqual({'星辰领主': 0, '星辰漂流': 1},
                         log.power_option_layouts['NEW1_007b'])
        self.assertEqual({'星辰领主': 0, '星辰漂流': 1},
                         log.power_option_layouts['NEW1_007a'])

        # 用子牌卡号去查也能命中：模拟 card_id 解析成了子牌。
        from src.game_state.recommendation_adapter import _learned_choice
        state = SimpleNamespace(power_option_layouts=log.power_option_layouts)
        self.assertEqual((1, 2), _learned_choice(state, 'NEW1_007a', '星辰漂流'))

    def test_name_without_table_or_state_refuses_to_guess(self):
        # 盒子给了选项名，卡表不认识这张牌，状态里又没有可用的同名子项：
        # 这是识别缺口，必须报错重试，不能静默当成普通打法盲点。
        state = SimpleNamespace(game_num_turns_in_play=3, is_my_turn=True,
            my_player_id='1', power_options={},
            my_hero_power=None, my_hand_cards=[], my_minions=[], my_locations=[],
            oppo_minions=[], oppo_locations=[],
            my_hero=SimpleNamespace(entity_id='hero'),
            oppo_hero=SimpleNamespace(entity_id='enemy'), discover_choice_count=None)
        card = SimpleNamespace(entity_id='152', card_id='UNKNOWN_1',
                               cardtype='SPELL', name='抉择牌')
        state.my_hand_cards = [card]
        with self.assertRaisesRegex(RecommendationStateError,
                                    'choose_one_card_unknown'):
            adapt_action(
                self.proposed('打出1号位法术', name='冰雪绽放'), state)

    def test_azeroth_alliance_recommendation_clicks_option_after_hand_card(self):
        """2026-09-18 22:39:24 的真实推荐：打出7号位法术 / 选择卡牌 / 为了联盟！

        日志原文（Power.log L90898-90900）：
          option 6 ... [entityName=艾泽拉斯的勇士 id=207 zone=HAND zonePos=7
                        cardId=WON_113 player=1] error=NONE
            subOption 0 ... [entityName=为了联盟！ id=208 zone=SETASIDE
                             cardId=WON_113a player=1] error=NONE
            subOption 1 ... [entityName=为了部落！ id=209 zone=SETASIDE
                             cardId=WON_113b player=1] error=NONE

        修复前：卡表没有 WON_113 → 静默放行 → 只点手牌 → 卡在等选阵营 →
        连续 4 轮 result_not_confirmed。修复后应点完手牌后补点「为了联盟！」。
        """
        log = LogState()
        log.my_player_id = '1'
        for text in (
            'id=66',
            '  option 0 type=END_TURN mainEntity= error=INVALID errorParam=',
            '  option 6 type=POWER mainEntity=[entityName=艾泽拉斯的勇士 id=207 '
            'zone=HAND zonePos=7 cardId=WON_113 player=1] error=NONE errorParam=',
            '    subOption 0 entity=[entityName=为了联盟！ id=208 zone=SETASIDE '
            'zonePos=0 cardId=WON_113a player=1] error=NONE errorParam=',
            '    subOption 1 entity=[entityName=为了部落！ id=209 zone=SETASIDE '
            'zonePos=0 cardId=WON_113b player=1] error=NONE errorParam=',
        ):
            self.feed(log, text)

        spell = SimpleNamespace(entity_id='207', card_id='WON_113',
                                cardtype='SPELL', name='艾泽拉斯的勇士')
        hand = [SimpleNamespace(entity_id=str(90 + i), card_id='FILLER',
                                cardtype='SPELL', name='填充')
                for i in range(6)] + [spell]
        state = SimpleNamespace(game_num_turns_in_play=3, is_my_turn=True,
            my_player_id='1', power_options=log.power_options,
            my_hero_power=None, my_hand_cards=hand, my_minions=[],
            my_locations=[], oppo_minions=[], oppo_locations=[],
            my_hero=SimpleNamespace(entity_id='hero'),
            oppo_hero=SimpleNamespace(entity_id='enemy'),
            discover_choice_count=None)

        proposed = RecommendationParser().parse(SimpleNamespace(
            frame_id='frame',
            normalized_text='打法参考A\n打出7号位法术\n选择卡牌\n为了联盟！\n'
                            '打法参考B\n结束回合',
            confidence=.99), 3, 7)
        self.assertEqual(ActionKind.PLAY_CARD, proposed.action)
        self.assertEqual(7, proposed.source.index)
        self.assertEqual('为了联盟！', proposed.choice_name)

        adapted = adapt_action(proposed, state)
        self.assertEqual(DiscoverChoiceAction(0, 2),
                         adapted.manual_action.choose_one)

        events = []
        executor = ClickExecutor(click_module=clicks, action_context=nullcontext,
                                 sleep_func=lambda _: None)
        with patch.object(clicks, 'cancel_click', side_effect=lambda: events.append('cancel')), \
             patch.object(clicks, 'choose_card', side_effect=lambda *a: events.append(('hand',) + a)), \
             patch.object(clicks, 'click_middle', side_effect=lambda: events.append('play')), \
             patch.object(clicks, 'choose_discover_card', side_effect=lambda i, n: events.append(('choice', i, n))):
            result = ManualController(executor=executor,
                                      output_func=lambda _: None).execute(
                adapted.manual_action, state)
        self.assertTrue(result.executed, result.message)
        self.assertIn(('choice', 0, 2), events, events)
        self.assertLess(events.index('play'), events.index(('choice', 0, 2)),
                        events)

    def test_unknown_or_ambiguous_name_never_guesses(self):
        log = self.options()
        with self.assertRaisesRegex(RecommendationStateError, 'choose_one_name'):
            adapt_action(self.proposed(name='错误名称'), self.state(log))
        self.feed(log, '    subOption 2 entity=[entityName=冰雪绽放 id=155 zone=SETASIDE zonePos=0 cardId=AV_205a player=1] error=NONE errorParam=')
        with self.assertRaisesRegex(RecommendationStateError, 'choose_one_name'):
            adapt_action(self.proposed(), self.state(log))

    def test_new_options_block_and_enemy_options_cannot_reuse_old_choices(self):
        log = self.options()
        self.feed(log, 'id=24')
        with self.assertRaisesRegex(RecommendationStateError, 'choose_one_options'):
            adapt_action(self.proposed(), self.state(log))
        # 对手的牌：选项里的实体号不在己方手牌/场上 → 不能点。
        enemy = self.state(self.options(entity_id='999'))
        with self.assertRaisesRegex(RecommendationStateError, 'choose_one_options'):
            adapt_action(self.proposed(), enemy)

    def test_stolen_minion_still_resolves_from_state(self):
        # 抢来的随从（如控制权变更偷到的泰坦）：``player=`` 仍是原主（对手），
        # 但 CONTROLLER 已经是自己。控制权必须按「实体在不在己方场上」判定，
        # 否则这类牌会被全部拒掉并卡死。
        log = self.options('TTN_800', player='1', entity_id='196',
                           choices=((0, '沙加恩之怒', 'TTN_800t3'),
                                    (1, '狂海怒涛', 'TTN_800t'),
                                    (2, '天空之王', 'TTN_800t2')))
        card = SimpleNamespace(entity_id='196', card_id='TTN_800',
                               cardtype='MINION', name='雷霆之神高戈奈斯',
                               zone_pos=2)
        stolen = SimpleNamespace(
            game_num_turns_in_play=3, is_my_turn=True, my_player_id='2',
            power_options=log.power_options, my_hero_power=None,
            my_hand_cards=[], my_minions=[card], my_locations=[],
            oppo_minions=[], oppo_locations=[],
            my_hero=SimpleNamespace(entity_id='hero'),
            oppo_hero=SimpleNamespace(entity_id='enemy'),
            discover_choice_count=None)
        adapted = adapt_action(
            self.proposed('操作1号位泰坦', name='狂海怒涛'), stolen)
        action = adapted.manual_action
        self.assertEqual('TTN_800', action.card_id)
        self.assertEqual(1, action.ability_index)   # 狂海怒涛 = subOption 1
        self.assertEqual(3, action.ability_count)

    def test_numbered_discover_still_uses_original_layout(self):
        state = self.state(self.options())
        state.discover_choice_count = 3
        proposed = RecommendationParser().parse(SimpleNamespace(frame_id='frame',
            normalized_text='选择我方2号位卡牌', confidence=.99), 3, 7)
        manual = adapt_action(proposed, state).manual_action
        self.assertEqual(DiscoverChoiceAction(1, 3), manual)

    def test_choice_name_participates_in_ocr_confidence_and_stability(self):
        reader = StableRecommendationReader(SimpleNamespace(min_ocr_confidence=.9), None,
            text_normalizer=RecommendationParser.normalize_action_text)
        evidence = OcrEvidence('frame', 0, tuple(OcrLine(t, c) for t, c in (
            ('使用英雄技能', .99), ('培育', .99), ('选择卡牌', .99), ('冰雪绽放', .5))),
            '使用英雄技能\n培育\n选择卡牌\n冰雪绽放', .99, 'test', 'test')
        result = reader._action_evidence(evidence)
        self.assertEqual('使用英雄技能\n选择卡牌\n冰雪绽放', result.normalized_text)
        self.assertEqual(.5, result.confidence)


if __name__ == '__main__':
    unittest.main()
