"""Optional release cleanup helper for CryptoScanner Nobitex-only builds."""
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parent
REMOVE_DIRS = {"__pycache__", ".pytest_cache"}
REMOVE_FILES = {".coverage"}

def cleanup():
    for p in ROOT.rglob("*"):
        if p.is_dir() and p.name in REMOVE_DIRS:
            shutil.rmtree(p, ignore_errors=True)
        elif p.is_file() and p.name in REMOVE_FILES:
            try: p.unlink()
            except OSError: pass

if __name__ == "__main__":
    cleanup()
    print("Cleanup complete.")
