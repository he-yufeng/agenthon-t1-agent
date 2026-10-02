"""Regression for real response-header slow drip before body reading begins."""
import importlib.util
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "submission"))
import engine

spec = importlib.util.spec_from_file_location("header_probe", ROOT / "diagnostics/header_deadline.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class HeaderDeadlineTests(unittest.TestCase):
    def test_real_header_drip_cannot_extend_shared_deadline(self):
        result = probe.observe(engine)
        self.assertEqual(result["attempted_requests"], 1)
        self.assertEqual(result["server_received_requests"], 1)
        self.assertFalse(result["returned_content"])
        self.assertEqual(result["endpoint_error_type"], "EndpointError")
        self.assertTrue(result["server_handler_settled"])
        self.assertLess(result["client_wall_seconds"], .6, result["timeline"])

    def test_response_arriving_after_deadline_is_closed(self):
        release = threading.Event()
        closed = threading.Event()
        class LateResponse:
            def close(self):
                closed.set()
        def delayed_open(*args, **kwargs):
            if not release.wait(2):
                raise RuntimeError("synthetic release timed out")
            return LateResponse()
        with patch.object(engine, "_open_house_request", delayed_open):
            with self.assertRaises(TimeoutError):
                engine._open_house_before_deadline(object(), time.monotonic() + .03)
            release.set()
            self.assertTrue(closed.wait(1))


if __name__ == "__main__":
    unittest.main()
