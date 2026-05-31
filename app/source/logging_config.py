"""Application-wide logging configuration for Agent Watch Terminal.

Provides setup_logging() to initialise root logger with file and console
handlers, and get_logger() as a thin wrapper around logging.getLogger().
"""

import logging
import os
from datetime import datetime

from source.security.log_masker import SecretMaskingFilter

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


class PywebviewNoiseFilter(logging.Filter):
    """pywebview が window.native を再帰列挙する際に発生する既知ノイズを抑制する.

    対象は WebView2 WinForms コントロールの自己参照プロパティ
    ``AccessibilityObject.Bounds.Empty.Empty...`` および
    ``ActiveControl.ModifierKeys.Add.Add...`` による
    ``maximum recursion depth exceeded`` 、UI スレッド外からの
    COM プロパティアクセスで発生する ``E_NOINTERFACE`` 等のエラー。
    これらはアプリ動作に影響しないためログから除外する。
    """

    _NOISE_SIGNATURES = (
        "AccessibilityObject",
        "ActiveControl",
        "ModifierKeys",
        "CoreWebView2Controller members can only be accessed",
        "CoreWebView2 can only be accessed from the UI thread",
    )

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        for sig in self._NOISE_SIGNATURES:
            if sig in message:
                return False
        return True


def setup_logging(log_dir: str, level: str = "INFO") -> logging.Logger:
    """Configure application-wide logging.

    Parameters
    ----------
    log_dir : str
        Directory where log files are stored.
        Created automatically if it does not exist.
    level : str, optional
        Logging level name (DEBUG / INFO / WARNING / ERROR / CRITICAL).
        Defaults to "INFO".

    Returns
    -------
    logging.Logger
        The configured root logger.
    """
    os.makedirs(log_dir, exist_ok=True)

    log_level = getattr(logging, level.upper(), logging.INFO)

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    # Avoid duplicate handlers when setup_logging is called multiple times
    root_logger.handlers.clear()

    formatter = logging.Formatter(LOG_FORMAT)
    masking_filter = SecretMaskingFilter()
    pywebview_noise_filter = PywebviewNoiseFilter()

    # pywebview logger にノイズ抑制フィルタを適用
    logging.getLogger("pywebview").addFilter(pywebview_noise_filter)

    # --- File handler (date-based log file) ---
    date_str = datetime.now().strftime("%Y%m%d")
    log_file = os.path.join(log_dir, f"{date_str}_agent_watch.log")

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(log_level)
    file_handler.setFormatter(formatter)
    file_handler.addFilter(masking_filter)
    root_logger.addHandler(file_handler)

    # --- Console handler (StreamHandler) ---
    console_handler = logging.StreamHandler()
    console_handler.setLevel(log_level)
    console_handler.setFormatter(formatter)
    console_handler.addFilter(masking_filter)
    root_logger.addHandler(console_handler)

    return root_logger


def setup_pty_output_logging(log_dir: str) -> logging.Logger:
    """PTY 生バイト出力専用ロガーを初期化する.

    メインログとは別ファイルに PTY 出力を記録するため、
    専用の名前付きロガー ``pty_output`` を構成する。
    ファイル名: ``{YYYYMMDD}_pty_output.log``

    Parameters
    ----------
    log_dir : str
        ログファイル保存先ディレクトリ。

    Returns
    -------
    logging.Logger
        PTY 出力専用の logger。
    """
    os.makedirs(log_dir, exist_ok=True)

    pty_logger = logging.getLogger("pty_output")
    pty_logger.setLevel(logging.DEBUG)
    pty_logger.propagate = False  # root logger への伝播を抑制

    # 重複ハンドラ防止
    pty_logger.handlers.clear()

    date_str = datetime.now().strftime("%Y%m%d")
    log_file = os.path.join(log_dir, f"{date_str}_pty_output.log")

    formatter = logging.Formatter(
        "%(asctime)s [%(name)s] session=%(session_id)s (%(session_name)s): %(message)s"
    )
    masking_filter = SecretMaskingFilter()

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    file_handler.addFilter(masking_filter)
    pty_logger.addHandler(file_handler)

    return pty_logger


def get_logger(name: str) -> logging.Logger:
    """Return a named logger.

    Parameters
    ----------
    name : str
        Logger name — typically __name__ of the calling module.

    Returns
    -------
    logging.Logger
        Logger instance bound to *name*.
    """
    return logging.getLogger(name)
