import json
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests

from ci_run import (
    _apply_notification_pause,
    _format_wecom_message,
    _record_dashboard_release,
    _record_release_event,
    _send_wecom_broadcast,
    _wecom_webhook_urls,
)
from quota_monitor.core import format_changes
from quota_monitor.notify import WECOM_MARKDOWN_MAX_BYTES, send_wecom_webhook

WECOM_URL = (
    "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?"
    "key=00000000-0000-0000-0000-000000000000"
)


class WecomNotifyTests(unittest.TestCase):
    def test_notification_pause_clears_human_channels_only(self):
        notification_env = {
            "NOTIFICATIONS_PAUSED": "true",
            "FEISHU_APP_ID": "app-id",
            "FEISHU_APP_SECRET": "app-secret",
            "FEISHU_CHAT_ID": "chat-id",
            "FEISHU_WEBHOOK_URL": "https://example.test/feishu",
            "WECOM_WEBHOOK_URL": WECOM_URL,
            "HKID_RELEASE_WEBHOOK_URL": "https://example.test/release",
            "HKID_RELEASE_WEBHOOK_SECRET": "release-secret",
        }
        with patch.dict(os.environ, notification_env, clear=True):
            self.assertTrue(_apply_notification_pause())
            for name in (
                "FEISHU_APP_ID",
                "FEISHU_APP_SECRET",
                "FEISHU_CHAT_ID",
                "FEISHU_WEBHOOK_URL",
                "WECOM_WEBHOOK_URL",
            ):
                self.assertNotIn(name, os.environ)
            self.assertEqual(
                os.environ["HKID_RELEASE_WEBHOOK_URL"],
                notification_env["HKID_RELEASE_WEBHOOK_URL"],
            )
            self.assertEqual(
                os.environ["HKID_RELEASE_WEBHOOK_SECRET"],
                notification_env["HKID_RELEASE_WEBHOOK_SECRET"],
            )

    @patch.dict(
        os.environ,
        {"NOTIFICATIONS_PAUSED": "false", "WECOM_WEBHOOK_URL": WECOM_URL},
        clear=True,
    )
    def test_notification_pause_false_keeps_human_channels(self):
        self.assertFalse(_apply_notification_pause())
        self.assertEqual(os.environ["WECOM_WEBHOOK_URL"], WECOM_URL)

    @patch("quota_monitor.notify.requests.post")
    def test_sends_markdown_to_official_webhook(self, post):
        post.return_value = Mock(status_code=200)
        post.return_value.json.return_value = {"errcode": 0, "errmsg": "ok"}

        self.assertTrue(send_wecom_webhook(WECOM_URL, "发现新名额"))

        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["msgtype"], "markdown")
        self.assertIn("发现新名额", payload["markdown"]["content"])
        self.assertEqual(post.call_args.kwargs["timeout"], 15)
        self.assertFalse(post.call_args.kwargs["allow_redirects"])

    @patch("quota_monitor.notify.requests.post")
    def test_splits_long_unicode_content_within_wecom_limit(self, post):
        post.return_value = Mock(status_code=200)
        post.return_value.json.return_value = {"errcode": 0, "errmsg": "ok"}

        self.assertTrue(send_wecom_webhook(WECOM_URL, "新名额一行\n" * 1000))

        self.assertGreater(post.call_count, 1)
        for call in post.call_args_list:
            content = call.kwargs["json"]["markdown"]["content"]
            self.assertLessEqual(len(content.encode("utf-8")), WECOM_MARKDOWN_MAX_BYTES)

    @patch("quota_monitor.notify.requests.post")
    def test_rejects_non_official_or_plain_http_url(self, post):
        self.assertFalse(
            send_wecom_webhook("https://example.test/hook?key=secret", "test")
        )
        self.assertFalse(
            send_wecom_webhook(
                "http://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=secret", "test"
            )
        )
        post.assert_not_called()

    @patch("quota_monitor.notify.requests.post")
    def test_reports_api_error_and_timeout(self, post):
        post.return_value = Mock(status_code=200)
        post.return_value.json.return_value = {"errcode": 93000, "errmsg": "invalid"}
        self.assertFalse(send_wecom_webhook(WECOM_URL, "test"))

        post.side_effect = requests.Timeout()
        self.assertFalse(send_wecom_webhook(WECOM_URL, "test"))

    @patch.dict(
        os.environ,
        {"WECOM_WEBHOOK_URL": f"{WECOM_URL},\n{WECOM_URL}&group=second"},
        clear=False,
    )
    def test_ci_parses_comma_and_newline_separated_webhooks(self):
        self.assertEqual(
            _wecom_webhook_urls(),
            [WECOM_URL, f"{WECOM_URL}&group=second"],
        )

    def test_ci_removes_group_link_and_everything_after_it(self):
        message = (
            "🟢 **新放出名额！**\n\n"
            "📋 预约办理：https://example.test/booking\n"
            "🪧 配额查询：https://example.test/quota\n"
            "📖 加群方式：https://example.test/group\n\n"
            "⚠️ 原项目免责声明"
        )

        formatted = _format_wecom_message(message)

        self.assertIn("📋 预约办理", formatted)
        self.assertIn("🪧 配额查询", formatted)
        self.assertNotIn("📖 加群方式", formatted)
        self.assertNotIn("⚠️ 原项目免责声明", formatted)

    def test_quota_message_uses_fork_dashboard(self):
        message = _format_wecom_message(format_changes({
            "newly_available": [
                (("09/01/2026", "FTO", "R"), "quota-r", "quota-g"),
            ],
        }))

        self.assertIn(
            "https://g2867082586-boop.github.io/quota-monitor/",
            message,
        )
        self.assertNotIn("https://Zheyi-D.github.io/quota-monitor", message)

    def test_records_only_this_forks_release_history(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "release_log.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({
                    "version": 1,
                    "monitoring_since": "2026-08-21T10:38:41+08:00",
                    "events": [],
                }, handle)

            event = _record_release_event(
                [(("09/01/2026", "FTO", "R"), "quota-r", "quota-g")],
                path=path,
                now="2026-08-21T12:00:00+08:00",
            )

            with open(path, encoding="utf-8") as handle:
                release_log = json.load(handle)

        self.assertEqual(
            release_log["monitoring_since"],
            "2026-08-21T10:38:41+08:00",
        )
        self.assertEqual(event["count"], 1)
        self.assertEqual(release_log["events"], [event])
        self.assertEqual(event["items"][0]["office"], "FTO")

    def test_dashboard_history_is_not_limited_to_two_week_message_window(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "release_log.json")
            event = _record_dashboard_release(
                {
                    "newly_available": [
                        (("01/01/2027", "FTO", "R"), "quota-r", "quota-g"),
                    ],
                },
                is_first_run=False,
                path=path,
                now="2026-08-24T12:00:00+08:00",
            )

            with open(path, encoding="utf-8") as handle:
                release_log = json.load(handle)

        self.assertEqual(event["count"], 1)
        self.assertEqual(release_log["events"], [event])

    def test_dashboard_history_skips_first_run_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "release_log.json")
            event = _record_dashboard_release(
                {
                    "newly_available": [
                        (("09/01/2026", "FTO", "R"), "quota-r", "quota-g"),
                    ],
                },
                is_first_run=True,
                path=path,
                now="2026-08-24T12:00:00+08:00",
            )

            self.assertIsNone(event)
            self.assertFalse(os.path.exists(path))

    @patch.dict(
        os.environ,
        {"WECOM_WEBHOOK_URL": f"{WECOM_URL},{WECOM_URL}&group=second"},
        clear=False,
    )
    @patch("ci_run.send_wecom_webhook", side_effect=[True, False])
    def test_ci_reports_partial_multi_group_delivery(self, send):
        self.assertEqual(_send_wecom_broadcast("test"), ("PARTIAL", 2))
        self.assertEqual(send.call_count, 2)


if __name__ == "__main__":
    unittest.main()
