"""PTY lifecycle and I/O manager for multiple sessions.

Creates, destroys, and mediates read/write operations for PTY sessions.
Each session runs in its own pseudo-terminal via PlatformPty, with an
optional background thread that forwards raw output to a caller-supplied
callback.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from source.pty.platform_pty import PlatformPty

logger = logging.getLogger(__name__)

# How many bytes to request per read iteration
_READ_CHUNK = 4096

# Seconds to sleep between read attempts when no data is available
_READ_INTERVAL = 0.02


@dataclass
class _PtySession:
    """Internal bookkeeping for a single PTY session."""

    pty: PlatformPty
    read_thread: threading.Thread | None = field(default=None)
    reading: bool = field(default=False)
    # フロントエンドからの初回 resize を検出するためのフラグ。
    # PTY は default cols/rows で起動するため PowerShell の PSReadLine が
    # 古い寸法でプロンプト位置をキャッシュしてしまう。初回 resize 時に
    # Ctrl+L (\x0c) を送って ClearScreen を発火させ、キャッシュを
    # 新しい寸法でリセットする。
    first_resize_pending: bool = field(default=True)


class PtyManager:
    """Manage PTY lifecycle and I/O for multiple sessions.

    Usage
    -----
    >>> mgr = PtyManager()
    >>> mgr.create_session("s1", cols=120, rows=30)
    >>> mgr.start_reading("s1", callback=_on_data)
    >>> mgr.write("s1", "echo hello\\n")
    >>> mgr.destroy_session("s1")
    """

    def __init__(self) -> None:
        self._sessions: dict[str, _PtySession] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    def create_session(
        self,
        session_id: str,
        cols: int = 120,
        rows: int = 30,
        cwd: str | None = None,
    ) -> None:
        """Create and spawn a new PTY session.

        Parameters
        ----------
        session_id : str
            Unique identifier for the session.
        cols : int
            Initial column count.
        rows : int
            Initial row count.
        cwd : str | None
            Working directory for the spawned shell.
            ``None`` falls back to the PlatformPty default.

        Raises
        ------
        ValueError
            If *session_id* already exists.
        """
        with self._lock:
            if session_id in self._sessions:
                raise ValueError(f"Session already exists: {session_id}")

            pty = PlatformPty(cols=cols, rows=rows, cwd=cwd)
            pty.spawn()
            self._sessions[session_id] = _PtySession(pty=pty)
            logger.info("Session created: %s (cols=%d, rows=%d)", session_id, cols, rows)

    def destroy_session(self, session_id: str) -> None:
        """Close and remove a PTY session.

        Stops the reading thread (if running) before closing the PTY.

        Parameters
        ----------
        session_id : str
            Session to destroy.

        Raises
        ------
        KeyError
            If *session_id* does not exist.
        """
        with self._lock:
            session = self._get_session(session_id)

        # Stop reader outside the lock to avoid deadlock with the read thread
        self.stop_reading(session_id)

        with self._lock:
            try:
                session.pty.close()
            except Exception:
                logger.exception("Error closing PTY for session %s", session_id)
            del self._sessions[session_id]
            logger.info("Session destroyed: %s", session_id)

    # ------------------------------------------------------------------
    # I/O
    # ------------------------------------------------------------------

    def write(self, session_id: str, data: str) -> None:
        """Send input data to a PTY session.

        Parameters
        ----------
        session_id : str
            Target session.
        data : str
            String to write (e.g. user keystrokes).

        Raises
        ------
        KeyError
            If *session_id* does not exist.
        """
        with self._lock:
            session = self._get_session(session_id)
        session.pty.write(data)

    def resize(self, session_id: str, cols: int, rows: int) -> None:
        """Resize a PTY session.

        Parameters
        ----------
        session_id : str
            Target session.
        cols : int
            New column count.
        rows : int
            New row count.

        Raises
        ------
        KeyError
            If *session_id* does not exist.
        """
        with self._lock:
            session = self._get_session(session_id)
            first_resize = session.first_resize_pending
            session.first_resize_pending = False
        session.pty.resize(cols, rows)
        logger.debug("Session resized: %s (cols=%d, rows=%d)", session_id, cols, rows)

        # 初回 resize のみ: PSReadLine が古いサイズで持っているプロンプト
        # 位置キャッシュを更新させるため Ctrl+L (ClearScreen) を送信する。
        # 2 回目以降のユーザ由来のウィンドウリサイズでは発火しない。
        if first_resize:
            try:
                session.pty.write("\x0c")
                logger.debug(
                    "Sent Ctrl+L after first resize: session=%s", session_id
                )
            except Exception:
                logger.exception(
                    "Failed to send Ctrl+L after first resize: session=%s",
                    session_id,
                )

    # ------------------------------------------------------------------
    # Background reading
    # ------------------------------------------------------------------

    def start_reading(
        self,
        session_id: str,
        callback: Callable[[str, bytes], None],
    ) -> None:
        """Start a background thread that reads PTY output.

        The *callback* receives ``(session_id, data)`` where *data* is
        raw bytes read from the PTY.  It is called from the reader thread.

        Parameters
        ----------
        session_id : str
            Session to read from.
        callback : Callable[[str, bytes], None]
            Function invoked with ``(session_id, raw_bytes)`` on each
            successful read.

        Raises
        ------
        KeyError
            If *session_id* does not exist.
        RuntimeError
            If reading is already active for this session.
        """
        with self._lock:
            session = self._get_session(session_id)
            if session.reading:
                raise RuntimeError(f"Already reading session: {session_id}")
            session.reading = True
            thread = threading.Thread(
                target=self._read_loop,
                args=(session_id, session, callback),
                name=f"pty-reader-{session_id}",
                daemon=True,
            )
            session.read_thread = thread

        thread.start()
        logger.info("Reading started for session: %s", session_id)

    def stop_reading(self, session_id: str) -> None:
        """Stop the background reading thread for a session.

        If no reader is active the call is a no-op.

        Parameters
        ----------
        session_id : str
            Session whose reader should be stopped.
        """
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None or not session.reading:
                return
            session.reading = False
            thread = session.read_thread

        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
            if thread.is_alive():
                logger.warning(
                    "Reader thread for session %s did not stop within timeout",
                    session_id,
                )

        with self._lock:
            if session_id in self._sessions:
                self._sessions[session_id].read_thread = None

        logger.info("Reading stopped for session: %s", session_id)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_cwd(self, session_id: str) -> str:
        """Return the current working directory of a PTY session.

        Parameters
        ----------
        session_id : str
            Target session.

        Returns
        -------
        str
            Current working directory path.

        Raises
        ------
        KeyError
            If *session_id* does not exist.
        """
        with self._lock:
            session = self._get_session(session_id)
        return session.pty.get_cwd()

    def is_alive(self, session_id: str) -> bool:
        """Check whether a PTY session process is still running.

        Parameters
        ----------
        session_id : str
            Target session.

        Returns
        -------
        bool
            ``True`` if the PTY child process is alive.

        Raises
        ------
        KeyError
            If *session_id* does not exist.
        """
        with self._lock:
            session = self._get_session(session_id)
        return session.pty.is_alive()

    # ------------------------------------------------------------------
    # Bulk operations
    # ------------------------------------------------------------------

    def close_all(self) -> None:
        """Close every PTY session managed by this instance.

        Reading threads are stopped first, then each PTY is closed.
        Errors during individual session teardown are logged but do not
        prevent other sessions from being cleaned up.
        """
        with self._lock:
            ids = list(self._sessions.keys())

        for sid in ids:
            try:
                self.destroy_session(sid)
            except Exception:
                logger.exception("Error destroying session %s during close_all", sid)

        logger.info("All sessions closed")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_session(self, session_id: str) -> _PtySession:
        """Return the ``_PtySession`` for *session_id*.

        Must be called with ``self._lock`` held.

        Raises
        ------
        KeyError
            If *session_id* is not in the sessions dict.
        """
        try:
            return self._sessions[session_id]
        except KeyError:
            raise KeyError(f"Unknown session: {session_id}") from None

    def _read_loop(
        self,
        session_id: str,
        session: _PtySession,
        callback: Callable[[str, bytes], None],
    ) -> None:
        """Background loop that reads from a PTY and forwards data.

        Runs until ``session.reading`` becomes ``False`` or the PTY
        process dies.  Read errors are logged and the loop continues
        (unless the flag/alive check terminates it).
        """
        logger.debug("Reader thread started for session: %s", session_id)

        while session.reading:
            # Terminate if the child process has exited
            try:
                if not session.pty.is_alive():
                    logger.info(
                        "PTY process exited for session %s — reader stopping",
                        session_id,
                    )
                    break
            except Exception:
                logger.exception(
                    "Error checking PTY liveness for session %s", session_id
                )
                break

            # Attempt a read
            try:
                data: bytes = session.pty.read(_READ_CHUNK)
                if data:
                    callback(session_id, data)
                else:
                    # No data available — brief sleep to avoid busy-wait
                    time.sleep(_READ_INTERVAL)
            except EOFError:
                logger.info(
                    "EOF on PTY for session %s — reader stopping", session_id
                )
                break
            except OSError as exc:
                if not session.reading:
                    # PTY was likely closed intentionally; exit quietly
                    break
                logger.warning(
                    "Read error on session %s: %s", session_id, exc
                )
                time.sleep(_READ_INTERVAL)
            except Exception:
                if not session.reading:
                    break
                logger.exception(
                    "Unexpected read error on session %s", session_id
                )
                time.sleep(_READ_INTERVAL)

        session.reading = False
        logger.debug("Reader thread exiting for session: %s", session_id)
