/**
 * resize_handler.js -- Auto-resize handler for terminal and sidebar
 *
 * Responsibility:
 *   - Window resize -> terminal fit (debounced)
 *   - Sidebar horizontal resize via drag handle
 *   - Accordion panel toggle (expand / collapse)
 */

/* ------------------------------------------------------------------ */
/*  ResizeHandler                                                      */
/* ------------------------------------------------------------------ */

const ResizeHandler = {
  init() {
    // Window resize -> fit terminal
    window.addEventListener('resize', () => {
      this._debounce('fitTerminal', () => {
        TerminalManager.fitAll();
      }, 100);
    });

    // Sidebar horizontal resize handle
    this._initSidebarResize();

    // パネル間の上下リサイズハンドル
    this._initPanelResize();

    // アコーディオンヘッダーの開閉ハンドラー
    this._initAccordionToggle();
  },

  _timers: {},

  _debounce(key, fn, delay) {
    clearTimeout(this._timers[key]);
    this._timers[key] = setTimeout(fn, delay);
  },

  /* ---------------------------------------------------------------- */
  /*  Sidebar width resize (horizontal drag)                          */
  /* ---------------------------------------------------------------- */

  _initSidebarResize() {
    const handle = document.getElementById('sidebar-resize-handle');
    const sidebar = document.getElementById('sidebar');

    let startX, startWidth;

    const onMouseDown = (e) => {
      startX = e.clientX;
      startWidth = sidebar.offsetWidth;
      document.addEventListener('mousemove', onMouseMove);
      document.addEventListener('mouseup', onMouseUp);
      document.body.style.cursor = 'ew-resize';
      document.body.style.userSelect = 'none';
    };

    const onMouseMove = (e) => {
      const delta = e.clientX - startX;
      const newWidth = Math.max(
        parseInt(getComputedStyle(document.documentElement).getPropertyValue('--sidebar-min-width')),
        Math.min(
          parseInt(getComputedStyle(document.documentElement).getPropertyValue('--sidebar-max-width')),
          startWidth + delta
        )
      );
      sidebar.style.width = newWidth + 'px';

      // Debounce terminal refit
      this._debounce('sidebarResize', () => {
        TerminalManager.fitAll();
      }, 50);
    };

    const onMouseUp = () => {
      document.removeEventListener('mousemove', onMouseMove);
      document.removeEventListener('mouseup', onMouseUp);
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
      TerminalManager.fitAll();
    };

    handle.addEventListener('mousedown', onMouseDown);
  },

  /* ---------------------------------------------------------------- */
  /*  Panel vertical resize (セッション / ファイル間の上下ドラッグ)      */
  /* ---------------------------------------------------------------- */

  _initPanelResize() {
    const handle = document.getElementById('panel-resize-handle');
    const sessionPanel = document.getElementById('session-panel');
    const filePanel = document.getElementById('file-panel');
    const sidebar = document.getElementById('sidebar');

    let startY, sessionStartH, fileStartH;

    const onMouseDown = (e) => {
      // 両パネルが展開中のみリサイズ可能
      if (sessionPanel.classList.contains('collapsed') ||
          filePanel.classList.contains('collapsed')) return;

      startY = e.clientY;
      sessionStartH = sessionPanel.offsetHeight;
      fileStartH = filePanel.offsetHeight;

      document.addEventListener('mousemove', onMouseMove);
      document.addEventListener('mouseup', onMouseUp);
      document.body.style.cursor = 'ns-resize';
      document.body.style.userSelect = 'none';
    };

    const onMouseMove = (e) => {
      const delta = e.clientY - startY;
      const minH = 40; // ヘッダー + 少なくとも数px
      let newSessionH = sessionStartH + delta;
      let newFileH = fileStartH - delta;

      // 最小高さでクランプ
      if (newSessionH < minH) {
        newSessionH = minH;
        newFileH = sessionStartH + fileStartH - minH;
      }
      if (newFileH < minH) {
        newFileH = minH;
        newSessionH = sessionStartH + fileStartH - minH;
      }

      sessionPanel.style.flex = '0 0 ' + newSessionH + 'px';
      filePanel.style.flex = '0 0 ' + newFileH + 'px';
    };

    const onMouseUp = () => {
      document.removeEventListener('mousemove', onMouseMove);
      document.removeEventListener('mouseup', onMouseUp);
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
    };

    handle.addEventListener('mousedown', onMouseDown);
  },

  /* ---------------------------------------------------------------- */
  /*  Accordion toggle (ヘッダークリックでパネル開閉)                   */
  /* ---------------------------------------------------------------- */

  _initAccordionToggle() {
    document.querySelectorAll('.accordion-header').forEach(function (header) {
      header.addEventListener('click', function (e) {
        // ヘッダー内のボタン（+ 新規）クリック時は開閉しない
        if (e.target.closest('button')) return;
        const panel = header.closest('.accordion-panel');
        panel.classList.toggle('collapsed');

        // リサイズで設定した固定高さをリセットし flex:1 に戻す
        panel.style.flex = '';
      });
    });
  },
};
