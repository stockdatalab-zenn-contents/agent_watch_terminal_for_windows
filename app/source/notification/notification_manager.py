"""
notification_manager - 通知オーケストレーションモジュール

トースト通知・タスクバー点滅・未読バッジなど、全通知タイプの
発行を一元管理する。他モジュールは notify() を呼び出すだけでよく、
フォアグラウンド判定や通知種別の選択はこのクラスが内部で行う。
"""

import logging
from typing import Callable, Optional

from source.notification.toast_notifier import ToastNotifier
from source.notification.taskbar_flasher import TaskbarFlasher

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Status → notification message mapping
# ---------------------------------------------------------------------------

_TITLE = "Agent Watch Terminal"

_STATUS_MESSAGES: dict[str, str] = {
    "waiting": "許可待ち",
    "error": "エラー発生",
}

_DEFAULT_MESSAGE = "出力停止"


def _build_message(session_name: str, status: str) -> str:
    """ステータスに応じた通知メッセージを組み立てる。

    Parameters
    ----------
    session_name : str
        セッション表示名。
    status : str
        検出されたステータス文字列。

    Returns
    -------
    str
        ``[session_name] メッセージ`` 形式の通知本文。
    """
    body = _STATUS_MESSAGES.get(status, _DEFAULT_MESSAGE)
    return f"[{session_name}] {body}"


# ---------------------------------------------------------------------------
# NotificationManager
# ---------------------------------------------------------------------------


class NotificationManager:
    """全通知タイプの発行を一元管理するクラス。

    トースト通知 (``ToastNotifier``) とタスクバー点滅
    (``TaskbarFlasher``) の制御、およびコールバックによる
    UI 内部更新 (未読バッジ等) をまとめて提供する。

    Parameters
    ----------
    toast_notifier : ToastNotifier
        トースト通知の送出を担うインスタンス。
    taskbar_flasher : TaskbarFlasher
        タスクバー点滅の送出を担うインスタンス。
    toast_enabled : bool, optional
        トースト通知の有効フラグ (既定 True)。
    flash_enabled : bool, optional
        タスクバー点滅の有効フラグ (既定 True)。
    """

    def __init__(
        self,
        toast_notifier: ToastNotifier,
        taskbar_flasher: TaskbarFlasher,
        toast_enabled: bool = True,
        flash_enabled: bool = True,
    ) -> None:
        self._toast_notifier = toast_notifier
        self._taskbar_flasher = taskbar_flasher
        self._toast_enabled = toast_enabled
        self._flash_enabled = flash_enabled

        self._on_notify: Optional[Callable[[str, str, str], None]] = None
        self._foreground_checker: Optional[Callable[[], bool]] = None

    # ------ callback setters ------

    def set_on_notify(
        self, callback: Callable[[str, str, str], None]
    ) -> None:
        """内部 UI 更新用コールバックを設定する。

        コールバックは ``(session_id, session_name, status)`` の
        3 引数で呼び出される。未読バッジの更新等に利用する。

        Parameters
        ----------
        callback : Callable[[str, str, str], None]
            通知発生時に呼び出される関数。
        """
        self._on_notify = callback
        logger.debug("on_notify コールバックを設定")

    def set_foreground_checker(
        self, checker: Callable[[], bool]
    ) -> None:
        """フォアグラウンド判定関数を設定する。

        設定された関数が ``True`` を返す場合、外部通知
        (トースト・タスクバー点滅) を抑制する。

        Parameters
        ----------
        checker : Callable[[], bool]
            アプリがフォアグラウンドかどうかを返す関数。
        """
        self._foreground_checker = checker
        logger.debug("foreground_checker を設定")

    # ------ main entry point ------

    def notify(
        self,
        session_id: str,
        session_name: str,
        status: str,
        is_foreground: bool = False,
    ) -> None:
        """通知を発行する。

        ステータスに応じたメッセージを生成し、フォアグラウンド
        状態に応じて外部通知 (トースト・タスクバー点滅) の
        発行有無を判定する。``on_notify`` コールバックが設定
        されていれば、フォアグラウンド状態に関係なく常に呼び出す。

        Parameters
        ----------
        session_id : str
            対象セッションの ID。
        session_name : str
            対象セッションの表示名。
        status : str
            検出されたステータス
            (``"waiting"`` / ``"error"`` / その他)。
        is_foreground : bool, optional
            アプリがフォアグラウンドかどうか (既定 False)。
            ``set_foreground_checker`` が設定されている場合、
            そちらの戻り値で上書きされる。
        """
        # foreground_checker が設定されていればそちらを優先
        if self._foreground_checker is not None:
            is_foreground = self._foreground_checker()

        message = _build_message(session_name, status)

        logger.info(
            "通知: session_id=%s, status=%s, foreground=%s, message=%s",
            session_id,
            status,
            is_foreground,
            message,
        )

        # --- 外部通知 (非フォアグラウンド時のみ) ---
        if not is_foreground:
            if self._toast_enabled:
                try:
                    self._toast_notifier.show(_TITLE, message)
                    logger.debug("トースト通知を送出")
                except Exception:
                    logger.exception("トースト通知の送出に失敗")

            if self._flash_enabled:
                try:
                    self._taskbar_flasher.flash()
                    logger.debug("タスクバー点滅を送出")
                except Exception:
                    logger.exception("タスクバー点滅の送出に失敗")
        else:
            logger.debug("フォアグラウンドのため外部通知をスキップ")

        # --- 内部コールバック (常に呼び出す) ---
        if self._on_notify is not None:
            try:
                self._on_notify(session_id, session_name, status)
            except Exception:
                logger.exception("on_notify コールバックの実行に失敗")
