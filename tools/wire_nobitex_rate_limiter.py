#!/usr/bin/env python3
"""Wire client-side NobitexRateLimiter into trading/nobitex_client.py.

Idempotent: safe to run multiple times.
Run from repo root:

    python tools/wire_nobitex_rate_limiter.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "trading" / "nobitex_client.py"


def main() -> int:
    if not TARGET.exists():
        print(f"Missing {TARGET}", file=sys.stderr)
        return 1
    text = TARGET.read_text(encoding="utf-8")
    original = text

    if "from .rate_limiter import NobitexRateLimiter" not in text:
        text = text.replace(
            "from .exceptions import (",
            "from .rate_limiter import NobitexRateLimiter\nfrom .exceptions import (",
            1,
        )

    if "self._rate_limiter = NobitexRateLimiter()" not in text:
        marker = 'logger.info("Nobitex base URL set to: %s", self.BASE_URL)'
        if marker not in text:
            print("Init marker not found", file=sys.stderr)
            return 1
        text = text.replace(
            marker,
            marker
            + "\n\n        # Phase-1: client-side rate limiter\n"
            + "        self._rate_limiter = NobitexRateLimiter()",
            1,
        )

    if "self._rate_limiter.acquire" not in text:
        old = "        max_attempts = 4\n        for attempt in range(max_attempts):\n            timestamp = str(int(time.time()))"
        new = (
            "        max_attempts = 4\n"
            "        for attempt in range(max_attempts):\n"
            "            # Proactive client-side rate limiting (Token Bucket)\n"
            "            if not getattr(self, \"_rate_limiter\", None):\n"
            "                self._rate_limiter = NobitexRateLimiter()\n"
            "            if not self._rate_limiter.acquire(path, method=method, timeout=15.0):\n"
            "                raise RateLimitError(\n"
            "                    \"Client-side rate limiter timeout — request not sent.\",\n"
            "                    retry_after=1.0,\n"
            "                )\n"
            "            timestamp = str(int(time.time()))"
        )
        if old not in text:
            print("_request loop marker not found", file=sys.stderr)
            return 1
        text = text.replace(old, new, 1)

    # Prefer shared ExchangeClientError from exceptions if local class remains
    if "class ExchangeClientError(RuntimeError):" in text and "ExchangeClientError," in text:
        text = re.sub(
            r"\n\nclass ExchangeClientError\(RuntimeError\):.*?\n\n\n",
            "\n\n",
            text,
            count=1,
            flags=re.DOTALL,
        )

    if text == original:
        print("Already wired — no changes.")
        return 0

    TARGET.write_text(text, encoding="utf-8")
    print(f"Updated {TARGET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
