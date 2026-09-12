import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional

from config import DB_PATH


@contextmanager
def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _column_exists(conn, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row["name"] == column for row in rows)


def init_db() -> None:
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS members (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                is_approved INTEGER NOT NULL DEFAULT 0,
                linked_account_ref TEXT,
                linked_at TEXT,
                joined_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS allowed_users (
                user_id INTEGER PRIMARY KEY,
                added_by INTEGER NOT NULL,
                added_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_clients (
                username TEXT PRIMARY KEY,
                added_by INTEGER NOT NULL,
                added_at TEXT NOT NULL,
                pin TEXT
            )
            """
        )
        if not _column_exists(conn, "agent_clients", "pin"):
            conn.execute("ALTER TABLE agent_clients ADD COLUMN pin TEXT")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS held_numbers_cache (
                client_username TEXT PRIMARY KEY,
                numbers TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS range_assignments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_username TEXT NOT NULL,
                range_id TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                assigned_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_range_assignments_lookup
            ON range_assignments (client_username, range_id, assigned_at)
            """
        )


def upsert_member(user_id: int, username: Optional[str], full_name: str) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO members (user_id, username, full_name, joined_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                full_name = excluded.full_name
            """,
            (user_id, username, full_name, datetime.now(timezone.utc).isoformat()),
        )


def get_member(user_id: int) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM members WHERE user_id = ?", (user_id,)
        ).fetchone()
        return dict(row) if row else None


def list_members() -> list:
    with _connect() as conn:
        return [
            dict(row)
            for row in conn.execute("SELECT * FROM members ORDER BY joined_at")
        ]


def set_approved(user_id: int, approved: bool) -> bool:
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE members SET is_approved = ? WHERE user_id = ?",
            (int(approved), user_id),
        )
        return cur.rowcount > 0


def link_account(user_id: int, account_ref: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE members SET linked_account_ref = ?, linked_at = ? WHERE user_id = ?",
            (account_ref, datetime.now(timezone.utc).isoformat(), user_id),
        )


def unlink_account(user_id: int) -> bool:
    with _connect() as conn:
        cur = conn.execute(
            """
            UPDATE members
            SET linked_account_ref = NULL, linked_at = NULL, is_approved = 0
            WHERE user_id = ? AND linked_account_ref IS NOT NULL
            """,
            (user_id,),
        )
        return cur.rowcount > 0


def is_allowed(user_id: int) -> bool:
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM allowed_users WHERE user_id = ?", (user_id,)
        ).fetchone()
        return row is not None


def add_allowed_user(user_id: int, added_by: int) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO allowed_users (user_id, added_by, added_at) VALUES (?, ?, ?)",
            (user_id, added_by, datetime.now(timezone.utc).isoformat()),
        )


def remove_allowed_user(user_id: int) -> bool:
    with _connect() as conn:
        cur = conn.execute("DELETE FROM allowed_users WHERE user_id = ?", (user_id,))
        return cur.rowcount > 0


def is_agent_client(username: str) -> bool:
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM agent_clients WHERE username = ? COLLATE NOCASE", (username,)
        ).fetchone()
        return row is not None


def resolve_agent_client(username: str) -> Optional[str]:
    """Return the canonically-cased registered username matching `username`, or None."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT username FROM agent_clients WHERE username = ? COLLATE NOCASE", (username,)
        ).fetchone()
        return row["username"] if row else None


def add_agent_client(username: str, added_by: int) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO agent_clients (username, added_by, added_at) VALUES (?, ?, ?)",
            (username, added_by, datetime.now(timezone.utc).isoformat()),
        )


def set_client_pin(username: str, pin: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE agent_clients SET pin = ? WHERE username = ? COLLATE NOCASE",
            (pin, username),
        )


def get_client_pin(username: str) -> Optional[str]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT pin FROM agent_clients WHERE username = ? COLLATE NOCASE",
            (username,),
        ).fetchone()
        return row["pin"] if row else None


def list_agent_clients() -> list:
    with _connect() as conn:
        return [
            dict(row)
            for row in conn.execute("SELECT * FROM agent_clients ORDER BY added_at")
        ]


def log_range_assignment(client_username: str, range_id: str, quantity: int) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO range_assignments (client_username, range_id, quantity, assigned_at) "
            "VALUES (?, ?, ?, ?)",
            (client_username, range_id, quantity, datetime.now(timezone.utc).isoformat()),
        )


def get_range_assignment_count(client_username: str, range_id: str, since_iso: str) -> int:
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(quantity), 0) AS total FROM range_assignments
            WHERE client_username = ? COLLATE NOCASE AND range_id = ? AND assigned_at >= ?
            """,
            (client_username, range_id, since_iso),
        ).fetchone()
        return row["total"]


def list_distinct_linked_clients() -> list:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT linked_account_ref FROM members "
            "WHERE is_approved = 1 AND linked_account_ref IS NOT NULL"
        ).fetchall()
        return [row["linked_account_ref"] for row in rows]


def get_held_numbers_cache(client_username: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT numbers, updated_at FROM held_numbers_cache WHERE client_username = ? COLLATE NOCASE",
            (client_username,),
        ).fetchone()
        if not row:
            return None
        return {"numbers": json.loads(row["numbers"]), "updated_at": row["updated_at"]}


def set_held_numbers_cache(client_username: str, numbers: list) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO held_numbers_cache (client_username, numbers, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(client_username) DO UPDATE SET
                numbers = excluded.numbers, updated_at = excluded.updated_at
            """,
            (client_username, json.dumps(numbers), datetime.now(timezone.utc).isoformat()),
        )


def get_state(key: str) -> Optional[str]:
    with _connect() as conn:
        row = conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None


def set_state(key: str, value: str) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO state (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
