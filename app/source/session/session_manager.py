"""
session_manager - セッションの CRUD 操作と状態追跡を行うモジュール

SessionStore による永続化と PtyManager による PTY 制御を組み合わせ、
セッションのライフサイクル全体を管理する。
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from source.config.settings_manager import get
from source.session.session_store import SessionStore, create_default_session
from source.session.session_id_collector import save_session_ids
from source.detection.agent_patterns import (
    AGENTS,
    SESSION_MATCH_CWD_LATEST,
    SESSION_MATCH_NAME_CWD,
    SESSION_MATCH_NONE,
    get_session_match_mode,
)

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

    def set_agent_started_at(
        self, session_id: str, iso_timestamp: str | None = None
    ) -> None:
        """AI ツールのゲート開放時刻を設定する。

        終了時のセッション ID 突合で「この起動より後に更新された
        セッションのみを候補にする」下限として使う。

        Parameters
        ----------
        session_id : str
            対象セッション ID。
        iso_timestamp : str | None
            ISO 8601 (UTC) 文字列。``None`` なら現在時刻。
        """
        session = self._sessions.get(session_id)
        if session is None:
            return
        if iso_timestamp is None:
            iso_timestamp = datetime.now(timezone.utc).isoformat()
        session["agent_started_at"] = iso_timestamp
        logger.debug(
            "ゲート開放時刻を記録: session=%s, at=%s",
            session_id, iso_timestamp,
        )

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
        ``agent_session_id``, ``agent_started_at``。
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
                entry["agent_started_at"] = s.get(
                    "agent_started_at", ""
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

        AI ツール側のセッション情報を収集して session_ids.json に書き出し、
        エージェント定義の ``session_match`` に従って agent_session_id を
        付与する。

        - ``none`` (claude/copilot): name で復元するため空文字のまま
        - ``name_cwd`` (codex/bob): name と cwd の完全一致で突合
        - ``cwd_latest`` (opencode): 2 段階で割り当てる。まず前回取得済みの
          ID がまだ存在するセッションへ優先的に予約し（同一 cwd の複数タブで
          ID が入れ替わるのを防ぐ）、残りへ cwd 一致かつゲート開放後に
          更新されたもののうち最終更新が新しい順に割当

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

        # 割当済みの ID — cwd_latest 方式で同一 cwd への重複割当を防ぐ
        assigned_ids: set[str] = set()
        # 第 1 パスで確定したアプリ側セッション ID
        resolved: set[str] = set()

        # cwd_latest は更新の新しい順に上から割り当てるため、
        # 走査順をタブ表示順（order 昇順）に揃える
        ordered = self.get_sessions()

        # --- 第 1 パス: 前回取得済みの ID がまだ有効なら最優先で予約 ---
        # 同一 cwd に複数タブがある場合、更新の新しい順だけで割り当てると
        # タブ間で ID が入れ替わるため、自タブの ID を先に押さえる。
        for s in ordered:
            agent_key = s.get("agent_key") or ""
            if get_session_match_mode(agent_key) != SESSION_MATCH_CWD_LATEST:
                continue
            try:
                kept = self._keep_existing_id(
                    session_ids,
                    agent_key,
                    s.get("cwd", ""),
                    s.get("agent_session_id", ""),
                    assigned_ids,
                )
            except Exception:
                logger.exception(
                    "既存 agent_session_id の確認に失敗: session=%s", s["id"]
                )
                continue
            if kept:
                s["agent_session_id"] = kept
                assigned_ids.add(kept)
                resolved.add(s["id"])

        # --- 第 2 パス: 残りを突合方式に従って割り当てる ---
        for s in ordered:
            if s["id"] in resolved:
                continue

            agent_key = s.get("agent_key") or ""
            match_mode = get_session_match_mode(agent_key)

            if match_mode == SESSION_MATCH_NONE:
                # name で復元するエージェント — 空文字を設定
                s["agent_session_id"] = ""
                continue

            try:
                if match_mode == SESSION_MATCH_CWD_LATEST:
                    matched_id = self._match_by_cwd_latest(
                        session_ids,
                        agent_key,
                        s.get("cwd", ""),
                        assigned_ids,
                        s.get("agent_started_at", ""),
                    )
                elif match_mode == SESSION_MATCH_NAME_CWD:
                    matched_id = self._match_by_name_cwd(
                        session_ids,
                        agent_key,
                        s.get("name", ""),
                        s.get("cwd", ""),
                    )
                else:
                    # 未知の突合方式 — 黙って name_cwd へ倒さず原因を残す
                    logger.error(
                        "未知の session_match: agent=%s, mode=%s",
                        agent_key, match_mode,
                    )
                    matched_id = ""
            except Exception:
                # 1 セッションの失敗で終了処理全体を止めない
                logger.exception(
                    "agent_session_id の突合に失敗: session=%s", s["id"]
                )
                matched_id = ""

            s["agent_session_id"] = matched_id
            if matched_id:
                assigned_ids.add(matched_id)
                logger.info(
                    "agent_session_id 設定: session=%s, agent=%s, id=%s",
                    s["id"], agent_key, matched_id,
                )
            else:
                logger.warning(
                    "agent_session_id が見つからない: session=%s, agent=%s, name=%s",
                    s["id"], agent_key, s.get("name"),
                )

    @staticmethod
    def _match_by_name_cwd(
        session_ids: list[dict],
        agent_key: str,
        name: str,
        cwd: str,
    ) -> str:
        """name と cwd の完全一致で agent_session_id を検索する。

        Parameters
        ----------
        session_ids : list[dict]
            収集済みセッション情報。
        agent_key : str
            対象エージェントキー。
        name : str
            アプリ側セッション名。
        cwd : str
            アプリ側作業ディレクトリ。

        Returns
        -------
        str
            一致した agent_session_id。見つからない場合は空文字。

        Notes
        -----
        cwd は既存挙動を維持するため完全一致で比較する
        （``_match_by_cwd_latest`` の正規化比較とは非対称）。
        """
        for sid_entry in session_ids:
            if (
                sid_entry.get("agent_key") == agent_key
                and sid_entry.get("name") == name
                and sid_entry.get("cwd") == cwd
            ):
                return sid_entry.get("agent_session_id", "")
        return ""

    @staticmethod
    def _match_by_cwd_latest(
        session_ids: list[dict],
        agent_key: str,
        cwd: str,
        assigned_ids: set[str],
        started_at: str = "",
    ) -> str:
        """cwd 一致のうち最終更新が新しいものを検索する。

        セッション名を任意に付けられない AI ツール（opencode 等）向け。
        ``session_ids`` は収集側で更新日時の降順に並んでいる前提。
        既に他セッションへ割り当てた ID は除外する。

        ``started_at`` を与えると、それ以降に更新されたセッションのみを
        候補にする。AI ツールを起動しただけでメッセージを送らなかった
        場合に、同じ cwd の古いセッションを誤って復元しないための下限。

        Parameters
        ----------
        session_ids : list[dict]
            収集済みセッション情報。
        agent_key : str
            対象エージェントキー。
        cwd : str
            アプリ側作業ディレクトリ。
        assigned_ids : set[str]
            割当済み agent_session_id の集合。
        started_at : str
            ゲート開放時刻の ISO 8601 文字列。空なら下限なし。

        Returns
        -------
        str
            一致した agent_session_id。見つからない場合は空文字。
        """
        if not cwd:
            return ""

        target = SessionManager._normalize_cwd(cwd)
        for sid_entry in session_ids:
            if sid_entry.get("agent_key") != agent_key:
                continue
            if SessionManager._normalize_cwd(sid_entry.get("cwd", "")) != target:
                continue
            if not SessionManager._is_updated_after(
                sid_entry.get("updated_at", ""), started_at
            ):
                continue
            candidate = sid_entry.get("agent_session_id", "")
            if candidate and candidate not in assigned_ids:
                return candidate
        return ""

    @staticmethod
    def _keep_existing_id(
        session_ids: list[dict],
        agent_key: str,
        cwd: str,
        current_id: str,
        assigned_ids: set[str],
    ) -> str:
        """前回取得済みの agent_session_id を、まだ有効なら維持する。

        AI ツール側に同じ ID のセッションが同じ cwd で残っている場合のみ
        採用する。既に他セッションへ割り当て済みの ID は採用しない。

        Parameters
        ----------
        session_ids : list[dict]
            収集済みセッション情報。
        agent_key : str
            対象エージェントキー。
        cwd : str
            アプリ側作業ディレクトリ。
        current_id : str
            前回保存された agent_session_id。
        assigned_ids : set[str]
            割当済み agent_session_id の集合。

        Returns
        -------
        str
            維持する agent_session_id。該当しない場合は空文字。
        """
        if not current_id or current_id in assigned_ids:
            return ""

        target = SessionManager._normalize_cwd(cwd)
        for sid_entry in session_ids:
            if sid_entry.get("agent_key") != agent_key:
                continue
            if sid_entry.get("agent_session_id", "") != current_id:
                continue
            if SessionManager._normalize_cwd(sid_entry.get("cwd", "")) != target:
                continue
            logger.info(
                "agent_session_id を維持（更新なし）: agent=%s, id=%s",
                agent_key, current_id,
            )
            return current_id
        return ""

    @staticmethod
    def _is_updated_after(updated_at: object, started_at: object) -> bool:
        """``updated_at`` が ``started_at`` 以降かどうかを判定する。

        ``started_at`` が空（下限なし）なら常に ``True``。
        いずれかが ISO 8601 として解釈できない場合は ``False`` を返し、
        誤った復元を避ける（判定できないものは候補にしない）。

        Parameters
        ----------
        updated_at : object
            AI ツール側セッションの最終更新時刻 (ISO 8601)。
        started_at : object
            ゲート開放時刻 (ISO 8601)。空なら下限なし。

        Returns
        -------
        bool
            候補として採用してよいなら ``True``。
        """
        if not started_at:
            return True

        base = SessionManager._parse_iso(started_at)
        target = SessionManager._parse_iso(updated_at)
        if base is None or target is None:
            logger.warning(
                "更新時刻の比較に失敗: updated_at=%r, started_at=%r",
                updated_at, started_at,
            )
            return False
        return target >= base

    @staticmethod
    def _parse_iso(value: object) -> datetime | None:
        """ISO 8601 文字列を timezone 付き datetime へ変換する。

        タイムゾーン指定がない文字列は UTC とみなす。
        sessions.json が手編集・破損などで文字列以外を持っていても
        例外を投げない。

        Parameters
        ----------
        value : object
            ISO 8601 文字列。文字列以外は解釈不能として扱う。

        Returns
        -------
        datetime | None
            変換結果。解釈できない場合は ``None``。
        """
        if not isinstance(value, str) or not value:
            return None
        text = value.strip()
        # 末尾 Z 表記を +00:00 へ寄せる
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed

    @staticmethod
    def _normalize_cwd(path: str) -> str:
        """cwd 比較用にパスを正規化する。

        AI ツール側がスラッシュ区切り・大小文字違いで保持している場合に
        備え、区切り文字と大小文字を揃える。

        Parameters
        ----------
        path : str
            正規化対象のパス。

        Returns
        -------
        str
            正規化済みパス。空文字はそのまま返す。
        """
        if not path:
            return ""
        return os.path.normcase(os.path.normpath(path))

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
            ``agent_session_named``, ``agent_session_id``,
            ``agent_started_at``)。

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
            "agent_started_at": entry.get("agent_started_at", ""),
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
        """PtyManager を介して PTY を起動する。

        初期寸法は settings.json の terminal.initial_cols / initial_rows を
        使う。フロント側の Terminal 生成も同じ値を読むため、ウィンドウが
        表示されて実サイズへ fit されるまでの間、PTY と xterm.js の寸法が
        一致する。非アクティブセッションは切り替えるまで fit されないので、
        ここが食い違うと表示が崩れたまま残る。
        """
        try:
            self._pty_manager.create_session(
                session_id,
                cols=get("terminal.initial_cols", 120),
                rows=get("terminal.initial_rows", 30),
                cwd=cwd,
            )
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
