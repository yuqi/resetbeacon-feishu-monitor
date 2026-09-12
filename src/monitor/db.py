from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def payload_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def notification_key(source: str, item_id: str, payload: dict[str, Any]) -> str:
    if source == "history":
        return f"post:{payload.get('id') or item_id}"
    if source == "alerts":
        post_id = payload.get("postId")
        return f"post:{post_id}" if post_id else f"alert:{item_id}"
    return item_id


@dataclass(frozen=True)
class Delivery:
    id: int
    source: str
    item_id: str
    payload: dict[str, Any]
    attempt_count: int


@dataclass(frozen=True)
class TranslationDelivery:
    delivery_id: int
    source: str
    payload: dict[str, Any]
    body: str | None


class StateDB:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "StateDB":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _migrate(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS items (
                source TEXT NOT NULL,
                item_id TEXT NOT NULL,
                current_hash TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (source, item_id)
            );

            CREATE TABLE IF NOT EXISTS deliveries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                item_id TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('pending', 'delivered')),
                attempt_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                created_at TEXT NOT NULL,
                delivered_at TEXT,
                original_skipped INTEGER NOT NULL DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS deliveries_pending_idx
                ON deliveries(status, id);

            CREATE TABLE IF NOT EXISTS translation_deliveries (
                delivery_id INTEGER PRIMARY KEY REFERENCES deliveries(id),
                body TEXT,
                status TEXT NOT NULL CHECK(status IN ('pending', 'delivered')),
                attempt_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                created_at TEXT NOT NULL,
                delivered_at TEXT
            );

            CREATE TABLE IF NOT EXISTS notification_keys (
                source TEXT NOT NULL,
                notification_key TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                PRIMARY KEY (source, notification_key)
            );

            CREATE TABLE IF NOT EXISTS source_state (
                source TEXT PRIMARY KEY,
                consecutive_failures INTEGER NOT NULL DEFAULT 0,
                outage_number INTEGER NOT NULL DEFAULT 0,
                outage_alerted INTEGER NOT NULL DEFAULT 0,
                last_success_at TEXT,
                last_error TEXT
            );
            """
        )
        columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(deliveries)")}
        if "original_skipped" not in columns:
            self.connection.execute(
                "ALTER TABLE deliveries ADD COLUMN original_skipped INTEGER NOT NULL DEFAULT 0"
            )
        self._seed_notification_keys()
        self.connection.commit()

    def _seed_notification_keys(self) -> None:
        migration_key = "migration:notification-keys:v1"
        migrated = self.connection.execute(
            "SELECT 1 FROM metadata WHERE key = ?",
            (migration_key,),
        ).fetchone()
        if migrated is not None:
            return

        rows = self.connection.execute(
            """
            SELECT source, item_id, payload_json, first_seen_at
            FROM items
            WHERE source IN ('history', 'alerts')
            """
        ).fetchall()
        for row in rows:
            payload = json.loads(row["payload_json"])
            key = notification_key(row["source"], row["item_id"], payload)
            self.connection.execute(
                """
                INSERT OR IGNORE INTO notification_keys(
                    source, notification_key, first_seen_at
                ) VALUES (?, ?, ?)
                """,
                (row["source"], key, row["first_seen_at"]),
            )
        self.connection.execute(
            "INSERT INTO metadata(key, value) VALUES (?, '1')",
            (migration_key,),
        )

    def is_initialized(self, source: str) -> bool:
        row = self.connection.execute(
            "SELECT value FROM metadata WHERE key = ?",
            (f"initialized:{source}",),
        ).fetchone()
        return row is not None and row["value"] == "1"

    def mark_initialized(self, source: str) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO metadata(key, value) VALUES (?, '1')",
            (f"initialized:{source}",),
        )
        self.connection.commit()

    def has_item(self, source: str, item_id: str) -> bool:
        return (
            self.connection.execute(
                "SELECT 1 FROM items WHERE source = ? AND item_id = ?",
                (source, item_id),
            ).fetchone()
            is not None
        )

    def has_notification_key(self, source: str, key: str) -> bool:
        return (
            self.connection.execute(
                """
                SELECT 1 FROM notification_keys
                WHERE source = ? AND notification_key = ?
                """,
                (source, key),
            ).fetchone()
            is not None
        )

    def detect(self, source: str, item_id: str, payload: dict[str, Any]) -> str:
        row = self.connection.execute(
            "SELECT current_hash FROM items WHERE source = ? AND item_id = ?",
            (source, item_id),
        ).fetchone()
        if row is None:
            return "new"
        return "same" if row["current_hash"] == payload_hash(payload) else "changed"

    def observe(
        self,
        source: str,
        item_id: str,
        payload: dict[str, Any],
        *,
        notify: bool,
    ) -> str:
        state = self.detect(source, item_id, payload)
        if state == "same":
            return state

        now = utc_now()
        digest = payload_hash(payload)
        serialized = canonical_json(payload)
        self.connection.execute(
            """
            INSERT INTO items(source, item_id, current_hash, payload_json, first_seen_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(source, item_id) DO UPDATE SET
                current_hash = excluded.current_hash,
                payload_json = excluded.payload_json,
                updated_at = excluded.updated_at
            """,
            (source, item_id, digest, serialized, now, now),
        )
        if notify:
            self.connection.execute(
                """
                INSERT INTO deliveries(
                    source, item_id, payload_hash, payload_json, status, created_at
                ) VALUES (?, ?, ?, ?, 'pending', ?)
                """,
                (source, item_id, digest, serialized, now),
            )
        self.connection.commit()
        return state

    def observe_once(
        self,
        source: str,
        item_id: str,
        payload: dict[str, Any],
        *,
        key: str,
        notify: bool,
    ) -> tuple[str, bool]:
        state = self.detect(source, item_id, payload)
        now = utc_now()
        digest = payload_hash(payload)
        serialized = canonical_json(payload)

        if state != "same":
            self.connection.execute(
                """
                INSERT INTO items(
                    source, item_id, current_hash, payload_json, first_seen_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(source, item_id) DO UPDATE SET
                    current_hash = excluded.current_hash,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (source, item_id, digest, serialized, now, now),
            )

        inserted = self.connection.execute(
            """
            INSERT OR IGNORE INTO notification_keys(
                source, notification_key, first_seen_at
            ) VALUES (?, ?, ?)
            """,
            (source, key, now),
        ).rowcount
        first_seen = inserted == 1
        if notify and first_seen:
            self.connection.execute(
                """
                INSERT INTO deliveries(
                    source, item_id, payload_hash, payload_json, status, created_at
                ) VALUES (?, ?, ?, ?, 'pending', ?)
                """,
                (source, item_id, digest, serialized, now),
            )
        self.connection.commit()
        return state, first_seen

    def pending_deliveries(self) -> list[Delivery]:
        rows = self.connection.execute(
            """
            SELECT id, source, item_id, payload_json, attempt_count
            FROM deliveries
            WHERE status = 'pending'
            ORDER BY id
            """
        ).fetchall()
        return [
            Delivery(
                id=row["id"],
                source=row["source"],
                item_id=row["item_id"],
                payload=json.loads(row["payload_json"]),
                attempt_count=row["attempt_count"],
            )
            for row in rows
        ]

    def mark_delivered(
        self, delivery_id: int, *, queue_translation: bool = False,
        skip_original: bool = False,
    ) -> None:
        # Close the original stage and record its Chinese follow-up atomically.
        with self.connection:
            self.connection.execute(
                """
                UPDATE deliveries
                SET status = 'delivered', delivered_at = ?, original_skipped = ?, last_error = NULL
                WHERE id = ?
                """,
                (None if skip_original else utc_now(), int(skip_original), delivery_id),
            )
            if queue_translation:
                self.connection.execute(
                    """
                    INSERT OR IGNORE INTO translation_deliveries(delivery_id, status, created_at)
                    VALUES (?, 'pending', ?)
                    """,
                    (delivery_id, utc_now()),
                )

    def pending_translations(self) -> list[TranslationDelivery]:
        rows = self.connection.execute(
            """
            SELECT t.delivery_id, d.source, d.payload_json, t.body
            FROM translation_deliveries t JOIN deliveries d ON d.id = t.delivery_id
            WHERE t.status = 'pending' AND d.status = 'delivered'
            ORDER BY t.delivery_id
            """
        ).fetchall()
        return [
            TranslationDelivery(
                delivery_id=row["delivery_id"],
                source=row["source"],
                payload=json.loads(row["payload_json"]),
                body=row["body"],
            )
            for row in rows
        ]

    def save_translation(self, delivery_id: int, body: str) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE translation_deliveries SET body = ? WHERE delivery_id = ?",
                (body, delivery_id),
            )

    def mark_translation_delivered(self, delivery_id: int) -> None:
        with self.connection:
            self.connection.execute(
                """
                UPDATE translation_deliveries
                SET status = 'delivered', delivered_at = ?, last_error = NULL
                WHERE delivery_id = ?
                """,
                (utc_now(), delivery_id),
            )

    def mark_translation_failed(self, delivery_id: int, error: str) -> None:
        with self.connection:
            self.connection.execute(
                """
                UPDATE translation_deliveries
                SET attempt_count = attempt_count + 1, last_error = ?
                WHERE delivery_id = ?
                """,
                (error[:1000], delivery_id),
            )

    def mark_failed(self, delivery_id: int, error: str) -> None:
        self.connection.execute(
            """
            UPDATE deliveries
            SET attempt_count = attempt_count + 1, last_error = ?
            WHERE id = ?
            """,
            (error[:1000], delivery_id),
        )
        self.connection.commit()

    def record_source_success(self, source: str) -> int | None:
        row = self.connection.execute(
            "SELECT outage_number, outage_alerted FROM source_state WHERE source = ?",
            (source,),
        ).fetchone()
        recovered_outage = (
            int(row["outage_number"])
            if row is not None and row["outage_alerted"]
            else None
        )
        self.connection.execute(
            """
            INSERT INTO source_state(source, consecutive_failures, last_success_at, last_error)
            VALUES (?, 0, ?, NULL)
            ON CONFLICT(source) DO UPDATE SET
                consecutive_failures = 0,
                outage_alerted = 0,
                last_success_at = excluded.last_success_at,
                last_error = NULL
            """,
            (source, utc_now()),
        )
        self.connection.commit()
        return recovered_outage

    def record_source_failure(self, source: str, error: str) -> tuple[int, int, bool]:
        row = self.connection.execute(
            """
            SELECT consecutive_failures, outage_number, outage_alerted
            FROM source_state WHERE source = ?
            """,
            (source,),
        ).fetchone()
        failures = int(row["consecutive_failures"]) if row else 0
        outage_number = int(row["outage_number"]) if row else 0
        alerted = bool(row["outage_alerted"]) if row else False
        if failures == 0:
            outage_number += 1
        failures += 1
        should_alert = failures >= 3 and not alerted
        self.connection.execute(
            """
            INSERT INTO source_state(
                source, consecutive_failures, outage_number, outage_alerted, last_error
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(source) DO UPDATE SET
                consecutive_failures = excluded.consecutive_failures,
                outage_number = excluded.outage_number,
                outage_alerted = excluded.outage_alerted,
                last_error = excluded.last_error
            """,
            (source, failures, outage_number, int(alerted or should_alert), error[:1000]),
        )
        self.connection.commit()
        return failures, outage_number, should_alert

    def stats(self) -> dict[str, int]:
        return {
            "items": self.connection.execute("SELECT COUNT(*) FROM items").fetchone()[0],
            "pending": self.connection.execute(
                "SELECT COUNT(*) FROM deliveries WHERE status = 'pending'"
            ).fetchone()[0],
            "delivered": self.connection.execute(
                "SELECT COUNT(*) FROM deliveries WHERE status = 'delivered' AND original_skipped = 0"
            ).fetchone()[0],
            "original_skipped": self.connection.execute(
                "SELECT COUNT(*) FROM deliveries WHERE original_skipped = 1"
            ).fetchone()[0],
            "translation_pending": self.connection.execute(
                "SELECT COUNT(*) FROM translation_deliveries WHERE status = 'pending'"
            ).fetchone()[0],
            "translation_delivered": self.connection.execute(
                "SELECT COUNT(*) FROM translation_deliveries WHERE status = 'delivered'"
            ).fetchone()[0],
        }
