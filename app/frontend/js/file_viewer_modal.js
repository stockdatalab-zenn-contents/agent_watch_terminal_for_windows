/**
 * file_viewer_modal.js -- マルチペイン・ファイルビューア モーダル
 *
 * 責務:
 *   - モーダルの表示 / 非表示
 *   - 複数ペインの追加 / 削除 / 横並び表示
 *   - ファイル重複オープン防止
 *   - ペイン間リサイズハンドル
 *   - Ctrl+S / ESC のキーボードショートカット
 *
 * 依存:
 *   - FileViewerPane (file_viewer_pane.js)
 */

/* ------------------------------------------------------------------ */
/*  FileViewerModal                                                    */
/* ------------------------------------------------------------------ */

const FileViewerModal = {
  /** @type {Map<string, {path: string, fileName: string, element: HTMLElement}>} */
  _panes: new Map(),

  /** 同時表示上限 */
  _MAX_PANES: 4,

  /* ---------------------------------------------------------------- */
  /*  openFile -- ファイルを開く（重複チェック付き）                     */
  /* ---------------------------------------------------------------- */

  async openFile(filePath, fileName) {
    var key = this._normalizeKey(filePath);

    // 重複チェック: 既に開いている場合はハイライトして終了
    if (this._panes.has(key)) {
      this._highlightPane(this._panes.get(key).element);
      return;
    }

    // 上限チェック
    if (this._panes.size >= this._MAX_PANES) {
      alert('最大 ' + this._MAX_PANES + ' ファイルまで同時に開けます。\n不要なペインを閉じてから再度開いてください。');
      return;
    }

    var container = document.getElementById('fv-panes');

    // 既にペインがある場合、リサイズハンドルを挿入
    if (this._panes.size > 0) {
      var handle = this._createResizeHandle();
      container.appendChild(handle);
    }

    // ペイン生成
    var pane = await FileViewerPane.create(filePath, fileName);
    container.appendChild(pane);

    this._panes.set(key, {
      path: filePath,
      fileName: fileName,
      element: pane,
    });

    // モーダル表示
    this._show();
  },

  /* ---------------------------------------------------------------- */
  /*  closePane -- 指定パスのペインを閉じる                             */
  /* ---------------------------------------------------------------- */

  closePane(filePath) {
    var key = this._normalizeKey(filePath);
    var entry = this._panes.get(key);
    if (!entry) return;

    // 未保存確認
    if (!FileViewerPane.confirmClose(entry.element)) return;

    var container = document.getElementById('fv-panes');
    var paneEl = entry.element;

    // リサイズハンドルの削除
    // ペインの直前または直後のリサイズハンドルを削除
    var prev = paneEl.previousElementSibling;
    var next = paneEl.nextElementSibling;

    if (prev && prev.classList.contains('fv-pane-resize-handle')) {
      container.removeChild(prev);
    } else if (next && next.classList.contains('fv-pane-resize-handle')) {
      container.removeChild(next);
    }

    // ペイン削除
    container.removeChild(paneEl);
    this._panes.delete(key);

    // 残りペインが0 → モーダル非表示
    if (this._panes.size === 0) {
      this._hide();
    }
  },

  /* ---------------------------------------------------------------- */
  /*  closeAll -- 全ペインを閉じてモーダルを非表示にする                 */
  /* ---------------------------------------------------------------- */

  closeAll() {
    // 未保存確認（1つでもキャンセルしたら中止）
    var entries = Array.from(this._panes.values());
    for (var i = 0; i < entries.length; i++) {
      if (FileViewerPane.hasUnsavedChanges(entries[i].element)) {
        var ok = confirm(
          entries[i].fileName + ' に未保存の変更があります。全て閉じますか？'
        );
        if (!ok) return;
        break; // 1回確認すれば十分
      }
    }

    // 全ペインを削除
    var container = document.getElementById('fv-panes');
    container.innerHTML = '';
    this._panes.clear();

    this._hide();
  },

  /* ---------------------------------------------------------------- */
  /*  isOpen -- 指定パスのファイルが開いているか                        */
  /* ---------------------------------------------------------------- */

  isOpen(filePath) {
    return this._panes.has(this._normalizeKey(filePath));
  },

  /* ---------------------------------------------------------------- */
  /*  _show -- モーダルを表示                                          */
  /* ---------------------------------------------------------------- */

  _show() {
    var overlay = document.getElementById('fv-overlay');
    if (overlay.classList.contains('hidden')) {
      overlay.classList.remove('hidden');
    }

    // サイドバーをオーバーレイの上に昇格（ファイルエクスプローラを操作可能に）
    var sidebar = document.getElementById('sidebar');
    sidebar.classList.add('fv-sidebar-active');

    // セッションパネルを無効化（セッション切替による混乱を防止）
    var sessionPanel = document.getElementById('session-panel');
    sessionPanel.classList.add('fv-disabled');

    // パネル位置をサイドバーの右端に合わせる
    this._adjustPanelPosition();

    document.getElementById('fv-panel').focus();
  },

  /* ---------------------------------------------------------------- */
  /*  _hide -- モーダルを非表示にしてターミナルへフォーカスを戻す        */
  /* ---------------------------------------------------------------- */

  _hide() {
    document.getElementById('fv-overlay').classList.add('hidden');

    // サイドバーのz-indexを元に戻す
    document.getElementById('sidebar').classList.remove('fv-sidebar-active');

    // セッションパネルを再有効化
    document.getElementById('session-panel').classList.remove('fv-disabled');

    // パネル位置をリセット
    var panel = document.getElementById('fv-panel');
    panel.style.left = '';
    panel.style.width = '';

    // アクティブターミナルへフォーカスを戻す
    if (typeof AppState !== 'undefined' && AppState.activeSessionId) {
      var term = typeof TerminalManager !== 'undefined'
        ? TerminalManager.getTerminal(AppState.activeSessionId)
        : null;
      if (term) term.focus();
    }
  },

  /* ---------------------------------------------------------------- */
  /*  _adjustPanelPosition -- パネルをサイドバーの右に配置              */
  /* ---------------------------------------------------------------- */

  _adjustPanelPosition() {
    var sidebar = document.getElementById('sidebar');
    var sidebarHandle = document.getElementById('sidebar-resize-handle');
    var panel = document.getElementById('fv-panel');

    // サイドバー幅 + リサイズハンドル幅 + 余白
    var sidebarRight = sidebar.getBoundingClientRect().width
      + (sidebarHandle ? sidebarHandle.getBoundingClientRect().width : 4);
    var margin = 12;

    panel.style.left = (sidebarRight + margin) + 'px';
    panel.style.width = 'calc(100vw - ' + (sidebarRight + margin + 12) + 'px)';
  },

  /* ---------------------------------------------------------------- */
  /*  _normalizeKey -- パスを正規化して重複判定用キーにする              */
  /* ---------------------------------------------------------------- */

  _normalizeKey(filePath) {
    // バックスラッシュ → スラッシュ、小文字化（Windows対応）
    return filePath.replace(/\\/g, '/').toLowerCase();
  },

  /* ---------------------------------------------------------------- */
  /*  _highlightPane -- 既に開いているペインを一瞬ハイライトする         */
  /* ---------------------------------------------------------------- */

  _highlightPane(paneEl) {
    paneEl.classList.add('fv-pane-highlight');
    setTimeout(function () {
      paneEl.classList.remove('fv-pane-highlight');
    }, 600);
  },

  /* ---------------------------------------------------------------- */
  /*  _createResizeHandle -- ペイン間リサイズハンドルを生成              */
  /* ---------------------------------------------------------------- */

  _createResizeHandle() {
    var handle = document.createElement('div');
    handle.className = 'fv-pane-resize-handle';

    var self = this;
    handle.addEventListener('mousedown', function (e) {
      e.preventDefault();
      self._startPaneResize(handle, e.clientX);
    });

    return handle;
  },

  /* ---------------------------------------------------------------- */
  /*  _startPaneResize -- ペイン間ドラッグリサイズ開始                   */
  /* ---------------------------------------------------------------- */

  _startPaneResize(handle, startX) {
    var leftPane = handle.previousElementSibling;
    var rightPane = handle.nextElementSibling;

    if (!leftPane || !rightPane) return;

    var leftStart = leftPane.getBoundingClientRect().width;
    var rightStart = rightPane.getBoundingClientRect().width;
    var minWidth = 150;

    function onMouseMove(e) {
      var delta = e.clientX - startX;
      var newLeft = leftStart + delta;
      var newRight = rightStart - delta;

      if (newLeft < minWidth || newRight < minWidth) return;

      leftPane.style.flex = 'none';
      rightPane.style.flex = 'none';
      leftPane.style.width = newLeft + 'px';
      rightPane.style.width = newRight + 'px';
    }

    function onMouseUp() {
      document.removeEventListener('mousemove', onMouseMove);
      document.removeEventListener('mouseup', onMouseUp);
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
    }

    document.body.style.cursor = 'ew-resize';
    document.body.style.userSelect = 'none';
    document.addEventListener('mousemove', onMouseMove);
    document.addEventListener('mouseup', onMouseUp);
  },

  /* ---------------------------------------------------------------- */
  /*  _saveFocusedPane -- フォーカスがあるペインを保存（Ctrl+S用）      */
  /* ---------------------------------------------------------------- */

  _saveFocusedPane() {
    // フォーカスされている textarea の親ペインを探す
    var active = document.activeElement;
    if (!active) return;

    var pane = active.closest('.fv-pane');
    if (pane) {
      FileViewerPane.save(pane);
    }
  },

  /* ---------------------------------------------------------------- */
  /*  _openSearchInFocusedPane -- フォーカス中のペインで検索バーを開く   */
  /* ---------------------------------------------------------------- */

  _openSearchInFocusedPane() {
    var active = document.activeElement;
    var pane = active ? active.closest('.fv-pane') : null;

    // ペインが見つからない場合、最初のペインを使用
    if (!pane) {
      var container = document.getElementById('fv-panes');
      pane = container ? container.querySelector('.fv-pane') : null;
    }

    if (pane) {
      FileViewerPane.openSearch(pane);
    }
  },

  /* ---------------------------------------------------------------- */
  /*  init -- イベントリスナー登録                                      */
  /* ---------------------------------------------------------------- */

  init() {
    var self = this;

    // 全閉じボタン
    var closeAllBtn = document.getElementById('fv-close-all-btn');
    if (closeAllBtn) {
      closeAllBtn.addEventListener('click', function () {
        self.closeAll();
      });
    }

    // 背景クリックで閉じる
    var overlay = document.getElementById('fv-overlay');
    if (overlay) {
      overlay.addEventListener('click', function (e) {
        if (e.target.id === 'fv-overlay') {
          self.closeAll();
        }
      });
    }

    // ESC で閉じる
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') {
        var ov = document.getElementById('fv-overlay');
        if (ov && !ov.classList.contains('hidden')) {
          self.closeAll();
        }
      }
    });

    // Ctrl+S で保存
    document.addEventListener('keydown', function (e) {
      if ((e.ctrlKey || e.metaKey) && e.key === 's') {
        var ov = document.getElementById('fv-overlay');
        if (ov && !ov.classList.contains('hidden')) {
          e.preventDefault();
          self._saveFocusedPane();
        }
      }
    });

    // Ctrl+F で検索バーを開く
    document.addEventListener('keydown', function (e) {
      if ((e.ctrlKey || e.metaKey) && e.key === 'f') {
        var ov = document.getElementById('fv-overlay');
        if (ov && !ov.classList.contains('hidden')) {
          e.preventDefault();
          self._openSearchInFocusedPane();
        }
      }
    });
  },
};

/* ------------------------------------------------------------------ */
/*  Bootstrap                                                          */
/* ------------------------------------------------------------------ */

document.addEventListener('DOMContentLoaded', function () {
  FileViewerModal.init();
});
