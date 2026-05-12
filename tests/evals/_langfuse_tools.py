"""Per-session Mira tool registry, sourced from Langfuse traces and cached on disk.

Each Mira agent run records its full tool-definition payload as part of the
GENERATION span's `input` field on Langfuse: messages first, then one
stringified JSON object per tool (`{"type":"function","name":...,
"description":...,"inputSchema":...}`). This module pulls those out so the
eval harness knows the *true* available-tools set for that environment,
including descriptions which downstream metrics use as judging context.

Lifecycle:
  - report.py reads the cache at startup; if missing/stale, ToolUseMetric
    starts with empty available_tools (skipped on the first golden).
  - After the first golden completes, report.py calls refresh_cache_from_session
    with that golden's conv_id; we pull tools from Langfuse and union into cache.
  - Subsequent goldens in the same run use the freshly-populated cache.

Soft expiry: cache older than 7 days triggers a refresh next run, but the
stale cache is still used until refresh succeeds. We never wipe on read.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from _env import ROOT, current_env, langfuse_client

CACHE_DIR = ROOT / ".cache"
CACHE_TTL = timedelta(days=7)


def _cache_path(env: str) -> Path:
    return CACHE_DIR / f"tools-{env}.json"


def load_cached(env: str | None = None) -> dict | None:
    p = _cache_path(env or current_env())
    if not p.exists():
        return None
    with p.open(encoding="utf-8") as f:
        return json.load(f)


def is_stale(cached: dict) -> bool:
    raw = cached.get("fetched_at")
    if not raw:
        return True
    try:
        # accept both `2026-05-12T11:30:00Z` and `...+00:00`
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return True
    return (datetime.now(timezone.utc) - ts) > CACHE_TTL


def _extract_tools_from_obs_input(obs_input: Any) -> list[dict]:
    """Pull tool definitions out of one GENERATION observation's input list.

    Mira's input is a flat list mixing messages (dicts with `role`) and tool
    definitions (stringified JSON or dicts with `type=function`). We only keep
    the tool entries.
    """
    out: list[dict] = []
    if not isinstance(obs_input, list):
        return out
    for item in obs_input:
        obj = None
        if isinstance(item, str):
            try:
                obj = json.loads(item)
            except Exception:
                continue
        elif isinstance(item, dict):
            obj = item
        if isinstance(obj, dict) and obj.get("type") == "function" and obj.get("name"):
            out.append({
                "name": obj["name"],
                "description": obj.get("description"),
                "input_schema": obj.get("inputSchema"),
            })
    return out


def fetch_session_tools(session_id: str, *, poll_s: int = 15) -> list[dict]:
    """Poll Langfuse for traces under `session_id`; return the union of tool
    entries across all GENERATION observations. Returns [] if nothing landed
    within `poll_s` seconds (Langfuse ingestion is async; few-second delay
    is normal).

    Dedups by name. When the same tool appears with and without a description,
    we keep the version that has one.
    """
    lf = langfuse_client()
    deadline = time.time() + poll_s
    while True:
        traces = lf.api.trace.list(session_id=session_id, limit=20).data
        if traces:
            seen: dict[str, dict] = {}
            for tr in traces:
                gens = lf.api.legacy.observations_v1.get_many(
                    trace_id=tr.id, type="GENERATION", limit=20,
                ).data
                for g_short in gens:
                    g = lf.api.legacy.observations_v1.get(g_short.id)
                    for entry in _extract_tools_from_obs_input(g.input):
                        existing = seen.get(entry["name"])
                        if not existing or (not existing.get("description") and entry.get("description")):
                            seen[entry["name"]] = entry
            if seen:
                return sorted(seen.values(), key=lambda x: x["name"])
        if time.time() >= deadline:
            return []
        time.sleep(2)


def refresh_cache_from_session(
    session_id: str,
    env: str | None = None,
    *,
    union_with_existing: bool = True,
) -> dict | None:
    """Pull tools from Langfuse for `session_id`, union into cache, write to
    disk. Returns the new cache dict, or None if Langfuse returned nothing
    (cache untouched).
    """
    env = env or current_env()
    fresh = fetch_session_tools(session_id)
    if not fresh:
        return None

    merged: dict[str, dict] = {}
    source_ids: set[str] = set()
    if union_with_existing:
        existing = load_cached(env)
        if existing:
            for t in existing.get("tools", []):
                merged[t["name"]] = t
            source_ids.update(existing.get("source_session_ids", []) or [])
    for t in fresh:
        # Fresh wins on description / schema updates.
        merged[t["name"]] = t
    source_ids.add(session_id)

    payload = {
        "env": env,
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_session_ids": sorted(source_ids),
        "tools": sorted(merged.values(), key=lambda x: x["name"]),
    }
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with _cache_path(env).open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return payload


def available_tool_registry(env: str | None = None) -> dict[str, dict]:
    """name → {name, description, input_schema}, for cheap by-name lookup
    from the driver when enriching tools_called with descriptions."""
    cached = load_cached(env)
    return {t["name"]: t for t in (cached or {}).get("tools", [])}


def available_tool_names(env: str | None = None) -> list[str]:
    cached = load_cached(env)
    return [t["name"] for t in (cached or {}).get("tools", [])]
