"""Unit tests run without Docker, external services, or third-party packages."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import build_opener, ProxyHandler

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


backend = load_module("lab_backend", ROOT / "labs/01-gateway/backend/server.py")
lab = load_module("lab_helper", ROOT / "scripts/lab.py")


class BackendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = backend.make_server(port=0)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"
        cls.opener = build_opener(ProxyHandler({}))

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def get(self, path):
        with self.opener.open(self.url + path, timeout=3) as response:
            return response.status, json.load(response)

    def test_health(self):
        status, body = self.get("/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")

    def test_order_response(self):
        status, body = self.get("/orders/123")
        self.assertEqual(status, 200)
        self.assertEqual(body["service"], "orders")
        self.assertEqual(body["instance"], "orders-a")
        self.assertEqual(body["path"], "/orders/123")

    def test_query_does_not_change_path(self):
        _, body = self.get("/orders/123?page=1")
        self.assertEqual(body["path"], "/orders/123")

    def test_wrong_service_is_404(self):
        with self.assertRaises(HTTPError) as result:
            self.get("/products/123")
        self.assertEqual(result.exception.code, 404)

    def test_prefix_without_slash_is_404(self):
        with self.assertRaises(HTTPError) as result:
            self.get("/orders-other/123")
        self.assertEqual(result.exception.code, 404)


class HelperTests(unittest.TestCase):
    def test_route_jsons(self):
        lab.validate()

    def test_prepare_creates_unique_secrets_and_preserves_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / ".env"
            with patch.object(lab, "ENV_FILE", path):
                lab.prepare()
                initial = path.read_text()
                with patch.dict(os.environ, {}, clear=True):
                    keys = lab.read_keys()
                self.assertEqual(len(keys["ADMIN_KEY"]), 64)
                self.assertNotEqual(keys["ADMIN_KEY"], keys["CONSUMER_KEY"])
                if os.name != "nt":
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                lab.prepare()
                self.assertEqual(path.read_text(), initial)

    def test_read_keys_requires_preparation(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(lab, "ENV_FILE", Path(folder) / ".env"):
                with self.assertRaisesRegex(RuntimeError, "prepare"):
                    lab.read_keys()

    def test_conflicting_environment_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / ".env"
            path.write_text("ADMIN_KEY=local\nCONSUMER_KEY=consumer\n")
            with patch.object(lab, "ENV_FILE", path):
                with patch.dict(os.environ, {"ADMIN_KEY": "different"}, clear=True):
                    with self.assertRaisesRegex(RuntimeError, "Unset"):
                        lab.read_keys()

    def test_bad_status_fails_check(self):
        with self.assertRaisesRegex(RuntimeError, "expected HTTP 200"):
            lab.check("route", 502, 200)

    def test_protected_route_rewrites_prefix(self):
        route = json.loads((lab.LAB / "routes/protected-orders.json").read_text())
        self.assertIn("key-auth", route["plugins"])
        self.assertEqual(route["plugins"]["proxy-rewrite"]["regex_uri"],
                         ["^/protected/(.*)$", "/$1"])

    def test_readiness_auth_error_is_not_retried(self):
        with patch.object(lab, "request", return_value=(403, "forbidden")) as request:
            with self.assertRaisesRegex(RuntimeError, "rejected access"):
                lab.wait_for_admin("key")
            self.assertEqual(request.call_count, 1)


if __name__ == "__main__":
    unittest.main()
