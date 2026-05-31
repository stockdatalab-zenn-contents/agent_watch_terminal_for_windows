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
        "start_command": "copilot",
        "rename_command": "/rename {name}",
        "resume_command": 'copilot --resume="{name}"',
    },
    "bob": {
        "name": "Bob",
        "gate_patterns": [
            r"Bob ",
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
        "start_command": "bob",
        "rename_command": "/chat save {name}",
        "resume_command": "bob resume {agent_session_id}",
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
        Agent identifiers (e.g. ``["claude", "codex", "copilot", "bob"]``).
    """
    return list(AGENTS.keys())
