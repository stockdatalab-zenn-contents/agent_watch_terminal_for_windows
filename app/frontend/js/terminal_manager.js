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

// theme は既定値の複製を持つ。CATPPUCCIN_MOCHA_THEME 側は
// 「有効なキー一覧」と「未設定時の既定値」の参照元として不変に保つ。
const TERMINAL_OPTIONS = {
  theme:             Object.assign({}, CATPPUCCIN_MOCHA_THEME),
  fontFamily:        "'Consolas', 'Segoe UI Emoji', 'Courier New', monospace",
  fontSize:          14,
  // 初期寸法。PTY 側（session_manager._spawn_pty）と同じ値を使う。
  // xterm.js の既定は 80x24 で PTY の既定と食い違うため明示する。
  // 非アクティブセッションは切り替えるまで fit されず、食い違ったままだと
  // PSReadLine が PTY の桁数前提で出すカーソル復帰がずれて表示が崩れる。
  // settings.json の terminal.initial_cols / initial_rows で上書きできる。
  cols:              120,
  rows:              30,
  cursorStyle:       'bar',
  cursorBlink:       true,
  scrollback:        5000,
  allowTransparency: true,
  convertEol:        true,
};

/* ------------------------------------------------------------------ */
/*  マウス報告の判別                                                   */
/* ------------------------------------------------------------------ */

// SGR 形式（DECSET 1006）のマウス報告。
// 例: ESC[<0;12;3M（押下） / ESC[<64;12;3M（ホイール上）
// AI ツールは 3 種とも 1006 を要求するため、実際に流れるのはこの形式のみ。
// 1006 を使わない場合は onBinary 経由になり、awt は転送していない。
const MOUSE_REPORT_RE = /^\x1b\[<\d+;\d+;\d+[Mm]$/;

/* ------------------------------------------------------------------ */
/*  TerminalManager                                                   */
/* ------------------------------------------------------------------ */

const TerminalManager = {

  /** @type {Object.<string, {term: Terminal, fitAddon: FitAddon, container: HTMLElement}>} */
  terminals: {},

  /** Shift を押しながらマウスを操作している間だけ true。 */
  _mouseShiftHeld: false,

  /* ---- applySettings -------------------------------------------- */

  /**
   * settings.json の terminal セクションを TERMINAL_OPTIONS に反映する。
   * createTerminal() より前に呼び出すこと。
   *
   * 値の検証は緩めに行い、型が合わない項目は既定値のまま残す。
   * xterm.js は不正なオプションで例外を投げるため、
   * 設定ファイルの記述ミスでアプリ全体が起動不能になるのを避ける。
   *
   * @param {object} termSettings - settings.terminal オブジェクト
   */
  applySettings(termSettings) {
    if (!termSettings) {
      this._syncCursorCssVars();
      return;
    }
    if (typeof termSettings.font_family === 'string' && termSettings.font_family) {
      TERMINAL_OPTIONS.fontFamily = termSettings.font_family;
    }
    // 0 以下だと xterm.js のセル寸法計算が破綻するため下限を設ける。
    if (typeof termSettings.font_size === 'number' && termSettings.font_size > 0) {
      TERMINAL_OPTIONS.fontSize = termSettings.font_size;
    }
    if (typeof termSettings.scrollback === 'number') {
      TERMINAL_OPTIONS.scrollback = termSettings.scrollback;
    }
    // 初期寸法。PTY 側も同じ設定値を読むため、ここで揃えておく。
    // 0 以下だと xterm.js が例外を投げるため下限を設ける。
    if (typeof termSettings.initial_cols === 'number' && termSettings.initial_cols > 0) {
      TERMINAL_OPTIONS.cols = termSettings.initial_cols;
    }
    if (typeof termSettings.initial_rows === 'number' && termSettings.initial_rows > 0) {
      TERMINAL_OPTIONS.rows = termSettings.initial_rows;
    }
    if (typeof termSettings.cursor_blink === 'boolean') {
      TERMINAL_OPTIONS.cursorBlink = termSettings.cursor_blink;
    }
    // カーソル形状。bar は 1px の縦線で視認しづらいため、
    // block / underline へ切り替えられるようにする。
    // 未知の値は xterm.js が例外を投げるため、既知の 3 種のみ受け付ける。
    if (['bar', 'block', 'underline'].indexOf(termSettings.cursor_style) !== -1) {
      TERMINAL_OPTIONS.cursorStyle = termSettings.cursor_style;
    }
    if (typeof termSettings.allow_transparency === 'boolean') {
      TERMINAL_OPTIONS.allowTransparency = termSettings.allow_transparency;
    }
    this._applyTheme(termSettings.theme);
    this._syncCursorCssVars();
  },

  /**
   * settings.json の theme（snake_case）を xterm.js の ITheme（camelCase）へ
   * 変換して TERMINAL_OPTIONS.theme に上書きする。
   *
   * 例: cursor_accent -> cursorAccent、bright_black -> brightBlack
   *
   * CATPPUCCIN_MOCHA_THEME に存在するキーだけを受け付け、
   * 未知のキーや文字列以外の値は無視する。
   *
   * @param {object|undefined} themeSettings - settings.terminal.theme オブジェクト
   */
  _applyTheme(themeSettings) {
    if (!themeSettings || typeof themeSettings !== 'object') return;

    Object.keys(themeSettings).forEach(function (key) {
      var camel = key.replace(/_([a-z])/g, function (_m, c) {
        return c.toUpperCase();
      });
      if (!Object.prototype.hasOwnProperty.call(CATPPUCCIN_MOCHA_THEME, camel)) return;
      if (typeof themeSettings[key] !== 'string' || !themeSettings[key]) return;
      TERMINAL_OPTIONS.theme[camel] = themeSettings[key];
    });
  },

  /**
   * カーソル配色を CSS カスタムプロパティへ反映する。
   *
   * terminal.css は truecolor セルの inline style に勝つため
   * block カーソルの配色を !important で指定しており、その色を
   * settings.json の theme.cursor / theme.cursor_accent と揃える必要がある。
   * CSS 側にも既定値を持たせてあるため、ここで未設定でも破綻しない。
   */
  _syncCursorCssVars() {
    var root = document.documentElement;
    root.style.setProperty('--terminal-cursor', TERMINAL_OPTIONS.theme.cursor);
    root.style.setProperty('--terminal-cursor-accent', TERMINAL_OPTIONS.theme.cursorAccent);
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

    // 3.5. TUI が要求するモードはすべて通す
    //
    // Alternate screen buffer (1049, 47, 1047):
    //   ブロックすると TUI の全画面描画が通常バッファへ流れ込む。
    //   TUI は画面消去（ESC[2J / ESC[K）を出さず代替画面の切替に
    //   任せる設計のため、前のフレームが残る・横幅変更時の reflow で
    //   行がずれる・終了後もフレームが残る、という崩れがそのまま
    //   scrollback へ焼き付く。代替画面は scrollback を持たないので、
    //   通せば履歴を一切汚さない。
    //
    // Mouse tracking (1000, 1002, 1003):
    //   ブロックすると TUI がホイールを受け取れない。代替画面では
    //   xterm.js がホイールをカーソルキー（ESC[A / ESC[B）へ変換して
    //   PTY へ送るため、TUI はそれをチャット欄の操作として扱い、
    //   出力欄が動かなくなる。この変換は xterm.js 5.3.0 に
    //   ハードコードされておりオプションで無効化できない。
    //   通せば TUI が本物のホイール報告を受け取り自前でスクロールする。
    //
    //   代償として、左ドラッグはマウストラッキングに消費される。
    //   テキスト選択は Shift + ドラッグで行う（xterm.js の
    //   shouldForceSelection が Windows では shiftKey を見るため）。
    //   右クリックによるコピー・ペーストは影響を受けない
    //   （xterm.js の contextmenu ハンドラーは preventDefault せず、
    //   context_menu.js は祖先要素で拾っている）。

    // 3.6. 代替画面バッファへの取り残し対策
    this._installAltScreenGuard(term);

    // 3.7. Shift+ドラッグを選択専用にする
    this._trackShiftDrag(container);

    // 4. Mount into DOM
    term.open(container);
    fitAddon.fit();

    // 5. Keyboard input -> Python backend
    var self = this;
    term.onData(async function (data) {
      // Shift 押下中のマウス報告は AI ツールへ渡さない
      if (self._isSuppressedMouseReport(data)) return;
      if (window.pywebview && window.pywebview.api) {
        await window.pywebview.api.send_input(sessionId, data);
      }
    });

    // 6. Store
    this.terminals[sessionId] = { term: term, fitAddon: fitAddon, serializeAddon: serializeAddon, container: container };

    return term;
  },

  /* ---- Shift+ドラッグの選択 (private) ------------------------------ */

  /**
   * Shift を押しながらのマウス操作かどうかを記録する。
   *
   * マウストラッキングが有効なとき、xterm.js の選択サービスは無効化され、
   * 押下時のみ shouldForceSelection（Windows では shiftKey）で選択を
   * 強制できる。ところがドラッグ中の移動報告は Shift を考慮しない。
   *
   *   mousedrag: e => { e.buttons && i(e) }   // DRAG / ANY で登録
   *   mousemove: e => { e.buttons || i(e) }   // ANY で登録
   *
   * そのため Shift+ドラッグで選択している最中も AI ツールへ報告が届き、
   * ツール側が反応して再描画すると選択が壊れる。ここで Shift の状態を
   * 拾い、onData 側で報告を落とす。
   *
   * @param {HTMLElement} container - ターミナルのラッパー要素
   */
  _trackShiftDrag(container) {
    var self = this;
    var update = function (e) {
      self._mouseShiftHeld = e.shiftKey === true;
    };
    // xterm.js のリスナーより先に状態を更新するためキャプチャで拾う。
    ['mousedown', 'mousemove', 'mouseup'].forEach(function (type) {
      container.addEventListener(type, update, true);
    });
  },

  /**
   * PTY へ送らずに捨てるべきマウス報告かどうかを返す。
   *
   * マウス報告は triggerMouseEvent -> triggerDataEvent -> onData を
   * 通るため、ここで落とせば PTY へ届かない。Shift を押していない
   * 通常のマウス操作（ホイールでの出力欄スクロール等）は素通しする。
   *
   * @param {string} data - onData が渡す入力データ
   * @returns {boolean} 捨てるなら true
   */
  _isSuppressedMouseReport(data) {
    return this._mouseShiftHeld && MOUSE_REPORT_RE.test(data);
  },

  /* ---- TUI モード制御 (private) ----------------------------------- */

  /**
   * 代替画面バッファへ取り残された場合の保険を登録する。
   *
   * TUI が ESC[?1049l を出さないまま落ちる（強制終了・クラッシュ等）と、
   * 通常バッファへ戻れず scrollback が見えないままになる。
   * awt はシェルのプロンプト表示のたびに OSC 7 を注入しているため、
   * OSC 7 の到着をもって「シェルへ戻った」とみなし強制復帰させる。
   * TUI 自身は OSC 7 を出さないので誤爆しない。
   *
   * 復帰の書込みはキューに積まれるため、同じチャンクに続くプロンプト
   * 文字列は代替画面側へ描かれて表示には残らない。TUI が異常終了した
   * ときだけ通る保険であり、次のプロンプト以降は通常どおり描画される。
   *
   * @param {Terminal} term
   */
  _installAltScreenGuard(term) {
    term.parser.registerOscHandler(7, function () {
      if (term.buffer.active.type === 'alternate') {
        term.write('\x1b[?1049l');
      }
      // cwd 通知としての既定処理は妨げない
      return false;
    });
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
        // 代替画面（TUI の一時的な描画内容）は保存しない。
        // 復元したいのは通常バッファ側のシェル履歴であり、TUI は
        // セッション再開時に自分で描き直すため。
        // モード設定も復元対象から外し、マウストラッキング等が
        // 書き戻されるのを防ぐ。
        result[id] = self.terminals[id].serializeAddon.serialize({
          excludeAltBuffer: true,
          excludeModes: true,
        });
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
