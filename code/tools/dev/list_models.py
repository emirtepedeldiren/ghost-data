"""List the models available to the configured API key. Prints model metadata only, never the key.

Usage:
    .venv/bin/python code/tools/list_models.py [--provider gemini|anthropic] [--all]

Keys come from the environment or the repo-root .env (uncommitted):
GEMINI_API_KEY for gemini, ANTHROPIC_API_KEY for anthropic.

Decisions: D10, D17 (model discovery; development only).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
GEMINI_MODELS_URL = "https://generativelanguage.googleapis.com/v1beta/models"


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


def list_gemini(show_all: bool) -> int:
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        print("GEMINI_API_KEY is not set (environment or .env).", file=sys.stderr)
        return 2
    models, token = [], None
    while True:
        url = GEMINI_MODELS_URL + "?pageSize=1000" + (f"&pageToken={token}" if token else "")
        req = urllib.request.Request(url, headers={"x-goog-api-key": key})  # key in a header, never in the URL
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                page = json.load(resp)
        except urllib.error.HTTPError as e:
            print(f"Gemini models API error: HTTP {e.code}", file=sys.stderr)
            return 1
        except urllib.error.URLError as e:
            print(f"Could not reach the Gemini API: {e.reason}", file=sys.stderr)
            return 1
        models.extend(page.get("models", []))
        token = page.get("nextPageToken")
        if not token:
            break

    rows = []
    for m in models:
        methods = m.get("supportedGenerationMethods", [])
        if not show_all and "generateContent" not in methods:
            continue
        rows.append(m)
    # The list endpoint has no modality field; image input is not exposed by the API.
    print(f"{'id':<44} {'version':<10} {'in_tokens':>9} {'thinking':<8} display_name")
    for m in sorted(rows, key=lambda m: m["name"]):
        mid = m["name"].removeprefix("models/")
        print(f"{mid:<44} {str(m.get('version', '')):<10} {m.get('inputTokenLimit', 0):>9} "
              f"{str(m.get('thinking', '')):<8} {m.get('displayName', '')}")
    print(f"\n{len(rows)} model(s) supporting generateContent "
          f"(image-input support is not a field of the Gemini models API; see the model docs)")
    return 0


def list_anthropic() -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set (environment or .env).", file=sys.stderr)
        return 2
    import anthropic

    try:
        models = list(anthropic.Anthropic().models.list())
    except anthropic.AuthenticationError:
        print("Authentication failed: the API key was rejected.", file=sys.stderr)
        return 1
    except anthropic.APIStatusError as e:
        print(f"Models API error: HTTP {e.status_code}", file=sys.stderr)
        return 1
    except anthropic.APIConnectionError:
        print("Could not reach the Anthropic API.", file=sys.stderr)
        return 1
    for m in sorted(models, key=lambda m: m.created_at, reverse=True):
        caps = m.capabilities if isinstance(getattr(m, "capabilities", None), dict) else {}
        print(f"{m.id:<32} {str(m.created_at):<26} vision={caps.get('image_input', {}).get('supported', '?')}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--provider", choices=("gemini", "anthropic"), default="gemini")
    ap.add_argument("--all", action="store_true", help="also list models without generateContent (embeddings, etc.)")
    args = ap.parse_args()
    load_dotenv(REPO_ROOT / ".env")
    return list_gemini(args.all) if args.provider == "gemini" else list_anthropic()


if __name__ == "__main__":
    sys.exit(main())
