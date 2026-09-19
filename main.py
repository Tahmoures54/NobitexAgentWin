# source/main.py
from __future__ import annotations
import argparse
import logging
import os
import sys
import traceback

def _setup_path() -> None:
    this_dir   = os.path.dirname(os.path.abspath(__file__))
    source_dir = this_dir
    candidate  = os.path.join(this_dir, "source")
    if os.path.isdir(candidate): source_dir = candidate
    if source_dir not in sys.path: sys.path.insert(0, source_dir)

_setup_path()

def setup_logging(debug: bool = False, log_file: str | None = None) -> logging.Logger:
    """Prefer structured logger; fall back to classic if unavailable."""
    try:
        from core.logger import setup_structured_logging
        return setup_structured_logging(debug=debug, log_file=log_file, json_output=False)
    except Exception:
        level = logging.DEBUG if debug else logging.INFO
        fmt = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
        date_fmt = "%H:%M:%S"
        handlers = [logging.StreamHandler(sys.stdout)]

        if log_file:
            try:
                fh = logging.FileHandler(log_file, encoding="utf-8")
                fh.setFormatter(logging.Formatter(fmt, date_fmt))
                handlers.append(fh)
            except OSError as exc:
                print(f"[WARNING] Cannot open log file '{log_file}': {exc}", file=sys.stderr)

        logging.basicConfig(level=level, format=fmt, datefmt=date_fmt, handlers=handlers, force=True)
        for noisy in ("urllib3", "requests", "PIL", "matplotlib"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
        return logging.getLogger("CryptoScanner")

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="CryptoScanner", description="Advanced Crypto Scanner")
    p.add_argument("--version", "-v", action="store_true")
    p.add_argument("--debug", "-d", action="store_true")
    p.add_argument("--log", metavar="FILE", default=None)
    return p

def show_error_and_exit(title: str, message: str, code: int = 1) -> None:
    logging.critical("%s: %s", title, message)
    print(f"\n{'='*60}\nERROR: {title}\n{'='*60}\n{message}\n{'='*60}\n", file=sys.stderr)
    sys.exit(code)

def check_dependencies(logger: logging.Logger) -> None:
    missing = []
    required = {
        "numpy": "numpy",
        "pandas": "pandas",
        "requests": "requests",
        "cryptography": "cryptography",
        "PIL": "Pillow",
        "matplotlib": "matplotlib",
    }
    for module, package in required.items():
        try:
            __import__(module)
        except ImportError:
            missing.append(package)
    if missing:
        show_error_and_exit("Missing Dependencies", "Required packages missing:\n" + "\n".join(f"  pip install {p}" for p in missing))

def _close_splash(splash, logger: logging.Logger) -> None:
    if splash is None: return
    try:
        if not splash.winfo_exists(): return
    except Exception: return
    if hasattr(splash, "close"):
        try: splash.close(); return
        except Exception: pass
    try: splash.destroy()
    except: pass

def launch_gui(logger: logging.Logger) -> None:
    try:
        import tkinter as tk
    except ImportError:
        show_error_and_exit("tkinter Not Found", "tkinter is required.")
        return

    logger.info("Initializing UI and Splash Screen…")
    root = tk.Tk()
    root.withdraw()

    splash = None
    try:
        from gui.splash_screen import SplashScreen
        splash = SplashScreen(root)
        root.update()
    except Exception as e:
        logger.warning("Splash screen failed (ignored): %s", e)

    logger.info("Loading heavy application modules...")
    try:
        from gui.gui_main import CryptoScannerApp
    except ImportError as exc:
        logger.debug("Import traceback:\n%s", traceback.format_exc())
        show_error_and_exit("Module Load Error", str(exc))
        return

    logger.info("Starting CryptoScanner GUI…")
    try:
        app = CryptoScannerApp(root)

        def _try_show_main(attempt: int = 0) -> None:
            splash_done = True
            if splash is not None:
                try:
                    if splash.winfo_exists():
                        if hasattr(splash, "animation_finished_var"):
                            splash_done = bool(splash.animation_finished_var.get())
                        elif hasattr(splash, "is_finished"):
                            splash_done = bool(splash.is_finished())
                        else:
                            splash_done = attempt >= 30
                    else:
                        splash_done = True
                except Exception:
                    splash_done = True

            if splash_done or attempt >= 50:
                _close_splash(splash, logger)
                root.deiconify()
                logger.info("Application started successfully.")
            else:
                root.after(100, lambda: _try_show_main(attempt + 1))

        root.after(200, _try_show_main)
        root.mainloop()
        logger.info("Application closed normally.")

    except Exception as exc:
        logger.critical("Fatal runtime error:\n%s", traceback.format_exc())
        show_error_and_exit("Application Error", f"{type(exc).__name__}: {exc}")

def main() -> None:
    def handle_exception(exc_type, exc_value, exc_traceback):
        logging.critical("Unhandled exception:", exc_info=(exc_type, exc_value, exc_traceback))
    sys.excepthook = handle_exception

    parser = build_parser()
    args = parser.parse_args()
    logger = setup_logging(debug=args.debug, log_file=args.log)

    if args.version:
        try:
            from core.config import APP_VERSION
            print(f"CryptoScanner v{APP_VERSION}")
        except ImportError:
            print("CryptoScanner (version unknown)")
        sys.exit(0)

    logger.info("=" * 50)
    logger.info("Advanced Crypto Scanner — Starting")
    logger.info("Python %s | %s", sys.version.split()[0], sys.platform)
    logger.info("Working dir: %s", os.getcwd())
    logger.info("=" * 50)

    # Install graceful shutdown + watchdog
    try:
        from core.shutdown import get_shutdown_manager
        from core.database import Database

        shutdown_mgr = get_shutdown_manager()
        shutdown_mgr.install_handlers()

        db = Database()
        shutdown_mgr.register("database-checkpoint", db.close, priority=10, timeout=3.0)

        try:
            from trading.watchdog import get_watchdog
            wd = get_watchdog()
            wd.register("main", max_silence_seconds=120.0)
            wd.heartbeat("main")
            wd.start()
            shutdown_mgr.register("watchdog-stop", wd.stop, priority=5, timeout=2.0)
            logger.info("Health watchdog started")
        except Exception as wd_exc:
            logger.warning("Watchdog not started: %s", wd_exc)

        logger.debug("Shutdown hooks registered")
    except Exception as exc:
        logger.warning("Could not install full shutdown manager: %s", exc)

    check_dependencies(logger)
    launch_gui(logger)

if __name__ == "__main__":
    main()
