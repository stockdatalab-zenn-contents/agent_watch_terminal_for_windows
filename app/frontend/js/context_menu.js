/**
 * context_menu.js -- Right-click context menu handler
 *
 * Responsibility:
 *   - Right-click copy/paste in the terminal
 *   - Text selected  -> copy to clipboard, clear selection
 *   - No selection   -> paste from clipboard into terminal
 *   - Clipboard operations go through Python backend (pyperclip)
 *   - Suppress right-click mouse reports so AI tools do not paste too
 */

/* ------------------------------------------------------------------ */
/*  ContextMenu                                                       */
/* ------------------------------------------------------------------ */

const ContextMenu = {
  /**
   * Attach contextmenu listener to the terminal container.
   * Must be called after DOM is ready.
   */
  init() {
    const container = document.getElementById('terminal-container');

    this._blockRightClickReports(container);

    container.addEventListener('contextmenu', async (e) => {
      e.preventDefault();

      const activeId = AppState.activeSessionId;
      if (!activeId) return;

      const term = TerminalManager.getTerminal(activeId);
      if (!term) return;

      const selection = term.getSelection();

      if (selection && selection.length > 0) {
        // Text is selected -> copy to clipboard via Python backend
        await window.pywebview.api.copy_to_clipboard(selection);
        // Clear selection after copy
        term.clearSelection();
      } else {
        // No selection -> paste from clipboard
        const clipText = await window.pywebview.api.paste_from_clipboard();
        if (clipText) {
          // Send clipboard content to PTY as input
          await window.pywebview.api.send_input(activeId, clipText);
        }
      }
    });
  },

  /**
   * 右クリックのマウス報告が AI ツールへ届かないようにする。
   *
   * マウストラッキング（DECSET 1000/1002/1003）が有効なとき、xterm.js は
   * 右クリックを ESC[<2;col;rowM（押下）/ ESC[<2;col;rowm（離し）として
   * PTY へ送る。AI ツール側にも右クリック貼り付けがあると、上の
   * contextmenu ハンドラーによる貼り付けと二重になる。
   * 実測では Claude Code が右クリック 1 回につき 1 回貼り付けており、
   * awt の 1 回と合わせて 2 回貼られていた（Copilot CLI でも同症状）。
   *
   * xterm.js のリスナーは #terminal-container の子孫に登録されるため、
   * キャプチャフェーズで止めれば必ず先に処理できる。押下と離しの両方を
   * 止める必要がある（xterm.js は離し用のリスナーも別に持つ）。
   * contextmenu は別イベントなので、コピー・ペーストには影響しない。
   * 左ボタンとホイールには触れないため、Shift+ドラッグでの選択と
   * 出力欄のスクロールも従来どおり動く。
   *
   * @param {HTMLElement} container - #terminal-container
   */
  _blockRightClickReports(container) {
    container.addEventListener('mousedown', function (e) {
      if (e.button !== 2) return;
      // xterm.js の mousedown ハンドラーはフォーカスも当てている。
      // ここで止めるとそれが走らないため、代わりに当てておく。
      var term = TerminalManager.getTerminal(AppState.activeSessionId);
      if (term) term.focus();
      e.stopImmediatePropagation();
    }, true);

    container.addEventListener('mouseup', function (e) {
      if (e.button !== 2) return;
      e.stopImmediatePropagation();
    }, true);
  },
};

/* ------------------------------------------------------------------ */
/*  Auto-init on DOMContentLoaded                                     */
/* ------------------------------------------------------------------ */

document.addEventListener('DOMContentLoaded', () => {
  ContextMenu.init();
});
