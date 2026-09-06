"""Pattern matching module for AI agent status detection.

Performs regex pattern matching on PTY output lines to determine which
AI agent is running and what its current status is.  All matching is
done on ANSI-stripped text.

Typical usage::

    matcher = PatternMatcher()
    clean   = matcher.strip_ansi(raw_line)
    agent   = matcher.check_gate(clean)
    status  = matcher.check_status(clean, agent) if agent else None
    done    = matcher.check_completed(clean)
"""

import logging
import re

from source.detection.agent_patterns import (
    AGENTS,
    SHELL_PROMPT_PATTERNS,
    TRAILING_PROMPT_PATTERNS,
    strip_ansi,
)

logger = logging.getLogger(__name__)


class PatternMatcher:
    """Compile and evaluate agent / status / shell-prompt patterns.

    On instantiation every regex string defined in ``AGENTS`` and
    ``SHELL_PROMPT_PATTERNS`` is compiled once and stored in internal
    dictionaries for fast lookup at match time.
    """

    def __init__(self) -> None:
        self._gate: dict[str, list[re.Pattern[str]]] = {}
        self._status: dict[str, dict[str, list[re.Pattern[str]]]] = {}
        # AND 条件グループ: agent -> status -> [[pattern, ...], ...]
        self._status_combo: dict[
            str, dict[str, list[list[re.Pattern[str]]]]
        ] = {}
        self._shell: list[re.Pattern[str]] = []
        self._trailing: list[re.Pattern[str]] = []

        self._compile_patterns()
        logger.info(
            "PatternMatcher 初期化完了 — エージェント数=%d, "
            "シェルプロンプトパターン数=%d, "
            "末尾プロンプトパターン数=%d",
            len(self._gate),
            len(self._shell),
            len(self._trailing),
        )

    # ------------------------------------------------------------------
    # Pattern compilation
    # ------------------------------------------------------------------

    def _compile_patterns(self) -> None:
        """Compile all raw pattern strings into ``re.Pattern`` objects."""

        # Gate patterns (per agent)
        for key, agent in AGENTS.items():
            self._gate[key] = [
                re.compile(p) for p in agent["gate_patterns"]
            ]

        # Status patterns (per agent, per status)
        for key, agent in AGENTS.items():
            self._status[key] = {}
            for status, patterns in agent["status_patterns"].items():
                self._status[key][status] = [
                    re.compile(p) for p in patterns
                ]

        # Status combo patterns (per agent, per status, AND groups)
        # 未定義のエージェントは空 dict となり、判定はスキップされる。
        for key, agent in AGENTS.items():
            self._status_combo[key] = {}
            combo_map = agent.get("status_combo_patterns", {})
            for status, groups in combo_map.items():
                self._status_combo[key][status] = [
                    [re.compile(p) for p in group] for group in groups
                ]

        # Shell prompt patterns (for completed detection)
        self._shell = [re.compile(p) for p in SHELL_PROMPT_PATTERNS]

        # Trailing-buffer-only prompt patterns (strict subset)
        self._trailing = [re.compile(p) for p in TRAILING_PROMPT_PATTERNS]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check_gate(self, line: str) -> str | None:
        """Check whether *line* matches any agent's gate pattern.

        Parameters
        ----------
        line : str
            A single line of terminal output (**already ANSI-stripped**).

        Returns
        -------
        str | None
            The agent key (e.g. ``"claude"``) on match, or ``None``.
        """
        cleaned = strip_ansi(line)
        for agent_key, patterns in self._gate.items():
            for pat in patterns:
                if pat.search(cleaned):
                    logger.debug(
                        "ゲート一致 — agent=%s, pattern=%s, line=%r",
                        agent_key,
                        pat.pattern,
                        cleaned,
                    )
                    return agent_key
        return None

    def check_status(
        self, line: str, agent_key: str
    ) -> tuple[str, str] | None:
        """Check *line* against the status patterns of *agent_key*.

        Parameters
        ----------
        line : str
            A single line of terminal output (**already ANSI-stripped**).
        agent_key : str
            Agent identifier returned by :meth:`check_gate`.

        Returns
        -------
        tuple[str, str] | None
            ``(status, pattern)`` on match — *status* is ``"waiting"``
            or ``"error"``, *pattern* is the regex source string.
            ``None`` if no status pattern matches.
        """
        cleaned = strip_ansi(line)
        status_map = self._status.get(agent_key)
        if status_map is None:
            logger.warning(
                "不明なエージェントキー: %s", agent_key
            )
            return None

        for status, patterns in status_map.items():
            for pat in patterns:
                if pat.search(cleaned):
                    logger.debug(
                        "ステータス一致 — agent=%s, status=%s, "
                        "pattern=%s, line=%r",
                        agent_key,
                        status,
                        pat.pattern,
                        cleaned,
                    )
                    return (status, pat.pattern)
        return None

    def check_status_combo(
        self, window: str, agent_key: str
    ) -> tuple[str, str] | None:
        """*window* 内での複数パターン同時出現（AND 条件）を判定する。

        ``check_status`` が 1 行単位の OR 判定なのに対し、こちらは
        直近出力をまとめた *window* に対して「グループ内の全パターンが
        どこかに出現するか」を見る。TUI がカーソル制御でボタンを別々の
        フラグメントに分割して描画するため、行単位では拾えない
        「2 つのボタンが同時に表示されている」条件を判定できる。

        Parameters
        ----------
        window : str
            直近出力をまとめたテキスト（リングバッファ＋末尾バッファを
            改行連結したもの）。**ANSI 除去済みを想定**。
        agent_key : str
            エージェント識別子。

        Returns
        -------
        tuple[str, str] | None
            ``(status, pattern)`` on match、未一致なら ``None``。
            *pattern* は成立したグループ各要素の **選択（OR）** 表現。
            AND の成立自体はここで判定済みで、この文字列は呼び出し側が
            「実際にマッチした行」を特定・ログ出力するために使う。
        """
        combo_map = self._status_combo.get(agent_key)
        if not combo_map:
            return None

        cleaned = strip_ansi(window)
        for status, groups in combo_map.items():
            for group in groups:
                if not group:
                    continue
                if all(pat.search(cleaned) for pat in group):
                    pattern = "|".join(
                        f"(?:{pat.pattern})" for pat in group
                    )
                    logger.debug(
                        "ステータス同時一致 — agent=%s, status=%s, "
                        "patterns=%s",
                        agent_key,
                        status,
                        [pat.pattern for pat in group],
                    )
                    return (status, pattern)
        return None

    def check_completed(self, line: str) -> str | None:
        """Check whether *line* matches a shell prompt pattern.

        A match indicates that the AI agent has exited and the terminal
        has returned to a normal shell prompt (``completed`` state).

        Parameters
        ----------
        line : str
            A single line of terminal output (**already ANSI-stripped**).

        Returns
        -------
        str | None
            The matched regex source string, or ``None``.
        """
        cleaned = strip_ansi(line)
        for pat in self._shell:
            if pat.search(cleaned):
                logger.debug(
                    "シェルプロンプト一致 — pattern=%s, line=%r",
                    pat.pattern,
                    cleaned,
                )
                return pat.pattern
        return None

    def check_trailing_prompt(self, line: str) -> str | None:
        """Check whether *line* matches a *trailing-buffer* shell prompt.

        Uses the strict subset :data:`TRAILING_PROMPT_PATTERNS` (PowerShell
        ``PS C:\\...`` and ``user@host$``/``user@host#`` only), not the full
        :data:`SHELL_PROMPT_PATTERNS`. Exists to avoid false positives on
        AI TUI input indicators (``> ``, ``$ ``) that often end the trailing
        (un-newlined) buffer while an AI tool is still running.

        Parameters
        ----------
        line : str
            Trailing buffer content (**already ANSI-stripped**).

        Returns
        -------
        str | None
            The matched regex source string, or ``None``.
        """
        cleaned = strip_ansi(line)
        for pat in self._trailing:
            if pat.search(cleaned):
                logger.debug(
                    "末尾シェルプロンプト一致 — pattern=%s, line=%r",
                    pat.pattern,
                    cleaned,
                )
                return pat.pattern
        return None

    def strip_ansi(self, text: str) -> str:
        """Remove ANSI escape sequences from *text*.

        Delegates to :func:`app.source.detection.agent_patterns.strip_ansi`.

        Parameters
        ----------
        text : str
            Raw terminal output that may contain ANSI codes.

        Returns
        -------
        str
            Clean string with all escape sequences removed.
        """
        return strip_ansi(text)
