"""Agent Watch Terminal - application entry point.

All components are instantiated here via dependency injection and wired
together before the pywebview GUI starts.  On exit every PTY session is
closed and state is persisted.
"""

import base64
import json
import logging
import os
import platform
import re
import subprocess
import sys
import threading
import time
from urllib.parse import unquote

import webview

# ---------------------------------------------------------------------------
# Package path — ensure ``app/`` is importable when run as a script
# ---------------------------------------------------------------------------

_APP_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_DIR = os.path.dirname(_APP_DIR)
if _PROJECT_DIR not in sys.path:
    sys.path.insert(0, _PROJECT_DIR)

# ---------------------------------------------------------------------------
# Internal imports
# ---------------------------------------------------------------------------

from source.config.settings_manager import get_settings, get  # noqa: E402
from source.logging_config import setup_logging, setup_pty_output_logging, get_logger  # noqa: E402
from source.pty.pty_manager import PtyManager  # noqa: E402
from source.session.session_store import SessionStore  # noqa: E402
from source.session.session_manager import SessionManager  # noqa: E402
from source.detection.agent_detector import AgentDetector  # noqa: E402
from source.detection.agent_patterns import (  # noqa: E402
    AGENTS,
    STATUS_SOURCE_API,
    get_close_tab_keys,
    get_resume_command,
    get_status_source,
)
from source.opencode2.status_poller import Opencode2StatusPoller  # noqa: E402
from source.notification.toast_notifier import ToastNotifier  # noqa: E402
from source.notification.taskbar_flasher import TaskbarFlasher  # noqa: E402
from source.notification.notification_manager import NotificationManager  # noqa: E402
from source.explorer.file_explorer import FileExplorer  # noqa: E402
from source.recording.session_recorder import SessionRecorder  # noqa: E402
from source.api import Api  # noqa: E402

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# pywebview window reference (set after window creation)
# ---------------------------------------------------------------------------

_window: webview.Window | None = None


# ---------------------------------------------------------------------------
# Foreground detection (Win32)
# ---------------------------------------------------------------------------

_foreground_detection_available = False

if platform.system() == "Windows":
    try:
        import ctypes
        import ctypes.wintypes

        _foreground_detection_available = True
    except ImportError:
        pass


def _is_foreground() -> bool:
    """アプリウィンドウがフォアグラウンドかどうかを判定する。

    Win32 API の GetForegroundWindow() と FindWindowW() を比較する。
    判定不能な場合は False を返し、通知を発火させる側に倒す。
    """
    if not _foreground_detection_available:
        return False

    try:
        user32 = ctypes.windll.user32  # type: ignore[union-attr]
        window_title = get("window.title", "Agent Watch Terminal")
        app_hwnd: int = user32.FindWindowW(None, window_title)
        if not app_hwnd:
            return False
        fg_hwnd: int = user32.GetForegroundWindow()
        return app_hwnd == fg_hwnd
    except Exception:
        logging.getLogger(__name__).debug("フォアグラウンド判定に失敗")
        return False


# ---------------------------------------------------------------------------
# Callbacks
# ---------------------------------------------------------------------------


def _on_pty_output(session_id: str, data: bytes) -> None:
    """PTY 出力データの統合コールバック.

    受信データを以下の順に処理する:
      1. agent_detector      -- AI エージェント状態の検出
      2. pty_output_logger   -- 生バイトのテキストログ記録
      3. session_recorder    -- 録画中であればデータ記録
      4. JS フロントエンド   -- window.evaluate_js() で出力転送
    """
    # 1. エージェント検出
    text = data.decode("utf-8", errors="replace")
    _agent_detector.feed(session_id, text)

    # 1.1. OSC 7 (cwd 変更通知) の検出
    _detect_osc7(session_id, text)

    # 2. PTY 生出力ログ
    if _pty_output_logger is not None:
        session = _session_manager.get_session(session_id)
        session_name = session["name"] if session else session_id
        _pty_output_logger.debug(
            "%s", text, extra={"session_id": session_id, "session_name": session_name}
        )

    # 3. 録画
    if _session_recorder.is_recording():
        _session_recorder.record_event(session_id, "io", "output", {"size": len(data)})

    # 4. フロントエンドへ転送
    if _window is not None:
        try:
            encoded = base64.b64encode(data).decode("ascii")
            _window.evaluate_js(
                f"window.onPtyOutput({json.dumps(session_id)}, {json.dumps(encoded)})"
            )
        except Exception:
            logger.debug("JS 転送に失敗 (ウィンドウが未準備の可能性)")


def _get_terminal_info(session_id: str) -> dict | None:
    """opencode2 の状態監視に必要なターミナル情報を返す。

    ポーラーはこの情報を使って、ターミナルと opencode2 のセッションを
    1 対 1 で束縛する。セッションが既に閉じられている場合は None を返し、
    ポーラー側で監視対象から自動的に外させる。

    Parameters
    ----------
    session_id : str
        awt のセッション（ターミナル）ID。

    Returns
    -------
    dict | None
        ``{"cwd", "session_id", "started_at"}``。
        ターミナルが存在しなければ None。
    """
    session = _session_manager.get_session(session_id)
    if session is None:
        return None

    cwd = session.get("cwd", "")
    bound_id = session.get("agent_session_id", "")
    started_at = session.get("agent_started_at", "")
    return {
        "cwd": cwd if isinstance(cwd, str) else "",
        "session_id": bound_id if isinstance(bound_id, str) else "",
        "started_at": started_at if isinstance(started_at, str) else "",
    }


def _push_opencode2_title(session_id: str, name: str) -> None:
    """awt のセッション名を opencode2 のタイトルへ反映する。

    opencode2 は他ツールと違い REST API でタイトルを変更できるため、
    終了時のキー送信を待たずに即座に揃えられる。失敗しても awt 側の
    名前は変わったままにする（表示の一貫性より確実性を優先しない）。

    Parameters
    ----------
    session_id : str
        awt のセッション（ターミナル）ID。
    name : str
        新しい名前。
    """
    if _opencode2_poller is None or not name:
        return
    try:
        if _agent_detector.get_agent(session_id) != "opencode2":
            return
        bound = _opencode2_poller.get_bound_session_id(session_id)
        if not bound:
            logger.debug(
                "opencode2 セッション未束縛のためタイトル反映を見送り: "
                "session=%s",
                session_id,
            )
            return
        ok = _opencode2_poller.rename_bound_session(session_id, name)
        logger.info(
            "opencode2 タイトル反映: session=%s, opencode2=%s, 結果=%s",
            session_id,
            bound,
            "成功" if ok else "失敗",
        )
    except Exception:
        logger.exception(
            "opencode2 タイトル反映に失敗: session=%s", session_id
        )


# AI ツールにキー列を送ってから PTY を閉じるまでの待ち時間（秒）。
# opencode2 は tabs.json への反映が即時（実測 0.6 秒以内）なので、
# 余裕を見てこの値とする。長くすると × の反応が鈍くなる。
_CLOSE_TAB_WAIT_SEC = 0.6


def _close_agent_tab(session_id: str) -> None:
    """セッション削除の直前に、AI ツール側のタブを閉じる。

    opencode2 のタブは cwd ごとに tabs.json へ永続化されるため、
    PTY を殺すだけではタブが残り、次回起動で復活して溜まり続ける。
    PTY がまだ生きているうちに session.tab.close のキー列を送る。

    キー列を持たないエージェント、および TUI が動いていない
    （シェルに戻っている）セッションでは何もしない。

    Parameters
    ----------
    session_id : str
        削除対象の awt セッション（ターミナル）ID。
    """
    try:
        agent_key = _agent_detector.get_agent(session_id)
    except Exception:
        logger.debug("エージェント判定に失敗: session=%s", session_id)
        return
    if not agent_key:
        return

    keys = get_close_tab_keys(agent_key)
    if not keys:
        return

    try:
        _pty_manager.write(session_id, keys)
    except Exception:
        logger.exception(
            "タブを閉じるキーの送出に失敗: session=%s, agent=%s",
            session_id,
            agent_key,
        )
        return

    logger.info(
        "タブを閉じるキーを送出: session=%s, agent=%s", session_id, agent_key
    )
    # AI ツール側が処理して状態を書き出すまで待つ。
    time.sleep(_CLOSE_TAB_WAIT_SEC)


def _on_opencode2_status(
    session_id: str, status: str, detail: str, children: dict
) -> None:
    """opencode2 のポーラーから受け取った状態を detector へ渡す。

    Parameters
    ----------
    session_id : str
        awt のセッション（ターミナル）ID。
    status : str
        集約したステータス。
    detail : str
        セッション内訳の文字列。
    children : dict
        opencode2 のセッション ID をキーとする状態辞書。
    """
    logger.debug(
        "opencode2 状態更新: session=%s, status=%s, 内訳=%s",
        session_id,
        status,
        children,
    )
    # 到達不可から復旧した場合に備え、通知のたびに外部供給源を有効化する
    _agent_detector.set_external_source_active(session_id, True)
    _agent_detector.set_external_status(session_id, status, detail)


def _on_opencode2_unavailable(session_id: str) -> None:
    """opencode2 のサーバーへ到達できない間、画面文言による判定へ戻す。

    Parameters
    ----------
    session_id : str
        awt のセッション（ターミナル）ID。
    """
    _agent_detector.set_external_source_active(session_id, False)


def _sync_opencode2_watch(
    session_id: str, agent_key: str | None, event_type: str | None
) -> None:
    """agent_key に応じて opencode2 の状態監視を開始・停止する。

    ``status_source`` が ``api`` のエージェントに切り替わったら監視を始め、
    ゲート閉鎖や別ツールへの乗り換えで止める。

    Parameters
    ----------
    session_id : str
        セッション ID。
    agent_key : str | None
        現在のエージェント識別子。
    event_type : str | None
        ステータス変化のイベント種別。
    """
    if _opencode2_poller is None:
        return

    use_api = (
        event_type != "gate_closed"
        and agent_key is not None
        and get_status_source(agent_key) == STATUS_SOURCE_API
    )

    try:
        if use_api:
            if not _opencode2_poller.is_watching(session_id):
                _agent_detector.set_external_source_active(session_id, True)
                _opencode2_poller.watch(session_id)
        elif _opencode2_poller.is_watching(session_id):
            _opencode2_poller.unwatch(session_id)
            _agent_detector.set_external_source_active(session_id, False)
    except Exception:
        logger.exception(
            "opencode2 状態監視の切り替えに失敗: session=%s", session_id
        )


def _on_status_change(
    session_id: str,
    status: str,
    agent: str | None,
    text: str,
    event_type: str | None = None,
    matched_pattern: str | None = None,
) -> None:
    """agent_detector がステータス変化を検出したときのコールバック.

    1. session_manager のステータスを更新
    2. 非アクティブセッションの場合は未読フラグを設定
    3. 録画中であればイベント記録
    4. JS フロントエンドへステータス変更を通知
    5. 通知発火
    6. PTY 出力ログにマーカー行を記録
    """
    # 1. SessionManager 更新
    # 「処理完了」の通知は running/waiting/error からの復帰に限る。
    # 外部供給源（opencode2 の API）経由では event_type が "external" に
    # なり、従来の ("shell_prompt", "debounce") の条件から漏れるため、
    # 更新前のステータスを控えて遷移で判定する。
    previous = _session_manager.get_session(session_id)
    prev_status = previous.get("status", "idle") if previous else "idle"
    _session_manager.update_status(session_id, status, agent)

    # 1.5. agent_key の追跡（永続化用）
    agent_key = _agent_detector.get_agent(session_id)
    _session_manager.set_agent_key(session_id, agent_key)
    # ゲート開放時刻を記録（終了時のセッション突合で下限として使う）。
    # ツール乗り換え（agent_switched）も新しいツールの開始とみなす。
    if event_type in ("gate_opened", "agent_switched"):
        _session_manager.set_agent_started_at(session_id)
    # ゲート閉鎖時は命名済みフラグと開放時刻をクリア
    if event_type == "gate_closed":
        _session_manager.set_agent_session_named(session_id, False)
        _session_manager.set_agent_started_at(session_id, "")
    # ツール乗り換え時は前のツールの命名済みフラグを引き継がない
    if event_type == "agent_switched":
        _session_manager.set_agent_session_named(session_id, False)

    # 1.6. opencode2 の状態監視（外部供給源）の開始・停止
    _sync_opencode2_watch(session_id, agent_key, event_type)

    # 2. 非アクティブセッションは未読バッジ
    active_id = _session_manager.get_active_session_id()
    if session_id != active_id:
        _session_manager.mark_unread(session_id)

    # 3. 録画
    if _session_recorder.is_recording():
        _session_recorder.record_event(
            session_id,
            "status_change",
            status,
            {"agent": agent, "text": text[:200], "event_type": event_type},
        )

    # 4. JS へ通知
    session = _session_manager.get_session(session_id)
    payload = {
        "session_id": session_id,
        "status": status,
        "agent": agent,
        "agent_name": agent,
        "name": session["name"] if session else session_id,
        "unread": session.get("unread", False) if session else False,
    }
    if _window is not None:
        try:
            _window.evaluate_js(
                f"window.onStatusChange({json.dumps(payload)})"
            )
        except Exception:
            logger.debug("JS ステータス通知に失敗")

    # 5. 通知発火
    session_name = session["name"] if session else session_id
    if status in ("waiting", "error"):
        _notification_manager.notify(session_id, session_name, status)
    elif status == "idle" and (
        event_type in ("shell_prompt", "debounce")
        or (
            event_type == "external"
            and prev_status in ("running", "waiting", "error")
        )
    ):
        # シェルプロンプト / デバウンス確定 / 外部供給源での
        # running・waiting・error → idle の遷移を「処理完了」として通知
        _notification_manager.notify(session_id, session_name, "completed")

    # 6. PTY 出力ログにステータス変更マーカーを記録
    if _pty_output_logger is not None:
        pattern_info = f" | pattern={matched_pattern!r}" if matched_pattern else ""
        text_info = f" | text={text[:200]!r}" if text else ""
        _pty_output_logger.info(
            ">>> STATUS: %s | event=%s%s%s",
            status,
            event_type,
            pattern_info,
            text_info,
            extra={"session_id": session_id, "session_name": session_name},
        )

    logger.info(
        "ステータス変更 — session=%s, status=%s, agent=%s, event_type=%s",
        session_id,
        status,
        agent,
        event_type,
    )


# ---------------------------------------------------------------------------
# Module-level component placeholders (populated in main())
# ---------------------------------------------------------------------------

_buffers_dir: str
_pty_manager: PtyManager
_session_store: SessionStore
_session_manager: SessionManager
_agent_detector: AgentDetector
# opencode2 のローカル API から状態を取得するポーラー。
# settings.json で無効化されている場合は None のまま。
_opencode2_poller: Opencode2StatusPoller | None = None
_toast_notifier: ToastNotifier
_taskbar_flasher: TaskbarFlasher
_notification_manager: NotificationManager
_file_explorer: FileExplorer
_session_recorder: SessionRecorder
_pty_output_logger: logging.Logger | None


# ---------------------------------------------------------------------------
# PTY 読み取り開始ヘルパー
# ---------------------------------------------------------------------------


_closing_in_progress = False
_BUFFER_SAVE_TIMEOUT = 10.0  # seconds
_RENAME_POLL_INTERVAL = 0.1  # seconds — ポーリング間隔
_RENAME_TIMEOUT = 5.0  # seconds — セッションあたりの rename 完了待ちタイムアウト

# OSC 7 シーケンスの正規表現パターン
# 形式: ESC ]7;file:///path BEL  または  ESC ]7;file://hostname/path ST
_OSC7_RE = re.compile(r"\x1b\]7;file://([^/]*)(.*?)(?:\x07|\x1b\\)")

# WSL の既定ディストリビューション名キャッシュ
# `wsl.exe -l -q` の出力をそのまま保持。失敗時は空文字。
# `_detect_osc7` で /home/... など WSL ファイルシステムのパスを
# `\\wsl$\<distro>\...` UNC に変換するために使用。
_WSL_DEFAULT_DISTRO: str | None = None


def _get_wsl_default_distro() -> str:
    """WSL の既定ディストリビューション名を返す（キャッシュ）.

    `wsl.exe -l -q` の先頭行を採用する。`-q` は名前のみ出力する
    フラグで、既定ディストリビューションが先頭に来る。
    Windows 以外、または WSL 未導入時は空文字を返す。
    """
    global _WSL_DEFAULT_DISTRO
    if _WSL_DEFAULT_DISTRO is not None:
        return _WSL_DEFAULT_DISTRO

    if platform.system() != "Windows":
        _WSL_DEFAULT_DISTRO = ""
        return _WSL_DEFAULT_DISTRO

    try:
        result = subprocess.run(
            ["wsl.exe", "-l", "-q"],
            capture_output=True,
            timeout=3.0,
            check=False,
        )
        # `wsl.exe -l` は UTF-16 LE で出力する仕様
        text = result.stdout.decode("utf-16-le", errors="replace")
        for line in text.splitlines():
            name = line.strip().replace("\x00", "")
            if name:
                _WSL_DEFAULT_DISTRO = name
                logger.info("WSL 既定ディストリビューション検出: %s", name)
                return _WSL_DEFAULT_DISTRO
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    except Exception:
        logger.exception("WSL ディストリビューション検出に失敗")

    _WSL_DEFAULT_DISTRO = ""
    return _WSL_DEFAULT_DISTRO


def _wsl_path_to_windows(linux_path: str) -> str:
    """WSL Linux 形式の絶対パスを Windows でアクセス可能なパスに変換する.

    変換規則:
      - ``/mnt/<drive>/...``        → ``<DRIVE>:\\...``
      - ``/<その他絶対パス>``       → ``\\\\wsl$\\<distro>\\<その他>``

    distro 名が取得できない場合は変換不能と判定し、入力をそのまま返す
    （呼び出し側の `os.path.isdir` で False となり cwd 更新がスキップされる）。
    """
    if not linux_path.startswith("/"):
        return linux_path

    # /mnt/<drive>/path → <DRIVE>:\path
    mnt_match = re.match(r"^/mnt/([a-zA-Z])(/.*)?$", linux_path)
    if mnt_match:
        drive = mnt_match.group(1).upper()
        rest = mnt_match.group(2) or ""
        return f"{drive}:" + rest.replace("/", "\\")

    # /home/... 等 → \\wsl$\<distro>\...
    distro = _get_wsl_default_distro()
    if not distro:
        return linux_path

    return f"\\\\wsl$\\{distro}" + linux_path.replace("/", "\\")



def _on_closing() -> bool | None:
    """Two-phase close でウィンドウクローズ時のデッドロックを回避する.

    closing イベントハンドラから evaluate_js() を同期呼び出しすると、
    GUI スレッドが自身の完了を待つデッドロックが発生する。
    そこで 1 回目はクローズをキャンセルし、別スレッドでバッファを保存
    してから _window.destroy() で改めてウィンドウを閉じる。

    1 回目: クローズをキャンセル → 別スレッドでバッファ保存開始
    2 回目 (_window.destroy() 由来): そのまま通過してウィンドウを閉じる
    """
    global _closing_in_progress
    if _closing_in_progress:
        return  # 2 回目 — そのまま閉じる
    _closing_in_progress = True
    threading.Thread(target=_save_buffers_and_destroy, daemon=True).start()
    return False  # 1 回目 — クローズをキャンセル


def _save_buffers_and_destroy() -> None:
    """別スレッドでターミナルバッファを保存し、ウィンドウを破棄する.

    evaluate_js() は GUI スレッドが解放された状態で呼び出されるため
    デッドロックしない。タイムアウト内に完了しなければスキップして破棄する。

    手順:
      1. シャットダウンオーバーレイ表示
      2. ターミナルバッファ保存
      3. AI ツール命名コマンド送信 + 完了ポーリング
      4. セッション状態保存（agent_key + agent_session_named）
      5. ウィンドウ破棄
    """
    done = threading.Event()

    def _force_destroy() -> None:
        if not done.is_set():
            logger.warning("バッファ保存がタイムアウト — ウィンドウを強制破棄")
            try:
                _window.destroy()
            except Exception:
                pass

    timer = threading.Timer(_BUFFER_SAVE_TIMEOUT, _force_destroy)
    timer.daemon = True
    timer.start()

    try:
        # 1. シャットダウンオーバーレイ表示
        if _window is not None:
            _window.evaluate_js(
                "document.getElementById('shutdown-overlay')"
                ".classList.remove('hidden')"
            )

        # 2. ターミナルバッファ保存
        if _window is not None:
            json_str = _window.evaluate_js(
                "JSON.stringify(TerminalManager.serializeAll())"
            )
            if json_str:
                buffers = json.loads(json_str)
                os.makedirs(_buffers_dir, exist_ok=True)
                for sid, content in buffers.items():
                    path = os.path.join(_buffers_dir, f"{sid}.txt")
                    with open(path, "w", encoding="utf-8") as f:
                        f.write(content)
                logger.info("ターミナルバッファ保存完了: %d 件", len(buffers))

        # 3. AI ツール命名コマンド送信 + 完了ポーリング
        _send_rename_commands_on_shutdown()

        # 4. セッション状態保存（agent_key + agent_session_named 含む）
        _session_manager.save_state()
        logger.info("セッション状態保存完了（agent_key 含む）")

    except Exception:
        logger.exception("シャットダウン処理に失敗")
    finally:
        done.set()
        timer.cancel()
        if _window is not None:
            try:
                _window.destroy()
            except Exception:
                logger.debug("ウィンドウ破棄に失敗 (既に破棄済みの可能性)")


def _send_rename_commands_on_shutdown() -> None:
    """終了時に未命名セッションの AI ツール命名コマンドを送信し、完了を待つ.

    agent_session_named が既に True のセッション（前回送信済み）は
    スキップする。未命名セッションのうち、AI ツールがプロンプトにいる
    セッションのみ命名コマンドを送信し、プロンプト復帰をポーリングで待つ。
    """
    # rename 送信対象を収集
    sent_sessions: list[str] = []

    for session in _session_manager.get_sessions():
        sid = session["id"]
        agent_key = session.get("agent_key")
        if not agent_key:
            continue

        # 前回送信済み → スキップ
        if session.get("agent_session_named"):
            logger.debug(
                "終了時 rename スキップ (命名済み): session=%s", sid
            )
            continue

        agent_info = AGENTS.get(agent_key)
        if not agent_info:
            continue

        # rename コマンドを持たない AI ツール（opencode 等）は命名手順が不要。
        # 命名済み扱いにしないと次回起動時の自動復元がスキップされるため、
        # ここでフラグを立てて送信対象から外す。
        if not agent_info.get("rename_command"):
            _session_manager.set_agent_session_named(sid, True)
            logger.debug(
                "終了時 rename 不要 (rename 非対応): session=%s, agent=%s",
                sid,
                agent_key,
            )
            continue

        # AI ツールがプロンプトにいる場合のみ送信
        if _agent_detector.is_agent_at_prompt(sid):
            name = session["name"]
            cmd = agent_info["rename_command"].format(name=name)
            try:
                _pty_manager.write(sid, f"{cmd}\r")
                sent_sessions.append(sid)
                logger.info(
                    "終了時 rename 送信: session=%s, cmd=%r", sid, cmd
                )
            except Exception:
                logger.exception(
                    "終了時 rename 送信に失敗: session=%s", sid
                )
                _session_manager.set_agent_session_named(sid, False)
        else:
            logger.info(
                "終了時 rename スキップ "
                "(AI ツールがプロンプトにいない): session=%s",
                sid,
            )

    # 送信したセッションの完了をポーリングで待つ
    if sent_sessions:
        _wait_rename_completion(sent_sessions)


def _wait_rename_completion(session_ids: list[str]) -> None:
    """rename コマンド送信済みセッションのプロンプト復帰を待つ.

    各セッションについて is_agent_at_prompt() が True に戻るまで
    ポーリングする。タイムアウト超過時はログを出して次へ進む。
    """
    deadline = time.time() + _RENAME_TIMEOUT

    pending = set(session_ids)
    while pending and time.time() < deadline:
        time.sleep(_RENAME_POLL_INTERVAL)
        for sid in list(pending):
            if _agent_detector.is_agent_at_prompt(sid):
                _session_manager.set_agent_session_named(sid, True)
                pending.discard(sid)
                logger.info(
                    "終了時 rename 完了確認: session=%s", sid
                )

    # タイムアウトしたセッション
    for sid in pending:
        logger.warning(
            "終了時 rename 完了待ちタイムアウト: session=%s", sid
        )
        _session_manager.set_agent_session_named(sid, False)


def _send_pending_command(session_id: str, command: str) -> None:
    """自動復元用コマンドを PTY に送信する.

    Parameters
    ----------
    session_id : str
        送信先セッション ID。
    command : str
        送信するコマンド文字列（末尾の改行含む）。
    """
    try:
        _pty_manager.write(session_id, command)
        logger.info(
            "自動復元コマンド送信: session=%s, cmd=%r", session_id, command
        )
    except Exception:
        logger.exception(
            "自動復元コマンド送信に失敗: session=%s", session_id
        )


def _detect_osc7(session_id: str, text: str) -> None:
    """PTY 出力から OSC 7 シーケンスを検出し、cwd を更新する.

    OSC 7 形式: ESC ]7;file:///path BEL
    シェルの prompt 関数が毎回この形式で cwd を出力する。
    検出時に session_manager.update_cwd() を呼び出し、
    アクティブセッションならフロントエンドへ通知する。

    Windows / PowerShell: ``/C:/Users/...`` 形式 → ``C:\\Users\\...``
    Windows / WSL bash:   ``/mnt/c/Users/...`` 形式 → ``C:\\Users\\...``
                           ``/home/user/...``       → ``\\\\wsl$\\<distro>\\home\\user\\...``
    """
    match = _OSC7_RE.search(text)
    if not match:
        return

    raw_path = unquote(match.group(2))
    is_windows = platform.system() == "Windows"

    if is_windows:
        # PowerShell prompt 関数由来: /C:/Users/... → C:/Users/...
        if len(raw_path) > 2 and raw_path[0] == "/" and raw_path[2] == ":":
            native_path = raw_path[1:].replace("/", os.sep)
        else:
            # WSL bash 由来の Linux 絶対パスを Windows 形式へ変換
            native_path = _wsl_path_to_windows(raw_path)
            # `_wsl_path_to_windows` が変換できなかった場合（Linux 絶対パス
            # のまま）はそのまま os.sep 置換しても無効パスにしかならない
            if native_path == raw_path and raw_path.startswith("/"):
                # distro 不明等の理由で変換不能 — cwd 更新スキップ
                logger.debug(
                    "OSC 7: WSL パス変換失敗（distro 不明）: %s", raw_path
                )
                return
    else:
        # Linux ネイティブ: そのまま使用
        native_path = raw_path

    if not os.path.isdir(native_path):
        logger.debug("OSC 7: 存在しないディレクトリ — スキップ: %s", native_path)
        return

    session = _session_manager.get_session(session_id)
    if session is None:
        return

    old_cwd = session.get("cwd", "")
    if old_cwd == native_path:
        return

    _session_manager.update_cwd(session_id, native_path)

    # アクティブセッションならフロントエンドへ通知
    active_id = _session_manager.get_active_session_id()
    if session_id == active_id and _window is not None:
        try:
            _window.evaluate_js(
                f"window.onCwdChange({json.dumps(session_id)}, {json.dumps(native_path)})"
            )
        except Exception:
            logger.debug("cwd 変更通知に失敗")


def _start_reading_all() -> None:
    """全セッションに対して PTY 読み取りスレッドを開始する."""
    for session in _session_manager.get_sessions():
        sid = session["id"]
        try:
            _pty_manager.start_reading(sid, _on_pty_output)
            logger.info("PTY 読み取り開始 — session=%s", sid)
        except RuntimeError:
            # 既に読み取り中の場合はスキップ
            logger.debug("PTY 読み取り済み — session=%s", sid)
        except Exception:
            logger.exception("PTY 読み取り開始に失敗 — session=%s", sid)


def _schedule_auto_resume() -> None:
    """保存済み agent_key を持つセッションの自動復元をスケジュールする.

    agent_session_named=True のセッションに対して、
    agent_key に応じた resume コマンドを直接シェルに送信する。
    復元方式はエージェント定義の ``session_match`` に従う。
    - name ベース (claude/copilot): 例 ``claude --resume "name"``
    - ID ベース (codex/bob/opencode/opencode2): 例 ``codex resume <id>``

    ID ベースで ``agent_session_id`` が未取得の場合は、
    ``resume_command_fallback`` が定義されていればそちらへ倒す
    （opencode: ``opencode --continue``、opencode2: ``opencode2 --continue``）。
    """
    for session in _session_manager.get_sessions():
        sid = session.get("id", "")
        # 1 セッションの準備失敗で、以降のタブの自動復元まで止めないよう
        # ループ本体全体を保護する（設定値の欠落・タイマー生成の失敗など）。
        try:
            agent_key = session.get("agent_key")
            if not agent_key:
                continue

            if agent_key not in AGENTS:
                continue

            if not session.get("agent_session_named"):
                logger.info(
                    "自動復元スキップ (命名未完了): session=%s, agent=%s",
                    sid,
                    agent_key,
                )
                continue

            # 復元コマンドを構築する。ID ベース復元のエージェントで
            # agent_session_id が未取得の場合は、定義されていれば
            # resume_command_fallback（opencode なら --continue）へ倒す。
            agent_session_id = session.get("agent_session_id", "")
            resume_cmd = get_resume_command(
                agent_key, session.get("name", ""), agent_session_id
            )

            if not resume_cmd:
                logger.warning(
                    "自動復元スキップ (復元コマンドを構築できない): "
                    "session=%s, agent=%s, agent_session_id=%r",
                    sid,
                    agent_key,
                    agent_session_id,
                )
                continue

            # 前回起動時の値が下限として残らないよう、この起動の時刻へ更新する。
            # ゲート開放時にも設定されるが、開放前にアプリが落ちた場合の保険。
            _session_manager.set_agent_started_at(sid)

            # シェル起動待ち後に resume コマンドを直接送信
            timer = threading.Timer(
                3.0, _send_pending_command, args=(sid, f"{resume_cmd}\r")
            )
            timer.daemon = True
            timer.start()
            logger.info(
                "自動復元スケジュール: session=%s, agent=%s, cmd=%r",
                sid,
                agent_key,
                resume_cmd,
            )
        except Exception:
            logger.exception(
                "自動復元の準備に失敗（このセッションのみスキップ）: "
                "session=%s",
                sid,
            )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    """アプリケーションのメインエントリポイント."""
    global _window
    global _pty_manager, _session_store, _session_manager
    global _agent_detector
    global _opencode2_poller
    global _toast_notifier, _taskbar_flasher, _notification_manager
    global _file_explorer, _session_recorder
    global _pty_output_logger
    global _buffers_dir

    # ---- settings ----
    settings = get_settings()

    # ---- logging ----
    log_dir = os.path.join(_APP_DIR, "logs")
    log_level = get("logging.level", "INFO")
    setup_logging(log_dir, level=log_level)

    # ---- PTY output logging ----
    if get("logging.pty_output_log", False):
        _pty_output_logger = setup_pty_output_logging(log_dir)
        logger.info("PTY 出力ログ有効 — app/logs/YYYYMMDD_pty_output.log")
    else:
        _pty_output_logger = None

    logger.info("Agent Watch Terminal 起動")

    # ---- data directory ----
    data_dir = os.path.join(_APP_DIR, "data")
    os.makedirs(data_dir, exist_ok=True)
    _buffers_dir = os.path.join(data_dir, "buffers")

    # ---- notification settings ----
    toast_enabled = get("notification.toast_enabled", True)
    flash_enabled = get("notification.taskbar_flash_enabled", True)
    recording_enabled = get("logging.session_recording", False)

    # ---- component instantiation (DI) ----
    _pty_manager = PtyManager()
    _session_store = SessionStore(os.path.join(data_dir, "sessions.json"))
    _session_manager = SessionManager(
        _session_store,
        _pty_manager,
        default_cwd=_PROJECT_DIR,
    )

    _toast_notifier = ToastNotifier()
    _taskbar_flasher = TaskbarFlasher(
        window_title=get("window.title", "Agent Watch Terminal"),
    )
    _notification_manager = NotificationManager(
        toast_notifier=_toast_notifier,
        taskbar_flasher=_taskbar_flasher,
        toast_enabled=toast_enabled,
        flash_enabled=flash_enabled,
    )
    _notification_manager.set_foreground_checker(_is_foreground)

    _agent_detector = AgentDetector(
        on_status_change=lambda sid, status, agent, text, event_type=None, matched_pattern=None: _on_status_change(
            sid, status, agent, text, event_type, matched_pattern
        ),
        debounce_ms=get("notification.debounce_ms", 3000),
        running_threshold_ms=get("notification.running_threshold_ms", 3000),
        waiting_recovery_threshold_ms=get("notification.waiting_recovery_threshold_ms", 1500),
        error_recovery_threshold_ms=get("notification.error_recovery_threshold_ms", 1500),
        ctrlc_window_ms=get("notification.ctrlc_window_ms", 1000),
    )

    if get("opencode2.api_status_enabled", True):
        _opencode2_poller = Opencode2StatusPoller(
            on_status=_on_opencode2_status,
            on_unavailable=_on_opencode2_unavailable,
            get_terminal_info=_get_terminal_info,
            interval_sec=get("opencode2.poll_interval_ms", 750) / 1000.0,
        )
        logger.info("opencode2 のローカル API による状態取得を有効化")
    else:
        logger.info("opencode2 のローカル API による状態取得は無効")

    _file_explorer = FileExplorer()
    _session_recorder = SessionRecorder(
        os.path.join(data_dir, "recording"),
        enabled=recording_enabled,
    )

    # ---- session initialisation ----
    _session_manager.initialize()
    logger.info("セッション初期化完了")

    # ---- PTY output forwarding ----
    _start_reading_all()

    # ---- auto-resume: AI ツールの自動復元 ----
    _schedule_auto_resume()

    # ---- Api ----
    api = Api(
        session_manager=_session_manager,
        pty_manager=_pty_manager,
        notification_manager=_notification_manager,
        file_explorer=_file_explorer,
        session_recorder=_session_recorder,
        agent_detector=_agent_detector,
        on_pty_output=_on_pty_output,
        buffers_dir=_buffers_dir,
        on_rename=_push_opencode2_title,
        on_remove=_close_agent_tab,
    )

    # ---- pywebview window ----
    window_title = get("window.title", "Agent Watch Terminal")
    window_width = get("window.width", 1000)
    window_height = get("window.height", 600)

    frontend_dir = os.path.join(_APP_DIR, "frontend")
    _window = webview.create_window(
        title=window_title,
        url=os.path.join(frontend_dir, "index.html"),
        js_api=api,
        width=window_width,
        height=window_height,
        min_size=(600, 400),
        # 既定 False だと pywebview が body に user-select:none を注入し、
        # Markdown プレビュー等でマウスによる文字列選択ができなくなる。
        text_select=True,
    )
    _window.events.closing += _on_closing

    logger.info(
        "pywebview ウィンドウ作成 — title=%s, size=%dx%d",
        window_title,
        window_width,
        window_height,
    )

    # ---- start GUI (blocks until window is closed) ----
    # debug=True は pywebview が window.native を再帰列挙するため
    # AccessibilityObject 再帰深度超過と WebView2 COM スレッド例外が発生する。
    # 既定は False とし、必要時のみ settings.json で有効化。
    window_debug = get("window.debug", False)
    webview.start(debug=window_debug)

    # ---- shutdown ----
    logger.info("アプリケーション終了処理を開始")
    if _opencode2_poller is not None:
        _opencode2_poller.stop_all()
    _session_manager.close_all()
    logger.info("Agent Watch Terminal 終了")


# ---------------------------------------------------------------------------
# Script entry
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    main()
