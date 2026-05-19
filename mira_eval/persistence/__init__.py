"""Adapter to Mira's Postgres — only SessionHealth uses this today.

Lazy-connected, retries on Railway proxy drops. See `db.py` for the
connection pool semantics.
"""
