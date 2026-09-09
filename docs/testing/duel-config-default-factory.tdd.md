# Duel config default-factory serialization

Source plan: derived from the reported OASX task-configuration page failure.

User journey: a user opens the generic configuration page for Duel and can
load the strict shikigami-pool setting even though its Pydantic field uses a
`default_factory`.

| # | Guarantee | Test | Type | Result | Evidence |
|---|---|---|---|---|---|
| 1 | A default-factory list field is serialized with a usable default and current value. | `tests.test_duel_bp.DuelBPConfigTest.test_generic_task_arguments_support_default_factory_fields` | Unit | PASS | `python -m unittest tests.test_duel_bp.DuelBPConfigTest.test_generic_task_arguments_support_default_factory_fields` |
| 2 | The complete Duel regression suite still passes. | `test_duel*.py` | Unit/integration | PASS | `python -m unittest discover -s tests -p 'test_duel*.py'` (200 tests) |

RED evidence: before the fix, the new test failed in
`ConfigModel.script_task()` with `KeyError: 'default'` because Pydantic omits
JSON Schema `default` for `bp_shishen_pool`'s `default_factory`.

GREEN evidence: the generic task serializer now falls back to the materialized
task value when the schema omits a default. The real `古或今` configuration
returns a `duel_config.bp_shishen_pool` argument with `default: []` and
`value: []`.

Coverage: the repository does not provide a configured coverage threshold for
this Python unittest suite. The focused reproducer and the full 200-test Duel
suite were run; project-wide coverage was not claimed.
