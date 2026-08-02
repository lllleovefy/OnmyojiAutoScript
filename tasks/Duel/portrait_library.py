"""Local Duel portrait template storage and runtime matching."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import threading
import time
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

import numpy as np
from PIL import Image


VIEW_ALIASES = {
    "candidate": "候选",
    "lineup": "阵容",
    "deployed": "上阵",
    "候选": "候选",
    "阵容": "阵容",
    "上阵": "上阵",
}
VALID_VIEWS = frozenset(VIEW_ALIASES.values())
_LIBRARY_LOCKS: dict[Path, threading.RLock] = {}
_LIBRARY_LOCKS_GUARD = threading.Lock()


def _library_thread_lock(library_path: Path) -> threading.RLock:
    key = library_path.resolve()
    with _LIBRARY_LOCKS_GUARD:
        return _LIBRARY_LOCKS.setdefault(key, threading.RLock())


@contextlib.contextmanager
def _library_write_lock(
    library_path: Path,
    *,
    timeout: float = 30.0,
):
    """Serialize index/sequence writes across threads and local processes."""

    thread_lock = _library_thread_lock(library_path)
    with thread_lock:
        library_path.mkdir(parents=True, exist_ok=True)
        lock_path = library_path / ".portrait-library.lock"
        handle = lock_path.open("a+b")
        acquired = False
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            deadline = time.monotonic() + max(0.1, float(timeout))
            if os.name == "nt":
                import msvcrt

                while True:
                    try:
                        handle.seek(0)
                        msvcrt.locking(
                            handle.fileno(),
                            msvcrt.LK_NBLCK,
                            1,
                        )
                        acquired = True
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise TimeoutError(
                                f"timed out locking portrait library: "
                                f"{library_path}"
                            )
                        time.sleep(0.05)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                acquired = True
            yield
        finally:
            if acquired:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(
                        handle.fileno(),
                        msvcrt.LK_UNLCK,
                        1,
                    )
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()


def normalize_view(view: str) -> str:
    normalized = VIEW_ALIASES.get(str(view).strip().lower())
    if normalized is None:
        raise ValueError(f"unsupported portrait view: {view}")
    return normalized


def _safe_component(value: Any) -> str:
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", str(value or "")).strip(
        " ."
    )
    return text or "unknown"


def _as_bgr(image: np.ndarray) -> np.ndarray:
    if not isinstance(image, np.ndarray) or image.ndim not in (2, 3):
        raise TypeError("portrait must be a numpy image")
    if not image.size:
        raise ValueError("portrait must not be empty")
    if image.ndim == 2:
        return np.repeat(image[:, :, None], 3, axis=2).astype(np.uint8)
    if image.shape[2] == 4:
        return np.ascontiguousarray(image[:, :, :3]).astype(np.uint8)
    if image.shape[2] != 3:
        raise ValueError("portrait must have 1, 3, or 4 channels")
    return np.ascontiguousarray(image).astype(np.uint8)


def load_image(path: str | Path) -> np.ndarray:
    rgb = np.asarray(Image.open(path).convert("RGB"))
    return np.ascontiguousarray(rgb[:, :, ::-1])


def save_image(path: str | Path, image: np.ndarray) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    bgr = _as_bgr(image)
    Image.fromarray(bgr[:, :, ::-1]).save(target, format="PNG")


def image_sha256(image: np.ndarray) -> str:
    normalized = _as_bgr(image)
    digest = hashlib.sha256()
    digest.update(str(tuple(normalized.shape)).encode("ascii"))
    digest.update(normalized.tobytes())
    return digest.hexdigest()


def _gray32(image: np.ndarray) -> np.ndarray:
    bgr = _as_bgr(image).astype(np.float64)
    gray = bgr[:, :, 0] * 0.114 + bgr[:, :, 1] * 0.587 + bgr[:, :, 2] * 0.299
    resized = Image.fromarray(np.clip(gray, 0, 255).astype(np.uint8)).resize(
        (32, 32),
        Image.Resampling.BILINEAR,
    )
    return np.asarray(resized, dtype=np.float64)


@lru_cache(maxsize=1)
def _dct_matrix() -> np.ndarray:
    size = 32
    x = np.arange(size, dtype=np.float64)
    k = np.arange(size, dtype=np.float64)[:, None]
    matrix = np.cos(np.pi * (2 * x + 1) * k / (2 * size))
    matrix[0] *= np.sqrt(1 / size)
    matrix[1:] *= np.sqrt(2 / size)
    return matrix


def _phash_from_gray(pixels: np.ndarray) -> str:
    matrix = _dct_matrix()
    low = (matrix @ pixels @ matrix.T)[:8, :8].reshape(-1)
    median = float(np.median(low[1:]))
    bits = low >= median
    value = 0
    for bit in bits:
        value = (value << 1) | int(bool(bit))
    return f"{value:016x}"


def image_phash(image: np.ndarray) -> str:
    return _phash_from_gray(_gray32(image))


def phash_distance(left: str, right: str) -> int:
    return (int(left, 16) ^ int(right, 16)).bit_count()


def _visual_similarity_gray(a: np.ndarray, b: np.ndarray) -> float:
    pixel_score = 1.0 - float(np.mean(np.abs(a - b))) / 255.0
    a_centered = a - float(a.mean())
    b_centered = b - float(b.mean())
    denominator = float(
        np.linalg.norm(a_centered) * np.linalg.norm(b_centered)
    )
    correlation = (
        float(np.sum(a_centered * b_centered)) / denominator
        if denominator > 1e-9
        else float(np.array_equal(a, b))
    )
    correlation_score = (max(-1.0, min(correlation, 1.0)) + 1.0) / 2.0
    return max(0.0, min(1.0, 0.6 * correlation_score + 0.4 * pixel_score))


def visual_similarity(left: np.ndarray, right: np.ndarray) -> float:
    return _visual_similarity_gray(_gray32(left), _gray32(right))


@dataclass(frozen=True)
class PortraitTemplate:
    shikigami_id: str
    name: str
    view: str
    relative_path: str
    sha256: str
    phash: str
    source: str
    confidence: float
    sequence: int


@dataclass(frozen=True)
class PortraitAddResult:
    record: PortraitTemplate
    created: bool
    dedupe_reason: str | None = None


@dataclass(frozen=True)
class PortraitMatch:
    shikigami_id: str | None
    name: str | None
    confidence: float
    source: str
    highest_candidate: dict[str, Any] | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class PortraitLibrary:
    """Idempotent on-disk library using ID-name-view-sequence filenames."""

    def __init__(
        self,
        library_path: str | Path,
        *,
        database_path: str | Path | None = None,
    ) -> None:
        self.library_path = Path(library_path)
        self.database_path = Path(database_path) if database_path else None
        self._records = self._read_index()

    @property
    def records(self) -> tuple[PortraitTemplate, ...]:
        return tuple(self._records)

    def add_template(
        self,
        image: np.ndarray,
        *,
        shikigami_id: str | int,
        name: str,
        view: str,
        source: str,
        confidence: float,
        analyze_only: bool = False,
    ) -> PortraitAddResult:
        if analyze_only:
            return self._add_template_unlocked(
                image,
                shikigami_id=shikigami_id,
                name=name,
                view=view,
                source=source,
                confidence=confidence,
                analyze_only=True,
            )
        with _library_write_lock(self.library_path):
            # A different request/process may have extended the index after
            # this instance was constructed. Refresh it before dedupe and
            # sequence allocation while holding the shared lock.
            self._records = self._read_index()
            return self._add_template_unlocked(
                image,
                shikigami_id=shikigami_id,
                name=name,
                view=view,
                source=source,
                confidence=confidence,
                analyze_only=False,
            )

    def _add_template_unlocked(
        self,
        image: np.ndarray,
        *,
        shikigami_id: str | int,
        name: str,
        view: str,
        source: str,
        confidence: float,
        analyze_only: bool,
    ) -> PortraitAddResult:
        normalized = _as_bgr(image)
        identity = str(shikigami_id)
        canonical = str(name).strip()
        normalized_view = normalize_view(view)
        sha256 = image_sha256(normalized)
        phash = image_phash(normalized)
        relevant = [
            item
            for item in self._records
            if item.shikigami_id == identity and item.view == normalized_view
        ]
        for item in relevant:
            if item.sha256 == sha256:
                return PortraitAddResult(item, False, "sha256")
        for item in relevant:
            try:
                existing = load_image(self.library_path / item.relative_path)
            except (FileNotFoundError, OSError):
                continue
            if (
                phash_distance(item.phash, phash) <= 2
                and visual_similarity(existing, normalized) >= 0.995
            ):
                return PortraitAddResult(item, False, "phash")

        sequence = max((item.sequence for item in relevant), default=0) + 1
        base = f"{identity}-{_safe_component(canonical)}"
        filename = (
            f"{base}-{normalized_view}-{sequence:03d}.png"
        )
        relative_path = (Path(base) / filename).as_posix()
        record = PortraitTemplate(
            shikigami_id=identity,
            name=canonical,
            view=normalized_view,
            relative_path=relative_path,
            sha256=sha256,
            phash=phash,
            source=str(source),
            confidence=max(0.0, min(float(confidence), 1.0)),
            sequence=sequence,
        )
        self._records.append(record)
        self._records.sort(
            key=lambda item: (
                int(item.shikigami_id)
                if item.shikigami_id.isdigit()
                else item.shikigami_id,
                item.view,
                item.sequence,
            )
        )
        if analyze_only:
            return PortraitAddResult(record, True)

        target = self.library_path / relative_path
        save_image(target, normalized)
        self._write_index()
        self._upsert_database(record)
        return PortraitAddResult(record, True)

    def register_unresolved(
        self,
        *,
        relative_path: str | Path,
        view: str,
        sha256: str,
        source: str = "video_unresolved",
        confidence: float = 0.0,
    ) -> bool:
        """Expose an unresolved crop to the central correction workflow."""

        if self.database_path is None:
            return False
        from module.duel_data.repository import DuelRepository

        _, created = DuelRepository(
            self.database_path
        ).upsert_portrait_template(
            {
                "path": Path(relative_path).as_posix(),
                "shishen_id": None,
                "name": None,
                "view": normalize_view(view),
                "hash": str(sha256),
                "source": str(source),
                "confidence": max(0.0, min(float(confidence), 1.0)),
            }
        )
        return created

    def coverage_report(
        self,
        assets: Iterable[dict[str, Any]],
    ) -> dict[str, Any]:
        by_id: dict[str, set[str]] = {}
        for item in self._records:
            by_id.setdefault(item.shikigami_id, set()).add(item.view)
        rows = []
        for asset in assets:
            identity = str(asset["id"])
            views = sorted(by_id.get(identity, ()))
            rows.append(
                {
                    "id": identity,
                    "name": str(asset.get("name") or ""),
                    "covered": bool(views),
                    "views": views,
                    "template_count": sum(
                        item.shikigami_id == identity
                        for item in self._records
                    ),
                }
            )
        rows.sort(
            key=lambda item: (
                int(item["id"]) if item["id"].isdigit() else item["id"]
            )
        )
        covered = sum(item["covered"] for item in rows)
        return {
            "asset_count": len(rows),
            "covered_count": covered,
            "missing_count": len(rows) - covered,
            "items": rows,
        }

    def write_coverage_report(
        self,
        assets: Iterable[dict[str, Any]],
        path: str | Path | None = None,
    ) -> dict[str, Any]:
        with _library_write_lock(self.library_path):
            self._records = self._read_index()
            report = self.coverage_report(assets)
            target = (
                Path(path)
                if path
                else self.library_path / "coverage.json"
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(target.name + ".tmp")
            temporary.write_text(
                json.dumps(
                    report,
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, target)
            return report

    def prune_missing_records(self) -> tuple[PortraitTemplate, ...]:
        """Remove index rows whose image was moved or deleted manually."""

        with _library_write_lock(self.library_path):
            self._records = self._read_index()
            kept: list[PortraitTemplate] = []
            removed: list[PortraitTemplate] = []
            library_root = self.library_path.resolve()
            for record in self._records:
                path = (self.library_path / record.relative_path).resolve()
                try:
                    path.relative_to(library_root)
                    image = load_image(path)
                except (FileNotFoundError, OSError, TypeError, ValueError):
                    removed.append(record)
                    continue
                if image_sha256(image) != record.sha256:
                    removed.append(record)
                    continue
                kept.append(record)
            if removed:
                self._records = kept
                self._write_index()
            return tuple(removed)

    def _read_index(self) -> list[PortraitTemplate]:
        path = self.library_path / "index.jsonl"
        if not path.exists():
            return self._scan_files()
        records: list[PortraitTemplate] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(PortraitTemplate(**json.loads(line)))
        return records

    def _scan_files(self) -> list[PortraitTemplate]:
        if not self.library_path.exists():
            return []
        pattern = re.compile(
            r"^(?P<id>\d+)-(?P<name>.+)-"
            r"(?P<view>候选|阵容|上阵)-(?P<sequence>\d{3})\.png$"
        )
        records = []
        for path in sorted(self.library_path.rglob("*.png")):
            match = pattern.match(path.name)
            if match is None or "_unresolved" in path.parts:
                continue
            image = load_image(path)
            records.append(
                PortraitTemplate(
                    shikigami_id=match["id"],
                    name=match["name"],
                    view=match["view"],
                    relative_path=path.relative_to(
                        self.library_path
                    ).as_posix(),
                    sha256=image_sha256(image),
                    phash=image_phash(image),
                    source="filesystem",
                    confidence=1.0,
                    sequence=int(match["sequence"]),
                )
            )
        return records

    def _write_index(self) -> None:
        self.library_path.mkdir(parents=True, exist_ok=True)
        target = self.library_path / "index.jsonl"
        temporary = target.with_name(target.name + ".tmp")
        temporary.write_text(
            "".join(
                json.dumps(
                    asdict(item),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
                for item in self._records
            ),
            encoding="utf-8",
        )
        os.replace(temporary, target)

    def _upsert_database(self, record: PortraitTemplate) -> None:
        if self.database_path is None:
            return
        # Keep schema ownership in the central repository.  Import lazily to
        # avoid coupling the runtime matcher to Pydantic/SQLite startup.
        from module.duel_data.repository import DuelRepository

        DuelRepository(self.database_path).upsert_portrait_template(
            {
                "path": record.relative_path,
                "shishen_id": int(record.shikigami_id),
                "name": record.name,
                "view": record.view,
                "hash": record.sha256,
                "source": record.source,
                "confidence": record.confidence,
            }
        )


class PortraitMatcher:
    """Runtime pHash/pixel matcher for opponent BP portraits."""

    PREFILTER_LIMIT = 32

    def __init__(self, library_path: str | Path) -> None:
        self.library_path = Path(library_path)
        self.library = PortraitLibrary(self.library_path)
        self._features: dict[str, tuple[int, np.ndarray]] = {}
        self._index_mtime_ns = self._current_index_mtime()

    def reload(self) -> None:
        """Reload records after an OASX/manual correction."""

        self.library = PortraitLibrary(self.library_path)
        self._features.clear()
        self._index_mtime_ns = self._current_index_mtime()

    def match(
        self,
        image: np.ndarray,
        views: Iterable[str] = (),
    ) -> PortraitMatch:
        return self.match_many((image,), views=views)[0]

    def match_many(
        self,
        images: Iterable[np.ndarray],
        views: Iterable[str] = (),
    ) -> list[PortraitMatch]:
        """Match a batch while loading/resizing every template only once."""

        queries = list(images)
        if not queries:
            return []
        self._reload_if_changed()
        requested = {normalize_view(view) for view in views}
        records = [
            item
            for item in self.library.records
            if not requested or item.view in requested
        ]
        if not records:
            return [self._empty_match() for _ in queries]

        template_features: list[
            tuple[PortraitTemplate, int, np.ndarray]
        ] = []
        for item in records:
            feature = self._features.get(item.relative_path)
            if feature is None:
                try:
                    loaded = load_image(
                        self.library_path / item.relative_path
                    )
                except (FileNotFoundError, OSError):
                    continue
                gray = _gray32(loaded)
                feature = (int(item.phash, 16), gray)
                self._features[item.relative_path] = feature
            template_features.append((item, feature[0], feature[1]))
        if not template_features:
            return [self._empty_match() for _ in queries]
        return [
            self._match_feature(_gray32(_as_bgr(query)), template_features)
            for query in queries
        ]

    def _match_feature(
        self,
        query_gray: np.ndarray,
        template_features: list[
            tuple[PortraitTemplate, int, np.ndarray]
        ],
    ) -> PortraitMatch:
        query_hash_text = _phash_from_gray(query_gray)
        query_hash = int(query_hash_text, 16)
        prefiltered = sorted(
            template_features,
            key=lambda row: (
                (query_hash ^ row[1]).bit_count(),
                row[0].shikigami_id,
                row[0].view,
                row[0].sequence,
            ),
        )[: self.PREFILTER_LIMIT]
        candidates: list[tuple[float, PortraitTemplate]] = []
        for item, template_hash, template_gray in prefiltered:
            distance = (query_hash ^ template_hash).bit_count()
            perceptual = 1.0 - distance / 64.0
            similarity = _visual_similarity_gray(query_gray, template_gray)
            confidence = max(
                0.0,
                min(1.0, 0.45 * perceptual + 0.55 * similarity),
            )
            candidates.append((confidence, item))
        confidence, best = max(
            candidates,
            key=lambda row: (
                row[0],
                row[1].shikigami_id,
                row[1].view,
                -row[1].sequence,
            ),
        )
        highest = {
            "shikigami_id": best.shikigami_id,
            "name": best.name,
            "confidence": round(confidence, 6),
            "view": best.view,
            "relative_path": best.relative_path,
        }
        accepted = confidence >= 0.98
        return PortraitMatch(
            best.shikigami_id if accepted else None,
            best.name if accepted else None,
            round(confidence, 6),
            "portrait_library",
            highest,
        )

    @staticmethod
    def _empty_match() -> PortraitMatch:
        return PortraitMatch(None, None, 0.0, "portrait_library", None)

    def _current_index_mtime(self) -> int | None:
        try:
            return (self.library_path / "index.jsonl").stat().st_mtime_ns
        except FileNotFoundError:
            return None

    def _reload_if_changed(self) -> None:
        current = self._current_index_mtime()
        if current != self._index_mtime_ns:
            self.reload()
