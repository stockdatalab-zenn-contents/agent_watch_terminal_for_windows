"""
session_id_collector - codex/bob/opencode のセッション情報を収集するモジュール

アプリ終了時に各 AI ツールのローカルデータから
セッション ID を収集し、session_ids.json として出力する。
次回起動時の resume コマンドに使用する。
"""

import glob
import json
import logging
import os
import re
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

logger = logging.getLogger(__name__)

# codex データディレクトリのベースパス
_CODEX_BASE_DIR = os.path.join(str(Path.home()), ".codex")

# bob データディレクトリのベースパス
_BOB_BASE_DIR = os.path.join(str(Path.home()), ".bob")

# opencode データディレクトリのベースパス（xdg-basedir 準拠、XDG_DATA_HOME 優先）
_OPENCODE_XDG_DATA_HOME = os.environ.get("XDG_DATA_HOME") or os.path.join(
    str(Path.home()), ".local", "share"
)
_OPENCODE_BASE_DIR = os.path.join(_OPENCODE_XDG_DATA_HOME, "opencode")

# bob の /chat save コマンドの正規表現
_CHAT_SAVE_RE = re.compile(r'^/chat save "(.+)"$')

# bob の checkpoint ファイルから cwd を抽出する正規表現
_CWD_RE = re.compile(
    r"I'm currently working in the directory: ([^\n]+)"
)


# ---------------------------------------------------------------------------
# codex セッション情報の収集
# ---------------------------------------------------------------------------


def collect_codex_sessions() -> list[dict]:
    """codex のセッション情報を収集する。

    ~/.codex/session_index.jsonl を読み込み、
    各セッションの cwd を sessions/ 配下の .jsonl ファイルから取得する。

    Returns
    -------
    list[dict]
        codex セッション情報のリスト。各要素は
        ``{"agent_key", "name", "agent_session_id", "cwd", "updated_at"}``。
    """
    index_path = os.path.join(_CODEX_BASE_DIR, "session_index.jsonl")

    if not os.path.exists(index_path):
        logger.debug("codex session_index.jsonl が見つからない: %s", index_path)
        return []

    entries: list[dict] = []

    try:
        with open(index_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("codex session_index.jsonl の行パースに失敗: %s", line[:80])
                    continue

                session_id = row.get("id", "")
                thread_name = row.get("thread_name", "")
                updated_at = row.get("updated_at", "")

                # sessions/ 配下から cwd を取得
                cwd = _find_codex_session_cwd(session_id)

                entries.append({
                    "agent_key": "codex",
                    "name": thread_name,
                    "agent_session_id": session_id,
                    "cwd": cwd,
                    "updated_at": updated_at,
                })

    except OSError as exc:
        logger.warning("codex session_index.jsonl の読み込みに失敗: %s", exc)

    logger.info("codex セッション情報を収集: %d 件", len(entries))
    return entries


def _find_codex_session_cwd(session_id: str) -> str:
    """codex セッションファイルから cwd を取得する。

    sessions/ 配下のファイル名に session_id が含まれる .jsonl を検索し、
    1 行目の payload.cwd を返す。

    Parameters
    ----------
    session_id : str
        codex セッション ID。

    Returns
    -------
    str
        作業ディレクトリパス。取得できない場合は空文字。
    """
    sessions_dir = os.path.join(_CODEX_BASE_DIR, "sessions")
    if not os.path.isdir(sessions_dir):
        return ""

    # sessions/yyyy/mm/dd/*.jsonl を再帰検索
    for root, _dirs, files in os.walk(sessions_dir):
        for fname in files:
            if session_id in fname and fname.endswith(".jsonl"):
                fpath = os.path.join(root, fname)
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        first_line = f.readline().strip()
                    if first_line:
                        row = json.loads(first_line)
                        payload = row.get("payload", {})
                        return payload.get("cwd", "")
                except (json.JSONDecodeError, OSError) as exc:
                    logger.warning(
                        "codex セッションファイルの読み込みに失敗: %s — %s",
                        fpath, exc,
                    )
                return ""

    return ""


# ---------------------------------------------------------------------------
# bob セッション情報の収集
# ---------------------------------------------------------------------------


def collect_bob_sessions() -> list[dict]:
    """bob のセッション情報を収集する。

    ~/.bob/tmp/ 配下の各サブディレクトリの logs.json を読み込み、
    最後の ``/chat save`` コマンドからセッション情報を取得する。

    Returns
    -------
    list[dict]
        bob セッション情報のリスト。各要素は
        ``{"agent_key", "name", "agent_session_id", "cwd", "updated_at"}``。
    """
    tmp_dir = os.path.join(_BOB_BASE_DIR, "tmp")

    if not os.path.isdir(tmp_dir):
        logger.debug("bob tmp ディレクトリが見つからない: %s", tmp_dir)
        return []

    entries: list[dict] = []

    try:
        for subdir_name in os.listdir(tmp_dir):
            subdir_path = os.path.join(tmp_dir, subdir_name)
            if not os.path.isdir(subdir_path):
                continue

            entry = _collect_bob_session_from_dir(subdir_path)
            if entry:
                entries.append(entry)
    except OSError as exc:
        logger.warning("bob tmp ディレクトリの読み込みに失敗: %s", exc)

    logger.info("bob セッション情報を収集: %d 件", len(entries))
    return entries


def _collect_bob_session_from_dir(dir_path: str) -> dict | None:
    """bob の1サブディレクトリからセッション情報を収集する。

    Parameters
    ----------
    dir_path : str
        ~/.bob/tmp/<subdir> のパス。

    Returns
    -------
    dict | None
        セッション情報。``/chat save`` が見つからない場合は ``None``。
    """
    logs_path = os.path.join(dir_path, "logs.json")

    if not os.path.exists(logs_path):
        return None

    try:
        with open(logs_path, "r", encoding="utf-8") as f:
            logs = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("bob logs.json の読み込みに失敗: %s — %s", logs_path, exc)
        return None

    if not isinstance(logs, list) or not logs:
        return None

    # 最後の /chat save エントリを逆順検索
    name = ""
    session_id = ""
    for entry in reversed(logs):
        message = entry.get("message", "")
        match = _CHAT_SAVE_RE.match(message)
        if match:
            name = match.group(1)
            session_id = entry.get("sessionId", "")
            break

    if not name:
        return None

    # updated_at: 配列の最後のエントリの timestamp
    updated_at = logs[-1].get("timestamp", "")

    # cwd: checkpoint ファイルから取得
    cwd = _find_bob_cwd(dir_path)

    return {
        "agent_key": "bob",
        "name": name,
        "agent_session_id": session_id,
        "cwd": cwd,
        "updated_at": updated_at,
    }


def _find_bob_cwd(dir_path: str) -> str:
    """bob の checkpoint ファイルから cwd を取得する。

    Parameters
    ----------
    dir_path : str
        logs.json と同じディレクトリパス。

    Returns
    -------
    str
        作業ディレクトリパス。取得できない場合は空文字。
    """
    try:
        for fname in os.listdir(dir_path):
            if fname.startswith("checkpoint-") and fname.endswith(".json"):
                fpath = os.path.join(dir_path, fname)
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        data = json.load(f)
                except (json.JSONDecodeError, OSError):
                    continue

                # history[0].parts[0].text から cwd を抽出
                history = data.get("history", [])
                if not history:
                    continue
                parts = history[0].get("parts", [])
                if not parts:
                    continue
                text = parts[0].get("text", "")
                match = _CWD_RE.search(text)
                if match:
                    return match.group(1).strip()
    except OSError:
        pass

    return ""


# ---------------------------------------------------------------------------
# opencode セッション情報の収集
# ---------------------------------------------------------------------------


def collect_opencode_sessions() -> list[dict]:
    """opencode のセッション情報を収集する。

    現行方式（SQLite: opencode.db / opencode-<channel>.db）を優先して読み込み、
    テーブル・カラムが無い、DB が存在しないなどで読めない場合は
    旧方式（JSON: storage/session/<project_id>/ses_*.json）へフォールバックする。

    Returns
    -------
    list[dict]
        opencode セッション情報のリスト。updated_at の新しい順（降順）。
        各要素は ``{"agent_key", "name", "agent_session_id", "cwd", "updated_at"}``。
    """
    try:
        if not os.path.isdir(_OPENCODE_BASE_DIR):
            logger.debug("opencode データディレクトリが見つからない: %s", _OPENCODE_BASE_DIR)
            return []

        entries = _collect_opencode_sessions_from_sqlite()

        if not entries:
            entries = _collect_opencode_sessions_from_json()

        entries.sort(key=lambda e: e.get("updated_at", ""), reverse=True)

        logger.info("opencode セッション情報を収集: %d 件", len(entries))
        return entries
    except Exception as exc:
        # opencode 側の想定外の異常で codex/bob の収集結果まで
        # 失うことがないよう、呼び出し元へは例外を投げず空リストで返す
        logger.warning("opencode セッション情報の収集に失敗: %s", exc)
        return []


def _find_opencode_db_paths() -> list[str]:
    """opencode の SQLite DB ファイル一覧を取得する。

    opencode.db（通常チャンネル）と opencode-<channel>.db（beta 等）を対象とする。

    Returns
    -------
    list[str]
        DB ファイルパスのリスト。1 件も無い場合は空リスト。
    """
    if not os.path.isdir(_OPENCODE_BASE_DIR):
        return []

    paths: list[str] = []

    default_db = os.path.join(_OPENCODE_BASE_DIR, "opencode.db")
    if os.path.isfile(default_db):
        paths.append(default_db)

    # ディレクトリ部分のみエスケープし、ファイル名側の "*" はワイルドカードとして残す
    paths.extend(
        sorted(
            glob.glob(
                os.path.join(glob.escape(_OPENCODE_BASE_DIR), "opencode-*.db")
            )
        )
    )

    return paths


def _collect_opencode_sessions_from_sqlite() -> list[dict]:
    """SQLite（opencode.db 系）からセッション情報を収集する。

    Returns
    -------
    list[dict]
        opencode セッション情報のリスト。読み取り不可の場合は空リスト。
    """
    entries: list[dict] = []
    for db_path in _find_opencode_db_paths():
        try:
            entries.extend(_read_opencode_db(db_path))
        except Exception as exc:
            # 1 DB の想定外の破損（型不整合など）で他 DB の収集まで
            # 巻き添えにしないよう、ファイル単位で例外を握り潰す
            logger.warning(
                "opencode DB の読み込み中に予期しないエラー: %s — %s", db_path, exc
            )
    return entries


def _read_opencode_db(db_path: str) -> list[dict]:
    """opencode の SQLite DB 1 ファイルから session テーブルを読み込む。

    起動中の opencode をロックしないよう、読み取り専用の URI 接続で開く。
    session テーブルや期待カラムが存在しない場合は例外を握り潰し空リストを返す。

    Parameters
    ----------
    db_path : str
        opencode.db または opencode-<channel>.db のパス。

    Returns
    -------
    list[dict]
        セッション情報のリスト。読み取り不可の場合は空リスト。
    """
    entries: list[dict] = []
    uri = _to_sqlite_ro_uri(db_path)

    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        logger.warning("opencode SQLite への接続に失敗: %s — %s", db_path, exc)
        return []

    try:
        cur = conn.execute(
            "SELECT id, title, directory, time_updated FROM session"
        )
        for session_id, title, directory, time_updated in cur.fetchall():
            # スキーマ上は TEXT 想定だが、破損 DB では INTEGER/BLOB/NULL が
            # 混入し得る。非文字列は空文字扱いにし、os.path.normpath() の
            # TypeError を未然に防ぐ（id が空文字になった行は不正データとして
            # スキップする）
            session_id = session_id if isinstance(session_id, str) else ""
            if not session_id:
                logger.debug(
                    "opencode session テーブルの id が不正なため行をスキップ: %s", db_path
                )
                continue
            title = title if isinstance(title, str) else ""
            directory = directory if isinstance(directory, str) else ""
            entries.append({
                "agent_key": "opencode",
                "name": title,
                "agent_session_id": session_id,
                "cwd": os.path.normpath(directory) if directory else "",
                "updated_at": _ms_to_iso(time_updated),
            })
    except sqlite3.Error as exc:
        logger.warning(
            "opencode session テーブルの読み込みに失敗: %s — %s", db_path, exc
        )
        entries = []
    finally:
        conn.close()

    return entries


def _to_sqlite_ro_uri(db_path: str) -> str:
    """SQLite の読み取り専用 URI 接続文字列に変換する。

    Windows のパス区切りを POSIX 形式へ変換し、``?``/``#`` などの
    URI 予約文字が混入していても壊れないようパーセントエンコードする。

    Parameters
    ----------
    db_path : str
        DB ファイルの絶対パス。

    Returns
    -------
    str
        ``sqlite3.connect(..., uri=True)`` にそのまま渡せる URI 文字列。
    """
    posix_path = Path(db_path).as_posix()
    if posix_path.startswith("//"):
        # UNC パス（\\server\share\...）は先頭 2 連続スラッシュがサーバー名の
        # 直前に残る形になり、"file://server/..." のままだと server が URI の
        # authority と誤認識される。"file:" の後ろにもう一段スラッシュを足し、
        # "file:////server/share/..." の形にして authority 部分を空にする
        quoted = quote(posix_path, safe="/:")
        return f"file://{quoted}?mode=ro"
    if not posix_path.startswith("/"):
        posix_path = "/" + posix_path
    quoted = quote(posix_path, safe="/:")
    return f"file:{quoted}?mode=ro"


def _collect_opencode_sessions_from_json() -> list[dict]:
    """旧方式（sst/opencode 時代の JSON）からセッション情報を収集する。

    ``storage/session/<project_id>/ses_*.json`` を走査するフォールバック。

    Returns
    -------
    list[dict]
        opencode セッション情報のリスト。
    """
    storage_dir = os.path.join(_OPENCODE_BASE_DIR, "storage", "session")
    if not os.path.isdir(storage_dir):
        return []

    entries: list[dict] = []

    try:
        for project_id in os.listdir(storage_dir):
            project_dir = os.path.join(storage_dir, project_id)
            if not os.path.isdir(project_dir):
                continue

            for fname in os.listdir(project_dir):
                if not (fname.startswith("ses_") and fname.endswith(".json")):
                    continue
                entry = _read_opencode_session_json(
                    os.path.join(project_dir, fname)
                )
                if entry:
                    entries.append(entry)
    except OSError as exc:
        logger.warning("opencode storage/session の読み込みに失敗: %s", exc)

    return entries


def _read_opencode_session_json(fpath: str) -> dict | None:
    """opencode の旧 JSON セッションファイル 1 件を読み込む。

    キー名のバージョン差異（``id``/``sessionID``、``directory``/``cwd``/``path``、
    ``time.updated``/``time_updated``/``updated``）を吸収して読む。

    Parameters
    ----------
    fpath : str
        ses_*.json のパス。

    Returns
    -------
    dict | None
        セッション情報。読み込み・パースに失敗した場合は ``None``。
    """
    try:
        with open(fpath, "r", encoding="utf-8") as f:
            row = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("opencode session json の読み込みに失敗: %s — %s", fpath, exc)
        return None

    if not isinstance(row, dict):
        return None

    session_id = row.get("id") or row.get("sessionID") or ""
    title = row.get("title") or ""
    directory = row.get("directory") or row.get("cwd") or row.get("path") or ""

    time_obj = row.get("time")
    updated_ms = time_obj.get("updated") if isinstance(time_obj, dict) else None
    if updated_ms is None:
        updated_ms = row.get("time_updated")
    if updated_ms is None:
        updated_ms = row.get("updated")

    return {
        "agent_key": "opencode",
        "name": title,
        "agent_session_id": session_id,
        "cwd": os.path.normpath(directory) if directory else "",
        "updated_at": _ms_to_iso(updated_ms),
    }


def _ms_to_iso(epoch_ms: int | float | None) -> str:
    """epoch ミリ秒を ISO 8601 文字列（UTC）に変換する。

    Parameters
    ----------
    epoch_ms : int | float | None
        epoch ミリ秒。

    Returns
    -------
    str
        ISO 8601 形式の日時文字列。変換できない場合は空文字。
    """
    if not epoch_ms:
        return ""
    try:
        return datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError, OverflowError) as exc:
        logger.debug("epoch ミリ秒の変換に失敗: %r — %s", epoch_ms, exc)
        return ""


# ---------------------------------------------------------------------------
# 統合: session_ids.json の生成
# ---------------------------------------------------------------------------


def collect_all_session_ids() -> list[dict]:
    """codex と bob と opencode のセッション情報を統合して返す。

    Returns
    -------
    list[dict]
        全セッション情報のリスト。
    """
    result: list[dict] = []
    result.extend(collect_codex_sessions())
    result.extend(collect_bob_sessions())
    result.extend(collect_opencode_sessions())
    return result


def save_session_ids(data_dir: str) -> list[dict]:
    """セッション情報を収集し、session_ids.json として保存する。

    Parameters
    ----------
    data_dir : str
        sessions.json と同じディレクトリ（app/data/）。

    Returns
    -------
    list[dict]
        保存したセッション情報のリスト。
    """
    session_ids = collect_all_session_ids()

    out_path = os.path.join(data_dir, "session_ids.json")
    os.makedirs(data_dir, exist_ok=True)

    # 原子的書き込み
    fd, tmp_path = tempfile.mkstemp(
        dir=data_dir, suffix=".tmp", prefix=".session_ids_"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(session_ids, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp_path, out_path)
        logger.info("session_ids.json を保存: %d 件", len(session_ids))
    except BaseException:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise

    return session_ids
