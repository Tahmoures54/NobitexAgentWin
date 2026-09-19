#!/usr/bin/env python3
"""
Apply Phase-1 rate-limiter integration to trading/nobitex_client.py

Run from repo root:
    python tools/apply_phase1_client_patch.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "trading" / "nobitex_client.py"


def main() -> int:
    if not TARGET.exists():
        print(f"ERROR: {TARGET} not found")
        return 1

    src = TARGET.read_text(encoding="utf-8")
    original = src

    # 1. Update imports
    old_imp = '''from .exceptions import (
    AuthenticationError,
    AuthorizationError,
    NetworkExchangeError,
    RateLimitError,
    ServerExchangeError,
)'''

    new_imp = '''from .exceptions import (
    AuthenticationError,
    AuthorizationError,
    ExchangeClientError,
    NetworkExchangeError,
    RateLimitError,
    ServerExchangeError,
)
from .rate_limiter import NobitexRateLimiter'''

    if old_imp not in src:
        if "from .rate_limiter import NobitexRateLimiter" in src:
            print("Already patched (imports). Skipping.")
            return 0
        print("ERROR: expected import block not found. File may have changed.")
        return 1
    src = src.replace(old_imp, new_imp)

    # 2. Remove local ExchangeClientError class (now lives in exceptions.py)
    pattern = r"\n\nclass ExchangeClientError\(RuntimeError\):.*?(?=\n\ndef |\n\nclass |\n\n# )"
    src, n = re.subn(pattern, "\n\n", src, count=1, flags=re.DOTALL)
    if n != 1:
        print(f"WARNING: local ExchangeClientError removal count = {n}")

    # 3. Instantiate rate limiter in __init__
    marker = '        logger.info("Nobitex base URL set to: %s", self.BASE_URL)\n'
    if "self._rate_limiter = NobitexRateLimiter()" not in src:
        if marker not in src:
            print("ERROR: __init__ marker not found")
            return 1
        src = src.replace(
            marker,
            marker + "\n        # Phase-1: client-side rate limiter\n"
            "        self._rate_limiter = NobitexRateLimiter()\n",
        )

    # 4. Acquire token at the start of each attempt inside _request
    old_loop = '''        max_attempts = 4
        for attempt in range(max_attempts):
            timestamp = str(int(time.time()))'''

    new_loop = '''        max_attempts = 4
        for attempt in range(max_attempts):
            # Proactive client-side rate limiting (Token Bucket)
            if not self._rate_limiter.acquire(path, method=method, timeout=15.0):
                raise RateLimitError(
                    "Client-side rate limiter timeout — request not sent.",
                    retry_after=1.0,
                )
            timestamp = str(int(time.time()))'''

    if old_loop not in src:
        if "self._rate_limiter.acquire" in src:
            print("Already patched (acquire). Done.")
        else:
            print("ERROR: _request loop marker not found")
            return 1
    else:
        src = src.replace(old_loop, new_loop)

    if src == original:
        print("No changes needed.")
        return 0

    backup = TARGET.with_suffix(".py.bak")
    backup.write_text(original, encoding="utf-8")
    TARGET.write_text(src, encoding="utf-8")
    print(f"Patched {TARGET}")
    print(f"Backup written to {backup}")
    print("Done. Review the diff and commit when ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
