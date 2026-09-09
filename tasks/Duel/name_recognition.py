"""Strict shikigami name recognition shared by Duel runtime and dev tools.

The BP UI renders names vertically and with stylised rarity/level decorations.
This module deliberately favours an unresolved result over a plausible but
wrong identity: automatic labels require two independent crop/rotation
variants to resolve to the same shikigami.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any, Iterable, Protocol

import numpy as np
from PIL import Image

from tasks.Duel.identity import ResolvedIdentity, ShikigamiNameIndex


class NameOcrModel(Protocol):
    def ocr_single_line(self, image: np.ndarray) -> tuple[str, float]:
        ...

    def detect_and_ocr(self, image: np.ndarray, **kwargs: Any) -> Iterable[Any]:
        ...


def normalize_name(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"^(?:sp|ssr|sr|r|n)[级階阶_：:\-\s]*", "", text)
    return re.sub(r"[^0-9a-z\u3400-\u9fff]", "", text)


def asset_names(
    assets: Iterable[dict[str, Any]],
) -> list[tuple[str, str, str]]:
    """Return normalized (alias, id, canonical-name) rows."""

    names: list[tuple[str, str, str]] = []
    for item in assets:
        if not isinstance(item, dict) or item.get("id") is None:
            continue
        shikigami_id = str(item["id"])
        canonical = str(item.get("name") or "").strip()
        aliases = item.get("alias") or ()
        if isinstance(aliases, str):
            aliases = re.split(r"[,，、;；|/]+", aliases)
        elif not isinstance(aliases, (list, tuple, set)):
            aliases = ()
        for value in (canonical, *aliases):
            normalized = normalize_name(value)
            if len(normalized) >= 2:
                names.append((normalized, shikigami_id, canonical))
    return names


def normalize_shishen_assets(payload: Any) -> list[dict[str, Any]]:
    """Normalize the public snapshot wrappers used by import and runtime."""

    if isinstance(payload, dict):
        payload = next(
            (
                payload[key]
                for key in ("items", "data", "list")
                if isinstance(payload.get(key), list)
            ),
            [],
        )
    if isinstance(payload, (str, bytes)):
        return []
    try:
        return [dict(item) for item in payload if isinstance(item, dict)]
    except TypeError:
        return []


def resolve_ocr_text(
    text: str,
    *,
    ocr_score: float,
    index: ShikigamiNameIndex,
    names: Iterable[tuple[str, str, str]],
) -> tuple[ResolvedIdentity | None, str]:
    """Resolve exact substrings first, then a tightly bounded fuzzy match."""

    normalized = normalize_name(text)
    name_rows = list(names)
    def is_ambiguous_base(alias: str, shikigami_id: str) -> bool:
        return any(
            other_id != shikigami_id
            and len(other_alias) > len(alias)
            and alias in other_alias
            for other_alias, other_id, _ in name_rows
        )

    exact = {
        (alias, shikigami_id, canonical)
        for alias, shikigami_id, canonical in name_rows
        if alias and alias == normalized
    }
    if len(exact) == 1:
        alias, shikigami_id, canonical = exact.pop()
        # An exact full-name crop remains authoritative even when the same
        # name is embedded in a newer identity. Decorated/partial substring
        # results below still reject that ambiguous base form.
        return (
            ResolvedIdentity(
                shikigami_id=shikigami_id,
                name=canonical,
                confidence=max(0.0, min(float(ocr_score), 1.0)),
            ),
            "exact",
        )

    contained = [
        (len(alias), alias, shikigami_id, canonical)
        for alias, shikigami_id, canonical in name_rows
        if alias and alias in normalized
    ]
    if contained:
        longest = max(row[0] for row in contained)
        identities = {
            (alias, shikigami_id, canonical)
            for length, alias, shikigami_id, canonical in contained
            if length == longest
        }
        if len(identities) == 1:
            alias, shikigami_id, canonical = identities.pop()
            # Base forms such as 桃花妖/犬神/妖刀姬 are embedded in newer
            # canonical identities. If OCR also contains surrounding CJK
            # glyphs but did not recognize a longer registered alias, choosing
            # the base ID is unsafe; leave the crop unresolved instead.
            ambiguous_base = is_ambiguous_base(alias, shikigami_id)
            if not ambiguous_base:
                return (
                    ResolvedIdentity(
                        shikigami_id=shikigami_id,
                        name=canonical,
                        confidence=max(
                            0.0, min(float(ocr_score), 1.0)
                        ),
                    ),
                    "substring",
                )

    resolved = index.resolve(
        text,
        detector_confidence=ocr_score,
        fuzzy_threshold=0.90,
        fuzzy_margin=0.08,
    )
    return resolved, "fuzzy" if resolved is not None else "unresolved"


@dataclass(frozen=True)
class OcrCandidate:
    text: str
    ocr_score: float
    resolved: ResolvedIdentity | None
    method: str
    variant: str
    consensus: int = 0

    @property
    def accepted(self) -> bool:
        return self.resolved is not None and self.consensus >= 2

    @property
    def rank(self) -> tuple[int, int, float, float, int]:
        return (
            int(self.resolved is not None),
            {
                "exact": 2,
                "substring": 1,
                "fuzzy": 0,
            }.get(self.method, -1),
            self.resolved.confidence if self.resolved is not None else 0.0,
            self.ocr_score,
            len(normalize_name(self.text)),
        )


def _resize_four_times(image: np.ndarray) -> np.ndarray:
    height, width = image.shape[:2]
    if image.ndim == 3:
        rgb = np.ascontiguousarray(image[:, :, ::-1])
        resized = Image.fromarray(rgb).resize(
            (max(1, width * 4), max(1, height * 4)),
            Image.Resampling.BICUBIC,
        )
        return np.asarray(resized)[:, :, ::-1].copy()
    resized = Image.fromarray(image).resize(
        (max(1, width * 4), max(1, height * 4)),
        Image.Resampling.BICUBIC,
    )
    return np.asarray(resized)


class StrictNameRecognizer:
    """OCR adapter with strict alias resolution and multi-variant consensus."""

    def __init__(self, assets: Any) -> None:
        self.assets = normalize_shishen_assets(assets)
        if not self.assets:
            raise ValueError("shishen assets must not be empty")
        self.index = ShikigamiNameIndex(self.assets)
        self.names = asset_names(self.assets)

    def resolve_text(
        self,
        text: str,
        *,
        ocr_score: float = 1.0,
    ) -> tuple[ResolvedIdentity | None, str]:
        return resolve_ocr_text(
            text,
            ocr_score=ocr_score,
            index=self.index,
            names=self.names,
        )

    def recognize(
        self,
        image: np.ndarray,
        model: NameOcrModel,
        *,
        min_consensus: int = 2,
    ) -> OcrCandidate:
        if not isinstance(image, np.ndarray) or image.ndim not in (2, 3):
            raise TypeError("name crop must be a numpy image")
        if min_consensus not in (1, 2):
            raise ValueError("min_consensus must be 1 or 2")
        height, width = image.shape[:2]
        candidates: list[OcrCandidate] = []
        # The card can stop between its original fixed positions after a
        # horizontal swipe.  Narrow glyph-core variants are therefore tried
        # before the wider ribbon crops, which avoids borrowing decorations
        # or vertical text from the neighbouring card.
        x_ranges = tuple(
            dict.fromkeys(
                (
                    (min(2, width), min(18, width)),
                    (min(3, width), min(20, width)),
                    (min(1, width), min(21, width)),
                    (min(1, width), min(25, width)),
                    (0, width),
                )
            )
        )
        # The UI always lays names top-to-bottom. Rotating counter-clockwise
        # produces the model's expected left-to-right text; trying the inverse
        # direction and every redundant height made one 108-second video take
        # tens of minutes without adding reliable labels. Evaluate the full
        # height first, then a decoration-trimmed crop only when needed, and
        # stop as soon as the caller's consensus contract is satisfied.
        for y1, y2 in ((0, height),):
            if y2 - y1 < 40:
                continue
            for x1, x2 in x_ranges:
                if x2 - x1 < 8:
                    continue
                crop = image[y1:y2, x1:x2]
                candidates.append(
                    self._recognize_crop(
                        crop,
                        model,
                        variant=f"x{x1}:{x2}-y{y1}:{y2}-ccw",
                    )
                )
                votes = self._resolved_votes(candidates)
                if max(
                    (len(items) for items in votes.values()),
                    default=0,
                ) >= min_consensus:
                    winner = self._winning_candidate(candidates, votes)
                    if winner.accepted or winner.method == "exact":
                        return winner

        if not candidates:
            return OcrCandidate("", 0.0, None, "unresolved", "none")

        votes = self._resolved_votes(candidates)
        # Remove level/rarity/skin decorations from both ends.  These are
        # genuinely different crops for the two-vote contract; grayscale or
        # threshold preprocessing of the same crop never creates extra votes.
        for y1, y2 in (
            (4, height - 4),
            (8, height - 8),
            (0, min(90, height)),
        ):
            if y2 - y1 >= 40:
                for x1, x2 in x_ranges:
                    if x2 - x1 < 8:
                        continue
                    candidates.append(
                        self._recognize_crop(
                            image[y1:y2, x1:x2],
                            model,
                            variant=(
                                f"x{x1}:{x2}-y{y1}:{y2}-ccw"
                            ),
                        )
                    )
                    votes = self._resolved_votes(candidates)
                    if max(
                        (len(items) for items in votes.values()),
                        default=0,
                    ) >= min_consensus:
                        winner = self._winning_candidate(candidates, votes)
                        if winner.accepted or winner.method == "exact":
                            return winner
            if votes:
                break

        if votes:
            self._append_detector_candidates(
                image,
                model,
                candidates,
                max_variants=1,
            )
            votes = self._resolved_votes(candidates)

        if not votes:
            return max(candidates, key=lambda item: item.rank)
        return self._winning_candidate(candidates, votes)

    def _recognize_crop(
        self,
        crop: np.ndarray,
        model: NameOcrModel,
        *,
        variant: str,
    ) -> OcrCandidate:
        rotated = _resize_four_times(np.rot90(crop, 1))
        attempts: list[OcrCandidate] = []
        gray = None
        for preprocessing in ("original", "gray", "otsu"):
            if preprocessing == "original":
                prepared = rotated
            else:
                if gray is None:
                    if rotated.ndim == 3:
                        pixels = rotated.astype(np.float32)
                        gray = np.clip(
                            pixels[:, :, 0] * 0.114
                            + pixels[:, :, 1] * 0.587
                            + pixels[:, :, 2] * 0.299,
                            0,
                            255,
                        ).astype(np.uint8)
                    else:
                        gray = rotated.astype(np.uint8)
                if preprocessing == "gray":
                    prepared_gray = gray
                else:
                    threshold = self._otsu_threshold(gray)
                    prepared_gray = np.where(
                        gray >= threshold, 255, 0
                    ).astype(np.uint8)
                prepared = np.repeat(
                    prepared_gray[:, :, None], 3, axis=2
                )
            text, raw_score = model.ocr_single_line(prepared)
            score = float(raw_score)
            if not np.isfinite(score):
                score = 0.0
            resolved, method = self.resolve_text(
                str(text),
                ocr_score=score,
            )
            attempts.append(
                OcrCandidate(
                    text=str(text),
                    ocr_score=score,
                    resolved=resolved,
                    method=method,
                    variant=variant,
                )
            )
            if resolved is not None and method == "exact":
                break
        return max(attempts, key=lambda item: item.rank)

    @staticmethod
    def _otsu_threshold(gray: np.ndarray) -> int:
        histogram = np.bincount(gray.ravel(), minlength=256).astype(
            np.float64
        )
        total = float(gray.size)
        sum_total = float(np.dot(np.arange(256), histogram))
        background_weight = 0.0
        background_sum = 0.0
        best_threshold = 127
        best_variance = -1.0
        for threshold, count in enumerate(histogram):
            background_weight += float(count)
            if background_weight <= 0:
                continue
            foreground_weight = total - background_weight
            if foreground_weight <= 0:
                break
            background_sum += threshold * float(count)
            background_mean = background_sum / background_weight
            foreground_mean = (
                sum_total - background_sum
            ) / foreground_weight
            variance = (
                background_weight
                * foreground_weight
                * (background_mean - foreground_mean) ** 2
            )
            if variance > best_variance:
                best_variance = variance
                best_threshold = threshold
        return best_threshold

    @staticmethod
    def _winning_candidate(
        candidates: list[OcrCandidate],
        votes: dict[str, set[str]],
    ) -> OcrCandidate:
        winning_id = max(
            votes,
            key=lambda shikigami_id: (
                len(votes[shikigami_id]),
                max(
                    item.rank
                    for item in candidates
                    if item.resolved is not None
                    and item.resolved.shikigami_id == shikigami_id
                ),
            ),
        )
        best = max(
            (
                item
                for item in candidates
                if item.resolved is not None
                and item.resolved.shikigami_id == winning_id
            ),
            key=lambda item: item.rank,
        )
        return replace(best, consensus=len(votes[winning_id]))

    @staticmethod
    def _resolved_votes(
        candidates: Iterable[OcrCandidate],
    ) -> dict[str, set[str]]:
        votes: dict[str, set[str]] = {}
        for candidate in candidates:
            if candidate.resolved is not None:
                votes.setdefault(
                    candidate.resolved.shikigami_id, set()
                ).add(candidate.variant)
        return votes

    def _append_detector_candidates(
        self,
        image: np.ndarray,
        model: NameOcrModel,
        candidates: list[OcrCandidate],
        *,
        max_variants: int = 3,
    ) -> None:
        detector = getattr(model, "detect_and_ocr", None)
        if detector is None:
            return
        height, width = image.shape[:2]
        for variant_index, (x1, x2, y1, y2) in enumerate((
            (0, width, 0, height),
            (0, width, 0, min(65, height)),
            (0, min(25, width), 0, height),
        )):
            if variant_index >= max_variants:
                break
            detected = detector(
                image[y1:y2, x1:x2],
                drop_score=0.05,
                box_thresh=0.4,
                unclip_ratio=1.6,
            )
            detected = sorted(
                (item for item in detected if float(item.score) >= 0.25),
                key=lambda item: float(np.min(item.box[:, 1])),
            )
            primary: list[Any] = []
            previous_bottom: float | None = None
            for item in detected:
                top = float(np.min(item.box[:, 1]))
                bottom = float(np.max(item.box[:, 1]))
                if previous_bottom is not None and top > previous_bottom + 12:
                    break
                primary.append(item)
                previous_bottom = (
                    bottom
                    if previous_bottom is None
                    else max(previous_bottom, bottom)
                )
            if not primary:
                continue
            text = "".join(str(item.ocr_text) for item in primary)
            score = min(float(item.score) for item in primary)
            resolved, method = self.resolve_text(text, ocr_score=score)
            candidates.append(
                OcrCandidate(
                    text=text,
                    ocr_score=score,
                    resolved=resolved,
                    method=method,
                    variant=f"detect-x{x1}:{x2}-y{y1}:{y2}",
                )
            )


def recognize_name_crop(
    image: np.ndarray,
    *,
    model: NameOcrModel,
    index: ShikigamiNameIndex,
    names: list[tuple[str, str, str]],
) -> OcrCandidate:
    """Compatibility entry point for the original offline label tool."""

    assets: dict[str, dict[str, Any]] = {}
    for _, shikigami_id, canonical in names:
        assets.setdefault(
            shikigami_id,
            {"id": shikigami_id, "name": canonical, "alias": ""},
        )
    recognizer = StrictNameRecognizer(assets.values())
    # Preserve the exact caller-provided index/name aliases.
    recognizer.index = index
    recognizer.names = names
    return recognizer.recognize(image, model)
