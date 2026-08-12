import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import cv2

from tasks.ActivityShikigami.activities.budo_tournament import BudoTournamentAct
from tasks.ActivityShikigami.base_act import BaseAct, TicketsNotEnough
import tasks.ActivityShikigami.page as pages


class BudoTournamentActTest(unittest.TestCase):
    def make_act(self, climb_type: str) -> BudoTournamentAct:
        act = object.__new__(BudoTournamentAct)
        battle_conf = object()
        general_climb = SimpleNamespace(random_sleep=False)
        act.__dict__['conf'] = SimpleNamespace(
            general_climb=general_climb,
            pass_battle_conf=battle_conf,
            ap_battle_conf=battle_conf,
        )
        act.__dict__['run_sequence'] = [climb_type]
        act.run_idx = 0
        act._count_map = {climb_type: 0}
        act.switch_soul = Mock()
        act.enter_battle = Mock(return_value=True)
        act.run_general_battle = Mock()
        return act

    def test_pass_counts_only_after_battle_entry(self):
        act = self.make_act('pass')
        act._open_pass_challenge = Mock()

        act.enter_battle.return_value = False
        act._run_pass()
        self.assertEqual(act.count_map['pass'], 0)

        act.enter_battle.return_value = True
        act._run_pass()
        self.assertEqual(act.count_map['pass'], 1)
        act._open_pass_challenge.assert_called()
        act.enter_battle.assert_called_with(act.I_BUDO_PASS_FIRE)

    def test_ap_counts_only_after_battle_entry(self):
        act = self.make_act('ap')

        act.enter_battle.return_value = False
        act._run_ap()
        self.assertEqual(act.count_map['ap'], 0)

        act.enter_battle.return_value = True
        act._run_ap()
        self.assertEqual(act.count_map['ap'], 1)
        act.enter_battle.assert_called_with(act.I_BUDO_AP_FIRE)

    def test_unsupported_modes_are_filtered(self):
        act = object.__new__(BudoTournamentAct)
        act.__dict__['conf'] = SimpleNamespace(
            general_climb=SimpleNamespace(run_sequence_v=['boss', 'pass', 'ap100', 'ap'])
        )

        self.assertEqual(act.run_sequence, ['pass', 'ap'])

    def test_pass_lock_is_safely_skipped(self):
        act = self.make_act('pass')
        act.screenshot = Mock()

        act.lock_team(SimpleNamespace(lock_team_enable=True))

        act.screenshot.assert_not_called()

    def test_pass_search_waits_for_start_challenge(self):
        act = self.make_act('pass')
        state = {'opened': False}
        act.screenshot = Mock()
        act.appear = Mock(side_effect=lambda target, **_kwargs:
                          state['opened'] if target is act.I_BUDO_PASS_FIRE else False)

        def click(target, **_kwargs):
            if target is act.I_BUDO_SEARCH:
                state['opened'] = True
                return True
            return False

        act.appear_then_click = Mock(side_effect=click)

        act._open_pass_challenge()

        act.appear_then_click.assert_any_call(act.I_BUDO_SEARCH, interval=1.5)

    def test_pass_search_stops_after_repeated_failure(self):
        act = self.make_act('pass')
        act.screenshot = Mock()
        act.appear = Mock(return_value=False)
        act.appear_then_click = Mock(
            side_effect=lambda target, **_kwargs: target is act.I_BUDO_SEARCH
        )

        with patch('tasks.ActivityShikigami.activities.budo_tournament.random.randint', return_value=3):
            with self.assertRaises(TicketsNotEnough):
                act._open_pass_challenge()

    def test_navigation_contains_only_supported_activity_pages(self):
        source_pages = (
            pages.page_main,
            pages.page_act,
            pages.page_act_pass,
            pages.page_act_ap,
            pages.page_act_ap100,
            pages.page_act_boss,
        )
        page_map = {page.key: page.clone() for page in source_pages}
        page_act = page_map[pages.page_act.key]
        for target in (pages.page_act_pass, pages.page_act_ap,
                       pages.page_act_ap100, pages.page_act_boss):
            page_act.connect(page_map[target.key], object(), key=f'old->{target.key}')

        act = object.__new__(BudoTournamentAct)
        act.navigator = SimpleNamespace(resolve_page=lambda page: page_map[page.key])
        with patch.object(BaseAct, 'before_run'):
            act.before_run()

        destinations = {transition.destination.key for transition in page_act.transitions}
        self.assertEqual(destinations, {pages.page_act_pass.key, pages.page_act_ap.key})
        main_transition = next(
            transition for transition in page_map[pages.page_main.key].transitions
            if transition.key == 'page_main->page_act'
        )
        self.assertIs(main_transition.action, act.I_BUDO_MAIN_ENTRY)


class BudoTournamentAssetsTest(unittest.TestCase):
    ROOT = Path(__file__).resolve().parents[1]
    ASSET_DIR = ROOT / 'tasks' / 'ActivityShikigami' / 'as'

    def test_new_assets_have_expected_threshold_and_dimensions(self):
        definitions = []
        for filename in ('pages.json', 'image.json'):
            with (self.ASSET_DIR / filename).open(encoding='utf-8') as file:
                definitions.extend(json.load(file))

        budo_assets = [item for item in definitions if item['itemName'].startswith('budo_')]
        self.assertEqual(len(budo_assets), 12)
        for item in budo_assets:
            self.assertGreaterEqual(item['threshold'], 0.8)
            image = cv2.imread(str(self.ASSET_DIR / item['imageName']))
            self.assertIsNotNone(image, item['imageName'])
            _, _, width, height = map(int, item['roiFront'].split(','))
            self.assertEqual(image.shape[:2], (height, width), item['imageName'])


if __name__ == '__main__':
    unittest.main()
