#!/usr/bin/env python3
"""Production daemon for direct, persistent quota polling on Linux."""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import signal
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import requests

from quota_monitor.core import (
    DEFAULT_OFFICES,
    detect_changes,
    export_web_data,
    fetch_snapshot,
    format_changes,
    has_significant_change,
    load_config,
)
from quota_monitor.monitor import setup_logging
from quota_monitor.notify import send_notifications
from quota_monitor.release_sink import build_release_signal, deliver_outbox
from quota_monitor.state import load_state, save_state

logger = logging.getLogger("quota_monitor")

DEFAULT_INTERVAL_SECONDS = 20
DEFAULT_JITTER_SECONDS = 3
MIN_INTERVAL_SECONDS = 20


@dataclass(frozen=True)
class PollResult:
    success: bool
    first_run: bool = False
    significant_change: bool = False
    snapshot_size: int = 0


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}


def _notification_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """Apply secret environment variables without writing them to config.json."""
    result = json.loads(json.dumps(config.get("notifications", {})))
    feishu = result.setdefault("feishu", {})
    wecom = result.setdefault("wecom", {})

    env_values = {
        "app_id": os.environ.get("FEISHU_APP_ID", ""),
        "app_secret": os.environ.get("FEISHU_APP_SECRET", ""),
        "chat_id": os.environ.get("FEISHU_CHAT_ID", ""),
        "webhook_url": os.environ.get("FEISHU_WEBHOOK_URL", ""),
    }
    for key, value in env_values.items():
        if value:
            feishu[key] = value
    if any(feishu.get(key) for key in ("webhook_url", "chat_id")):
        feishu["enabled"] = True

    wecom_url = os.environ.get("WECOM_WEBHOOK_URL", "")
    if wecom_url:
        wecom.update({"enabled": True, "webhook_url": wecom_url})
    return result


def _write_last_update(path: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(
        json.dumps({"time": now}, ensure_ascii=False), encoding="utf-8"
    )
    os.replace(temporary, target)


def _state_extra(state: Dict[str, Any]) -> Dict[str, Any]:
    ignored = {"version", "last_snapshot", "last_snapshot_time"}
    return {key: value for key, value in state.items() if key not in ignored}


class QuotaDaemon:
    """One reusable HTTP session plus an in-memory and on-disk baseline."""

    def __init__(
        self,
        config: Dict[str, Any],
        *,
        state_path: str,
        web_data_path: str,
        last_update_path: str,
        shadow: bool,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.config = config
        self.state_path = state_path
        self.web_data_path = web_data_path
        self.last_update_path = last_update_path
        self.shadow = shadow
        self.session = session or requests.Session()
        self._owns_session = session is None
        self.state = load_state(state_path)

    def close(self) -> None:
        if self._owns_session:
            self.session.close()

    def poll(self) -> PollResult:
        offices = self.config.get("offices", DEFAULT_OFFICES)
        office_codes = list(offices) if isinstance(offices, dict) else offices
        date_range = self.config.get("date_range", {})
        retry = self.config.get("retry", {})
        api = self.config.get("api", {})

        snapshot = fetch_snapshot(
            offices=office_codes,
            date_start=date_range.get("start"),
            date_end=date_range.get("end"),
            svc_id=api.get("svc_id", 579),
            timeout=api.get("timeout", 30),
            max_retries=retry.get("max_retries", 3),
            backoff_base=retry.get("backoff_base_seconds", 5),
            session=self.session,
        )
        if not snapshot:
            logger.error("本轮未获得有效快照，保留原基准状态")
            return PollResult(success=False)

        old_snapshot = self.state.get("last_snapshot", {})
        first_run = not old_snapshot
        changes = detect_changes(old_snapshot, snapshot)
        significant = not first_run and has_significant_change(changes)

        # Dashboard files are local runtime artifacts, not Git commits.
        export_web_data(snapshot, self.web_data_path)
        _write_last_update(self.last_update_path)

        extra = _state_extra(self.state)
        pending = extra.get("pending_release_signals", [])
        if not isinstance(pending, list):
            logger.warning("ReleaseSignal outbox 无效，已安全重置")
            pending = []
        else:
            pending = [item for item in pending if isinstance(item, dict)]

        release_url = os.environ.get("HKID_RELEASE_WEBHOOK_URL", "").strip()
        release_secret = os.environ.get("HKID_RELEASE_WEBHOOK_SECRET", "")

        if first_run:
            logger.info("首次运行：已建立 %d 条基准快照，不发送通知", len(snapshot))
        elif significant:
            message = format_changes(changes, offices)
            logger.warning("检测到 %d 个新配额", len(changes["newly_available"]))
            if self.shadow:
                logger.warning("影子模式：已抑制通知和预约唤醒事件")
            else:
                result = send_notifications(message, _notification_config(self.config))
                logger.info("通知结果: %s", result)
                if release_url and release_secret:
                    event = build_release_signal(changes["newly_available"])
                    known_ids = {item.get("event_id") for item in pending}
                    if event["event_id"] not in known_ids:
                        pending.append(event)
        else:
            available = sum(
                1 for value in snapshot.values() if value in {"quota-g", "quota-y"}
            )
            logger.info("配额无变化，当前有 %d 条可用记录", available)

        if pending and release_url and release_secret and not self.shadow:
            try:
                pending, rejected = deliver_outbox(
                    pending,
                    url=release_url,
                    secret=release_secret,
                    timeout_seconds=float(
                        os.environ.get("HKID_RELEASE_WEBHOOK_TIMEOUT_SECONDS", "10")
                    ),
                    max_attempts=int(
                        os.environ.get("HKID_RELEASE_WEBHOOK_MAX_ATTEMPTS", "3")
                    ),
                    backoff_seconds=float(
                        os.environ.get("HKID_RELEASE_WEBHOOK_BACKOFF_SECONDS", "1")
                    ),
                )
            except ValueError as exc:
                logger.error("ReleaseSignal 配置无效，事件已保留: %s", exc)
            else:
                if rejected:
                    logger.error("ReleaseSignal 被永久拒绝: %d 个", len(rejected))
                if pending:
                    logger.warning("ReleaseSignal 待重试: %d 个", len(pending))

        extra["pending_release_signals"] = pending
        save_state(self.state_path, snapshot, state_extra=extra)
        self.state = {
            "version": self.state.get("version", 1),
            "last_snapshot": snapshot,
            "last_snapshot_time": datetime.now().isoformat(),
            **extra,
        }
        return PollResult(
            success=True,
            first_run=first_run,
            significant_change=significant,
            snapshot_size=len(snapshot),
        )


def run_forever(
    daemon: QuotaDaemon,
    *,
    interval: float,
    jitter: float,
    stop_event: threading.Event,
    random_source=random.uniform,
) -> None:
    consecutive_failures = 0
    failure_base = float(
        daemon.config.get("daemon", {}).get("failure_backoff_base_seconds", 30)
    )
    failure_max = float(
        daemon.config.get("daemon", {}).get("failure_backoff_max_seconds", 900)
    )
    cycle = 0
    while not stop_event.is_set():
        cycle += 1
        logger.info("第 %d 轮轮询", cycle)
        result = daemon.poll()
        if result.success:
            consecutive_failures = 0
            wait_seconds = interval + random_source(-jitter, jitter)
        else:
            consecutive_failures += 1
            wait_seconds = max(
                interval,
                min(failure_max, failure_base * (2 ** (consecutive_failures - 1))),
            ) + random_source(0, jitter)
        wait_seconds = max(MIN_INTERVAL_SECONDS, wait_seconds)
        logger.info("%.1f 秒后进行下一轮", wait_seconds)
        stop_event.wait(wait_seconds)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="quota-monitor Linux 常驻轮询服务")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--state", default="state.json")
    parser.add_argument("--web-data", default="data/quota.json")
    parser.add_argument("--last-update", default="data/last_update.json")
    parser.add_argument("--interval", type=float)
    parser.add_argument("--jitter", type=float)
    parser.add_argument("--once", action="store_true")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--shadow", action="store_true", dest="shadow")
    mode.add_argument("--live", action="store_false", dest="shadow")
    parser.set_defaults(shadow=None)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    config = load_config(args.config)
    daemon_config = config.get("daemon", {})
    interval = args.interval
    if interval is None:
        interval = float(
            os.environ.get(
                "QUOTA_MONITOR_INTERVAL_SECONDS",
                daemon_config.get("interval_seconds", DEFAULT_INTERVAL_SECONDS),
            )
        )
    if interval < MIN_INTERVAL_SECONDS:
        raise SystemExit(f"轮询间隔不能小于 {MIN_INTERVAL_SECONDS} 秒")
    jitter = args.jitter
    if jitter is None:
        jitter = float(
            os.environ.get(
                "QUOTA_MONITOR_JITTER_SECONDS",
                daemon_config.get("jitter_seconds", DEFAULT_JITTER_SECONDS),
            )
        )
    if jitter < 0 or jitter >= interval:
        raise SystemExit("轮询抖动必须大于等于 0 且小于轮询间隔")
    shadow = args.shadow
    if shadow is None:
        shadow = _env_bool("QUOTA_MONITOR_SHADOW", daemon_config.get("shadow", True))

    for path in (args.state, args.web_data, args.last_update):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    log_file = config.get("log", {}).get("file")
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    setup_logging(config)
    logger.info(
        "启动常驻监控：间隔 %.1f 秒，抖动 %.1f 秒，模式=%s",
        interval,
        jitter,
        "shadow" if shadow else "live",
    )

    daemon = QuotaDaemon(
        config,
        state_path=args.state,
        web_data_path=args.web_data,
        last_update_path=args.last_update,
        shadow=shadow,
    )
    stop_event = threading.Event()

    def stop(signum: int, _frame: object) -> None:
        logger.info("收到信号 %s，正在安全停止", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        if args.once:
            return 0 if daemon.poll().success else 1
        run_forever(
            daemon,
            interval=interval,
            jitter=jitter,
            stop_event=stop_event,
        )
        return 0
    finally:
        daemon.close()


if __name__ == "__main__":
    raise SystemExit(main())
