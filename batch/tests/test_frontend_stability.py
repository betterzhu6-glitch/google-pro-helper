from copy import deepcopy
import json
import os
from pathlib import Path
import time
import unittest

from playwright.sync_api import sync_playwright


PAGE_PATH = Path(os.environ.get("UI_STABILITY_PAGE", Path(__file__).resolve().parents[1] / "templates/index.html"))


def account(account_id, **fields):
    result = {
        "id": account_id, "email": f"{account_id}@example.invalid", "password": "test-only",
        "status": "pending", "current_step": 0,
    }
    result.update(fields)
    return result


class FrontendStabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(channel="chrome", headless=True)
        cls.html = PAGE_PATH.read_text(encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.context = self.browser.new_context()
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.page.set_default_timeout(5000)
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.state = {"accounts": [account("first"), account("second")], "logs": ["系统已就绪"], "active_count": 0, "card": {}}
        self.hold_requests = False
        self.held_routes = []
        self.page.route("**/*", self.handle_request)
        self.page.add_init_script("""window.uiTestIntervals = [];
          window.setInterval = (callback, delay) => { window.uiTestIntervals.push(delay); return 0; };""")
        self.page.goto("http://console.test/")
        self.page.wait_for_function("document.querySelector('#statTotal').textContent === '2'")

    def handle_request(self, route):
        if route.request.url == "http://console.test/":
            route.fulfill(content_type="text/html; charset=utf-8", body=self.html)
        elif route.request.url == "http://console.test/api/state":
            if self.hold_requests:
                self.held_routes.append(route)
            else:
                route.fulfill(content_type="application/json", body=json.dumps(self.state))
        else:
            route.abort()

    def refresh(self):
        self.page.evaluate("() => fetchState()")

    def begin_overlapping_requests(self):
        self.hold_requests = True
        self.page.evaluate("() => { window.olderRequest = fetchState(); window.newerRequest = fetchState(); }")
        deadline = time.monotonic() + 5
        while len(self.held_routes) < 2 and time.monotonic() < deadline:
            self.page.wait_for_timeout(10)
        self.assertEqual(len(self.held_routes), 2)

    def finish_request(self, index, state=None, status=200):
        self.held_routes[index].fulfill(status=status, content_type="application/json", body=json.dumps(state or self.state))
        self.page.evaluate(f"() => window.{'olderRequest' if index == 0 else 'newerRequest'}")

    def test_unchanged_rows_and_logs_are_not_recreated(self):
        self.page.evaluate("""() => {
          window.savedRows = [...document.querySelector('#accountsTbody').children];
          window.savedLog = document.querySelector('#logTerminal').firstChild;
          document.querySelector('#accountsTbody select').focus();
        }""")
        for _ in range(5):
            self.refresh()
        self.assertTrue(self.page.evaluate("""() =>
          window.savedRows.every((row, index) => row === document.querySelector('#accountsTbody').children[index])
          && window.savedLog === document.querySelector('#logTerminal').firstChild
          && document.activeElement === window.savedRows[0].querySelector('select')"""))
        self.assertEqual(self.page.evaluate("window.uiTestIntervals"), [1500])

    def test_only_changed_account_row_is_replaced(self):
        self.page.evaluate("window.savedRows = [...document.querySelector('#accountsTbody').children]")
        self.state["accounts"][1].update(status="running", current_step=2, is_running=True, totp_secret="TEST", totp_now="123456", totp_rem=20)
        self.refresh()
        self.assertTrue(self.page.evaluate("window.savedRows[0] === document.querySelector('#accountsTbody').children[0]"))
        self.assertFalse(self.page.evaluate("window.savedRows[1] === document.querySelector('#accountsTbody').children[1]"))
        self.assertEqual(self.page.locator(".totp-pill span").first.inner_text(), "123456")
        self.assertEqual(self.page.locator(".totp-rem").inner_text(), "20s")
        self.assertEqual(self.page.locator(".status-running").count(), 1)
        self.state["accounts"][1]["totp_rem"] = 18
        self.refresh()
        self.assertEqual(self.page.locator(".totp-rem").inner_text(), "18s")
        self.assertTrue(self.page.evaluate("window.savedRows[0] === document.querySelector('#accountsTbody').children[0]"))

    def test_repeated_row_updates_do_not_accumulate_text_nodes(self):
        self.page.evaluate("""() => {
          for (let index = 0; index < 40; index++) {
            renderAccounts([
              { id: 'first', email: 'first@example.invalid', password: 'test-only', status: 'pending' },
              { id: 'second', email: 'second@example.invalid', password: 'test-only', status: 'running', step_message: `更新 ${index}` },
            ]);
          }
        }""")
        self.assertEqual(self.page.locator("#accountsTbody").evaluate("el => el.childNodes.length"), 2)

    def test_row_add_remove_reorder_duplicate_ids_and_empty_state(self):
        scenarios = [
            [account("first"), account("second"), account("third")],
            [account("third"), account("first")],
            [account("same", email="a@example.invalid"), account("same", email="b@example.invalid")],
            [], [account("new")], [],
        ]
        for accounts in scenarios:
            self.state["accounts"] = accounts
            self.refresh()
            self.assertEqual(self.page.locator("#accountsTbody tr").count(), max(len(accounts), 1))
            self.assertEqual(self.page.locator(".account-email").all_text_contents(), [item["email"] for item in accounts])
            self.assertEqual(self.page.locator(".empty-state").count(), int(not accounts))

    def test_local_button_feedback_and_step_selection_remain_visible(self):
        self.page.evaluate("selectedStartSteps.first = 5; copiedAccountMap.first = true; openedBrowserMap.first = true;")
        self.refresh()
        first = self.page.locator("#accountsTbody tr").first
        self.assertEqual(first.locator("select").input_value(), "5")
        self.assertEqual(first.get_by_role("button", name="已复制", exact=True).count(), 1)
        self.assertEqual(first.get_by_role("button", name="已打开", exact=True).count(), 1)
        self.page.evaluate("delete copiedAccountMap.first; delete openedBrowserMap.first;")
        self.refresh()
        self.assertEqual(first.get_by_role("button", name="复制", exact=True).count(), 1)
        self.assertEqual(first.get_by_role("button", name="浏览器", exact=True).count(), 1)
        self.assertEqual(first.locator("select").input_value(), "5")

    def test_log_styles_escaping_and_scroll_behavior_unchanged(self):
        self.state["logs"] = [f"日志行 {index}" for index in range(100)]
        self.refresh()
        self.page.evaluate("document.querySelector('#logTerminal').scrollTop = 0")
        self.state["logs"].extend(["⚠️ 等待验证", "❌ 异常", "✅ 完成", "<script>window.logInjected = true</script>"])
        self.refresh()
        self.assertEqual(self.page.locator(".log-warn").count(), 1)
        self.assertEqual(self.page.locator(".log-err").count(), 1)
        self.assertEqual(self.page.locator(".log-ok").count(), 1)
        self.assertEqual(self.page.locator("#logTerminal").evaluate("el => el.scrollTop"), 0)
        self.assertFalse(self.page.evaluate("Boolean(window.logInjected)"))
        self.page.evaluate("document.querySelector('#logTerminal').scrollTop = 99999")
        self.state["logs"].append("新日志")
        self.refresh()
        self.assertTrue(self.page.locator("#logTerminal").evaluate("el => el.scrollHeight - el.clientHeight <= el.scrollTop + 1"))
        self.state["logs"] = []
        self.refresh()
        self.assertEqual(self.page.locator("#logTerminal").inner_html(), "")

    def test_older_success_cannot_overwrite_newer_success(self):
        self.begin_overlapping_requests()
        newer = deepcopy(self.state)
        newer["accounts"].append(account("third"))
        self.finish_request(1, newer)
        self.finish_request(0)
        self.assertEqual(self.page.locator("#statTotal").inner_text(), "3")
        self.assertEqual(self.page.locator(".account-email").count(), 3)

    def test_older_failure_cannot_overwrite_newer_connection(self):
        self.begin_overlapping_requests()
        self.finish_request(1)
        self.finish_request(0, status=503)
        self.assertIn("已连接", self.page.locator("#connectionStatus").inner_text())

    def test_older_response_can_display_while_newer_request_is_pending(self):
        self.begin_overlapping_requests()
        older = deepcopy(self.state)
        older["accounts"].append(account("third"))
        self.finish_request(0, older)
        self.assertEqual(self.page.locator("#statTotal").inner_text(), "3")
        self.finish_request(1)
        self.assertEqual(self.page.locator("#statTotal").inner_text(), "2")

    def test_latest_failure_and_recovery_preserve_form_edits(self):
        self.page.locator("#cfgHolder").fill("UNSAVED UI TEST")
        self.begin_overlapping_requests()
        self.finish_request(1, status=503)
        self.finish_request(0)
        self.assertIn("连接中断", self.page.locator("#connectionStatus").inner_text())
        self.hold_requests = False
        self.refresh()
        self.assertIn("已连接", self.page.locator("#connectionStatus").inner_text())
        self.assertEqual(self.page.locator("#cfgHolder").input_value(), "UNSAVED UI TEST")
        self.assertEqual(self.errors, [])

    def test_account_fields_are_rendered_as_text(self):
        payload = '<img src=x onerror="window.accountInjected=true">'
        self.state["accounts"] = [account(
            "'\\\n" + payload, email=payload, password=payload,
            recovery_email=payload, ip_info=payload, status=payload,
            step_message=payload, expiry_date=payload, totp_secret="TEST",
            totp_now=payload, totp_rem=payload, last_screenshot=payload,
        )]
        self.refresh()
        self.assertEqual(self.page.locator("#accountsTbody img").count(), 0)
        self.assertEqual(self.page.locator("#accountsTbody [onclick], #accountsTbody [onchange]").count(), 0)
        self.assertEqual(self.page.locator(".account-email").inner_text(), payload)
        self.assertEqual(self.page.locator(".step-msg").inner_text(), payload)
        self.assertFalse(self.page.evaluate("Boolean(window.accountInjected)"))
        self.assertEqual(self.errors, [])

    def test_quoted_values_are_passed_intact_to_button_handlers(self):
        item = account("id'\\\n<&", email="name'@example.invalid", password="p'\\\n<&\"", recovery_email="recovery'@example.invalid")
        self.state["accounts"] = [item]
        self.refresh()
        self.page.evaluate("""() => {
          window.buttonCalls = [];
          copyAccountFull = (...args) => window.buttonCalls.push(args);
          resetAccount = (...args) => window.buttonCalls.push(args);
        }""")
        self.page.get_by_role("button", name="复制", exact=True).click()
        self.page.get_by_role("button", name="重置", exact=True).click()
        self.assertEqual(self.page.evaluate("window.buttonCalls"), [
            [item["id"], item["email"], item["password"], item["recovery_email"]], [item["id"]],
        ])
        self.page.locator("#accountsTbody select").select_option("5")
        self.refresh()
        self.assertEqual(self.page.locator("#accountsTbody select").input_value(), "5")
        self.assertEqual(self.errors, [])

    def test_prototype_named_account_and_status_are_safe(self):
        self.state["accounts"] = [account("__proto__", status="__proto__"), account("constructor", status="constructor")]
        self.refresh()
        self.assertEqual(self.page.locator(".status-badge").all_text_contents(), ["__proto__", "constructor"])
        self.assertEqual(self.errors, [])

    def test_screenshot_paths_stay_inside_image_directory(self):
        for filename in ["../config.json", "/api/state", "https://evil.example/x.png", "foo\\bar.png", "x.svg", "x.png?other=1"]:
            self.page.evaluate("filename => showScreenshot('test', filename)", filename)
            self.assertFalse(self.page.locator("#imgModal").evaluate("el => el.open"))
        self.page.evaluate("() => showScreenshot('test', \"核验 ' 1.png\")")
        self.assertTrue(self.page.locator("#imgModal").evaluate("el => el.open"))
        self.assertTrue(self.page.locator("#modalImg").get_attribute("src").startswith("/screenshots/%E6%A0%B8%E9%AA%8C%20'%201.png?t="))

    def test_write_request_includes_console_marker(self):
        headers_seen = []

        def stop_request(route):
            headers_seen.append(route.request.headers)
            route.fulfill(content_type="application/json", body='{"ok":true}')

        self.page.route("**/api/stop", stop_request)
        self.page.evaluate("() => stopAll()")
        self.assertEqual(len(headers_seen), 1)
        self.assertEqual(headers_seen[0].get("x-console-request"), "1")


if __name__ == "__main__":
    unittest.main()
