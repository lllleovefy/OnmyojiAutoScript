from __future__ import annotations

import unittest

from tasks.Duel.bp import BPObservation, DuelBPAssistant, DuelBPMode, DuelBPState
from tasks.Duel.identity import DuelIdentityObservation
from tasks.Duel.script_task import ScriptTask


def bare_task(mode: DuelBPMode = DuelBPMode.OBSERVE) -> ScriptTask:
    task = ScriptTask.__new__(ScriptTask)
    task.bp_assistant = DuelBPAssistant(mode=mode)
    task.bp_last_decision = None
    task._bp_last_observation = None
    task._bp_result_published = False
    task._bp_last_published_state = None
    task._bp_last_published_recommendation = None
    task._bp_own_picks = ()
    task._bp_opponent_picks = ()
    task._bp_bans = ()
    task._bp_seen_onmyoji_selection = False
    task._bp_last_identity = DuelIdentityObservation()
    task._duel_aborted_before_battle = False
    task._duel_abort_reason = None
    task.bp_rule_candidates = lambda observation: ()
    task.bp_personal_candidates = lambda observation: ()
    task.bp_external_candidates = lambda observation: ()
    task.publish_bp_live_event = lambda event, data: None
    return task


class DuelTaskSafetyTest(unittest.TestCase):
    def test_observe_and_recommend_always_suppress_legacy_selection(self) -> None:
        for mode in (DuelBPMode.OBSERVE, DuelBPMode.RECOMMEND):
            with self.subTest(mode=mode):
                task = bare_task(mode)
                task.recognize_bp_observation = lambda: BPObservation(
                    DuelBPState.SELF_PICK,
                    confidence=1.0,
                )
                self.assertTrue(task.handle_bp_assistant())

    def test_aborted_result_is_unknown_not_loss(self) -> None:
        task = bare_task()
        events = []
        task.publish_bp_live_event = lambda event, data: events.append(
            (event, data)
        )
        task._duel_aborted_before_battle = True
        task._duel_abort_reason = "configured_pick_banned"
        task.publish_bp_result_event(None)

        self.assertEqual(1, len(events))
        payload = events[0][1]
        self.assertEqual("unknown", payload["result"])
        self.assertFalse(payload["valid"])
        self.assertTrue(payload["aborted_before_battle"])
        self.assertEqual("configured_pick_banned", payload["abort_reason"])

    def test_explicit_selection_exit_marks_abort_before_cleanup(self) -> None:
        task = bare_task()
        task.screenshot = lambda: None
        task.appear = lambda rule: True
        task.appear_then_click = lambda rule, interval=None: False
        task.duel_exit_battle(
            abort_before_battle=True,
            reason="configured_pick_banned",
        )
        self.assertTrue(task._duel_aborted_before_battle)
        self.assertEqual("configured_pick_banned", task._duel_abort_reason)


if __name__ == "__main__":
    unittest.main()
