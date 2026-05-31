/**
 * terminal_manager.js -- xterm.js Terminal instance manager
 *
 * Responsibility:
 *   - Create / destroy / show xterm.js Terminal instances (one per session)
 *   - Manage FitAddon and WebLinksAddon per terminal
 *   - Forward keyboard input to Python backend via pywebview API
 *   - Notify backend of terminal resize events
 *
 * Dependencies:
 *   - xterm.js (Terminal)
 *   - xterm-addon-fit (FitAddon)
 *   - xterm-addon-web-links (WebLinksAddon)
 *   - AppState (app.js) -- referenced for active session in fitAll()
 *   - pywebview API -- send_input(), resize_terminal()
 */

/* ------------------------------------------------------------------ */
/*  Catppuccin Mocha theme                                            */
/* ------------------------------------------------------------------ */

const CATPPUCCIN_MOCHA_THEME = {
  background:          '#1e1e2e',
  foreground:          '#cdd6f4',
  cursor:              '#f5e0dc',
  cursorAccent:        '#1e1e2e',
  selectionBackground: 'rgba(88, 91, 112, 0.5)',
  black:               '#45475a',
  red:                 '#f38ba8',
  green:               '#a6e3a1',
  yellow:              '#f9e2af',
  blue:                '#89b4fa',
  magenta:             '#f5c2e7',
  cyan:                '#94e2d5',
  white:               '#bac2de',
  brightBlack:         '#585b70',
  brightRed:           '#f38ba8',
  brightGreen:         '#a6e3a1',
  brightYellow:        '#f9e2af',
  brightBlue:          '#89b4fa',
  brightMagenta:       '#f5c2e7',
  brightCyan:          '#94e2d5',
  brightWhite:         '#a6adc8',
};

/* ------------------------------------------------------------------ */
/*  Terminal defaults                                                 */
/* ------------------------------------------------------------------ */

const TERMINAL_OPTIONS = {
  theme:             CATPPUCCIN_MOCHA_THEME,
  fontFamily:        "'Consolas', 'Segoe UI Emoji', 'Courier New', monospace",
  fontSize:          14,
  cursorStyle:       'bar',
  cursorBlink:       true,
  scrollback:        5000,
  allowTransparency: true,
  convertEol:        true,
};

/* ------------------------------------------------------------------ */
/*  TerminalManager                                                   */
/* ------------------------------------------------------------------ */

const TerminalManager = {

  /** @type {Object.<string, {term: Terminal, fitAddon: FitAddon, container: HTMLElement}>} */
  terminals: {},

  /* ---- applySettings -------------------------------------------- */

  /**
   * settings.json の terminal セクションを TERMINAL_OPTIONS に反映する。
   * createTerminal() より前に呼び出すこと。
   *
   * @param {object} termSettings - settings.terminal オブジェクト
   */
  applySettings(termSettings) {
    if (!termSettings) return;
    if (typeof termSettings.scrollback === 'number') {
      TERMINAL_OPTIONS.scrollback = termSettings.scrollback;
    }
    if (typeof termSettings.cursor_blink === 'boolean') {
      TERMINAL_OPTIONS.cursorBlink = termSettings.cursor_blink;
    }
    if (typeof termSettings.allow_transparency === 'boolean') {
      TERMINAL_OPTIONS.allowTransparency = termSettings.allow_transparency;
    }
  },

  /* ---- create ---------------------------------------------------- */

  /**
   * Create and mount a new xterm.js Terminal for the given session.
   *
   * @param {string} sessionId - Unique session identifier.
   * @returns {Terminal} The created xterm.js Terminal instance.
   */
  createTerminal(sessionId) {
    // 1. Container div
    const container = document.createElement('div');
    container.className = 'terminal-wrapper hidden';
    container.id = 'terminal-' + sessionId;
    document.getElementById('terminal-container').appendChild(container);

    // 2. Terminal instance
    const term = new Terminal(TERMINAL_OPTIONS);

    // 3. Addons
    const fitAddon = new FitAddon.FitAddon();
    term.loadAddon(fitAddon);

    const webLinksAddon = new WebLinksAddon.WebLinksAddon();
    term.loadAddon(webLinksAddon);

    const serializeAddon = new SerializeAddon.SerializeAddon();
    term.loadAddon(serializeAddon);

    // 3.5. TUI エスケープシーケンスのブロック
    // モニタリング用途では scrollback 確保とテキスト選択を優先し、
    // TUI が要求する以下のモードをブロックする。
    //
    // Alternate screen buffer (1049, 47, 1047):
    //   Copilot CLI 等が切り替え後に復帰しない → scrollback 消失 →
    //   カスタムスクロールバーが非表示になる問題を防止。
    //
    // Mouse tracking (1000, 1002, 1003):
    //   Copilot CLI が [?1002h] で有効化 → 左クリック+ドラッグが
    //   xterm.js の selection ではなくマウストラッキングに消費される →
    //   term.getSelection() が常に空 → 右クリックコピーが動作しない
    //   問題を防止。tracking 無効時は右クリックもマウスシーケンスとして
    //   PTY に送信されないため、contextmenu ハンドラーとの二重動作も解消。
    var blockedModes = [1049, 47, 1047, 1000, 1002, 1003];
    term.parser.registerCsiHandler({ final: 'h', prefix: '?' }, function (params) {
      for (var i = 0; i < params.length; i++) {
        if (blockedModes.indexOf(params[i]) !== -1) return true;
      }
      return false;
    });
    term.parser.registerCsiHandler({ final: 'l', prefix: '?' }, function (params) {
      for (var i = 0; i < params.length; i++) {
        if (blockedModes.indexOf(params[i]) !== -1) return true;
      }
      return false;
    });

    // 4. Mount into DOM
    term.open(container);
    fitAddon.fit();

    // 5. Keyboard input -> Python backend
    term.onData(async function (data) {
      if (window.pywebview && window.pywebview.api) {
        await window.pywebview.api.send_input(sessionId, data);
      }
    });

    // 6. Store
    this.terminals[sessionId] = { term: term, fitAddon: fitAddon, serializeAddon: serializeAddon, container: container };

    return term;
  },

  /* ---- destroy --------------------------------------------------- */

  /**
   * Dispose a Terminal instance and remove its container from the DOM.
   *
   * @param {string} sessionId - Session to destroy.
   */
  destroyTerminal(sessionId) {
    var entry = this.terminals[sessionId];
    if (!entry) return;

    entry.term.dispose();
    entry.container.remove();
    delete this.terminals[sessionId];
  },

  /* ---- get ------------------------------------------------------- */

  /**
   * Retrieve the Terminal instance for a session.
   *
   * @param {string} sessionId
   * @returns {Terminal|null}
   */
  getTerminal(sessionId) {
    var entry = this.terminals[sessionId];
    return entry ? entry.term : null;
  },

  /* ---- show / hide ----------------------------------------------- */

  /**
   * Show the terminal for the given session and hide all others.
   * Triggers a fit + resize notification after the container becomes visible.
   *
   * @param {string} sessionId - Session to display.
   */
  showTerminal(sessionId) {
    var self = this;
    Object.keys(this.terminals).forEach(function (id) {
      var entry = self.terminals[id];
      if (id === sessionId) {
        entry.container.classList.remove('hidden');
        // fit after DOM reflow (container must be visible)
        setTimeout(function () {
          entry.fitAddon.fit();
          self._notifyResize(id, entry);
        }, 10);
      } else {
        entry.container.classList.add('hidden');
      }
    });
  },

  /* ---- fitAll ---------------------------------------------------- */

  /**
   * Fit the currently active (visible) terminal to its container.
   * Called by ResizeHandler on window resize.
   */
  fitAll() {
    var activeId = AppState.activeSessionId;
    if (!activeId || !this.terminals[activeId]) return;

    var entry = this.terminals[activeId];
    if (entry.container.classList.contains('hidden')) return;

    entry.fitAddon.fit();
    this._notifyResize(activeId, entry);
  },

  /* ---- serialize / restore ---------------------------------------- */

  /**
   * Serialize all terminal buffers into an object.
   *
   * @returns {Object.<string, string>} sessionId -> serialized buffer string
   */
  serializeAll() {
    var result = {};
    var self = this;
    Object.keys(this.terminals).forEach(function (id) {
      try {
        result[id] = self.terminals[id].serializeAddon.serialize();
      } catch (e) {
        // skip terminals that fail to serialize
      }
    });
    return result;
  },

  /**
   * Write previously serialized buffer content into a terminal.
   *
   * @param {string} sessionId
   * @param {string} data - Serialized buffer string from SerializeAddon.
   */
  restoreBuffer(sessionId, data) {
    var entry = this.terminals[sessionId];
    if (entry && data) {
      entry.term.write(data);
    }
  },

  /* ---- resize notification (private) ----------------------------- */

  /**
   * Notify the Python backend of terminal column/row changes.
   *
   * @param {string} sessionId
   * @param {{term: Terminal}} entry
   */
  async _notifyResize(sessionId, entry) {
    var cols = entry.term.cols;
    var rows = entry.term.rows;
    if (window.pywebview && window.pywebview.api) {
      await window.pywebview.api.resize_terminal(sessionId, cols, rows);
    }
  },
};
