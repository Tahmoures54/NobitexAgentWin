#!/usr/bin/env python3
"""Trim the published result JSON so the committed copy stays small."""
from __future__ import annotations

import json
import os
import sys

TARGET = "research_results"


def trim(obj):
    if isinstance(obj, dict):
        obj.pop("trades_detail", None)
        if "sample" in obj and isinstance(obj["sample"], list):
            obj["sample"] = obj["sample"][:10]
        for value in obj.values():
            trim(value)
    elif isinstance(obj, list):
        for item in obj:
            trim(item)


def main() -> int:
    for name in sorted(os.listdir(TARGET)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(TARGET, name)
        try:
            with open(path) as fh:
                blob = json.load(fh)
        except Exception as exc:  # noqa: BLE001
            print(f"skip {name}: {exc}")
            continue
        trim(blob)
        with open(path, "w") as fh:
            json.dump(blob, fh, indent=1, default=str)
        print(f"trimmed {name}: {os.path.getsize(path)} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
