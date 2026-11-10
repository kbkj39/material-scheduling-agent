"""Transactional storage for draft receipts; this module never publishes content."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class StorageError(Exception):
    """A stable business error safe to return in a tool observation."""

    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


def _instant(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("timezone required")
        return parsed.astimezone(UTC).isoformat()
    except (TypeError, ValueError) as exc:
        raise StorageError("invalid_argument", "A timestamp with an explicit UTC offset is required.") from exc


class DraftStore:
    """SQLite transactions provide cross-process idempotency and slot exclusion.

    A pending-confirmation draft tentatively occupies its proposed slot too.
    Replaying the same key returns its original receipt before checking current
    occupancy. Such a receipt does not assert that a slot is still available.
    """

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._memory: sqlite3.Connection | None = None
        try:
            if self.db_path == ":memory:":
                self._memory = sqlite3.connect(":memory:", timeout=5)
            else:
                Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
            connection = self._connect()
            try:
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS drafts (
                        request_id TEXT NOT NULL,
                        asset_id TEXT NOT NULL,
                        fingerprint TEXT NOT NULL,
                        proposed_at TEXT NOT NULL,
                        payload TEXT NOT NULL,
                        PRIMARY KEY (request_id, asset_id)
                    )"""
                )
                connection.commit()
            finally:
                self._close(connection)
        except (sqlite3.Error, OSError) as exc:
            raise StorageError("storage_error", "Unable to initialize the draft store.", True) from exc

    def _connect(self) -> sqlite3.Connection:
        if self._memory is not None:
            return self._memory
        return sqlite3.connect(self.db_path, timeout=5)

    def _close(self, connection: sqlite3.Connection) -> None:
        if connection is not self._memory:
            connection.close()

    @staticmethod
    def _receipt(row: tuple[Any, ...] | None, fingerprint: str) -> dict[str, Any] | None:
        if row is None:
            return None
        stored_fingerprint, payload = row
        if stored_fingerprint != fingerprint:
            raise StorageError(
                "idempotency_conflict",
                "This request ID and asset already have a draft for different request parameters.",
            )
        try:
            receipt = json.loads(payload)
            if not isinstance(receipt, dict):
                raise ValueError("invalid payload")
            return receipt
        except (ValueError, TypeError) as exc:
            raise StorageError("storage_error", "The stored draft receipt is invalid.") from exc

    def get(self, request_id: str, asset_id: str, fingerprint: str) -> dict[str, Any] | None:
        connection = None
        try:
            connection = self._connect()
            row = connection.execute(
                "SELECT fingerprint, payload FROM drafts WHERE request_id = ? AND asset_id = ?",
                (request_id, asset_id),
            ).fetchone()
            return self._receipt(row, fingerprint)
        except (sqlite3.Error, OSError) as exc:
            raise StorageError("storage_error", "Unable to read the draft store.", True) from exc
        finally:
            if connection is not None:
                self._close(connection)

    def occupied(self, exclude_request_id: str | None = None, exclude_asset_id: str | None = None) -> list[str]:
        connection = None
        try:
            connection = self._connect()
            if exclude_request_id is not None and exclude_asset_id is not None:
                rows = connection.execute(
                    "SELECT proposed_at FROM drafts WHERE NOT (request_id = ? AND asset_id = ?)",
                    (exclude_request_id, exclude_asset_id),
                ).fetchall()
            else:
                rows = connection.execute("SELECT proposed_at FROM drafts").fetchall()
            return [row[0] for row in rows]
        except (sqlite3.Error, OSError) as exc:
            raise StorageError("storage_error", "Unable to read occupied draft slots.", True) from exc
        finally:
            if connection is not None:
                self._close(connection)

    def save(
        self,
        request_id: str,
        asset_id: str,
        fingerprint: str,
        draft: dict[str, Any],
        external_occupied: list[str],
    ) -> tuple[dict[str, Any], bool]:
        connection = None
        try:
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT fingerprint, payload FROM drafts WHERE request_id = ? AND asset_id = ?",
                (request_id, asset_id),
            ).fetchone()
            existing = self._receipt(row, fingerprint)
            if existing is not None:
                connection.commit()
                return existing, True

            if draft.get("request_id") != request_id or draft.get("asset_id") != asset_id or draft.get("status") != "draft":
                raise StorageError("invalid_argument", "The draft receipt does not match the request and asset.")
            proposed_at = _instant(draft.get("proposed_at"))
            occupied = {_instant(value) for value in external_occupied}
            occupied.update(
                _instant(row[0])
                for row in connection.execute("SELECT proposed_at FROM drafts").fetchall()
            )
            if proposed_at in occupied:
                raise StorageError("slot_conflict", "The proposed slot is already occupied.", True)

            # Round-trip JSON creates a detached receipt matching exactly what
            # was committed, rather than returning the caller's mutable object.
            payload = json.dumps(draft, ensure_ascii=False, sort_keys=True)
            connection.execute(
                "INSERT INTO drafts (request_id, asset_id, fingerprint, proposed_at, payload) VALUES (?, ?, ?, ?, ?)",
                (request_id, asset_id, fingerprint, proposed_at, payload),
            )
            connection.commit()
            return json.loads(payload), False
        except StorageError:
            if connection is not None:
                connection.rollback()
            raise
        except (sqlite3.Error, OSError) as exc:
            if connection is not None:
                connection.rollback()
            raise StorageError("storage_error", "Unable to save the draft.", True) from exc
        except (TypeError, ValueError) as exc:
            if connection is not None:
                connection.rollback()
            raise StorageError("invalid_argument", "The draft must be JSON serializable.") from exc
        finally:
            if connection is not None:
                self._close(connection)
