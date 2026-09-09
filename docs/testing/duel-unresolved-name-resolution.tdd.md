# Duel unresolved name resolution TDD evidence

## Source and user journey

The journey was derived from the 2026-08-01 request: as the local Duel data owner, the user wants name-bearing unresolved portraits placed in the canonical `ID-name` directory, while samples that cannot be identified reliably are discarded.

## RED / GREEN evidence

| Guarantee | Test | Evidence |
|---|---|---|
| Analysis is read-only and separates strict recognized from discarded samples | `test_analyze_recognizes_strict_name_without_mutating_files` | RED: `ModuleNotFoundError: dev_tools.duel_resolve_unresolved`; GREEN: targeted suite passed |
| A file deliberately placed under an `ID-name` directory is treated as an explicit human label | `test_parent_directory_is_an_explicit_manual_label` | GREEN |
| Detector OCR still needs two independent variants for one ID | `test_enhanced_detector_can_supply_two_independent_votes` | GREEN |
| Commit normalizes accepted files and removes rejected files, manifest rows, and SQLite unresolved rows | `test_commit_imports_recognized_and_discards_every_other_unresolved` | GREEN |
| Destructive cleanup needs an explicit flag | `test_commit_requires_explicit_discard_authorization` | GREEN |
| Moving a previously indexed image to another ID directory corrects the identity and removes the broken index row | `test_misplaced_standard_file_uses_parent_as_manual_correction` | GREEN |

Commands and results:

```text
D:\Game\oas-mine\toolkit\python.exe -m unittest tests.test_duel_unresolved_resolver
RED: import failed because the resolver module did not exist.
GREEN: Ran 6 tests ... OK

D:\Game\oas-mine\toolkit\python.exe -m unittest tests.test_duel_unresolved_resolver tests.test_duel_portrait_library tests.test_duel_data_repository
Ran 21 tests ... OK
```

## Data execution evidence

- Analysis plan: 496 physical decisions, 11 explicit manual labels, 485 discarded.
- Automated enhanced OCR: zero samples reached the required two-vote threshold.
- Commit: 11 templates created, one broken index row removed, 485 unresolved SQLite rows deleted.
- Final state: 81 indexed/loadable templates, 47/274 covered IDs, zero unresolved files/rows.

## Coverage and known gaps

The bundled toolkit does not include the Python `coverage` package (`No module named coverage`), so no percentage is claimed. The six resolver tests cover analysis, enhanced OCR consensus, manual labels, destructive authorization, commit integration, SQLite cleanup, idempotence, and stale-index correction. Real PaddleOCR output is validated by the executed 225-image analysis rather than mocked unit tests.

No TDD checkpoint commits were created because the branch already contains the user's larger uncommitted Duel/OASX work; the RED/GREEN evidence is preserved here without staging unrelated changes.
