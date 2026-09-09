import unittest
from unittest.mock import Mock, patch

from module.server.api_logger import log_http_access


def http_payload(path: str, status_code: int, *, response_type: str = "ASCIIJSONResponse") -> dict:
    return {
        "request": {"url": f"http://127.0.0.1:22288{path}"},
        "response": {
            "status_code": status_code,
            "response_type": response_type,
        },
    }


class ApiAccessLoggingTest(unittest.TestCase):
    def test_successful_log_poll_is_debug_only(self) -> None:
        logger = Mock()
        with patch("module.server.api_logger.ensure_api_logger", return_value=logger):
            log_http_access(http_payload("/logs/errors", 200))

        logger.debug.assert_called_once()
        logger.info.assert_not_called()

    def test_successful_stream_connection_is_debug_only(self) -> None:
        logger = Mock()
        with patch("module.server.api_logger.ensure_api_logger", return_value=logger):
            log_http_access(
                http_payload(
                    "/stats/oas1/stream",
                    200,
                    response_type="StreamingResponse",
                )
            )

        logger.debug.assert_called_once()
        logger.info.assert_not_called()

    def test_failed_log_request_remains_an_error(self) -> None:
        logger = Mock()
        with patch("module.server.api_logger.ensure_api_logger", return_value=logger):
            log_http_access(http_payload("/logs/errors", 500))

        logger.error.assert_called_once()
        logger.debug.assert_not_called()


class FileTracebackLoggingTest(unittest.TestCase):
    def test_file_tracebacks_do_not_include_locals(self) -> None:
        from module.logger import RichFileHandler, logger, set_file_logger

        set_file_logger("test_api_logger_traceback_settings")
        handler = next(
            item for item in logger.handlers if isinstance(item, RichFileHandler)
        )
        self.assertFalse(handler.tracebacks_show_locals)
