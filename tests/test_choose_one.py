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
from src.recommendation_models import OcrEvidence, OcrLine


class ChooseOneTests(unittest.TestCase):
    def feed(self, log, text):
        parsed = parse_line('D 23:06:06.3682005 GameState.DebugPrintOptions() - ' + text)
        self.assertIsNotNone(parsed, text)
        update_state(log, parsed)

    def options(self, source='AV_205p', player='1'):
        log = LogState()
        log.my_player_id = '1'
        self.feed(log, 'id=23')
        self.feed(log, f'  option 5 type=POWER mainEntity=[entityName=培育 id=152 zone=PLAY zonePos=0 cardId={source} player={player}] error=NONE errorParam=')
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

    def test_non_choose_one_keeps_original_hero_power_action(self):
        state = self.state(self.options('CS2_017'), 'CS2_017')
        adapted = adapt_action(self.proposed(), state)
        self.assertIsNone(getattr(adapted.manual_action, 'choose_one', None))

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
        with self.assertRaisesRegex(RecommendationStateError, 'choose_one_options'):
            adapt_action(self.proposed(), self.state(self.options(player='2')))

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
