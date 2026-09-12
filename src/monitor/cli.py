from __future__ import annotations

import argparse
import json
import logging
import sys
from contextlib import ExitStack

from .api import ResetBeaconClient
from .config import Config, ConfigError
from .db import StateDB
from .feishu import FeishuSender
from .locking import AlreadyRunningError, process_lock
from .service import MonitorService
from .translation import OpenAITranslator


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Forward Reset Beacon events to Feishu")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_once = subparsers.add_parser("run-once", help="run one polling cycle")
    run_once.add_argument("--dry-run", action="store_true", help="fetch and preview without writes")
    subparsers.add_parser("health", help="check configuration, database and Reset Beacon")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = Config.from_env()
        logging.basicConfig(
            level=getattr(logging, config.log_level, logging.INFO),
            format="%(asctime)s %(levelname)s %(message)s",
        )
        # httpx logs full request URLs at INFO; a Feishu Webhook URL is a secret.
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        if args.command == "health":
            return _health(config)
        return _run_once(config, dry_run=args.dry_run)
    except ConfigError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2


def _run_once(config: Config, *, dry_run: bool) -> int:
    if not dry_run:
        config.require_delivery_config()
    lock_path = config.db_path.with_suffix(config.db_path.suffix + ".lock")
    try:
        with process_lock(lock_path), StateDB(config.db_path) as db, ResetBeaconClient(
            history_url=config.history_url,
            alerts_url=config.alerts_url,
            health_url=config.health_url,
            timeout_seconds=config.timeout_seconds,
            max_retries=config.max_retries,
        ) as api:
            if dry_run:
                summary = MonitorService(db, api, None).run_once(dry_run=True)
            else:
                assert config.webhook_url is not None
                with ExitStack() as stack:
                    sender = stack.enter_context(FeishuSender(
                        config.webhook_url,
                        signing_secret=config.signing_secret,
                        timeout_seconds=config.timeout_seconds,
                        max_retries=config.max_retries,
                    ))
                    translator = None
                    if config.translation_enabled:
                        translator = stack.enter_context(OpenAITranslator(
                            base_url=config.translation_base_url,
                            api_key=config.translation_api_key,
                            model=config.translation_model,
                            timeout_seconds=config.translation_timeout_seconds,
                            max_retries=config.translation_max_retries,
                        ))
                    summary = MonitorService(db, api, sender, translator).run_once()
    except AlreadyRunningError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 3
    print(json.dumps(summary.to_dict(), ensure_ascii=False, indent=2))
    return 1 if summary.errors else 0


def _health(config: Config) -> int:
    result: dict[str, object] = {
        "webhookConfigured": bool(config.webhook_url),
        "signingConfigured": bool(config.signing_secret),
        "translationConfigured": config.translation_enabled,
        "database": str(config.db_path.resolve()),
    }
    try:
        with StateDB(config.db_path) as db:
            result["databaseStats"] = db.stats()
        with ResetBeaconClient(
            history_url=config.history_url,
            alerts_url=config.alerts_url,
            health_url=config.health_url,
            timeout_seconds=config.timeout_seconds,
            max_retries=config.max_retries,
        ) as api:
            health = api.fetch_health()
            reset_status = health.get("status")
            result["resetBeacon"] = {
                "status": reset_status,
                "reasons": health.get("reasons") or [],
                "checkedAt": health.get("checkedAt"),
            }
        result["ok"] = reset_status == "ok"
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ok"] else 1
    except Exception as exc:
        result["ok"] = False
        result["error"] = str(exc)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    sys.exit(main())
