/**
 * file_explorer_ui.js -- File Explorer sidebar module (tree view)
 *
 * Responsibility:
 *   - Render file/directory tree in the sidebar
 *   - Expand/collapse directories inline via single click (lazy-load)
 *   - Open files via double click (markdown -> preview, others -> OS default)
 *   - Provide file-type icons based on extension
 *   - Sort files by name or modified date (frontend-side)
 *
 * Backend dependency:
 *   - window.pywebview.api.list_files(path)  -- returns file list
 *   - window.pywebview.api.open_file(path)   -- opens file with OS default app
 */

/* ------------------------------------------------------------------ */
/*  FileExplorerUI                                                     */
/* ------------------------------------------------------------------ */

const FileExplorerUI = {
  currentPath: '',
  _expandedPaths: new Set(),   // 展開中フォルダのパス集合
  _childrenCache: {},          // path -> files[] のキャッシュ
  _sortKey: 'name',            // 'name' | 'modified'
  _sortOrder: 'asc',           // 'asc' | 'desc'

  /* ---------------------------------------------------------------- */
  /*  refresh -- ルートを設定してツリー全体を再描画                      */
  /* ---------------------------------------------------------------- */

  async refresh(path) {
    if (!window.pywebview || !window.pywebview.api) return;

    var targetPath = path || '';
    var files = await window.pywebview.api.list_files(targetPath);

    // パスが空（セッション cwd）の場合、レスポンスからルートパスを逆算
    if (!targetPath) {
      targetPath = this._resolveRootPath(files);
    }

    this.currentPath = targetPath;

    // ツリー状態をクリア
    this._expandedPaths.clear();
    this._childrenCache = {};
    this._childrenCache[targetPath] = files;
    this._renderTree();
  },

  /* ---------------------------------------------------------------- */
  /*  _renderTree -- コンテナをクリアしてツリー全体を描画                */
  /* ---------------------------------------------------------------- */

  _renderTree() {
    var container = document.getElementById('file-explorer');
    container.innerHTML = '';

    // パスバー（現在のルートパスを表示）
    if (this.currentPath) {
      var pathBar = document.createElement('div');
      pathBar.className = 'file-path-bar';
      pathBar.textContent = this._shortenPath(this.currentPath);
      pathBar.title = this.currentPath;
      container.appendChild(pathBar);
    }

    // 親ディレクトリエントリ（ツリーのルートを親に切替）
    if (this.currentPath) {
      var parentItem = document.createElement('div');
      parentItem.className = 'file-item';
      parentItem.innerHTML =
        '<span class="tree-chevron-spacer"></span>' +
        '<span class="file-icon">..</span>' +
        '<span class="file-name">..</span>';

      var self = this;
      parentItem.addEventListener('click', function () {
        var parentPath = self.currentPath.replace(/[\\/][^\\/]+$/, '');
        self.refresh(parentPath);
      });
      container.appendChild(parentItem);
    }

    // ルート直下のアイテムを描画
    var rootFiles = this._childrenCache[this.currentPath] || [];
    this._renderItems(container, rootFiles, 0);
  },

  /* ---------------------------------------------------------------- */
  /*  _renderItems -- files 配列をフラットに描画 (depth でインデント)   */
  /* ---------------------------------------------------------------- */

  _renderItems(container, files, depth) {
    var that = this;
    var sorted = this._sortFiles(files);
    sorted.forEach(function (file) {
      var item = document.createElement('div');
      item.className = 'file-item' + (file.is_dir ? ' is-dir' : '');
      item.style.paddingLeft = (12 + depth * 16) + 'px';

      // フォルダ: シェブロン、ファイル: スペーサー
      if (file.is_dir) {
        var chevron = document.createElement('span');
        chevron.className = 'tree-chevron';
        if (that._expandedPaths.has(file.path)) {
          chevron.classList.add('expanded');
        }
        item.appendChild(chevron);
      } else {
        var spacer = document.createElement('span');
        spacer.className = 'tree-chevron-spacer';
        item.appendChild(spacer);
      }

      var icon = document.createElement('span');
      icon.className = 'file-icon';
      icon.innerHTML = file.is_dir ? '\u{1F4C1}' : that._getFileIcon(file.name);

      var name = document.createElement('span');
      name.className = 'file-name';
      name.textContent = file.name;

      item.appendChild(icon);
      item.appendChild(name);

      // フォルダ: クリックで展開/折りたたみ
      if (file.is_dir) {
        item.addEventListener('click', function () {
          that._toggleFolder(file.path);
        });
      }

      // ダブルクリック: ファイルを開く
      item.addEventListener('dblclick', async function () {
        if (file.is_dir) return;

        if (that._isViewable(file.name)) {
          await FileViewerModal.openFile(file.path, file.name);
        } else {
          await window.pywebview.api.open_file(file.path);
        }
      });

      container.appendChild(item);

      // 展開中のフォルダの子要素を再帰描画
      if (file.is_dir && that._expandedPaths.has(file.path)) {
        var children = that._childrenCache[file.path];
        if (children) {
          that._renderItems(container, children, depth + 1);
        }
      }
    });
  },

  /* ---------------------------------------------------------------- */
  /*  _toggleFolder -- フォルダの展開/折りたたみを切替                   */
  /* ---------------------------------------------------------------- */

  async _toggleFolder(path) {
    if (this._expandedPaths.has(path)) {
      this._expandedPaths.delete(path);
    } else {
      // 未キャッシュなら子要素を取得
      if (!this._childrenCache[path]) {
        var files = await window.pywebview.api.list_files(path);
        this._childrenCache[path] = files;
      }
      this._expandedPaths.add(path);
    }
    this._renderTree();
  },

  /* ---------------------------------------------------------------- */
  /*  _getFileIcon -- 拡張子から絵文字アイコンを返す                    */
  /* ---------------------------------------------------------------- */

  _getFileIcon(filename) {
    var ext = filename.split('.').pop().toLowerCase();
    var icons = {
      'py':   '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 110 110"><path d="M54.9 2C27.3 2 29 13.8 29 13.8l.1 12.2h26.4v3.7H17.8S2 27.2 2 55.1s13.8 26.9 13.8 26.9h8.2V69.6s-.4-13.8 13.6-13.8h23.4s13.1.2 13.1-12.7V18.5S76.4 2 54.9 2zm-13 9.5a4.3 4.3 0 110 8.6 4.3 4.3 0 010-8.6z" fill="#3776AB"/><path d="M55.1 108c27.6 0 25.9-11.8 25.9-11.8l-.1-12.2H54.5v-3.7h37.7S108 82.8 108 54.9s-13.8-26.9-13.8-26.9h-8.2v12.4s.4 13.8-13.6 13.8H49s-13.1-.2-13.1 12.7v24.6S33.6 108 55.1 108zm13-9.5a4.3 4.3 0 110-8.6 4.3 4.3 0 010 8.6z" fill="#FFD43B"/></svg>',    // Pythonロゴ（インラインSVG）
      'ipynb': '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 110 110"><path d="M54.9 2C27.3 2 29 13.8 29 13.8l.1 12.2h26.4v3.7H17.8S2 27.2 2 55.1s13.8 26.9 13.8 26.9h8.2V69.6s-.4-13.8 13.6-13.8h23.4s13.1.2 13.1-12.7V18.5S76.4 2 54.9 2zm-13 9.5a4.3 4.3 0 110 8.6 4.3 4.3 0 010-8.6z" fill="#3776AB"/><path d="M55.1 108c27.6 0 25.9-11.8 25.9-11.8l-.1-12.2H54.5v-3.7h37.7S108 82.8 108 54.9s-13.8-26.9-13.8-26.9h-8.2v12.4s.4 13.8-13.6 13.8H49s-13.1-.2-13.1 12.7v24.6S33.6 108 55.1 108zm13-9.5a4.3 4.3 0 110-8.6 4.3 4.3 0 010 8.6z" fill="#FFD43B"/></svg>',
      'js':   '\u{1F4DC}',    // scroll
      'ts':   '\u{1F4DC}',
      'json': '\u{1F4CB}',    // clipboard
      'md':   '\u{1F4DD}',    // memo
      'txt':  '\u{1F4C4}',    // page facing up
      'html': '\u{1F310}',    // globe with meridians
      'css':  '\u{1F3A8}',    // artist palette
      'yml':  '\u2699\uFE0F', // gear
      'yaml': '\u2699\uFE0F',
      'toml': '\u2699\uFE0F',
      'ini':  '\u2699\uFE0F',
      'png':  '\u{1F5BC}\uFE0F', // framed picture
      'jpg':  '\u{1F5BC}\uFE0F',
      'gif':  '\u{1F5BC}\uFE0F',
      'svg':  '\u{1F5BC}\uFE0F',
      'sh':   '\u26A1',       // high voltage
      'bat':  '\u26A1',
      'cmd':  '\u26A1',
      'ps1':  '\u26A1',
    };
    return icons[ext] || '\u{1F4C4}'; // default: page facing up
  },

  /* ---------------------------------------------------------------- */
  /*  _isViewable -- ファイルビューアで開けるファイルかどうか              */
  /* ---------------------------------------------------------------- */

  _isViewable(fileName) {
    var lower = fileName.toLowerCase();

    // 拡張子で判定
    var viewableExts = [
      'md', 'markdown', 'txt',
      'json', 'yml', 'yaml', 'toml',
      'ini', 'cfg', 'conf',
      'env', 'gitignore', 'editorconfig',
      'py', 'js', 'ts', 'html', 'css',
      'sh', 'bat', 'cmd', 'ps1',
    ];
    var dotIndex = lower.lastIndexOf('.');
    if (dotIndex > 0) {
      // 通常のファイル（拡張子あり）: 拡張子を判定
      var ext = lower.substring(dotIndex + 1);
      return viewableExts.indexOf(ext) !== -1;
    }

    if (dotIndex === 0) {
      // ドットファイル（.env, .gitignore 等）: ドット以降を判定
      var dotExt = lower.substring(1);
      return viewableExts.indexOf(dotExt) !== -1;
    }

    // 拡張子なし（Dockerfile, Makefile 等）: テキストファイルとみなす
    return true;
  },

  /* ---------------------------------------------------------------- */
  /*  _sortFiles -- ディレクトリ先頭を維持しつつソート                    */
  /* ---------------------------------------------------------------- */

  _sortFiles(files) {
    var dirs = [];
    var regular = [];

    for (var i = 0; i < files.length; i++) {
      if (files[i].is_dir) {
        dirs.push(files[i]);
      } else {
        regular.push(files[i]);
      }
    }

    var key = this._sortKey;
    var asc = this._sortOrder === 'asc';

    var comparator = function (a, b) {
      var va, vb;
      if (key === 'name') {
        va = a.name.toLowerCase();
        vb = b.name.toLowerCase();
        if (va < vb) return asc ? -1 : 1;
        if (va > vb) return asc ? 1 : -1;
        return 0;
      } else {
        // modified: float タイムスタンプ（大きい=新しい）
        va = a.modified || 0;
        vb = b.modified || 0;
        return asc ? va - vb : vb - va;
      }
    };

    dirs.sort(comparator);
    regular.sort(comparator);

    return dirs.concat(regular);
  },

  /* ---------------------------------------------------------------- */
  /*  cycleSort -- ソート状態を循環切替して再描画                        */
  /* ---------------------------------------------------------------- */

  cycleSort() {
    // 名前昇順 → 名前降順 → 日時降順(新しい順) → 日時昇順(古い順) → 名前昇順
    if (this._sortKey === 'name' && this._sortOrder === 'asc') {
      this._sortOrder = 'desc';
    } else if (this._sortKey === 'name' && this._sortOrder === 'desc') {
      this._sortKey = 'modified';
      this._sortOrder = 'desc';
    } else if (this._sortKey === 'modified' && this._sortOrder === 'desc') {
      this._sortOrder = 'asc';
    } else {
      this._sortKey = 'name';
      this._sortOrder = 'asc';
    }

    this._updateSortButton();
    this._renderTree();
  },

  /* ---------------------------------------------------------------- */
  /*  _resolveRootPath -- レスポンスからルートパスを逆算                  */
  /*  ファイルエントリのフルパスから親ディレクトリを導出する。              */
  /*  空ディレクトリの場合は AppState のセッション cwd にフォールバック。  */
  /* ---------------------------------------------------------------- */

  _resolveRootPath(files) {
    // レスポンスにエントリがある場合、最初のエントリの path から親を逆算
    if (files.length > 0 && files[0].path) {
      return files[0].path.replace(/[\\/][^\\/]+$/, '');
    }

    // 空ディレクトリ: AppState のセッション cwd にフォールバック
    if (typeof AppState !== 'undefined' && AppState.activeSessionId) {
      var session = AppState.sessions.find(function (s) {
        return s.id === AppState.activeSessionId;
      });
      if (session && session.cwd) {
        return session.cwd;
      }
    }

    return '';
  },

  /* ---------------------------------------------------------------- */
  /*  _shortenPath -- パスを短縮表示用に加工                            */
  /* ---------------------------------------------------------------- */

  _shortenPath(fullPath) {
    // パス区切りを統一
    var normalized = fullPath.replace(/\\/g, '/');
    var segments = normalized.split('/').filter(function (s) { return s !== ''; });

    // ドライブレター（C:）がある場合、セグメントの先頭に含まれる
    // 末尾3セグメント以下ならそのまま表示
    if (segments.length <= 3) {
      return segments.join('/');
    }

    // 末尾3セグメントを表示し、先頭を … で省略
    var tail = segments.slice(-3);
    return '\u2026/' + tail.join('/');
  },

  /* ---------------------------------------------------------------- */
  /*  _updateSortButton -- ソートボタンの表示を現在の状態に合わせる       */
  /* ---------------------------------------------------------------- */

  _updateSortButton() {
    var btn = document.getElementById('sort-explorer-btn');
    if (!btn) return;

    var labels = {
      'name-asc':      'A\u2193',    // A↓
      'name-desc':     'A\u2191',    // A↑
      'modified-desc': '\uD83D\uDD53\u2193',  // 🕓↓
      'modified-asc':  '\uD83D\uDD53\u2191',  // 🕓↑
    };

    var titles = {
      'name-asc':      '並び替え: ファイル名 昇順',
      'name-desc':     '並び替え: ファイル名 降順',
      'modified-desc': '並び替え: 更新日時 新しい順',
      'modified-asc':  '並び替え: 更新日時 古い順',
    };

    var state = this._sortKey + '-' + this._sortOrder;
    btn.textContent = labels[state] || 'A\u2193';
    btn.title = titles[state] || '並び替え';
  },
};
