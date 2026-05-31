"""
session_id_collector - codex/bob のセッション情報を収集するモジュール

アプリ終了時に各 AI ツールのローカルデータから
セッション ID を収集し、session_ids.json として出力する。
次回起動時の resume コマンドに使用する。
"""

import json
import logging
import os
import re
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

# codex データディレクトリのベースパス
_CODEX_BASE_DIR = os.path.join(str(Path.home()), ".codex")

# bob データディレクトリのベースパス
_BOB_BASE_DIR = os.path.join(str(Path.home()), ".bob")

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
# 統合: session_ids.json の生成
# ---------------------------------------------------------------------------


def collect_all_session_ids() -> list[dict]:
    """codex と bob のセッション情報を統合して返す。

    Returns
    -------
    list[dict]
        全セッション情報のリスト。
    """
    result: list[dict] = []
    result.extend(collect_codex_sessions())
    result.extend(collect_bob_sessions())
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
