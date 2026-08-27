"""Local durable compensation queue for failed NocoBase event upserts."""

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone


BUSY_TIMEOUT_MILLISECONDS = 5000
_ERROR_MESSAGE = "本机事件补偿队列不可用"
_SCHEMA = """
CREATE TABLE IF NOT EXISTS event_outbox (
    station_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    device_id TEXT NOT NULL,
    start_time TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (station_id, event_type, device_id, start_time)
)
"""


class EventOutboxError(ValueError):
    """The local event compensation queue could not be used safely."""


def _database_path(path):
    try:
        value = os.fspath(path)
    except TypeError as exc:
        raise EventOutboxError(_ERROR_MESSAGE) from exc
    if not isinstance(value, str) or not value.strip() or value == ":memory:":
        raise EventOutboxError(_ERROR_MESSAGE)
    return value


@contextmanager
def _connection(path):
    connection = None
    try:
        connection = sqlite3.connect(
            _database_path(path),
            timeout=BUSY_TIMEOUT_MILLISECONDS / 1000.0,
            isolation_level=None,
        )
        connection.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MILLISECONDS}")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute(_SCHEMA)
        yield connection
    except EventOutboxError:
        raise
    except (sqlite3.Error, OSError, TypeError, ValueError) as exc:
        raise EventOutboxError(_ERROR_MESSAGE) from exc
    finally:
        if connection is not None:
            connection.close()


def _identifier(value):
    if value is None or isinstance(value, bool) or not str(value).strip():
        raise EventOutboxError(_ERROR_MESSAGE)
    return str(value).strip()


def _canonical_start_time(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise EventOutboxError(_ERROR_MESSAGE) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise EventOutboxError(_ERROR_MESSAGE)
    return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _event_row(event):
    if not isinstance(event, dict):
        raise EventOutboxError(_ERROR_MESSAGE)
    identity = (
        _identifier(event.get("station_id")),
        _identifier(event.get("event_type")),
        _identifier(event.get("device_id")),
        _canonical_start_time(event.get("start_time")),
    )
    try:
        payload = json.dumps(
            event,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise EventOutboxError(_ERROR_MESSAGE) from exc
    return identity, payload


def _write(connection, statement, parameters):
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(statement, parameters)
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def enqueue_event(event, path):
    """Persist or replace the newest payload for one logical event."""
    identity, payload = _event_row(event)
    with _connection(path) as connection:
        _write(
            connection,
            """
            INSERT INTO event_outbox (
                station_id, event_type, device_id, start_time, payload_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(station_id, event_type, device_id, start_time)
            DO UPDATE SET payload_json = excluded.payload_json,
                          updated_at = CURRENT_TIMESTAMP
            """,
            (*identity, payload),
        )


def discard_event(event, path):
    """Remove only the exact queued payload whose remote upsert succeeded."""
    identity, payload = _event_row(event)
    with _connection(path) as connection:
        _write(
            connection,
            """
            DELETE FROM event_outbox
            WHERE station_id = ?
              AND event_type = ?
              AND device_id = ?
              AND start_time = ?
              AND payload_json = ?
            """,
            (*identity, payload),
        )


def load_station_events(station_id, path):
    """Return a station's queued payloads without holding a lock during network I/O."""
    station_id = _identifier(station_id)
    with _connection(path) as connection:
        rows = connection.execute(
            """
            SELECT payload_json FROM event_outbox
            WHERE station_id = ?
            ORDER BY updated_at, event_type, device_id, start_time
            """,
            (station_id,),
        ).fetchall()
    try:
        events = [json.loads(row[0]) for row in rows]
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise EventOutboxError(_ERROR_MESSAGE) from exc
    if not all(isinstance(event, dict) for event in events):
        raise EventOutboxError(_ERROR_MESSAGE)
    return events


def pending_event_count(station_id, path):
    station_id = _identifier(station_id)
    with _connection(path) as connection:
        row = connection.execute(
            "SELECT COUNT(*) FROM event_outbox WHERE station_id = ?",
            (station_id,),
        ).fetchone()
    count = row[0] if row else None
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise EventOutboxError(_ERROR_MESSAGE)
    return count


def flush_station_events(station_id, path, save_event):
    """Attempt all queued writes; successful rows are deleted, failures remain."""
    queued = load_station_events(station_id, path)
    saved = 0
    failed = 0
    for event in queued:
        try:
            save_event(event)
        except Exception:
            failed += 1
        else:
            discard_event(event, path)
            saved += 1
    return {
        "attempted": len(queued),
        "saved": saved,
        "failed": failed,
        "outbox_pending": pending_event_count(station_id, path),
    }
