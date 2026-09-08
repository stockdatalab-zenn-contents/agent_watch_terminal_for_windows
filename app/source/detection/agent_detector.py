"""Two-stage gate detection system for AI agents — hybrid architecture.

PTY 出力テキストを行単位で処理し、ゲート機構で AI エージェントの
セッション所有権を特定した後、ステータスをパターンマッチ＋出力
スループットで追跡する。

主要な設計方針:
  - running はパターンで検出しない。出力が閾値時間以上継続 = running
  - waiting / error はデバウンス確定時にリングバッファから行分割して判定
  - シェルプロンプト検出 → idle（即時確定、デバウンス不要）
  - ゲート閉鎖は Ctrl+C 入力でのみ発生

Gate flow per line (after ANSI stripping + CR handling):
  1. If gate not open -> check all gate patterns -> open gate on match
  2. If gate open     -> check shell-prompt patterns -> idle（即時確定）
  3. Append cleaned line to ring buffer (recent_output)
  4. Duplicate suppression via ``agent:status:matched_text`` key
  5. Fire ``on_status_change`` callback when status actually changes

Timer callbacks:
  - デバウンスタイマー（3 秒無出力）→ リングバッファから waiting/error 判定
  - スループット検出（出力継続 3 秒）→ running で即時確定

Input processing:
  - feed_input() で Ctrl+C カウントを管理 → 閾値到達でゲート閉鎖
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from source.detection.agent_patterns import (
    AGENTS,
    get_running_min_cps,
    get_running_threshold_ms,
    in_same_pin_group,
    strip_ansi,
)
from source.detection.pattern_matcher import PatternMatcher

logger = logging.getLogger(__name__)

# Soft flush threshold. TUI アプリ（Codex CLI 等）は画面を ANSI カーソル
# 位置指定で再描画するため出力に `\n` がほぼ含まれない。`\n` 到着を待つと
# バッファが肥大化しパターンマッチ機会も失われる。一定サイズ以上で暫定的
# にフラッシュし、strip_ansi 後にマッチ判定させる。
_SOFT_FLUSH = 2 * 1024  # 2 KB: 典型的な TUI フレーム 1 枚分相当

# Hard safety limit. Soft flush が機能していれば通常到達しない最終防衛線。
_MAX_BUFFER = 16 * 1024  # 16 KB

# Ring buffer size for recent output (ANSI-stripped, CR-fragmented).
# 4 KB に拡張済み。古いエントリは _check_ring_buffer の年齢フィルタで
# 自動的に走査対象外となるため、サイズを今後変更しても false-positive は
# 起きない（根本対応）。
_RING_BUFFER_SIZE = 4 * 1024  # 4 KB


@dataclass
class _SessionState:
    """Per-session internal state for the detector."""

    agent_key: str | None = None
    # --- 版確定（pin） ---
    # 打ち込まれた起動コマンドのエコーから確定したエージェントキー。
    # opencode / opencode2 のように画面文言が共通で gate だけでは
    # 版を判別できない組を切り分けるために保持する。
    pinned_agent_key: str | None = None
    status: str = "idle"
    last_emitted_key: str = ""
    line_buffer: str = ""
    # --- リングバッファ（ANSI 除去・\r 分割済みフラグメントのリスト） ---
    # 各エントリ: ``(timestamp, fragment)``。timestamp は蓄積時刻（秒）。
    # _check_ring_buffer の年齢フィルタで古いエントリは走査対象外。
    recent_output: list[tuple[float, str]] = field(default_factory=list)
    # 蓄積中フラグメントの合計バイト数概算（サイズ制限管理用）。
    recent_output_size: int = 0
    # --- デバウンス関連 ---
    debounce_timer: threading.Timer | None = field(default=None, repr=False)
    output_start_time: float | None = None
    # output_start_time 以降に受け取った文字数。running のレート判定に使う。
    output_bytes: int = 0
    last_output_time: float = 0.0
    # --- Ctrl+C 関連 ---
    ctrlc_count: int = 0
    ctrlc_last_time: float = 0.0
    # --- シェルプロンプト検出フラグ ---
    # AI ツールが終了してシェルに戻った場合に True。
    # ゲート開放時に False にリセットされる。
    exited_to_shell: bool = False
    # --- 外部ステータス供給源 ---
    # True の間はステータスを外部（AI ツールの API）から受け取る。
    # 文言ベースの waiting/error 判定と running 判定は抑止し、
    # 2 つの供給源が互いを上書きし合わないようにする。
    external_active: bool = False


class AgentDetector:
    """Two-stage gate detector for AI agent sessions.

    Stage 1 (*gate check*): scan every line against all registered agent
    gate patterns.  When a match is found the gate "opens" -- the agent
    key is recorded for the session, and status detection becomes active.

    Stage 2 (*status check*): once the gate is open, each subsequent
    line is checked against shell-prompt patterns for idle detection.
    Cleaned lines are accumulated in a ring buffer.  On debounce timeout
    the ring buffer is split into lines and checked against status
    patterns for waiting/error detection.

    Parameters
    ----------
    on_status_change : callback or None
        Optional callback invoked on every *new* status change::

            on_status_change(session_id, status, agent_name, matched_text,
                             event_type=None, matched_pattern=None)
    debounce_ms : int
        出力停止からラベル確定までの待機時間（ミリ秒）
    running_threshold_ms : int
        出力継続から running 強制までの閾値（ミリ秒）
    waiting_recovery_threshold_ms : int
        waiting 状態からの running 復帰閾値（ミリ秒）
    error_recovery_threshold_ms : int
        error 状態からの running 復帰閾値（ミリ秒）
    ctrlc_window_ms : int
        Ctrl+C 連続入力の判定ウィンドウ（ミリ秒）
    """

    def __init__(
        self,
        on_status_change: (
            Callable[[str, str, str | None, str | None], None] | None
        ) = None,
        debounce_ms: int = 3000,
        running_threshold_ms: int = 3000,
        waiting_recovery_threshold_ms: int = 1500,
        error_recovery_threshold_ms: int = 1500,
        ctrlc_window_ms: int = 1000,
    ) -> None:
        self._on_status_change = on_status_change
        self._matcher = PatternMatcher()
        self._sessions: dict[str, _SessionState] = {}
        self._lock = threading.Lock()

        # ms → sec 変換
        self._debounce_sec = debounce_ms / 1000.0
        self._running_threshold_sec = running_threshold_ms / 1000.0
        self._waiting_recovery_sec = waiting_recovery_threshold_ms / 1000.0
        self._error_recovery_sec = error_recovery_threshold_ms / 1000.0
        self._ctrlc_window_sec = ctrlc_window_ms / 1000.0

        logger.info("AgentDetector 初期化完了")

    # ------------------------------------------------------------------
    # Public API — Output processing
    # ------------------------------------------------------------------

    def feed(self, session_id: str, data: str) -> None:
        """Process raw PTY output text for *session_id*.

        The data is appended to a per-session line buffer.  Complete
        lines (terminated by ``\\n``) are extracted and processed one by
        one.  An incomplete trailing fragment is kept in the buffer for
        the next call.

        Parameters
        ----------
        session_id : str
            Session identifier.
        data : str
            Raw PTY output chunk (may contain partial lines).
        """
        callback_args_list: list[tuple] = []

        with self._lock:
            state = self._sessions.setdefault(session_id, _SessionState())

            # --- \r\n 正規化 ---
            normalized = data.replace("\r\n", "\n")
            state.line_buffer += normalized

            # 行区切り or ソフトサイズ閾値の早い方でフラッシュする。
            # TUI アプリは `\n` を含まないフレームを流し続けるため、
            # サイズ閾値で暫定的にラインとして処理へ回す。
            while True:
                if "\n" in state.line_buffer:
                    line, state.line_buffer = state.line_buffer.split(
                        "\n", 1
                    )
                    cb = self._process_line(session_id, state, line)
                    if cb is not None:
                        callback_args_list.append(cb)
                elif len(state.line_buffer) >= _SOFT_FLUSH:
                    line = state.line_buffer
                    state.line_buffer = ""
                    cb = self._process_line(session_id, state, line)
                    if cb is not None:
                        callback_args_list.append(cb)
                else:
                    break

            # 最終防衛線: 想定外の状況で soft flush をすり抜けた場合に備える。
            if len(state.line_buffer) > _MAX_BUFFER:
                logger.warning(
                    "セッション %s: ラインバッファが上限 %d を超過 — 切り詰め",
                    session_id,
                    _MAX_BUFFER,
                )
                state.line_buffer = state.line_buffer[-_MAX_BUFFER:]

            # --- 出力受信時刻の記録 ---
            state.last_output_time = time.time()

            # --- 出力開始時刻の初期化 ---
            if state.output_start_time is None and state.agent_key is not None:
                state.output_start_time = time.time()
                state.output_bytes = 0

            # --- 出力量の加算（running のレート判定用）---
            if state.output_start_time is not None:
                state.output_bytes += len(normalized)

            # --- デバウンスタイマーのリセット ---
            if state.debounce_timer is not None:
                state.debounce_timer.cancel()
                state.debounce_timer = None
            timer = threading.Timer(
                self._debounce_sec,
                self._on_debounce_settled,
                args=(session_id,),
            )
            timer.daemon = True
            state.debounce_timer = timer
            timer.start()

            # --- running 強制判定 ---
            cb = self._check_running_by_throughput(session_id, state)
            if cb is not None:
                callback_args_list.append(cb)

            # --- 末尾バッファのシェルプロンプト検出 ---
            cb = self._check_trailing_prompt(session_id, state)
            if cb is not None:
                callback_args_list.append(cb)

        # --- ロック外でコールバック発火 ---
        for args in callback_args_list:
            self._fire_callback(*args)

    # ------------------------------------------------------------------
    # Public API — Input processing
    # ------------------------------------------------------------------

    def feed_input(self, session_id: str, data: str) -> None:
        """Process raw PTY input data for *session_id*.

        Ctrl+C (``\\x03``) のカウントを管理し、エージェント固有の
        閾値に到達した場合にゲートを閉鎖する。

        Parameters
        ----------
        session_id : str
            Session identifier.
        data : str
            Raw input data sent to the PTY.
        """
        callback_args: tuple | None = None

        with self._lock:
            state = self._sessions.get(session_id)
            if state is None:
                return

            # Ctrl+C 以外の入力 → カウントリセット
            if "\x03" not in data:
                state.ctrlc_count = 0
                return

            # Ctrl+C を含む入力
            now = time.time()
            if now - state.ctrlc_last_time > self._ctrlc_window_sec:
                # ウィンドウ超過 → カウントリセット＋1
                state.ctrlc_count = 1
            else:
                state.ctrlc_count += 1
            state.ctrlc_last_time = now

            # ゲートが閉じている → 何もしない
            if state.agent_key is None:
                return

            # 閾値判定
            agent = AGENTS.get(state.agent_key)
            if agent is None:
                return
            threshold = agent.get("ctrlc_to_close", 2)
            if state.ctrlc_count >= threshold:
                callback_args = self._close_gate(session_id, state)

        # ロック外でコールバック発火
        if callback_args is not None:
            self._fire_callback(*callback_args)

    # ------------------------------------------------------------------
    # Public API — Query
    # ------------------------------------------------------------------

    def get_agent(self, session_id: str) -> str | None:
        """Return the active agent key for *session_id*.

        Returns
        -------
        str | None
            Agent key (e.g. ``"claude"``) if the gate is open,
            ``None`` otherwise.
        """
        with self._lock:
            state = self._sessions.get(session_id)
            if state is None:
                return None
            return state.agent_key

    def get_status(self, session_id: str) -> str:
        """Return the current detection status for *session_id*.

        Returns
        -------
        str
            One of ``"idle"``, ``"running"``, ``"waiting"``,
            or ``"error"``.  Defaults to ``"idle"`` for unknown sessions.
        """
        with self._lock:
            state = self._sessions.get(session_id)
            if state is None:
                return "idle"
            return state.status

    def get_agent_name(self, session_id: str) -> str | None:
        """Return the human-readable agent name for *session_id*.

        Returns
        -------
        str | None
            Display name (e.g. ``"Claude Code"``), or ``None`` if the
            gate is not open.
        """
        with self._lock:
            return self._get_agent_name_unlocked(session_id)

    def is_agent_at_prompt(self, session_id: str) -> bool:
        """AI ツールが自身のプロンプトにいるかどうかを返す。

        ゲートが開放中かつシェルに戻っていない場合に ``True``。
        rename コマンド等を AI ツールに送信可能かの判定に使用する。

        Returns
        -------
        bool
            AI ツールがプロンプトにいれば ``True``。
        """
        with self._lock:
            state = self._sessions.get(session_id)
            if state is None:
                return False
            return state.agent_key is not None and not state.exited_to_shell

    def set_external_source_active(
        self, session_id: str, active: bool
    ) -> None:
        """外部ステータス供給源の有効・無効を切り替える。

        有効の間、文言ベースの waiting/error 判定と出力スループットによる
        running 判定を抑止する。ゲート判定（どのツールか）は従来どおり
        PTY 出力から行うため影響しない。

        Parameters
        ----------
        session_id : str
            セッション ID。
        active : bool
            有効にするなら True。
        """
        with self._lock:
            state = self._sessions.setdefault(session_id, _SessionState())
            if state.external_active == active:
                return
            state.external_active = active
            logger.info(
                "セッション %s: 外部ステータス供給源を%s",
                session_id,
                "有効化" if active else "無効化",
            )

    def set_external_status(
        self, session_id: str, status: str, detail: str = ""
    ) -> None:
        """外部供給源から受け取ったステータスを反映する。

        重複するステータスは通知しない。``set_external_source_active`` で
        有効化されていないセッションに対しては何もしない（文言方式へ
        フォールバック中に外部の値が割り込まないようにするため）。

        Parameters
        ----------
        session_id : str
            セッション ID。
        status : str
            ``idle`` / ``running`` / ``waiting`` / ``error`` のいずれか。
        detail : str
            内訳などの補足文字列。通知本文とログに使う。
        """
        callback_args: tuple | None = None

        with self._lock:
            state = self._sessions.get(session_id)
            if state is None or not state.external_active:
                return

            dedup_key = f"external:{status}:{detail}"
            if dedup_key == state.last_emitted_key:
                return
            state.last_emitted_key = dedup_key

            old_status = state.status
            state.status = status
            agent_name = self._get_agent_name_unlocked(session_id)

            logger.info(
                "セッション %s: 外部供給源からステータス更新 — %s -> %s (%s)",
                session_id,
                old_status,
                status,
                detail or "内訳なし",
            )

            callback_args = (
                session_id, status, agent_name, detail, "external", None,
            )

        if callback_args is not None:
            self._fire_callback(*callback_args)

    def reset_session(self, session_id: str) -> None:
        """Close the gate and reset detection state for *session_id*.

        The session entry is preserved (with default values) so that
        subsequent ``feed()`` calls continue to work without needing a
        new registration step.

        Parameters
        ----------
        session_id : str
            Session identifier.
        """
        with self._lock:
            self._reset_session_unlocked(session_id)

    def remove_session(self, session_id: str) -> None:
        """Remove all tracking data for *session_id*.

        Parameters
        ----------
        session_id : str
            Session identifier.
        """
        with self._lock:
            state = self._sessions.pop(session_id, None)
            if state is not None:
                if state.debounce_timer is not None:
                    state.debounce_timer.cancel()
                logger.debug("セッション %s: 追跡データを削除", session_id)

    # ------------------------------------------------------------------
    # Internal — line processing
    # ------------------------------------------------------------------

    @staticmethod
    def _clean_line(raw_line: str) -> str:
        """ANSI 除去 + 単独 \\r の上書き処理 + strip。

        ``\\r\\n`` は ``feed()`` で事前に ``\\n`` へ正規化済みのため、
        ここで残る ``\\r`` は TUI の行上書き。最後の ``\\r`` 以降を残す。

        Gate / shell-prompt 検出など「現在画面に表示されている内容」を
        対象にする用途で使う。リングバッファ蓄積には ``_split_fragments``
        を使うこと。
        """
        stripped = strip_ansi(raw_line)
        # 単独 \r の上書き処理: 最後の \r 以降が画面表示内容
        cleaned = stripped.rsplit("\r", 1)[-1]
        return cleaned.strip()

    @staticmethod
    def _split_fragments(raw_line: str) -> list[str]:
        """ANSI 除去後、``\\r`` で分割した非空フラグメントのリストを返す。

        TUI の差分再描画では ``\\r`` で行頭に戻ってから新しい内容を
        上書きするため、``\\r`` 以前のフラグメントにも待機プロンプト等の
        重要なステータス文字列が含まれる可能性がある。``_clean_line`` の
        ように最後だけ残すと取り逃すため、各フラグメントを独立した
        「行」として扱うリングバッファ蓄積用。
        """
        stripped = strip_ansi(raw_line)
        return [f.strip() for f in stripped.split("\r") if f.strip()]

    def _resolve_gate_agent(
        self,
        session_id: str,
        state: _SessionState,
        line: str,
    ) -> str | None:
        """ゲート判定を行い、版の取り違えを pin で補正して返す。

        画面文言ベースのゲートは opencode / opencode2 のように版違いで
        共通のため、そのままでは取り違える。先に観測した起動コマンドの
        エコー（pin）が同じ PIN_GROUPS に属していれば、そちらを優先する。

        pin の観測はゲートが開くかどうかに関係なく毎行行う。エコー行と
        TUI バナー行が別の行で届くのが通常のため。

        ロック内で呼ばれる。

        Parameters
        ----------
        session_id : str
            セッション ID（ログ用）。
        state : _SessionState
            対象セッションの内部状態。
        line : str
            ANSI 除去済みの 1 行。

        Returns
        -------
        str | None
            採用するエージェントキー。ゲートが開かない場合は ``None``。
        """
        pinned = self._matcher.check_pin(line)
        if pinned is not None:
            state.pinned_agent_key = pinned

        agent_key = self._matcher.check_gate(line)
        if agent_key is None:
            return None

        pin = state.pinned_agent_key
        if (
            pin is not None
            and pin != agent_key
            and in_same_pin_group(pin, agent_key)
        ):
            logger.info(
                "セッション %s: ゲート判定を pin で補正 — %s -> %s",
                session_id,
                agent_key,
                pin,
            )
            return pin

        return agent_key

    def _process_line(
        self,
        session_id: str,
        state: _SessionState,
        raw_line: str,
    ) -> tuple | None:
        """Process a single complete line of terminal output.

        Applies the gate + idle detection logic:

        1. Gate check (all agents) -- only when gate is closed.
        2. Shell prompt check → idle で即時確定.
        3. Append cleaned line to ring buffer (recent_output).
        4. Duplicate suppression and callback args preparation.

        waiting/error のパターンマッチはここでは行わない。
        デバウンス時にリングバッファから判定する。

        ロック内で呼ばれる。コールバック引数を返し、ロック外で発火する。

        Returns
        -------
        tuple | None
            コールバック引数
            ``(session_id, status, agent_name, text, event_type,
            matched_pattern)``
            または None（コールバック不要の場合）
        """
        line = self._clean_line(raw_line)
        if not line:
            return None

        new_status: str | None = None
        matched_text: str | None = None
        matched_pattern: str | None = None
        event_type: str | None = None

        # --- Step 1: Gate check (gate closed) ----------------------------
        if state.agent_key is None:
            agent_key = self._resolve_gate_agent(session_id, state, line)
            if agent_key is not None:
                state.agent_key = agent_key
                state.status = "idle"
                state.exited_to_shell = False
                new_status = "idle"
                matched_text = line
                event_type = "gate_opened"
                logger.info(
                    "セッション %s: ゲートを開いた — agent=%s, line=%r",
                    session_id,
                    agent_key,
                    line,
                )
            # No gate match -> nothing more to do for this line
            if state.agent_key is None:
                return None

        # --- Step 1.5: シェル復帰後のツール乗り換え検出 -------------------
        # AI ツールを終了して同じタブで別のツールを起動した場合、
        # agent_key が古いままだと自動復元で誤ったコマンドを送ってしまう
        # （例: opencode を使った後に opencode2 を起動しても
        # ``opencode --continue`` が送られる）。
        #
        # 再判定はシェルへ戻った後（exited_to_shell）に限定する。
        # AI ツールの動作中に再判定すると、出力本文に含まれる他ツールの
        # 名前で乗り換えたと誤認するため。
        elif state.exited_to_shell:
            switched_key = self._resolve_gate_agent(session_id, state, line)
            if switched_key is not None:
                if switched_key != state.agent_key:
                    old_key = state.agent_key
                    state.agent_key = switched_key
                    state.status = "idle"
                    state.exited_to_shell = False
                    state.recent_output = []
                    state.recent_output_size = 0
                    state.output_start_time = None
                    state.output_bytes = 0
                    new_status = "idle"
                    matched_text = line
                    event_type = "agent_switched"
                    logger.info(
                        "セッション %s: ツール乗り換えを検出 — %s -> %s, "
                        "line=%r",
                        session_id,
                        old_key,
                        switched_key,
                        line,
                    )
                else:
                    # 同じツールを起動し直しただけ。ゲートは開いたまま、
                    # シェル復帰フラグのみ解除する。
                    state.exited_to_shell = False

        # --- Step 2: Shell prompt check → idle 即時確定 -------------------
        if event_type not in ("gate_opened", "agent_switched"):
            completed_pattern = self._matcher.check_completed(line)
            if completed_pattern is not None:
                new_status = "idle"
                matched_text = line
                matched_pattern = completed_pattern
                event_type = "shell_prompt"
                state.exited_to_shell = True

                # デバウンスタイマーをキャンセル
                if state.debounce_timer is not None:
                    state.debounce_timer.cancel()
                    state.debounce_timer = None
                state.output_start_time = None
                state.output_bytes = 0
                # リングバッファをクリア（前タスクの stale データで
                # デバウンス再発火時に waiting 誤検出するのを防止）
                state.recent_output = []
                state.recent_output_size = 0

                logger.info(
                    "セッション %s: シェルプロンプト検出 — idle, line=%r",
                    session_id,
                    line,
                )

        # --- Step 3: リングバッファに蓄積 ---------------------------------
        # ``\r`` で分割した全フラグメントを timestamp 付きで格納する。
        # サイズ超過時は先頭（古い）から pop。
        fragments = self._split_fragments(raw_line)
        if fragments:
            now = time.time()
            for frag in fragments:
                state.recent_output.append((now, frag))
                state.recent_output_size += len(frag) + 1
            while (
                state.recent_output_size > _RING_BUFFER_SIZE
                and state.recent_output
            ):
                _, popped = state.recent_output.pop(0)
                state.recent_output_size -= len(popped) + 1

        # Nothing detected on this line
        if new_status is None:
            return None

        # --- Step 4: Duplicate suppression --------------------------------
        agent_key_for_key = state.agent_key or ""
        if event_type == "shell_prompt":
            # シェルプロンプト → idle は固定デデュープキーを使用
            dedup_key = f"{agent_key_for_key}:idle:task_finished"
        elif event_type == "gate_opened":
            dedup_key = f"{agent_key_for_key}:idle:gate_opened"
        else:
            dedup_key = f"{agent_key_for_key}:{new_status}:{matched_text}"

        if dedup_key == state.last_emitted_key:
            logger.debug(
                "セッション %s: 重複抑制 — key=%s", session_id, dedup_key
            )
            return None

        state.last_emitted_key = dedup_key

        # Update internal status
        old_status = state.status
        state.status = new_status

        # --- Step 5: Prepare callback args --------------------------------
        agent_name = self._get_agent_name_unlocked(session_id)

        logger.info(
            "セッション %s: 状態変化 %s -> %s (agent=%s, text=%r, "
            "event=%s, pattern=%s)",
            session_id,
            old_status,
            new_status,
            agent_name,
            matched_text,
            event_type,
            matched_pattern,
        )

        return (
            session_id,
            new_status,
            agent_name,
            matched_text,
            event_type,
            matched_pattern,
        )

    # ------------------------------------------------------------------
    # Internal — trailing buffer inspection
    # ------------------------------------------------------------------

    def _check_trailing_prompt(
        self,
        session_id: str,
        state: _SessionState,
    ) -> tuple | None:
        """末尾バッファ（未改行）に対してゲート開放・シェルプロンプトを検出する。

        ゲートが閉じている場合:
            末尾バッファに対して ``check_gate`` を試行し、マッチすればゲートを
            開いて ``idle`` コールバックを発火。

        ゲートが開いている場合:
            ``check_trailing_prompt`` でシェルプロンプトを検出 → ``idle``
            （即時確定、event_type="shell_prompt"）

        ロック内で呼ばれる。コールバック引数を返す。

        Returns
        -------
        tuple | None
            コールバック引数、または None
        """
        if not state.line_buffer:
            return None

        line = self._clean_line(state.line_buffer)
        if not line:
            return None

        # --- Gate 未開放: gate パターンを試行 --------------------------------
        if state.agent_key is None:
            agent_key = self._resolve_gate_agent(session_id, state, line)
            if agent_key is None:
                return None

            dedup_key = f"{agent_key}:idle:gate_opened"
            if dedup_key == state.last_emitted_key:
                return None
            state.last_emitted_key = dedup_key

            state.agent_key = agent_key
            state.status = "idle"
            state.exited_to_shell = False
            agent_name = self._get_agent_name_unlocked(session_id)

            logger.info(
                "セッション %s: 末尾バッファからゲート開放 — "
                "agent=%s (text=%r)",
                session_id,
                agent_name,
                line[:200],
            )

            return (
                session_id, "idle", agent_name, line[:200], "gate_opened",
                None,
            )

        # --- Gate 開放中: shell prompt (idle) を試行 -------------------------
        trailing_pattern = self._matcher.check_trailing_prompt(line)
        if trailing_pattern is not None:
            agent_key_for_key = state.agent_key or ""
            dedup_key = f"{agent_key_for_key}:idle:task_finished"
            if dedup_key == state.last_emitted_key:
                return None
            state.last_emitted_key = dedup_key

            old_status = state.status
            state.status = "idle"
            state.exited_to_shell = True

            # デバウンスタイマーをキャンセル
            if state.debounce_timer is not None:
                state.debounce_timer.cancel()
                state.debounce_timer = None
            state.output_start_time = None
            state.output_bytes = 0
            # リングバッファをクリア（前タスクの stale データで
            # デバウンス再発火時に waiting 誤検出するのを防止）
            state.recent_output = []
            state.recent_output_size = 0

            agent_name = self._get_agent_name_unlocked(session_id)

            logger.info(
                "セッション %s: 末尾バッファから idle 検出 — "
                "%s -> idle (agent=%s, text=%r)",
                session_id,
                old_status,
                agent_name,
                line,
            )

            return (
                session_id, "idle", agent_name, line, "shell_prompt",
                trailing_pattern,
            )

        return None

    # ------------------------------------------------------------------
    # Internal — timer callbacks
    # ------------------------------------------------------------------

    def _on_debounce_settled(self, session_id: str) -> None:
        """デバウンスタイマー満了時のコールバック（出力が一定時間停止）。

        リングバッファ（recent_output）を行分割し、各行に対して
        ステータスパターンマッチを実行する。マッチすれば waiting/error
        で確定。マッチしなければ、末尾バッファ（line_buffer）も確認
        した上で、running なら idle に遷移する。

        タイマースレッドから呼ばれる。
        """
        callback_args: tuple | None = None

        with self._lock:
            state = self._sessions.get(session_id)
            if state is None:
                return

            # ゲートが閉じている → 何もしない
            if state.agent_key is None:
                return

            # 外部供給源が有効な間は、文言ベースの判定を行わない
            if state.external_active:
                state.output_start_time = None
                state.output_bytes = 0
                return

            agent_key_for_key = state.agent_key or ""

            # --- 同時出現（AND 条件）判定を最優先 ---
            # 「2 つのボタンが同時に表示されている」という具体的な条件は、
            # 単独語の一致より確度が高いため先に評価する。
            result = self._check_status_combo(state)

            # --- リングバッファからステータスパターン判定 ---
            if result is None:
                result = self._check_ring_buffer(state)

            # --- リングバッファで見つからなければ末尾バッファも確認 ---
            if result is None:
                trailing = self._clean_line(state.line_buffer)
                if trailing and state.agent_key:
                    result = self._matcher.check_status(
                        trailing, state.agent_key
                    )

            if result is not None:
                detected, detected_pattern = result
                matched_text = detected  # ステータス名をテキストとして使用
                # リングバッファからマッチした行を特定するために
                # 再度検索して実際のテキストを取得
                matched_text = self._find_matched_text(
                    state, detected_pattern
                )

                dedup_key = (
                    f"{agent_key_for_key}:{detected}:{matched_text[:200]}"
                )
                if dedup_key != state.last_emitted_key:
                    state.last_emitted_key = dedup_key
                    old_status = state.status
                    state.status = detected
                    agent_name = self._get_agent_name_unlocked(session_id)

                    logger.info(
                        "セッション %s: デバウンス確定 — %s -> %s "
                        "(agent=%s, text=%r, pattern=%s)",
                        session_id,
                        old_status,
                        detected,
                        agent_name,
                        matched_text[:200],
                        detected_pattern,
                    )

                    callback_args = (
                        session_id,
                        detected,
                        agent_name,
                        matched_text[:200],
                        "debounce",
                        detected_pattern,
                    )
            else:
                # パターン不一致
                if state.status == "running":
                    # running → idle（処理が終わった）
                    dedup_key = (
                        f"{agent_key_for_key}:idle:task_finished"
                    )
                    if dedup_key != state.last_emitted_key:
                        state.last_emitted_key = dedup_key
                        old_status = state.status
                        state.status = "idle"
                        agent_name = self._get_agent_name_unlocked(
                            session_id
                        )

                        logger.info(
                            "セッション %s: デバウンス確定"
                            "（パターンなし）— %s -> idle "
                            "(agent=%s)",
                            session_id,
                            old_status,
                            agent_name,
                        )

                        callback_args = (
                            session_id,
                            "idle",
                            agent_name,
                            "",
                            "debounce",
                            None,
                        )
                # else: error / idle → 現状維持（何もしない）

            # 発火後のリセット
            state.output_start_time = None
            state.output_bytes = 0

        # ロック外でコールバック発火
        if callback_args is not None:
            self._fire_callback(*callback_args)

    def _check_ring_buffer(
        self, state: _SessionState
    ) -> tuple[str, str] | None:
        """リングバッファを逆順走査し、ステータスパターンを検索する。

        末尾（最新）から逆順に走査し、最初にマッチした結果を返す。
        最新の出力ほど現在の画面表示に近く、ステータス判定に適するため。

        年齢フィルタ: ``debounce_sec * 2`` 秒より古いエントリは
        走査対象外とする。リングバッファ拡張（4KB）後に旧タスクの
        stale プロンプトが残ってしまっても、デバウンス再発火時に
        false-positive で waiting 検出することを防ぐ。
        エントリは時系列順に append されるため、逆順走査中に古いものに
        当たった時点で ``break`` してよい（それ以前は更に古い）。

        Returns
        -------
        tuple[str, str] | None
            ``(status, pattern)`` on match, or ``None``.
        """
        if not state.recent_output or state.agent_key is None:
            return None

        now = time.time()
        age_limit = self._debounce_sec * 2

        for ts, line in reversed(state.recent_output):
            if now - ts > age_limit:
                break
            if not line:
                continue
            result = self._matcher.check_status(line, state.agent_key)
            if result is not None:
                return result

        return None

    def _recent_window(self, state: _SessionState) -> str:
        """直近出力ウィンドウ（リングバッファ＋末尾バッファ）を連結して返す。

        ``_check_ring_buffer`` と同じ年齢フィルタ（``debounce_sec * 2``）を
        適用し、古いエントリは含めない。時系列順に改行で連結する。

        画面上に同時表示されている内容をひとまとまりのテキストとして
        扱うためのもので、``check_status_combo`` の入力に使う。
        """
        now = time.time()
        age_limit = self._debounce_sec * 2

        fragments: list[str] = []
        for ts, frag in reversed(state.recent_output):
            if now - ts > age_limit:
                break
            if frag:
                fragments.append(frag)
        fragments.reverse()  # 逆順走査したので時系列順へ戻す

        trailing = self._clean_line(state.line_buffer)
        if trailing:
            fragments.append(trailing)

        return "\n".join(fragments)

    def _check_status_combo(
        self, state: _SessionState
    ) -> tuple[str, str] | None:
        """直近出力ウィンドウに対する AND 条件のステータス判定。

        opencode の権限確認ダイアログのように、複数のボタン文言が
        同時に表示されている場合を waiting と判定するために使う。
        ``status_combo_patterns`` 未定義のエージェントでは常に ``None``。

        Returns
        -------
        tuple[str, str] | None
            ``(status, pattern)`` on match, or ``None``.
        """
        if state.agent_key is None:
            return None

        window = self._recent_window(state)
        if not window:
            return None

        return self._matcher.check_status_combo(window, state.agent_key)

    def _find_matched_text(
        self, state: _SessionState, pattern: str
    ) -> str:
        """リングバッファ + 末尾バッファから、パターンにマッチした行を返す。"""
        import re

        try:
            compiled = re.compile(pattern)
        except re.error:
            return ""

        # リングバッファの各フラグメントを逆順に確認（最新を優先）
        for _, line in reversed(state.recent_output):
            if line and compiled.search(line):
                return line

        # 末尾バッファ
        trailing = self._clean_line(state.line_buffer)
        if trailing and compiled.search(trailing):
            return trailing

        # 同時出現条件（status_combo_patterns）はフラグメントをまたいで成立
        # し得るため、個々の断片には一致しないことがある。その場合に空文字を
        # 返すと、ログが空になるうえ重複抑制キーが
        # ``<agent>:<status>:`` に潰れて後続の別ダイアログの通知を
        # 取りこぼすため、連結ウィンドウから一致箇所を取り出す。
        window = self._recent_window(state)
        if window:
            hit = compiled.search(window)
            if hit:
                # フラグメント境界の改行を含み得るので空白を畳む
                return " ".join(hit.group(0).split())

        return ""

    # ------------------------------------------------------------------
    # Internal — throughput-based running detection
    # ------------------------------------------------------------------

    def _check_running_by_throughput(
        self,
        session_id: str,
        state: _SessionState,
    ) -> tuple | None:
        """出力スループットに基づく running 検出。

        ロック内で呼ばれる。コールバック引数を返す。

        Returns
        -------
        tuple | None
            コールバック引数、または None
        """
        # ゲートが閉じている → 何もしない
        if state.agent_key is None:
            return None

        # 外部供給源が有効な間は、出力量からの running 判定を行わない
        if state.external_active:
            return None

        # 出力開始時刻が未設定 → 何もしない
        if state.output_start_time is None:
            return None

        # 閾値の選択。復帰時はエージェント別設定ではなく全体設定を使う
        # （待ち状態から抜けたことを素早く反映するための短い値のため）。
        if state.status == "error":
            threshold = self._error_recovery_sec
        elif state.status == "waiting":
            threshold = self._waiting_recovery_sec
        else:
            threshold = (
                get_running_threshold_ms(
                    state.agent_key,
                    int(self._running_threshold_sec * 1000),
                )
                / 1000.0
            )

        elapsed = time.time() - state.output_start_time
        if elapsed < threshold:
            return None

        # 出力レートの下限（エージェント任意設定）。
        # 継続時間だけで判定すると、TUI の入力欄でタイプしている間の
        # 再描画まで running と見なしてしまう。閾値を下げたエージェントでは
        # レートを併用して「人が打っている」と「AI が生成している」を分ける。
        min_cps = get_running_min_cps(state.agent_key)
        if min_cps > 0 and elapsed > 0:
            cps = state.output_bytes / elapsed
            if cps < min_cps:
                logger.debug(
                    "セッション %s: 出力レートが下限未満のため running 抑止 "
                    "(%.0f < %d 文字/秒)",
                    session_id,
                    cps,
                    min_cps,
                )
                return None

        # 既に running → 重複防止
        if state.status == "running":
            return None

        # ダイアログが同時表示されている間は running に遷移させない。
        # 画面再描画で出力が続いていても実態はユーザー入力待ちであり、
        # waiting はデバウンス確定時に発火する。
        combo = self._check_status_combo(state)
        if combo is not None:
            logger.debug(
                "セッション %s: 同時出現条件が成立中のため "
                "running 遷移を抑止 (status=%s, pattern=%s)",
                session_id,
                combo[0],
                combo[1],
            )
            return None

        # → running に遷移
        old_status = state.status
        state.status = "running"

        agent_key_for_key = state.agent_key or ""
        dedup_key = f"{agent_key_for_key}:running:throughput"
        state.last_emitted_key = dedup_key

        agent_name = self._get_agent_name_unlocked(session_id)

        logger.info(
            "セッション %s: 出力スループットにより running 検出 — "
            "%s -> running (agent=%s, 経過=%.1f秒)",
            session_id,
            old_status,
            agent_name,
            elapsed,
        )

        return (session_id, "running", agent_name, "", "throughput", None)

    # ------------------------------------------------------------------
    # Internal — gate closure
    # ------------------------------------------------------------------

    def _close_gate(
        self,
        session_id: str,
        state: _SessionState,
    ) -> tuple | None:
        """Ctrl+C 閾値到達によるゲート閉鎖。

        ロック内で呼ばれる。コールバック引数を返す。

        Returns
        -------
        tuple | None
            コールバック引数 (session_id, "idle", None, "", "gate_closed")
        """
        # デバウンスタイマーをキャンセル
        if state.debounce_timer is not None:
            state.debounce_timer.cancel()
            state.debounce_timer = None

        logger.info(
            "セッション %s: Ctrl+C 閾値到達によりゲート閉鎖 "
            "(agent=%s, count=%d)",
            session_id,
            state.agent_key,
            state.ctrlc_count,
        )

        # コールバック引数を準備（ロック外で発火する）
        callback_args = (session_id, "idle", None, "", "gate_closed", None)

        # 内部状態をリセット
        self._reset_session_unlocked(session_id)

        return callback_args

    # ------------------------------------------------------------------
    # Internal — helpers
    # ------------------------------------------------------------------

    def _get_agent_name_unlocked(self, session_id: str) -> str | None:
        """ロック保持下で agent_name を取得する。

        get_agent_name() のロックなし版。ロック内から呼ぶ。
        """
        state = self._sessions.get(session_id)
        if state is None or state.agent_key is None:
            return None
        agent = AGENTS.get(state.agent_key)
        return agent["name"] if agent else None

    def _reset_session_unlocked(self, session_id: str) -> None:
        """ロック保持下でセッション状態をリセットする。

        reset_session() のロックなし版。ロック内から呼ぶ。
        """
        state = self._sessions.get(session_id)
        if state is None:
            return
        # デバウンスタイマーをキャンセル
        if state.debounce_timer is not None:
            state.debounce_timer.cancel()
            state.debounce_timer = None
        state.agent_key = None
        state.pinned_agent_key = None
        state.external_active = False
        state.status = "idle"
        state.exited_to_shell = False
        state.last_emitted_key = ""
        state.line_buffer = ""
        state.recent_output = []
        state.recent_output_size = 0
        state.output_start_time = None
        state.output_bytes = 0
        state.last_output_time = 0.0
        state.ctrlc_count = 0
        state.ctrlc_last_time = 0.0
        logger.info("セッション %s: ゲートを閉じ、状態をリセット", session_id)

    def _fire_callback(
        self,
        session_id: str,
        status: str,
        agent_name: str | None,
        matched_text: str | None,
        event_type: str | None = None,
        matched_pattern: str | None = None,
    ) -> None:
        """ロック外でコールバックを発火する。"""
        if self._on_status_change is not None:
            try:
                self._on_status_change(
                    session_id,
                    status,
                    agent_name,
                    matched_text,
                    event_type,
                    matched_pattern,
                )
            except Exception:
                logger.exception(
                    "on_status_change コールバックで例外が発生 (session=%s)",
                    session_id,
                )
