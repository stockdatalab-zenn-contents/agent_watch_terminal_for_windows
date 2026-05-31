"""Windows taskbar icon flashing for Agent Watch Terminal.

Flashes the taskbar icon (and window caption) to attract attention
when an agent terminal requires user interaction.

On non-Windows platforms the module degrades gracefully:
``is_available()`` returns ``False`` and ``flash()`` is a no-op.

Usage:
    flasher = TaskbarFlasher()
    if flasher.is_available():
        flasher.flash()
"""

from __future__ import annotations

import logging
import platform

# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------

IS_WINDOWS = platform.system() == "Windows"

# ---------------------------------------------------------------------------
# Platform-specific imports (graceful fallback)
# ---------------------------------------------------------------------------

_ctypes_available = False

if IS_WINDOWS:
    try:
        import ctypes
        import ctypes.wintypes

        _ctypes_available = True
    except ImportError:
        ctypes = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# FLASHWINFO structure (defined at module level to avoid repeated creation)
# ---------------------------------------------------------------------------

if _ctypes_available:

    class FLASHWINFO(ctypes.Structure):  # type: ignore[name-defined]
        """Win32 ``FLASHWINFO`` structure for ``FlashWindowEx``."""

        _fields_ = [
            ("cbSize", ctypes.wintypes.UINT),
            ("hwnd", ctypes.wintypes.HWND),
            ("dwFlags", ctypes.wintypes.DWORD),
            ("uCount", ctypes.wintypes.UINT),
            ("dwTimeout", ctypes.wintypes.DWORD),
        ]


# Flash flags
_FLASHW_ALL = 0x00000003  # flash both caption bar and taskbar button
_FLASHW_TIMERNOFG = 0x0000000C  # flash until the window comes to foreground


# ---------------------------------------------------------------------------
# TaskbarFlasher
# ---------------------------------------------------------------------------


class TaskbarFlasher:
    """Flash the Windows taskbar icon to notify the user.

    Parameters
    ----------
    window_title : str
        Title of the target window.  Used by ``FindWindowW`` to locate
        the window handle.  Defaults to ``"Agent Watch Terminal"``.
    """

    def __init__(self, window_title: str = "Agent Watch Terminal") -> None:
        self.window_title = window_title

    # -- public API ---------------------------------------------------------

    def is_available(self) -> bool:
        """Return whether taskbar flashing is supported on this platform.

        Returns
        -------
        bool
            ``True`` on Windows when ``ctypes`` is importable, otherwise
            ``False``.
        """
        return IS_WINDOWS and _ctypes_available

    def flash(self) -> bool:
        """Flash the taskbar icon for the target window.

        The icon keeps flashing until the user brings the window to the
        foreground.

        Returns
        -------
        bool
            ``True`` if the flash was triggered successfully, ``False``
            if the platform is unsupported or the window was not found.
        """
        if not self.is_available():
            logger.debug("Taskbar flashing not available on this platform")
            return False

        hwnd = self._find_window()
        if not hwnd:
            logger.warning(
                "Window not found: '%s'",
                self.window_title,
            )
            return False

        try:
            finfo = FLASHWINFO()
            finfo.cbSize = ctypes.sizeof(FLASHWINFO)  # type: ignore[arg-type]
            finfo.hwnd = hwnd
            finfo.dwFlags = _FLASHW_ALL | _FLASHW_TIMERNOFG
            finfo.uCount = 0  # flash until foreground
            finfo.dwTimeout = 0  # default cursor blink rate

            ctypes.windll.user32.FlashWindowEx(ctypes.byref(finfo))  # type: ignore[union-attr]
            logger.debug("Taskbar flash triggered for '%s'", self.window_title)
            return True
        except Exception:
            logger.exception("Failed to flash taskbar icon")
            return False

    # -- internal -----------------------------------------------------------

    def _find_window(self) -> int:
        """Find the window handle by title.

        Returns
        -------
        int
            Window handle (``HWND``), or ``0`` if not found.
        """
        try:
            user32 = ctypes.windll.user32  # type: ignore[union-attr]
            hwnd: int = user32.FindWindowW(None, self.window_title)
            return hwnd
        except Exception:
            logger.exception("Error searching for window handle")
            return 0
