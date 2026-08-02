"""Passive screenshot collection for Duel BP identity training.

The collector intentionally knows nothing about a Device or click controls.
It only receives an already captured image, crops fixed 1280x720 regions, and
writes an append-only manifest under ``log``.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

import numpy as np
from PIL import Image


CAPTURE_PHASES = frozenset(
    {
        "BAN",
        "SELF_PICK",
        "OPPONENT_PICK",
        "READY",
    }
)


@dataclass(frozen=True)
class DuelBPCrop:
    kind: str
    role: str
    slot: int | None
    roi: tuple[int, int, int, int]
    training: bool = True


@dataclass(frozen=True)
class DuelBPCaptureResult:
    captured: bool
    frame_sha256: str | None = None
    crop_count: int = 0
    new_file_count: int = 0


def _top_crops() -> Iterable[DuelBPCrop]:
    for role, base_x in (("self", 97), ("opponent", 829)):
        for index in range(5):
            slot = index + 1
            x = base_x + 72 * index
            yield DuelBPCrop("top_slot", role, slot, (x, 13, 69, 69))
            yield DuelBPCrop(
                "top_identity",
                role,
                slot,
                (x + 3, 16, 63, 48),
            )


def _candidate_crops() -> Iterable[DuelBPCrop]:
    for index in range(9):
        slot = index + 1
        x = 183 + 101 * index
        # The ninth portrait is partly covered by the confirm-button panel.
        # Its narrow name strip remains useful for OCR and manual review.
        if index < 8:
            yield DuelBPCrop(
                "candidate_card",
                "candidate",
                slot,
                (x, 535, 90, 144),
                training=False,
            )
            yield DuelBPCrop(
                "candidate_portrait",
                "candidate",
                slot,
                (x + 21, 538, 67, 120),
            )
        yield DuelBPCrop(
            "candidate_name",
            "candidate",
            slot,
            # Keep a little portrait-side context.  Offline OCR tries several
            # narrower sub-crops; saving only the 20px glyph core can clip the
            # right-hand strokes on cards with a wider red name ribbon.
            (x - 1, 550, 30, 105),
            training=False,
        )


BASE_CROPS = (
    *tuple(_top_crops()),
    DuelBPCrop("ban", "self", 1, (45, 355, 85, 90)),
    DuelBPCrop("ban", "opponent", 1, (1150, 355, 90, 90)),
    DuelBPCrop(
        "selected_name",
        "self",
        None,
        (170, 150, 40, 165),
        training=False,
    ),
)
CANDIDATE_CROPS = tuple(_candidate_crops())


class DuelBPSampleCollector:
    """Store throttled, exact-deduplicated Duel BP frames and crops."""

    def __init__(
        self,
        *,
        config_name: str,
        root: str | Path = "log/duel/bp_samples",
        interval_seconds: float = 1.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if interval_seconds < 0:
            raise ValueError("interval_seconds must not be negative")
        self.config_name = self._safe_component(config_name or "default")
        self.root = Path(root)
        self.interval_seconds = float(interval_seconds)
        self._monotonic = monotonic
        self._last_capture_at: float | None = None
        self._seen_frames: set[str] = set()
        self._seen_samples: set[str] = set()
        self._known_files: set[str] = set()
        self.session_dir: Path | None = None
        self.manifest_path: Path | None = None

    @staticmethod
    def _safe_component(value: Any) -> str:
        text = re.sub(r"[^0-9A-Za-z._-]+", "_", str(value or "")).strip("._")
        return text[:80] or "default"

    @staticmethod
    def _digest(image: np.ndarray) -> str:
        digest = hashlib.sha256()
        digest.update(str(tuple(image.shape)).encode("ascii"))
        digest.update(str(image.dtype).encode("ascii"))
        digest.update(image.tobytes())
        return digest.hexdigest()

    @staticmethod
    def _crop(
        image: np.ndarray,
        roi: tuple[int, int, int, int],
    ) -> np.ndarray:
        x, y, width, height = roi
        return image[y : y + height, x : x + width].copy()

    def start_session(self, session_id: str | None = None) -> Path:
        if session_id is None:
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
            session_id = f"{timestamp}-{uuid.uuid4().hex[:8]}"
        safe_session = self._safe_component(session_id)
        self.session_dir = self.root / self.config_name / safe_session
        self.manifest_path = self.session_dir / "manifest.jsonl"
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self._last_capture_at = None
        self._seen_frames.clear()
        self._seen_samples.clear()
        self._known_files.clear()
        return self.session_dir

    def capture(
        self,
        image: np.ndarray,
        *,
        phase: str,
        confidence: float,
        onmyoji_selection: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> DuelBPCaptureResult:
        phase = str(phase)
        if phase not in CAPTURE_PHASES:
            return DuelBPCaptureResult(False)
        if not isinstance(image, np.ndarray) or image.ndim not in (2, 3):
            raise TypeError("Duel BP capture image must be a numpy array")
        height, width = image.shape[:2]
        if width < 1280 or height < 720:
            raise ValueError(
                f"Duel BP capture requires at least 1280x720, got {width}x{height}"
            )

        if self.session_dir is None or self.manifest_path is None:
            self.start_session()

        now = self._monotonic()
        if (
            self._last_capture_at is not None
            and now - self._last_capture_at < self.interval_seconds
        ):
            return DuelBPCaptureResult(False)
        self._last_capture_at = now

        normalized = np.ascontiguousarray(image[:720, :1280])
        frame_sha256 = self._digest(normalized)
        if frame_sha256 in self._seen_frames:
            return DuelBPCaptureResult(False, frame_sha256=frame_sha256)
        specs = BASE_CROPS
        if not onmyoji_selection:
            specs = (*specs, *CANDIDATE_CROPS)
        crop_data: list[tuple[DuelBPCrop, np.ndarray, str]] = []
        for spec in specs:
            crop = self._crop(normalized, spec.roi)
            crop_sha256 = self._digest(crop)
            crop_data.append((spec, crop, crop_sha256))

        stable_kinds = {
            "top_identity",
            "ban",
            "candidate_portrait",
            "candidate_name",
        }
        sample_payload = {
            "phase": phase,
            "onmyoji_selection": bool(onmyoji_selection),
            "crops": [
                (
                    spec.kind,
                    spec.role,
                    spec.slot,
                    crop_sha256,
                )
                for spec, _, crop_sha256 in crop_data
                if spec.kind in stable_kinds
            ],
        }
        sample_sha256 = hashlib.sha256(
            json.dumps(
                sample_payload,
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        self._seen_frames.add(frame_sha256)
        if sample_sha256 in self._seen_samples:
            return DuelBPCaptureResult(False, frame_sha256=frame_sha256)
        self._seen_samples.add(sample_sha256)

        frame_path, frame_is_new = self._save_image(
            normalized,
            category="frames",
            digest=frame_sha256,
        )
        crop_records: list[dict[str, Any]] = []
        new_file_count = int(frame_is_new)
        for spec, crop, crop_sha256 in crop_data:
            crop_path, is_new = self._save_image(
                crop,
                category=f"crops/{spec.kind}",
                digest=crop_sha256,
            )
            new_file_count += int(is_new)
            crop_records.append(
                {
                    "kind": spec.kind,
                    "role": spec.role,
                    "slot": spec.slot,
                    "roi": list(spec.roi),
                    "training": spec.training,
                    "sha256": crop_sha256,
                    "path": crop_path,
                }
            )

        event = {
            "type": "frame",
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "phase": phase,
            "confidence": max(0.0, min(float(confidence), 1.0)),
            "onmyoji_selection": bool(onmyoji_selection),
            "frame_sha256": frame_sha256,
            "sample_sha256": sample_sha256,
            "frame_path": frame_path,
            "crops": crop_records,
            "metadata": metadata or {},
        }
        with self.manifest_path.open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(event, ensure_ascii=False, separators=(",", ":"))
                + "\n"
            )
        return DuelBPCaptureResult(
            True,
            frame_sha256=frame_sha256,
            crop_count=len(crop_records),
            new_file_count=new_file_count,
        )

    def _save_image(
        self,
        image: np.ndarray,
        *,
        category: str,
        digest: str,
    ) -> tuple[str, bool]:
        assert self.session_dir is not None
        relative_path = Path(category) / f"{digest}.png"
        output_path = self.session_dir / relative_path
        relative_text = relative_path.as_posix()
        if relative_text in self._known_files or output_path.exists():
            self._known_files.add(relative_text)
            return relative_text, False
        output_path.parent.mkdir(parents=True, exist_ok=True)
        Image.fromarray(image).save(output_path)
        self._known_files.add(relative_text)
        return relative_text, True
