# console_setup.py
"""Keep Rich/console output from crashing on pages containing non-GBK text."""
import sys


def configure_utf8_console() -> None:
    """Reconfigure redirected stdio to UTF-8 with replacement instead of raising."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            # Detached or non-reconfigurable streams are left untouched.
            pass
