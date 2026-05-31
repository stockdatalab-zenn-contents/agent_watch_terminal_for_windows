"""
toast_notifier - OS トースト／バルーン通知モジュール

Windows では winotify、Linux では plyer をバックエンドとして使用し、
プラットフォームに応じたデスクトップ通知を表示する。
どちらも利用できない環境では警告ログを出力して静かに終了する。
"""

import logging
import platform
import threading
from typing import Callable

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Platform backend detection (import errors are handled gracefully)
# ---------------------------------------------------------------------------

_HAS_WINOTIFY = False
_HAS_PLYER = False

try:
    from winotify import Notification, audio as _winotify_audio  # type: ignore[import-untyped]

    _HAS_WINOTIFY = True
except ImportError:
    pass

if not _HAS_WINOTIFY:
    try:
        from plyer import notification as _plyer_notification  # type: ignore[import-untyped]

        _HAS_PLYER = True
    except ImportError:
        pass


class ToastNotifier:
    """OS トースト通知の送信クラス。

    Parameters
    ----------
    app_name : str
        通知に表示するアプリケーション名。
    on_click : Callable[[str], None] | None
        通知クリック時のコールバック。
        現時点では winotify の制約により未使用（後述の NOTE を参照）。
    """

    def __init__(
        self,
        app_name: str = "Agent Watch Terminal",
        on_click: Callable[[str], None] | None = None,
    ) -> None:
        self._app_name = app_name
        self._on_click = on_click
        self._lock = threading.Lock()
        self._platform = platform.system()

        if self._on_click is not None:
            # NOTE: winotify は toast.add_actions(label, launch_url) で
            # URL またはプロトコルスキームを起動できるが、
            # Python コールバックを直接バインドする機能は持たない。
            # クリックでセッションにフォーカスするには、
            # カスタム URI スキーム（例: agentwatch://focus/<session_id>）を
            # 登録し、アプリ側で受け取る仕組みが必要になる。
            logger.info(
                "on_click コールバックが指定されたが、"
                "現在のバックエンドでは直接利用できない"
            )

    # ------ public API ------

    def show(self, title: str, message: str, session_id: str = "") -> None:
        """トースト通知を表示する。

        バックグラウンドスレッドから呼び出されても安全に動作する。

        Parameters
        ----------
        title : str
            通知タイトル。
        message : str
            通知本文。
        session_id : str
            関連するセッション ID（将来のクリック連携用）。
        """
        with self._lock:
            self._dispatch(title, message, session_id)

    def is_available(self) -> bool:
        """トースト通知がこのプラットフォームで利用可能かどうかを返す。

        Returns
        -------
        bool
            winotify または plyer が使用可能なら ``True``。
        """
        return _HAS_WINOTIFY or _HAS_PLYER

    # ------ internal ------

    def _dispatch(self, title: str, message: str, session_id: str) -> None:
        """バックエンドに応じた通知送信の実処理。"""
        if _HAS_WINOTIFY:
            self._show_winotify(title, message, session_id)
        elif _HAS_PLYER:
            self._show_plyer(title, message)
        else:
            logger.warning(
                "トースト通知バックエンドが見つからない "
                "(winotify / plyer のいずれもインストールされていない)"
            )

    def _show_winotify(
        self, title: str, message: str, session_id: str
    ) -> None:
        """winotify による Windows トースト通知。"""
        try:
            toast = Notification(
                app_id=self._app_name,
                title=title,
                msg=message,
            )
            toast.set_audio(_winotify_audio.Default, loop=False)

            # NOTE: クリックでセッションにフォーカスする機能を実装する場合は、
            # toast.add_actions("Open", f"agentwatch://focus/{session_id}")
            # のようにカスタム URI スキームを登録し、
            # アプリ起動時に URI ハンドラを設定する必要がある。

            toast.show()
            logger.debug("winotify 通知を送信: title=%s", title)
        except Exception:
            logger.exception("winotify 通知の送信に失敗")

    def _show_plyer(self, title: str, message: str) -> None:
        """plyer による Linux トースト通知。"""
        try:
            _plyer_notification.notify(
                title=title,
                message=message,
                app_name=self._app_name,
                timeout=5,
            )
            logger.debug("plyer 通知を送信: title=%s", title)
        except Exception:
            logger.exception("plyer 通知の送信に失敗")
