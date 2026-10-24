"""Integration tests: the frozen run is cache-only (a miss aborts) and the shipped frozen config matches the code (D26).

Decisions covered: D26.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE))


class FrozenRun(unittest.TestCase):
    """D26: the frozen run is cache-only and its config is verified against the code."""

    def test_cache_miss_aborts_instead_of_calling(self):
        import tempfile
        from llm import CacheMiss, LLMClient
        with tempfile.TemporaryDirectory() as d:
            c = LLMClient(cache_only=True)
            c.cache_dir = Path(d)
            with self.assertRaises(CacheMiss):
                c.generate("intent", "never cached", system="x")

    def test_shipped_frozen_config_matches_code(self):
        import main
        cfg, sha = main.load_frozen(main.FROZEN_DEFAULT)
        self.assertTrue(cfg["llm"]["cache_only"])
        self.assertEqual(len(sha), 64)
