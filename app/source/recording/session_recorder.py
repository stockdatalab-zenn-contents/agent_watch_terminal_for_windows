"""Session recording module for Agent Watch Terminal.

Records session events (status changes, screenshots, I/O) into a
timestamped directory for optional playback and post-mortem analysis.

Recording directory layout::

    {output_dir}/{YYYYMMDD_HHMMSS}/
    ├── events.json       ← array of event dicts
    └── screenshots/      ← PNG files
        ├── 0000_{session_id}_{status}.png
        └── 0001_{session_id}_{status}.png

Typical usage::

    recorder = SessionRecorder("app/data/recording", enabled=True)
    path = recorder.start_recording()
    recorder.record_event("abc123", "status_change", "running")
    fname = recorder.record_screenshot("abc123", "waiting", png_bytes)
    recorder.stop_recording()
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


class SessionRecorder:
    """セッションイベントを記録し、スクリーンショットを保存するクラス。

    Parameters
    ----------
    output_dir : str
        録画ディレクトリのベースパス (例: ``"app/data/recording"``)。
    enabled : bool, optional
        録画が有効かどうか。``False`` の場合、全記録メソッドは何もしない。
        デフォルトは ``False``。
    """

    def __init__(self, output_dir: str, enabled: bool = False) -> None:
        self._output_dir = output_dir
        self._enabled = enabled

        self._recording: bool = False
        self._session_dir: str | None = None
        self._screenshots_dir: str | None = None
        self._events: list[dict] = []
        self._screenshot_counter: int = 0

        logger.info(
            "SessionRecorder 初期化 — output_dir=%s, enabled=%s",
            output_dir,
            enabled,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start_recording(self) -> str:
        """新規録画セッションを開始する。

        タイムスタンプ名のディレクトリと ``screenshots/`` サブディレクトリ
        を作成し、内部状態をリセットする。

        Returns
        -------
        str
            作成した録画ディレクトリの絶対パス。
            録画が無効の場合は空文字列を返す。
        """
        if not self._enabled:
            logger.debug("録画が無効 — start_recording をスキップ")
            return ""

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._session_dir = str(
            Path(self._output_dir) / timestamp
        )
        self._screenshots_dir = str(
            Path(self._session_dir) / "screenshots"
        )

        os.makedirs(self._screenshots_dir, exist_ok=True)

        self._events = []
        self._screenshot_counter = 0
        self._recording = True

        logger.info("録画開始 — ディレクトリ=%s", self._session_dir)
        return self._session_dir

    def stop_recording(self) -> None:
        """録画を終了し、蓄積したイベントを events.json に書き出す。

        録画中でない場合は何もしない。
        """
        if not self._recording:
            logger.debug("録画中でない — stop_recording をスキップ")
            return

        events_path = str(Path(self._session_dir) / "events.json")  # type: ignore[arg-type]
        try:
            with open(events_path, "w", encoding="utf-8") as f:
                json.dump(self._events, f, indent=2, ensure_ascii=False)
                f.write("\n")
            logger.info(
                "イベントファイルを書き出し — path=%s, イベント数=%d",
                events_path,
                len(self._events),
            )
        except OSError as exc:
            logger.error("events.json の書き込みに失敗: %s", exc)

        self._recording = False
        logger.info("録画停止")

    def record_event(
        self,
        session_id: str,
        event_type: str,
        status: str,
        data: dict | None = None,
    ) -> None:
        """セッションイベントを記録する。

        Parameters
        ----------
        session_id : str
            対象セッションの識別子。
        event_type : str
            イベント種別 (例: ``"status_change"``, ``"io"``)。
        status : str
            現在のステータス (例: ``"running"``, ``"waiting"``)。
        data : dict | None, optional
            イベントに付随する任意データ。デフォルトは ``None``。
        """
        if not self._recording:
            return

        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "session_id": session_id,
            "event_type": event_type,
            "status": status,
            "data": data if data is not None else {},
        }
        self._events.append(event)

        logger.debug(
            "イベント記録 — session=%s, type=%s, status=%s",
            session_id,
            event_type,
            status,
        )

    def record_screenshot(
        self,
        session_id: str,
        status: str,
        image_data: bytes,
    ) -> str:
        """スクリーンショット PNG を保存する。

        Parameters
        ----------
        session_id : str
            対象セッションの識別子。
        status : str
            スクリーンショット取得時のステータス。
        image_data : bytes
            PNG 形式の画像バイナリデータ。

        Returns
        -------
        str
            保存したファイル名。録画中でない場合は空文字列を返す。
        """
        if not self._recording:
            return ""

        filename = f"{self._screenshot_counter:04d}_{session_id}_{status}.png"
        filepath = str(Path(self._screenshots_dir) / filename)  # type: ignore[arg-type]

        try:
            with open(filepath, "wb") as f:
                f.write(image_data)
            logger.debug("スクリーンショット保存 — %s", filename)
        except OSError as exc:
            logger.error("スクリーンショット保存に失敗: %s", exc)
            return ""

        self._screenshot_counter += 1
        return filename

    def is_recording(self) -> bool:
        """現在録画中かどうかを返す。

        Returns
        -------
        bool
            録画中であれば ``True``。
        """
        return self._recording
