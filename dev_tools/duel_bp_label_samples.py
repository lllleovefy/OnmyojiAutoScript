"""Offline OCR labelling for passive Duel BP sample sessions.

Run from the repository root:

    python -m dev_tools.duel_bp_label_samples log/duel/bp_samples/oas2/<session>

The live task only writes screenshots.  This tool performs the slower OCR and
name resolution after a draft, and leaves uncertain cards as ``unresolved``.
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import cv2
import numpy as np

from module.duel_data.repository import DuelRepository
from module.ocr.ppocr import TextSystem
from tasks.Duel.identity import ResolvedIdentity, ShikigamiNameIndex


def normalize_name(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"^(?:sp|ssr|sr|r|n)[路._\-\s]*", "", text)
    return re.sub(r"[^0-9a-z\u3400-\u9fff]", "", text)


def asset_names(
    assets: Iterable[dict[str, Any]],
) -> list[tuple[str, str, str]]:
    names: list[tuple[str, str, str]] = []
    for item in assets:
        if not isinstance(item, dict) or item.get("id") is None:
            continue
        shikigami_id = str(item["id"])
        canonical = str(item.get("name") or "").strip()
        aliases = item.get("alias") or ()
        if isinstance(aliases, str):
            aliases = re.split(r"[,，、;；]+", aliases)
        for value in (canonical, *aliases):
            normalized = normalize_name(value)
            if len(normalized) >= 2:
                names.append((normalized, shikigami_id, canonical))
    return names


def resolve_ocr_text(
    text: str,
    *,
    ocr_score: float,
    index: ShikigamiNameIndex,
    names: Iterable[tuple[str, str, str]],
) -> tuple[ResolvedIdentity | None, str]:
    """Resolve exact substrings first, then use bounded fuzzy matching."""

    normalized = normalize_name(text)
    contained = [
        (len(alias), shikigami_id, canonical)
        for alias, shikigami_id, canonical in names
        if alias in normalized
    ]
    if contained:
        longest = max(item[0] for item in contained)
        best = {
            (shikigami_id, canonical)
            for length, shikigami_id, canonical in contained
            if length == longest
        }
        if len(best) == 1:
            shikigami_id, canonical = best.pop()
            return (
                ResolvedIdentity(
                    shikigami_id=shikigami_id,
                    name=canonical,
                    confidence=max(0.0, min(float(ocr_score), 1.0)),
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
    def rank(self) -> tuple[int, int, float, float, int]:
        return (
            int(self.resolved is not None),
            int(self.method == "substring"),
            self.resolved.confidence if self.resolved is not None else 0.0,
            self.ocr_score,
            len(normalize_name(self.text)),
        )


def recognize_name_crop(
    image: np.ndarray,
    *,
    model: TextSystem,
    index: ShikigamiNameIndex,
    names: list[tuple[str, str, str]],
) -> OcrCandidate:
    height, width = image.shape[:2]
    x_ranges = (
        (min(1, width), min(21, width)),
        (min(1, width), min(25, width)),
        (0, width),
    )
    y_ranges = (
        (0, height),
        (0, min(90, height)),
        (0, min(100, height)),
    )
    candidates: list[OcrCandidate] = []
    for x1, x2 in x_ranges:
        if x2 - x1 < 8:
            continue
        for y1, y2 in y_ranges:
            if y2 - y1 < 40:
                continue
            crop = image[y1:y2, x1:x2]
            for rotation, rotated in (
                ("ccw", np.rot90(crop, 1)),
                ("cw", np.rot90(crop, 3)),
            ):
                enlarged = cv2.resize(
                    rotated,
                    None,
                    fx=4,
                    fy=4,
                    interpolation=cv2.INTER_CUBIC,
                )
                text, score = model.ocr_single_line(enlarged)
                score = float(score)
                resolved, method = resolve_ocr_text(
                    text,
                    ocr_score=score,
                    index=index,
                    names=names,
                )
                candidates.append(
                    OcrCandidate(
                        text=str(text),
                        ocr_score=score,
                        resolved=resolved,
                        method=method,
                        variant=f"x{x1}:{x2}-y{y1}:{y2}-{rotation}",
                    )
                )

    initial_ids = {
        item.resolved.shikigami_id
        for item in candidates
        if item.resolved is not None
    }
    initial_consensus = max(
        (
            sum(
                item.resolved is not None
                and item.resolved.shikigami_id == shikigami_id
                for item in candidates
            )
            for shikigami_id in initial_ids
        ),
        default=0,
    )
    if initial_consensus < 2:
        # The stylized vertical ribbon often defeats line recognition for
        # short names such as 鬼童丸/阎魔.  Detection splits those glyphs into
        # upright boxes; join them from top to bottom.  It is deliberately an
        # offline fallback because detector OCR is much slower.
        for x1, x2, y1, y2 in (
            (0, width, 0, height),
            (0, width, 0, min(65, height)),
            (0, min(25, width), 0, height),
        ):
            detected = model.detect_and_ocr(
                image[y1:y2, x1:x2],
                drop_score=0.05,
                box_thresh=0.4,
                unclip_ratio=1.6,
            )
            detected = sorted(
                (item for item in detected if float(item.score) >= 0.25),
                key=lambda item: float(np.min(item.box[:, 1])),
            )
            primary = []
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
            text = "".join(str(item.ocr_text) for item in primary)
            if not text or not primary:
                continue
            score = min(float(item.score) for item in primary)
            resolved, method = resolve_ocr_text(
                text,
                ocr_score=score,
                index=index,
                names=names,
            )
            candidates.append(
                OcrCandidate(
                    text=text,
                    ocr_score=score,
                    resolved=resolved,
                    method=method,
                    variant=f"detect-x{x1}:{x2}-y{y1}:{y2}",
                )
            )
    resolved_votes: dict[str, set[str]] = {}
    for candidate in candidates:
        if candidate.resolved is None:
            continue
        resolved_votes.setdefault(candidate.resolved.shikigami_id, set()).add(
            candidate.variant
        )
    best = max(candidates, key=lambda item: item.rank)
    if resolved_votes:
        winning_id = max(
            resolved_votes,
            key=lambda shikigami_id: (
                len(resolved_votes[shikigami_id]),
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
        best = OcrCandidate(
            text=best.text,
            ocr_score=best.ocr_score,
            resolved=best.resolved,
            method=best.method,
            variant=best.variant,
            consensus=len(resolved_votes[winning_id]),
        )
    return best


def label_session(
    session_dir: Path,
    *,
    assets: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    manifest_path = session_dir / "manifest.jsonl"
    if not manifest_path.exists():
        raise FileNotFoundError(f"Missing manifest: {manifest_path}")
    if assets is None:
        assets = DuelRepository().latest_snapshot("shishen_assets")
    if not isinstance(assets, list) or not assets:
        raise RuntimeError("No shishen_assets snapshot is available")

    index = ShikigamiNameIndex(assets)
    names = asset_names(assets)
    model = TextSystem()
    labels: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for line in manifest_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        if event.get("onmyoji_selection"):
            continue
        by_slot: dict[int, dict[str, dict[str, Any]]] = {}
        for crop in event.get("crops", ()):
            if crop.get("role") != "candidate" or crop.get("slot") is None:
                continue
            by_slot.setdefault(int(crop["slot"]), {})[str(crop["kind"])] = crop
        for slot, crops in sorted(by_slot.items()):
            name_crop = crops.get("candidate_name")
            if name_crop is None or crops.get("candidate_portrait") is None:
                continue
            dedupe_key = (str(name_crop["sha256"]), slot)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            name_path = session_dir / str(name_crop["path"])
            image = cv2.imread(str(name_path), cv2.IMREAD_COLOR)
            if image is None:
                continue
            candidate = recognize_name_crop(
                image,
                model=model,
                index=index,
                names=names,
            )
            resolved = candidate.resolved
            status = (
                "auto"
                if resolved is not None and candidate.consensus >= 2
                else "unresolved"
            )
            labels.append(
                {
                    "frame_sha256": event.get("frame_sha256"),
                    "slot": slot,
                    "status": status,
                    "ocr_text": candidate.text,
                    "ocr_score": candidate.ocr_score,
                    "ocr_variant": candidate.variant,
                    "resolve_method": candidate.method,
                    "consensus": candidate.consensus,
                    "resolved_id": (
                        resolved.shikigami_id if resolved is not None else None
                    ),
                    "resolved_name": (
                        resolved.name if resolved is not None else None
                    ),
                    "resolve_confidence": (
                        resolved.confidence if resolved is not None else None
                    ),
                    "name_path": name_crop["path"],
                    "portrait_path": (
                        crops.get("candidate_portrait", {}).get("path")
                    ),
                    "card_path": crops.get("candidate_card", {}).get("path"),
                }
            )

    output_path = session_dir / "labels.jsonl"
    output_path.write_text(
        "".join(
            json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n"
            for item in labels
        ),
        encoding="utf-8",
    )
    return labels


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("session", type=Path)
    args = parser.parse_args()
    labels = label_session(args.session)
    auto = sum(item["status"] == "auto" for item in labels)
    unresolved = len(labels) - auto
    print(
        json.dumps(
            {
                "session": str(args.session),
                "labels": len(labels),
                "auto": auto,
                "unresolved": unresolved,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
