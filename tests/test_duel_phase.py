from __future__ import annotations

import unittest

from tasks.Duel.bp import DuelBPState, build_pick_payload
from tasks.Duel.phase import DuelBPPhaseSignals, classify_bp_phase


class DuelBPPhaseClassifierTest(unittest.TestCase):
    def classify(
        self,
        signals: DuelBPPhaseSignals,
        *,
        previous: DuelBPState = DuelBPState.OPPONENT_PICK,
        seen_onmyoji: bool = False,
    ):
        return classify_bp_phase(
            signals,
            previous_state=previous,
            seen_onmyoji_selection=seen_onmyoji,
        )

    def test_active_confirm_owns_turn_regardless_of_opponent_text(self) -> None:
        for opponent_signal in (
            {"opponent_selecting": 0.99},
            {"opponent_locked": 0.99},
        ):
            with self.subTest(opponent_signal=opponent_signal):
                result = self.classify(
                    DuelBPPhaseSignals(
                        confirm_active=0.99,
                        **opponent_signal,
                    )
                )
                self.assertEqual(DuelBPState.SELF_PICK, result.state)

    def test_opponent_text_without_own_control_has_no_phase_authority(
        self,
    ) -> None:
        for opponent_signal in (
            {"opponent_selecting": 0.99},
            {"opponent_locked": 0.99},
        ):
            with self.subTest(opponent_signal=opponent_signal):
                result = self.classify(
                    DuelBPPhaseSignals(**opponent_signal),
                    previous=DuelBPState.SELF_PICK,
                )
                self.assertIsNone(result.state)
                self.assertEqual(0.0, result.confidence)

    def test_opponent_text_prevents_ready_after_onmyoji_selection(
        self,
    ) -> None:
        for opponent_signal in (
            {"opponent_selecting": 0.99},
            {"opponent_locked": 0.99},
        ):
            with self.subTest(opponent_signal=opponent_signal):
                result = self.classify(
                    DuelBPPhaseSignals(**opponent_signal),
                    previous=DuelBPState.OPPONENT_PICK,
                    seen_onmyoji=True,
                )
                self.assertIsNone(result.state)
                self.assertNotEqual(DuelBPState.READY, result.state)

    def test_overlays_do_not_inflate_confirm_control_confidence(self) -> None:
        active = self.classify(
            DuelBPPhaseSignals(
                confirm_active=0.91,
                opponent_selecting=0.99,
            )
        )
        locked = self.classify(
            DuelBPPhaseSignals(
                confirm_locked=0.92,
                opponent_locked=0.995,
            )
        )
        self.assertEqual(0.91, active.confidence)
        self.assertEqual(0.92, locked.confidence)

    def test_locked_confirm_is_non_action_even_during_reveal(self) -> None:
        result = self.classify(
            DuelBPPhaseSignals(confirm_locked=0.99)
        )
        self.assertEqual(DuelBPState.OPPONENT_PICK, result.state)

    def test_legacy_prepare_button_never_authorizes_self_pick(self) -> None:
        result = self.classify(
            DuelBPPhaseSignals(legacy_action=0.999)
        )
        self.assertEqual(DuelBPState.OPPONENT_PICK, result.state)
        self.assertEqual(
            "selection_without_active_confirm",
            result.reason,
        )

    def test_onmyoji_is_round_six_selection_not_ready(self) -> None:
        active = self.classify(
            DuelBPPhaseSignals(
                confirm_active=0.99,
                onmyoji_selection=0.99,
            )
        )
        locked = self.classify(
            DuelBPPhaseSignals(
                confirm_locked=0.99,
                onmyoji_selection=0.99,
            )
        )
        self.assertEqual(DuelBPState.SELF_PICK, active.state)
        self.assertEqual(DuelBPState.OPPONENT_PICK, locked.state)
        self.assertTrue(active.seen_onmyoji_selection)
        self.assertTrue(locked.seen_onmyoji_selection)

    def test_ready_requires_round_six_and_selection_controls_to_end(self) -> None:
        too_early = self.classify(
            DuelBPPhaseSignals(lineup_reveal=0.99),
            seen_onmyoji=False,
        )
        still_selecting = self.classify(
            DuelBPPhaseSignals(
                confirm_locked=0.99,
                onmyoji_selection=0.99,
            ),
            seen_onmyoji=True,
        )
        controls_gone = self.classify(
            DuelBPPhaseSignals(),
            seen_onmyoji=True,
        )
        revealed = self.classify(
            DuelBPPhaseSignals(lineup_reveal=0.99),
            seen_onmyoji=True,
        )
        self.assertIsNone(too_early.state)
        self.assertEqual(DuelBPState.OPPONENT_PICK, still_selecting.state)
        self.assertEqual(DuelBPState.READY, controls_gone.state)
        self.assertEqual(DuelBPState.READY, revealed.state)

    def test_battle_and_result_override_stale_selection_controls(self) -> None:
        battle = self.classify(
            DuelBPPhaseSignals(battle=0.99, confirm_locked=0.99),
            seen_onmyoji=True,
        )
        result = self.classify(
            DuelBPPhaseSignals(result=0.99, battle=0.99),
            seen_onmyoji=True,
        )
        self.assertEqual(DuelBPState.BATTLE, battle.state)
        self.assertEqual(DuelBPState.RESULT, result.state)

    def test_portrait_count_is_not_a_phase_signal(self) -> None:
        # Five pre-filled portraits deliberately have no input in the phase
        # contract and therefore cannot terminate BP.
        result = self.classify(DuelBPPhaseSignals())
        self.assertIsNone(result.state)


class DuelPickPayloadTest(unittest.TestCase):
    def test_repeated_identity_uses_occurrence_ordinal_per_side(self) -> None:
        payload = build_pick_payload(
            ("586", "586", "303"),
            ("586", "586"),
        )
        self.assertEqual([1, 2, 1, 1, 2], [item["count"] for item in payload])
        self.assertEqual(
            ["self", "self", "self", "opponent", "opponent"],
            [item["side"] for item in payload],
        )


if __name__ == "__main__":
    unittest.main()
