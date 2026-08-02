"""Extract a local Duel portrait library from recorded BP video/samples.

Typical usage from the repository root::

    python -m dev_tools.duel_extract_portraits log/式神素材视频.mp4

Use ``--analyze-only`` to run OCR and report coverage without writing images,
the database, manifests, or the library index.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import shutil
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator

import numpy as np

from module.duel_data.repository import (
    DEFAULT_DATABASE_PATH,
    DuelRepository,
)
from tasks.Duel.name_recognition import StrictNameRecognizer
from tasks.Duel.portrait_library import (
    PortraitLibrary,
    PortraitMatcher,
    image_phash,
    image_sha256,
    load_image,
    phash_distance,
    save_image,
    visual_similarity,
)
from tasks.Duel.selection import (
    CANDIDATE_BASE_X as DEFAULT_CARD_BASE_X,
    CANDIDATE_STEP_X as CARD_PERIOD,
    detect_candidate_base_x,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LIBRARY_PATH = PROJECT_ROOT / "config" / "duel" / "portrait_library"
CANDIDATE_STABLE_ROI = (170, 525, 1045, 170)
MAX_GRID_BOXES = 24
GRID_EMITTED_PHASH_DISTANCE = 8
UNRESOLVED_PHASH_DISTANCE = 6
UNRESOLVED_VISUAL_SIMILARITY = 0.985


@dataclass(frozen=True)
class CandidateCropSpec:
    slot: int
    card: tuple[int, int, int, int]
    portrait: tuple[int, int, int, int]
    name: tuple[int, int, int, int]


@dataclass(frozen=True)
class ExtractionStats:
    sampled_frames: int
    stable_frames: int
    labelled_candidates: int
    unresolved_candidates: int
    templates_created: int
    templates_deduplicated: int
    grid_templates_created: int


def candidate_crop_specs(
    count: int = 8,
    *,
    base_x: int = DEFAULT_CARD_BASE_X,
) -> tuple[CandidateCropSpec, ...]:
    """1280x720 BP candidate ROIs for a detected horizontal-list phase.

    The ninth card is hidden by the confirmation panel, so automatic portrait
    extraction intentionally stops at eight complete cards.
    """

    if not 1 <= count <= 8:
        raise ValueError("candidate crop count must be between 1 and 8")
    if not 0 <= base_x <= 1280:
        raise ValueError("candidate card base must be inside the frame")
    rows = []
    for index in range(count):
        x = base_x + CARD_PERIOD * index
        rows.append(
            CandidateCropSpec(
                slot=index + 1,
                card=(x, 535, 90, 144),
                portrait=(x + 21, 538, 67, 120),
                name=(x - 1, 550, 30, 105),
            )
        )
    return tuple(rows)


def crop_roi(
    image: np.ndarray,
    roi: tuple[int, int, int, int],
) -> np.ndarray:
    x, y, width, height = roi
    return np.ascontiguousarray(image[y : y + height, x : x + width]).copy()


def _box_iou(
    left: tuple[int, int, int, int],
    right: tuple[int, int, int, int],
) -> float:
    lx, ly, lw, lh = left
    rx, ry, rw, rh = right
    x1, y1 = max(lx, rx), max(ly, ry)
    x2, y2 = min(lx + lw, rx + rw), min(ly + lh, ry + rh)
    intersection = max(0, x2 - x1) * max(0, y2 - y1)
    union = lw * lh + rw * rh - intersection
    return intersection / union if union else 0.0


def deduplicate_grid_boxes(
    boxes: Iterable[tuple[int, int, int, int]],
    *,
    max_boxes: int = MAX_GRID_BOXES,
) -> list[tuple[int, int, int, int]]:
    """NMS nested contour boxes and cap work before template matching."""

    if max_boxes < 1:
        raise ValueError("max_boxes must be positive")
    ranked = sorted(
        boxes,
        key=lambda box: (box[2] * box[3], box[3], box[2]),
        reverse=True,
    )
    selected: list[tuple[int, int, int, int]] = []
    for candidate in ranked:
        x, y, width, height = candidate
        centre = (x + width / 2, y + height / 2)
        duplicate = False
        for existing in selected:
            ex, ey, ew, eh = existing
            existing_centre = (ex + ew / 2, ey + eh / 2)
            if (
                _box_iou(candidate, existing) >= 0.45
                or (
                    abs(centre[0] - existing_centre[0]) <= 8
                    and abs(centre[1] - existing_centre[1]) <= 8
                )
            ):
                duplicate = True
                break
        if not duplicate:
            selected.append(candidate)
        if len(selected) >= max_boxes:
            break
    return sorted(selected, key=lambda box: (box[1], box[0]))


def is_ascii_path(path: str | Path) -> bool:
    try:
        str(path).encode("ascii")
        return True
    except UnicodeEncodeError:
        return False


@contextlib.contextmanager
def unicode_safe_video_path(
    source: str | Path,
    *,
    force_copy: bool = False,
) -> Iterator[Path]:
    """Yield an ASCII-only path for OpenCV builds that reject Unicode paths."""

    path = Path(source).resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    if is_ascii_path(path) and not force_copy:
        yield path
        return
    with tempfile.TemporaryDirectory(prefix="duel_video_") as temp:
        copied = Path(temp) / f"source{path.suffix.lower() or '.mp4'}"
        shutil.copyfile(path, copied)
        yield copied


class StableFrameGate:
    """Require stable sampled frames and emit each settled roster once."""

    def __init__(
        self,
        *,
        required_frames: int = 3,
        difference_threshold: float = 2.5,
        emitted_phash_distance: int = 2,
        roi: tuple[int, int, int, int] = CANDIDATE_STABLE_ROI,
    ) -> None:
        if required_frames < 1:
            raise ValueError("required_frames must be positive")
        self.required_frames = required_frames
        self.difference_threshold = float(difference_threshold)
        self.emitted_phash_distance = int(emitted_phash_distance)
        self.roi = roi
        self._previous: np.ndarray | None = None
        self._stable_count = 0
        self._last_emitted_hash: str | None = None

    def update(self, frame: np.ndarray) -> bool:
        region = crop_roi(frame, self.roi)
        gray = self._gray_small(region)
        if self._previous is None:
            self._stable_count = 1
        else:
            difference = float(
                np.mean(
                    np.abs(
                        gray.astype(np.float32)
                        - self._previous.astype(np.float32)
                    )
                )
            )
            self._stable_count = (
                self._stable_count + 1
                if difference <= self.difference_threshold
                else 1
            )
        self._previous = gray
        if self._stable_count < self.required_frames:
            return False
        fingerprint = image_phash(region)
        if (
            self._last_emitted_hash is not None
            and phash_distance(fingerprint, self._last_emitted_hash)
            <= self.emitted_phash_distance
        ):
            return False
        self._last_emitted_hash = fingerprint
        return True

    @staticmethod
    def _gray_small(image: np.ndarray) -> np.ndarray:
        from PIL import Image

        if image.ndim == 3:
            source = image[:, :, ::-1]
        else:
            source = image
        return np.asarray(
            Image.fromarray(source).convert("L").resize(
                (160, 28),
                Image.Resampling.BILINEAR,
            )
        )


class VideoPortraitExtractor:
    def __init__(
        self,
        *,
        assets: Iterable[dict[str, Any]],
        ocr_model: Any,
        library_path: str | Path = DEFAULT_LIBRARY_PATH,
        database_path: str | Path | None = DEFAULT_DATABASE_PATH,
        sample_fps: float = 10.0,
        candidate_start: float = 11.0,
        candidate_end: float = 79.0,
        grid_start: float = 83.0,
        grid_end: float | None = None,
    ) -> None:
        self.assets = sorted(
            (dict(item) for item in assets),
            key=lambda item: int(item["id"]),
        )
        if not self.assets:
            raise ValueError("shishen assets must not be empty")
        if sample_fps <= 0:
            raise ValueError("sample_fps must be positive")
        self.ocr_model = ocr_model
        self.recognizer = StrictNameRecognizer(self.assets)
        self.library = PortraitLibrary(
            library_path,
            database_path=database_path,
        )
        self.library_path = Path(library_path)
        self.sample_fps = float(sample_fps)
        self.candidate_start = float(candidate_start)
        self.candidate_end = float(candidate_end)
        self.grid_start = float(grid_start)
        self.grid_end = grid_end
        self._unresolved: list[dict[str, Any]] = []
        self._unresolved_keys: set[tuple[str, str, str]] = set()
        self._unresolved_visual: dict[
            str, list[tuple[str, np.ndarray]]
        ] = {}

    def extract(
        self,
        video_path: str | Path,
        *,
        analyze_only: bool = False,
    ) -> dict[str, Any]:
        self._begin_unresolved_run(analyze_only=analyze_only)
        cv2 = self._cv2()
        stats = {
            "sampled_frames": 0,
            "stable_frames": 0,
            "labelled_candidates": 0,
            "unresolved_candidates": 0,
            "templates_created": 0,
            "templates_deduplicated": 0,
            "grid_templates_created": 0,
        }
        candidate_gate = StableFrameGate()
        grid_gate = StableFrameGate(
            roi=(140, 90, 1000, 560),
            emitted_phash_distance=GRID_EMITTED_PHASH_DISTANCE,
        )
        with unicode_safe_video_path(video_path) as safe_path:
            capture = cv2.VideoCapture(str(safe_path))
            if not capture.isOpened():
                raise RuntimeError(f"cannot open video: {video_path}")
            try:
                source_fps = float(capture.get(cv2.CAP_PROP_FPS) or 0)
                if source_fps <= 0:
                    raise RuntimeError("video FPS is unavailable")
                frame_index = -1
                next_sample_time = 0.0
                grid_matcher: PortraitMatcher | None = None
                while True:
                    ok, frame = capture.read()
                    if not ok:
                        break
                    frame_index += 1
                    timestamp = frame_index / source_fps
                    if timestamp + 1e-9 < next_sample_time:
                        continue
                    while next_sample_time <= timestamp + 1e-9:
                        next_sample_time += 1.0 / self.sample_fps
                    stats["sampled_frames"] += 1
                    if frame.shape[0] < 720 or frame.shape[1] < 1280:
                        raise ValueError(
                            "portrait video requires at least 1280x720 frames"
                        )
                    frame = np.ascontiguousarray(frame[:720, :1280])
                    if self.candidate_start <= timestamp <= self.candidate_end:
                        if candidate_gate.update(frame):
                            stats["stable_frames"] += 1
                            self._process_candidate_frame(
                                frame,
                                timestamp=timestamp,
                                frame_index=frame_index,
                                analyze_only=analyze_only,
                                stats=stats,
                            )
                    elif timestamp >= self.grid_start and (
                        self.grid_end is None or timestamp <= self.grid_end
                    ):
                        if grid_gate.update(frame):
                            stats["stable_frames"] += 1
                            if grid_matcher is None:
                                grid_matcher = PortraitMatcher(
                                    self.library_path
                                )
                            self._process_grid_frame(
                                frame,
                                timestamp=timestamp,
                                frame_index=frame_index,
                                matcher=grid_matcher,
                                analyze_only=analyze_only,
                                stats=stats,
                            )
            finally:
                capture.release()

        return self._finish(
            source=str(Path(video_path)),
            analyze_only=analyze_only,
            stats=stats,
        )

    def import_sample_session(
        self,
        session_path: str | Path,
        *,
        analyze_only: bool = False,
    ) -> dict[str, Any]:
        """Import only strict ``auto`` labels from a passive sample session."""

        self._begin_unresolved_run(analyze_only=analyze_only)
        session = Path(session_path)
        labels_path = session / "labels.jsonl"
        manifest_path = session / "manifest.jsonl"
        if not labels_path.exists():
            raise FileNotFoundError(labels_path)
        labels = [
            json.loads(line)
            for line in labels_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        events = {
            str(event.get("frame_sha256")): event
            for event in (
                json.loads(line)
                for line in manifest_path.read_text(
                    encoding="utf-8"
                ).splitlines()
                if line.strip()
            )
        } if manifest_path.exists() else {}
        stats = {
            "sampled_frames": len(events),
            "stable_frames": len(events),
            "labelled_candidates": 0,
            "unresolved_candidates": 0,
            "templates_created": 0,
            "templates_deduplicated": 0,
            "grid_templates_created": 0,
        }
        for label in sorted(
            labels,
            key=lambda item: (
                str(item.get("frame_sha256") or ""),
                int(item.get("slot") or 0),
            ),
        ):
            if (
                label.get("status") != "auto"
                or not label.get("resolved_id")
                or not label.get("portrait_path")
            ):
                stats["unresolved_candidates"] += 1
                continue
            portrait_path = session / str(label["portrait_path"])
            if not portrait_path.exists():
                stats["unresolved_candidates"] += 1
                continue
            portrait = load_image(portrait_path)
            result = self.library.add_template(
                portrait,
                shikigami_id=label["resolved_id"],
                name=str(label.get("resolved_name") or ""),
                view="候选",
                source="sample_session",
                confidence=float(label.get("resolve_confidence") or 0),
                analyze_only=analyze_only,
            )
            stats["labelled_candidates"] += 1
            stats[
                "templates_created"
                if result.created
                else "templates_deduplicated"
            ] += 1
            self._associate_sample_top_views(
                session,
                label,
                events.get(str(label.get("frame_sha256"))),
                portrait,
                analyze_only=analyze_only,
                stats=stats,
            )
        return self._finish(
            source=str(session),
            analyze_only=analyze_only,
            stats=stats,
        )

    def _process_candidate_frame(
        self,
        frame: np.ndarray,
        *,
        timestamp: float,
        frame_index: int,
        analyze_only: bool,
        stats: dict[str, int],
    ) -> None:
        base_x = detect_candidate_base_x(frame)
        for spec in candidate_crop_specs(base_x=base_x):
            portrait = crop_roi(frame, spec.portrait)
            name_crop = crop_roi(frame, spec.name)
            if float(np.std(portrait)) < 7.0:
                continue
            candidate = self.recognizer.recognize(
                name_crop,
                self.ocr_model,
            )
            if candidate.accepted and candidate.resolved is not None:
                result = self.library.add_template(
                    portrait,
                    shikigami_id=candidate.resolved.shikigami_id,
                    name=candidate.resolved.name,
                    view="候选",
                    source="video_ocr",
                    confidence=candidate.resolved.confidence,
                    analyze_only=analyze_only,
                )
                stats["labelled_candidates"] += 1
                stats[
                    "templates_created"
                    if result.created
                    else "templates_deduplicated"
                ] += 1
            else:
                stats["unresolved_candidates"] += 1
                self._record_unresolved(
                    portrait,
                    name_crop=name_crop,
                    timestamp=timestamp,
                    frame_index=frame_index,
                    slot=spec.slot,
                    candidate=candidate,
                    analyze_only=analyze_only,
                )

    def _process_grid_frame(
        self,
        frame: np.ndarray,
        *,
        timestamp: float,
        frame_index: int,
        matcher: PortraitMatcher,
        analyze_only: bool,
        stats: dict[str, int],
    ) -> None:
        portraits = self._detect_grid_portraits(frame)
        matches = matcher.match_many(portraits, views=("候选",))
        for slot, (portrait, matched) in enumerate(
            zip(portraits, matches),
            1,
        ):
            if matched.shikigami_id is None:
                self._record_unresolved(
                    portrait,
                    name_crop=None,
                    timestamp=timestamp,
                    frame_index=frame_index,
                    slot=slot,
                    candidate=None,
                    view="阵容",
                    highest_candidate=matched.highest_candidate,
                    analyze_only=analyze_only,
                )
                continue
            result = self.library.add_template(
                portrait,
                shikigami_id=matched.shikigami_id,
                name=str(matched.name or ""),
                view="阵容",
                source="video_visual_link",
                confidence=matched.confidence,
                analyze_only=analyze_only,
            )
            if result.created:
                stats["grid_templates_created"] += 1
                stats["templates_created"] += 1
            else:
                stats["templates_deduplicated"] += 1

    @staticmethod
    def _detect_grid_portraits(frame: np.ndarray) -> list[np.ndarray]:
        """Detect portrait-like bordered cells; never assigns identities."""

        cv2 = VideoPortraitExtractor._cv2()
        x0, y0, width, height = (140, 90, 1000, 560)
        region = crop_roi(frame, (x0, y0, width, height))
        gray = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY)
        edges = cv2.Canny(gray, 70, 160)
        contours, _ = cv2.findContours(
            edges,
            cv2.RETR_LIST,
            cv2.CHAIN_APPROX_SIMPLE,
        )
        boxes: list[tuple[int, int, int, int]] = []
        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            if not (48 <= w <= 120 and 55 <= h <= 150):
                continue
            if not (0.55 <= w / h <= 1.25):
                continue
            boxes.append((x, y, w, h))
        boxes = deduplicate_grid_boxes(boxes)
        return [
            crop_roi(region, (x + 2, y + 2, max(1, w - 4), max(1, h - 4)))
            for x, y, w, h in boxes
        ]

    def _associate_sample_top_views(
        self,
        session: Path,
        label: dict[str, Any],
        event: dict[str, Any] | None,
        candidate_portrait: np.ndarray,
        *,
        analyze_only: bool,
        stats: dict[str, int],
    ) -> None:
        """Create deployed views only after a >=.98 visual association."""

        if event is None:
            return
        # Compare directly against the candidate; no identity is inferred
        # from slot order or side.
        candidate_hash = image_phash(candidate_portrait)
        for crop in event.get("crops", ()):
            if crop.get("kind") != "top_identity" or not crop.get("path"):
                continue
            path = session / str(crop["path"])
            if not path.exists():
                continue
            top = load_image(path)
            distance = phash_distance(candidate_hash, image_phash(top))
            confidence = (
                0.45 * (1.0 - distance / 64.0)
                + 0.55 * visual_similarity(candidate_portrait, top)
            )
            if confidence < 0.98:
                continue
            result = self.library.add_template(
                top,
                shikigami_id=label["resolved_id"],
                name=str(label.get("resolved_name") or ""),
                view="上阵",
                source="sample_visual_link",
                confidence=confidence,
                analyze_only=analyze_only,
            )
            if result.created:
                stats["grid_templates_created"] += 1
                stats["templates_created"] += 1
            else:
                stats["templates_deduplicated"] += 1

    def _record_unresolved(
        self,
        portrait: np.ndarray,
        *,
        name_crop: np.ndarray | None,
        timestamp: float,
        frame_index: int,
        slot: int,
        candidate: Any,
        analyze_only: bool,
        view: str = "候选",
        highest_candidate: dict[str, Any] | None = None,
    ) -> None:
        portrait_hash = image_sha256(portrait)
        portrait_phash = image_phash(portrait)
        name_hash = image_sha256(name_crop) if name_crop is not None else ""
        stem = (
            f"{int(round(timestamp * 1000)):09d}-"
            f"{frame_index:06d}-slot{slot:02d}-{portrait_hash[:12]}"
        )
        portrait_path = Path("_unresolved") / f"{stem}-portrait.png"
        name_path = (
            Path("_unresolved") / f"{stem}-name.png"
            if name_crop is not None
            else None
        )
        confidence = self._unresolved_confidence(
            candidate=candidate,
            highest_candidate=highest_candidate,
        )
        key = (view, portrait_hash, name_hash)
        if key in self._unresolved_keys:
            # The first occurrence already owns the persisted path and DB
            # row. Updating it to this later timestamp would point at a file
            # that is intentionally not written for the duplicate.
            return
        if self._is_unresolved_visual_duplicate(
            view=view,
            portrait=portrait,
            portrait_phash=portrait_phash,
        ):
            return
        self._unresolved_keys.add(key)
        self._unresolved_visual.setdefault(view, []).append(
            (portrait_phash, portrait.copy())
        )
        item = {
            "timestamp": round(float(timestamp), 3),
            "frame_index": int(frame_index),
            "slot": int(slot),
            "view": view,
            "portrait_path": portrait_path.as_posix(),
            "portrait_sha256": portrait_hash,
            "portrait_phash": portrait_phash,
            "name_path": name_path.as_posix() if name_path else None,
            "name_sha256": name_hash or None,
            "ocr_text": getattr(candidate, "text", None),
            "ocr_score": getattr(candidate, "ocr_score", None),
            "ocr_variant": getattr(candidate, "variant", None),
            "resolve_method": getattr(candidate, "method", None),
            "consensus": getattr(candidate, "consensus", 0),
            "highest_candidate": highest_candidate,
            "confidence": confidence,
            "source": "video_unresolved",
        }
        self._unresolved.append(item)
        if not analyze_only:
            save_image(self.library_path / portrait_path, portrait)
            if name_path is not None and name_crop is not None:
                save_image(self.library_path / name_path, name_crop)
            self.library.register_unresolved(
                relative_path=portrait_path,
                view=view,
                sha256=portrait_hash,
                source="video_unresolved",
                confidence=confidence,
            )

    def _begin_unresolved_run(self, *, analyze_only: bool) -> None:
        self._unresolved.clear()
        self._unresolved_keys.clear()
        self._unresolved_visual.clear()
        if analyze_only:
            return
        for item in self._read_unresolved_manifest():
            item = dict(item)
            portrait_hash = str(item.get("portrait_sha256") or "")
            name_hash = str(item.get("name_sha256") or "")
            view = str(item.get("view") or "候选")
            if not portrait_hash:
                continue
            relative_path = str(item.get("portrait_path") or "")
            portrait: np.ndarray | None = None
            if relative_path:
                try:
                    portrait = load_image(self.library_path / relative_path)
                except (FileNotFoundError, OSError):
                    portrait = None
            portrait_phash = str(item.get("portrait_phash") or "")
            if not portrait_phash and portrait is not None:
                portrait_phash = image_phash(portrait)
                item["portrait_phash"] = portrait_phash
            if (
                portrait is not None
                and portrait_phash
                and self._is_unresolved_visual_duplicate(
                    view=view,
                    portrait=portrait,
                    portrait_phash=portrait_phash,
                )
            ):
                continue
            self._unresolved.append(item)
            self._unresolved_keys.add((view, portrait_hash, name_hash))
            if portrait is not None and portrait_phash:
                self._unresolved_visual.setdefault(view, []).append(
                    (portrait_phash, portrait)
                )
            if relative_path:
                self.library.register_unresolved(
                    relative_path=relative_path,
                    view=view,
                    sha256=portrait_hash,
                    source=str(item.get("source") or "video_unresolved"),
                    confidence=float(
                        item.get("confidence")
                        or self._unresolved_confidence(
                            candidate=None,
                            highest_candidate=item.get(
                                "highest_candidate"
                            ),
                            ocr_score=item.get("ocr_score"),
                        )
                    ),
                )

    def _is_unresolved_visual_duplicate(
        self,
        *,
        view: str,
        portrait: np.ndarray,
        portrait_phash: str,
    ) -> bool:
        for existing_phash, existing_image in self._unresolved_visual.get(
            view, ()
        ):
            if (
                phash_distance(portrait_phash, existing_phash)
                <= UNRESOLVED_PHASH_DISTANCE
                and visual_similarity(portrait, existing_image)
                >= UNRESOLVED_VISUAL_SIMILARITY
            ):
                return True
        return False

    @staticmethod
    def _unresolved_confidence(
        *,
        candidate: Any,
        highest_candidate: dict[str, Any] | None,
        ocr_score: Any = None,
    ) -> float:
        scores = [float(ocr_score or 0)]
        if candidate is not None:
            scores.append(float(getattr(candidate, "ocr_score", 0) or 0))
            resolved = getattr(candidate, "resolved", None)
            scores.append(float(getattr(resolved, "confidence", 0) or 0))
        if isinstance(highest_candidate, dict):
            scores.append(float(highest_candidate.get("confidence") or 0))
        return max(0.0, min(max(scores), 1.0))

    def _read_unresolved_manifest(self) -> list[dict[str, Any]]:
        path = self.library_path / "unresolved.jsonl"
        if not path.exists():
            return []
        rows: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                rows.append(item)
        return rows

    def _finish(
        self,
        *,
        source: str,
        analyze_only: bool,
        stats: dict[str, int],
    ) -> dict[str, Any]:
        coverage = self.library.coverage_report(self.assets)
        result = {
            "source": source,
            "analyze_only": bool(analyze_only),
            "stats": asdict(ExtractionStats(**stats)),
            "coverage": coverage,
            "unresolved_count": len(self._unresolved),
        }
        if not analyze_only:
            self.library_path.mkdir(parents=True, exist_ok=True)
            unresolved_path = self.library_path / "unresolved.jsonl"
            merged: dict[tuple[str, str, str], dict[str, Any]] = {}
            for item in self._unresolved:
                key = (
                    str(item.get("view") or ""),
                    str(item.get("portrait_sha256") or ""),
                    str(item.get("name_sha256") or ""),
                )
                merged.setdefault(key, item)
            unresolved_rows = sorted(
                merged.values(),
                key=lambda row: (
                    float(row.get("timestamp") or 0),
                    int(row.get("frame_index") or 0),
                    int(row.get("slot") or 0),
                    str(row.get("portrait_sha256") or ""),
                ),
            )
            unresolved_path.write_text(
                "".join(
                    json.dumps(
                        item,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n"
                    for item in unresolved_rows
                ),
                encoding="utf-8",
            )
            result["unresolved_count"] = len(unresolved_rows)
            self.library.write_coverage_report(self.assets)
        return result

    @staticmethod
    def _cv2() -> Any:
        try:
            import cv2
        except ImportError as exc:
            raise RuntimeError(
                "opencv-python is required to extract portrait videos"
            ) from exc
        return cv2


def _load_assets(path: Path | None) -> list[dict[str, Any]]:
    if path is None:
        assets = DuelRepository().latest_snapshot("shishen_assets")
    else:
        assets = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(assets, list) or not assets:
        raise RuntimeError("No shishen_assets snapshot is available")
    return assets


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("video", type=Path, nargs="?")
    parser.add_argument("--sample-session", type=Path)
    parser.add_argument("--output", type=Path, default=DEFAULT_LIBRARY_PATH)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    parser.add_argument("--assets-json", type=Path)
    parser.add_argument("--sample-fps", type=float, default=10.0)
    parser.add_argument("--candidate-start", type=float, default=11.0)
    parser.add_argument("--candidate-end", type=float, default=79.0)
    parser.add_argument("--grid-start", type=float, default=83.0)
    parser.add_argument("--grid-end", type=float)
    parser.add_argument("--analyze-only", action="store_true")
    args = parser.parse_args(argv)
    if args.video is None and args.sample_session is None:
        parser.error("video or --sample-session is required")

    if args.video is not None:
        from module.ocr.ppocr import TextSystem

        ocr_model: Any = TextSystem()
    else:
        # A labelled passive session does not perform OCR again.
        class _UnusedOcr:
            def ocr_single_line(self, image: np.ndarray) -> tuple[str, float]:
                return "", 0.0

            def detect_and_ocr(
                self, image: np.ndarray, **kwargs: Any
            ) -> tuple[Any, ...]:
                return ()

        ocr_model = _UnusedOcr()

    extractor = VideoPortraitExtractor(
        assets=_load_assets(args.assets_json),
        ocr_model=ocr_model,
        library_path=args.output,
        database_path=args.database,
        sample_fps=args.sample_fps,
        candidate_start=args.candidate_start,
        candidate_end=args.candidate_end,
        grid_start=args.grid_start,
        grid_end=args.grid_end,
    )
    reports = []
    if args.video is not None:
        reports.append(
            extractor.extract(
                args.video,
                analyze_only=args.analyze_only,
            )
        )
    if args.sample_session is not None:
        reports.append(
            extractor.import_sample_session(
                args.sample_session,
                analyze_only=args.analyze_only,
            )
        )
    print(
        json.dumps(
            reports[0] if len(reports) == 1 else reports,
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
