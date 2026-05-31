"""
session_manager - セッションの CRUD 操作と状態追跡を行うモジュール

SessionStore による永続化と PtyManager による PTY 制御を組み合わせ、
セッションのライフサイクル全体を管理する。
"""

from __future__ import annotations

import logging
import os
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from source.session.session_store import SessionStore, create_default_session
from source.session.session_id_collector import save_session_ids
from source.detection.agent_patterns import AGENTS

if TYPE_CHECKING:
    from source.pty.pty_manager import PtyManager

logger = logging.getLogger(__name__)


class SessionManager:
    """セッションの作成・取得・更新・削除および状態追跡を担当するクラス。

    Parameters
    ----------
    store : SessionStore
        セッション永続化ストア。
    pty_manager : PtyManager
        PTY ライフサイクル管理。
    default_cwd : str | None
        新規 / 初期セッションの既定作業ディレクトリ。
        ``None`` の場合はユーザーホーム。
    """

    def __init__(
        self,
        store: SessionStore,
        pty_manager: "PtyManager",
        default_cwd: str | None = None,
    ) -> None:
        self._store = store
        self._pty_manager = pty_manager
        self._default_cwd = default_cwd or str(Path.home())

        # セッション ID をキーとする内部状態辞書
        self._sessions: dict[str, dict] = {}

        # 現在アクティブなセッション ID
        self._active_session_id: str | None = None

    # ------------------------------------------------------------------
    # initialisation
    # ------------------------------------------------------------------

    def initialize(self) -> list[dict]:
        """ストアからセッションを読み込み、各セッションに PTY を起動する。

        ストアが空（初回起動）の場合はデフォルトセッションを 1 件作成する。

        Returns
        -------
        list[dict]
            起動済みセッションのリスト。
        """
        persisted = self._store.load()

        if not persisted:
            logger.info("保存済みセッションなし — デフォルトセッションを作成")
            persisted = [create_default_session(self._default_cwd)]
            self._store.save(persisted)

        for entry in persisted:
            sid = entry["id"]
            self._sessions[sid] = self._build_state(entry)
            self._spawn_pty(sid, entry.get("cwd"))

        logger.info("セッション初期化完了: %d 件", len(self._sessions))
        return self.get_sessions()

    # ------------------------------------------------------------------
    # read
    # ------------------------------------------------------------------

    def get_sessions(self) -> list[dict]:
        """全セッションの現在の状態を返す。

        Returns
        -------
        list[dict]
            ``order`` 昇順にソートしたセッション辞書のリスト。
        """
        return sorted(self._sessions.values(), key=lambda s: s["order"])

    def get_session(self, session_id: str) -> dict | None:
        """指定 ID のセッション辞書を返す。

        Parameters
        ----------
        session_id : str
            セッション ID。

        Returns
        -------
        dict | None
            セッション辞書。存在しなければ ``None``。
        """
        return self._sessions.get(session_id)

    # ------------------------------------------------------------------
    # create / delete
    # ------------------------------------------------------------------

    def add_session(
        self,
        name: str = "New Session",
        cwd: str | None = None,
    ) -> dict:
        """新しいセッションを作成し、PTY を起動する。

        Parameters
        ----------
        name : str
            セッション名（既定 ``"New Session"``）。
        cwd : str | None
            作業ディレクトリ。``None`` の場合はユーザーホーム。

        Returns
        -------
        dict
            作成されたセッション辞書。
        """
        sid = uuid.uuid4().hex
        order = self._next_order()
        resolved_cwd = cwd or self._default_cwd

        entry: dict = {
            "id": sid,
            "name": name,
            "cwd": resolved_cwd,
            "order": order,
        }
        self._sessions[sid] = self._build_state(entry)
        self._spawn_pty(sid, resolved_cwd)
        self.save_state()

        logger.info("セッション追加: id=%s, name=%s", sid, name)
        return self._sessions[sid]

    def remove_session(self, session_id: str) -> bool:
        """セッションと対応する PTY を削除する。

        Parameters
        ----------
        session_id : str
            削除対象のセッション ID。

        Returns
        -------
        bool
            削除に成功した場合 ``True``。
        """
        if session_id not in self._sessions:
            logger.warning("削除対象のセッションが見つからない: %s", session_id)
            return False

        self._close_pty(session_id)
        del self._sessions[session_id]

        # アクティブセッションが削除された場合はクリア
        if self._active_session_id == session_id:
            self._active_session_id = None

        self.save_state()
        logger.info("セッション削除: %s", session_id)
        return True

    # ------------------------------------------------------------------
    # update
    # ------------------------------------------------------------------

    def rename_session(self, session_id: str, name: str) -> bool:
        """セッション名を変更する。

        Parameters
        ----------
        session_id : str
            対象セッション ID。
        name : str
            新しいセッション名。

        Returns
        -------
        bool
            変更に成功した場合 ``True``。
        """
        session = self._sessions.get(session_id)
        if session is None:
            logger.warning("名前変更対象のセッションが見つからない: %s", session_id)
            return False

        session["name"] = name
        self.save_state()
        logger.info("セッション名変更: %s -> %s", session_id, name)
        return True

    def set_active_session(self, session_id: str) -> None:
        """アクティブセッションを切り替える。

        切り替え先の未読バッジを自動でクリアする。

        Parameters
        ----------
        session_id : str
            アクティブにするセッション ID。
        """
        if session_id not in self._sessions:
            logger.warning("アクティブ設定対象のセッションが見つからない: %s", session_id)
            return

        self._active_session_id = session_id
        self.mark_read(session_id)
        logger.debug("アクティブセッション変更: %s", session_id)

    def get_active_session_id(self) -> str | None:
        """現在のアクティブセッション ID を返す。

        Returns
        -------
        str | None
            アクティブセッション ID。未設定の場合 ``None``。
        """
        return self._active_session_id

    def update_cwd(self, session_id: str, cwd: str) -> None:
        """セッションの作業ディレクトリを更新する。

        Parameters
        ----------
        session_id : str
            対象セッション ID。
        cwd : str
            新しい作業ディレクトリパス。
        """
        session = self._sessions.get(session_id)
        if session is None:
            return
        if session.get("cwd") != cwd:
            session["cwd"] = cwd
            logger.debug("cwd 更新: session=%s, cwd=%s", session_id, cwd)

    def update_status(
        self,
        session_id: str,
        status: str,
        agent: str | None = None,
    ) -> None:
        """AI ステータスを更新する。

        非アクティブセッションのステータスが変化した場合、
        自動的に未読フラグを立てる。

        Parameters
        ----------
        session_id : str
            対象セッション ID。
        status : str
            新しいステータス (``"running"`` / ``"waiting"`` /
            ``"error"`` / ``"idle"``)。
        agent : str | None
            AI エージェント名（例: ``"claude"``）。
        """
        session = self._sessions.get(session_id)
        if session is None:
            logger.warning("ステータス更新対象のセッションが見つからない: %s", session_id)
            return

        old_status = session["status"]
        session["status"] = status

        if agent is not None:
            session["agent"] = agent

        # 非アクティブセッションで状態変化があれば未読フラグを立てる
        if session_id != self._active_session_id and status != old_status:
            session["unread"] = True

        logger.debug(
            "ステータス更新: %s  %s -> %s (agent=%s)",
            session_id,
            old_status,
            status,
            agent,
        )

    # ------------------------------------------------------------------
    # agent restore helpers
    # ------------------------------------------------------------------

    def set_agent_key(self, session_id: str, agent_key: str | None) -> None:
        """セッションの AI エージェントキーを設定する。

        Parameters
        ----------
        session_id : str
            対象セッション ID。
        agent_key : str | None
            エージェントキー (``"claude"`` 等)。``None`` でクリア。
        """
        session = self._sessions.get(session_id)
        if session is not None:
            session["agent_key"] = agent_key

    def set_agent_session_named(
        self, session_id: str, named: bool
    ) -> None:
        """AI ツール側のセッション命名済みフラグを設定する。

        Parameters
        ----------
        session_id : str
            対象セッション ID。
        named : bool
            命名済みなら ``True``。
        """
        session = self._sessions.get(session_id)
        if session is not None:
            session["agent_session_named"] = named

    # ------------------------------------------------------------------
    # unread badge
    # ------------------------------------------------------------------

    def mark_read(self, session_id: str) -> None:
        """未読バッジをクリアする。

        Parameters
        ----------
        session_id : str
            対象セッション ID。
        """
        session = self._sessions.get(session_id)
        if session is not None:
            session["unread"] = False

    def mark_unread(self, session_id: str) -> None:
        """未読バッジを設定する。

        Parameters
        ----------
        session_id : str
            対象セッション ID。
        """
        session = self._sessions.get(session_id)
        if session is not None:
            session["unread"] = True

    # ------------------------------------------------------------------
    # persistence / shutdown
    # ------------------------------------------------------------------

    def save_state(self) -> None:
        """現在のセッション一覧をストアに永続化する。

        永続化対象は ``id``, ``name``, ``cwd``, ``order`` に加え、
        AI ツール復元用の ``agent_key``, ``agent_session_named``,
        ``agent_session_id``。
        ランタイム状態 (``status``, ``agent``, ``unread``) は保存しない。
        """
        persistable = []
        for s in self._sessions.values():
            entry: dict = {
                "id": s["id"],
                "name": s["name"],
                "cwd": s["cwd"],
                "order": s["order"],
            }
            # AI ツール情報が存在する場合のみ保存
            if s.get("agent_key"):
                entry["agent_key"] = s["agent_key"]
                entry["agent_session_named"] = s.get(
                    "agent_session_named", False
                )
                entry["agent_session_id"] = s.get(
                    "agent_session_id", ""
                )
            persistable.append(entry)
        self._store.save(persistable)

    def close_all(self) -> None:
        """全セッションの PTY を閉じ、状態を保存する。

        保存前に session_ids.json を生成し、各セッションの
        agent_session_id を更新してから sessions.json を書き出す。
        """
        logger.info("全セッションのクローズを開始")
        for sid in list(self._sessions):
            self._close_pty(sid)

        # session_ids.json 生成 → agent_session_id 付与
        data_dir = os.path.dirname(os.path.abspath(self._store._path))
        self._populate_agent_session_ids(data_dir)

        self.save_state()
        logger.info("全セッションのクローズ完了")

    def _populate_agent_session_ids(self, data_dir: str) -> None:
        """session_ids.json を生成し、該当セッションに agent_session_id を設定する。

        codex/bob のセッション情報を収集して session_ids.json に書き出し、
        sessions.json の各エントリの name と cwd が一致するものに
        agent_session_id を付与する。
        claude/copilot は name で復元するため空文字のまま。

        Parameters
        ----------
        data_dir : str
            session_ids.json の出力先ディレクトリ。
        """
        try:
            session_ids = save_session_ids(data_dir)
        except Exception:
            logger.exception("session_ids.json の生成に失敗")
            return

        for s in self._sessions.values():
            agent_key = s.get("agent_key")
            if agent_key not in ("codex", "bob"):
                # claude/copilot は name で復元 — 空文字を設定
                s["agent_session_id"] = ""
                continue

            # name と cwd が一致するエントリを検索
            matched_id = ""
            for sid_entry in session_ids:
                if (
                    sid_entry.get("agent_key") == agent_key
                    and sid_entry.get("name") == s.get("name")
                    and sid_entry.get("cwd") == s.get("cwd")
                ):
                    matched_id = sid_entry.get("agent_session_id", "")
                    break

            s["agent_session_id"] = matched_id
            if matched_id:
                logger.info(
                    "agent_session_id 設定: session=%s, agent=%s, id=%s",
                    s["id"], agent_key, matched_id,
                )
            else:
                logger.warning(
                    "agent_session_id が見つからない: session=%s, agent=%s, name=%s",
                    s["id"], agent_key, s.get("name"),
                )

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------

    def _build_state(self, entry: dict) -> dict:
        """永続データにランタイムフィールドを付加した状態辞書を構築する。

        Parameters
        ----------
        entry : dict
            ストアから読み込んだ永続データ (``id``, ``name``,
            ``cwd``, ``order``, ``agent_key``,
            ``agent_session_named``)。

        Returns
        -------
        dict
            ランタイムフィールド (``status``, ``agent``, ``unread``)
            を含む完全な状態辞書。
        """
        # 保存済み agent_key から表示名を復元
        agent_key = entry.get("agent_key")
        agent_name = None
        if agent_key:
            agent_info = AGENTS.get(agent_key)
            if agent_info:
                agent_name = agent_info["name"]

        return {
            "id": entry["id"],
            "name": entry["name"],
            "cwd": entry.get("cwd", self._default_cwd),
            "order": entry.get("order", 0),
            # 永続化対象フィールド
            "agent_key": agent_key,
            "agent_session_named": entry.get("agent_session_named", False),
            "agent_session_id": entry.get("agent_session_id", ""),
            # ランタイム専用フィールド
            "status": "idle",
            "agent": agent_name,
            "unread": False,
        }

    def _next_order(self) -> int:
        """現在の最大 order + 1 を返す。"""
        if not self._sessions:
            return 0
        return max(s["order"] for s in self._sessions.values()) + 1

    def _spawn_pty(self, session_id: str, cwd: str | None) -> None:
        """PtyManager を介して PTY を起動する。"""
        try:
            self._pty_manager.create_session(session_id, cwd=cwd)
            logger.debug("PTY 起動: session=%s, cwd=%s", session_id, cwd)
        except Exception:
            logger.exception("PTY 起動に失敗: session=%s", session_id)

    def _close_pty(self, session_id: str) -> None:
        """PtyManager を介して PTY を終了する。"""
        try:
            self._pty_manager.destroy_session(session_id)
            logger.debug("PTY 終了: session=%s", session_id)
        except Exception:
            logger.exception("PTY 終了に失敗: session=%s", session_id)
