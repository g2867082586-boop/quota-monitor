"""Limit human quota notifications to a Hong Kong calendar window."""

from __future__ import annotations

from datetime import date, datetime, timedelta


def filter_notification_window(
    changes: dict,
    *,
    today: date,
    days_ahead: int = 14,
) -> dict:
    """Keep newly available rows dated from ``today`` through the boundary."""

    if days_ahead < 0:
        raise ValueError("days_ahead must not be negative")
    latest = today + timedelta(days=days_ahead)
    filtered = []
    for change in changes.get("newly_available", []):
        try:
            raw_date = change[0][0]
            appointment_date = datetime.strptime(raw_date, "%m/%d/%Y").date()
        except (IndexError, TypeError, ValueError):
            continue
        if today <= appointment_date <= latest:
            filtered.append(change)

    result = dict(changes)
    result["newly_available"] = filtered
    return result
