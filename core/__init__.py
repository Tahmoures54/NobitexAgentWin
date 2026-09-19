# ===== core/__init__.py =====

"""
Core module for configuration, encryption, user management,
structured logging, database and graceful shutdown.
"""

from . import config
from . import encryption
from . import user_status

__all__ = [
    "config",
    "encryption",
    "user_status",
    "logger",
    "database",
    "shutdown",
]

# Lazy-friendly re-exports for common helpers
try:
    from .logger import get_logger, setup_structured_logging, set_log_context, clear_log_context
    from .database import Database
    from .shutdown import get_shutdown_manager, ShutdownManager
except ImportError:
    pass
