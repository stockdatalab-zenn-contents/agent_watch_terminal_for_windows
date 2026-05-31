"""
settings_manager - settings.json の読み込み・参照・更新を行うモジュール

singleton 的にキャッシュした設定辞書を返し、
ドット記法によるネストアクセスと原子的ファイル書き込みを提供する。
"""

import json
import os
import tempfile
import threading

# ---------- module-level state ----------

_CONFIG_DIR = os.path.dirname(os.path.abspath(__file__))
_SETTINGS_PATH = os.path.join(_CONFIG_DIR, "settings.json")

_settings: dict | None = None
_lock = threading.Lock()


# ---------- internal helpers ----------

def _load() -> dict:
    """settings.json を読み込んで dict を返す。ファイルが無ければ空 dict。"""
    if not os.path.exists(_SETTINGS_PATH):
        return {}
    with open(_SETTINGS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _save(data: dict) -> None:
    """dict を settings.json へ原子的に書き込む (temp → rename)。"""
    fd, tmp_path = tempfile.mkstemp(
        dir=_CONFIG_DIR, suffix=".tmp", prefix=".settings_"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        # Windows では上書き rename に os.replace を使う
        os.replace(tmp_path, _SETTINGS_PATH)
    except BaseException:
        # 書き込み失敗時は一時ファイルを削除
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def _ensure_loaded() -> dict:
    """キャッシュが無ければ読み込み、キャッシュ済み dict を返す。"""
    global _settings
    if _settings is None:
        _settings = _load()
    return _settings


# ---------- public API ----------

def get_settings() -> dict:
    """設定辞書の全体を返す (singleton キャッシュ)。

    Returns:
        dict: settings.json の内容。
    """
    with _lock:
        return _ensure_loaded()


def get(key_path: str, default=None):
    """ドット記法のキーパスでネスト値を取得する。

    Args:
        key_path: ドット区切りのキー (例: "terminal.font_size")。
        default:  キーが見つからない場合の既定値。

    Returns:
        対応する値、または *default*。
    """
    with _lock:
        data = _ensure_loaded()

    current = data
    for key in key_path.split("."):
        if not isinstance(current, dict) or key not in current:
            return default
        current = current[key]
    return current


def update(key_path: str, value) -> None:
    """ドット記法のキーパスで値を更新し、ファイルへ保存する。

    中間キーが存在しない場合は dict を自動生成する。

    Args:
        key_path: ドット区切りのキー (例: "terminal.font_size")。
        value:    設定する値。
    """
    with _lock:
        data = _ensure_loaded()
        keys = key_path.split(".")
        current = data
        for key in keys[:-1]:
            if key not in current or not isinstance(current[key], dict):
                current[key] = {}
            current = current[key]
        current[keys[-1]] = value
        _save(data)


def reload() -> dict:
    """キャッシュを破棄してファイルから再読み込みする。

    Returns:
        dict: 再読み込みした設定辞書。
    """
    global _settings
    with _lock:
        _settings = _load()
        return _settings
