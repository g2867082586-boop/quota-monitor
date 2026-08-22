import unittest

from quota_monitor.notification_state import filter_repeat_releases


KEY = ("10/26/2026", "FTO", "R")


def release(old="quota-r", new="quota-y"):
    return {
        "newly_available": [(KEY, old, new)],
        "newly_full": [],
        "newly_added": [],
    }


class NotificationEpisodeTest(unittest.TestCase):
    def test_first_release_is_delivered_and_recorded(self):
        changes, episodes = filter_repeat_releases(
            release(), {KEY: "quota-y"}, {}, now=100
        )

        self.assertEqual(changes["newly_available"], release()["newly_available"])
        self.assertEqual(
            episodes["10/26/2026|FTO|R"],
            {"notified_at": 100.0, "unavailable_since": None},
        )

    def test_rapid_full_available_flapping_is_suppressed(self):
        _, episodes = filter_repeat_releases(
            release(), {KEY: "quota-y"}, {}, now=100
        )
        _, episodes = filter_repeat_releases(
            {"newly_available": []}, {KEY: "quota-r"}, episodes, now=130
        )
        changes, episodes = filter_repeat_releases(
            release(), {KEY: "quota-y"}, episodes, now=160
        )

        self.assertEqual(changes["newly_available"], [])
        self.assertIsNone(episodes["10/26/2026|FTO|R"]["unavailable_since"])

    def test_release_rearms_after_continuous_unavailability(self):
        _, episodes = filter_repeat_releases(
            release(), {KEY: "quota-y"}, {}, rearm_seconds=1800, now=100
        )
        _, episodes = filter_repeat_releases(
            {"newly_available": []},
            {KEY: "quota-r"},
            episodes,
            rearm_seconds=1800,
            now=200,
        )
        changes, episodes = filter_repeat_releases(
            release(),
            {KEY: "quota-y"},
            episodes,
            rearm_seconds=1800,
            now=2000,
        )

        self.assertEqual(changes["newly_available"], release()["newly_available"])
        self.assertEqual(episodes["10/26/2026|FTO|R"]["notified_at"], 2000.0)

    def test_zero_rearm_restores_red_yellow_flap_notifications(self):
        _, episodes = filter_repeat_releases(
            release(), {KEY: "quota-y"}, {}, rearm_seconds=0, now=100
        )
        _, episodes = filter_repeat_releases(
            {"newly_available": []},
            {KEY: "quota-r"},
            episodes,
            rearm_seconds=0,
            now=130,
        )
        changes, episodes = filter_repeat_releases(
            release(),
            {KEY: "quota-y"},
            episodes,
            rearm_seconds=0,
            now=160,
        )

        self.assertEqual(changes["newly_available"], release()["newly_available"])
        self.assertEqual(episodes["10/26/2026|FTO|R"]["notified_at"], 160.0)

    def test_rows_that_leave_the_snapshot_are_pruned(self):
        episodes = {
            "10/26/2026|FTO|R": {
                "notified_at": 100,
                "unavailable_since": None,
            }
        }

        _, episodes = filter_repeat_releases(
            {"newly_available": []}, {}, episodes, now=200
        )

        self.assertEqual(episodes, {})

    def test_invalid_persisted_entries_are_ignored(self):
        changes, episodes = filter_repeat_releases(
            release(),
            {KEY: "quota-y"},
            {"bad": "value", "10/26/2026|FTO|R": {"notified_at": "bad"}},
            now=100,
        )

        self.assertEqual(changes["newly_available"], release()["newly_available"])
        self.assertIn("10/26/2026|FTO|R", episodes)


if __name__ == "__main__":
    unittest.main()
