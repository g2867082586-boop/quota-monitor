import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from quota_monitor.core import fetch_snapshot
from quota_monitor.daemon import QuotaDaemon, run_forever
from quota_monitor.state import load_state

BASELINE = {("08/30/2026", "RHK", "R"): "quota-r"}
AVAILABLE = {("08/30/2026", "RHK", "R"): "quota-g"}


def config():
    return {
        "api": {"svc_id": 579, "timeout": 5},
        "offices": {"RHK": "Hong Kong"},
        "date_range": {"start": None, "end": None},
        "notifications": {"feishu": {"enabled": False}},
        "retry": {"max_retries": 2, "backoff_base_seconds": 1},
        "daemon": {
            "failure_backoff_base_seconds": 30,
            "failure_backoff_max_seconds": 120,
        },
    }


class FetchSnapshotSessionTests(unittest.TestCase):
    def test_reuses_supplied_session_and_honours_retry_after(self):
        throttled = Mock(status_code=429, headers={"Retry-After": "7"})
        success = Mock(status_code=200, headers={})
        success.json.return_value = {
            "data": [
                {
                    "date": "08/30/2026",
                    "officeId": "RHK",
                    "quotaR": "quota-g",
                    "quotaK": "quota-r",
                }
            ]
        }
        session = Mock()
        session.get.side_effect = [throttled, success]
        sleeps = []

        snapshot = fetch_snapshot(
            session=session,
            max_retries=2,
            sleep=sleeps.append,
        )

        self.assertEqual(sleeps, [7.0])
        self.assertEqual(session.get.call_count, 2)
        self.assertEqual(snapshot[("08/30/2026", "RHK", "R")], "quota-g")

    def test_rejects_empty_api_data(self):
        response = Mock(status_code=200, headers={})
        response.json.return_value = {"data": []}
        session = Mock()
        session.get.return_value = response

        self.assertEqual(fetch_snapshot(session=session), {})


class DaemonCycleTests(unittest.TestCase):
    def _paths(self, directory):
        root = Path(directory)
        return (
            str(root / "state.json"),
            str(root / "web" / "quota.json"),
            str(root / "web" / "last_update.json"),
        )

    @patch("quota_monitor.daemon.send_notifications")
    @patch("quota_monitor.daemon.fetch_snapshot", side_effect=[BASELINE, AVAILABLE])
    def test_shadow_mode_persists_but_suppresses_side_effects(self, fetch, notify):
        with tempfile.TemporaryDirectory() as directory:
            state, web, update = self._paths(directory)
            daemon = QuotaDaemon(
                config(),
                state_path=state,
                web_data_path=web,
                last_update_path=update,
                shadow=True,
                session=Mock(),
            )

            first = daemon.poll()
            second = daemon.poll()

            self.assertTrue(first.first_run)
            self.assertTrue(second.significant_change)
            notify.assert_not_called()
            self.assertEqual(load_state(state)["last_snapshot"], AVAILABLE)
            self.assertTrue(Path(web).exists())
            self.assertTrue(Path(update).exists())

    @patch("quota_monitor.daemon.deliver_outbox", return_value=([], []))
    @patch("quota_monitor.daemon.send_notifications", return_value={"feishu": True})
    @patch("quota_monitor.daemon.fetch_snapshot", side_effect=[BASELINE, AVAILABLE])
    def test_live_mode_notifies_and_delivers_release_signal(
        self, fetch, notify, deliver
    ):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {
                "HKID_RELEASE_WEBHOOK_URL": "http://127.0.0.1:8765/internal/release",
                "HKID_RELEASE_WEBHOOK_SECRET": "x" * 32,
            },
            clear=True,
        ):
            state, web, update = self._paths(directory)
            daemon = QuotaDaemon(
                config(),
                state_path=state,
                web_data_path=web,
                last_update_path=update,
                shadow=False,
                session=Mock(),
            )

            daemon.poll()
            result = daemon.poll()

            self.assertTrue(result.significant_change)
            notify.assert_called_once()
            deliver.assert_called_once()
            self.assertEqual(load_state(state)["pending_release_signals"], [])

    @patch("quota_monitor.daemon.fetch_snapshot", return_value={})
    def test_failed_poll_does_not_replace_existing_state(self, fetch):
        with tempfile.TemporaryDirectory() as directory:
            state, web, update = self._paths(directory)
            Path(state).write_text(
                json.dumps(
                    {
                        "version": 1,
                        "last_snapshot": {"08/30/2026|RHK|R": "quota-r"},
                        "last_snapshot_time": "2026-08-24T00:00:00",
                    }
                ),
                encoding="utf-8",
            )
            daemon = QuotaDaemon(
                config(),
                state_path=state,
                web_data_path=web,
                last_update_path=update,
                shadow=True,
                session=Mock(),
            )

            self.assertFalse(daemon.poll().success)
            self.assertEqual(load_state(state)["last_snapshot"], BASELINE)


class LoopBackoffTests(unittest.TestCase):
    def test_failure_uses_exponential_backoff(self):
        daemon = Mock()
        daemon.config = config()
        daemon.poll.side_effect = [
            Mock(success=False),
            Mock(success=False),
            Mock(success=True),
        ]

        class StopAfterThree(threading.Event):
            def __init__(self):
                super().__init__()
                self.waits = []

            def wait(self, timeout=None):
                self.waits.append(timeout)
                if len(self.waits) == 3:
                    self.set()
                return self.is_set()

        stop = StopAfterThree()
        run_forever(
            daemon,
            interval=20,
            jitter=0,
            stop_event=stop,
            random_source=lambda _start, _end: 0,
        )

        self.assertEqual(stop.waits, [30, 60, 20])


if __name__ == "__main__":
    unittest.main()
