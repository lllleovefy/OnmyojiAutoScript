"""Pure geometry and ranking helpers for the current 1280x720 Duel BP UI."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

import numpy as np
from PIL import Image


CANDIDATE_SLOT_COUNT = 8
CANDIDATE_BASE_X = 183
CANDIDATE_STEP_X = 101
CANDIDATE_CARD_Y = 535
CANDIDATE_CARD_WIDTH = 90
CANDIDATE_CARD_HEIGHT = 144
CANDIDATE_PORTRAIT_OFFSET_X = 21
CANDIDATE_PORTRAIT_Y = 538
CANDIDATE_PORTRAIT_WIDTH = 67
CANDIDATE_PORTRAIT_HEIGHT = 120
CANDIDATE_NAME_OFFSET_X = -1
CANDIDATE_NAME_Y = 550
CANDIDATE_NAME_WIDTH = 30
CANDIDATE_NAME_HEIGHT = 105
CANDIDATE_CLICK_Y = 610

# This strip contains every complete candidate card throughout a horizontal
# roster scan. Its dimensions are intentionally divisible by the block size
# used by ``candidate_region_visual_fingerprint``.
CANDIDATE_FINGERPRINT_ROI = (125, 535, 976, 144)
CANDIDATE_FINGERPRINT_BLOCK = (8, 16)

SELECTED_NAME_ROI = (170, 150, 40, 165)
# During the short reveal after both sides have locked their current pick, the
# opponent uses the mirrored vertical name ribbon on the right half.  This is
# intentionally a glyph-core crop rather than the full decorative placard.
OPPONENT_REVEAL_NAME_ROI = (680, 150, 40, 165)

OPPONENT_SLOT_CENTERS = tuple((863 + 72 * index, 48) for index in range(5))
OPPONENT_IDENTITY_ROIS = tuple(
    (832 + 72 * index, 16, 63, 48) for index in range(5)
)

# The current roster is displayed in this order even though the public enum
# keeps the two Minamoto/hero entries first for historical compatibility.
ONMYOJI_SLOT_BY_ENUM_NAME = {
    "SEIMI": 1,
    "KAGURA": 2,
    "HIROMASA": 3,
    "YAO_BIKUNI": 4,
    "YORIMITSU": 5,
    "MICHINAGA": 6,
}
ONMYOJI_SLOT_CENTERS = (
    (250, 613),
    (404, 613),
    (558, 613),
    (712, 613),
    (865, 613),
    (1018, 613),
)
ONMYOJI_ROSTER_CARD_ROIS = tuple(
    (193 + 153 * index, 548, 139, 172) for index in range(6)
)
ONMYOJI_ROSTER_IDENTITY_ROIS = tuple(
    (251 + 153 * index, 554, 74, 150) for index in range(6)
)
# The selected Onmyoji is rendered as the large character on the left.  Card
# artwork alone does not prove which roster item is active, so all six
# target-specific templates use this common identity ROI.
ONMYOJI_SELECTED_IDENTITY_ROI = (250, 125, 340, 415)

REFERENCE_SCREEN_SIZE = (1280, 720)


def candidate_card_roi(
    slot: int,
    *,
    base_x: int = CANDIDATE_BASE_X,
) -> tuple[int, int, int, int]:
    index = _candidate_index(slot)
    return (
        int(base_x) + CANDIDATE_STEP_X * index,
        CANDIDATE_CARD_Y,
        CANDIDATE_CARD_WIDTH,
        CANDIDATE_CARD_HEIGHT,
    )


def candidate_portrait_roi(
    slot: int,
    *,
    base_x: int = CANDIDATE_BASE_X,
) -> tuple[int, int, int, int]:
    x, _, _, _ = candidate_card_roi(slot, base_x=base_x)
    return (
        x + CANDIDATE_PORTRAIT_OFFSET_X,
        CANDIDATE_PORTRAIT_Y,
        CANDIDATE_PORTRAIT_WIDTH,
        CANDIDATE_PORTRAIT_HEIGHT,
    )


def candidate_name_roi(
    slot: int,
    *,
    base_x: int = CANDIDATE_BASE_X,
) -> tuple[int, int, int, int]:
    x, _, _, _ = candidate_card_roi(slot, base_x=base_x)
    return (
        x + CANDIDATE_NAME_OFFSET_X,
        CANDIDATE_NAME_Y,
        CANDIDATE_NAME_WIDTH,
        CANDIDATE_NAME_HEIGHT,
    )


def candidate_click_point(
    slot: int,
    *,
    base_x: int = CANDIDATE_BASE_X,
) -> tuple[int, int]:
    x, _, width, _ = candidate_card_roi(slot, base_x=base_x)
    return (x + width // 2, CANDIDATE_CLICK_Y)


def detect_candidate_base_x(
    frame: np.ndarray,
    *,
    minimum_x: int = 125,
    maximum_x: int = 225,
    fallback_x: int = CANDIDATE_BASE_X,
) -> int:
    """Find the first complete card from the roster's 101 px edge period."""

    if frame.shape[0] < 679 or frame.shape[1] < 1100:
        raise ValueError("candidate baseline detection requires 1280x720")
    strip = frame[535:679, :1100]
    if strip.ndim == 3:
        pixels = strip.astype(np.float32)
        gray = (
            pixels[:, :, 0] * 0.114
            + pixels[:, :, 1] * 0.587
            + pixels[:, :, 2] * 0.299
        )
    else:
        gray = strip.astype(np.float32)
    vertical_edges = np.mean(np.abs(np.diff(gray, axis=1)), axis=0)

    def peak(center: int) -> float:
        left = max(0, center - 1)
        right = min(len(vertical_edges), center + 2)
        return (
            float(np.sum(vertical_edges[left:right]))
            if right > left
            else 0.0
        )

    scored: list[tuple[float, int]] = []
    for base in range(minimum_x, maximum_x + 1):
        expected_edges = []
        for index in range(CANDIDATE_SLOT_COUNT):
            left = base + CANDIDATE_STEP_X * index
            right = left + CANDIDATE_CARD_WIDTH - 1
            if right >= len(vertical_edges):
                break
            # Besides the two outer card edges, the candidate layout has a
            # stable boundary between the narrow vertical name ribbon and the
            # portrait.  During a horizontal swipe the outer-edge-only score
            # can lock onto the portrait/next-ribbon phase (roughly 14--21 px
            # to the right), which makes both OCR and clicks use the wrong
            # card.  The third expected edge disambiguates the two phases.
            expected_edges.append(
                peak(left)
                + peak(left + CANDIDATE_PORTRAIT_OFFSET_X)
                + peak(right)
            )
        if expected_edges:
            score = float(np.median(expected_edges)) + 0.25 * float(
                np.mean(expected_edges)
            )
            scored.append((score, base))
    if not scored:
        return fallback_x
    best_score = max(row[0] for row in scored)
    # A scrolled roster can expose two nearly identical periodic edge phases.
    # The nominal card/name edge is the leftmost of those near-ties; choosing
    # it keeps the documented name and portrait offsets aligned.
    near_best = [
        row for row in scored if row[0] >= best_score * 0.99
    ]
    _, best_x = min(
        near_best,
        key=lambda row: (row[1], abs(row[1] - fallback_x)),
    )
    median_score = float(np.median([row[0] for row in scored]))
    if best_score < 3.0 or best_score < median_score * 1.15:
        return fallback_x
    return best_x


def crop_xywh(image, roi: tuple[int, int, int, int]):
    """Return an XYWH crop without silently accepting an invalid screenshot."""

    x, y, width, height = roi
    if image is None or len(getattr(image, "shape", ())) < 2:
        raise ValueError("Duel BP crop requires an image array")
    image_height, image_width = image.shape[:2]
    if (
        x < 0
        or y < 0
        or width <= 0
        or height <= 0
        or x + width > image_width
        or y + height > image_height
    ):
        raise ValueError(
            f"Duel BP ROI {roi!r} is outside {image_width}x{image_height}"
        )
    return image[y : y + height, x : x + width]


def scale_reference_roi(
    roi: tuple[int, int, int, int],
    image_shape: object,
    *,
    reference_size: tuple[int, int] = REFERENCE_SCREEN_SIZE,
) -> tuple[int, int, int, int]:
    """Scale a 1280x720 ROI onto a same-aspect or letterboxed frame.

    Coordinates use floor for the leading edge and ceil for the trailing
    edge, so scaling never clips a border glyph.  The fit is centred for
    screenshots that include a thin emulator/recording letterbox.
    """

    shape = getattr(image_shape, "shape", image_shape)
    if not isinstance(shape, Sequence) or len(shape) < 2:
        raise ValueError("Duel BP scaling requires an image shape")
    image_height = int(shape[0])
    image_width = int(shape[1])
    reference_width, reference_height = reference_size
    if image_height <= 0 or image_width <= 0:
        raise ValueError("Duel BP scaling requires a non-empty image")
    if reference_width <= 0 or reference_height <= 0:
        raise ValueError("Duel BP reference size must be positive")

    scale = min(
        image_width / float(reference_width),
        image_height / float(reference_height),
    )
    offset_x = (image_width - reference_width * scale) / 2.0
    offset_y = (image_height - reference_height * scale) / 2.0
    x, y, width, height = roi
    left = math.floor(offset_x + x * scale)
    top = math.floor(offset_y + y * scale)
    right = math.ceil(offset_x + (x + width) * scale)
    bottom = math.ceil(offset_y + (y + height) * scale)
    return left, top, right - left, bottom - top


def crop_reference_roi(
    image: np.ndarray,
    roi: tuple[int, int, int, int],
    *,
    reference_size: tuple[int, int] = REFERENCE_SCREEN_SIZE,
) -> np.ndarray:
    """Crop a reference ROI and normalize it back to its authored size."""

    scaled_roi = scale_reference_roi(
        roi,
        image,
        reference_size=reference_size,
    )
    crop = crop_xywh(image, scaled_roi)
    target_width, target_height = roi[2], roi[3]
    if crop.shape[:2] == (target_height, target_width):
        return crop
    resized = Image.fromarray(crop).resize(
        (target_width, target_height),
        Image.Resampling.BICUBIC,
    )
    return np.ascontiguousarray(resized)


@dataclass(frozen=True)
class VisibleCandidate:
    slot: int
    shikigami_id: str
    name: str
    confidence: float
    disabled: bool = False
    source: str = "name"
    base_x: int = CANDIDATE_BASE_X

    @property
    def click_point(self) -> tuple[int, int]:
        return candidate_click_point(self.slot, base_x=self.base_x)


def choose_visible_candidate(
    ranked_ids: Sequence[str],
    candidates: Iterable[VisibleCandidate],
    *,
    unavailable_ids: Iterable[str] = (),
    allowed_ids: Iterable[str] | None = None,
) -> VisibleCandidate | None:
    """Choose the highest-ranked verified, enabled card on the current page."""

    unavailable = {str(value) for value in unavailable_ids}
    allowed = (
        None
        if allowed_ids is None
        else {str(value) for value in allowed_ids}
    )
    by_id: dict[str, VisibleCandidate] = {}
    for candidate in candidates:
        shikigami_id = str(candidate.shikigami_id)
        if (
            candidate.disabled
            or shikigami_id in unavailable
            or (allowed is not None and shikigami_id not in allowed)
        ):
            continue
        previous = by_id.get(shikigami_id)
        if previous is None or candidate.confidence > previous.confidence:
            by_id[shikigami_id] = candidate
    for shikigami_id in ranked_ids:
        candidate = by_id.get(str(shikigami_id))
        if candidate is not None:
            return candidate
    return None


def candidate_page_fingerprint(
    candidates: Iterable[VisibleCandidate],
    frame: np.ndarray | None = None,
) -> tuple[
    tuple[tuple[int, str, bool], ...],
    str | None,
]:
    """Combine recognized IDs with a stable raw roster-region fingerprint.

    The identity component keeps the original order-independent page key.
    The visual component distinguishes pages even when every OCR/template
    lookup is empty. Small pixel noise is removed by block averaging and
    16-level luminance quantization, while a scrolling transition remains a
    distinct page instead of being mistaken for the preceding empty page.
    """

    identity_fingerprint = tuple(
        sorted(
            (
                int(candidate.slot),
                str(candidate.shikigami_id),
                bool(candidate.disabled),
            )
            for candidate in candidates
        )
    )
    visual_fingerprint = (
        candidate_region_visual_fingerprint(frame)
        if frame is not None
        else None
    )
    return identity_fingerprint, visual_fingerprint


def candidate_region_visual_fingerprint(frame: np.ndarray) -> str:
    """Return a deterministic perceptual digest for the raw candidate strip."""

    crop = crop_xywh(frame, CANDIDATE_FINGERPRINT_ROI)
    if crop.ndim == 3:
        pixels = crop[:, :, :3].astype(np.float32)
        gray = (
            pixels[:, :, 0] * 0.114
            + pixels[:, :, 1] * 0.587
            + pixels[:, :, 2] * 0.299
        )
    else:
        gray = crop.astype(np.float32)
    block_height, block_width = CANDIDATE_FINGERPRINT_BLOCK
    rows = gray.shape[0] // block_height
    columns = gray.shape[1] // block_width
    means = gray.reshape(
        rows,
        block_height,
        columns,
        block_width,
    ).mean(axis=(1, 3))
    quantized = np.rint(means / 16.0).astype(np.uint8)
    return hashlib.sha256(quantized.tobytes()).hexdigest()[:24]


def onmyoji_click_point(onmyoji: object) -> tuple[int, int]:
    enum_name = getattr(onmyoji, "name", str(onmyoji))
    try:
        slot = ONMYOJI_SLOT_BY_ENUM_NAME[str(enum_name)]
    except KeyError as exc:
        raise ValueError(f"Unsupported Duel Onmyoji: {enum_name!r}") from exc
    return ONMYOJI_SLOT_CENTERS[slot - 1]


def _candidate_index(slot: int) -> int:
    try:
        normalized = int(slot)
    except (TypeError, ValueError) as exc:
        raise ValueError("Duel BP candidate slot must be an integer") from exc
    if not 1 <= normalized <= CANDIDATE_SLOT_COUNT:
        raise ValueError(
            f"Duel BP candidate slot must be 1..{CANDIDATE_SLOT_COUNT}"
        )
    return normalized - 1
