import unittest

from tasks.Duel.bp import (
    BPObservation,
    DuelBPAssistant,
    DuelBPMode,
    DuelBPState,
    DuelBPStateMachine,
    DuelRecommendationCandidate,
    DuelRecommendationEngine,
    RecommendationSource,
)
from tasks.Duel.config import DuelConfig


def observation(
    state: DuelBPState,
    confidence: float = 1.0,
    own_picks: tuple[str, ...] = (),
) -> BPObservation:
    return BPObservation(
        state=state,
        confidence=confidence,
        own_picks=own_picks,
    )


def candidate(
    shikigami_id: str,
    source: RecommendationSource,
    score: float,
    *,
    confidence: float = 1.0,
    priority: int = 0,
    sample_size: int = 0,
) -> DuelRecommendationCandidate:
    return DuelRecommendationCandidate(
        shikigami_id=shikigami_id,
        source=source,
        score=score,
        confidence=confidence,
        priority=priority,
        sample_size=sample_size,
    )


class DuelBPConfigTest(unittest.TestCase):
    def test_mode_defaults_to_off_and_accepts_public_string_values(self) -> None:
        self.assertEqual(DuelConfig().bp_mode, DuelBPMode.OFF)
        for mode in DuelBPMode:
            with self.subTest(mode=mode):
                self.assertEqual(DuelConfig(bp_mode=mode.value).bp_mode, mode)


class DuelBPStateMachineTest(unittest.TestCase):
    def test_requires_three_identical_frames_for_every_transition(self) -> None:
        machine = DuelBPStateMachine(stable_frames=3)
        sequence = (
            DuelBPState.SELF_PICK,
            DuelBPState.OPPONENT_PICK,
            DuelBPState.READY,
            DuelBPState.BATTLE,
            DuelBPState.RESULT,
        )

        for state in sequence:
            with self.subTest(state=state):
                first = machine.observe(observation(state))
                second = machine.observe(observation(state))
                third = machine.observe(observation(state))
                self.assertFalse(first.accepted)
                self.assertFalse(second.accepted)
                self.assertTrue(third.accepted)
                self.assertTrue(third.transitioned)
                self.assertEqual(machine.state, state)

    def test_changed_pick_resets_stability_window(self) -> None:
        machine = DuelBPStateMachine(stable_frames=3)
        machine.observe(observation(DuelBPState.SELF_PICK, own_picks=("a",)))
        update = machine.observe(
            observation(DuelBPState.SELF_PICK, own_picks=("b",))
        )
        self.assertEqual(update.stable_frames, 1)
        self.assertFalse(update.is_stable)

    def test_self_and_opponent_pick_can_alternate_for_multiple_rounds(self) -> None:
        machine = DuelBPStateMachine(stable_frames=3)
        states = (
            DuelBPState.SELF_PICK,
            DuelBPState.OPPONENT_PICK,
            DuelBPState.SELF_PICK,
            DuelBPState.OPPONENT_PICK,
            DuelBPState.READY,
        )
        for state in states:
            for _ in range(3):
                update = machine.observe(observation(state))
            self.assertTrue(update.accepted)
            self.assertEqual(machine.state, state)

    def test_duplicate_shikigami_count_is_part_of_stability_fingerprint(self) -> None:
        machine = DuelBPStateMachine(stable_frames=3)
        machine.observe(
            observation(DuelBPState.SELF_PICK, own_picks=("same",))
        )
        update = machine.observe(
            observation(
                DuelBPState.SELF_PICK, own_picks=("same", "same")
            )
        )
        self.assertEqual(update.stable_frames, 1)
        self.assertFalse(update.is_stable)

    def test_legal_skip_and_missing_result_reset(self) -> None:
        machine = DuelBPStateMachine(stable_frames=1)
        # A player may enter on the opponent's turn and may go straight to
        # ready when no further self pick is needed.
        self.assertTrue(
            machine.observe(observation(DuelBPState.OPPONENT_PICK)).accepted
        )
        self.assertTrue(machine.observe(observation(DuelBPState.READY)).accepted)
        self.assertTrue(machine.observe(observation(DuelBPState.BATTLE)).accepted)

        missing_result = machine.observe(observation(DuelBPState.BAN))
        self.assertFalse(missing_result.accepted)
        self.assertEqual(machine.state, DuelBPState.BATTLE)
        machine.reset()
        self.assertEqual(machine.state, DuelBPState.BAN)

    def test_low_confidence_and_skipped_state_are_rejected(self) -> None:
        machine = DuelBPStateMachine(stable_frames=3)
        for _ in range(3):
            low = machine.observe(
                observation(DuelBPState.SELF_PICK, confidence=0.89)
            )
        self.assertEqual(low.reason, "recognition_confidence_below_threshold")
        self.assertEqual(machine.state, DuelBPState.BAN)

        for _ in range(3):
            skipped = machine.observe(observation(DuelBPState.READY))
        self.assertFalse(skipped.accepted)
        self.assertEqual(skipped.reason, "unexpected_state_transition")
        self.assertEqual(machine.state, DuelBPState.BAN)


class DuelRecommendationEngineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = DuelRecommendationEngine(personal_min_samples=20)
        self.frame = observation(DuelBPState.SELF_PICK)

    def test_rules_beat_personal_and_external_sources(self) -> None:
        result = self.engine.recommend(
            self.frame,
            rule_candidates=(candidate("rule", RecommendationSource.RULE, 0.1),),
            personal_candidates=(
                candidate(
                    "personal",
                    RecommendationSource.PERSONAL,
                    0.99,
                    sample_size=100,
                ),
            ),
            external_candidates=(
                candidate("external", RecommendationSource.EXTERNAL, 1.0),
            ),
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.shikigami_id, "rule")
        self.assertEqual(result.source, RecommendationSource.RULE)

    def test_qualified_personal_stats_rerank_only_top_rule_tier(self) -> None:
        rules = (
            candidate("a", RecommendationSource.RULE, 0.9, priority=0),
            candidate("b", RecommendationSource.RULE, 0.8, priority=0),
            candidate("c", RecommendationSource.RULE, 1.0, priority=1),
        )
        personal = (
            candidate(
                "a", RecommendationSource.PERSONAL, 0.95, sample_size=19
            ),
            candidate(
                "b", RecommendationSource.PERSONAL, 0.70, sample_size=20
            ),
            candidate(
                "c", RecommendationSource.PERSONAL, 1.0, sample_size=100
            ),
        )
        result = self.engine.recommend(
            self.frame,
            rule_candidates=rules,
            personal_candidates=personal,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.shikigami_id, "b")
        self.assertEqual(
            result.evidence_sources,
            (RecommendationSource.RULE, RecommendationSource.PERSONAL),
        )

    def test_personal_requires_twenty_samples_then_external_is_fallback(self) -> None:
        result = self.engine.recommend(
            self.frame,
            personal_candidates=(
                candidate(
                    "personal",
                    RecommendationSource.PERSONAL,
                    1.0,
                    sample_size=19,
                ),
            ),
            external_candidates=(
                candidate("external", RecommendationSource.EXTERNAL, 0.5),
            ),
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.shikigami_id, "external")

    def test_banned_or_already_picked_candidates_are_filtered(self) -> None:
        frame = BPObservation(
            DuelBPState.SELF_PICK,
            confidence=1.0,
            own_picks=("owned",),
            bans=("banned",),
        )
        result = self.engine.recommend(
            frame,
            rule_candidates=(
                candidate("owned", RecommendationSource.RULE, 1.0),
                candidate("banned", RecommendationSource.RULE, 0.9),
                candidate("available", RecommendationSource.RULE, 0.1),
            ),
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.shikigami_id, "available")

    def test_opponent_mirror_pick_remains_available(self) -> None:
        frame = BPObservation(
            DuelBPState.SELF_PICK,
            confidence=1.0,
            opponent_picks=("mirror",),
        )
        result = self.engine.recommend(
            frame,
            rule_candidates=(
                candidate("mirror", RecommendationSource.RULE, 1.0),
            ),
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.shikigami_id, "mirror")


class DuelBPAssistantTest(unittest.TestCase):
    def test_auto_waits_three_frames_then_allows_high_confidence_pick(self) -> None:
        assistant = DuelBPAssistant(mode=DuelBPMode.AUTO)
        frame = observation(DuelBPState.SELF_PICK)
        rules = (candidate("pick", RecommendationSource.RULE, 1.0),)

        first = assistant.process(frame, rule_candidates=rules)
        second = assistant.process(frame, rule_candidates=rules)
        third = assistant.process(frame, rule_candidates=rules)

        self.assertFalse(first.should_auto_pick)
        self.assertFalse(first.fallback_to_auto_entry)
        self.assertFalse(second.should_auto_pick)
        self.assertTrue(third.should_auto_pick)
        self.assertFalse(third.fallback_to_auto_entry)

    def test_auto_falls_back_for_low_confidence_or_missing_recommendation(self) -> None:
        low_assistant = DuelBPAssistant(mode=DuelBPMode.AUTO)
        low_frame = observation(DuelBPState.SELF_PICK, confidence=0.95)
        low_rules = (
            candidate("pick", RecommendationSource.RULE, 1.0, confidence=1.0),
        )
        for _ in range(3):
            low = low_assistant.process(low_frame, rule_candidates=low_rules)
        self.assertTrue(low.fallback_to_auto_entry)
        self.assertEqual(low.reason, "auto_confidence_below_threshold")

        empty_assistant = DuelBPAssistant(mode=DuelBPMode.AUTO)
        for _ in range(3):
            empty = empty_assistant.process(
                observation(DuelBPState.SELF_PICK)
            )
        self.assertTrue(empty.fallback_to_auto_entry)
        self.assertEqual(empty.reason, "no_stable_recommendation")

    def test_auto_requires_every_frame_in_window_to_reach_point_98(self) -> None:
        assistant = DuelBPAssistant(mode=DuelBPMode.AUTO)
        rules = (candidate("pick", RecommendationSource.RULE, 1.0),)
        decisions = [
            assistant.process(
                observation(DuelBPState.SELF_PICK, confidence=confidence),
                rule_candidates=rules,
            )
            for confidence in (0.90, 0.90, 0.99)
        ]
        self.assertFalse(any(item.should_auto_pick for item in decisions))
        self.assertTrue(decisions[-1].fallback_to_auto_entry)

    def test_auto_pick_is_latched_until_observation_changes(self) -> None:
        assistant = DuelBPAssistant(mode=DuelBPMode.AUTO)
        rules = (candidate("pick", RecommendationSource.RULE, 1.0),)
        frame = observation(DuelBPState.SELF_PICK)
        for _ in range(3):
            first = assistant.process(frame, rule_candidates=rules)
        repeated = assistant.process(frame, rule_candidates=rules)
        self.assertTrue(first.should_auto_pick)
        self.assertFalse(repeated.should_auto_pick)
        self.assertEqual(repeated.reason, "auto_pick_already_issued")

    def test_recommend_mode_never_authorizes_click(self) -> None:
        assistant = DuelBPAssistant(mode=DuelBPMode.RECOMMEND)
        rules = (candidate("pick", RecommendationSource.RULE, 1.0),)
        for _ in range(3):
            decision = assistant.process(
                observation(DuelBPState.SELF_PICK), rule_candidates=rules
            )
        self.assertIsNotNone(decision.recommendation)
        self.assertFalse(decision.should_auto_pick)
        self.assertFalse(decision.fallback_to_auto_entry)


if __name__ == "__main__":
    unittest.main()
