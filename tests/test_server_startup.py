import logging
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import server
from module.exception import ScriptError
from module.ocr import models as ocr_models
from module.ocr import rpc as ocr_rpc


class ServerStartupTest(unittest.TestCase):
    def setUp(self) -> None:
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.log_path = Path(directory) / "startup.log"
        handler = logging.FileHandler(self.log_path, encoding="utf-8")
        self.stack.callback(handler.close)
        logger = logging.Logger("startup-test", level=logging.INFO)
        logger.addHandler(handler)
        logger.hr = logger.info
        logger.attr = lambda key, value: logger.info("%s: %s", key, value)
        self.stack.enter_context(patch.object(server, "logger", logger))
        self.stack.enter_context(patch.object(server, "State", SimpleNamespace(
            restart_event=None,
            deploy_config=SimpleNamespace(
                WebuiHost="127.0.0.1", WebuiPort=22288,
                ImageClientAddress="127.0.0.1:22269",
                OcrClientAddress="192.168.50.164:22268",
                StartOcrServer=True,
            ),
        )))
        self.stack.enter_context(patch.object(sys, "argv", ["server.py"]))
        self.stack.enter_context(patch("asyncio.set_event_loop_policy"))
        self.image_ready = self.stack.enter_context(patch.object(server, "ensure_image_server_ready"))
        self.ocr_ready = self.stack.enter_context(patch.object(server, "ensure_ocr_server_ready"))
        self.stop_image = self.stack.enter_context(patch.object(server, "shutdown_image_server"))
        self.stop_ocr = self.stack.enter_context(patch.object(server, "shutdown_ocr_server"))
        self.run_backend = self.stack.enter_context(patch("uvicorn.run"))

    def log_text(self) -> str:
        return self.log_path.read_text(encoding="utf-8")

    def assert_services_cleaned(self) -> None:
        self.stop_image.assert_called_once_with()
        self.stop_ocr.assert_called_once_with()

    def test_image_failure_is_logged_and_prevents_later_stages(self) -> None:
        failure = RuntimeError("image ping failed")
        self.image_ready.side_effect = failure
        with self.assertRaises(RuntimeError) as raised:
            server.fun(None)
        self.assertIs(raised.exception, failure)
        self.ocr_ready.assert_not_called()
        self.run_backend.assert_not_called()
        self.assert_services_cleaned()
        self.assertIn("Backend failed during image service (127.0.0.1:22269)", self.log_text())
        self.assertIn("Traceback (most recent call last)", self.log_text())

    def test_ocr_failure_keeps_exception_chain_on_disk(self) -> None:
        def fail_ocr() -> None:
            try:
                raise TimeoutError("remote ping timed out")
            except TimeoutError as cause:
                raise ScriptError("OCR connection failed") from cause

        self.ocr_ready.side_effect = fail_ocr
        with self.assertRaisesRegex(ScriptError, "OCR connection failed"):
            server.fun(None)
        self.run_backend.assert_not_called()
        self.assert_services_cleaned()
        text = self.log_text()
        self.assertIn("Backend failed during OCR service (192.168.50.164:22268)", text)
        self.assertIn("TimeoutError: remote ping timed out", text)
        self.assertIn("ScriptError: OCR connection failed", text)

    def test_remote_ocr_failure_warns_but_backend_starts(self) -> None:
        server.State.deploy_config.StartOcrServer = False
        self.ocr_ready.side_effect = ScriptError("OCR connection failed")
        server.fun(None)
        self.run_backend.assert_called_once()
        self.assert_services_cleaned()
        text = self.log_text()
        self.assertIn("Remote OCR unavailable during startup", text)
        self.assertIn("Tasks will connect on demand", text)
        self.assertIn("Startup stage: HTTP backend", text)
        self.assertNotIn("Backend failed", text)
        self.assertNotIn("Traceback", text)

    def test_remote_ocr_success_does_not_warn(self) -> None:
        server.State.deploy_config.StartOcrServer = False
        server.fun(None)
        self.ocr_ready.assert_called_once_with()
        self.run_backend.assert_called_once()
        self.assertNotIn("Remote OCR unavailable", self.log_text())

    def test_remote_mode_does_not_swallow_unexpected_errors(self) -> None:
        server.State.deploy_config.StartOcrServer = False
        self.ocr_ready.side_effect = ValueError("invalid startup state")
        with self.assertRaisesRegex(ValueError, "invalid startup state"):
            server.fun(None)
        self.run_backend.assert_not_called()
        self.assert_services_cleaned()

    def test_nonzero_backend_exit_is_logged_and_preserved(self) -> None:
        self.run_backend.side_effect = SystemExit(3)
        with self.assertRaises(SystemExit) as raised:
            server.fun(None)
        self.assertEqual(3, raised.exception.code)
        self.assert_services_cleaned()
        self.assertIn("Backend exited during HTTP backend (127.0.0.1:22288) (exit_code=3)", self.log_text())

    def test_cleanup_failure_does_not_mask_original_error_or_skip_ocr_cleanup(self) -> None:
        failure = RuntimeError("original startup error")
        self.ocr_ready.side_effect = failure
        self.stop_image.side_effect = RuntimeError("cleanup error")
        with self.assertRaises(RuntimeError) as raised:
            server.fun(None)
        self.assertIs(failure, raised.exception)
        self.assert_services_cleaned()
        self.assertIn("Failed to clean up image service", self.log_text())

    def test_success_preserves_order_and_logs_all_stages(self) -> None:
        calls = Mock()
        for name, action in (("image", self.image_ready), ("ocr", self.ocr_ready),
                             ("backend", self.run_backend), ("stop_image", self.stop_image),
                             ("stop_ocr", self.stop_ocr)):
            calls.attach_mock(action, name)
        server.fun(None)
        self.assertEqual(["image", "ocr", "backend", "stop_image", "stop_ocr"],
                         [call[0] for call in calls.mock_calls])
        text = self.log_text()
        for stage in ("image service", "OCR service", "HTTP backend"):
            self.assertIn(f"Startup stage: {stage}", text)
        self.assertNotIn("Traceback", text)

    def test_keyboard_interrupt_still_cleans_up(self) -> None:
        self.ocr_ready.side_effect = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            server.fun(None)
        self.assert_services_cleaned()
        self.assertNotIn("Backend failed", self.log_text())


class OcrOnDemandTest(unittest.TestCase):
    def test_task_failure_is_not_cached_and_next_call_can_reconnect(self) -> None:
        state = SimpleNamespace(deploy_config=SimpleNamespace(
            OcrClientAddress="192.168.50.164:22268",
        ))
        connection = Mock()
        connection.ping.side_effect = [TimeoutError("OCR offline"), True]
        connection.ocr_single_line.return_value = ("12345", 0.99)
        with patch.object(ocr_models, "State", state), \
                patch.object(ocr_rpc, "_OCR_CLIENT_CACHE", {}), \
                patch.object(ocr_rpc.zerorpc, "Client", return_value=connection) as factory:
            with self.assertRaisesRegex(ScriptError, "OCR server connection failed"):
                ocr_models.get_ocr_model()
            self.assertEqual({}, ocr_rpc._OCR_CLIENT_CACHE)
            model = ocr_models.get_ocr_model()
            self.assertEqual(("12345", 0.99), model.ocr_single_line(ocr_rpc.np.zeros((32, 32, 3))))
            self.assertEqual(2, factory.call_count)


if __name__ == "__main__":
    unittest.main()
