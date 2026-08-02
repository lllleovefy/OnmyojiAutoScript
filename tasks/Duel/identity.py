"""Independent shikigami identity adapter for Duel BP screenshots.

The detector deliberately consumes generic OAS detections instead of the
YysRank local service or its encrypted models.  The existing ``oashya``
tracker can feed it at runtime, while tests and future detectors can provide
the same small tuple contract.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Callable, Iterable


DEFAULT_IDENTITY_REGIONS = {
    # Spatial slots are fixed at 1280x720.  Our side is the blue left half and
    # is always numbered left-to-right 1..5, independent of pick chronology.
    "self": [96, 10, 360, 78],
    "opponent": [827, 10, 360, 78],
    "self_ban": [45, 355, 85, 90],
    "opponent_ban": [1150, 355, 90, 90],
}
DEFAULT_IDENTITY_REGIONS_JSON = json.dumps(
    DEFAULT_IDENTITY_REGIONS,
    separators=(",", ":"),
)


def _normalize_name(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"^(?:sp|ssr|sr|r|n)[·._\-\s]*", "", text)
    return re.sub(r"[^0-9a-z\u3400-\u9fff]", "", text)


@dataclass(frozen=True)
class ResolvedIdentity:
    shikigami_id: str
    name: str
    confidence: float


class ShikigamiNameIndex:
    """Resolve detector/OCR names to the imported raw shikigami IDs."""

    def __init__(self, assets: Iterable[dict[str, Any]]) -> None:
        self._names: dict[str, list[tuple[str, str]]] = {}
        for item in assets:
            if not isinstance(item, dict) or item.get("id") is None:
                continue
            shikigami_id = str(item["id"])
            canonical = str(item.get("name") or "").strip()
            aliases = item.get("alias") or ""
            if isinstance(aliases, str):
                alias_values = re.split(r"[,，、;；]", aliases)
            elif isinstance(aliases, (list, tuple, set)):
                alias_values = list(aliases)
            else:
                alias_values = []
            for value in (canonical, *alias_values):
                normalized = _normalize_name(value)
                if normalized:
                    self._names.setdefault(normalized, []).append(
                        (shikigami_id, canonical or str(value))
                    )

    def resolve(
        self,
        name: Any,
        *,
        detector_confidence: float,
        fuzzy_threshold: float = 0.84,
        fuzzy_margin: float = 0.05,
    ) -> ResolvedIdentity | None:
        normalized = _normalize_name(name)
        if not normalized or not self._names:
            return None
        confidence = max(0.0, min(float(detector_confidence), 1.0))
        exact = self._names.get(normalized, [])
        exact_ids = {item[0] for item in exact}
        if len(exact_ids) == 1:
            shikigami_id, canonical = exact[0]
            return ResolvedIdentity(shikigami_id, canonical, confidence)

        scored: dict[str, tuple[float, str]] = {}
        for candidate, entries in self._names.items():
            similarity = SequenceMatcher(None, normalized, candidate).ratio()
            for shikigami_id, canonical in entries:
                current = scored.get(shikigami_id)
                if current is None or similarity > current[0]:
                    scored[shikigami_id] = (similarity, canonical)
        ranked = sorted(
            ((score, shikigami_id, canonical) for shikigami_id, (score, canonical) in scored.items()),
            reverse=True,
        )
        if not ranked or ranked[0][0] < fuzzy_threshold:
            return None
        if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < fuzzy_margin:
            return None
        similarity, shikigami_id, canonical = ranked[0]
        return ResolvedIdentity(
            shikigami_id,
            canonical,
            min(confidence, similarity),
        )


@dataclass(frozen=True)
class DuelIdentityObservation:
    own_picks: tuple[str, ...] = ()
    opponent_picks: tuple[str, ...] = ()
    bans: tuple[str, ...] = ()
    confidence: float | None = None
    # Locked spatial slots, always left-to-right.  Chronological own_picks are
    # reversed because the current celebrity UI locks our slots 5 -> 1.
    own_slots: tuple[str, ...] = ()
    opponent_slots: tuple[str, ...] = ()
    # Visible slots include unconfirmed auto-entry candidates and preserve
    # empty positions.  They are diagnostic/verification data, never match
    # completion evidence by themselves.
    own_visible_slots: tuple[str | None, ...] = ()
    opponent_visible_slots: tuple[str | None, ...] = ()


def auto_identity_is_complete(
    *,
    state: str,
    previous_state: str,
    observation: DuelIdentityObservation,
    previous_own_picks: tuple[str, ...] = (),
    previous_opponent_picks: tuple[str, ...] = (),
    previous_bans: tuple[str, ...] = (),
    is_celeb: bool = False,
) -> bool:
    """Conservatively prove that an AUTO observation contains every slot.

    Detector confidence only describes boxes that were found.  It says nothing
    about a missing box, so AUTO additionally verifies the exact pick-count
    change implied by a phase transition.  Ambiguous shapes fail closed.
    """
    own = observation.own_picks
    opponent = observation.opponent_picks
    bans = observation.bans
    if (
        len(own) < len(previous_own_picks)
        or len(opponent) < len(previous_opponent_picks)
        or len(bans) < len(previous_bans)
    ):
        return False

    if is_celeb and state != "BAN" and len(bans) < 2:
        return False

    def extends_once(
        current: tuple[str, ...], previous: tuple[str, ...]
    ) -> bool:
        if len(current) != len(previous) + 1:
            return False
        current_counts = Counter(current)
        return all(
            current_counts[shikigami_id] >= count
            for shikigami_id, count in Counter(previous).items()
        )

    if state == previous_state:
        return (
            own == previous_own_picks
            and opponent == previous_opponent_picks
            and bans == previous_bans
        )

    if previous_state == "BAN" and state in ("SELF_PICK", "OPPONENT_PICK"):
        # At the first pick phase there must not already be an unexplained pick.
        return (
            own == previous_own_picks
            and opponent == previous_opponent_picks
            and (not is_celeb or len(bans) == 2)
        )

    if previous_state == "SELF_PICK" and state == "OPPONENT_PICK":
        return (
            extends_once(own, previous_own_picks)
            and opponent == previous_opponent_picks
            and bans == previous_bans
        )

    if previous_state == "OPPONENT_PICK" and state == "SELF_PICK":
        return (
            own == previous_own_picks
            and extends_once(opponent, previous_opponent_picks)
            and bans == previous_bans
        )

    if state == "READY":
        if previous_state == "SELF_PICK":
            return (
                extends_once(own, previous_own_picks)
                and opponent == previous_opponent_picks
                and bans == previous_bans
            )
        if previous_state == "OPPONENT_PICK":
            return (
                own == previous_own_picks
                and extends_once(opponent, previous_opponent_picks)
                and bans == previous_bans
            )
    return False


def parse_identity_regions(value: str | dict[str, Any] | None) -> dict[str, tuple[int, int, int, int]]:
    if value in (None, ""):
        raw: Any = DEFAULT_IDENTITY_REGIONS
    elif isinstance(value, str):
        raw = json.loads(value)
    else:
        raw = value
    if not isinstance(raw, dict):
        raise ValueError("Duel identity regions must be a JSON object")
    regions: dict[str, tuple[int, int, int, int]] = {}
    for key in ("self", "opponent", "self_ban", "opponent_ban"):
        roi = raw.get(key)
        if roi is None:
            continue
        if not isinstance(roi, (list, tuple)) or len(roi) != 4:
            raise ValueError(f"Duel identity region {key} must contain x, y, width, height")
        x, y, width, height = (int(item) for item in roi)
        if x < 0 or y < 0 or width <= 0 or height <= 0:
            raise ValueError(f"Duel identity region {key} is invalid")
        regions[key] = (x, y, width, height)
    return regions


class DuelIdentityAdapter:
    """Map tracker detections to ordered BP slots and imported raw IDs."""

    def __init__(
        self,
        name_index: ShikigamiNameIndex,
        *,
        name_resolver: Callable[[int], str],
        regions: dict[str, tuple[int, int, int, int]],
        minimum_confidence: float = 0.90,
    ) -> None:
        if not 0 <= minimum_confidence <= 1:
            raise ValueError("minimum_confidence must be between 0 and 1")
        self.name_index = name_index
        self.name_resolver = name_resolver
        self.regions = regions
        self.minimum_confidence = minimum_confidence

    @staticmethod
    def _inside(cx: int, cy: int, roi: tuple[int, int, int, int]) -> bool:
        x, y, width, height = roi
        return x <= cx < x + width and y <= cy < y + height

    def recognize(
        self,
        detections: Iterable[tuple[Any, ...]],
        *,
        state: str,
        own_locked_slots: int | None = None,
        opponent_locked_slots: int | None = None,
    ) -> DuelIdentityObservation:
        resolved: list[tuple[int, int, ResolvedIdentity]] = []
        for detection in detections:
            if len(detection) < 7:
                continue
            try:
                class_id = int(detection[1])
                confidence = float(detection[2])
                cx, cy = int(detection[3]), int(detection[4])
            except (TypeError, ValueError):
                continue
            if confidence < self.minimum_confidence:
                continue
            try:
                name = self.name_resolver(class_id)
            except Exception:
                continue
            identity = self.name_index.resolve(
                name,
                detector_confidence=confidence,
            )
            if identity is not None:
                resolved.append((cx, cy, identity))

        own_visible = self._slot_identities(resolved, "self", slot_count=5)
        opponent_visible = self._slot_identities(
            resolved, "opponent", slot_count=5
        )
        if own_locked_slots is None:
            own_locked = own_visible
        else:
            locked_count = max(0, min(int(own_locked_slots), 5))
            own_locked = own_visible[5 - locked_count :] if locked_count else ()
        if opponent_locked_slots is None:
            opponent_locked = opponent_visible
        else:
            locked_count = max(0, min(int(opponent_locked_slots), 5))
            opponent_locked = opponent_visible[:locked_count]

        own_slots = tuple(
            identity.shikigami_id for identity in own_locked if identity is not None
        )
        opponent_slots = tuple(
            identity.shikigami_id
            for identity in opponent_locked
            if identity is not None
        )
        # Spatial self slots are 1..5 left-to-right.  The observed celebrity
        # flow locks them 5..1, so chronology is derived rather than changing
        # the spatial contract.
        own = tuple(reversed(own_slots))
        opponent = opponent_slots

        self_ban = self._ordered_identities(
            resolved, "self_ban", reverse=True
        )
        opponent_ban = self._ordered_identities(
            resolved, "opponent_ban", reverse=False
        )
        ban_identities = tuple((*self_ban[:1], *opponent_ban[:1]))
        bans = tuple(identity.shikigami_id for identity in ban_identities)

        used_identities = [
            identity
            for identity in (*own_locked, *opponent_locked, *ban_identities)
            if identity is not None
        ]
        return DuelIdentityObservation(
            own_picks=own[:6],
            opponent_picks=opponent[:6],
            bans=bans,
            confidence=(
                min(identity.confidence for identity in used_identities)
                if used_identities
                else None
            ),
            own_slots=own_slots,
            opponent_slots=opponent_slots,
            own_visible_slots=tuple(
                identity.shikigami_id if identity is not None else None
                for identity in own_visible
            ),
            opponent_visible_slots=tuple(
                identity.shikigami_id if identity is not None else None
                for identity in opponent_visible
            ),
        )

    def _ordered_identities(
        self,
        resolved: Iterable[tuple[int, int, ResolvedIdentity]],
        region_name: str,
        *,
        reverse: bool,
    ) -> tuple[ResolvedIdentity, ...]:
        roi = self.regions.get(region_name)
        if roi is None:
            return ()
        matches = [
            (cx, identity)
            for cx, cy, identity in resolved
            if self._inside(cx, cy, roi)
        ]
        matches.sort(key=lambda item: item[0], reverse=reverse)
        return tuple(identity for _, identity in matches)

    def _slot_identities(
        self,
        resolved: Iterable[tuple[int, int, ResolvedIdentity]],
        region_name: str,
        *,
        slot_count: int,
    ) -> tuple[ResolvedIdentity | None, ...]:
        roi = self.regions.get(region_name)
        if roi is None:
            return tuple(None for _ in range(slot_count))
        x, _, width, _ = roi
        slot_width = width / slot_count
        slots: list[ResolvedIdentity | None] = [None] * slot_count
        for cx, cy, identity in resolved:
            if not self._inside(cx, cy, roi):
                continue
            index = min(slot_count - 1, max(0, int((cx - x) / slot_width)))
            previous = slots[index]
            if previous is None or identity.confidence > previous.confidence:
                slots[index] = identity
        return tuple(slots)
