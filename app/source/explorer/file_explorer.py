"""File explorer backend for the sidebar file browser.

Provides directory listing, text file reading, and OS-level file
opening.  All public methods catch exceptions and return
safe defaults so that the UI layer never receives an unhandled error.
"""

from __future__ import annotations

import base64
import logging
import os
import platform
import subprocess
from datetime import datetime
from pathlib import Path


logger = logging.getLogger(__name__)

# Maximum file size (bytes) that read_file will load into memory.
_MAX_READ_BYTES = 1_048_576  # 1 MB

# 画像ファイルの最大サイズ (5 MB)
_MAX_IMAGE_BYTES = 5_242_880

# 拡張子 → MIME タイプ マッピング
_IMAGE_MIME_TYPES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".ico": "image/x-icon",
}

# エディタで編集可能な拡張子ホワイトリスト
# "" は拡張子なし（Dockerfile, Makefile 等）およびドットファイル（.env 等）に対応
_EDITABLE_EXTENSIONS = {
    ".md", ".markdown", ".txt",
    ".json", ".yml", ".yaml", ".toml",
    ".ini", ".cfg", ".conf",
    ".env", ".gitignore", ".editorconfig",
    ".py", ".js", ".ts", ".html", ".css",
    ".sh", ".bat", ".cmd", ".ps1",
    "",
}


class FileExplorer:
    """Backend for the sidebar file explorer panel.

    Usage
    -----
    >>> explorer = FileExplorer()
    >>> items = explorer.list_directory("/some/path")
    >>> for item in items:
    ...     print(item["name"], item["is_dir"])
    """

    def __init__(self) -> None:
        """Initialise the FileExplorer.

        No special setup is required.
        """

    # ------------------------------------------------------------------
    # Directory listing
    # ------------------------------------------------------------------

    def list_directory(
        self,
        path: str,
        *,
        show_hidden: bool = False,
    ) -> list[dict]:
        """Return a sorted list of files and folders in *path*.

        Directories are listed first, then files.  Within each group
        entries are sorted alphabetically (case-insensitive).

        Parameters
        ----------
        path : str
            Directory to list.
        show_hidden : bool, optional
            When ``False`` (default) entries whose name starts with
            ``"."`` are excluded.

        Returns
        -------
        list[dict]
            Each dict contains:

            - **name** (``str``) -- file or folder name.
            - **path** (``str``) -- absolute path.
            - **is_dir** (``bool``) -- ``True`` for directories.
            - **size** (``int``) -- size in bytes (``0`` for dirs).
            - **modified** (``str``) -- last-modified timestamp in
              ISO 8601 format (``YYYY-MM-DD HH:MM:SS``).
        """
        try:
            entries = os.scandir(path)
        except PermissionError:
            logger.warning("Permission denied: %s", path)
            return []
        except FileNotFoundError:
            logger.warning("Directory not found: %s", path)
            return []
        except OSError as exc:
            logger.warning("Cannot list directory %s: %s", path, exc)
            return []

        dirs: list[dict] = []
        files: list[dict] = []

        try:
            for entry in entries:
                # Skip hidden files when configured to do so
                if not show_hidden and entry.name.startswith("."):
                    continue

                try:
                    stat_info = entry.stat()
                    modified = datetime.fromtimestamp(
                        stat_info.st_mtime
                    ).strftime("%Y-%m-%d %H:%M:%S")
                    size = stat_info.st_size if not entry.is_dir() else 0
                except OSError:
                    modified = ""
                    size = 0

                item = {
                    "name": entry.name,
                    "path": os.path.abspath(entry.path),
                    "is_dir": entry.is_dir(),
                    "size": size,
                    "modified": modified,
                }

                if entry.is_dir():
                    dirs.append(item)
                else:
                    files.append(item)
        finally:
            # scandir iterator should be closed explicitly
            if hasattr(entries, "close"):
                entries.close()

        # Sort each group alphabetically (case-insensitive)
        dirs.sort(key=lambda d: d["name"].lower())
        files.sort(key=lambda f: f["name"].lower())

        return dirs + files

    # ------------------------------------------------------------------
    # File reading
    # ------------------------------------------------------------------

    def read_file(self, path: str) -> str:
        """Read and return the text content of a file.

        Files larger than 1 MB are not read; an empty string is
        returned instead.

        Parameters
        ----------
        path : str
            Path to the text file.

        Returns
        -------
        str
            File content, or an empty string on error.
        """
        try:
            file_size = os.path.getsize(path)
            if file_size > _MAX_READ_BYTES:
                logger.warning(
                    "File too large to read (%d bytes, limit %d): %s",
                    file_size,
                    _MAX_READ_BYTES,
                    path,
                )
                return ""

            with open(path, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        except FileNotFoundError:
            logger.warning("File not found: %s", path)
            return ""
        except PermissionError:
            logger.warning("Permission denied reading file: %s", path)
            return ""
        except OSError as exc:
            logger.warning("Cannot read file %s: %s", path, exc)
            return ""

    # ------------------------------------------------------------------
    # Image reading (Base64 data URI)
    # ------------------------------------------------------------------

    def read_image_base64(self, path: str) -> str:
        """画像ファイルを Base64 data URI として返す。

        Parameters
        ----------
        path : str
            画像ファイルのパス。

        Returns
        -------
        str
            ``data:<mime>;base64,<encoded>`` 形式の文字列。
            失敗時は空文字列。
        """
        try:
            normalized = os.path.normpath(path)

            if not os.path.isfile(normalized):
                logger.warning("画像ファイルが存在しない: %s", normalized)
                return ""

            suffix = Path(normalized).suffix.lower()
            mime = _IMAGE_MIME_TYPES.get(suffix)
            if not mime:
                logger.warning("非対応の画像形式: %s (%s)", suffix, normalized)
                return ""

            file_size = os.path.getsize(normalized)
            if file_size > _MAX_IMAGE_BYTES:
                logger.warning(
                    "画像ファイルが大きすぎる (%d bytes, limit %d): %s",
                    file_size,
                    _MAX_IMAGE_BYTES,
                    normalized,
                )
                return ""

            with open(normalized, "rb") as f:
                raw = f.read()

            encoded = base64.b64encode(raw).decode("ascii")
            return f"data:{mime};base64,{encoded}"
        except PermissionError:
            logger.warning("画像ファイルの読み取り権限がない: %s", path)
            return ""
        except OSError as exc:
            logger.warning("画像ファイル読み取りに失敗 %s: %s", path, exc)
            return ""

    # ------------------------------------------------------------------
    # File saving
    # ------------------------------------------------------------------

    def save_file(self, path: str, content: str) -> bool:
        """テキストファイルを保存する。

        編集可能な拡張子（_EDITABLE_EXTENSIONS）のみ受け付ける。

        Parameters
        ----------
        path : str
            保存先ファイルパス。
        content : str
            書き込む内容。

        Returns
        -------
        bool
            保存成功なら ``True``。
        """
        try:
            suffix = Path(path).suffix.lower()
            if suffix not in _EDITABLE_EXTENSIONS:
                logger.warning("編集不可の拡張子: %s (%s)", suffix, path)
                return False

            if not os.path.isfile(path):
                logger.warning("保存先ファイルが存在しない: %s", path)
                return False

            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write(content)

            logger.info("ファイル保存完了: %s", path)
            return True
        except PermissionError:
            logger.warning("書き込み権限がない: %s", path)
            return False
        except OSError as exc:
            logger.warning("ファイル保存に失敗 %s: %s", path, exc)
            return False

    # ------------------------------------------------------------------
    # Opening files with default application
    # ------------------------------------------------------------------

    def open_file(self, path: str) -> bool:
        """Open a file with the operating system's default application.

        On Windows ``os.startfile`` is used; on Linux ``xdg-open`` is
        called via ``subprocess.Popen``.

        Parameters
        ----------
        path : str
            Path to the file to open.

        Returns
        -------
        bool
            ``True`` if the open command was issued without error.
        """
        try:
            system = platform.system()
            if system == "Windows":
                os.startfile(path)  # type: ignore[attr-defined]
            elif system == "Linux":
                subprocess.Popen(
                    ["xdg-open", path],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            elif system == "Darwin":
                subprocess.Popen(
                    ["open", path],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            else:
                logger.warning("Unsupported platform for open_file: %s", system)
                return False
            return True
        except FileNotFoundError:
            logger.warning("File not found: %s", path)
            return False
        except OSError as exc:
            logger.warning("Failed to open file %s: %s", path, exc)
            return False
