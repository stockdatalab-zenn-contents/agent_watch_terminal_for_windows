/**
 * context_menu.js -- Right-click context menu handler
 *
 * Responsibility:
 *   - Right-click copy/paste in the terminal
 *   - Text selected  -> copy to clipboard, clear selection
 *   - No selection   -> paste from clipboard into terminal
 *   - Clipboard operations go through Python backend (pyperclip)
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
    document.getElementById('terminal-container').addEventListener('contextmenu', async (e) => {
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
};

/* ------------------------------------------------------------------ */
/*  Auto-init on DOMContentLoaded                                     */
/* ------------------------------------------------------------------ */

document.addEventListener('DOMContentLoaded', () => {
  ContextMenu.init();
});
