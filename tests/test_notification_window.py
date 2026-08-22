import unittest
from datetime import date

from quota_monitor.notification_window import filter_notification_window


def release(day: str):
    return ((day, "FTO", "R"), "quota-r", "quota-y")


class NotificationWindowTest(unittest.TestCase):
    def test_keeps_today_and_exact_two_week_boundary(self):
        changes = {
            "newly_available": [
                release("08/22/2026"),
                release("09/05/2026"),
            ]
        }

        filtered = filter_notification_window(
            changes,
            today=date(2026, 8, 22),
        )

        self.assertEqual(filtered["newly_available"], changes["newly_available"])

    def test_excludes_past_and_more_than_two_weeks_ahead(self):
        changes = {
            "newly_available": [
                release("08/21/2026"),
                release("09/06/2026"),
            ],
            "newly_full": ["preserved"],
        }

        filtered = filter_notification_window(
            changes,
            today=date(2026, 8, 22),
        )

        self.assertEqual(filtered["newly_available"], [])
        self.assertEqual(filtered["newly_full"], ["preserved"])

    def test_mixed_release_only_keeps_qualifying_rows(self):
        qualifying = release("08/30/2026")
        filtered = filter_notification_window(
            {"newly_available": [qualifying, release("09/30/2026")]},
            today=date(2026, 8, 22),
        )

        self.assertEqual(filtered["newly_available"], [qualifying])

    def test_invalid_dates_fail_closed_for_human_notifications(self):
        filtered = filter_notification_window(
            {"newly_available": [release("not-a-date")]},
            today=date(2026, 8, 22),
        )

        self.assertEqual(filtered["newly_available"], [])


if __name__ == "__main__":
    unittest.main()
