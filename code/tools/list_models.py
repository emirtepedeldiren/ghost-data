"""List the models available to the configured API key (N2). Prints model metadata only, never the key.

Usage: .venv/bin/python code/tools/list_models.py
Reads ANTHROPIC_API_KEY from the environment or from the repo-root .env (uncommitted).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def load_dotenv(path: Path) -> None:
    """Minimal KEY=VALUE loader; existing environment variables win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def main() -> int:
    load_dotenv(REPO_ROOT / ".env")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set (environment or .env). Nothing to list.", file=sys.stderr)
        return 2
    import anthropic

    client = anthropic.Anthropic()
    try:
        models = list(client.models.list())
    except anthropic.AuthenticationError:
        print("Authentication failed: the API key was rejected.", file=sys.stderr)
        return 1
    except anthropic.APIStatusError as e:
        print(f"Models API error: HTTP {e.status_code}", file=sys.stderr)
        return 1
    except anthropic.APIConnectionError:
        print("Could not reach the Anthropic API.", file=sys.stderr)
        return 1

    models.sort(key=lambda m: m.created_at, reverse=True)
    print(f"{'id':<32} {'created_at':<26} {'vision':<6} display_name")
    for m in models:
        caps = getattr(m, "capabilities", None) or {}
        vision = caps.get("image_input", {}).get("supported", "?") if isinstance(caps, dict) else "?"
        print(f"{m.id:<32} {str(m.created_at):<26} {str(vision):<6} {m.display_name}")
    for family in ("haiku", "sonnet"):
        newest = next((m for m in models if family in m.id), None)
        print(f"newest {family}: {newest.id if newest else 'none available'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
