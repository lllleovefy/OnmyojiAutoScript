"""Pure duel battle-pick state and recommendation primitives.

The module deliberately has no dependency on the device, database, or server
layers.  Screenshot recognition and persistence can therefore feed it without
making the Duel task depend on a specific model or storage implementation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Mapping, Sequence


class DuelBPMode(str, Enum):
    """How much control the battle-pick assistant is allowed to take."""

    OFF = "off"
    OBSERVE = "observe"
    RECOMMEND = "recommend"
    AUTO = "auto"


class DuelBPState(str, Enum):
    BAN = "BAN"
    SELF_PICK = "SELF_PICK"
    OPPONENT_PICK = "OPPONENT_PICK"
    READY = "READY"
    BATTLE = "BATTLE"
    RESULT = "RESULT"


class RecommendationSource(str, Enum):
    RULE = "rule"
    PERSONAL = "personal"
    EXTERNAL = "external"


_ALLOWED_TRANSITIONS = {
    DuelBPState.BAN: {
        DuelBPState.BAN,
        DuelBPState.SELF_PICK,
        DuelBPState.OPPONENT_PICK,
    },
    DuelBPState.SELF_PICK: {
        DuelBPState.SELF_PICK,
        DuelBPState.OPPONENT_PICK,
        DuelBPState.READY,
        DuelBPState.BATTLE,
    },
    DuelBPState.OPPONENT_PICK: {
        DuelBPState.OPPONENT_PICK,
        DuelBPState.SELF_PICK,
        DuelBPState.READY,
        DuelBPState.BATTLE,
    },
    DuelBPState.READY: {DuelBPState.READY, DuelBPState.BATTLE},
    DuelBPState.BATTLE: {DuelBPState.BATTLE, DuelBPState.RESULT},
    DuelBPState.RESULT: {DuelBPState.RESULT},
}


@dataclass(frozen=True)
class BPObservation:
    """One normalized recognition result from a screenshot."""

    state: DuelBPState
    confidence: float
    own_picks: tuple[str, ...] = ()
    opponent_picks: tuple[str, ...] = ()
    bans: tuple[str, ...] = ()

    @property
    def fingerprint(self) -> tuple[object, ...]:
        return (
            self.state,
            self.own_picks,
            self.opponent_picks,
            self.bans,
        )

    @property
    def unavailable_shikigami(self) -> frozenset[str]:
        # Mirror picks across both teams are legal. Only our own selections and
        # globally banned shikigami must be excluded from our next choice.
        return frozenset((*self.own_picks, *self.bans))


def build_pick_payload(
    own_picks: Sequence[str], opponent_picks: Sequence[str]
) -> list[dict[str, object]]:
    """Build wire/database picks with per-side occurrence ordinals."""

    payload: list[dict[str, object]] = []
    for side, values in (("self", own_picks), ("opponent", opponent_picks)):
        occurrences: dict[str, int] = {}
        for round_number, shikigami_id in enumerate(values, 1):
            occurrence = occurrences.get(shikigami_id, 0) + 1
            occurrences[shikigami_id] = occurrence
            payload.append(
                {
                    "side": side,
                    "round": round_number,
                    "shishen_id": shikigami_id,
                    "count": occurrence,
                }
            )
    return payload


@dataclass(frozen=True)
class BPStateUpdate:
    previous_state: DuelBPState
    state: DuelBPState
    stable_frames: int
    is_stable: bool
    accepted: bool
    transitioned: bool = False
    reason: str = ""


class DuelBPStateMachine:
    """Legal-transition BP state machine with an exact-frame debounce."""

    def __init__(
        self,
        stable_frames: int = 3,
        minimum_confidence: float = 0.90,
    ) -> None:
        if stable_frames < 1:
            raise ValueError("stable_frames must be at least 1")
        if not 0 <= minimum_confidence <= 1:
            raise ValueError("minimum_confidence must be between 0 and 1")
        self.stable_frames_required = stable_frames
        self.minimum_confidence = minimum_confidence
        self.reset()

    def reset(self) -> None:
        self.state = DuelBPState.BAN
        self._candidate_fingerprint: tuple[object, ...] | None = None
        self._candidate_frames = 0

    def observe(self, observation: BPObservation) -> BPStateUpdate:
        previous = self.state
        if observation.confidence < self.minimum_confidence:
            self._candidate_fingerprint = None
            self._candidate_frames = 0
            return BPStateUpdate(
                previous_state=previous,
                state=self.state,
                stable_frames=0,
                is_stable=False,
                accepted=False,
                reason="recognition_confidence_below_threshold",
            )

        if observation.fingerprint == self._candidate_fingerprint:
            self._candidate_frames += 1
        else:
            self._candidate_fingerprint = observation.fingerprint
            self._candidate_frames = 1

        stable = self._candidate_frames >= self.stable_frames_required
        if not stable:
            return BPStateUpdate(
                previous_state=previous,
                state=self.state,
                stable_frames=self._candidate_frames,
                is_stable=False,
                accepted=False,
                reason="waiting_for_stable_frames",
            )

        if observation.state == self.state:
            return BPStateUpdate(
                previous_state=previous,
                state=self.state,
                stable_frames=self._candidate_frames,
                is_stable=True,
                accepted=True,
                reason="state_confirmed",
            )

        if observation.state not in _ALLOWED_TRANSITIONS[self.state]:
            return BPStateUpdate(
                previous_state=previous,
                state=self.state,
                stable_frames=self._candidate_frames,
                is_stable=True,
                accepted=False,
                reason="unexpected_state_transition",
            )

        self.state = observation.state
        return BPStateUpdate(
            previous_state=previous,
            state=self.state,
            stable_frames=self._candidate_frames,
            is_stable=True,
            accepted=True,
            transitioned=True,
            reason="state_transitioned",
        )


@dataclass(frozen=True)
class DuelRecommendationCandidate:
    shikigami_id: str
    source: RecommendationSource
    score: float
    confidence: float = 1.0
    priority: int = 0
    sample_size: int = 0
    reason: str = ""


@dataclass(frozen=True)
class DuelRecommendation:
    shikigami_id: str
    source: RecommendationSource
    score: float
    confidence: float
    sample_size: int = 0
    reason: str = ""
    evidence_sources: tuple[RecommendationSource, ...] = ()


class DuelRecommendationEngine:
    """Resolve rule, personal, and external candidates in deterministic order.

    Rules are authoritative.  Qualified personal statistics can only re-rank
    candidates inside the best rule priority tier.  Personal statistics become
    a standalone source only when no rule matches, and external data is the
    final cold-start seed.
    """

    def __init__(self, personal_min_samples: int = 20) -> None:
        if personal_min_samples < 1:
            raise ValueError("personal_min_samples must be at least 1")
        self.personal_min_samples = personal_min_samples

    def recommend(
        self,
        observation: BPObservation,
        rule_candidates: Iterable[DuelRecommendationCandidate] = (),
        personal_candidates: Iterable[DuelRecommendationCandidate] = (),
        external_candidates: Iterable[DuelRecommendationCandidate] = (),
    ) -> DuelRecommendation | None:
        unavailable = observation.unavailable_shikigami
        rules = self._eligible(
            rule_candidates, RecommendationSource.RULE, unavailable
        )
        personal = [
            candidate
            for candidate in self._eligible(
                personal_candidates, RecommendationSource.PERSONAL, unavailable
            )
            if candidate.sample_size >= self.personal_min_samples
        ]
        external = self._eligible(
            external_candidates, RecommendationSource.EXTERNAL, unavailable
        )

        if rules:
            return self._from_rules(rules, personal)
        if personal:
            return self._to_recommendation(
                max(personal, key=self._score_key),
                evidence_sources=(RecommendationSource.PERSONAL,),
            )
        if external:
            return self._to_recommendation(
                max(external, key=self._score_key),
                evidence_sources=(RecommendationSource.EXTERNAL,),
            )
        return None

    @staticmethod
    def _eligible(
        candidates: Iterable[DuelRecommendationCandidate],
        expected_source: RecommendationSource,
        unavailable: frozenset[str],
    ) -> list[DuelRecommendationCandidate]:
        return [
            candidate
            for candidate in candidates
            if candidate.source == expected_source
            and candidate.shikigami_id
            and candidate.shikigami_id not in unavailable
        ]

    def _from_rules(
        self,
        rules: Sequence[DuelRecommendationCandidate],
        personal: Sequence[DuelRecommendationCandidate],
    ) -> DuelRecommendation:
        top_priority = min(candidate.priority for candidate in rules)
        top_rules = [
            candidate for candidate in rules if candidate.priority == top_priority
        ]
        personal_by_id: Mapping[str, DuelRecommendationCandidate] = {
            candidate.shikigami_id: candidate for candidate in personal
        }

        def rule_key(
            candidate: DuelRecommendationCandidate,
        ) -> tuple[int, float, float, float, str]:
            statistic = personal_by_id.get(candidate.shikigami_id)
            return (
                int(statistic is not None),
                statistic.score if statistic else float("-inf"),
                candidate.score,
                candidate.confidence,
                candidate.shikigami_id,
            )

        selected = max(top_rules, key=rule_key)
        statistic = personal_by_id.get(selected.shikigami_id)
        if statistic is None:
            return self._to_recommendation(
                selected, evidence_sources=(RecommendationSource.RULE,)
            )

        reason_parts = [part for part in (selected.reason, statistic.reason) if part]
        return DuelRecommendation(
            shikigami_id=selected.shikigami_id,
            source=RecommendationSource.RULE,
            score=statistic.score,
            confidence=min(selected.confidence, statistic.confidence),
            sample_size=statistic.sample_size,
            reason="; ".join(reason_parts),
            evidence_sources=(
                RecommendationSource.RULE,
                RecommendationSource.PERSONAL,
            ),
        )

    @staticmethod
    def _score_key(
        candidate: DuelRecommendationCandidate,
    ) -> tuple[float, float, int, str]:
        return (
            candidate.score,
            candidate.confidence,
            candidate.sample_size,
            candidate.shikigami_id,
        )

    @staticmethod
    def _to_recommendation(
        candidate: DuelRecommendationCandidate,
        evidence_sources: tuple[RecommendationSource, ...],
    ) -> DuelRecommendation:
        return DuelRecommendation(
            shikigami_id=candidate.shikigami_id,
            source=candidate.source,
            score=candidate.score,
            confidence=candidate.confidence,
            sample_size=candidate.sample_size,
            reason=candidate.reason,
            evidence_sources=evidence_sources,
        )


@dataclass(frozen=True)
class DuelBPDecision:
    mode: DuelBPMode
    state_update: BPStateUpdate
    recommendation: DuelRecommendation | None = None
    should_auto_pick: bool = False
    fallback_to_auto_entry: bool = False
    reason: str = ""


class DuelBPAssistant:
    """Mode and confidence policy around the state/recommendation engines."""

    def __init__(
        self,
        mode: DuelBPMode = DuelBPMode.OFF,
        stable_frames: int = 3,
        recommend_confidence: float = 0.90,
        auto_confidence: float = 0.98,
        personal_min_samples: int = 20,
    ) -> None:
        if stable_frames < 3:
            raise ValueError("Duel BP assistant requires at least 3 stable frames")
        if not 0 <= recommend_confidence <= 1:
            raise ValueError("recommend_confidence must be between 0 and 1")
        if not max(recommend_confidence, 0.98) <= auto_confidence <= 1:
            raise ValueError(
                "auto_confidence must be at least 0.98 and no lower than "
                "recommend_confidence"
            )
        self.mode = DuelBPMode(mode)
        self.recommend_confidence = recommend_confidence
        self.auto_confidence = auto_confidence
        self.state_machine = DuelBPStateMachine(
            stable_frames=stable_frames,
            # AUTO is fail-closed: every frame in the debounce window must
            # satisfy the auto threshold, not merely the final frame.
            minimum_confidence=(
                auto_confidence
                if self.mode == DuelBPMode.AUTO
                else recommend_confidence
            ),
        )
        self.recommendation_engine = DuelRecommendationEngine(
            personal_min_samples=personal_min_samples
        )
        self._self_pick_attempts = 0
        self._auto_pick_fingerprint: tuple[object, ...] | None = None

    @property
    def stable_frames_required(self) -> int:
        return self.state_machine.stable_frames_required

    def reset(self) -> None:
        self.state_machine.reset()
        self._self_pick_attempts = 0
        self._auto_pick_fingerprint = None

    def process(
        self,
        observation: BPObservation,
        rule_candidates: Iterable[DuelRecommendationCandidate] = (),
        personal_candidates: Iterable[DuelRecommendationCandidate] = (),
        external_candidates: Iterable[DuelRecommendationCandidate] = (),
    ) -> DuelBPDecision:
        update = self.state_machine.observe(observation)
        if observation.state == DuelBPState.SELF_PICK:
            self._self_pick_attempts += 1
        else:
            self._self_pick_attempts = 0
            self._auto_pick_fingerprint = None

        if self.mode == DuelBPMode.OFF:
            return DuelBPDecision(self.mode, update, reason="assistant_disabled")
        if self.mode == DuelBPMode.OBSERVE:
            return DuelBPDecision(self.mode, update, reason="observe_only")

        can_recommend = (
            observation.state == DuelBPState.SELF_PICK
            and update.accepted
            and update.is_stable
            and observation.confidence >= self.recommend_confidence
        )
        recommendation = None
        if can_recommend:
            recommendation = self.recommendation_engine.recommend(
                observation,
                rule_candidates=rule_candidates,
                personal_candidates=personal_candidates,
                external_candidates=external_candidates,
            )

        if self.mode == DuelBPMode.RECOMMEND:
            return DuelBPDecision(
                self.mode,
                update,
                recommendation=recommendation,
                reason=(
                    "recommendation_ready"
                    if recommendation
                    else "recommendation_not_ready"
                ),
            )

        attempts_exhausted = (
            self._self_pick_attempts >= self.stable_frames_required
        )
        if not attempts_exhausted:
            return DuelBPDecision(
                self.mode, update, reason="waiting_for_stable_frames"
            )
        if observation.confidence < self.auto_confidence:
            return DuelBPDecision(
                self.mode,
                update,
                fallback_to_auto_entry=True,
                reason="auto_confidence_below_threshold",
            )
        if recommendation is None:
            return DuelBPDecision(
                self.mode,
                update,
                fallback_to_auto_entry=True,
                reason="no_stable_recommendation",
            )
        if (
            observation.confidence < self.auto_confidence
            or recommendation.confidence < self.auto_confidence
        ):
            return DuelBPDecision(
                self.mode,
                update,
                recommendation=recommendation,
                fallback_to_auto_entry=True,
                reason="auto_confidence_below_threshold",
            )
        if self._auto_pick_fingerprint == observation.fingerprint:
            return DuelBPDecision(
                self.mode,
                update,
                recommendation=recommendation,
                reason="auto_pick_already_issued",
            )
        self._auto_pick_fingerprint = observation.fingerprint
        return DuelBPDecision(
            self.mode,
            update,
            recommendation=recommendation,
            should_auto_pick=True,
            reason="auto_pick_ready",
        )
