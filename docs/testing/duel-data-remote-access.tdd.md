# Duel data remote access TDD evidence

## Source and user journey

This change was requested directly by the user. The clarified acceptance scope
is that `/duel-data` uses the same access policy as the other OAS APIs: no
additional client-address or Origin restriction on reads, writes, or SSE.

As an OASX user on another machine, I can use the configured OAS address to
load and update Duel data in the same way as existing OAS endpoints.

## Task report

The router-level `Depends(_require_local_duel_access)` dependency and its
loopback/Origin helpers were removed. The global OAS listener and CORS behavior
remain unchanged. `config/deploy.yaml` already defaults `WebuiHost` to
`0.0.0.0`.

## RED / GREEN evidence

| # | What is guaranteed | Test or command | Type | Result | Evidence |
|---|---|---|---|---|---|
| 1 | The Duel router and every registered Duel route have no additional access dependency | `DuelDataRouterContractTest.test_duel_routes_use_the_same_unrestricted_access_as_other_apis` | contract | PASS | RED previously reported `Depends(_require_local_duel_access)`; GREEN passes after removal. |
| 2 | All Duel route contracts still work | `toolkit/python.exe -m unittest tests.test_duel_data_router` | integration | PASS | `Ran 7 tests ... OK` |
| 3 | Database, recommendation, live state, recognition, portrait, selection, and task-flow behavior remain intact | `toolkit/python.exe -m unittest discover -s tests -p "test_duel*.py"` | regression | PASS | `Ran 202 tests ... OK` |
| 4 | The changed source has no whitespace errors | `git diff --check` | static | PASS | No output. |

## Coverage and known gaps

The bundled project Python does not contain the `coverage` module, so a numeric
coverage report could not be generated. The focused route suite and all 202
Duel tests passed.

Remote reachability still depends on the configured `WebuiHost`, port, network
routing, and operating-system firewall. This change intentionally does not add
authentication because the user explicitly requested parity with the other OAS
APIs.

## Merge evidence

- Final RED: the router still exposed `Depends(_require_local_duel_access)`.
- GREEN: the dependency was removed; 7 route tests and 202 Duel tests passed.
