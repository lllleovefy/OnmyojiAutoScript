# API log noise reduction

Source plan: derived from the observed growth of `log/*_api.txt` while the
OASX error list refreshes in the background.

User journey: a user can keep the log browser open without successful polling
and streaming connections filling the API audit log, while failed requests
remain visible for diagnosis.

| # | Guarantee | Test | Type | Result | Evidence |
|---|---|---|---|---|---|
| 1 | Successful `/logs/*` polling is emitted at DEBUG rather than INFO. | `tests.test_api_logger.ApiAccessLoggingTest.test_successful_log_poll_is_debug_only` | Unit | PASS | `python -m unittest tests.test_api_logger` |
| 2 | Successful stream connections are emitted at DEBUG rather than INFO. | `tests.test_api_logger.ApiAccessLoggingTest.test_successful_stream_connection_is_debug_only` | Unit | PASS | `python -m unittest tests.test_api_logger` |
| 3 | Failed log requests remain ERROR records. | `tests.test_api_logger.ApiAccessLoggingTest.test_failed_log_request_remains_an_error` | Unit | PASS | `python -m unittest tests.test_api_logger` |
| 4 | Script file tracebacks omit local-variable dumps. | `tests.test_api_logger.FileTracebackLoggingTest.test_file_tracebacks_do_not_include_locals` | Unit | PASS | `python -m unittest tests.test_api_logger` |
| 5 | The complete Python test suite remains green. | `test_*.py` | Unit/integration | PASS | `python -m unittest discover -s tests -p 'test_*.py'` (204 tests) |

RED evidence: before the fix, successful `/logs/errors` and streaming
connections called the INFO logger, while the file handler had
`tracebacks_show_locals=True`.

GREEN evidence: successful `/logs/*` responses and all `StreamingResponse`
connections now call DEBUG; 4xx/5xx responses retain warning/error severity.
File tracebacks still include the stack but omit local-variable tables.

Coverage: the bundled Python environment does not include the `coverage`
module. No coverage percentage is claimed; the focused tests and full suite
were run.
