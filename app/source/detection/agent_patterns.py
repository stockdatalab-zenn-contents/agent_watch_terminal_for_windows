"""Agent pattern definitions for the Agent Watch Terminal.

Defines gate patterns (used to identify which AI agent owns a terminal)
and status patterns (used to detect waiting / error states) for each
supported agent.  Also provides shell-prompt patterns for "completed"
detection and a helper to strip ANSI escape sequences.
"""

import re
from typing import Any

# ---------------------------------------------------------------------------
# ANSI escape removal
# ---------------------------------------------------------------------------

# CUF (Cursor Forward): \x1b[C, \x1b[1C, \x1b[5C, etc.
# Claude Code の TUI は単語間をリテラルスペースではなく CUF で描画するため、
# 他の ANSI シーケンスと区別して空白に置換する。
# CUB (\x1b[nD) 等の他のカーソル移動は上書き用途であり空白置換しない。
_CUF_RE: re.Pattern[str] = re.compile(r"\x1b\[\d*C")

# CHA (Cursor Horizontal Absolute): \x1b[G, \x1b[2G, \x1b[35G, etc.
# Claude Code の TUI は確認プロンプト等のコンパクト再描画で語間を CHA で
# 詰めて配置する。ANSI 除去のみだと単語が結合してしまい、waiting パターン
# (例: r"Do you want to ") の末尾スペースに当たらず取りこぼす事象が発生する。
# CUF と同様に空白 1 文字へ置換し、パターンマッチ時の語境界を維持する。
_CHA_RE: re.Pattern[str] = re.compile(r"\x1b\[\d*G")

ANSI_ESCAPE_RE: re.Pattern[str] = re.compile(
    r"\x1b(?:\[[\x20-\x3f]*[\x40-\x7e]|\][^\x07]*\x07|\([A-Z])"
)

# ---------------------------------------------------------------------------
# Agent definitions
# ---------------------------------------------------------------------------

AgentDict = dict[str, Any]

# ---------------------------------------------------------------------------
# セッション ID 突合方式 (``session_match``)
#
# 終了時に収集した AI ツール側セッション情報と、アプリ側セッションを
# どう突き合わせて ``agent_session_id`` を決めるかの区分。
#
#   SESSION_MATCH_NONE       : name で復元するため agent_session_id 不要
#                              (claude / copilot)
#   SESSION_MATCH_NAME_CWD   : name と cwd の完全一致で突合 (codex / bob)
#   SESSION_MATCH_CWD_LATEST : cwd 一致のうち最終更新が新しい順に割当
#                              (opencode) — セッション名を任意指定できず
#                              name で突合できない AI ツール向け
# ---------------------------------------------------------------------------

SESSION_MATCH_NONE: str = "none"
SESSION_MATCH_NAME_CWD: str = "name_cwd"
SESSION_MATCH_CWD_LATEST: str = "cwd_latest"

# ---------------------------------------------------------------------------
# 復元コマンド (``resume_command`` / ``resume_command_fallback``)
#
#   resume_command          : 通常の復元コマンド。``{name}`` と
#                             ``{agent_session_id}`` を差し込める。
#
#   resume_command_fallback : ``{agent_session_id}`` を要求するのに ID が
#                             取得できなかった場合の代替コマンド。省略可。
#
# opencode は「最初のメッセージを送るまでセッションを永続化しない」ため、
# タブを開いただけ・起動しただけで再起動すると ID が採取できず、ID ベースの
# 自動復元が丸ごとスキップされていた。``opencode --continue`` は
# 「そのディレクトリの直近セッション」を再開するため ID が不要で、
# 対象セッションが無ければ通常起動になる（実機確認済み）。
#
# 組み立ては get_resume_command() を使うこと。
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# ステータス判定パターンの 2 種類
#
#   status_patterns       : {status: [regex, ...]}
#                           1 フラグメント（\r 分割済みの 1 行相当）に対する
#                           OR 判定。どれか 1 つが当たれば成立。
#
#   status_combo_patterns : {status: [[regex, regex, ...], ...]}
#                           直近出力ウィンドウ（リングバッファ＋末尾バッファ
#                           を改行連結したもの）に対する AND 判定。
#                           内側リストの全パターンがウィンドウ内のどこかに
#                           出現した場合のみ成立し、外側リストは OR。
#                           「2 つのボタンが同時に画面に出ている」のような、
#                           1 行では表現できない条件に使う。
#                           未定義のエージェントは判定をスキップする。
#
# 判定順は status_combo_patterns が先。AND 条件の方が具体的であり、
# 単独語の取りこぼし・誤検出よりも優先させるべきため。
# ---------------------------------------------------------------------------

AGENTS: dict[str, AgentDict] = {
    "claude": {
        "name": "Claude Code",
        "gate_patterns": [
            r"Claude Code",
            # r"claude-code",
            r"╭.*Claude",
        ],
        "status_patterns": {
            "waiting": [
                r"Do you want to ",
                r"Enter to select",
            ],
            "error": [],
        },
        "ctrlc_to_close": 2,
        "session_match": SESSION_MATCH_NONE,
        "start_command": "claude",
        "rename_command": "/rename {name}",
        "resume_command": 'claude --resume "{name}"',
    },
    "codex": {
        "name": "Codex CLI",
        "gate_patterns": [
            r"codex ",          # trailing space intentional
            r"OpenAI Codex",
        ],
        "status_patterns": {
            "waiting": [
                r"^codex>\s*$",  # empty codex prompt
                r"enter to ",
            ],
            "error": [],
        },
        "ctrlc_to_close": 1,
        "session_match": SESSION_MATCH_NAME_CWD,
        "start_command": "codex",
        "rename_command": "/rename {name}",
        "resume_command": "codex resume {agent_session_id}",
    },
    "copilot": {
        "name": "Copilot CLI",
        "gate_patterns": [
            r"GitHub Copilot",
            # r"gh copilot",
            # r"copilot-cli",
            r"Copilot v\d+\.\d+",  # TUI 起動バナー（可視テキスト）
        ],
        "status_patterns": {
            "waiting": [
                r"^copilot>\s*$",
                r"Do you want to ",
            ],
            "error": [],
        },
        "ctrlc_to_close": 2,
        "session_match": SESSION_MATCH_NONE,
        "start_command": "copilot",
        "rename_command": "/rename {name}",
        "resume_command": 'copilot --resume="{name}"',
    },
    "bob": {
        "name": "Bob",
        "gate_patterns": [
            r"Bob (?!goes to sleep)",  # 終了メッセージ「Bob goes to sleep」を除外
            r"Sandbox mode",              # bob ステータスライン（Disabled/Enabled）
            r"Enter your prompt, / for",  # bob プロンプトテンプレート
        ],
        "status_patterns": {
            "waiting": [
                r"^bob>\s*$",
            ],
            "error": [],
        },
        "ctrlc_to_close": 2,
        "session_match": SESSION_MATCH_NAME_CWD,
        "start_command": "bob",
        "rename_command": "/chat save {name}",
        "resume_command": "bob resume {agent_session_id}",
    },
    "opencode": {
        "name": "opencode",
        # 語間は \s+ で表現する。opencode の TUI はカーソル制御
        # （CUF/CHA）で語を配置するため、strip_ansi 後の語間が
        # 空白 1 文字とは限らず、リテラル空白だと取りこぼすため。
        "gate_patterns": [
            # 起動直後のホーム画面に出る入力欄プレースホルダ。
            # 実際の表示は "Ask anything… \"...\"" で末尾が三点リーダ。
            # 三点リーダまで含めることで、英語の散文や本リポジトリの
            # ドキュメント中に現れる同じ語句へ反応しないようにする。
            r"Ask\s+anything\s*(?:…|\.\.\.)",
            # 権限確認ダイアログの見出し
            r"Permission\s+required",
            # 生成中に入力欄下部へ出る中断キーヒント
            r"esc\s+interrupt",
            # 復元・継続コマンドのシェルエコー。
            # セッション復元時の画面には "Ask anything" が出ず
            # 他のアンカーもカーソル制御で分断されるため、
            # 打ち込まれたコマンド自体でゲートを開く。
            #
            # 誤検出を避けるため次の 3 点で絞り込む。
            #   1. 行頭、またはシェルプロンプト末尾 (> $ #) の直後
            #   2. --session/-s はセッション ID (ses_...) を伴う
            #      → 文書中の "opencode --session <id>" 等に反応しない
            #   3. --continue/-c は後続が語構成文字でない
            #      → "--continue-on-error" に反応しない
            r"(?:^|[>$#]\s*)opencode\s+"
            r"(?:(?:--session|-s)\s+ses_[A-Za-z0-9]+"
            r"|(?:--continue|-c)(?![\w-]))",
        ],
        "status_patterns": {
            "waiting": [
                # 権限確認ダイアログ（Allow once / Allow always / Reject）
                r"Permission\s+required",
                r"Allow\s+once",
                # 質問ダイアログの自由入力欄
                r"Type\s+your\s+own\s+answer",
            ],
            "error": [],
        },
        # 同時出現（AND 条件）による判定。詳細は AGENTS 定義前の
        # 「status_combo_patterns」コメント参照。
        #
        # ダイアログのボタン群は TUI がカーソル制御で別フラグメントへ
        # 分割して描画するため、1 フラグメント単位の status_patterns では
        # 取りこぼす。連結ウィンドウに対する AND 判定で確実に拾う。
        #
        # "Confirm" / "Cancel" は単独だと AI の応答文中にも現れる一般語の
        # ため、対で出現することを条件にして誤検出を避ける。
        "status_combo_patterns": {
            "waiting": [
                # 権限確認ダイアログのボタン
                [r"Allow\s+once", r"Allow\s+always"],
                # 確認ダイアログのボタン
                [r"Confirm", r"Cancel"],
            ],
        },
        # keybind の app_exit が "ctrl+c,ctrl+d,<leader>q" で、
        # Ctrl+C 1 回で終了する（中断は Esc に割当）
        "ctrlc_to_close": 1,
        # opencode はセッション名を任意指定できず自動生成のため、
        # name ではなく cwd + 最終更新で突合する
        "session_match": SESSION_MATCH_CWD_LATEST,
        "start_command": "opencode",
        # /rename 相当のコマンドを持たないため rename_command は定義しない。
        # 終了時の命名手順はスキップされ、命名済み扱いになる。
        "resume_command": "opencode --session {agent_session_id}",
        # opencode はメッセージ送信までセッションを永続化しないため、
        # agent_session_id が採取できないまま再起動するケースが日常的に発生する。
        # その場合は ID 不要の --continue（そのディレクトリの直近セッションを
        # 再開）へフォールバックする。対象が無ければ通常起動になる。
        "resume_command_fallback": "opencode --continue",
    },
}

# ---------------------------------------------------------------------------
# Shell prompt patterns  --  used for "completed" detection
# (agent exited, terminal returned to a normal shell prompt)
# ---------------------------------------------------------------------------

SHELL_PROMPT_PATTERNS: list[str] = [
    r"^PS [A-Z]:\\",                    # PowerShell: PS C:\path>
    r"^[a-zA-Z0-9_-]+@.*[\$#]\s*$",    # user@host$
]

# ---------------------------------------------------------------------------
# Trailing-buffer-only prompt patterns.
# SHELL_PROMPT_PATTERNS よりも厳格。末尾バッファ（未改行の最終フラグメント）
# に対する shell prompt 検出用。曖昧な `^>\s*$` / `^\$ $` は除外する。
# これは AI ツール（Claude / Codex 等）の TUI が描画する入力インジケータ
# (`> ` / `$ `) が AI 稼働中の末尾バッファに頻繁に残り、誤って `completed`
# を発火させゲートを閉じてしまう事象を防ぐため。
#
# 行末 (`$`) アンカーで「末尾バッファの末尾」にプロンプトが存在する場合に
# マッチさせる。行頭 (`^`) アンカーにすると、codex / copilot の TUI 終了時
# に前行メッセージと PS プロンプトが `\r\n` を挟まず連結されるケースで
# 先頭が PS でなくなり検出を取りこぼすため。
# ---------------------------------------------------------------------------

TRAILING_PROMPT_PATTERNS: list[str] = [
    r"PS [A-Z]:\\[^\r\n]*>\s*$",        # 末尾が PS C:\path>
    r"[a-zA-Z0-9_-]+@[^\r\n]*[\$#]\s*$",  # 末尾が user@host$/#
]

# ---------------------------------------------------------------------------
# Pre-compiled patterns  --  avoids repeated compilation at runtime
# ---------------------------------------------------------------------------

_compiled_gate: dict[str, list[re.Pattern[str]]] = {
    key: [re.compile(p) for p in agent["gate_patterns"]]
    for key, agent in AGENTS.items()
}

_compiled_status: dict[str, dict[str, list[re.Pattern[str]]]] = {
    key: {
        status: [re.compile(p) for p in patterns]
        for status, patterns in agent["status_patterns"].items()
    }
    for key, agent in AGENTS.items()
}

_compiled_shell: list[re.Pattern[str]] = [
    re.compile(p) for p in SHELL_PROMPT_PATTERNS
]

# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from *text*.

    CUF (Cursor Forward, ``\\x1b[nC``) と CHA (Cursor Horizontal
    Absolute, ``\\x1b[nG``) は空白 1 文字に置換してから、残りの
    ANSI シーケンスを除去する。Claude Code 等の TUI は単語間の配置に
    CUF と CHA の双方を使うため、単純除去すると単語が結合し
    パターンマッチが失敗する。

    Parameters
    ----------
    text : str
        Raw terminal output that may contain ANSI codes.

    Returns
    -------
    str
        Clean string with all escape sequences removed.
    """
    text = _CUF_RE.sub(" ", text)
    text = _CHA_RE.sub(" ", text)
    return ANSI_ESCAPE_RE.sub("", text)


def get_agent_names() -> list[str]:
    """Return the list of registered agent keys.

    Returns
    -------
    list[str]
        Agent identifiers
        (e.g. ``["claude", "codex", "copilot", "bob", "opencode"]``).
    """
    return list(AGENTS.keys())


def get_session_match_mode(agent_key: str) -> str:
    """エージェントのセッション ID 突合方式を返す。

    Parameters
    ----------
    agent_key : str
        エージェント識別子 (``"claude"`` 等)。未登録キーも許容。

    Returns
    -------
    str
        ``SESSION_MATCH_NONE`` / ``SESSION_MATCH_NAME_CWD`` /
        ``SESSION_MATCH_CWD_LATEST`` のいずれか。未登録・未指定は
        ``SESSION_MATCH_NONE``。
    """
    agent = AGENTS.get(agent_key)
    if not agent:
        return SESSION_MATCH_NONE
    return agent.get("session_match", SESSION_MATCH_NONE)


def requires_agent_session_id(agent_key: str) -> bool:
    """resume に ``agent_session_id`` が必須かどうかを返す。

    ``resume_command`` のテンプレートが ``{agent_session_id}`` を含むか
    どうかで判定する。「どう突合するか」(``session_match``) と「復元に
    ID が要るか」は別概念のため、突合方式からは推論しない。

    Parameters
    ----------
    agent_key : str
        エージェント識別子。

    Returns
    -------
    bool
        ID ベースで復元するエージェントなら ``True``。
    """
    agent = AGENTS.get(agent_key)
    if not agent:
        return False
    return "{agent_session_id}" in agent.get("resume_command", "")


def get_resume_command(
    agent_key: str, name: str, agent_session_id: str = ""
) -> str:
    """自動復元に使うコマンド文字列を組み立てて返す。

    ``resume_command`` が ``{agent_session_id}`` を要求するのに ID が
    未取得の場合は、``resume_command_fallback`` があればそちらを使う。
    どちらも使えない場合は空文字を返す（＝自動復元は不可）。

    Parameters
    ----------
    agent_key : str
        エージェント識別子。未登録キーも許容（空文字を返す）。
    name : str
        セッション名。``{name}`` に差し込む。
    agent_session_id : str
        AI ツール側のセッション ID。未取得なら空文字。

    Returns
    -------
    str
        実行する復元コマンド。自動復元できない場合は空文字。

    Raises
    ------
    KeyError, IndexError, ValueError
        テンプレートに未知のプレースホルダが含まれる場合など、
        ``str.format()`` が失敗したとき。呼び出し側で保護すること。
    """
    agent = AGENTS.get(agent_key)
    if not agent:
        return ""

    template = agent.get("resume_command", "")
    if (
        template
        and "{agent_session_id}" in template
        and not agent_session_id
    ):
        template = agent.get("resume_command_fallback", "")
    if not template:
        return ""

    return template.format(name=name, agent_session_id=agent_session_id)
