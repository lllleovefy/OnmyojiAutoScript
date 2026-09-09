from __future__ import annotations

import unittest
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from tasks.Duel.bp import (
    BPObservation,
    DuelBPAssistant,
    DuelDraftLedger,
    DuelBPMode,
    DuelBPState,
    DuelRecommendation,
    DuelRecommendationCandidate,
    RecommendationSource,
)
from tasks.Duel.config import DuelConfig
from tasks.Duel.identity import DuelIdentityObservation
from tasks.Duel.phase import DuelBPPhaseSignals
from tasks.Duel.selection import (
    OPPONENT_REVEAL_NAME_ROI,
    SELECTED_NAME_ROI,
    VisibleCandidate,
)
from tasks.Duel.script_task import ScriptTask


def bare_task(mode: DuelBPMode = DuelBPMode.OBSERVE) -> ScriptTask:
    task = ScriptTask.__new__(ScriptTask)
    task.bp_assistant = DuelBPAssistant(mode=mode)
    task.bp_last_decision = None
    task._bp_last_observation = None
    task._bp_result_published = False
    task._bp_last_published_state = None
    task._bp_last_published_recommendation = None
    task._bp_last_published_action = None
    task._bp_own_picks = ()
    task._bp_opponent_picks = ()
    task._bp_bans = ()
    task._bp_ledger = DuelDraftLedger()
    task._bp_opponent_slot_meta = [
        {
            "slot": slot,
            "shikigami_id": None,
            "confidence": 0.0,
            "source": "",
            "status": "pending",
        }
        for slot in range(1, 6)
    ]
    task._bp_selected_onmyoji = None
    task._bp_onmyoji_action_issued = False
    task._bp_onmyoji_attempts = 0
    task._bp_onmyoji_template_missing_reported = False
    task._bp_selection_in_progress = False
    task._bp_pending_source = None
    task._bp_manual_name_candidate = None
    task._bp_manual_name_frames = 0
    task._bp_manual_pending_verified = False
    task._bp_manual_baseline_round = None
    task._bp_manual_baseline_id = None
    task._bp_manual_selection_changed = False
    task._bp_manual_tracking_blocked = False
    task._bp_candidate_cache_key = None
    task._bp_candidate_cache = None
    task._bp_seen_onmyoji_selection = False
    task._bp_onmyoji_signal_frames = 0
    task._bp_last_phase_signals = None
    task._bp_reveal_pair_round = None
    task._bp_reveal_pair_ids = None
    task._bp_reveal_pair_frames = 0
    task._bp_reveal_pair_confidence = 0.0
    task._bp_reveal_episode_consumed = False
    task._bp_last_identity = DuelIdentityObservation()
    task._bp_sample_collector = None
    task._duel_aborted_before_battle = False
    task._duel_abort_reason = None
    task.bp_rule_candidates = lambda observation: ()
    task.bp_personal_candidates = lambda observation: ()
    task.bp_external_candidates = lambda observation: ()
    task.publish_bp_live_event = lambda event, data: None
    task._bp_actionable_self_pick = lambda: True
    task.conf = SimpleNamespace(duel_config=DuelConfig())
    return task


def complete_bp_ledger(task: ScriptTask) -> None:
    for identity in ("1", "2", "3", "4", "5"):
        task._bp_ledger.begin_own_pick(identity)
        task._bp_ledger.commit_pending_own_pick()


class DuelTaskSafetyTest(unittest.TestCase):
    def test_auto_mode_is_downgraded_until_persistent_gates_pass(self) -> None:
        task = bare_task(DuelBPMode.AUTO)
        task.conf.duel_config.bp_mode = DuelBPMode.AUTO
        task.conf.duel_config.bp_auto_verified = False

        assistant = task.create_bp_assistant()

        self.assertEqual(DuelBPMode.RECOMMEND, assistant.mode)

    def test_auto_rejects_forged_noncanonical_portrait_library(self) -> None:
        task = bare_task(DuelBPMode.AUTO)
        task.conf.duel_config.bp_mode = DuelBPMode.AUTO
        task.conf.duel_config.bp_auto_verified = True
        with tempfile.TemporaryDirectory() as directory:
            fake_root = Path(directory)
            (fake_root / "coverage.json").write_text(
                json.dumps(
                    {
                        "asset_count": 274,
                        "covered_count": 274,
                        "items": [
                            {"id": str(index), "covered": True}
                            for index in range(274)
                        ],
                    }
                ),
                encoding="utf-8",
            )
            task.conf.duel_config.bp_portrait_library = str(fake_root)

            failures = task._bp_auto_prerequisite_failures()
            assistant = task.create_bp_assistant()

        self.assertIn("portrait_library_not_canonical", failures)
        self.assertEqual(DuelBPMode.RECOMMEND, assistant.mode)

    def test_auto_does_not_require_onmyoji_identity_templates(
        self,
    ) -> None:
        task = bare_task(DuelBPMode.AUTO)
        task.conf.duel_config.bp_mode = DuelBPMode.AUTO
        task.conf.duel_config.bp_auto_verified = True
        assets = [{"id": 596, "name": "神无月", "alias": []}]

        with (
            patch(
                "module.duel_data.repository."
                "DuelRepository.latest_snapshot",
                return_value=assets,
            ),
            patch(
                "tasks.Duel.portrait_library.load_image",
                side_effect=FileNotFoundError,
            ),
        ):
            failures = task._bp_auto_prerequisite_failures()
            assistant = task.create_bp_assistant()

        self.assertEqual((), failures)
        self.assertEqual(DuelBPMode.AUTO, assistant.mode)

    def test_stable_name_does_not_inflate_low_ocr_confidence(self) -> None:
        task = bare_task(DuelBPMode.AUTO)
        task.screenshot = lambda: None
        task._recognize_bp_name_roi = lambda roi, **kwargs: {
            "shikigami_id": "596",
            "name": "神无月",
            "confidence": 0.97,
            "consensus": 2,
            "text": "神无月",
        }

        with patch("tasks.Duel.script_task.sleep", lambda _: None):
            result = task._recognize_bp_name_stable(
                (0, 0, 10, 10),
                expected_id="596",
                min_confidence=0.98,
            )

        self.assertIsNone(result)

    def test_name_recognizer_uses_the_shared_ocr_provider(self) -> None:
        task = bare_task(DuelBPMode.AUTO)
        task._bp_name_index = object()
        task._bp_name_ocr_model = None
        task._bp_strict_name_recognizer = object()
        shared_model = object()

        with (
            patch(
                "module.ocr.models.get_ocr_model",
                return_value=shared_model,
            ) as shared_provider,
            patch("module.ocr.ppocr.TextSystem") as local_model,
        ):
            task._ensure_bp_name_recognizer()

        self.assertIs(shared_model, task._bp_name_ocr_model)
        shared_provider.assert_called_once_with("ch")
        local_model.assert_not_called()

    def test_off_mode_can_capture_without_changing_legacy_result(self) -> None:
        task = bare_task(DuelBPMode.OFF)
        captures = []

        class Collector:
            def capture(self, image, **kwargs):
                captures.append((image.copy(), kwargs))

                class Result:
                    captured = False

                return Result()

        task._bp_sample_collector = Collector()
        task.device = type(
            "Device",
            (),
            {"image": np.zeros((720, 1280, 3), dtype=np.uint8)},
        )()
        task.recognize_bp_observation = lambda: BPObservation(
            DuelBPState.SELF_PICK,
            confidence=1.0,
        )

        self.assertFalse(task.handle_bp_assistant())
        self.assertEqual(1, len(captures))
        self.assertEqual("SELF_PICK", captures[0][1]["phase"])

    def test_observe_and_recommend_always_suppress_legacy_selection(self) -> None:
        for mode in (DuelBPMode.OBSERVE, DuelBPMode.RECOMMEND):
            with self.subTest(mode=mode):
                task = bare_task(mode)
                task.recognize_bp_observation = lambda: BPObservation(
                    DuelBPState.SELF_PICK,
                    confidence=1.0,
                )
                self.assertTrue(task.handle_bp_assistant())

    def test_stable_bp_progress_resets_generic_stuck_timer_once(self) -> None:
        task = bare_task()
        progress = []
        task.reset_device = lambda status: progress.append(status)
        task.recognize_bp_observation = lambda: BPObservation(
            DuelBPState.SELF_PICK,
            confidence=1.0,
        )

        for _ in range(5):
            self.assertTrue(task.handle_bp_assistant())

        self.assertEqual(["DUEL_BP_PROGRESS"], progress)

        task.recognize_bp_observation = lambda: BPObservation(
            DuelBPState.OPPONENT_PICK,
            confidence=1.0,
        )
        for _ in range(3):
            self.assertTrue(task.handle_bp_assistant())

        self.assertEqual(
            ["DUEL_BP_PROGRESS", "DUEL_BP_PROGRESS"],
            progress,
        )

    def test_battle_state_does_not_replace_battle_stuck_record(self) -> None:
        task = bare_task(DuelBPMode.OBSERVE)
        progress = []
        task.reset_device = lambda status: progress.append(status)
        task.recognize_bp_observation = lambda: BPObservation(
            DuelBPState.SELF_PICK,
            confidence=1.0,
        )
        for _ in range(3):
            self.assertTrue(task.handle_bp_assistant())
        progress.clear()

        task.recognize_bp_observation = lambda: BPObservation(
            DuelBPState.BATTLE,
            confidence=1.0,
        )

        for _ in range(3):
            self.assertTrue(task.handle_bp_assistant())

        self.assertEqual([], progress)

        task.recognize_bp_observation = lambda: BPObservation(
            DuelBPState.RESULT,
            confidence=1.0,
        )
        for _ in range(3):
            self.assertTrue(task.handle_bp_assistant())

        self.assertEqual([], progress)

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

    def test_unknown_opponent_slot_is_positional_and_does_not_block_next_round(
        self,
    ) -> None:
        task = bare_task(DuelBPMode.OBSERVE)
        task._bp_ledger.begin_own_pick("596")
        task._bp_ledger.commit_pending_own_pick()
        task.device = SimpleNamespace(click=lambda **kwargs: None)
        task._recognize_opponent_portrait_stable = (
            lambda slot: (None, "399", 0.72)
        )
        saved = []
        task._save_unresolved_opponent_portrait = (
            lambda slot, **kwargs: saved.append((slot, kwargs))
        )

        self.assertTrue(task._observe_next_bp_opponent())
        self.assertEqual((None,), task._bp_ledger.opponent_context)
        self.assertTrue(task._bp_ledger.ready_for_recommendation)
        self.assertEqual(
            "unresolved", task._bp_opponent_slot_meta[0]["status"]
        )
        self.assertEqual(1, saved[0][0])

    def test_locked_reveal_pair_records_name_without_clicking_portrait(self) -> None:
        task = bare_task(DuelBPMode.RECOMMEND)
        task._bp_ledger.begin_own_pick("399")
        task._bp_ledger.commit_pending_own_pick()
        task._bp_last_phase_signals = DuelBPPhaseSignals(
            confirm_locked=0.99,
        )
        clicks = []
        task.device = SimpleNamespace(
            click=lambda **kwargs: clicks.append(kwargs)
        )
        portrait_calls = []
        task._recognize_opponent_portrait_stable = (
            lambda slot: portrait_calls.append(slot)
        )

        def recognize(roi, **kwargs):
            if roi == SELECTED_NAME_ROI:
                return {
                    "shikigami_id": "399",
                    "name": "言灵",
                    "confidence": 0.613,
                    "method": "substring",
                    "text": "满言灵",
                    "consensus": 2,
                }
            self.assertEqual(OPPONENT_REVEAL_NAME_ROI, roi)
            return {
                "shikigami_id": "583",
                "name": "卑弥呼",
                "confidence": 0.668,
                "method": "substring",
                "text": "满卑弥呼",
                "consensus": 2,
            }

        task._recognize_bp_name_roi = recognize

        self.assertFalse(task._observe_bp_reveal_pair_frame())
        self.assertFalse(task._observe_bp_reveal_pair_frame())
        self.assertTrue(task._observe_bp_reveal_pair_frame())
        self.assertFalse(task._observe_bp_reveal_pair_frame())

        self.assertEqual(("583",), task._bp_ledger.opponent_context)
        self.assertEqual([], clicks)
        self.assertEqual([], portrait_calls)
        self.assertEqual(
            "reveal_name_relaxed",
            task._bp_opponent_slot_meta[0]["source"],
        )

    def test_locked_reveal_episode_commits_once_until_next_active_turn(
        self,
    ) -> None:
        task = bare_task(DuelBPMode.OBSERVE)
        task._bp_last_phase_signals = DuelBPPhaseSignals(
            confirm_locked=0.99,
        )
        pair = ["399", "583"]
        task._recognize_bp_name_roi = lambda roi, **kwargs: {
            "shikigami_id": (
                pair[0] if roi == SELECTED_NAME_ROI else pair[1]
            ),
            "name": "",
            "confidence": 0.95,
            "method": "exact",
            "text": "",
            "consensus": 1,
        }

        commits = [
            task._observe_bp_reveal_pair_frame()
            for _ in range(8)
        ]

        self.assertEqual(1, commits.count(True))
        self.assertEqual(("399",), task._bp_ledger.own_picks)
        self.assertEqual(("583",), task._bp_ledger.opponent_context)

        task._bp_last_phase_signals = DuelBPPhaseSignals(
            confirm_active=0.99,
        )
        self.assertFalse(task._observe_bp_reveal_pair_frame())

        pair[:] = ["596", "567"]
        task._bp_last_phase_signals = DuelBPPhaseSignals(
            confirm_locked=0.99,
        )
        self.assertFalse(task._observe_bp_reveal_pair_frame())
        self.assertFalse(task._observe_bp_reveal_pair_frame())
        self.assertTrue(task._observe_bp_reveal_pair_frame())
        self.assertEqual(("399", "596"), task._bp_ledger.own_picks)
        self.assertEqual(("583", "567"), task._bp_ledger.opponent_context)

    def test_auto_accepts_relaxed_opponent_name_only_with_own_ledger_match(
        self,
    ) -> None:
        task = bare_task(DuelBPMode.AUTO)
        task._bp_ledger.begin_own_pick("399")
        task._bp_ledger.commit_pending_own_pick()
        task._bp_last_phase_signals = DuelBPPhaseSignals(
            confirm_locked=0.99,
        )
        task._recognize_bp_name_roi = lambda roi, **kwargs: {
            "shikigami_id": (
                "399" if roi == SELECTED_NAME_ROI else "583"
            ),
            "name": "",
            "confidence": (
                0.613 if roi == SELECTED_NAME_ROI else 0.668
            ),
            "method": "substring",
            "text": "",
            "consensus": 2,
        }

        self.assertFalse(task._observe_bp_reveal_pair_frame())
        self.assertFalse(task._observe_bp_reveal_pair_frame())
        self.assertTrue(task._observe_bp_reveal_pair_frame())

        self.assertEqual(("399",), task._bp_ledger.own_picks)
        self.assertEqual(("583",), task._bp_ledger.opponent_context)
        self.assertEqual(
            "reveal_name_relaxed",
            task._bp_opponent_slot_meta[0]["source"],
        )

    def test_reveal_pair_mismatch_never_advances_opponent_slot(self) -> None:
        task = bare_task(DuelBPMode.AUTO)
        task._bp_ledger.begin_own_pick("399")
        task._bp_ledger.commit_pending_own_pick()
        task._bp_last_phase_signals = DuelBPPhaseSignals(
            confirm_locked=0.99,
            opponent_locked=0.99,
        )
        task._recognize_bp_name_roi = lambda roi, **kwargs: {
            "shikigami_id": (
                "596" if roi == SELECTED_NAME_ROI else "583"
            ),
            "name": "",
            "confidence": 0.99,
            "method": "exact",
            "text": "",
            "consensus": 1,
        }

        for _ in range(4):
            self.assertFalse(task._observe_bp_reveal_pair_frame())

        self.assertEqual((), task._bp_ledger.opponent_context)

    def test_observe_reveal_pair_learns_both_sides_from_locked_names(self) -> None:
        task = bare_task(DuelBPMode.OBSERVE)
        task._bp_last_phase_signals = DuelBPPhaseSignals(
            confirm_locked=0.99,
        )
        task._recognize_bp_name_roi = lambda roi, **kwargs: {
            "shikigami_id": (
                "399" if roi == SELECTED_NAME_ROI else "583"
            ),
            "name": "",
            "confidence": 0.95,
            "method": "exact",
            "text": "",
            "consensus": 1,
        }

        self.assertFalse(task._observe_bp_reveal_pair_frame())
        self.assertFalse(task._observe_bp_reveal_pair_frame())
        self.assertTrue(task._observe_bp_reveal_pair_frame())

        self.assertEqual(("399",), task._bp_ledger.own_picks)
        self.assertEqual(("583",), task._bp_ledger.opponent_context)
        self.assertTrue(task._bp_ledger.ready_for_recommendation)

    def test_auto_reveal_pair_never_invents_a_missing_own_pick(self) -> None:
        task = bare_task(DuelBPMode.AUTO)
        task._bp_last_phase_signals = DuelBPPhaseSignals(
            confirm_locked=0.99,
        )
        calls = []
        task._recognize_bp_name_roi = lambda *args, **kwargs: calls.append(1)

        for _ in range(3):
            self.assertFalse(task._observe_bp_reveal_pair_frame())

        self.assertEqual([], calls)
        self.assertEqual((), task._bp_ledger.own_picks)
        self.assertEqual((), task._bp_ledger.opponent_context)

    def test_recommend_reveal_pair_unblocks_a_late_manual_identity(self) -> None:
        task = bare_task(DuelBPMode.RECOMMEND)
        task._bp_manual_tracking_blocked = True
        task._bp_manual_baseline_round = 1
        task._bp_manual_baseline_id = "399"
        task._bp_last_phase_signals = DuelBPPhaseSignals(
            confirm_locked=0.99,
        )
        task._recognize_bp_name_roi = lambda roi, **kwargs: {
            "shikigami_id": (
                "399" if roi == SELECTED_NAME_ROI else "583"
            ),
            "name": "",
            "confidence": 0.95,
            "method": "exact",
            "text": "",
            "consensus": 1,
        }

        for _ in range(3):
            task._observe_bp_reveal_pair_frame()

        self.assertFalse(task._bp_manual_tracking_blocked)
        self.assertIsNone(task._bp_manual_baseline_round)
        self.assertIsNone(task._bp_manual_baseline_id)
        self.assertTrue(task._bp_ledger.ready_for_recommendation)

    def test_unlocked_reveal_name_does_not_reserve_an_unknown_slot(self) -> None:
        task = bare_task(DuelBPMode.RECOMMEND)
        task._bp_ledger.begin_own_pick("399")
        task._bp_ledger.commit_pending_own_pick()
        task._bp_last_phase_signals = DuelBPPhaseSignals(
            confirm_locked=0.99,
            opponent_selecting=0.99,
        )
        calls = []
        task._recognize_bp_name_roi = lambda *args, **kwargs: calls.append(1)

        self.assertFalse(task._observe_bp_reveal_pair_frame())
        self.assertEqual([], calls)
        self.assertEqual((), task._bp_ledger.opponent_context)

    def test_opponent_locked_text_blocks_reveal_name_ocr(self) -> None:
        task = bare_task(DuelBPMode.RECOMMEND)
        task._bp_ledger.begin_own_pick("399")
        task._bp_ledger.commit_pending_own_pick()
        task._bp_last_phase_signals = DuelBPPhaseSignals(
            confirm_locked=0.99,
            opponent_locked=0.99,
        )
        calls = []
        task._recognize_bp_name_roi = lambda *args, **kwargs: calls.append(1)

        self.assertFalse(task._observe_bp_reveal_pair_frame())
        self.assertEqual([], calls)
        self.assertEqual((), task._bp_ledger.opponent_context)

    def test_auto_verification_failure_tries_next_ranked_candidate(self) -> None:
        task = bare_task(DuelBPMode.AUTO)
        clicked = []
        events = []
        task.publish_bp_live_event = lambda event, data: events.append(
            (event, data)
        )
        task.device = SimpleNamespace(
            click=lambda **kwargs: clicked.append(kwargs)
        )
        task.screenshot = lambda: None
        task.click = lambda rule, interval=None: clicked.append(
            {"control_name": "confirm"}
        )
        first = DuelRecommendation(
            "596", RecommendationSource.RULE, 1.0, 1.0
        )
        second = DuelRecommendation(
            "566", RecommendationSource.PERSONAL, 0.9, 1.0
        )
        visible = {
            "596": VisibleCandidate(1, "596", "神无月", 1.0),
            "566": VisibleCandidate(2, "566", "晨晖惠比寿", 1.0),
        }
        task._scan_ranked_bp_candidate = lambda remaining: (
            visible[remaining[0].shikigami_id],
            1,
        )
        task._recognize_bp_name_roi = lambda roi, **kwargs: {
            "shikigami_id": (
                task._bp_ledger.pending_own_pick
                or ("596" if not clicked else "566")
            ),
            "name": "",
            "confidence": 1.0,
            "text": "",
        }
        task._recognize_bp_name_stable = (
            lambda roi, expected_id=None, **kwargs: (
                None
                if expected_id == "596"
                else {"confidence": 1.0}
            )
        )
        task._verify_bp_confirm_transition = lambda: True

        with patch("tasks.Duel.script_task.sleep", lambda _: None):
            self.assertTrue(
                task.execute_bp_recommendations((first, second))
            )

        self.assertEqual(("566",), task._bp_ledger.own_picks)
        self.assertIn("596", task._bp_ledger.unavailable_ids)
        self.assertEqual(3, len(clicked))
        success = [
            data
            for event, data in events
            if event == "action" and data["status"] == "success"
        ][-1]
        self.assertEqual(1, success["round"])

    def test_auto_rechecks_action_window_after_scan_before_card_click(
        self,
    ) -> None:
        task = bare_task(DuelBPMode.AUTO)
        clicked = []
        checks = iter((True, False))
        task._bp_actionable_self_pick = lambda: next(checks)
        task.device = SimpleNamespace(
            click=lambda **kwargs: clicked.append(kwargs)
        )
        recommendation = DuelRecommendation(
            "596", RecommendationSource.RULE, 1.0, 1.0
        )
        visible = VisibleCandidate(
            1,
            "596",
            "神无月",
            0.995,
            source="portrait",
        )
        task._scan_ranked_bp_candidate = lambda remaining: (visible, 1)

        self.assertTrue(
            task.execute_bp_recommendations((recommendation,))
        )

        self.assertEqual([], clicked)
        self.assertIsNone(task._bp_ledger.pending_own_pick)

    def test_auto_rechecks_fresh_action_window_after_card_ocr(self) -> None:
        task = bare_task(DuelBPMode.AUTO)
        clicked = []
        checks = iter((True, True, False))
        task._bp_actionable_self_pick = lambda: next(checks)
        task.device = SimpleNamespace(
            click=lambda **kwargs: clicked.append(kwargs)
        )
        task.screenshot = lambda: None
        recommendation = DuelRecommendation(
            "596", RecommendationSource.RULE, 1.0, 1.0
        )
        visible = VisibleCandidate(
            1,
            "596",
            "神无月",
            0.995,
            source="portrait",
        )
        task._scan_ranked_bp_candidate = lambda remaining: (visible, 1)
        task._recognize_bp_name_roi = lambda roi, **kwargs: {
            "shikigami_id": "596",
            "name": "神无月",
            "confidence": 1.0,
            "text": "神无月",
        }

        self.assertTrue(
            task.execute_bp_recommendations((recommendation,))
        )

        self.assertEqual([], clicked)
        self.assertIsNone(task._bp_ledger.pending_own_pick)
        self.assertIsNone(task._bp_pending_source)

    def test_auto_stops_candidate_scan_before_swipe_when_window_closes(
        self,
    ) -> None:
        task = bare_task(DuelBPMode.AUTO)
        swipes = []
        checks = iter((True, False))
        task._bp_actionable_self_pick = lambda: next(checks)
        task.device = SimpleNamespace(
            image=np.zeros((720, 1280, 3), dtype=np.uint8),
            swipe=lambda *args, **kwargs: swipes.append((args, kwargs)),
        )
        task.screenshot = lambda: None
        task.conf.duel_config.bp_candidate_swipe_limit = 2
        task._recognize_visible_bp_candidates = lambda: ()
        recommendation = DuelRecommendation(
            "596", RecommendationSource.RULE, 1.0, 1.0
        )

        self.assertTrue(
            task.execute_bp_recommendations((recommendation,))
        )

        self.assertEqual([], swipes)
        self.assertIsNone(task._bp_ledger.pending_own_pick)

    def test_portrait_only_candidate_never_clicks_without_card_name(
        self,
    ) -> None:
        task = bare_task(DuelBPMode.AUTO)
        clicked = []
        task.device = SimpleNamespace(
            click=lambda **kwargs: clicked.append(kwargs)
        )
        task.screenshot = lambda: None
        task.click = lambda rule, interval=None: clicked.append(
            {"control_name": "confirm"}
        )
        recommendation = DuelRecommendation(
            "596", RecommendationSource.RULE, 1.0, 1.0
        )
        visible = VisibleCandidate(
            1,
            "596",
            "神无月",
            0.995,
            source="portrait",
            base_x=160,
        )
        task._scan_ranked_bp_candidate = lambda remaining: (visible, 1)
        task._recognize_bp_name_roi = lambda roi, **kwargs: {
            "shikigami_id": None,
            "name": None,
            "confidence": 0.0,
            "text": "",
        }
        task._recognize_bp_name_stable = (
            lambda roi, expected_id=None, **kwargs: {"confidence": 1.0}
        )
        task._verify_bp_confirm_transition = lambda: True

        with patch("tasks.Duel.script_task.sleep", lambda _: None):
            self.assertTrue(
                task.execute_bp_recommendations((recommendation,))
            )

        self.assertEqual((), task._bp_ledger.own_picks)
        self.assertEqual([], clicked)
        self.assertIsNone(task._bp_ledger.pending_own_pick)

    def test_unverified_pending_pick_never_falls_through_to_legacy(self) -> None:
        task = bare_task(DuelBPMode.AUTO)
        task._bp_ledger.begin_own_pick("596")
        task._bp_pending_source = "auto"
        task.reset_device = lambda status: None
        task._bp_name = lambda value: value
        task.recognize_bp_observation = lambda: BPObservation(
            DuelBPState.SELF_PICK,
            confidence=1.0,
        )

        for _ in range(5):
            self.assertTrue(task.handle_bp_assistant())
        self.assertEqual("596", task._bp_ledger.pending_own_pick)

    def test_opponent_status_cannot_commit_pending_own_pick(self) -> None:
        for status_signal in (
            DuelBPPhaseSignals(opponent_selecting=0.99),
            DuelBPPhaseSignals(opponent_locked=0.99),
        ):
            with self.subTest(status_signal=status_signal):
                task = bare_task(DuelBPMode.AUTO)
                task._bp_ledger.begin_own_pick("399")
                task._bp_pending_source = "auto"
                task._bp_last_phase_signals = status_signal
                task.reset_device = lambda status: None
                task._bp_name = lambda value: str(value)
                task.recognize_bp_observation = lambda: BPObservation(
                    DuelBPState.OPPONENT_PICK,
                    confidence=0.99,
                    own_picks=task._bp_ledger.own_picks,
                    opponent_picks=task._bp_ledger.opponent_context,
                )

                for _ in range(3):
                    self.assertTrue(task.handle_bp_assistant())

                self.assertEqual((), task._bp_ledger.own_picks)
                self.assertEqual("399", task._bp_ledger.pending_own_pick)

    def test_late_phase_without_own_locked_cannot_commit_pending_pick(
        self,
    ) -> None:
        for state in (DuelBPState.READY, DuelBPState.BATTLE):
            with self.subTest(state=state):
                task = bare_task(DuelBPMode.AUTO)
                task._bp_ledger.begin_own_pick("399")
                task._bp_pending_source = "auto"
                task._bp_last_phase_signals = DuelBPPhaseSignals()
                task.reset_device = lambda status: None
                task._bp_name = lambda value: str(value)
                self_pick = BPObservation(
                    DuelBPState.SELF_PICK,
                    confidence=0.99,
                )
                for _ in range(3):
                    task.bp_assistant.state_machine.observe(self_pick)
                task.recognize_bp_observation = lambda: BPObservation(
                    state,
                    confidence=0.99,
                    own_picks=task._bp_ledger.own_picks,
                    opponent_picks=task._bp_ledger.opponent_context,
                )

                for _ in range(3):
                    self.assertTrue(task.handle_bp_assistant())

                self.assertEqual((), task._bp_ledger.own_picks)
                self.assertEqual("399", task._bp_ledger.pending_own_pick)

    def test_recommend_tracks_manual_first_pick_and_advances_round_two(self) -> None:
        task = bare_task(DuelBPMode.RECOMMEND)
        events = []
        clicks = []
        phase = {"state": DuelBPState.SELF_PICK}
        selected = {"id": "111"}
        opponent_click_permissions = []
        task.publish_bp_live_event = lambda event, data: events.append(
            (event, data)
        )
        task.reset_device = lambda status: None
        task._bp_name = lambda value: str(value)
        task.appear = lambda rule: True
        task.device = SimpleNamespace(
            click=lambda **kwargs: clicks.append(kwargs)
        )
        task.recognize_bp_observation = lambda: BPObservation(
            phase["state"],
            confidence=1.0,
            own_picks=task._bp_ledger.own_picks,
            opponent_picks=task._bp_ledger.opponent_context,
        )
        task._recognize_bp_name_roi = lambda roi, **kwargs: {
            "shikigami_id": selected["id"],
            "name": selected["id"],
            "confidence": 1.0 if selected["id"] is not None else 0.0,
            "text": "",
        }
        task.bp_rule_candidates = lambda observation: (
            DuelRecommendationCandidate(
                "596" if not task._bp_ledger.own_picks else "566",
                RecommendationSource.RULE,
                1.0,
            ),
        )

        def observe_opponent(*, allow_click):
            opponent_click_permissions.append(allow_click)
            task._bp_ledger.record_opponent_pick(1, "399")
            task._sync_bp_ledger()
            return True

        task._observe_next_bp_opponent = observe_opponent

        # The initial default highlight establishes a baseline and is never
        # treated as a user selection.
        for _ in range(5):
            self.assertTrue(task.handle_bp_assistant())
        self.assertIsNone(task._bp_ledger.pending_own_pick)
        self.assertEqual((), task._bp_ledger.own_picks)

        # Three stable frames of the user's actual selection replace it.
        selected["id"] = "596"
        for _ in range(3):
            self.assertTrue(task.handle_bp_assistant())
        self.assertEqual("596", task._bp_ledger.pending_own_pick)

        phase["state"] = DuelBPState.OPPONENT_PICK
        task._bp_last_phase_signals = DuelBPPhaseSignals(
            confirm_locked=0.99,
            opponent_selecting=0.99,
        )
        for _ in range(3):
            self.assertTrue(task.handle_bp_assistant())
        self.assertEqual(("596",), task._bp_ledger.own_picks)
        self.assertIsNone(task._bp_ledger.pending_own_pick)

        # The next stable self turn records opponent round one without an
        # inspection click, then recalculates a round-two recommendation.
        selected["id"] = None
        phase["state"] = DuelBPState.SELF_PICK
        for _ in range(6):
            self.assertTrue(task.handle_bp_assistant())

        self.assertEqual(("399",), task._bp_ledger.opponent_context)
        self.assertEqual([False], opponent_click_permissions)
        self.assertEqual([], clicks)
        round_two = [
            data
            for event, data in events
            if event == "recommendation" and data["target_round"] == 2
        ]
        self.assertTrue(round_two)
        self.assertEqual(566, round_two[-1]["shishen_id"])

    def test_recommend_dropped_name_frame_does_not_commit_pending(self) -> None:
        task = bare_task(DuelBPMode.RECOMMEND)
        phase = {"state": DuelBPState.SELF_PICK}
        selected = {"id": "111"}
        task.reset_device = lambda status: None
        task._bp_name = lambda value: str(value)
        task.appear = lambda rule: True
        task.recognize_bp_observation = lambda: BPObservation(
            phase["state"],
            confidence=1.0,
            own_picks=task._bp_ledger.own_picks,
            opponent_picks=task._bp_ledger.opponent_context,
        )
        task._recognize_bp_name_roi = lambda roi, **kwargs: {
            "shikigami_id": selected["id"],
            "name": selected["id"],
            "confidence": 1.0 if selected["id"] is not None else 0.0,
            "text": "",
        }

        for _ in range(5):
            self.assertTrue(task.handle_bp_assistant())
        self.assertIsNone(task._bp_ledger.pending_own_pick)

        selected["id"] = "596"
        for _ in range(3):
            self.assertTrue(task.handle_bp_assistant())
        self.assertEqual("596", task._bp_ledger.pending_own_pick)

        selected["id"] = None
        self.assertTrue(task.handle_bp_assistant())
        phase["state"] = DuelBPState.OPPONENT_PICK
        task._bp_last_phase_signals = DuelBPPhaseSignals(
            confirm_locked=0.99,
            opponent_selecting=0.99,
        )
        for _ in range(3):
            self.assertTrue(task.handle_bp_assistant())

        self.assertEqual((), task._bp_ledger.own_picks)
        self.assertIsNone(task._bp_ledger.pending_own_pick)

    def test_recommend_default_highlight_confirmation_fails_closed(self) -> None:
        task = bare_task(DuelBPMode.RECOMMEND)
        phase = {"state": DuelBPState.SELF_PICK}
        task.reset_device = lambda status: None
        task._bp_name = lambda value: str(value)
        task.appear = lambda rule: True
        task.recognize_bp_observation = lambda: BPObservation(
            phase["state"],
            confidence=1.0,
            own_picks=task._bp_ledger.own_picks,
            opponent_picks=task._bp_ledger.opponent_context,
        )
        task._recognize_bp_name_roi = lambda roi, **kwargs: {
            "shikigami_id": "111",
            "name": "111",
            "confidence": 1.0,
            "text": "",
        }

        for _ in range(5):
            self.assertTrue(task.handle_bp_assistant())
        self.assertIsNone(task._bp_ledger.pending_own_pick)

        phase["state"] = DuelBPState.OPPONENT_PICK
        task._bp_last_phase_signals = DuelBPPhaseSignals(
            confirm_locked=0.99,
            opponent_selecting=0.99,
        )
        for _ in range(3):
            self.assertTrue(task.handle_bp_assistant())

        self.assertEqual((), task._bp_ledger.own_picks)
        self.assertTrue(task._bp_manual_tracking_blocked)
        phase["state"] = DuelBPState.SELF_PICK
        for _ in range(3):
            self.assertTrue(task.handle_bp_assistant())
        self.assertIsNone(task._bp_ledger.pending_own_pick)

    def test_observe_mode_never_tracks_manual_selection(self) -> None:
        task = bare_task(DuelBPMode.OBSERVE)
        name_reads = []
        task.reset_device = lambda status: None
        task.appear = lambda rule: True
        task.recognize_bp_observation = lambda: BPObservation(
            DuelBPState.SELF_PICK,
            confidence=1.0,
        )
        task._recognize_bp_name_roi = (
            lambda roi, **kwargs: name_reads.append(roi)
        )

        for _ in range(6):
            self.assertTrue(task.handle_bp_assistant())

        self.assertEqual([], name_reads)
        self.assertEqual((), task._bp_ledger.own_picks)
        self.assertIsNone(task._bp_ledger.pending_own_pick)

    def test_fifth_pick_accepts_stable_onmyoji_selection_as_transition(
        self,
    ) -> None:
        task = bare_task(DuelBPMode.AUTO)
        for shikigami_id in ("1", "2", "3", "4"):
            task._bp_ledger.begin_own_pick(shikigami_id)
            task._bp_ledger.commit_pending_own_pick()
            task._bp_ledger.record_opponent_pick(
                len(task._bp_ledger.own_picks), None
            )
        task._bp_ledger.begin_own_pick("5")
        task.screenshot = lambda: None
        task.appear = lambda rule: True

        def recognize_onmyoji():
            task._bp_seen_onmyoji_selection = True
            return BPObservation(DuelBPState.SELF_PICK, confidence=1.0)

        task.recognize_bp_observation = recognize_onmyoji
        with patch("tasks.Duel.script_task.sleep", lambda _: None):
            self.assertTrue(task._verify_bp_confirm_transition())

    def test_onmyoji_signal_is_context_gated_and_three_frame_stable(
        self,
    ) -> None:
        task = bare_task(DuelBPMode.AUTO)
        self.assertEqual(0.0, task._stable_bp_onmyoji_signal(0.95))
        for shikigami_id in ("1", "2", "3", "4"):
            task._bp_ledger.begin_own_pick(shikigami_id)
            task._bp_ledger.commit_pending_own_pick()
            task._bp_ledger.record_opponent_pick(
                len(task._bp_ledger.own_picks), None
            )
        task._bp_ledger.begin_own_pick("5")

        self.assertEqual(0.0, task._stable_bp_onmyoji_signal(0.95))
        self.assertEqual(0.0, task._stable_bp_onmyoji_signal(0.95))
        self.assertEqual(0.95, task._stable_bp_onmyoji_signal(0.95))

    def test_onmyoji_auto_rechecks_action_window_after_fixed_slot_click(
        self,
    ) -> None:
        task = bare_task(DuelBPMode.AUTO)
        complete_bp_ledger(task)
        task._bp_seen_onmyoji_selection = True
        roster_clicks = []
        confirms = []
        task.device = SimpleNamespace(
            click=lambda **kwargs: roster_clicks.append(kwargs)
        )
        task.screenshot = lambda: None
        task.appear = lambda rule: True
        task.click = lambda *args, **kwargs: confirms.append(
            (args, kwargs)
        )
        windows = iter((True, False))
        task._bp_actionable_self_pick = lambda: next(windows)
        task._load_bp_onmyoji_identity_templates = lambda: self.fail(
            "fixed-slot Onmyoji selection must not load identity templates"
        )
        task._verify_bp_onmyoji_identity_frame = lambda *args: self.fail(
            "fixed-slot Onmyoji selection must not compare identity templates"
        )

        with patch("tasks.Duel.script_task.sleep", lambda _: None):
            self.assertTrue(task.execute_bp_onmyoji_selection())

        self.assertEqual(1, len(roster_clicks))
        self.assertEqual([], confirms)
        self.assertFalse(task._bp_onmyoji_action_issued)
        self.assertEqual(1, task._bp_onmyoji_attempts)

    def test_onmyoji_auto_confirms_fixed_slot_without_identity_templates(
        self,
    ) -> None:
        task = bare_task(DuelBPMode.AUTO)
        complete_bp_ledger(task)
        task._bp_seen_onmyoji_selection = True
        roster_clicks = []
        confirms = []
        events = []
        task.device = SimpleNamespace(
            click=lambda **kwargs: roster_clicks.append(kwargs)
        )
        task.screenshot = lambda: None
        task.appear = lambda rule: True
        task.click = lambda *args, **kwargs: confirms.append(
            (args, kwargs)
        )
        task.publish_bp_live_event = lambda event, data: events.append(
            (event, data)
        )
        windows = []
        task._bp_actionable_self_pick = lambda: windows.append(True) or True
        task._load_bp_onmyoji_identity_templates = lambda: self.fail(
            "fixed-slot Onmyoji selection must not load identity templates"
        )
        task._verify_bp_onmyoji_identity_frame = lambda *args: self.fail(
            "fixed-slot Onmyoji selection must not compare identity templates"
        )
        task._verify_bp_confirm_transition = lambda: True

        with patch("tasks.Duel.script_task.sleep", lambda _: None):
            self.assertTrue(task.execute_bp_onmyoji_selection())

        self.assertEqual(1, len(roster_clicks))
        self.assertEqual(1, len(confirms))
        self.assertEqual(2, len(windows))
        self.assertTrue(task._bp_onmyoji_action_issued)
        self.assertEqual("源赖光", task._bp_selected_onmyoji)
        self.assertEqual(6, events[-1][1]["round"])
        self.assertEqual("success", events[-1][1]["status"])
        self.assertEqual(
            "fixed_slot",
            events[-1][1]["selection_method"],
        )
        self.assertNotIn("selected_verified", events[-1][1])

    def test_onmyoji_auto_rechecks_action_window_before_roster_click(
        self,
    ) -> None:
        task = bare_task(DuelBPMode.AUTO)
        complete_bp_ledger(task)
        task._bp_seen_onmyoji_selection = True
        clicks = []
        task.device = SimpleNamespace(
            click=lambda **kwargs: clicks.append(kwargs)
        )
        task.screenshot = lambda: None
        task.appear = lambda rule: True
        task._bp_actionable_self_pick = lambda: False
        task._load_bp_onmyoji_identity_templates = lambda: self.fail(
            "action-window loss must be checked before template I/O"
        )

        self.assertTrue(task.execute_bp_onmyoji_selection())

        self.assertEqual([], clicks)
        self.assertEqual(0, task._bp_onmyoji_attempts)
        self.assertFalse(task._bp_onmyoji_action_issued)

    def test_delayed_fifth_pick_commits_on_stable_onmyoji_page(self) -> None:
        task = bare_task(DuelBPMode.AUTO)
        events = []
        task.publish_bp_live_event = lambda event, data: events.append(
            (event, data)
        )
        for shikigami_id in ("1", "2", "3", "4"):
            task._bp_ledger.begin_own_pick(shikigami_id)
            task._bp_ledger.commit_pending_own_pick()
            task._bp_ledger.record_opponent_pick(
                len(task._bp_ledger.own_picks), None
            )
        task._bp_ledger.begin_own_pick("5")
        task._bp_pending_source = "auto"
        task.appear = lambda rule: True
        task.reset_device = lambda status: None
        task._bp_name = lambda value: value
        task._sync_bp_ledger = lambda: None
        task.execute_bp_onmyoji_selection = lambda: True
        task._observe_next_bp_opponent = lambda **kwargs: False
        observation = BPObservation(
            DuelBPState.SELF_PICK,
            confidence=1.0,
            own_picks=task._bp_ledger.own_picks,
        )
        for _ in range(3):
            task.bp_assistant.state_machine.observe(observation)

        def recognize_onmyoji():
            if task._stable_bp_onmyoji_signal(0.95) > 0:
                task._bp_seen_onmyoji_selection = True
            return observation

        task.recognize_bp_observation = recognize_onmyoji

        self.assertTrue(task.handle_bp_assistant())
        self.assertEqual("5", task._bp_ledger.pending_own_pick)
        self.assertTrue(task.handle_bp_assistant())
        self.assertEqual("5", task._bp_ledger.pending_own_pick)
        self.assertTrue(task.handle_bp_assistant())

        self.assertEqual(("1", "2", "3", "4", "5"), task._bp_ledger.own_picks)
        self.assertIsNone(task._bp_ledger.pending_own_pick)
        success = [
            data
            for event, data in events
            if event == "action" and data["status"] == "success"
        ][-1]
        self.assertEqual(5, success["round"])

    def test_recommend_event_contains_ranked_top_five_contract(self) -> None:
        task = bare_task(DuelBPMode.RECOMMEND)
        events = []
        task.publish_bp_live_event = lambda event, data: events.append(
            (event, data)
        )
        task.reset_device = lambda status: None
        task._bp_name = lambda value: {"596": "神无月"}.get(value, value)
        task.recognize_bp_observation = lambda: BPObservation(
            DuelBPState.SELF_PICK,
            confidence=1.0,
        )
        task.bp_rule_candidates = lambda observation: (
            DuelRecommendationCandidate(
                "596", RecommendationSource.RULE, 1.0
            ),
            DuelRecommendationCandidate(
                "566", RecommendationSource.RULE, 0.9
            ),
        )

        for _ in range(3):
            self.assertTrue(task.handle_bp_assistant())

        recommendation = [
            data for event, data in events if event == "recommendation"
        ][0]
        self.assertEqual(1, recommendation["target_round"])
        self.assertEqual(
            [1, 2],
            [item["rank"] for item in recommendation["recommendations"]],
        )
        self.assertEqual(
            [596, 566],
            [
                item["shishen_id"]
                for item in recommendation["recommendations"]
            ],
        )

    def test_all_candidate_sources_are_filtered_by_pool_and_own_picks(
        self,
    ) -> None:
        task = bare_task(DuelBPMode.RECOMMEND)
        task.conf.duel_config.bp_shishen_pool = [596, 566]

        class Provider:
            def get_candidates(self, **kwargs):
                return {
                    "rules": [
                        {"shikigami_id": "999", "source": "rule", "score": 1},
                        {"shikigami_id": "596", "source": "rule", "score": 0.9},
                        {"shikigami_id": "566", "source": "rule", "score": 0.8},
                    ],
                    "personal": [
                        {"shikigami_id": "999", "source": "personal", "score": 1},
                        {"shikigami_id": "566", "source": "personal", "score": 0.8},
                    ],
                    "external": [
                        {"shikigami_id": "999", "source": "external", "score": 1},
                        {"shikigami_id": "596", "source": "external", "score": 0.9},
                    ],
                }

        task._bp_candidate_provider = Provider()
        groups = task._bp_candidate_groups(
            BPObservation(
                DuelBPState.SELF_PICK,
                confidence=1.0,
                own_picks=("596",),
            )
        )

        self.assertEqual(
            ("566",),
            tuple(item.shikigami_id for item in groups["rules"]),
        )
        self.assertEqual(
            ("566",),
            tuple(item.shikigami_id for item in groups["personal"]),
        )
        self.assertEqual((), groups["external"])

    def test_outside_pool_recommendations_preserve_auto_entry_fallback(
        self,
    ) -> None:
        task = bare_task(DuelBPMode.AUTO)
        task.conf.duel_config.bp_shishen_pool = [596]
        task._bp_actionable_self_pick = lambda: self.fail(
            "outside-pool recommendations must not enter candidate scanning"
        )

        executed = task.execute_bp_recommendations(
            (
                DuelRecommendation(
                    "999",
                    RecommendationSource.RULE,
                    1.0,
                    1.0,
                ),
            )
        )

        self.assertFalse(executed)

    def test_reveal_context_change_suppresses_pre_update_recommendation(self) -> None:
        task = bare_task(DuelBPMode.RECOMMEND)
        events = []
        task.publish_bp_live_event = lambda event, data: events.append(
            (event, data)
        )
        task.reset_device = lambda status: None
        task._track_recommend_manual_selection = lambda *args: None
        task._observe_bp_reveal_pair_frame = lambda: True
        task._finalize_bp_reveal_pair_if_ready = lambda: False
        observation = BPObservation(
            DuelBPState.SELF_PICK,
            confidence=1.0,
        )
        task.recognize_bp_observation = lambda: observation
        task.bp_rule_candidates = lambda current: (
            DuelRecommendationCandidate(
                "596",
                RecommendationSource.RULE,
                1.0,
            ),
        )
        for _ in range(3):
            task.bp_assistant.state_machine.observe(observation)

        self.assertTrue(task.handle_bp_assistant())

        self.assertEqual(
            [],
            [data for event, data in events if event == "recommendation"],
        )

    def test_auto_waits_one_frame_after_reveal_context_change(self) -> None:
        task = bare_task(DuelBPMode.AUTO)
        task.reset_device = lambda status: None
        task._observe_bp_reveal_pair_frame = lambda: True
        task._finalize_bp_reveal_pair_if_ready = lambda: False
        observation = BPObservation(
            DuelBPState.SELF_PICK,
            confidence=1.0,
        )
        task.recognize_bp_observation = lambda: observation
        task.bp_rule_candidates = lambda current: (
            DuelRecommendationCandidate(
                "596",
                RecommendationSource.RULE,
                1.0,
            ),
        )
        actions = []
        task.execute_bp_recommendations = lambda values: actions.append(values)
        for _ in range(3):
            task.bp_assistant.state_machine.observe(observation)

        self.assertTrue(task.handle_bp_assistant())

        self.assertEqual([], actions)

    def test_visible_candidate_locator_never_calls_portrait_matcher(
        self,
    ) -> None:
        task = bare_task(DuelBPMode.AUTO)
        task.device = SimpleNamespace(
            image=np.zeros((720, 1280, 3), dtype=np.uint8)
        )
        portrait_initializations = []
        task._ensure_bp_portrait_matcher = lambda: (
            portrait_initializations.append(True)
        )
        reads = iter(
            [
                {
                    "shikigami_id": "596",
                    "name": "神无月",
                    "confidence": 0.995,
                    "text": "神无月",
                    "method": "exact",
                    "consensus": 1,
                },
                *(
                    {
                        "shikigami_id": None,
                        "name": None,
                        "confidence": 0.0,
                        "text": "",
                    }
                    for _ in range(7)
                ),
            ]
        )
        task._recognize_bp_name_roi = lambda roi, **kwargs: next(reads)

        with patch(
            "tasks.Duel.script_task.detect_candidate_base_x",
            return_value=183,
        ):
            candidates = task._recognize_visible_bp_candidates()

        self.assertEqual(1, len(candidates))
        self.assertEqual("596", candidates[0].shikigami_id)
        self.assertEqual("name", candidates[0].source)
        self.assertEqual([], portrait_initializations)

    def test_visible_candidate_accepts_exact_name_for_reversible_location(
        self,
    ) -> None:
        task = bare_task(DuelBPMode.AUTO)
        task.device = SimpleNamespace(
            image=np.zeros((720, 1280, 3), dtype=np.uint8)
        )
        reads = iter(
            [
                {
                    "shikigami_id": "596",
                    "name": "神无月",
                    "confidence": 0.65,
                    "text": "神无月",
                    "method": "exact",
                    "consensus": 1,
                },
                *(
                    {
                        "shikigami_id": None,
                        "name": None,
                        "confidence": 0.0,
                        "text": "",
                        "method": "unresolved",
                        "consensus": 0,
                    }
                    for _ in range(7)
                ),
            ]
        )
        task._recognize_bp_name_roi = lambda roi, **kwargs: next(reads)

        with patch(
            "tasks.Duel.script_task.detect_candidate_base_x",
            return_value=183,
        ):
            candidates = task._recognize_visible_bp_candidates()

        self.assertEqual(1, len(candidates))
        self.assertEqual("596", candidates[0].shikigami_id)
        self.assertEqual(0.65, candidates[0].confidence)

    def test_visible_candidate_rejects_fuzzy_name_below_auto_confidence(
        self,
    ) -> None:
        task = bare_task(DuelBPMode.AUTO)
        task.device = SimpleNamespace(
            image=np.zeros((720, 1280, 3), dtype=np.uint8)
        )
        task._ensure_bp_portrait_matcher = lambda: None
        task._bp_portrait_matcher = None
        task._recognize_bp_name_roi = lambda roi, **kwargs: {
            "shikigami_id": "596",
            "name": "神无月",
            "confidence": 0.97,
            "text": "神无月",
            "method": "fuzzy",
            "consensus": 2,
        }

        with patch(
            "tasks.Duel.script_task.detect_candidate_base_x",
            return_value=183,
        ):
            candidates = task._recognize_visible_bp_candidates()

        self.assertEqual((), candidates)

    def test_candidate_scan_fingerprint_includes_current_raw_frame(self) -> None:
        task = bare_task(DuelBPMode.AUTO)
        frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        task.device = SimpleNamespace(image=frame)
        task.conf = SimpleNamespace(
            duel_config=SimpleNamespace(bp_candidate_swipe_limit=0)
        )
        task._recognize_visible_bp_candidates = lambda: ()
        recommendation = DuelRecommendation(
            "596",
            RecommendationSource.RULE,
            1.0,
            1.0,
        )

        with patch(
            "tasks.Duel.script_task.candidate_page_fingerprint",
            return_value=((), "visual"),
        ) as fingerprint:
            self.assertIsNone(
                task._scan_ranked_bp_candidate((recommendation,))
            )

        fingerprint.assert_called_once()
        self.assertEqual((), fingerprint.call_args.args[0])
        self.assertIs(frame, fingerprint.call_args.args[1])

    def test_candidate_scan_uses_visible_ranked_fallback_without_swiping(
        self,
    ) -> None:
        task = bare_task(DuelBPMode.AUTO)
        swipes = []
        task.device = SimpleNamespace(
            image=np.zeros((720, 1280, 3), dtype=np.uint8),
            swipe=lambda *args, **kwargs: swipes.append((args, kwargs)),
        )
        task.conf = SimpleNamespace(
            duel_config=SimpleNamespace(
                bp_candidate_swipe_limit=12,
                bp_candidate_scan_budget=5.0,
            )
        )
        task._recognize_visible_bp_candidates = lambda: (
            VisibleCandidate(1, "second", "second", 0.99),
        )
        recommendations = (
            DuelRecommendation(
                "first", RecommendationSource.RULE, 1.0, 1.0
            ),
            DuelRecommendation(
                "second", RecommendationSource.RULE, 0.9, 1.0
            ),
        )

        selected, rank = task._scan_ranked_bp_candidate(recommendations)

        self.assertEqual("second", selected.shikigami_id)
        self.assertEqual(2, rank)
        self.assertEqual([], swipes)

    def test_candidate_scan_deadline_stops_before_another_swipe(self) -> None:
        task = bare_task(DuelBPMode.AUTO)
        swipes = []
        task.device = SimpleNamespace(
            image=np.zeros((720, 1280, 3), dtype=np.uint8),
            swipe=lambda *args, **kwargs: swipes.append((args, kwargs)),
        )
        task.conf = SimpleNamespace(
            duel_config=SimpleNamespace(
                bp_candidate_swipe_limit=12,
                bp_candidate_scan_budget=5.0,
            )
        )
        task._recognize_visible_bp_candidates = lambda: ()
        recommendation = DuelRecommendation(
            "596", RecommendationSource.RULE, 1.0, 1.0
        )

        with patch(
            "tasks.Duel.script_task.monotonic",
            side_effect=(0.0, 6.0),
        ):
            self.assertIsNone(
                task._scan_ranked_bp_candidate((recommendation,))
            )

        self.assertEqual([], swipes)


if __name__ == "__main__":
    unittest.main()
