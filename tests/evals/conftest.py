import sys
from pathlib import Path

# Make the project root importable so `from chatbot import ChatbotSession` works
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Import the env loader *before* pytest discovers test files. _env.py sets
# DEEPEVAL_DISABLE_DOTENV=1 at module-import time, which stops deepeval's
# package init from silently loading the project-root `.env` and shadowing
# whatever the user's `--env <name>` selection actually configured.
from tests.evals import _env  # noqa: F401


def pytest_configure(config):
    """Print the active metric/file allowlist so the run config is visible.

    Helps avoid "why did metric X not run" debugging — the first thing in the
    pytest log shows exactly what's enabled. Reads from _metrics_config.py
    (which honours MIRA_METRICS_ALL / MIRA_METRICS_ALLOWLIST / MIRA_FILES_ALLOWLIST).
    """
    from tests.evals._metrics_config import _all_mode, _resolve_allowlist, _resolve_files

    if _all_mode():
        print("\n[metrics-config] MIRA_METRICS_ALL=1 → running ALL metrics in ALL files")
        return

    active_m = _resolve_allowlist()
    active_f = _resolve_files()
    # Both are non-None here (we already returned in the _all_mode branch).
    print(
        f"\n[metrics-config] {len(active_m)} active metric(s): "
        f"{', '.join(sorted(active_m))}"
    )
    print(
        f"[metrics-config] {len(active_f)} active file(s): "
        f"{', '.join(sorted(active_f))}"
    )
    print(
        "[metrics-config] override via MIRA_METRICS_ALL=1 / "
        "MIRA_METRICS_ALLOWLIST / MIRA_FILES_ALLOWLIST"
    )
