"""Persistent release-episode deduplication for notification channels."""

from __future__ import annotations

import time
from collections.abc import Mapping

from quota_monitor.core import is_available


def filter_repeat_releases(
    changes,
    snapshot,
    episodes,
    *,
    rearm_seconds=1800,
    now=None,
):
    """Suppress rapid unavailable/available flapping for the same quota row.

    A notified row is eligible again only after it has remained continuously
    unavailable for ``rearm_seconds``.  The returned episode ledger is JSON
    serializable and can be persisted alongside the quota snapshot.
    """

    if rearm_seconds < 0:
        raise ValueError("rearm_seconds must not be negative")
    observed_at = float(time.time() if now is None else now)
    cleaned = _clean_episodes(episodes)
    newly_available = {
        "|".join(key): (key, old_status, new_status)
        for key, old_status, new_status in changes.get("newly_available", [])
    }

    for key_text in list(cleaned):
        key = tuple(key_text.split("|"))
        if len(key) != 3 or key not in snapshot:
            del cleaned[key_text]
            continue

        entry = cleaned[key_text]
        unavailable_since = entry["unavailable_since"]
        if not is_available(snapshot[key]):
            if unavailable_since is None:
                entry["unavailable_since"] = observed_at
            elif observed_at - unavailable_since >= rearm_seconds:
                del cleaned[key_text]
            continue

        if (
            unavailable_since is not None
            and observed_at - unavailable_since >= rearm_seconds
        ):
            del cleaned[key_text]
        else:
            entry["unavailable_since"] = None

    filtered = []
    for key_text, change in newly_available.items():
        if key_text in cleaned:
            continue
        filtered.append(change)
        cleaned[key_text] = {
            "notified_at": observed_at,
            "unavailable_since": None,
        }

    result = dict(changes)
    result["newly_available"] = filtered
    return result, cleaned


def _clean_episodes(episodes):
    if not isinstance(episodes, Mapping):
        return {}

    cleaned = {}
    for key, value in episodes.items():
        if not isinstance(key, str) or len(key.split("|")) != 3:
            continue
        if not isinstance(value, Mapping):
            continue
        notified_at = value.get("notified_at")
        unavailable_since = value.get("unavailable_since")
        if not isinstance(notified_at, (int, float)):
            continue
        if unavailable_since is not None and not isinstance(
            unavailable_since, (int, float)
        ):
            continue
        cleaned[key] = {
            "notified_at": float(notified_at),
            "unavailable_since": (
                None if unavailable_since is None else float(unavailable_since)
            ),
        }
    return cleaned
