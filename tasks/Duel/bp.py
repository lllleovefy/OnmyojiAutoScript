"""Pure duel battle-pick state and recommendation primitives.

The module deliberately has no dependency on the device, database, or server
layers.  Screenshot recognition and persistence can therefore feed it without
making the Duel task depend on a specific model or storage implementation.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
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


class RecommendationTier(str, Enum):
    """Ordered context tiers used to explain and rank recommendations."""

    RULE = "rule"
    EXACT_HISTORY = "exact_history"
    OPPONENT_KNOWN = "opponent_known"
    OWN_PREFIX = "own_prefix"
    ROUND_GLOBAL = "round_global"
    COLD_START = "cold_start"


@dataclass(frozen=True)
class DuelDraftSnapshot:
    """Serializable, immutable view of one match's draft ledger."""

    round_number: int
    own_picks: tuple[str, ...]
    opponent_slots: tuple[str | None, ...]
    opponent_rounds_seen: int
    pending_own_pick: str | None
    unavailable_ids: tuple[str, ...]
    completed: bool
    ready_for_recommendation: bool


class DuelDraftLedger:
    """Deterministic per-match record of sequential BP decisions.

    Our picks are committed only after the UI confirms the transition.
    Opponent slots are positional and nullable, so an unrecognized portrait in
    round one never shifts a later recognized portrait into the wrong round.
    """

    def __init__(self, max_rounds: int = 5) -> None:
        if max_rounds < 1:
            raise ValueError("max_rounds must be at least 1")
        self.max_rounds = int(max_rounds)
        self.reset()

    def reset(self) -> None:
        self._own_picks: list[str] = []
        self._opponent_slots: list[str | None] = [None] * self.max_rounds
        self._opponent_rounds_seen = 0
        self._pending_own_pick: str | None = None
        self._explicitly_unavailable: set[str] = set()

    @staticmethod
    def _normalize_id(value: object) -> str:
        normalized = str(value).strip()
        if not normalized or normalized == "0":
            raise ValueError("shikigami_id must be non-empty and non-zero")
        return normalized

    @property
    def own_picks(self) -> tuple[str, ...]:
        return tuple(self._own_picks)

    @property
    def opponent_slots(self) -> tuple[str | None, ...]:
        return tuple(self._opponent_slots)

    @property
    def opponent_context(self) -> tuple[str | None, ...]:
        """Return only observed rounds while retaining unknown positions."""

        return tuple(self._opponent_slots[: self._opponent_rounds_seen])

    @property
    def opponent_rounds_seen(self) -> int:
        return self._opponent_rounds_seen

    @property
    def pending_own_pick(self) -> str | None:
        return self._pending_own_pick

    @property
    def completed(self) -> bool:
        return len(self._own_picks) >= self.max_rounds

    @property
    def next_round(self) -> int | None:
        return None if self.completed else len(self._own_picks) + 1

    @property
    def round_number(self) -> int:
        """Current shikigami round, pinned to the final round after completion."""

        return self.next_round or self.max_rounds

    @property
    def unavailable_ids(self) -> frozenset[str]:
        unavailable = set(self._explicitly_unavailable)
        unavailable.update(self._own_picks)
        if self._pending_own_pick is not None:
            unavailable.add(self._pending_own_pick)
        return frozenset(unavailable)

    @property
    def ready_for_recommendation(self) -> bool:
        # Before our first pick no opponent round is expected. After committing
        # round N, wait until opponent round N has been observed (known or not).
        return (
            not self.completed
            and self._pending_own_pick is None
            and self._opponent_rounds_seen >= len(self._own_picks)
        )

    def record_opponent_pick(
        self,
        round_number: int,
        shikigami_id: object | None,
        *,
        replace: bool = False,
    ) -> bool:
        """Record an opponent slot; return whether the ledger changed.

        A later high-confidence recognition may resolve ``None`` to an ID.
        Replacing one non-null ID with another requires ``replace=True`` so
        recognition jitter cannot silently rewrite stable match context.
        """

        normalized_round = int(round_number)
        if not 1 <= normalized_round <= self.max_rounds:
            raise ValueError("round_number is outside the draft")
        index = normalized_round - 1
        normalized = (
            None
            if shikigami_id in (None, "", 0, "0")
            else self._normalize_id(shikigami_id)
        )
        previous = self._opponent_slots[index]
        # Once a slot is known, a later failed recognition must not erase it.
        if previous is not None and normalized is None:
            normalized = previous
        if (
            previous is not None
            and normalized is not None
            and previous != normalized
            and not replace
        ):
            raise ValueError("opponent slot already contains a different ID")
        changed = (
            previous != normalized
            or self._opponent_rounds_seen < normalized_round
        )
        self._opponent_slots[index] = normalized
        self._opponent_rounds_seen = max(
            self._opponent_rounds_seen, normalized_round
        )
        return changed

    def begin_own_pick(self, shikigami_id: object) -> bool:
        """Stage the candidate clicked in the UI without committing it."""

        if self.completed:
            raise RuntimeError("draft is already complete")
        normalized = self._normalize_id(shikigami_id)
        if self._pending_own_pick == normalized:
            return False
        if self._pending_own_pick is not None:
            raise RuntimeError("another own pick is already pending")
        if normalized in self.unavailable_ids:
            raise ValueError("shikigami_id is unavailable in this draft")
        self._pending_own_pick = normalized
        return True

    def commit_pending_own_pick(self, expected_id: object | None = None) -> str:
        """Commit the staged ID after selection and confirm are verified."""

        pending = self._pending_own_pick
        if pending is None:
            raise RuntimeError("there is no pending own pick")
        if expected_id is not None and self._normalize_id(expected_id) != pending:
            raise ValueError("verified ID does not match the pending own pick")
        self._own_picks.append(pending)
        self._pending_own_pick = None
        return pending

    def rollback_pending_own_pick(self, *, mark_unavailable: bool = False) -> str | None:
        """Discard a failed click; optionally exclude it for this match."""

        pending = self._pending_own_pick
        self._pending_own_pick = None
        if pending is not None and mark_unavailable:
            self._explicitly_unavailable.add(pending)
        return pending

    def mark_unavailable(self, shikigami_id: object) -> bool:
        normalized = self._normalize_id(shikigami_id)
        if normalized == self._pending_own_pick:
            raise RuntimeError("rollback the pending pick before excluding it")
        size = len(self._explicitly_unavailable)
        self._explicitly_unavailable.add(normalized)
        return len(self._explicitly_unavailable) != size

    def snapshot(self) -> DuelDraftSnapshot:
        return DuelDraftSnapshot(
            round_number=self.round_number,
            own_picks=self.own_picks,
            opponent_slots=self.opponent_slots,
            opponent_rounds_seen=self.opponent_rounds_seen,
            pending_own_pick=self.pending_own_pick,
            unavailable_ids=tuple(sorted(self.unavailable_ids)),
            completed=self.completed,
            ready_for_recommendation=self.ready_for_recommendation,
        )


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
    opponent_picks: tuple[str | None, ...] = ()
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
    own_picks: Sequence[str | None],
    opponent_picks: Sequence[str | None],
) -> list[dict[str, object]]:
    """Build wire/database picks while preserving nullable slot round numbers."""

    payload: list[dict[str, object]] = []
    for side, values in (("self", own_picks), ("opponent", opponent_picks)):
        occurrences: dict[str, int] = {}
        for round_number, shikigami_id in enumerate(values, 1):
            if shikigami_id is None:
                continue
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
    context_level: RecommendationTier | str | None = None
    context_sample_size: int = 0


@dataclass(frozen=True)
class DuelRecommendation:
    shikigami_id: str
    source: RecommendationSource
    score: float
    confidence: float
    sample_size: int = 0
    reason: str = ""
    evidence_sources: tuple[RecommendationSource, ...] = ()
    context_level: RecommendationTier = RecommendationTier.COLD_START
    context_sample_size: int = 0
    rank: int = 1


class DuelRecommendationEngine:
    """Produce deterministic ranked recommendations across context tiers.

    Rule priority remains authoritative. Personal history then falls back from
    exact context to known opponent positions, our own prefix, and finally a
    round-global Bayesian score. External data is last and remains cold-start
    evidence only.
    """

    _PERSONAL_TIERS = (
        RecommendationTier.EXACT_HISTORY,
        RecommendationTier.OPPONENT_KNOWN,
        RecommendationTier.OWN_PREFIX,
        RecommendationTier.ROUND_GLOBAL,
    )

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
        *,
        unavailable_ids: Iterable[str] = (),
    ) -> DuelRecommendation | None:
        ranked = self.recommend_ranked(
            observation,
            rule_candidates=rule_candidates,
            personal_candidates=personal_candidates,
            external_candidates=external_candidates,
            unavailable_ids=unavailable_ids,
            limit=1,
        )
        return ranked[0] if ranked else None

    def recommend_ranked(
        self,
        observation: BPObservation,
        rule_candidates: Iterable[DuelRecommendationCandidate] = (),
        personal_candidates: Iterable[DuelRecommendationCandidate] = (),
        external_candidates: Iterable[DuelRecommendationCandidate] = (),
        *,
        unavailable_ids: Iterable[str] = (),
        limit: int = 5,
    ) -> tuple[DuelRecommendation, ...]:
        """Return up to ``limit`` choices in fallback order.

        The list may contain fewer than five entries when the available data
        does not contain five distinct legal shikigami.
        """

        if limit < 1:
            raise ValueError("limit must be at least 1")
        unavailable = frozenset(
            (*observation.unavailable_shikigami, *(str(value) for value in unavailable_ids))
        )
        rules = self._eligible(
            rule_candidates, RecommendationSource.RULE, unavailable
        )
        personal = self._dedupe_candidates(
            candidate
            for candidate in self._eligible(
                personal_candidates, RecommendationSource.PERSONAL, unavailable
            )
            if self._personal_candidate_is_qualified(candidate)
        )
        external = self._dedupe_candidates(
            self._eligible(
                external_candidates, RecommendationSource.EXTERNAL, unavailable
            )
        )

        personal_by_tier: dict[
            RecommendationTier, list[DuelRecommendationCandidate]
        ] = {tier: [] for tier in self._PERSONAL_TIERS}
        for candidate in personal:
            personal_by_tier[self._candidate_tier(candidate)].append(candidate)
        for candidates in personal_by_tier.values():
            candidates.sort(key=self._candidate_sort_key)

        personal_evidence: dict[str, DuelRecommendationCandidate] = {}
        for tier in self._PERSONAL_TIERS:
            if tier == RecommendationTier.ROUND_GLOBAL:
                continue
            for candidate in personal_by_tier[tier]:
                personal_evidence.setdefault(candidate.shikigami_id, candidate)

        ordered: list[DuelRecommendation] = []
        seen_ids: set[str] = set()

        for recommendation in self._rank_rules(rules, personal_evidence):
            self._append_distinct(ordered, seen_ids, recommendation, limit)
            if len(ordered) >= limit:
                break

        if len(ordered) < limit:
            for tier in self._PERSONAL_TIERS:
                for candidate in personal_by_tier[tier]:
                    self._append_distinct(
                        ordered,
                        seen_ids,
                        self._to_recommendation(
                            candidate,
                            evidence_sources=(RecommendationSource.PERSONAL,),
                            context_level=tier,
                        ),
                        limit,
                    )
                    if len(ordered) >= limit:
                        break
                if len(ordered) >= limit:
                    break

        if len(ordered) < limit:
            for candidate in sorted(external, key=self._candidate_sort_key):
                self._append_distinct(
                    ordered,
                    seen_ids,
                    self._to_recommendation(
                        candidate,
                        evidence_sources=(RecommendationSource.EXTERNAL,),
                        context_level=RecommendationTier.COLD_START,
                    ),
                    limit,
                )
                if len(ordered) >= limit:
                    break

        return tuple(
            replace(recommendation, rank=index)
            for index, recommendation in enumerate(ordered, 1)
        )

    @staticmethod
    def _append_distinct(
        ordered: list[DuelRecommendation],
        seen_ids: set[str],
        recommendation: DuelRecommendation,
        limit: int,
    ) -> None:
        if (
            len(ordered) >= limit
            or recommendation.shikigami_id in seen_ids
        ):
            return
        seen_ids.add(recommendation.shikigami_id)
        ordered.append(recommendation)

    @staticmethod
    def _candidate_tier(
        candidate: DuelRecommendationCandidate,
    ) -> RecommendationTier:
        if candidate.context_level is not None:
            try:
                return RecommendationTier(candidate.context_level)
            except ValueError:
                pass
        if candidate.source == RecommendationSource.RULE:
            return RecommendationTier.RULE
        if candidate.source == RecommendationSource.PERSONAL:
            return RecommendationTier.EXACT_HISTORY
        return RecommendationTier.COLD_START

    def _personal_candidate_is_qualified(
        self, candidate: DuelRecommendationCandidate
    ) -> bool:
        tier = self._candidate_tier(candidate)
        if tier not in self._PERSONAL_TIERS:
            return False
        if tier == RecommendationTier.ROUND_GLOBAL:
            return candidate.sample_size > 0 or candidate.context_sample_size > 0
        context_samples = (
            candidate.context_sample_size
            if candidate.context_sample_size > 0
            else candidate.sample_size
        )
        return context_samples >= self.personal_min_samples

    @classmethod
    def _dedupe_candidates(
        cls, candidates: Iterable[DuelRecommendationCandidate]
    ) -> list[DuelRecommendationCandidate]:
        selected: dict[
            tuple[RecommendationTier, str], DuelRecommendationCandidate
        ] = {}
        for candidate in candidates:
            key = (cls._candidate_tier(candidate), candidate.shikigami_id)
            previous = selected.get(key)
            if previous is None or cls._candidate_sort_key(
                candidate
            ) < cls._candidate_sort_key(previous):
                selected[key] = candidate
        return list(selected.values())

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
            and str(candidate.shikigami_id) not in unavailable
        ]

    def _rank_rules(
        self,
        rules: Sequence[DuelRecommendationCandidate],
        personal_by_id: Mapping[str, DuelRecommendationCandidate],
    ) -> list[DuelRecommendation]:
        by_priority: dict[int, list[DuelRecommendationCandidate]] = {}
        for candidate in self._dedupe_rules(rules):
            by_priority.setdefault(candidate.priority, []).append(candidate)

        ordered: list[DuelRecommendation] = []
        for priority in sorted(by_priority):
            tier = by_priority[priority]

            def rule_key(
                candidate: DuelRecommendationCandidate,
            ) -> tuple[object, ...]:
                statistic = personal_by_id.get(candidate.shikigami_id)
                return (
                    0 if statistic is not None else 1,
                    -(statistic.score if statistic else -1.0),
                    -candidate.score,
                    -candidate.confidence,
                    candidate.shikigami_id,
                )

            for candidate in sorted(tier, key=rule_key):
                statistic = personal_by_id.get(candidate.shikigami_id)
                if statistic is None:
                    ordered.append(
                        self._to_recommendation(
                            candidate,
                            evidence_sources=(RecommendationSource.RULE,),
                            context_level=RecommendationTier.RULE,
                        )
                    )
                    continue
                reason_parts = [
                    part for part in (candidate.reason, statistic.reason) if part
                ]
                ordered.append(
                    DuelRecommendation(
                        shikigami_id=candidate.shikigami_id,
                        source=RecommendationSource.RULE,
                        score=statistic.score,
                        confidence=min(
                            candidate.confidence, statistic.confidence
                        ),
                        sample_size=statistic.sample_size,
                        reason="; ".join(reason_parts),
                        evidence_sources=(
                            RecommendationSource.RULE,
                            RecommendationSource.PERSONAL,
                        ),
                        context_level=RecommendationTier.RULE,
                        context_sample_size=statistic.context_sample_size,
                    )
                )
        return ordered

    @classmethod
    def _dedupe_rules(
        cls, candidates: Iterable[DuelRecommendationCandidate]
    ) -> list[DuelRecommendationCandidate]:
        selected: dict[str, DuelRecommendationCandidate] = {}
        for candidate in candidates:
            previous = selected.get(candidate.shikigami_id)
            candidate_key = (
                candidate.priority,
                *cls._candidate_sort_key(candidate),
            )
            previous_key = (
                previous.priority,
                *cls._candidate_sort_key(previous),
            ) if previous is not None else None
            if previous_key is None or candidate_key < previous_key:
                selected[candidate.shikigami_id] = candidate
        return list(selected.values())

    @staticmethod
    def _candidate_sort_key(
        candidate: DuelRecommendationCandidate,
    ) -> tuple[float, float, int, str]:
        return (
            -candidate.score,
            -candidate.confidence,
            -candidate.sample_size,
            candidate.shikigami_id,
        )

    @staticmethod
    def _to_recommendation(
        candidate: DuelRecommendationCandidate,
        evidence_sources: tuple[RecommendationSource, ...],
        context_level: RecommendationTier | None = None,
    ) -> DuelRecommendation:
        return DuelRecommendation(
            shikigami_id=candidate.shikigami_id,
            source=candidate.source,
            score=candidate.score,
            confidence=candidate.confidence,
            sample_size=candidate.sample_size,
            reason=candidate.reason,
            evidence_sources=evidence_sources,
            context_level=(
                context_level
                or DuelRecommendationEngine._candidate_tier(candidate)
            ),
            context_sample_size=candidate.context_sample_size,
        )


@dataclass(frozen=True)
class DuelBPDecision:
    mode: DuelBPMode
    state_update: BPStateUpdate
    recommendation: DuelRecommendation | None = None
    recommendations: tuple[DuelRecommendation, ...] = ()
    action_confidence: float = 0.0
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
        self._self_pick_fingerprint: tuple[object, ...] | None = None
        self._auto_pick_fingerprint: tuple[object, ...] | None = None

    @property
    def stable_frames_required(self) -> int:
        return self.state_machine.stable_frames_required

    def reset(self) -> None:
        self.state_machine.reset()
        self._self_pick_attempts = 0
        self._self_pick_fingerprint = None
        self._auto_pick_fingerprint = None

    def process(
        self,
        observation: BPObservation,
        rule_candidates: Iterable[DuelRecommendationCandidate] = (),
        personal_candidates: Iterable[DuelRecommendationCandidate] = (),
        external_candidates: Iterable[DuelRecommendationCandidate] = (),
        *,
        action_confidence: float | None = None,
        unavailable_ids: Iterable[str] = (),
    ) -> DuelBPDecision:
        effective_action_confidence = (
            observation.confidence
            if action_confidence is None
            else float(action_confidence)
        )
        if not 0 <= effective_action_confidence <= 1:
            raise ValueError("action_confidence must be between 0 and 1")
        unavailable = tuple(
            sorted({str(item) for item in unavailable_ids})
        )
        update = self.state_machine.observe(observation)
        if observation.state == DuelBPState.SELF_PICK:
            if self._self_pick_fingerprint != observation.fingerprint:
                self._self_pick_fingerprint = observation.fingerprint
                self._self_pick_attempts = 1
            else:
                self._self_pick_attempts += 1
        else:
            self._self_pick_attempts = 0
            self._self_pick_fingerprint = None
            self._auto_pick_fingerprint = None

        if self.mode == DuelBPMode.OFF:
            return DuelBPDecision(
                self.mode,
                update,
                action_confidence=effective_action_confidence,
                reason="assistant_disabled",
            )
        if self.mode == DuelBPMode.OBSERVE:
            return DuelBPDecision(
                self.mode,
                update,
                action_confidence=effective_action_confidence,
                reason="observe_only",
            )

        can_recommend = (
            observation.state == DuelBPState.SELF_PICK
            and update.accepted
            and update.is_stable
            and observation.confidence >= self.recommend_confidence
        )
        recommendations: tuple[DuelRecommendation, ...] = ()
        if can_recommend:
            recommendations = self.recommendation_engine.recommend_ranked(
                observation,
                rule_candidates=rule_candidates,
                personal_candidates=personal_candidates,
                external_candidates=external_candidates,
                unavailable_ids=unavailable,
                limit=5,
            )
        recommendation = recommendations[0] if recommendations else None

        if self.mode == DuelBPMode.RECOMMEND:
            return DuelBPDecision(
                self.mode,
                update,
                recommendation=recommendation,
                recommendations=recommendations,
                action_confidence=effective_action_confidence,
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
                self.mode,
                update,
                action_confidence=effective_action_confidence,
                reason="waiting_for_stable_frames",
            )
        if effective_action_confidence < self.auto_confidence:
            return DuelBPDecision(
                self.mode,
                update,
                recommendation=recommendation,
                recommendations=recommendations,
                action_confidence=effective_action_confidence,
                fallback_to_auto_entry=True,
                reason="auto_confidence_below_threshold",
            )
        if recommendation is None:
            return DuelBPDecision(
                self.mode,
                update,
                recommendations=recommendations,
                action_confidence=effective_action_confidence,
                fallback_to_auto_entry=True,
                reason="no_stable_recommendation",
            )
        action_fingerprint = (observation.fingerprint, unavailable)
        if self._auto_pick_fingerprint == action_fingerprint:
            return DuelBPDecision(
                self.mode,
                update,
                recommendation=recommendation,
                recommendations=recommendations,
                action_confidence=effective_action_confidence,
                reason="auto_pick_already_issued",
            )
        self._auto_pick_fingerprint = action_fingerprint
        return DuelBPDecision(
            self.mode,
            update,
            recommendation=recommendation,
            recommendations=recommendations,
            action_confidence=effective_action_confidence,
            should_auto_pick=True,
            reason="auto_pick_ready",
        )
