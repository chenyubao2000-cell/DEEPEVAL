"""Environment selection for the eval harness.

Each Mira deployment (preview / staging / prod / ...) has its own BFF URL,
session token, and Langfuse project. We keep one `.env.<name>` file per
environment and pick which one to load via the `MIRA_ENV` variable (default
`preview`).

Back-compat: if `.env.<name>` is missing, fall back to plain `.env` so the
existing setup keeps working without renaming files.

Notes:
- We standardise on `LANGFUSE_HOST` (the var the Langfuse Python SDK reads
  natively). Older `.env` may still carry `LANGFUSE_BASEURL` — we honour it
  as an alias.
- Validation runs once; missing required keys fail loud rather than silently
  using the wrong environment.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]

_REQUIRED_VARS = (
    "MIRA_BFF_URL",
    "MIRA_SESSION_TOKEN",
    "LANGFUSE_HOST",
    "LANGFUSE_PUBLIC_KEY",
    "LANGFUSE_SECRET_KEY",
)


def current_env() -> str:
    return os.environ.get("MIRA_ENV", "preview")


def _alias_langfuse_baseurl() -> None:
    # Old .env uses LANGFUSE_BASEURL; SDK reads LANGFUSE_HOST. Promote the
    # alias so downstream code (and the SDK auto-detect) sees the canonical name.
    if not os.environ.get("LANGFUSE_HOST") and os.environ.get("LANGFUSE_BASEURL"):
        os.environ["LANGFUSE_HOST"] = os.environ["LANGFUSE_BASEURL"]


def load_env(name: str | None = None) -> str:
    """Load `.env.<name>` (or `.env` as fallback). Returns resolved env name.

    Idempotent: safe to call multiple times. Existing process env vars take
    precedence (matches python-dotenv default behavior, important for CI where
    secrets are injected as env vars).
    """
    name = name or current_env()
    os.environ["MIRA_ENV"] = name  # make resolved name observable downstream

    env_file = ROOT / f".env.{name}"
    legacy = ROOT / ".env"

    # override=True so `.env.<name>` actually wins. Without it, a plain `.env`
    # (or stale process env vars from a prior `--env preview` run in the same
    # shell) would silently keep their values and the "switch" never happens.
    # Investigated 2026-05-13 after `--env mina` ran entirely on preview BFF.
    if env_file.exists():
        load_dotenv(env_file, override=True)
    elif legacy.exists():
        load_dotenv(legacy, override=True)
    else:
        raise FileNotFoundError(
            f"No env file found: tried {env_file} and {legacy}"
        )

    _alias_langfuse_baseurl()

    missing = [v for v in _REQUIRED_VARS if not os.environ.get(v)]
    if missing:
        raise RuntimeError(
            f"env={name}: required vars missing: {', '.join(missing)}"
        )
    return name


def langfuse_client():
    """Return a Langfuse client. Assumes load_env() has run."""
    from langfuse import Langfuse  # local import: keep module light at import-time
    return Langfuse()  # reads LANGFUSE_HOST / PUBLIC_KEY / SECRET_KEY
