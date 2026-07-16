from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from typing import Any

from module.duel_data.repository import DuelRepository


Candidate = dict[str, Any]


def _ids(values: Iterable[Any] | None) -> tuple[str, ...]:
    if values is None:
        return ()
    result: list[str] = []
    for value in values:
        if isinstance(value, dict):
            value = value.get("shishen_id") or value.get("raw_id") or value.get("id")
        if value not in (None, "", 0, "0"):
            result.append(str(value))
    return tuple(result)


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _rate(value: Any, default: float = 0.0) -> float:
    result = _number(value, default)
    if result > 1:
        result /= 100
    return max(0.0, min(result, 1.0))


class DuelDataCandidateProvider:
    """Translate persisted strategies/history/snapshots into BP engine candidates."""

    def __init__(self, repository: DuelRepository, *, personal_min_samples: int = 20):
        if personal_min_samples < 1:
            raise ValueError("personal_min_samples must be at least 1")
        self.repository = repository
        self.personal_min_samples = personal_min_samples

    def get_candidates(
        self,
        *,
        own_picks: Iterable[Any] = (),
        opponent_picks: Iterable[Any] = (),
        bans: Iterable[Any] = (),
    ) -> dict[str, list[Candidate]]:
        own = _ids(own_picks)
        opponent = _ids(opponent_picks)
        normalized_bans = _ids(bans)
        return {
            "rules": self.rule_candidates(own_picks=own, opponent_picks=opponent, bans=normalized_bans),
            "personal": self.personal_candidates(own_picks=own, opponent_picks=opponent, bans=normalized_bans),
            "external": self.external_candidates(own_picks=own, opponent_picks=opponent, bans=normalized_bans),
        }

    def rule_candidates(
        self,
        *,
        own_picks: Iterable[Any] = (),
        opponent_picks: Iterable[Any] = (),
        bans: Iterable[Any] = (),
    ) -> list[Candidate]:
        own = _ids(own_picks)
        opponent = _ids(opponent_picks)
        normalized_bans = _ids(bans)
        # Duel allows mirror picks across the two teams. Only our own picks
        # and the globally banned shikigami are unavailable to us.
        unavailable = frozenset(own + normalized_bans)
        candidates: list[Candidate] = []
        for strategy in self.repository.list_strategies():
            content = strategy.content
            if not isinstance(content, dict):
                continue
            if isinstance(content.get("content"), dict):
                content = content["content"]
            rules = content.get("rules", [])
            if not isinstance(rules, list):
                continue
            for rule in rules:
                if not isinstance(rule, dict) or not self._rule_matches(rule, own, opponent, normalized_bans):
                    continue
                recommendation = rule.get("recommended_shishen_ids", rule.get("recommend", []))
                if not isinstance(recommendation, (list, tuple, set)):
                    recommendation = [recommendation]
                priority = int(_number(rule.get("priority"), 100))
                confidence = max(0.0, min(_number(rule.get("confidence"), 1.0), 1.0))
                score = _number(rule.get("score"), confidence)
                reason = str(rule.get("reason") or strategy.name)
                for shikigami_id in _ids(recommendation):
                    if shikigami_id in unavailable:
                        continue
                    candidates.append(
                        self._candidate(
                            shikigami_id,
                            source="rule",
                            score=score,
                            confidence=confidence,
                            priority=priority,
                            sample_size=0,
                            reason=reason,
                        )
                    )
        return sorted(candidates, key=lambda item: (item["priority"], -item["score"], item["shikigami_id"]))

    @staticmethod
    def _rule_matches(rule: dict[str, Any], own: tuple[str, ...], opponent: tuple[str, ...], bans: tuple[str, ...]) -> bool:
        if "own_picks" in rule and _ids(rule.get("own_picks")) != own:
            return False
        if "opponent_picks" in rule and _ids(rule.get("opponent_picks")) != opponent:
            return False
        if "bans" in rule and frozenset(_ids(rule.get("bans"))) != frozenset(bans):
            return False
        return True

    def personal_candidates(
        self,
        *,
        own_picks: Iterable[Any] = (),
        opponent_picks: Iterable[Any] = (),
        bans: Iterable[Any] = (),
    ) -> list[Candidate]:
        own = _ids(own_picks)
        opponent = _ids(opponent_picks)
        normalized_bans = frozenset(_ids(bans))
        target_round = len(own) + 1
        if target_round > 6:
            return []

        candidate_counts: dict[str, int] = defaultdict(int)
        candidate_wins: dict[str, int] = defaultdict(int)
        condition_samples = 0
        unavailable = frozenset(own + tuple(normalized_bans))
        for match in self.repository.recommendation_matches():
            historical_bans = frozenset(
                str(value)
                for value in (match.self_ban, match.opponent_ban)
                if value not in (None, 0)
            )
            if historical_bans != normalized_bans:
                continue
            own_by_round = {
                pick.round: str(pick.shishen_id) for pick in match.picks if pick.side == "self"
            }
            opponent_by_round = {
                pick.round: str(pick.shishen_id) for pick in match.picks if pick.side == "opponent"
            }
            if any(own_by_round.get(index) != shikigami_id for index, shikigami_id in enumerate(own, 1)):
                continue
            if any(
                opponent_by_round.get(index) != shikigami_id
                for index, shikigami_id in enumerate(opponent, 1)
            ):
                continue
            candidate_id = own_by_round.get(target_round)
            if candidate_id is None or candidate_id in unavailable:
                continue
            condition_samples += 1
            candidate_counts[candidate_id] += 1
            if match.result == "win":
                candidate_wins[candidate_id] += 1

        if condition_samples < self.personal_min_samples:
            return []
        candidates = [
            self._candidate(
                shikigami_id,
                source="personal",
                score=candidate_wins[shikigami_id] / count,
                confidence=min(
                    0.99,
                    0.90 + max(0, count - self.personal_min_samples) * 0.005,
                ),
                priority=100,
                sample_size=count,
                reason=f"personal history: {candidate_wins[shikigami_id]}/{count} wins in {condition_samples} matching battles",
            )
            for shikigami_id, count in candidate_counts.items()
        ]
        return sorted(candidates, key=lambda item: (-item["score"], item["shikigami_id"]))

    def external_candidates(
        self,
        *,
        own_picks: Iterable[Any] = (),
        opponent_picks: Iterable[Any] = (),
        bans: Iterable[Any] = (),
    ) -> list[Candidate]:
        own = _ids(own_picks)
        opponent = _ids(opponent_picks)
        if own or opponent:
            return []
        unavailable = frozenset(_ids(bans))
        merged: dict[str, Candidate] = {}

        assist = self.repository.latest_snapshot("assist_first_pick", recommendation=True)
        if isinstance(assist, dict):
            for item in assist.get("my_shishens", []):
                if not isinstance(item, dict):
                    continue
                shikigami_ids = _ids([item.get("shishen_id")])
                if not shikigami_ids or shikigami_ids[0] in unavailable:
                    continue
                total = max(0, int(_number(item.get("total"), 0)))
                candidate = self._candidate(
                    shikigami_ids[0],
                    source="external",
                    score=_rate(item.get("win_rate"), 0.0),
                    confidence=min(0.97, 0.50 + (total / (total + 20) * 0.47 if total else 0.0)),
                    priority=1000,
                    sample_size=total,
                    reason="yysrank first-pick statistics snapshot",
                )
                merged[shikigami_ids[0]] = candidate

        ai = self.repository.latest_snapshot("ai_first_pick", recommendation=True)
        if isinstance(ai, dict):
            for item in ai.get("our_recommendation", []):
                if not isinstance(item, dict):
                    continue
                shikigami_ids = _ids([item.get("raw_id")])
                if not shikigami_ids or shikigami_ids[0] in unavailable:
                    continue
                score = _rate(
                    item.get("expected_win_rate"),
                    _rate(item.get("blended_score"), 0.0),
                )
                candidate = self._candidate(
                    shikigami_ids[0],
                    source="external",
                    score=score,
                    confidence=min(0.97, _rate(item.get("policy_prob"), 0.70)),
                    priority=900,
                    sample_size=0,
                    reason=str(item.get("reason") or "yysrank AI first-pick snapshot"),
                )
                previous = merged.get(shikigami_ids[0])
                if previous is None or (candidate["score"], candidate["confidence"]) > (
                    previous["score"],
                    previous["confidence"],
                ):
                    merged[shikigami_ids[0]] = candidate

        return sorted(merged.values(), key=lambda item: (-item["score"], -item["confidence"], item["shikigami_id"]))

    @staticmethod
    def _candidate(
        shikigami_id: str,
        *,
        source: str,
        score: float,
        confidence: float,
        priority: int,
        sample_size: int,
        reason: str,
    ) -> Candidate:
        return {
            "shikigami_id": str(shikigami_id),
            "source": source,
            "score": max(0.0, min(float(score), 1.0)),
            "confidence": max(0.0, min(float(confidence), 1.0)),
            "priority": int(priority),
            "sample_size": int(sample_size),
            "reason": reason,
        }
