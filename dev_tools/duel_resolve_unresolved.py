"""Resolve name-bearing Duel portraits and discard the remaining inbox.

The command is intentionally two-stage for destructive runs::

    python -m dev_tools.duel_resolve_unresolved --plan-output log/duel/resolve-plan.json
    python -m dev_tools.duel_resolve_unresolved --commit \
        --discard-unrecognized --plan-input log/duel/resolve-plan.json

Only strict two-variant OCR results and files explicitly placed by a human in
an ``ID-name`` directory are promoted.  A commit validates the planned image
hashes before changing the library.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

import numpy as np
from PIL import Image

from module.duel_data.repository import (
    DEFAULT_DATABASE_PATH,
    DuelRepository,
)
from tasks.Duel.portrait_library import (
    PortraitLibrary,
    image_sha256,
    load_image,
    normalize_view,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LIBRARY_PATH = PROJECT_ROOT / "config" / "duel" / "portrait_library"
STANDARD_FILE = re.compile(
    r"^(?P<id>\d+)-(?P<name>.+)-"
    r"(?P<view>候选|阵容|上阵)-(?P<sequence>\d{3})\.png$"
)


@dataclass(frozen=True)
class ResolutionDecision:
    portrait_path: str
    name_path: str | None
    portrait_sha256: str
    view: str
    shikigami_id: str | None
    name: str | None
    confidence: float
    method: str
    ocr_text: str = ""
    consensus: int = 0

    @property
    def recognized(self) -> bool:
        return self.shikigami_id is not None and bool(self.name)


@dataclass(frozen=True)
class ResolutionPlan:
    decisions: tuple[ResolutionDecision, ...]

    @property
    def total(self) -> int:
        return len(self.decisions)

    @property
    def recognized(self) -> int:
        return sum(item.recognized for item in self.decisions)

    @property
    def discarded(self) -> int:
        return self.total - self.recognized

    def as_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "recognized": self.recognized,
            "discarded": self.discarded,
            "decisions": [asdict(item) for item in self.decisions],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ResolutionPlan":
        rows = payload.get("decisions")
        if not isinstance(rows, list):
            raise ValueError("resolution plan decisions must be a list")
        return cls(tuple(ResolutionDecision(**row) for row in rows))


@dataclass(frozen=True)
class _OcrVote:
    text: str
    score: float
    resolved: Any
    method: str
    variant: str


class UnresolvedPortraitResolver:
    """Analyze and atomically reconcile the local unresolved portrait inbox."""

    def __init__(
        self,
        *,
        library_path: str | Path,
        database_path: str | Path,
        assets: Iterable[dict[str, Any]],
        recognizer: Any | None,
        ocr_model: Any | None,
    ) -> None:
        self.library_path = Path(library_path).resolve()
        self.database_path = Path(database_path).resolve()
        self.assets = [dict(item) for item in assets if isinstance(item, dict)]
        self.assets_by_directory = {
            f"{item['id']}-{str(item.get('name') or '').strip()}": item
            for item in self.assets
            if item.get("id") is not None
            and str(item.get("name") or "").strip()
        }
        self.recognizer = recognizer
        self.ocr_model = ocr_model

    def analyze(self) -> ResolutionPlan:
        if self.recognizer is None or self.ocr_model is None:
            raise RuntimeError("OCR recognizer and model are required for analysis")

        manual = self._manual_directory_decisions()
        manual_hashes = {item.portrait_sha256 for item in manual}
        decisions = list(manual)
        for row in self._read_manifest():
            recorded_hash = str(row.get("portrait_sha256") or "")
            # A user may move a pair from _unresolved into its known ID
            # directory.  Treat that physical sample exactly once.
            if recorded_hash and recorded_hash in manual_hashes:
                continue
            decisions.append(self._analyze_manifest_row(row))
        decisions.sort(
            key=lambda item: (
                item.portrait_path,
                item.method,
                item.shikigami_id or "",
            )
        )
        return ResolutionPlan(tuple(decisions))

    def commit(
        self,
        plan: ResolutionPlan,
        *,
        discard_unrecognized: bool,
    ) -> dict[str, Any]:
        if not discard_unrecognized:
            raise ValueError(
                "commit requires explicit discard_unrecognized authorization"
            )
        self._preflight(plan)

        library = PortraitLibrary(
            self.library_path,
            database_path=self.database_path,
        )
        created = 0
        deduplicated = 0
        imported: list[dict[str, Any]] = []
        manual_sources: list[Path] = []
        for decision in plan.decisions:
            if not decision.recognized:
                continue
            source = self._safe_path(decision.portrait_path)
            result = library.add_template(
                load_image(source),
                shikigami_id=decision.shikigami_id,
                name=str(decision.name),
                view=decision.view,
                source=(
                    "manual_directory"
                    if decision.method == "manual_directory"
                    else "unresolved_name_ocr"
                ),
                confidence=decision.confidence,
            )
            created += int(result.created)
            deduplicated += int(not result.created)
            imported.append(
                {
                    "id": decision.shikigami_id,
                    "name": decision.name,
                    "method": decision.method,
                    "source": decision.portrait_path,
                    "target": result.record.relative_path,
                    "created": result.created,
                }
            )
            if decision.method == "manual_directory":
                manual_sources.append(source)
                if decision.name_path:
                    manual_sources.append(self._safe_path(decision.name_path))

        # Delete manually sorted inbox copies only after their normalized
        # template has been stored successfully.
        for path in manual_sources:
            if path.is_file():
                path.unlink()

        unresolved_root = self.library_path / "_unresolved"
        if unresolved_root.exists():
            for path in sorted(unresolved_root.rglob("*"), reverse=True):
                if path.is_file():
                    path.unlink()
                elif path.is_dir():
                    path.rmdir()

        self._atomic_write_text(
            self.library_path / "unresolved.jsonl",
            "",
        )
        removed_index_rows = library.prune_missing_records()
        repository = DuelRepository(self.database_path)
        deleted_database_rows = (
            repository.delete_unresolved_portrait_templates()
        )
        coverage = library.write_coverage_report(self.assets)
        report = {
            "total": plan.total,
            "recognized": plan.recognized,
            "discarded": plan.discarded,
            "created": created,
            "deduplicated": deduplicated,
            "removed_index_rows": len(removed_index_rows),
            "deleted_database_rows": deleted_database_rows,
            "coverage": coverage,
            "imported": imported,
        }
        self._atomic_write_text(
            self.library_path / "resolve_report.json",
            json.dumps(
                report,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
            + "\n",
        )
        return report

    def _read_manifest(self) -> list[dict[str, Any]]:
        path = self.library_path / "unresolved.jsonl"
        if not path.exists():
            return []
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                payload = json.loads(line)
                if isinstance(payload, dict):
                    rows.append(payload)
        return rows

    def _manual_directory_decisions(self) -> list[ResolutionDecision]:
        decisions: list[ResolutionDecision] = []
        seen: set[Path] = set()
        for directory_name, asset in self.assets_by_directory.items():
            directory = self.library_path / directory_name
            if not directory.is_dir():
                continue
            identity = str(asset["id"])
            canonical = str(asset["name"])
            for portrait in sorted(directory.glob("*-portrait.png")):
                seen.add(portrait.resolve())
                name_path = portrait.with_name(
                    portrait.name[: -len("-portrait.png")] + "-name.png"
                )
                decisions.append(
                    self._manual_decision(
                        portrait,
                        name_path if name_path.is_file() else None,
                        identity=identity,
                        canonical=canonical,
                        view="候选",
                    )
                )
            for path in sorted(directory.glob("*.png")):
                if path.resolve() in seen:
                    continue
                match = STANDARD_FILE.fullmatch(path.name)
                if match is None:
                    continue
                if (
                    match["id"] == identity
                    and match["name"] == canonical
                ):
                    continue
                decisions.append(
                    self._manual_decision(
                        path,
                        None,
                        identity=identity,
                        canonical=canonical,
                        view=match["view"],
                    )
                )
        return decisions

    def _manual_decision(
        self,
        portrait: Path,
        name_path: Path | None,
        *,
        identity: str,
        canonical: str,
        view: str,
    ) -> ResolutionDecision:
        image = load_image(portrait)
        return ResolutionDecision(
            portrait_path=portrait.relative_to(
                self.library_path
            ).as_posix(),
            name_path=(
                name_path.relative_to(self.library_path).as_posix()
                if name_path is not None
                else None
            ),
            portrait_sha256=image_sha256(image),
            view=normalize_view(view),
            shikigami_id=identity,
            name=canonical,
            confidence=1.0,
            method="manual_directory",
            consensus=1,
        )

    def _analyze_manifest_row(
        self,
        row: dict[str, Any],
    ) -> ResolutionDecision:
        portrait_path = str(row.get("portrait_path") or "")
        name_path = (
            str(row.get("name_path")) if row.get("name_path") else None
        )
        view = normalize_view(str(row.get("view") or "候选"))
        recorded_hash = str(row.get("portrait_sha256") or "")
        base = ResolutionDecision(
            portrait_path=portrait_path,
            name_path=name_path,
            portrait_sha256=recorded_hash,
            view=view,
            shikigami_id=None,
            name=None,
            confidence=0.0,
            method="unresolved",
        )
        try:
            portrait = self._safe_path(portrait_path)
            if not portrait.is_file():
                return ResolutionDecision(**{**asdict(base), "method": "missing"})
            actual_hash = image_sha256(load_image(portrait))
            if recorded_hash and actual_hash != recorded_hash:
                return ResolutionDecision(
                    **{**asdict(base), "method": "hash_mismatch"}
                )
            if view != "候选" or name_path is None:
                return base
            name_image_path = self._safe_path(name_path)
            if not name_image_path.is_file():
                return ResolutionDecision(
                    **{**asdict(base), "method": "name_missing"}
                )
            candidate = self.recognizer.recognize(
                load_image(name_image_path),
                self.ocr_model,
            )
            if not bool(getattr(candidate, "accepted", False)):
                candidate = self._enhanced_recognize(
                    load_image(name_image_path),
                    candidate,
                )
            resolved = getattr(candidate, "resolved", None)
            if not bool(getattr(candidate, "accepted", False)) or resolved is None:
                return ResolutionDecision(
                    **{
                        **asdict(base),
                        "method": str(
                            getattr(candidate, "method", "unresolved")
                        ),
                        "ocr_text": str(getattr(candidate, "text", "")),
                        "confidence": float(
                            getattr(candidate, "ocr_score", 0.0) or 0.0
                        ),
                        "consensus": int(
                            getattr(candidate, "consensus", 0) or 0
                        ),
                    }
                )
            return ResolutionDecision(
                portrait_path=portrait_path,
                name_path=name_path,
                portrait_sha256=actual_hash,
                view=view,
                shikigami_id=str(resolved.shikigami_id),
                name=str(resolved.name),
                confidence=max(
                    0.0, min(float(resolved.confidence), 1.0)
                ),
                method=str(getattr(candidate, "method", "ocr")),
                ocr_text=str(getattr(candidate, "text", "")),
                consensus=int(getattr(candidate, "consensus", 0) or 0),
            )
        except (FileNotFoundError, OSError, TypeError, ValueError):
            return ResolutionDecision(**{**asdict(base), "method": "invalid"})

    def _enhanced_recognize(self, image: np.ndarray, initial: Any) -> Any:
        resolve_text = getattr(self.recognizer, "resolve_text", None)
        line_ocr = getattr(self.ocr_model, "ocr_single_line", None)
        if resolve_text is None or line_ocr is None:
            return initial
        height, width = image.shape[:2]
        votes: list[_OcrVote] = []

        def append_vote(text: Any, score: Any, variant: str) -> Any | None:
            numeric_score = float(score or 0.0)
            if not np.isfinite(numeric_score):
                numeric_score = 0.0
            resolved, method = resolve_text(
                str(text),
                ocr_score=numeric_score,
            )
            if resolved is not None:
                votes.append(
                    _OcrVote(
                        str(text),
                        numeric_score,
                        resolved,
                        method,
                        variant,
                    )
                )
            return self._accepted_vote(votes)

        x_ranges = tuple(
            dict.fromkeys(
                (
                    (min(1, width), min(21, width)),
                    (min(1, width), min(25, width)),
                    (0, width),
                )
            )
        )
        y_ranges = tuple(
            dict.fromkeys(
                (
                    (0, height),
                    (0, min(90, height)),
                    (0, min(100, height)),
                )
            )
        )
        for x1, x2 in x_ranges:
            for y1, y2 in y_ranges:
                if x2 - x1 < 8 or y2 - y1 < 40:
                    continue
                crop = image[y1:y2, x1:x2]
                for rotation, turns in (("ccw", 1), ("cw", 3)):
                    prepared = self._enlarge(np.rot90(crop, turns))
                    text, score = line_ocr(prepared)
                    accepted = append_vote(
                        text,
                        score,
                        f"line-x{x1}:{x2}-y{y1}:{y2}-{rotation}",
                    )
                    if accepted is not None:
                        return accepted

        detector = getattr(self.ocr_model, "detect_and_ocr", None)
        if detector is not None:
            for x1, x2, y1, y2 in (
                (0, width, 0, height),
                (0, width, 0, min(65, height)),
                (0, min(25, width), 0, height),
            ):
                detected = detector(
                    image[y1:y2, x1:x2],
                    drop_score=0.05,
                    box_thresh=0.4,
                    unclip_ratio=1.6,
                )
                detected = sorted(
                    (
                        item
                        for item in detected
                        if float(item.score) >= 0.25
                    ),
                    key=lambda item: float(np.min(item.box[:, 1])),
                )
                primary = []
                previous_bottom: float | None = None
                for item in detected:
                    top = float(np.min(item.box[:, 1]))
                    bottom = float(np.max(item.box[:, 1]))
                    if (
                        previous_bottom is not None
                        and top > previous_bottom + 12
                    ):
                        break
                    primary.append(item)
                    previous_bottom = (
                        bottom
                        if previous_bottom is None
                        else max(previous_bottom, bottom)
                    )
                if not primary:
                    continue
                accepted = append_vote(
                    "".join(str(item.ocr_text) for item in primary),
                    min(float(item.score) for item in primary),
                    f"detect-x{x1}:{x2}-y{y1}:{y2}",
                )
                if accepted is not None:
                    return accepted
        return initial

    @staticmethod
    def _accepted_vote(votes: list[_OcrVote]) -> Any | None:
        by_id: dict[str, dict[str, _OcrVote]] = {}
        for vote in votes:
            identity = str(vote.resolved.shikigami_id)
            by_id.setdefault(identity, {})[vote.variant] = vote
        if not by_id:
            return None
        top_count = max(len(rows) for rows in by_id.values())
        winners = [identity for identity, rows in by_id.items() if len(rows) == top_count]
        if top_count < 2 or len(winners) != 1:
            return None
        rows = list(by_id[winners[0]].values())
        best = max(
            rows,
            key=lambda item: (
                {"exact": 2, "substring": 1, "fuzzy": 0}.get(
                    item.method, -1
                ),
                item.score,
                len(item.text),
            ),
        )
        return type(
            "EnhancedOcrCandidate",
            (),
            {
                "accepted": True,
                "resolved": best.resolved,
                "method": best.method,
                "text": best.text,
                "ocr_score": best.score,
                "consensus": top_count,
            },
        )()

    @staticmethod
    def _enlarge(image: np.ndarray) -> np.ndarray:
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

    def _safe_path(self, relative_path: str) -> Path:
        path = (self.library_path / relative_path).resolve()
        try:
            path.relative_to(self.library_path)
        except ValueError as exc:
            raise ValueError(
                f"portrait path escapes library: {relative_path}"
            ) from exc
        return path

    def _preflight(self, plan: ResolutionPlan) -> None:
        for decision in plan.decisions:
            if not decision.recognized:
                continue
            path = self._safe_path(decision.portrait_path)
            if not path.is_file():
                raise FileNotFoundError(path)
            actual = image_sha256(load_image(path))
            if actual != decision.portrait_sha256:
                raise ValueError(
                    f"portrait changed since analysis: {decision.portrait_path}"
                )

    @staticmethod
    def _atomic_write_text(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)


def _load_assets(database_path: Path) -> list[dict[str, Any]]:
    assets = DuelRepository(database_path).latest_snapshot("shishen_assets")
    if isinstance(assets, dict):
        assets = next(
            (
                assets[key]
                for key in ("items", "data", "list")
                if isinstance(assets.get(key), list)
            ),
            [],
        )
    if not isinstance(assets, list) or not assets:
        raise RuntimeError("No shishen_assets snapshot is available")
    return [dict(item) for item in assets if isinstance(item, dict)]


def _write_plan(path: Path, plan: ResolutionPlan) -> None:
    UnresolvedPortraitResolver._atomic_write_text(
        path,
        json.dumps(
            plan.as_dict(),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library", type=Path, default=DEFAULT_LIBRARY_PATH)
    parser.add_argument(
        "--database", type=Path, default=DEFAULT_DATABASE_PATH
    )
    parser.add_argument("--plan-output", type=Path)
    parser.add_argument("--plan-input", type=Path)
    parser.add_argument("--commit", action="store_true")
    parser.add_argument("--discard-unrecognized", action="store_true")
    args = parser.parse_args(argv)

    library_path = args.library.resolve()
    database_path = args.database.resolve()
    assets = _load_assets(database_path)
    if args.commit:
        if not args.discard_unrecognized:
            parser.error("--commit requires --discard-unrecognized")
        if args.plan_input is None:
            parser.error("--commit requires --plan-input")
        plan = ResolutionPlan.from_dict(
            json.loads(args.plan_input.read_text(encoding="utf-8"))
        )
        resolver = UnresolvedPortraitResolver(
            library_path=library_path,
            database_path=database_path,
            assets=assets,
            recognizer=None,
            ocr_model=None,
        )
        result = resolver.commit(plan, discard_unrecognized=True)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0

    from module.ocr.ppocr import TextSystem
    from tasks.Duel.name_recognition import StrictNameRecognizer

    resolver = UnresolvedPortraitResolver(
        library_path=library_path,
        database_path=database_path,
        assets=assets,
        recognizer=StrictNameRecognizer(assets),
        ocr_model=TextSystem(),
    )
    plan = resolver.analyze()
    if args.plan_output is not None:
        _write_plan(args.plan_output.resolve(), plan)
    summary = {
        "total": plan.total,
        "recognized": plan.recognized,
        "discarded": plan.discarded,
        "by_method": {},
        "recognized_ids": sorted(
            {
                item.shikigami_id
                for item in plan.decisions
                if item.recognized
            },
            key=lambda value: int(str(value)),
        ),
    }
    for decision in plan.decisions:
        summary["by_method"][decision.method] = (
            summary["by_method"].get(decision.method, 0) + 1
        )
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
