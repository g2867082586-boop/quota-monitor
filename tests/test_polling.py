import os
import unittest
from unittest.mock import call, patch

from ci_run import _notification_rearm_seconds, _poll_settings, main


class PollSettingsTest(unittest.TestCase):
    def test_defaults_to_one_poll(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(_poll_settings(), (1, 30))

    def test_accepts_two_polls_thirty_seconds_apart(self):
        with patch.dict(
            os.environ,
            {"POLL_ITERATIONS": "2", "POLL_INTERVAL_SECONDS": "30"},
            clear=True,
        ):
            self.assertEqual(_poll_settings(), (2, 30))

    def test_rejects_out_of_range_values(self):
        with patch.dict(
            os.environ,
            {"POLL_ITERATIONS": "3", "POLL_INTERVAL_SECONDS": "61"},
            clear=True,
        ):
            self.assertEqual(_poll_settings(), (1, 30))

    def test_notification_rearm_defaults_to_immediate_flap_detection(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(_notification_rearm_seconds(), 0)

    def test_notification_rearm_rejects_invalid_values(self):
        with patch.dict(
            os.environ, {"NOTIFICATION_REARM_SECONDS": "90000"}, clear=True
        ):
            self.assertEqual(_notification_rearm_seconds(), 0)


class PollLoopTest(unittest.TestCase):
    @patch("ci_run.time.sleep")
    @patch("ci_run._run_poll_cycle")
    def test_repository_dispatch_runs_twice_and_sleeps_once(
        self, run_poll_cycle, sleep
    ):
        with patch.dict(
            os.environ,
            {
                "POLL_ITERATIONS": "2",
                "POLL_INTERVAL_SECONDS": "30",
                "WECOM_TEST_ONLY": "0",
            },
            clear=True,
        ):
            main()

        self.assertEqual(
            run_poll_cycle.call_args_list,
            [call(log_no_change=False), call(log_no_change=True)],
        )
        sleep.assert_called_once_with(30)

    @patch("ci_run.time.sleep")
    @patch("ci_run._run_poll_cycle")
    def test_manual_run_stays_single_poll(self, run_poll_cycle, sleep):
        with patch.dict(os.environ, {"WECOM_TEST_ONLY": "0"}, clear=True):
            main()

        run_poll_cycle.assert_called_once_with(log_no_change=True)
        sleep.assert_not_called()

    @patch("ci_run._send_wecom_broadcast", return_value=("OK", 1))
    @patch("ci_run._run_poll_cycle")
    def test_wecom_test_still_skips_all_polling(self, run_poll_cycle, send_wecom):
        with patch.dict(os.environ, {"WECOM_TEST_ONLY": "1"}, clear=True):
            main()

        send_wecom.assert_called_once()
        run_poll_cycle.assert_not_called()


if __name__ == "__main__":
    unittest.main()
