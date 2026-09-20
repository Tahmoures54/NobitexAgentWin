"""Publish a small head-sample of the large signal-event dumps.

The full event tables are tens of megabytes and stay on the runner; only the
first N rows are committed so the numbers can be spot-checked from a checkout.
"""
from __future__ import annotations

import gzip
import os
import sys

SRC = "research_data"
DST = "research_results"
KEEP = int(os.environ.get("SAMPLE_ROWS", "4000"))


def main() -> int:
    os.makedirs(DST, exist_ok=True)
    for name in sorted(os.listdir(SRC)):
        if not (name.startswith("signal_events") and name.endswith(".csv.gz")):
            continue
        src = os.path.join(SRC, name)
        out = os.path.join(DST, name.replace(".csv.gz", ".sample.csv.gz"))
        with gzip.open(src, "rt", encoding="utf-8") as fh:
            head = []
            for _ in range(KEEP):
                try:
                    head.append(next(fh))
                except StopIteration:
                    break
        with gzip.open(out, "wt", encoding="utf-8") as fh:
            fh.writelines(head)
        print(f"sampled {name} -> {out} ({len(head)} rows)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
