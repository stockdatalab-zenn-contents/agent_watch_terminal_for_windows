"""Cross-platform PTY abstraction.

Windows: pywinpty (PtyProcess) + PowerShell
Linux  : standard pty module + bash

Usage:
    pty = PlatformPty(cols=120, rows=30)
    pty.spawn()
    data = pty.read()
    pty.write("ls\n")
    pty.close()
"""

from __future__ import annotations

import os
import platform
import shutil
from typing import Optional

# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------

IS_WINDOWS = platform.system() == "Windows"

# ---------------------------------------------------------------------------
# Platform-specific imports (graceful fallback)
# ---------------------------------------------------------------------------

if IS_WINDOWS:
    try:
        from winpty import PtyProcess as _WinPtyProcess  # type: ignore[import-untyped]
    except ImportError:
        _WinPtyProcess = None  # type: ignore[assignment,misc]
else:
    try:
        import fcntl
        import pty as _pty_mod
        import select
        import signal
        import struct
        import subprocess
        import termios
    except ImportError:
        pass  # handled at spawn time


# ---------------------------------------------------------------------------
# Custom exception
# ---------------------------------------------------------------------------


class PtyError(Exception):
    """PTY operation failure."""


# ---------------------------------------------------------------------------
# PlatformPty
# ---------------------------------------------------------------------------


class PlatformPty:
    """Unified PTY interface for Windows and Linux.

    Parameters
    ----------
    cols : int
        Initial column count (default 120).
    rows : int
        Initial row count (default 30).
    cwd : str | None
        Working directory for the spawned shell.
        ``None`` uses the current process working directory.
    """

    def __init__(
        self,
        cols: int = 120,
        rows: int = 30,
        cwd: Optional[str] = None,
    ) -> None:
        self._cols = cols
        self._rows = rows
        self._cwd = cwd or os.getcwd()
        self._pid: Optional[int] = None

        # Windows handle
        self._win_proc: Optional[object] = None

        # Linux handles
        self._linux_proc: Optional[object] = None
        self._master_fd: Optional[int] = None

    # -- properties ---------------------------------------------------------

    @property
    def pid(self) -> int:
        """Process ID of the shell."""
        if self._pid is None:
            raise PtyError("PTY not spawned yet")
        return self._pid

    @property
    def cols(self) -> int:
        return self._cols

    @property
    def rows(self) -> int:
        return self._rows

    # -- public API ---------------------------------------------------------

    def spawn(self) -> None:
        """Start the shell process."""
        if IS_WINDOWS:
            self._spawn_windows()
        else:
            self._spawn_linux()

    def read(self, size: int = 4096) -> bytes:
        """Read output from the PTY.

        Returns up to *size* bytes.  On Windows the call blocks briefly;
        on Linux it uses ``select`` to avoid indefinite blocking.
        """
        if IS_WINDOWS:
            return self._read_windows(size)
        return self._read_linux(size)

    def write(self, data: str) -> None:
        """Send *data* (text) to the PTY."""
        if IS_WINDOWS:
            self._write_windows(data)
        else:
            self._write_linux(data)

    def resize(self, cols: int, rows: int) -> None:
        """Resize the PTY to *cols* x *rows*."""
        self._cols = cols
        self._rows = rows
        if IS_WINDOWS:
            self._resize_windows(cols, rows)
        else:
            self._resize_linux(cols, rows)

    def close(self) -> None:
        """Terminate the shell process and release resources."""
        if IS_WINDOWS:
            self._close_windows()
        else:
            self._close_linux()

    def is_alive(self) -> bool:
        """Return ``True`` if the shell process is still running."""
        if IS_WINDOWS:
            return self._is_alive_windows()
        return self._is_alive_linux()

    def get_cwd(self) -> str:
        """Return the working directory supplied at construction time.

        Note: the *actual* cwd of the child process may change after
        the user executes ``cd``.  Tracking that requires OS-specific
        queries (e.g. ``/proc/<pid>/cwd`` on Linux).
        """
        if not IS_WINDOWS and self._pid is not None:
            proc_cwd = f"/proc/{self._pid}/cwd"
            try:
                return os.readlink(proc_cwd)
            except OSError:
                pass
        return self._cwd

    # ======================================================================
    # Windows implementation
    # ======================================================================

    def _resolve_powershell(self) -> str:
        """Find ``pwsh.exe`` (PowerShell 7+) or fall back to ``powershell.exe``."""
        pwsh = shutil.which("pwsh")
        if pwsh:
            return pwsh
        ps = shutil.which("powershell")
        if ps:
            return ps
        raise PtyError("Neither pwsh.exe nor powershell.exe found on PATH")

    def _spawn_windows(self) -> None:
        if _WinPtyProcess is None:
            raise PtyError(
                "pywinpty is not installed. "
                "Install it with: pip install pywinpty"
            )

        shell = self._resolve_powershell()

        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"

        # WSL bash で OSC 7 を発火させるための仕込み。
        # PowerShell の prompt 関数フックは PS 自身のプロンプト時しか動かず、
        # `wsl` コマンドで起動した子プロセスの bash 内では発火しない。
        # WSLENV により PROMPT_COMMAND を Win→WSL に伝搬させ、
        # WSL bash の毎プロンプト時に OSC 7 を出力させる。
        #
        # 値は Python raw string で保持し、各層を素通しする想定:
        #   Python env dict → Win32 process env → PowerShell 子プロセス
        #   → wsl.exe → Linux env → bash が dquote 内文字列として
        #   PROMPT_COMMAND を eval → printf がエスケープを解釈
        #
        # 注意: ~/.bashrc が PROMPT_COMMAND を上書きする設定の場合は無効化される。
        # 注意: bash 以外（zsh/fish）では未対応（README.md の制約事項参照）。
        env["PROMPT_COMMAND"] = (
            r'printf "\033]7;file://localhost%s\a" "$PWD"'
        )
        existing_wslenv = env.get("WSLENV", "")
        wslenv_entry = "PROMPT_COMMAND/u"
        if existing_wslenv:
            env["WSLENV"] = f"{existing_wslenv}:{wslenv_entry}"
        else:
            env["WSLENV"] = wslenv_entry

        try:
            proc = _WinPtyProcess.spawn(
                shell,
                cwd=self._cwd,
                env=env,
                dimensions=(self._rows, self._cols),
            )
        except Exception as exc:
            raise PtyError(f"Failed to spawn Windows PTY: {exc}") from exc

        self._win_proc = proc
        self._pid = proc.pid

        # Enable UTF-8 code page inside the new console.
        # Use "\r" only -- PSReadLine treats "\r" as Enter (execute) and
        # "\n" as Shift+Enter (insert newline), so "\r\n" leaves a stray
        # newline in the next prompt and triggers the ">>" continuation
        # prompt.
        try:
            proc.write("chcp 65001\r")
        except Exception:
            pass  # best-effort

        # OSC 7 で現在の作業ディレクトリをターミナルへ通知する。
        # 既存の prompt 関数をラップし、プロンプト表示のたびに
        # cwd を ESC ]7;file:///path BEL として出力する。
        # OSC 7 はエスケープシーケンスなのでユーザーには見えない。
        try:
            osc7_inject = (
                "$__awOrigPrompt=${function:prompt};"
                "function prompt{"
                "$__awP=$PWD.ProviderPath;"
                "[Console]::Write("
                "\"$([char]27)]7;file:///$($__awP-replace'\\\\','/')$([char]7)\""
                ");"
                "& $__awOrigPrompt"
                "}"
            )
            proc.write(f"{osc7_inject}\r")
        except Exception:
            pass  # best-effort

    def _read_windows(self, size: int) -> bytes:
        proc = self._win_proc
        if proc is None:
            raise PtyError("PTY not spawned")
        try:
            data: str = proc.read(size)
            return data.encode("utf-8", errors="replace")
        except EOFError:
            return b""
        except Exception as exc:
            raise PtyError(f"Read error: {exc}") from exc

    def _write_windows(self, data: str) -> None:
        proc = self._win_proc
        if proc is None:
            raise PtyError("PTY not spawned")
        try:
            # \r\n → \r に正規化。PSReadLine は \r=Enter, \n=Shift+Enter と
            # 解釈するため、\r\n のままだと改行が二重になる。
            proc.write(data.replace("\r\n", "\r"))
        except Exception as exc:
            raise PtyError(f"Write error: {exc}") from exc

    def _resize_windows(self, cols: int, rows: int) -> None:
        proc = self._win_proc
        if proc is None:
            raise PtyError("PTY not spawned")
        try:
            proc.setwinsize(rows, cols)
        except Exception as exc:
            raise PtyError(f"Resize error: {exc}") from exc

    def _close_windows(self) -> None:
        proc = self._win_proc
        if proc is None:
            return
        try:
            if proc.isalive():
                proc.write("exit\r")
                try:
                    proc.wait(timeout=3)
                except Exception:
                    pass
            # Force-kill if still alive
            if proc.isalive():
                proc.terminate()
        except Exception:
            pass
        finally:
            self._win_proc = None
            self._pid = None

    def _is_alive_windows(self) -> bool:
        proc = self._win_proc
        if proc is None:
            return False
        try:
            return bool(proc.isalive())
        except Exception:
            return False

    # ======================================================================
    # Linux implementation
    # ======================================================================

    def _spawn_linux(self) -> None:
        shell = "/bin/bash"
        if not os.path.isfile(shell):
            raise PtyError(f"Shell not found: {shell}")

        try:
            master_fd, slave_fd = _pty_mod.openpty()
        except Exception as exc:
            raise PtyError(f"openpty failed: {exc}") from exc

        # Set initial terminal size
        self._set_linux_winsize(master_fd, self._rows, self._cols)

        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["TERM"] = env.get("TERM", "xterm-256color")

        try:
            proc = subprocess.Popen(
                [shell, "--login"],
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                cwd=self._cwd,
                env=env,
                preexec_fn=os.setsid,
                close_fds=True,
            )
        except Exception as exc:
            os.close(master_fd)
            os.close(slave_fd)
            raise PtyError(f"Failed to spawn Linux PTY: {exc}") from exc

        # Parent no longer needs the slave side
        os.close(slave_fd)

        # Make master_fd non-blocking
        flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
        fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

        self._linux_proc = proc
        self._master_fd = master_fd
        self._pid = proc.pid

        # OSC 7 で cwd をターミナルへ通知する (bash 用)。
        # PROMPT_COMMAND に追加し、既存の設定を壊さない。
        try:
            osc7_cmd = (
                r'__awterm_osc7(){ printf "\033]7;file://localhost%s\a" "$PWD"; };'
                r'PROMPT_COMMAND="__awterm_osc7;${PROMPT_COMMAND}"'
            )
            os.write(master_fd, f"{osc7_cmd}\n".encode())
        except Exception:
            pass  # best-effort

    @staticmethod
    def _set_linux_winsize(fd: int, rows: int, cols: int) -> None:
        """Apply terminal size via ``TIOCSWINSZ``."""
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)

    def _read_linux(self, size: int) -> bytes:
        fd = self._master_fd
        if fd is None:
            raise PtyError("PTY not spawned")
        try:
            ready, _, _ = select.select([fd], [], [], 0.05)
            if ready:
                return os.read(fd, size)
            return b""
        except OSError as exc:
            if exc.errno == 5:  # EIO — child exited
                return b""
            raise PtyError(f"Read error: {exc}") from exc

    def _write_linux(self, data: str) -> None:
        fd = self._master_fd
        if fd is None:
            raise PtyError("PTY not spawned")
        try:
            os.write(fd, data.encode("utf-8"))
        except Exception as exc:
            raise PtyError(f"Write error: {exc}") from exc

    def _resize_linux(self, cols: int, rows: int) -> None:
        fd = self._master_fd
        if fd is None:
            raise PtyError("PTY not spawned")
        try:
            self._set_linux_winsize(fd, rows, cols)
            # Notify the child process group
            if self._pid is not None:
                os.killpg(os.getpgid(self._pid), signal.SIGWINCH)
        except Exception as exc:
            raise PtyError(f"Resize error: {exc}") from exc

    def _close_linux(self) -> None:
        proc = self._linux_proc
        fd = self._master_fd

        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                    proc.wait(timeout=1)
                except Exception:
                    pass

        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

        self._linux_proc = None
        self._master_fd = None
        self._pid = None

    def _is_alive_linux(self) -> bool:
        proc = self._linux_proc
        if proc is None:
            return False
        return proc.poll() is None
