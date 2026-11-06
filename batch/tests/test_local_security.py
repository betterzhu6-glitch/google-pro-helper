from pathlib import Path
import io
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
import app as console


class LocalSecurityTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(console.app, base_url="http://127.0.0.1:8765")
        self.addCleanup(self.client.close)
        self.config = mock.patch.object(console, "load_config", return_value={"accounts": [], "card": {}}).start()
        self.addCleanup(mock.patch.stopall)

    def test_local_page_and_state_are_accessible(self):
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/api/state").json()["accounts"], [])

    def test_loopback_host_variants(self):
        for host in ["localhost", "localhost:8765", "LOCALHOST:8765", "127.0.0.1:8765", "[::1]:8765"]:
            with self.subTest(host=host):
                self.assertEqual(self.client.get("/api/state", headers={"Host": host}).status_code, 200)

    def test_rebinding_and_malformed_hosts_never_read_config(self):
        for host in ["evil.example", "localhost.evil.example", "127.0.0.1.evil.example", "user@localhost", "localhost:bad", "localhost:", "[::1]evil", "localhost/", "localhost?", "localhost:0"]:
            with self.subTest(host=host):
                self.assertEqual(self.client.get("/api/state", headers={"Host": host}).status_code, 400)
        self.config.assert_not_called()

    def test_duplicate_host_headers_are_rejected(self):
        response = self.client.get("/api/state", headers=[("Host", "localhost"), ("Host", "evil.example")])
        self.assertEqual(response.status_code, 400)
        self.config.assert_not_called()

    def test_cross_site_cannot_read_sensitive_responses(self):
        for path in ["/api/state", "/screenshots/example.png"]:
            self.assertEqual(self.client.get(path, headers={"Sec-Fetch-Site": "cross-site"}).status_code, 403)
        self.config.assert_not_called()

    def test_cross_origin_and_null_origin_are_rejected(self):
        for origin in ["https://evil.example", "null", "http://localhost:8765", "http://127.0.0.1:9999"]:
            with self.subTest(origin=origin):
                self.assertEqual(self.client.get("/api/state", headers={"Origin": origin}).status_code, 403)
        self.config.assert_not_called()

    def test_same_origin_request_is_allowed(self):
        response = self.client.get("/api/state", headers={"Origin": "http://127.0.0.1:8765", "Sec-Fetch-Site": "same-origin"})
        self.assertEqual(response.status_code, 200)

    def test_writes_require_custom_header_before_touching_config(self):
        for path in ["/api/config", "/api/accounts/import", "/api/run/single", "/api/run/batch", "/api/open_browser", "/api/stop"]:
            with self.subTest(path=path):
                self.assertEqual(self.client.post(path, json={}).status_code, 403)
        self.config.assert_not_called()

    def test_same_origin_write_reaches_real_handler(self):
        with mock.patch.object(console, "save_config") as save:
            response = self.client.post("/api/config", json={"card": {}}, headers={"X-Console-Request": "1"})
        self.assertEqual(response.status_code, 200)
        save.assert_called_once()

    def test_custom_header_cannot_override_origin_check(self):
        response = self.client.post("/api/config", json={"card": {}}, headers={"X-Console-Request": "1", "Origin": "https://evil.example"})
        self.assertEqual(response.status_code, 403)
        self.config.assert_not_called()

    def test_cors_preflight_does_not_grant_access(self):
        response = self.client.options("/api/config", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "x-console-request"})
        self.assertEqual(response.status_code, 403)
        self.assertNotIn("access-control-allow-origin", response.headers)

    def test_sensitive_and_error_responses_have_security_headers(self):
        for path, headers in [("/", {}), ("/api/state", {}), ("/missing", {}), ("/api/state", {"Origin": "null"})]:
            response = self.client.get(path, headers=headers)
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertEqual(response.headers["x-content-type-options"], "nosniff")
            self.assertEqual(response.headers["x-frame-options"], "DENY")
            self.assertEqual(response.headers["content-security-policy"], "frame-ancestors 'none'")
            self.assertEqual(response.headers["referrer-policy"], "no-referrer")

    def test_cp936_logging_keeps_request_successful_and_unicode_in_ui(self):
        output = io.BytesIO()
        stream = io.TextIOWrapper(output, encoding="gbk")
        self.addCleanup(stream.close)
        logs = []
        with mock.patch.object(console.sys, "stdout", stream), mock.patch.object(console, "LOGS", logs), mock.patch.object(console, "save_config"):
            response = self.client.post("/api/config", json={"card": {}}, headers={"X-Console-Request": "1"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("✅", logs[0])
        self.assertIn("\\u2705", output.getvalue().decode("gbk"))


if __name__ == "__main__":
    unittest.main()
