"""
session_store - sessions.json の読み書きによるセッション永続化モジュール

アプリ再起動時にセッション一覧を復元するために、
sessions.json をスレッドセーフかつ原子的に読み書きする。
"""

import json
import logging
import os
import tempfile
import threading
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)


def create_default_session(default_cwd: str | None = None) -> dict:
    """既定セッションを 1 件生成して返す。

    Parameters
    ----------
    default_cwd : str | None
        作業ディレクトリの既定値。``None`` ならユーザーホーム。

    Returns:
        dict: ``{"id": <uuid4_hex>, "name": "Main",
               "cwd": <default_cwd or ユーザーホーム>, "order": 0}``
    """
    return {
        "id": uuid.uuid4().hex,
        "name": "Main",
        "cwd": default_cwd or str(Path.home()),
        "order": 0,
        "agent_session_id": "",
    }


class SessionStore:
    """sessions.json に対するスレッドセーフな読み書きクラス。

    Parameters
    ----------
    store_path : str
        sessions.json のファイルパス。
    """

    def __init__(self, store_path: str) -> None:
        self._path = store_path
        self._lock = threading.Lock()

    # ------ public API ------

    def load(self) -> list[dict]:
        """sessions.json からセッション一覧を読み込む。

        ファイルが存在しない場合や JSON として不正な場合は
        空リストを返す。

        Returns:
            list[dict]: セッション辞書のリスト。
        """
        if not os.path.exists(self._path):
            logger.info("セッションファイルが見つからない: %s", self._path)
            return []

        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("セッションファイルの読み込みに失敗: %s", exc)
            return []

        if not isinstance(data, list):
            logger.warning("セッションファイルの形式が不正 (list でない)")
            return []

        return data

    def save(self, sessions: list[dict]) -> None:
        """セッション一覧を sessions.json へ原子的に書き込む。

        同一ディレクトリに一時ファイルを作成してから
        ``os.replace()`` でリネームすることで、
        書き込み中のクラッシュによるデータ破損を防ぐ。

        Parameters
        ----------
        sessions : list[dict]
            保存するセッション辞書のリスト。
        """
        store_dir = os.path.dirname(os.path.abspath(self._path))
        os.makedirs(store_dir, exist_ok=True)

        with self._lock:
            fd, tmp_path = tempfile.mkstemp(
                dir=store_dir, suffix=".tmp", prefix=".sessions_"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(sessions, f, indent=2, ensure_ascii=False)
                    f.write("\n")
                os.replace(tmp_path, self._path)
                logger.info("セッションファイルを保存: %s", self._path)
            except BaseException:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
                raise
