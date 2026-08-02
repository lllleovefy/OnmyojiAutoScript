import unittest

from tasks.Duel.bp import (
    BPObservation,
    DuelBPAssistant,
    DuelDraftLedger,
    DuelBPMode,
    DuelBPState,
    DuelBPStateMachine,
    DuelRecommendationCandidate,
    DuelRecommendationEngine,
    RecommendationSource,
    RecommendationTier,
    build_pick_payload,
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
    context_level: RecommendationTier | None = None,
    context_sample_size: int = 0,
) -> DuelRecommendationCandidate:
    return DuelRecommendationCandidate(
        shikigami_id=shikigami_id,
        source=source,
        score=score,
        confidence=confidence,
        priority=priority,
        sample_size=sample_size,
        context_level=context_level,
        context_sample_size=context_sample_size,
    )


class DuelBPConfigTest(unittest.TestCase):
    def test_mode_defaults_to_off_and_accepts_public_string_values(self) -> None:
        self.assertEqual(DuelConfig().bp_mode, DuelBPMode.OFF)
        for mode in DuelBPMode:
            with self.subTest(mode=mode):
                self.assertEqual(DuelConfig(bp_mode=mode.value).bp_mode, mode)

    def test_shishen_pool_defaults_empty_and_normalizes_unique_ids(self) -> None:
        self.assertEqual([], DuelConfig().bp_shishen_pool)
        config = DuelConfig(bp_shishen_pool=[596, "399", 596])
        self.assertEqual([596, 399], config.bp_shishen_pool)

    def test_script_argument_json_type_parses_an_id_list(self) -> None:
        from module.server.script_router import _coerce_script_argument_value

        self.assertEqual(
            [596, 399],
            _coerce_script_argument_value("json", "[596,399]"),
        )

    def test_generic_task_arguments_support_default_factory_fields(self) -> None:
        """The shared task-config page must serialize the strict pool."""

        from module.config.config_model import ConfigModel

        arguments = ConfigModel().script_task("Duel")
        pool = next(
            item
            for item in arguments["duel_config"]
            if item["name"] == "bp_shishen_pool"
        )
        self.assertEqual([], pool["default"])
        self.assertEqual([], pool["value"])


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


class DuelDraftLedgerTest(unittest.TestCase):
    def test_pending_pick_is_committed_only_after_verification(self) -> None:
        ledger = DuelDraftLedger()
        self.assertEqual(1, ledger.round_number)
        self.assertTrue(ledger.ready_for_recommendation)

        self.assertTrue(ledger.begin_own_pick(10))
        self.assertEqual("10", ledger.pending_own_pick)
        self.assertIn("10", ledger.unavailable_ids)
        self.assertFalse(ledger.ready_for_recommendation)
        self.assertEqual("10", ledger.commit_pending_own_pick(expected_id="10"))

        self.assertEqual(("10",), ledger.own_picks)
        self.assertEqual(2, ledger.round_number)
        self.assertFalse(ledger.ready_for_recommendation)

        ledger.record_opponent_pick(1, None)
        self.assertEqual((None,), ledger.opponent_context)
        self.assertTrue(ledger.ready_for_recommendation)

        ledger.begin_own_pick("20")
        self.assertEqual("20", ledger.rollback_pending_own_pick(mark_unavailable=True))
        self.assertIn("20", ledger.unavailable_ids)
        with self.assertRaises(ValueError):
            ledger.begin_own_pick("20")

    def test_nullable_opponent_slots_keep_round_positions(self) -> None:
        ledger = DuelDraftLedger()
        ledger.record_opponent_pick(1, None)
        ledger.record_opponent_pick(2, "22")
        self.assertEqual((None, "22"), ledger.opponent_context)

        self.assertTrue(ledger.record_opponent_pick(1, "11"))
        self.assertEqual(("11", "22"), ledger.opponent_context)
        self.assertFalse(ledger.record_opponent_pick(1, None))
        self.assertEqual(("11", "22"), ledger.opponent_context)
        with self.assertRaises(ValueError):
            ledger.record_opponent_pick(2, "different")

        snapshot = ledger.snapshot()
        self.assertEqual(("11", "22", None, None, None), snapshot.opponent_slots)
        self.assertEqual(2, snapshot.opponent_rounds_seen)

    def test_five_committed_picks_complete_the_ledger(self) -> None:
        ledger = DuelDraftLedger()
        for round_number in range(1, 6):
            if round_number > 1:
                ledger.record_opponent_pick(round_number - 1, None)
            ledger.begin_own_pick(str(round_number))
            ledger.commit_pending_own_pick()
        self.assertTrue(ledger.completed)
        self.assertIsNone(ledger.next_round)
        self.assertFalse(ledger.ready_for_recommendation)
        with self.assertRaises(RuntimeError):
            ledger.begin_own_pick("6")

    def test_pick_payload_skips_unknown_but_keeps_its_round(self) -> None:
        payload = build_pick_payload(
            ("10", "11"),
            (None, "20", "20"),
        )
        opponent = [item for item in payload if item["side"] == "opponent"]
        self.assertEqual([2, 3], [item["round"] for item in opponent])
        self.assertEqual([1, 2], [item["count"] for item in opponent])


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

    def test_ranked_result_uses_all_fallback_tiers_and_is_top_five(self) -> None:
        ranked = self.engine.recommend_ranked(
            self.frame,
            rule_candidates=(
                candidate("rule", RecommendationSource.RULE, 0.1),
            ),
            personal_candidates=(
                candidate(
                    "exact",
                    RecommendationSource.PERSONAL,
                    0.7,
                    sample_size=4,
                    context_level=RecommendationTier.EXACT_HISTORY,
                    context_sample_size=20,
                ),
                candidate(
                    "opponent",
                    RecommendationSource.PERSONAL,
                    0.9,
                    sample_size=20,
                    context_level=RecommendationTier.OPPONENT_KNOWN,
                    context_sample_size=20,
                ),
                candidate(
                    "own",
                    RecommendationSource.PERSONAL,
                    0.95,
                    sample_size=20,
                    context_level=RecommendationTier.OWN_PREFIX,
                    context_sample_size=20,
                ),
                candidate(
                    "global",
                    RecommendationSource.PERSONAL,
                    1.0,
                    sample_size=1,
                    context_level=RecommendationTier.ROUND_GLOBAL,
                    context_sample_size=1,
                ),
            ),
            external_candidates=(
                candidate("external", RecommendationSource.EXTERNAL, 1.0),
            ),
        )
        self.assertEqual(
            ["rule", "exact", "opponent", "own", "global"],
            [item.shikigami_id for item in ranked],
        )
        self.assertEqual([1, 2, 3, 4, 5], [item.rank for item in ranked])
        self.assertEqual(
            RecommendationTier.EXACT_HISTORY, ranked[1].context_level
        )

    def test_ranked_result_accepts_per_match_unavailable_ids(self) -> None:
        ranked = self.engine.recommend_ranked(
            self.frame,
            rule_candidates=(
                candidate("blocked", RecommendationSource.RULE, 1.0),
                candidate("available", RecommendationSource.RULE, 0.5),
            ),
            unavailable_ids=("blocked",),
        )
        self.assertEqual(["available"], [item.shikigami_id for item in ranked])

    def test_one_global_sample_does_not_reorder_equal_priority_rules(
        self,
    ) -> None:
        ranked = self.engine.recommend_ranked(
            self.frame,
            rule_candidates=(
                candidate(
                    "rule-a",
                    RecommendationSource.RULE,
                    0.9,
                    priority=1,
                ),
                candidate(
                    "rule-b",
                    RecommendationSource.RULE,
                    0.8,
                    priority=1,
                ),
            ),
            personal_candidates=(
                candidate(
                    "rule-b",
                    RecommendationSource.PERSONAL,
                    1.0,
                    sample_size=1,
                    context_level=RecommendationTier.ROUND_GLOBAL,
                    context_sample_size=1,
                ),
            ),
        )

        self.assertEqual(
            ["rule-a", "rule-b"],
            [item.shikigami_id for item in ranked[:2]],
        )
        self.assertEqual(
            (RecommendationSource.RULE,),
            ranked[1].evidence_sources,
        )


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

    def test_auto_pick_reopens_after_candidate_is_marked_unavailable(
        self,
    ) -> None:
        assistant = DuelBPAssistant(mode=DuelBPMode.AUTO)
        rules = (
            candidate("first", RecommendationSource.RULE, 1.0),
            candidate("second", RecommendationSource.RULE, 0.9),
        )
        frame = observation(DuelBPState.SELF_PICK)
        for _ in range(3):
            first = assistant.process(frame, rule_candidates=rules)
        self.assertTrue(first.should_auto_pick)
        self.assertEqual("first", first.recommendation.shikigami_id)

        retry = assistant.process(
            frame,
            rule_candidates=rules,
            unavailable_ids=("first",),
        )
        self.assertTrue(retry.should_auto_pick)
        self.assertEqual("second", retry.recommendation.shikigami_id)

    def test_auto_restarts_stability_window_when_opponent_context_changes(
        self,
    ) -> None:
        assistant = DuelBPAssistant(mode=DuelBPMode.AUTO)
        rules = (candidate("pick", RecommendationSource.RULE, 1.0),)
        before = observation(DuelBPState.SELF_PICK)
        for _ in range(3):
            assistant.process(before, rule_candidates=rules)

        after = BPObservation(
            state=DuelBPState.SELF_PICK,
            confidence=1.0,
            opponent_picks=("opponent",),
        )
        first = assistant.process(after, rule_candidates=rules)
        second = assistant.process(after, rule_candidates=rules)
        third = assistant.process(after, rule_candidates=rules)

        self.assertFalse(first.should_auto_pick)
        self.assertFalse(first.fallback_to_auto_entry)
        self.assertEqual("waiting_for_stable_frames", first.reason)
        self.assertFalse(second.should_auto_pick)
        self.assertTrue(third.should_auto_pick)

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

    def test_recommendation_quality_does_not_replace_action_safety(self) -> None:
        assistant = DuelBPAssistant(mode=DuelBPMode.AUTO)
        low_quality_rule = (
            candidate(
                "pick",
                RecommendationSource.RULE,
                0.4,
                confidence=0.2,
            ),
        )
        for _ in range(3):
            safe = assistant.process(
                observation(DuelBPState.SELF_PICK),
                rule_candidates=low_quality_rule,
                action_confidence=0.99,
            )
        self.assertTrue(safe.should_auto_pick)
        self.assertEqual(0.2, safe.recommendation.confidence)
        self.assertEqual(("pick",), tuple(
            item.shikigami_id for item in safe.recommendations
        ))

        unsafe_assistant = DuelBPAssistant(mode=DuelBPMode.AUTO)
        for _ in range(3):
            unsafe = unsafe_assistant.process(
                observation(DuelBPState.SELF_PICK),
                rule_candidates=low_quality_rule,
                action_confidence=0.97,
            )
        self.assertFalse(unsafe.should_auto_pick)
        self.assertTrue(unsafe.fallback_to_auto_entry)


if __name__ == "__main__":
    unittest.main()
