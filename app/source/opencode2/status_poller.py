"""status_poller - opencode2 のセッション状態をポーリングして通知するモジュール

awt のターミナル（1 タブ）と opencode2 のセッションを **1 対 1 で対応づける**。
opencode2 のセッションはバックグラウンドサーバー側にあり、どの TUI
インスタンスが開いているかは API から取れないため、次の順で束縛する。

1. awt が復元時に指定した ``agent_session_id``（最も確実）
2. 前回のポーリングで束縛済みの ID（実行中は維持する）
3. cwd が一致し、ゲート開放時刻以降に更新され、他のターミナルが
   まだ握っていないセッションのうち最新のもの

束縛が決まれば、そのセッションの状態だけを見る。同じ cwd に複数の
ターミナルがあっても、互いの状態を混同しない。

既知の制約: opencode2 の ``GET /api/session`` は最上位セッションのみを
返し、親子関係も公開しない。そのためサブエージェントが要求した権限待ちは
どのターミナルにも紐付けられず、検知できない。
"""

import logging
import os
import threading
from datetime import datetime

from source.opencode2.api_client import Opencode2ApiClient

logger = logging.getLogger(__name__)

# 集約の優先度。数値が大きいほど優先される。
_STATUS_PRIORITY = {
    "idle": 0,
    "running": 1,
    "waiting": 2,
    "error": 3,
}


class Opencode2StatusPoller:
    """opencode2 のセッション状態を定期取得し、ターミナル単位で通知する。

    1 本のワーカースレッドで登録済みの全ターミナルをまとめて処理する。
    ターミナルごとにスレッドを立てると、タブが増えるほど API 呼び出しと
    スレッドが線形に増えてしまうため。

    Parameters
    ----------
    on_status : callable
        ``on_status(terminal_id, status, detail, children)`` の形で
        呼ばれるコールバック。``children`` はセッション ID をキーとする
        ``{"title": str, "status": str}`` の辞書。
    get_terminal_info : callable
        ``get_terminal_info(terminal_id) -> dict | None``。
        ``{"cwd": str, "session_id": str, "started_at": str}`` を返す。
        ターミナルが既に閉じられている場合は None または空の cwd を返す。
    interval_sec : float
        ポーリング間隔（秒）。opencode2 の 1 ターンは 2 秒前後で終わることが
        あり、間隔が長いと実行中を一度も観測できないまま終わる。1 周期の
        所要時間は実測で中央値 40ms・最大 104ms のため、0.75 秒でも
        取得が待機時間を圧迫しない。
    client : Opencode2ApiClient | None
        差し替え用。省略時は既定のクライアントを生成する。
    on_unavailable : callable | None
        ``on_unavailable(terminal_id)`` の形で呼ばれるコールバック。
        サーバーへ到達できず状態を判断できない間、従来の画面文言による
        判定へ戻してもらうために使う。省略可。
    """

    def __init__(
        self,
        on_status,
        get_terminal_info,
        interval_sec: float = 0.75,
        client: Opencode2ApiClient | None = None,
        on_unavailable=None,
    ) -> None:
        self._on_status = on_status
        self._on_unavailable = on_unavailable
        self._get_terminal_info = get_terminal_info
        self._interval_sec = interval_sec
        self._client = client or Opencode2ApiClient()

        self._lock = threading.Lock()
        # terminal_id -> 直近に通知したステータス（重複通知の抑止用）
        self._targets: dict[str, str] = {}
        # terminal_id -> セッション ID をキーとする状態の辞書
        self._children: dict[str, dict] = {}
        # terminal_id -> 束縛した opencode2 のセッション ID
        self._bound: dict[str, str] = {}

        self._wakeup = threading.Event()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # 公開 API — 監視対象の登録・解除
    # ------------------------------------------------------------------

    def watch(self, terminal_id: str) -> None:
        """ターミナルを監視対象に加える。

        既に監視中なら何もしない。初回登録時にワーカースレッドを起動する。

        Parameters
        ----------
        terminal_id : str
            awt のセッション（ターミナル）ID。
        """
        with self._lock:
            if terminal_id in self._targets:
                return
            self._targets[terminal_id] = ""
            self._children[terminal_id] = {}
            logger.info("opencode2 の状態監視を開始: terminal=%s", terminal_id)

        self._ensure_thread()
        # 登録直後に 1 回走らせ、間隔ぶんの待ちを挟まずに反映する
        self._wakeup.set()

    def unwatch(self, terminal_id: str) -> None:
        """ターミナルを監視対象から外す。

        Parameters
        ----------
        terminal_id : str
            awt のセッション（ターミナル）ID。
        """
        with self._lock:
            existed = self._targets.pop(terminal_id, None) is not None
            self._children.pop(terminal_id, None)
            self._bound.pop(terminal_id, None)
        if existed:
            logger.info("opencode2 の状態監視を停止: terminal=%s", terminal_id)

    def is_watching(self, terminal_id: str) -> bool:
        """監視中かどうかを返す。

        Parameters
        ----------
        terminal_id : str
            awt のセッション（ターミナル）ID。

        Returns
        -------
        bool
            監視中なら True。
        """
        with self._lock:
            return terminal_id in self._targets

    def get_children(self, terminal_id: str) -> dict:
        """ターミナルに束縛された opencode2 セッションの状態を返す。

        Parameters
        ----------
        terminal_id : str
            awt のセッション（ターミナル）ID。

        Returns
        -------
        dict
            セッション ID をキーとする ``{"title", "status"}`` の辞書のコピー。
        """
        with self._lock:
            return dict(self._children.get(terminal_id, {}))

    def get_bound_session_id(self, terminal_id: str) -> str:
        """ターミナルに束縛された opencode2 のセッション ID を返す。

        Parameters
        ----------
        terminal_id : str
            awt のセッション（ターミナル）ID。

        Returns
        -------
        str
            セッション ID。束縛されていなければ空文字。
        """
        with self._lock:
            return self._bound.get(terminal_id, "")

    def rename_bound_session(self, terminal_id: str, title: str) -> bool:
        """ターミナルに束縛されたセッションのタイトルを変更する。

        awt でセッション名を変えたときに opencode2 側へ反映するために使う。

        Parameters
        ----------
        terminal_id : str
            awt のセッション（ターミナル）ID。
        title : str
            新しいタイトル。

        Returns
        -------
        bool
            成功なら True。束縛が無い場合や失敗した場合は False。
        """
        session_id = self.get_bound_session_id(terminal_id)
        if not session_id:
            return False
        return self._client.rename_session(session_id, title)

    def stop_all(self) -> None:
        """ワーカースレッドを停止し、監視対象をすべて解除する。"""
        with self._lock:
            self._targets.clear()
            self._children.clear()
            self._bound.clear()
        self._stop_event.set()
        self._wakeup.set()

        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=3.0)
        self._thread = None
        logger.info("opencode2 の状態監視をすべて停止")

    # ------------------------------------------------------------------
    # 内部 — ワーカースレッド
    # ------------------------------------------------------------------

    def _ensure_thread(self) -> None:
        """ワーカースレッドが動いていなければ起動する。"""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        thread = threading.Thread(
            target=self._run,
            name="opencode2-status-poller",
            daemon=True,
        )
        self._thread = thread
        thread.start()

    def _run(self) -> None:
        """ポーリングのメインループ。"""
        while not self._stop_event.is_set():
            try:
                self._poll_once()
            except Exception:
                # 1 回の失敗でループを止めない。次の周期で再試行する。
                logger.exception("opencode2 の状態取得で想定外のエラー")

            self._wakeup.wait(self._interval_sec)
            self._wakeup.clear()

    def _collect_infos(self, terminal_ids: list[str]) -> dict:
        """各ターミナルの cwd・セッション ID・開始時刻を集める。

        cwd が取れないターミナルは既に閉じられているとみなし、監視対象から
        自動的に外す（タブ削除のたびに外部から unwatch を呼ばずに済ませる）。

        Parameters
        ----------
        terminal_ids : list[str]
            監視中のターミナル ID。

        Returns
        -------
        dict
            terminal_id をキーとする情報の辞書。
        """
        infos: dict[str, dict] = {}
        for terminal_id in terminal_ids:
            try:
                info = self._get_terminal_info(terminal_id) or {}
            except Exception:
                logger.exception(
                    "ターミナル情報の取得に失敗: terminal=%s", terminal_id
                )
                continue

            cwd = info.get("cwd") or ""
            if not cwd:
                self.unwatch(terminal_id)
                continue

            infos[terminal_id] = {
                # API へは正規化前の生のパスを渡す。正規化（Windows では
                # 小文字化）した文字列を渡すと権限待ちが取得できない（実測）。
                "cwd": cwd,
                "norm": _normalize(cwd),
                "session_id": info.get("session_id") or "",
                "started_at": info.get("started_at") or "",
            }
        return infos

    def _poll_once(self) -> None:
        """1 周期分の取得と通知を行う。"""
        with self._lock:
            terminal_ids = list(self._targets.keys())
        if not terminal_ids:
            return

        infos = self._collect_infos(terminal_ids)
        if not infos:
            return

        # 実行中セッションはロケーションに依らず 1 回で取れる。
        # None はサーバーへ到達できないことを意味する。「実行中が 0 件」
        # とは区別し、状態を判断できない間は idle を配らずに
        # 従来の画面文言による判定へ戻してもらう。
        active = self._client.fetch_active()
        if active is None:
            logger.debug("opencode2 のサーバーへ到達できないため判定を見送る")
            self._notify_unavailable(list(infos.keys()))
            return

        # 同じディレクトリを見ているターミナルが複数あっても、
        # API 呼び出しは 1 回で済ませる。代表として生のパスを 1 つ選ぶ。
        groups: dict[str, str] = {}
        for info in infos.values():
            groups.setdefault(info["norm"], info["cwd"])

        fetched: dict[str, dict] = {}
        for normalized, raw_cwd in sorted(groups.items()):
            fetched[normalized] = {
                "sessions": self._client.list_sessions(raw_cwd),
                "permissions": self._client.list_permission_requests(raw_cwd),
                "forms": self._client.list_form_requests(raw_cwd),
            }

        self._resolve_and_publish(infos, fetched, active)

    def _resolve_and_publish(
        self, infos: dict, fetched: dict, active: dict
    ) -> None:
        """ターミナルごとにセッションを束縛し、状態を通知する。

        Parameters
        ----------
        infos : dict
            terminal_id をキーとするターミナル情報。
        fetched : dict
            正規化ディレクトリをキーとする取得結果。
        active : dict
            実行中セッションの辞書。
        """
        with self._lock:
            bound = dict(self._bound)

        # 既に他のターミナルが握っている ID は取り合いにしない
        claimed = set(bound.values())

        # 束縛の結果が実行順に依存しないよう、決定的な順序で処理する
        for terminal_id in sorted(infos.keys()):
            info = infos[terminal_id]
            pack = fetched.get(info["norm"], {})
            sessions = pack.get("sessions", [])
            by_id = {
                s.get("id"): s
                for s in sessions
                if isinstance(s.get("id"), str) and s.get("id")
            }

            session_id = info["session_id"] or bound.get(terminal_id, "")
            if not (session_id and session_id in by_id):
                # 既知の ID が無い、または既に消えている → 推定し直す
                claimed.discard(bound.get(terminal_id, ""))
                session_id = _infer_session_id(
                    sessions, info["norm"], info["started_at"], claimed
                )
                if session_id:
                    logger.info(
                        "opencode2 セッションを束縛: terminal=%s, session=%s",
                        terminal_id,
                        session_id,
                    )

            if session_id:
                claimed.add(session_id)
                bound[terminal_id] = session_id
            else:
                bound.pop(terminal_id, None)

            children = build_children_for_session(
                session_id,
                by_id,
                active,
                pack.get("permissions", []),
                pack.get("forms", []),
            )
            self._publish(terminal_id, children, session_id)

    def _notify_unavailable(self, terminal_ids: list[str]) -> None:
        """サーバーへ到達できないことを通知する。

        直近の通知状態をリセットし、復旧後に同じステータスでも改めて
        通知されるようにする。

        Parameters
        ----------
        terminal_ids : list[str]
            対象のターミナル ID。
        """
        with self._lock:
            for terminal_id in terminal_ids:
                if terminal_id in self._targets:
                    self._targets[terminal_id] = ""

        if self._on_unavailable is None:
            return
        for terminal_id in terminal_ids:
            try:
                self._on_unavailable(terminal_id)
            except Exception:
                logger.exception(
                    "opencode2 到達不可の通知に失敗: terminal=%s", terminal_id
                )

    def _publish(
        self, terminal_id: str, children: dict, session_id: str
    ) -> None:
        """集約したステータスを、変化したときだけ通知する。"""
        status = aggregate_status(children)
        detail = format_detail(children)

        with self._lock:
            if terminal_id not in self._targets:
                # 通知の直前に監視解除された
                return
            self._children[terminal_id] = children
            if session_id:
                self._bound[terminal_id] = session_id
            else:
                self._bound.pop(terminal_id, None)
            if self._targets[terminal_id] == status:
                return
            self._targets[terminal_id] = status

        try:
            self._on_status(terminal_id, status, detail, children)
        except Exception:
            logger.exception(
                "opencode2 ステータス通知に失敗: terminal=%s", terminal_id
            )


# ---------------------------------------------------------------------------
# 状態の組み立て（純粋関数。テストしやすいようクラス外に置く）
# ---------------------------------------------------------------------------


def _normalize(path: str) -> str:
    """パスを比較用に正規化する。

    Windows の大文字小文字差と区切り文字の揺れを吸収する。

    Parameters
    ----------
    path : str
        比較したいパス。

    Returns
    -------
    str
        正規化したパス。
    """
    return os.path.normcase(os.path.normpath(path)) if path else ""


def _session_directory(session: dict) -> str:
    """セッション情報から作業ディレクトリを取り出す。

    v2 のレスポンスは ``location.directory``、旧形式は ``directory`` を持つ。

    Parameters
    ----------
    session : dict
        セッション情報。

    Returns
    -------
    str
        正規化した作業ディレクトリ。取れなければ空文字。
    """
    location = session.get("location")
    if isinstance(location, dict):
        directory = location.get("directory")
        if isinstance(directory, str) and directory:
            return _normalize(directory)
    directory = session.get("directory")
    if isinstance(directory, str) and directory:
        return _normalize(directory)
    return ""


def _session_updated_ms(session: dict) -> int:
    """セッションの最終更新時刻（epoch ミリ秒）を取り出す。

    Parameters
    ----------
    session : dict
        セッション情報。

    Returns
    -------
    int
        epoch ミリ秒。取れなければ 0。
    """
    time_info = session.get("time")
    if isinstance(time_info, dict):
        value = time_info.get("updated")
        if isinstance(value, (int, float)):
            return int(value)
    value = session.get("time_updated")
    if isinstance(value, (int, float)):
        return int(value)
    return 0


def _iso_to_ms(text: str) -> int:
    """ISO 8601 文字列を epoch ミリ秒へ変換する。

    Parameters
    ----------
    text : str
        ISO 8601 形式の日時文字列。

    Returns
    -------
    int
        epoch ミリ秒。変換できなければ 0（下限なしの扱い）。
    """
    if not isinstance(text, str) or not text:
        return 0
    try:
        normalized = text.replace("Z", "+00:00")
        return int(datetime.fromisoformat(normalized).timestamp() * 1000)
    except (TypeError, ValueError):
        return 0


def _infer_session_id(
    sessions: list,
    directory: str,
    started_at: str,
    claimed: set,
) -> str:
    """ターミナルに束縛するセッションを推定する。

    cwd が一致し、ゲート開放時刻以降に更新され、他のターミナルがまだ
    握っていないセッションのうち、最終更新が新しいものを選ぶ。

    Parameters
    ----------
    sessions : list
        ``/api/session`` の結果。
    directory : str
        対象ディレクトリ（正規化済み）。
    started_at : str
        ゲート開放時刻（ISO 8601）。空なら下限なし。
    claimed : set
        既に他のターミナルが握っているセッション ID。

    Returns
    -------
    str
        束縛するセッション ID。該当なしなら空文字。
    """
    lower_bound = _iso_to_ms(started_at)

    best_id = ""
    best_updated = -1
    for session in sessions:
        session_id = session.get("id")
        if not isinstance(session_id, str) or not session_id:
            continue
        if session_id in claimed:
            continue
        if _session_directory(session) != directory:
            continue
        updated = _session_updated_ms(session)
        if lower_bound and updated < lower_bound:
            continue
        if updated > best_updated:
            best_updated = updated
            best_id = session_id
    return best_id


def build_children_for_session(
    session_id: str,
    by_id: dict,
    active: dict,
    permissions: list,
    forms: list,
) -> dict:
    """束縛したセッション 1 件分の状態辞書を組み立てる。

    優先度は「権限待ち・質問待ち > 実行中 > エラー > アイドル」。
    権限待ちのセッションは同時に実行中としても報告されるため、
    待ち側を優先する。

    Parameters
    ----------
    session_id : str
        束縛したセッション ID。空なら空辞書を返す。
    by_id : dict
        セッション ID をキーとするセッション情報の辞書。
    active : dict
        ``/api/session/active`` の結果（セッション ID をキーとする辞書）。
    permissions : list
        ``/api/permission/request`` の結果。
    forms : list
        ``/api/form/request`` の結果。

    Returns
    -------
    dict
        セッション ID をキーとする ``{"title", "status"}`` の辞書。
    """
    if not session_id or session_id not in by_id:
        return {}

    session = by_id[session_id]
    waiting_ids = {
        item.get("sessionID")
        for item in list(permissions) + list(forms)
        if isinstance(item.get("sessionID"), str)
    }

    if session_id in waiting_ids:
        status = "waiting"
    elif session_id in active:
        status = "running"
    elif session.get("outcome") == "failed":
        status = "error"
    else:
        status = "idle"

    title = session.get("title")
    title = title if isinstance(title, str) else ""
    return {session_id: {"title": title, "status": status}}


def aggregate_status(children: dict) -> str:
    """セッション単位の状態を、ターミナル 1 個分のステータスへ集約する。

    1 ターミナル 1 セッションの運用では要素が 1 つだけになるが、
    将来の拡張に備えて集約の形は残してある。優先度は
    error > waiting > running > idle。

    Parameters
    ----------
    children : dict
        セッション ID をキーとする状態辞書。

    Returns
    -------
    str
        ``idle`` / ``running`` / ``waiting`` / ``error`` のいずれか。
    """
    best = "idle"
    for child in children.values():
        status = child.get("status", "idle")
        if _STATUS_PRIORITY.get(status, 0) > _STATUS_PRIORITY.get(best, 0):
            best = status
    return best


def format_detail(children: dict) -> str:
    """通知やログに載せる内訳文字列を組み立てる。

    1 ターミナル 1 セッションの運用では ``waiting 1`` のような 1 件だけの
    文字列になる。

    Parameters
    ----------
    children : dict
        セッション ID をキーとする状態辞書。

    Returns
    -------
    str
        内訳文字列。セッションが無ければ空文字。
    """
    if not children:
        return ""
    counts: dict[str, int] = {}
    for child in children.values():
        status = child.get("status", "idle")
        counts[status] = counts.get(status, 0) + 1
    order = ["error", "waiting", "running", "idle"]
    parts = [f"{name} {counts[name]}" for name in order if name in counts]
    return " / ".join(parts)
