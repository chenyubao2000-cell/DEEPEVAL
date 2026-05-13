"""Postgres helper for metrics that verify Mira's persisted state.

Currently only ``SessionHealthMetric``'s persistence layer uses this; it
checks that user/assistant messages are properly paired and each assistant
turn's final ``parts[-1]`` is in a valid terminal state.

Reads ``TEST_DATABASE_URL`` from the loaded ``.env`` file. Lazy + cached
connection — same pattern as the Langfuse client.
"""
from __future__ import annotations

import os
from typing import Any

import psycopg

from tests.evals._env import load_env


_CONN: Any = None


def _ensure_env_loaded() -> None:
    # ``load_env`` is idempotent; calling each time is cheap.
    load_env()


def _connect() -> Any:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        raise RuntimeError("TEST_DATABASE_URL not set in env; cannot run DB metric")
    # keepalives: nudge the OS / Railway proxy to keep the SSL tunnel alive on
    # idle gaps. Doesn't help if the proxy hard-drops anyway — we still
    # pre-ping + retry below.
    return psycopg.connect(
        url,
        autocommit=True,
        keepalives=1,
        keepalives_idle=30,
        keepalives_interval=10,
        keepalives_count=3,
    )


def get_conn() -> Any:
    """Return a long-lived psycopg connection.

    Railway's proxy silently drops idle SSL connections, but psycopg's
    ``_CONN.closed`` only flips when the *client* closes the handle — so a
    naive cache returns zombies that explode on the next ``execute``. We
    pre-ping with ``SELECT 1`` and reconnect on any failure.
    """
    global _CONN
    _ensure_env_loaded()

    if _CONN is not None and not _CONN.closed:
        try:
            with _CONN.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
            return _CONN
        except Exception:  # noqa: BLE001 — drop zombie + reconnect
            try:
                _CONN.close()
            except Exception:  # noqa: BLE001
                pass
            _CONN = None

    _CONN = _connect()
    return _CONN


def fetch_messages(chat_id: str) -> list[dict[str, Any]]:
    """Return all messages for a chat in sequence order. One row per message.

    Columns returned: role, sequence_num, parts (JSONB), metadata (JSONB).
    ``parts``/``metadata`` come back as already-decoded Python objects.

    Reconnects + retries once on connection-level failures, so a single
    Railway-proxy EOF doesn't surface as a metric ERROR.
    """
    global _CONN
    sql = """
        SELECT role, sequence_num, parts, metadata
        FROM messages
        WHERE chat_id = %s
        ORDER BY sequence_num ASC
    """
    for attempt in (1, 2):
        try:
            with get_conn().cursor() as cur:
                cur.execute(sql, (chat_id,))
                cols = [d.name for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
        except (psycopg.OperationalError, psycopg.InterfaceError):
            if _CONN is not None:
                try:
                    _CONN.close()
                except Exception:  # noqa: BLE001
                    pass
            _CONN = None
            if attempt == 2:
                raise
    return []  # unreachable, kept for type-checker
