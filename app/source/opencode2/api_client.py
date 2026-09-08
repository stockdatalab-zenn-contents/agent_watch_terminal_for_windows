"""api_client - opencode2 のローカル HTTP API クライアント

opencode2 はバックグラウンドサーバー方式で、``http://127.0.0.1:<port>`` に
REST API を公開している。認証は HTTP Basic で、ユーザー名は固定文字列
``opencode``、パスワードは ``~/.config/opencode/service.json`` の ``password``。

外部ネットワークへは一切出ない。標準ライブラリのみで実装する
（``requests`` 等は .venv に無く、依存追加を避けるため）。
"""

import base64
import json
import logging
import os
import shutil
import subprocess
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------

# Basic 認証のユーザー名。opencode2 側で固定文字列として実装されている。
_BASIC_USER = "opencode"

# service.json の探索先（xdg-basedir 準拠、XDG_CONFIG_HOME 優先）
_XDG_CONFIG_HOME = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
    str(Path.home()), ".config"
)
_SERVICE_JSON_PATH = os.path.join(_XDG_CONFIG_HOME, "opencode", "service.json")

# サーバー URL を得るためのコマンド名。service.json に URL は書かれていない。
# Windows では opencode2 は .CMD シムであり、名前だけでは subprocess から
# 起動できない（FileNotFoundError になる）。shutil.which で実体を解決する。
_SERVICE_COMMAND = "opencode2"
_SERVICE_STATUS_ARGS = ["service", "status"]

# 外部コマンド・HTTP のタイムアウト（秒）
_CMD_TIMEOUT = 5.0
_HTTP_TIMEOUT = 5.0

# Windows でコンソールウィンドウを出さないためのフラグ
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class Opencode2ApiClient:
    """opencode2 のローカル API を叩くクライアント。

    ベース URL と認証情報はキャッシュし、失敗したときだけ再解決する。
    ネットワーク障害・サーバー停止は「取得不能」として扱い、例外は
    呼び出し元へ投げない（呼び出し元は None を見てフォールバックする）。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._base_url: str = ""
        self._password: str = ""

    # ------------------------------------------------------------------
    # 接続情報の解決
    # ------------------------------------------------------------------

    def _resolve_base_url(self) -> str:
        """``opencode2 service status`` を実行してベース URL を得る。

        Returns
        -------
        str
            ``http://127.0.0.1:49374`` 形式の URL。取得できなければ空文字。
        """
        exe = shutil.which(_SERVICE_COMMAND)
        if not exe:
            logger.debug("opencode2 が PATH に見つからない")
            return ""

        result = self._run_status(([exe] + _SERVICE_STATUS_ARGS))
        if result is None:
            # 一部の環境では .CMD を直接起動できない。cmd.exe 経由で再試行する。
            comspec = os.environ.get("COMSPEC", "cmd.exe")
            result = self._run_status(
                [comspec, "/c", exe] + _SERVICE_STATUS_ARGS
            )
        if result is None:
            return ""

        text = result.strip()
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("http://") or line.startswith("https://"):
                return line.rstrip("/")

        logger.debug("opencode2 service status が URL を返さない: %r", text[:200])
        return ""

    @staticmethod
    def _run_status(args: list[str]) -> str | None:
        """コマンドを実行し標準出力を返す。失敗時は None。

        Parameters
        ----------
        args : list[str]
            実行するコマンドと引数。

        Returns
        -------
        str | None
            標準出力。実行できなければ None。
        """
        try:
            result = subprocess.run(
                args,
                capture_output=True,
                timeout=_CMD_TIMEOUT,
                check=False,
                creationflags=_NO_WINDOW,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
            # opencode2 が未インストール、または応答しない。想定内。
            logger.debug("opencode2 service status の実行に失敗: %s", exc)
            return None
        except Exception:
            logger.exception("opencode2 service status で想定外のエラー")
            return None
        return (result.stdout or b"").decode("utf-8", errors="replace")

    def _load_password(self) -> str:
        """service.json からパスワードを読み込む。

        Returns
        -------
        str
            パスワード文字列。読めなければ空文字。
        """
        try:
            with open(_SERVICE_JSON_PATH, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            logger.debug(
                "service.json を読めない: %s — %s", _SERVICE_JSON_PATH, exc
            )
            return ""

        password = data.get("password") if isinstance(data, dict) else None
        return password if isinstance(password, str) else ""

    def _ensure_connection(self) -> tuple[str, str]:
        """ベース URL とパスワードを取得する（未解決なら解決する）。

        Returns
        -------
        tuple[str, str]
            ``(base_url, password)``。どちらかが空なら接続不可。
        """
        with self._lock:
            if not self._base_url:
                self._base_url = self._resolve_base_url()
            if not self._password:
                self._password = self._load_password()
            return self._base_url, self._password

    def invalidate(self) -> None:
        """キャッシュしたベース URL を破棄する。

        サーバーの再起動でポートが変わった場合に備え、通信失敗時に呼ぶ。
        パスワードは再生成されないためキャッシュを残す。
        """
        with self._lock:
            self._base_url = ""

    # ------------------------------------------------------------------
    # 低レベル GET
    # ------------------------------------------------------------------

    def get(self, path: str, params: dict | None = None) -> dict | list | None:
        """API を GET して JSON を返す。

        Parameters
        ----------
        path : str
            ``/api/session`` のような絶対パス。
        params : dict | None
            クエリパラメータ。値が dict の場合は ``key[subkey]=value``
            形式に展開する（opencode2 の ``location`` はオブジェクト）。

        Returns
        -------
        dict | list | None
            パースした JSON。取得できなければ None。
        """
        base_url, password = self._ensure_connection()
        if not base_url or not password:
            return None

        url = base_url + path
        query = _flatten_params(params or {})
        if query:
            url += "?" + urllib.parse.urlencode(query)

        token = base64.b64encode(
            f"{_BASIC_USER}:{password}".encode("utf-8")
        ).decode("ascii")
        request = urllib.request.Request(url, method="GET")
        request.add_header("Authorization", "Basic " + token)

        try:
            with urllib.request.urlopen(
                request, timeout=_HTTP_TIMEOUT
            ) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            # 401 は認証情報の不一致。サーバー再起動でパスワードが変わった
            # 可能性があるため、次回に読み直せるようキャッシュを捨てる。
            if exc.code == 401:
                with self._lock:
                    self._password = ""
            logger.debug("opencode2 API が %s を返す: %s", exc.code, url)
            return None
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            # サーバー停止・ポート変更。想定内なのでキャッシュを捨てて次回再解決。
            logger.debug("opencode2 API へ接続できない: %s — %s", url, exc)
            self.invalidate()
            return None
        except Exception:
            logger.exception("opencode2 API の呼び出しで想定外のエラー: %s", url)
            return None

        try:
            return json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            logger.warning("opencode2 API のレスポンスが JSON でない: %s — %s", url, exc)
            return None

    def post(self, path: str, body: dict | None = None) -> bool:
        """API へ POST する。

        レスポンス本文は使わないため、成否のみ返す。

        Parameters
        ----------
        path : str
            ``/api/session/xxx/rename`` のような絶対パス。
        body : dict | None
            JSON ボディ。

        Returns
        -------
        bool
            2xx が返れば True。
        """
        base_url, password = self._ensure_connection()
        if not base_url or not password:
            return False

        payload = json.dumps(body or {}).encode("utf-8")
        token = base64.b64encode(
            f"{_BASIC_USER}:{password}".encode("utf-8")
        ).decode("ascii")
        request = urllib.request.Request(
            base_url + path, data=payload, method="POST"
        )
        request.add_header("Authorization", "Basic " + token)
        request.add_header("Content-Type", "application/json")

        try:
            with urllib.request.urlopen(
                request, timeout=_HTTP_TIMEOUT
            ) as resp:
                return 200 <= resp.status < 300
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                with self._lock:
                    self._password = ""
            logger.debug(
                "opencode2 API への POST が %s を返す: %s", exc.code, path
            )
            return False
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            logger.debug("opencode2 API へ接続できない: %s — %s", path, exc)
            self.invalidate()
            return False
        except Exception:
            logger.exception("opencode2 API への POST で想定外のエラー: %s", path)
            return False

    # ------------------------------------------------------------------
    # 各エンドポイント
    # ------------------------------------------------------------------

    def list_sessions(self, directory: str = "") -> list[dict]:
        """セッション一覧を返す。

        各要素は ``id`` / ``title`` / ``location.directory`` / ``time`` /
        ``outcome`` などを持つ。

        Parameters
        ----------
        directory : str
            対象ディレクトリ。空なら既定のロケーション。

        Returns
        -------
        list[dict]
            セッション情報のリスト。取得できなければ空リスト。
        """
        params = {"location": {"directory": directory}} if directory else None
        payload = self.get("/api/session", params)
        return _extract_data_list(payload)

    def fetch_active(self) -> dict | None:
        """実行中セッションの map を返す。到達できなければ None。

        レスポンスは ``{"data": {"ses_xxx": {"type": "running"}}}`` 形式。
        ロケーションに依らず全セッションが対象。

        「サーバーへ到達できない」と「実行中が 0 件」は意味が違う。
        前者では状態を判断できないため、空辞書ではなく None を返して
        呼び出し元に区別させる。

        Returns
        -------
        dict | None
            セッション ID をキーとする辞書。到達できなければ None。
        """
        payload = self.get("/api/session/active")
        if not isinstance(payload, dict):
            return None
        data = payload.get("data")
        return data if isinstance(data, dict) else {}

    def list_active(self) -> dict:
        """実行中セッションの map を返す（到達できなければ空辞書）。

        Returns
        -------
        dict
            セッション ID をキーとする辞書。
        """
        return self.fetch_active() or {}

    def list_permission_requests(self, directory: str) -> list[dict]:
        """保留中の権限リクエスト一覧を返す。

        ``location`` を渡さないと既定のロケーション（ホーム）が使われ、
        別ディレクトリの権限待ちが取れないため、必ず directory を渡す。

        Parameters
        ----------
        directory : str
            対象ディレクトリ。

        Returns
        -------
        list[dict]
            各要素は ``id`` / ``sessionID`` / ``action`` / ``resources`` を持つ。
        """
        payload = self.get(
            "/api/permission/request", {"location": {"directory": directory}}
        )
        return _extract_data_list(payload)

    def list_form_requests(self, directory: str) -> list[dict]:
        """保留中のフォーム（質問待ち）一覧を返す。

        Parameters
        ----------
        directory : str
            対象ディレクトリ。

        Returns
        -------
        list[dict]
            フォーム情報のリスト。
        """
        payload = self.get(
            "/api/form/request", {"location": {"directory": directory}}
        )
        return _extract_data_list(payload)

    def rename_session(self, session_id: str, title: str) -> bool:
        """opencode2 側のセッションタイトルを変更する。

        キー送信ではなく API を呼ぶため、TUI の表示状態に依存しない。
        他の AI ツールのように「プロンプトにいるか」を気にする必要も、
        完了をポーリングで待つ必要もない。

        Parameters
        ----------
        session_id : str
            対象のセッション ID。
        title : str
            新しいタイトル。

        Returns
        -------
        bool
            成功なら True。
        """
        if not session_id or not title:
            return False
        quoted = urllib.parse.quote(session_id, safe="")
        return self.post(f"/api/session/{quoted}/rename", {"title": title})


# ---------------------------------------------------------------------------
# ヘルパー
# ---------------------------------------------------------------------------


def _flatten_params(params: dict) -> list[tuple[str, str]]:
    """ネストした dict を ``key[subkey]`` 形式のクエリへ展開する。

    opencode2 の ``location`` はオブジェクトを要求し、JSON 文字列では
    400 になる（実測）。``location[directory]=...`` の形にする必要がある。

    Parameters
    ----------
    params : dict
        クエリパラメータ。1 段のネストまで対応する。

    Returns
    -------
    list[tuple[str, str]]
        urlencode に渡せるタプルのリスト。
    """
    flat: list[tuple[str, str]] = []
    for key, value in params.items():
        if isinstance(value, dict):
            for sub_key, sub_value in value.items():
                if sub_value:
                    flat.append((f"{key}[{sub_key}]", str(sub_value)))
        elif value:
            flat.append((key, str(value)))
    return flat


def _extract_data_list(payload) -> list[dict]:
    """``{"data": [...]}`` 形式からリストを取り出す。

    Parameters
    ----------
    payload : Any
        API のレスポンス。

    Returns
    -------
    list[dict]
        dict 要素のみを含むリスト。取り出せなければ空リスト。
    """
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]
