/**
 * file_viewer_pane.js -- 個別ペインの生成・編集・プレビュー・保存
 *
 * 責務:
 *   - ペインDOM要素の生成
 *   - ファイル内容の読み込みと表示
 *   - 編集 ⇔ プレビュー モード切替（.md 専用）
 *   - ファイル保存
 *   - 未保存変更の検知
 *
 * バックエンド依存:
 *   - window.pywebview.api.read_file_content(path)
 *   - window.pywebview.api.save_file(path, content)
 *   - window.pywebview.api.read_image_base64(path)
 */

/* ------------------------------------------------------------------ */
/*  marked グローバル設定                                              */
/*    - breaks: true  単一改行を <br> に変換し、編集時と同じ改行を維持 */
/* ------------------------------------------------------------------ */

if (typeof marked !== 'undefined') {
  marked.use({ breaks: true });
}

/* ------------------------------------------------------------------ */
/*  FileViewerPane                                                     */
/* ------------------------------------------------------------------ */

const FileViewerPane = {

  /* ---------------------------------------------------------------- */
  /*  create -- ペインDOM要素を生成してファイル内容を読み込む            */
  /* ---------------------------------------------------------------- */

  async create(filePath, fileName) {
    var pane = document.createElement('div');
    pane.className = 'fv-pane';
    pane.dataset.path = filePath;
    pane.dataset.savedContent = '';
    pane.dataset.mode = 'edit';

    // --- ヘッダー ---
    var header = document.createElement('div');
    header.className = 'fv-pane-header';

    var nameSpan = document.createElement('span');
    nameSpan.className = 'fv-pane-filename';
    nameSpan.textContent = fileName;
    nameSpan.title = filePath;

    var actions = document.createElement('div');
    actions.className = 'fv-pane-actions';

    var status = document.createElement('span');
    status.className = 'fv-pane-status hidden';

    var modeBtn = document.createElement('button');
    modeBtn.className = 'fv-pane-mode-btn';
    modeBtn.textContent = 'Edit';

    var saveBtn = document.createElement('button');
    saveBtn.className = 'fv-pane-save-btn';
    saveBtn.textContent = 'Save';

    var closeBtn = document.createElement('button');
    closeBtn.className = 'fv-pane-close-btn';
    closeBtn.innerHTML = '&#x2715;';
    closeBtn.title = '閉じる';

    actions.appendChild(status);

    // .md のみモード切替ボタンを表示
    if (this._isMarkdown(fileName)) {
      actions.appendChild(modeBtn);
    }

    actions.appendChild(saveBtn);
    actions.appendChild(closeBtn);

    header.appendChild(nameSpan);
    header.appendChild(actions);

    // --- エディタ（行番号ガター + textarea） ---
    var editorWrapper = document.createElement('div');
    editorWrapper.className = 'fv-pane-editor-wrapper';

    var lineNumbers = document.createElement('div');
    lineNumbers.className = 'fv-pane-line-numbers';
    lineNumbers.textContent = '1';

    var textarea = document.createElement('textarea');
    textarea.className = 'fv-pane-editor';
    textarea.spellcheck = false;
    textarea.setAttribute('wrap', 'off');

    editorWrapper.appendChild(lineNumbers);
    editorWrapper.appendChild(textarea);

    // --- プレビュー ---
    var preview = document.createElement('div');
    preview.className = 'fv-pane-preview hidden';

    // --- 検索バー ---
    var searchBar = document.createElement('div');
    searchBar.className = 'fv-pane-search-bar hidden';

    var searchInput = document.createElement('input');
    searchInput.type = 'text';
    searchInput.className = 'fv-search-input';
    searchInput.placeholder = '検索...';

    var searchCount = document.createElement('span');
    searchCount.className = 'fv-search-count';

    var searchPrevBtn = document.createElement('button');
    searchPrevBtn.className = 'fv-search-prev-btn';
    searchPrevBtn.innerHTML = '&#x25B2;';
    searchPrevBtn.title = '前へ (Shift+Enter)';

    var searchNextBtn = document.createElement('button');
    searchNextBtn.className = 'fv-search-next-btn';
    searchNextBtn.innerHTML = '&#x25BC;';
    searchNextBtn.title = '次へ (Enter)';

    var searchCloseBtn = document.createElement('button');
    searchCloseBtn.className = 'fv-search-close-btn';
    searchCloseBtn.innerHTML = '&#x2715;';
    searchCloseBtn.title = '閉じる (Esc)';

    searchBar.appendChild(searchInput);
    searchBar.appendChild(searchCount);
    searchBar.appendChild(searchPrevBtn);
    searchBar.appendChild(searchNextBtn);
    searchBar.appendChild(searchCloseBtn);

    pane.appendChild(header);
    pane.appendChild(searchBar);
    pane.appendChild(editorWrapper);
    pane.appendChild(preview);

    // --- ファイル内容の読み込み ---
    var content = '';
    if (window.pywebview && window.pywebview.api) {
      content = await window.pywebview.api.read_file_content(filePath);
    }
    pane.dataset.savedContent = content;
    textarea.value = content;

    // 行番号を初期化
    this._updateLineNumbers(pane);

    // .md → プレビューモードで初期表示
    if (this._isMarkdown(fileName)) {
      editorWrapper.classList.add('hidden');
      preview.classList.remove('hidden');
      preview.innerHTML = typeof marked !== 'undefined' ? marked.parse(content) : content;
      pane.dataset.mode = 'preview';
      modeBtn.textContent = 'Edit';
      this._resolvePreviewImages(pane);
    } else {
      // その他 → 編集モードで初期表示
      preview.classList.add('hidden');
      pane.dataset.mode = 'edit';
    }

    // --- イベントリスナー ---
    var self = this;

    // プレビュー内リンクのクリックをインターセプトし外部ブラウザで開く
    preview.addEventListener('click', function (e) {
      var link = e.target.closest('a');
      if (link && link.href && /^https?:/i.test(link.href)) {
        e.preventDefault();
        if (window.pywebview && window.pywebview.api) {
          window.pywebview.api.open_url(link.href);
        }
      }
    });

    modeBtn.addEventListener('click', function () {
      self.toggleMode(pane);
    });

    saveBtn.addEventListener('click', function () {
      self.save(pane);
    });

    closeBtn.addEventListener('click', function () {
      if (typeof FileViewerModal !== 'undefined') {
        FileViewerModal.closePane(filePath);
      }
    });

    // textarea 変更時に未保存インジケータ・行番号を更新
    textarea.addEventListener('input', function () {
      self._updateUnsavedIndicator(pane);
      self._updateLineNumbers(pane);
    });

    // スクロール同期（textarea → 行番号ガター）
    textarea.addEventListener('scroll', function () {
      lineNumbers.scrollTop = textarea.scrollTop;
    });

    // --- 検索バー イベント ---
    searchInput.addEventListener('input', function () {
      self._searchInPane(pane);
    });

    searchInput.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') {
        e.preventDefault();
        if (e.shiftKey) {
          self._searchPrev(pane);
        } else {
          self._searchNext(pane);
        }
      }
      if (e.key === 'Escape') {
        self.closeSearch(pane);
      }
    });

    searchNextBtn.addEventListener('click', function () {
      self._searchNext(pane);
    });

    searchPrevBtn.addEventListener('click', function () {
      self._searchPrev(pane);
    });

    searchCloseBtn.addEventListener('click', function () {
      self.closeSearch(pane);
    });

    // 検索バー表示中は textarea の Enter を検索ナビゲーションに委譲
    textarea.addEventListener('keydown', function (e) {
      var sb = pane.querySelector('.fv-pane-search-bar');
      if (sb.classList.contains('hidden')) return;

      if (e.key === 'Enter') {
        e.preventDefault();
        if (e.shiftKey) {
          self._searchPrev(pane);
        } else {
          self._searchNext(pane);
        }
      }
    });

    return pane;
  },

  /* ---------------------------------------------------------------- */
  /*  toggleMode -- 編集 ⇔ プレビュー切替（.md 専用）                   */
  /* ---------------------------------------------------------------- */

  toggleMode(pane) {
    var editorWrapper = pane.querySelector('.fv-pane-editor-wrapper');
    var textarea = pane.querySelector('.fv-pane-editor');
    var preview = pane.querySelector('.fv-pane-preview');
    var modeBtn = pane.querySelector('.fv-pane-mode-btn');

    if (pane.dataset.mode === 'edit') {
      // プレビューモードへ
      var raw = textarea.value;
      preview.innerHTML = typeof marked !== 'undefined' ? marked.parse(raw) : raw;
      editorWrapper.classList.add('hidden');
      this._resolvePreviewImages(pane);
      preview.classList.remove('hidden');
      modeBtn.textContent = 'Edit';
      pane.dataset.mode = 'preview';
    } else {
      // 編集モードへ
      editorWrapper.classList.remove('hidden');
      preview.classList.add('hidden');
      modeBtn.textContent = 'Preview';
      pane.dataset.mode = 'edit';
      textarea.focus();
    }
  },

  /* ---------------------------------------------------------------- */
  /*  save -- ファイルを保存する                                       */
  /* ---------------------------------------------------------------- */

  async save(pane) {
    if (!window.pywebview || !window.pywebview.api) return false;

    var filePath = pane.dataset.path;
    var textarea = pane.querySelector('.fv-pane-editor');
    var content = textarea.value;

    var ok = await window.pywebview.api.save_file(filePath, content);

    if (ok) {
      pane.dataset.savedContent = content;
      this._updateUnsavedIndicator(pane);
      this._flashStatus(pane, '保存しました', 'success');
    } else {
      this._flashStatus(pane, '保存に失敗しました', 'error');
    }

    return ok;
  },

  /* ---------------------------------------------------------------- */
  /*  hasUnsavedChanges -- 未保存の変更があるか                         */
  /* ---------------------------------------------------------------- */

  hasUnsavedChanges(pane) {
    var textarea = pane.querySelector('.fv-pane-editor');
    return textarea.value !== pane.dataset.savedContent;
  },

  /* ---------------------------------------------------------------- */
  /*  confirmClose -- 閉じる前の未保存確認（true=閉じてOK）             */
  /* ---------------------------------------------------------------- */

  confirmClose(pane) {
    if (!this.hasUnsavedChanges(pane)) return true;
    return confirm('未保存の変更があります。閉じますか？');
  },

  /* ---------------------------------------------------------------- */
  /*  _isMarkdown -- ファイル名が .md かどうか                         */
  /* ---------------------------------------------------------------- */

  _isMarkdown(fileName) {
    var lower = fileName.toLowerCase();
    return lower.endsWith('.md') || lower.endsWith('.markdown');
  },

  /* ---------------------------------------------------------------- */
  /*  _resolvePreviewImages -- プレビュー内の画像を Base64 data URI に   */
  /*  非同期変換する。相対パスを MD ファイル基準で解決し、Backend API     */
  /*  経由で読み込む。                                                  */
  /* ---------------------------------------------------------------- */

  async _resolvePreviewImages(pane) {
    if (!window.pywebview || !window.pywebview.api) return;

    var filePath = pane.dataset.path;
    var lastSep = Math.max(filePath.lastIndexOf('\\'), filePath.lastIndexOf('/'));
    var mdDir = filePath.substring(0, lastSep);

    var preview = pane.querySelector('.fv-pane-preview');
    var imgs = preview.querySelectorAll('img');
    if (imgs.length === 0) return;

    // 全画像を並列で読み込む
    var promises = [];
    for (var i = 0; i < imgs.length; i++) {
      (function (img) {
        var src = img.getAttribute('src');
        if (!src || /^(https?:|data:)/i.test(src)) return;

        // 相対パスを MD ファイルのディレクトリ基準で絶対パスに変換
        var absPath = mdDir + '/' + src.replace(/^\.\//, '');

        promises.push(
          window.pywebview.api.read_image_base64(absPath).then(function (dataUri) {
            if (dataUri) img.src = dataUri;
          }).catch(function () {
            // 読み込み失敗は無視（alt テキストが表示される）
          })
        );
      })(imgs[i]);
    }

    await Promise.all(promises);
  },

  /* ---------------------------------------------------------------- */
  /*  _updateLineNumbers -- 行番号ガターを更新                         */
  /* ---------------------------------------------------------------- */

  _updateLineNumbers(pane) {
    var textarea = pane.querySelector('.fv-pane-editor');
    var lineNumbers = pane.querySelector('.fv-pane-line-numbers');
    if (!textarea || !lineNumbers) return;

    var lineCount = textarea.value.split('\n').length;
    var lines = [];
    for (var i = 1; i <= lineCount; i++) {
      lines.push(i);
    }
    lineNumbers.textContent = lines.join('\n');
  },

  /* ---------------------------------------------------------------- */
  /*  _updateUnsavedIndicator -- 未保存マーク(●)の表示切替              */
  /* ---------------------------------------------------------------- */

  _updateUnsavedIndicator(pane) {
    var nameSpan = pane.querySelector('.fv-pane-filename');
    if (this.hasUnsavedChanges(pane)) {
      nameSpan.classList.add('unsaved');
    } else {
      nameSpan.classList.remove('unsaved');
    }
  },

  /* ---------------------------------------------------------------- */
  /*  _flashStatus -- 保存結果を一時表示                                */
  /* ---------------------------------------------------------------- */

  _flashStatus(pane, message, type) {
    var status = pane.querySelector('.fv-pane-status');
    status.textContent = message;
    status.className = 'fv-pane-status ' + (type === 'error' ? 'status-error' : 'status-success');

    setTimeout(function () {
      status.className = 'fv-pane-status hidden';
      status.textContent = '';
    }, 2000);
  },

  /* ---------------------------------------------------------------- */
  /*  検索機能                                                         */
  /* ---------------------------------------------------------------- */

  /** 検索バーを開く。プレビューモード時は編集モードへ切替。 */
  openSearch(pane) {
    if (pane.dataset.mode === 'preview') {
      this.toggleMode(pane);
    }

    var searchBar = pane.querySelector('.fv-pane-search-bar');
    var input = pane.querySelector('.fv-search-input');
    searchBar.classList.remove('hidden');
    input.focus();
    input.select();
  },

  /** 検索バーを閉じてエディタにフォーカスを戻す。 */
  closeSearch(pane) {
    var searchBar = pane.querySelector('.fv-pane-search-bar');
    var input = pane.querySelector('.fv-search-input');
    var countEl = pane.querySelector('.fv-search-count');
    searchBar.classList.add('hidden');
    input.value = '';
    countEl.textContent = '';
    countEl.classList.remove('no-match');
    pane.dataset.searchIndex = '-1';

    var textarea = pane.querySelector('.fv-pane-editor');
    if (textarea) textarea.focus();
  },

  /** textarea 内のマッチ位置を全て収集する（大文字小文字無視）。 */
  _collectMatches(pane) {
    var textarea = pane.querySelector('.fv-pane-editor');
    var input = pane.querySelector('.fv-search-input');
    var query = input.value;
    if (!query) return [];

    var text = textarea.value.toLowerCase();
    var q = query.toLowerCase();
    var matches = [];
    var pos = 0;
    while ((pos = text.indexOf(q, pos)) !== -1) {
      matches.push({ start: pos, end: pos + query.length });
      pos += 1;
    }
    return matches;
  },

  /** 検索入力変更時: マッチ数を更新し最初のマッチ位置へスクロール。 */
  _searchInPane(pane) {
    var input = pane.querySelector('.fv-search-input');
    var countEl = pane.querySelector('.fv-search-count');

    if (!input.value) {
      countEl.textContent = '';
      countEl.classList.remove('no-match');
      pane.dataset.searchIndex = '-1';
      return;
    }

    var matches = this._collectMatches(pane);

    if (matches.length === 0) {
      countEl.textContent = '0件';
      countEl.classList.add('no-match');
      pane.dataset.searchIndex = '-1';
      return;
    }

    countEl.classList.remove('no-match');
    pane.dataset.searchIndex = '-1';

    // 最初のマッチ位置へ強制スクロール（blur/focus でブラウザにスクロールを強制）
    var textarea = pane.querySelector('.fv-pane-editor');
    var match = matches[0];
    textarea.focus();
    textarea.setSelectionRange(match.end, match.end);
    textarea.blur();
    textarea.focus();
    textarea.setSelectionRange(match.start, match.end);

    // 行番号ガターのスクロール同期
    var lineNumbers = pane.querySelector('.fv-pane-line-numbers');
    if (lineNumbers) lineNumbers.scrollTop = textarea.scrollTop;

    // フォーカスを検索入力に戻す
    input.focus();

    countEl.textContent = matches.length + '件';
  },

  /** 次のマッチへ移動。 */
  _searchNext(pane) {
    var matches = this._collectMatches(pane);
    if (matches.length === 0) return;

    var idx = parseInt(pane.dataset.searchIndex || '-1', 10);
    idx = (idx + 1) % matches.length;
    pane.dataset.searchIndex = String(idx);
    this._goToMatch(pane, matches, idx);
  },

  /** 前のマッチへ移動。 */
  _searchPrev(pane) {
    var matches = this._collectMatches(pane);
    if (matches.length === 0) return;

    var idx = parseInt(pane.dataset.searchIndex || '-1', 10);
    if (idx < 0) idx = 0;
    idx = (idx - 1 + matches.length) % matches.length;
    pane.dataset.searchIndex = String(idx);
    this._goToMatch(pane, matches, idx);
  },

  /** 指定マッチへジャンプしてテキストエリアにフォーカス。 */
  _goToMatch(pane, matches, idx) {
    var textarea = pane.querySelector('.fv-pane-editor');
    var countEl = pane.querySelector('.fv-search-count');
    var match = matches[idx];

    // blur/focus でブラウザにキャレット位置へのスクロールを強制
    textarea.focus();
    textarea.setSelectionRange(match.end, match.end);
    textarea.blur();
    textarea.focus();
    textarea.setSelectionRange(match.start, match.end);

    // 行番号ガターのスクロール同期
    var lineNumbers = pane.querySelector('.fv-pane-line-numbers');
    if (lineNumbers) lineNumbers.scrollTop = textarea.scrollTop;

    countEl.textContent = (idx + 1) + '/' + matches.length;
  },
};
