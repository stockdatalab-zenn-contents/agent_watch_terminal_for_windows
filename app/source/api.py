"""
api - pywebview JavaScript API ブリッジ

pywebview の JavaScript–Python 間通信レイヤー。
全パブリックメソッドは JS 側から ``window.pywebview.api.<method>()`` で呼び出される。
ロジックは各マネージャークラスに委譲し、本クラスは薄いブリッジに徹する。
"""

from __future__ import annotations

import logging
import os
import subprocess
import platform
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import pyperclip

from source.config import settings_manager
from source.detection.agent_patterns import (
    AGENTS,
    get_resume_command,
)

if TYPE_CHECKING:
    from source.session.session_manager import SessionManager
    from source.pty.pty_manager import PtyManager
    from source.recording.session_recorder import SessionRecorder
    from source.detection.agent_detector import AgentDetector

logger = logging.getLogger(__name__)


class Api:
    """pywebview に公開する JavaScript API ブリッジクラス。

    Parameters
    ----------
    session_manager : SessionManager
        セッション CRUD・状態管理。
    pty_manager : PtyManager
        PTY ライフサイクル・I/O 管理。
    file_explorer : object
        ファイルエクスプローラー操作 (list / open)。
    notification_manager : object
        通知制御 (toast / taskbar flash)。
    session_recorder : SessionRecorder
        セッション録画管理。
    on_pty_output : Callable[[str, bytes], None]
        PTY 出力の統合コールバック。
        agent_detector / session_recorder /
        JS 転送を連鎖実行する。main.py から注入される。
    on_rename : Callable[[str, str], None] | None
        セッション名変更後に呼ばれるコールバック。AI ツール側へ
        名前を反映する用途で main.py から注入される。省略可。
    on_remove : Callable[[str], None] | None
        セッション削除の直前に呼ばれるコールバック。PTY がまだ生きて
        いる状態で AI ツール側の後始末（タブを閉じる等）を行う用途で
        main.py から注入される。省略可。
    """

    def __init__(
        self,
        session_manager: "SessionManager",
        pty_manager: "PtyManager",
        file_explorer: object,
        notification_manager: object,
        session_recorder: "SessionRecorder",
        on_pty_output: Callable[[str, bytes], None],
        buffers_dir: str = "",
        agent_detector: "AgentDetector | None" = None,
        on_rename: Callable[[str, str], None] | None = None,
        on_remove: Callable[[str], None] | None = None,
    ) -> None:
        self._session_manager = session_manager
        self._pty_manager = pty_manager
        self._file_explorer = file_explorer
        self._notification_manager = notification_manager
        self._session_recorder = session_recorder
        self._on_pty_output = on_pty_output
        self._buffers_dir = buffers_dir
        self._agent_detector = agent_detector
        self._on_rename = on_rename
        self._on_remove = on_remove

    # ==================================================================
    # 初期化一括取得 (JS から呼び出し)
    # ==================================================================

    def get_init_data(self) -> dict:
        """フロントエンド初期化に必要な全データを一括返却する。

        1 回の IPC で settings / sessions / buffers / restore_hints を
        まとめて返すことで、起動時のラウンドトリップを削減する。

        Returns
        -------
        dict
            ``{"settings": dict, "sessions": list, "buffers": dict,
            "restore_hints": dict}``
        """
        # settings
        try:
            settings = settings_manager.get_settings()
        except Exception:
            logger.exception("get_init_data: settings 取得に失敗")
            settings = {}

        # sessions
        try:
            sessions = self._session_manager.get_sessions()
        except Exception:
            logger.exception("get_init_data: sessions 取得に失敗")
            sessions = []

        # buffers — 各セッションのバッファを一括読み込み
        buffers: dict[str, str] = {}
        if self._buffers_dir:
            for session in sessions:
                sid = session["id"]
                try:
                    path = os.path.join(self._buffers_dir, f"{sid}.txt")
                    if os.path.exists(path):
                        with open(path, "r", encoding="utf-8") as f:
                            buffers[sid] = f.read()
                except Exception:
                    logger.exception(
                        "get_init_data: buffer 読込に失敗: session=%s", sid
                    )

        # restore_hints
        hints = self.get_restore_hints()

        return {
            "settings": settings,
            "sessions": sessions,
            "buffers": buffers,
            "restore_hints": hints,
        }

    # ==================================================================
    # Session management (JS から呼び出し)
    # ==================================================================

    def add_session(self, name: str = "New Session") -> dict:
        """新しいセッションを作成する。

        Parameters
        ----------
        name : str
            セッション名。

        Returns
        -------
        dict
            作成されたセッション辞書。失敗時は空辞書。
        """
        try:
            session = self._session_manager.add_session(name=name)
            # 新規セッションも統合コールバック経由で読み取り開始。
            # これで agent_detector 等も発火し、ステータスラベルが更新される。
            self._pty_manager.start_reading(
                session["id"], self._on_pty_output
            )
            return session
        except Exception:
            logger.exception("add_session に失敗: name=%s", name)
            return {}

    def remove_session(self, session_id: str) -> bool:
        """セッションを削除する。

        Parameters
        ----------
        session_id : str
            削除対象のセッション ID。

        Returns
        -------
        bool
            削除成功なら ``True``。
        """
        try:
            # PTY を閉じる前に AI ツール側の後始末を済ませる。
            # 閉じた後では PTY へキーを送れないため順序が重要。
            if self._on_remove is not None:
                try:
                    self._on_remove(session_id)
                except Exception:
                    logger.exception(
                        "remove の外部反映に失敗: session=%s", session_id
                    )
            result = self._session_manager.remove_session(session_id)
            if result:
                self._delete_terminal_buffer(session_id)
            return result
        except Exception:
            logger.exception(
                "remove_session に失敗: session=%s", session_id
            )
            return False

    def rename_session(self, session_id: str, name: str) -> bool:
        """セッション名を変更する。

        AI ツールへの rename コマンド送信はシャットダウン時に行う。
        ここでは agent_session_named を False にリセットし、
        シャットダウン時の送信対象とする。

        Parameters
        ----------
        session_id : str
            対象セッション ID。
        name : str
            新しい名前。

        Returns
        -------
        bool
            変更成功なら ``True``。
        """
        try:
            ok = self._session_manager.rename_session(session_id, name)
            if not ok:
                return False

            # シャットダウン時に rename コマンドを送信するためフラグをリセット
            self._session_manager.set_agent_session_named(
                session_id, False
            )

            # AI ツール側へ即座に反映できるものは反映する
            # （opencode2 は REST API でタイトルを変更できる）
            if self._on_rename is not None:
                try:
                    self._on_rename(session_id, name)
                except Exception:
                    logger.exception(
                        "rename の外部反映に失敗: session=%s", session_id
                    )

            return True
        except Exception:
            logger.exception(
                "rename_session に失敗: session=%s, name=%s",
                session_id,
                name,
            )
            return False


    def set_active_session(self, session_id: str) -> None:
        """アクティブセッションを切り替える。

        Parameters
        ----------
        session_id : str
            アクティブにするセッション ID。
        """
        try:
            self._session_manager.set_active_session(session_id)
        except Exception:
            logger.exception(
                "set_active_session に失敗: session=%s", session_id
            )

    def mark_session_read(self, session_id: str) -> None:
        """未読バッジをクリアする。

        Parameters
        ----------
        session_id : str
            対象セッション ID。
        """
        try:
            self._session_manager.mark_read(session_id)
        except Exception:
            logger.exception(
                "mark_session_read に失敗: session=%s", session_id
            )

    # ==================================================================
    # Terminal I/O (JS から呼び出し)
    # ==================================================================

    def send_input(self, session_id: str, data: str) -> None:
        """キーボード入力を PTY へ送信する。

        Parameters
        ----------
        session_id : str
            送信先のセッション ID。
        data : str
            送信する入力文字列。
        """
        try:
            # Ctrl+C 検出のため入力データを agent_detector に通知
            if self._agent_detector is not None:
                self._agent_detector.feed_input(session_id, data)
            self._pty_manager.write(session_id, data)
        except Exception:
            logger.exception(
                "send_input に失敗: session=%s", session_id
            )

    def resize_terminal(
        self, session_id: str, cols: int, rows: int
    ) -> None:
        """PTY のサイズを変更する。

        Parameters
        ----------
        session_id : str
            対象セッション ID。
        cols : int
            新しいカラム数。
        rows : int
            新しい行数。
        """
        try:
            self._pty_manager.resize(session_id, cols, rows)
        except Exception:
            logger.exception(
                "resize_terminal に失敗: session=%s, cols=%d, rows=%d",
                session_id,
                cols,
                rows,
            )

    # ==================================================================
    # File explorer (JS から呼び出し)
    # ==================================================================

    def list_files(self, path: str = "") -> list[dict]:
        """ディレクトリの内容を一覧で返す。

        path が空の場合、アクティブセッションの作業ディレクトリを使用する。

        Parameters
        ----------
        path : str
            対象ディレクトリパス。空文字列ならアクティブセッションの cwd。

        Returns
        -------
        list[dict]
            ファイル・ディレクトリ情報の辞書リスト。
        """
        try:
            if not path:
                active_id = self._session_manager.get_active_session_id()
                if active_id is not None:
                    session = self._session_manager.get_session(active_id)
                    if session is not None:
                        path = session.get("cwd", str(Path.home()))
                if not path:
                    path = str(Path.home())

            target = Path(path)
            if not target.is_dir():
                logger.warning("指定パスはディレクトリでない: %s", path)
                return []

            entries: list[dict] = []
            for item in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
                try:
                    stat = item.stat()
                    entries.append({
                        "name": item.name,
                        "path": str(item),
                        "is_dir": item.is_dir(),
                        "size": stat.st_size if item.is_file() else 0,
                        "modified": stat.st_mtime,
                    })
                except OSError:
                    # アクセス権限エラー等 — スキップ
                    continue

            return entries
        except Exception:
            logger.exception("list_files に失敗: path=%s", path)
            return []

    def open_file(self, path: str) -> bool:
        """ファイルを OS デフォルトアプリで開く。

        Parameters
        ----------
        path : str
            開くファイルのパス。

        Returns
        -------
        bool
            起動に成功した場合 ``True``。
        """
        try:
            file_path = Path(path)
            if not file_path.exists():
                logger.warning("ファイルが存在しない: %s", path)
                return False

            system = platform.system()
            if system == "Windows":
                os.startfile(path)  # type: ignore[attr-defined]
            elif system == "Darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])

            logger.debug("ファイルを開く: %s", path)
            return True
        except Exception:
            logger.exception("open_file に失敗: path=%s", path)
            return False

    def open_url(self, url: str) -> bool:
        """URL を OS 既定ブラウザで開く。

        マークダウンプレビュー内のリンククリック時に使用する。
        pywebview ウィンドウ内ナビゲーションを防止し、外部ブラウザに委譲する。

        Parameters
        ----------
        url : str
            開く URL。

        Returns
        -------
        bool
            起動に成功した場合 ``True``。
        """
        try:
            import webbrowser

            webbrowser.open(url)
            logger.debug("URL を外部ブラウザで開く: %s", url)
            return True
        except Exception:
            logger.exception("open_url に失敗: url=%s", url)
            return False

    def read_file_content(self, path: str) -> str:
        """ファイルの生テキストを返す。

        Parameters
        ----------
        path : str
            読み取り対象ファイルのパス。

        Returns
        -------
        str
            ファイル内容。失敗時は空文字列。
        """
        try:
            return self._file_explorer.read_file(path)
        except Exception:
            logger.exception("read_file_content に失敗: path=%s", path)
            return ""

    def read_image_base64(self, path: str) -> str:
        """画像ファイルを Base64 data URI として返す。

        Parameters
        ----------
        path : str
            画像ファイルのパス。

        Returns
        -------
        str
            ``data:<mime>;base64,<encoded>`` 形式。失敗時は空文字列。
        """
        try:
            return self._file_explorer.read_image_base64(path)
        except Exception:
            logger.exception("read_image_base64 に失敗: path=%s", path)
            return ""

    def save_file(self, path: str, content: str) -> bool:
        """ファイルを保存する。

        Parameters
        ----------
        path : str
            保存先ファイルのパス。
        content : str
            書き込む内容。

        Returns
        -------
        bool
            保存成功なら ``True``。
        """
        try:
            return self._file_explorer.save_file(path, content)
        except Exception:
            logger.exception("save_file に失敗: path=%s", path)
            return False

    # ==================================================================
    # Clipboard (JS から呼び出し)
    # ==================================================================

    def copy_to_clipboard(self, text: str) -> None:
        """テキストをクリップボードにコピーする。

        Parameters
        ----------
        text : str
            コピーする文字列。
        """
        try:
            pyperclip.copy(text)
            logger.debug("クリップボードにコピー完了 (長さ=%d)", len(text))
        except Exception:
            logger.exception("copy_to_clipboard に失敗")

    def paste_from_clipboard(self) -> str:
        """クリップボードの内容を返す。

        Returns
        -------
        str
            クリップボードのテキスト。失敗時は空文字列。
        """
        try:
            return pyperclip.paste()
        except Exception:
            logger.exception("paste_from_clipboard に失敗")
            return ""

    # ==================================================================
    # Session restore hints (JS から呼び出し)
    # ==================================================================

    def get_restore_hints(self) -> dict[str, str]:
        """自動復元できないセッションのヒントテキストを返す。

        agent_key が保存されているが agent_session_named=False の
        セッションに対して、手動復元用のヒントを返す。

        Returns
        -------
        dict[str, str]
            session_id → ANSI 装飾付きヒントテキスト。
        """
        hints: dict[str, str] = {}
        for session in self._session_manager.get_sessions():
            # 1 セッションの失敗で後続のヒントを失わないよう個別に保護
            try:
                agent_key = session.get("agent_key")
                if not agent_key:
                    continue
                agent_info = AGENTS.get(agent_key)
                if not agent_info:
                    continue

                name = session["name"]
                agent_session_id = session.get("agent_session_id", "")
                # 自動復元されるセッションはヒント不要。
                # ID ベース復元でも agent_session_id が未取得なら
                # resume_command_fallback へ倒れて自動復元されるため、
                # 「コマンドを組み立てられるか」で判定する。
                auto_restorable = bool(
                    session.get("agent_session_named")
                ) and bool(
                    get_resume_command(agent_key, name, agent_session_id)
                )
                if auto_restorable:
                    continue

                start = agent_info["start_command"]
                # ここへ来るのは自動復元できないセッション。
                # ID ベース復元のエージェントは {agent_session_id} を含むため
                # 両方のプレースホルダを必ず渡す。ID 未取得時は
                # プレースホルダを表示して手動入力を促す。
                resume = agent_info["resume_command"].format(
                    name=name,
                    agent_session_id=agent_session_id or "<session-id>",
                )
                hints[session["id"]] = (
                    f"\r\n\x1b[33m[Agent Watch] "
                    f"前回 {agent_info['name']} が動作していました\x1b[0m"
                    f"\r\n\x1b[33m復元: {start} → {resume}\x1b[0m\r\n"
                )
            except Exception:
                logger.exception(
                    "復元ヒントの生成に失敗: session=%s", session.get("id")
                )
        return hints

    # ==================================================================
    # Terminal buffer persistence (JS から呼び出し)
    # ==================================================================

    def _delete_terminal_buffer(self, session_id: str) -> None:
        """セッション削除時にバッファファイルを削除する。

        Parameters
        ----------
        session_id : str
            対象セッション ID。
        """
        if not self._buffers_dir:
            return
        try:
            path = os.path.join(self._buffers_dir, f"{session_id}.txt")
            if os.path.exists(path):
                os.remove(path)
                logger.debug("バッファファイル削除: %s", path)
        except Exception:
            logger.exception(
                "バッファファイル削除に失敗: session=%s", session_id
            )
