# Agent Watch Terminal — 基本設計書 機能一覧

| 項目 | 内容 |
|------|------|
| 文書名 | 基本設計書 機能一覧 |
| 対象システム | Agent Watch Terminal |
| 作成日 | 2026-05-30 |
| 版数 | 1.0 |

---

## 1. システム概要

AIコーディングツール専用の「見守りターミナル」。
通常のターミナルとして使用しながら、AIツール（Claude Code / Codex CLI / GitHub Copilot CLI / Bob Shell / opencode）の動作状態を自動判定し、色分け表示・デスクトップ通知で利用者に知らせるWindows デスクトップアプリケーション。

### 1.1 技術スタック

| レイヤ | 技術 | 役割 |
|--------|------|------|
| バックエンド | Python 3.12+ | ビジネスロジック全般 |
| ウィンドウ | pywebview | WebView ウィンドウ管理 |
| ターミナル描画 | xterm.js 5.3.0 | ターミナルエミュレーション |
| PTY | pywinpty | Windows 疑似端末 |
| 通知 | winotify | Windows トースト通知 |
| クリップボード | pyperclip | コピー＆ペースト |
| Markdown | marked.js | プレビュー描画 |

### 1.2 アーキテクチャ概要

```
┌─ pywebview ウィンドウ ─────────────────────────────┐
│  フロントエンド (HTML/CSS/JS + xterm.js)           │
│  ↕  pywebview js_api (双方向ブリッジ)              │
│  バックエンド (Python)                              │
│  ├─ PTY管理           (pty/)                       │
│  ├─ セッション管理     (session/)                   │
│  ├─ AI検出エンジン     (detection/)                 │
│  ├─ 通知システム       (notification/)              │
│  ├─ ファイルエクスプローラ (explorer/)               │
│  ├─ セッション記録     (recording/)                 │
│  └─ セキュリティ       (security/)                  │
└────────────────────────────────────────────────────┘
```

---

## 2. 機能一覧

### 凡例

| 区分 | 説明 |
|------|------|
| 大機能 | システムを構成する主要な機能群 |
| 中機能 | 大機能を構成する個別機能 |
| 小機能 | 中機能の具体的な処理・操作単位 |

---

### F01: ターミナルエミュレーション

| ID | 中機能 | 小機能 | 概要 | 関連モジュール |
|----|--------|--------|------|----------------|
| F01-01 | PTY 生成・管理 | PTY プロセス起動 | セッション作成時に PowerShell プロセスを疑似端末として起動 | `pty/platform_pty.py` |
| F01-01 | | PTY 入出力 | キー入力の送信（write）とターミナル出力の非同期読取り（4KB/20ms） | `pty/pty_manager.py` |
| F01-01 | | PTY リサイズ | ウィンドウ・パネルサイズ変更時に PTY の cols/rows を同期 | `pty/pty_manager.py` |
| F01-01 | | PTY 破棄 | セッション削除・アプリ終了時にプロセスを安全に終了 | `pty/pty_manager.py` |
| F01-02 | ターミナル描画 | xterm.js 描画 | PTY 出力を xterm.js で画面描画（Base64 転送） | `frontend/js/terminal_manager.js` |
| F01-02 | | TUI モード制限 | 代替スクリーンバッファ (1049/47/1047) とマウストラッキング (1000/1002/1003) を遮断し、スクロールバック消失・選択干渉を防止 | `frontend/js/terminal_manager.js` |
| F01-02 | | カーソル可視化 | block カーソルの配色を `!important` で上書きし、truecolor セルの inline style に負けて不可視になる問題を防止（opencode は DECSCUSR `ESC[1 q` で block カーソルを要求） | `frontend/css/terminal.css` |
| F01-02 | | カスタムスクロールバー | xterm 標準スクロールバーを独自オーバーレイに置換（ドラッグ・クリック操作対応） | `frontend/js/scrollbar.js` |
| F01-03 | クリップボード操作 | コピー | 右クリック時、選択テキストをクリップボードへコピー | `frontend/js/context_menu.js` |
| F01-03 | | ペースト | 右クリック時（選択なし）、クリップボード内容を PTY へ送信 | `frontend/js/context_menu.js` |
| F01-04 | リサイズ処理 | ウィンドウリサイズ | ウィンドウサイズ変更をデバウンス（100ms）して xterm.fit() 実行 | `frontend/js/resize_handler.js` |
| F01-04 | | サイドバー幅調整 | ドラッグハンドルによるサイドバー幅変更（最小・最大制約あり） | `frontend/js/resize_handler.js` |
| F01-04 | | パネル高さ調整 | セッション一覧・ファイルエクスプローラ間の垂直分割ドラッグ | `frontend/js/resize_handler.js` |
| F01-04 | | アコーディオン折畳 | パネルヘッダクリックで折畳・展開を切替 | `frontend/js/resize_handler.js` |

---

### F02: マルチセッション管理

| ID | 中機能 | 小機能 | 概要 | 関連モジュール |
|----|--------|--------|------|----------------|
| F02-01 | セッション CRUD | 追加 | [+新規] ボタンで新規セッション作成。PTY を即時起動 | `session/session_manager.py` |
| F02-01 | | 削除 | ×ボタンでセッション削除（最低 1 セッション維持） | `session/session_manager.py` |
| F02-01 | | 名前変更 | セッション名ダブルクリックでインライン編集 | `frontend/js/session_ui.js` |
| F02-01 | | 切替 | セッションリストクリックで表示・操作対象を切替 | `frontend/js/session_ui.js` |
| F02-02 | セッション永続化 | 状態保存 | セッション情報を `data/sessions.json` にアトミック書込み（一時ファイル＋replace） | `session/session_store.py` |
| F02-02 | | 復元 | アプリ起動時に前回セッションを復元（PTY 再生成） | `session/session_manager.py` |
| F02-02 | | バッファ永続化 | アプリ終了時に xterm.js の画面バッファをシリアライズして保存、次回起動時に復元 | `frontend/js/terminal_manager.js`, `main.py` |
| F02-03 | 未読管理 | 未読マーク付与 | 非アクティブセッションの状態変化時にバッジ表示 | `session/session_manager.py` |
| F02-03 | | 未読クリア | セッション切替時にバッジを消去 | `frontend/js/session_ui.js` |

---

### F03: AI エージェント検出

| ID | 中機能 | 小機能 | 概要 | 関連モジュール |
|----|--------|--------|------|----------------|
| F03-01 | ゲート検出 | エージェント起動検出 | PTY 出力をパターンマッチし、AI ツールの起動を検知（ゲートオープン） | `detection/agent_detector.py` |
| F03-01 | | ゲートクローズ | Ctrl+C 連打（閾値到達）でゲートを閉じ、検出を停止。シェルプロンプト検出時はゲートを維持し `exited_to_shell` フラグのみ設定 | `detection/agent_detector.py` |
| F03-02 | ステータス判定 | パターンマッチ判定 | 正規表現パターンで「waiting（許可待ち）」「error（エラー）」を検出 | `detection/pattern_matcher.py` |
| F03-02 | | 同時出現判定 | `status_combo_patterns` の AND 条件グループを、直近出力ウィンドウ（リングバッファ＋末尾バッファの連結）に対して判定。1 フラグメントでは表現できない「複数ボタンの同時表示」を検出する。パターンマッチ判定より優先 | `detection/pattern_matcher.py`, `detection/agent_detector.py` |
| F03-02 | | スループット判定 | 出力が閾値時間（3秒）継続すると「running（実行中）」と判定。ただし同時出現条件が成立中は running へ遷移させない | `detection/agent_detector.py` |
| F03-02 | | シェルプロンプト判定 | シェルプロンプトパターンを検出し、即座に「idle（待機）」へ遷移 | `detection/pattern_matcher.py` |
| F03-02 | | デバウンス確認 | 出力停止後 3 秒間のリングバッファを走査し、ステータスを確定。判定順は 同時出現 → リングバッファ（行単位）→ 末尾バッファ | `detection/agent_detector.py` |
| F03-03 | ANSI 処理 | エスケープ除去 | ANSI エスケープシーケンスを除去（CUF/CHA はスペース置換で語境界を保持） | `detection/agent_patterns.py` |
| F03-03 | | 重複抑制 | 同一ステータス+テキストの重複検出を抑制（last_emitted_key 方式） | `detection/agent_detector.py` |
| F03-04 | リングバッファ | フラグメント蓄積 | PTY 出力を `\r` 分割して (timestamp, text) タプルで蓄積（4KB 上限） | `detection/agent_detector.py` |
| F03-04 | | 経過時間フィルタ | 6 秒超の古いフラグメントを無視し、前タスクの残骸による誤検知を防止 | `detection/agent_detector.py` |
| F03-05 | 対応エージェント | Claude Code | `claude` コマンド起動を検出。waiting / error パターン定義済み | `detection/agent_patterns.py` |
| F03-05 | | Codex CLI | `codex` コマンド起動を検出 | `detection/agent_patterns.py` |
| F03-05 | | GitHub Copilot CLI | `copilot` コマンド起動を検出 | `detection/agent_patterns.py` |
| F03-05 | | Bob Shell | `bob` コマンド起動を検出 | `detection/agent_patterns.py` |
| F03-05 | | opencode | `opencode` コマンド起動を検出。復元・継続コマンドのシェルエコー（`opencode --session/-s/--continue/-c`）もゲートパターンに追加し、復元起動画面で入力欄プレースホルダ等の既存アンカーが表示されずゲートが開かない不具合に対応。散文や本リポジトリのドキュメント自身への誤検出（実測 11 件）を受け、行頭/シェルプロンプト末尾限定・セッション ID 必須・入力欄プレースホルダの三点リーダ必須へ厳格化（誤マッチ 0 の組合せを採用）。`Allow once` + `Allow always`、`Confirm` + `Cancel` の同時表示を `status_combo_patterns` で waiting 判定 | `detection/agent_patterns.py` |

---

### F04: 通知システム

| ID | 中機能 | 小機能 | 概要 | 関連モジュール |
|----|--------|--------|------|----------------|
| F04-01 | 通知オーケストレーション | 通知判定 | ステータス変化時に通知種別（toast / taskbar / UI 内部）を決定 | `notification/notification_manager.py` |
| F04-01 | | フォアグラウンド判定 | ウィンドウがアクティブの場合、外部通知（toast / taskbar）を抑制 | `notification/notification_manager.py` |
| F04-02 | トースト通知 | Windows 通知 | winotify で OS ネイティブのトースト通知を表示（音声付き） | `notification/toast_notifier.py` |
| F04-02 | | フォールバック | winotify 不在時は plyer を使用。両方不在時は no-op で継続 | `notification/toast_notifier.py` |
| F04-03 | タスクバー点滅 | ウィンドウ点滅 | Win32 FlashWindowEx API でタスクバーアイコンを点滅 | `notification/taskbar_flasher.py` |
| F04-03 | | フォアグラウンド復帰で停止 | ウィンドウがフォアグラウンドに復帰すると自動停止 | `notification/taskbar_flasher.py` |
| F04-04 | UI 内通知 | ステータスカラー | セッション一覧のステータスラベルを状態色（Catppuccin Mocha 準拠）で表示 | `frontend/js/session_ui.js` |
| F04-04 | | 未読バッジ | 非アクティブセッションに未読ドット表示 | `frontend/js/session_ui.js` |

---

### F05: ファイルエクスプローラ

| ID | 中機能 | 小機能 | 概要 | 関連モジュール |
|----|--------|--------|------|----------------|
| F05-01 | ディレクトリ表示 | ファイル一覧 | カレントディレクトリの内容をフラットリスト（深さインデント）で表示 | `explorer/file_explorer.py` |
| F05-01 | | フォルダ展開 | フォルダクリックで子要素を遅延読込・展開 | `frontend/js/file_explorer_ui.js` |
| F05-01 | | ソート | 名前・更新日時でクライアント側ソート | `frontend/js/file_explorer_ui.js` |
| F05-01 | | 拡張子アイコン | ファイル種別に応じた絵文字アイコンを表示 | `frontend/js/file_explorer_ui.js` |
| F05-02 | CWD 連動 | OSC 7 検出 | シェルの OSC 7 エスケープシーケンスからカレントディレクトリを取得 | `main.py` |
| F05-02 | | WSL パス変換 | WSL パス (`/mnt/c/...`, `/home/...`) を Windows パス・UNC パスに変換 | `main.py` |
| F05-02 | | 自動リフレッシュ | CWD 変更時にファイルエクスプローラを自動更新 | `frontend/js/app.js` |
| F05-03 | ファイル操作 | OS で開く | ダブルクリックで OS 既定アプリケーションにて開く | `explorer/file_explorer.py` |
| F05-03 | | テキスト読取 | テキストファイルを読取（1MB 上限、UTF-8） | `explorer/file_explorer.py` |
| F05-03 | | ファイル保存 | ホワイトリスト拡張子（.md, .py, .json 等）のテキスト保存 | `explorer/file_explorer.py` |

---

### F06: ファイルビューア・エディタ

| ID | 中機能 | 小機能 | 概要 | 関連モジュール |
|----|--------|--------|------|----------------|
| F06-01 | マルチペインモーダル | ペイン追加 | ファイルをモーダル内に最大 4 ペインまで同時表示 | `frontend/js/file_viewer_modal.js` |
| F06-01 | | ペイン削除 | 未保存確認付きでペインを閉じる | `frontend/js/file_viewer_modal.js` |
| F06-01 | | ペイン幅調整 | ドラッグハンドルで水平分割比率を変更 | `frontend/js/file_viewer_modal.js` |
| F06-02 | 編集機能 | 編集切替 | 読取専用 → 編集モードのトグル | `frontend/js/file_viewer_pane.js` |
| F06-02 | | 保存・破棄 | 編集内容の保存ボタン・破棄ボタン | `frontend/js/file_viewer_pane.js` |
| F06-02 | | 未保存表示 | 未保存変更の視覚的インジケータ | `frontend/js/file_viewer_pane.js` |
| F06-03 | Markdown プレビュー | プレビュー描画 | クライアント側 marked.js で Markdown → HTML 変換（fenced_code / tables 対応） | `frontend/js/file_viewer_pane.js` |
| F06-03 | | 外部リンク | プレビュー内の http(s) リンククリック時、ウィンドウ内ナビゲーションを阻止し OS 既定ブラウザで開く | `frontend/js/file_viewer_pane.js`, `api.py` |

---

### F07: AI セッション自動復元

| ID | 中機能 | 小機能 | 概要 | 関連モジュール |
|----|--------|--------|------|----------------|
| F07-01 | セッション名付与 | 自動リネーム | アプリ終了時に、AI ツールのセッション名を取得するリネームコマンドを各 PTY へ送信 | `main.py` |
| F07-01 | | リネーム不要判定 | `rename_command` 未定義の AI ツール（opencode 等）は命名不要として `agent_session_named` を即時 True 化 | `main.py` |
| F07-02 | セッション ID 収集 | CLI 問合せ | アプリ終了時に AI ツールのローカルデータから稼働中セッション ID を収集（opencode は SQLite `opencode.db` を読取り専用参照、旧 JSON 形式へフォールバック） | `session/session_id_collector.py` |
| F07-02 | | セッション照合 | エージェント定義の `session_match`（`none`/`name_cwd`/`cwd_latest`）に従い sessions.json と照合し `agent_session_id` を紐付け。cwd 一致のうち最終更新が新しい順に割当てる `cwd_latest`（opencode 向け）を追加し、旧来のハードコード分岐（codex/bob）を廃止。`cwd_latest` は 2 パス構成。第 1 パスで前回取得済み ID がまだ有効なセッションへ優先予約し、同一 cwd の複数タブ間で `agent_session_id` が入れ替わる不具合を防止。第 2 パスで残りをゲート開放時刻（`agent_started_at`）以降に更新されたもののうち最終更新が新しい順に割当 | `session/session_manager.py`, `detection/agent_patterns.py` |
| F07-03 | 自動再開 | コマンド送信 | 起動時に `resume_command`（例: `claude --resume "{name}"` / ID ベースは `opencode --session {agent_session_id}`）を PTY へ送信し、前回セッションを復元 | `main.py`, `detection/agent_patterns.py` |
| F07-03 | | 復元ヒント表示 | 自動再開不可の場合、手動復元プロンプト（コマンド文字列）をターミナルに表示。`{agent_session_id}` を含む `resume_command` にも対応し両プレースホルダを渡す仕様に修正 | `api.py` |

---

### F08: セッション記録・レポート

| ID | 中機能 | 小機能 | 概要 | 関連モジュール |
|----|--------|--------|------|----------------|
| F08-01 | 記録制御 | 記録開始 | `data/recording/{YYYYMMDD_HHMMSS}/` ディレクトリを作成し、イベント蓄積を開始 | `recording/session_recorder.py` |
| F08-01 | | 記録停止 | `events.json` をフラッシュし、記録を終了 | `recording/session_recorder.py` |
| F08-02 | イベント記録 | イベント保存 | タイムスタンプ・セッション ID・イベント種別・ステータス・付帯データを JSON 配列に蓄積 | `recording/session_recorder.py` |
| F08-02 | | スクリーンショット | 状態変化時のターミナル画面を PNG で保存 | `recording/session_recorder.py` |
| F08-03 | レポート生成 | HTML レポート | `events.json` からセッション別タイムライン HTML を生成（Base64 画像埋込・Catppuccin テーマ）。**未実装**（旧実装は `99_old/` に退蔵） | — |

---

### F09: セキュリティ

| ID | 中機能 | 小機能 | 概要 | 関連モジュール |
|----|--------|--------|------|----------------|
| F09-01 | ログマスキング | API キー検出 | Anthropic キー (`sk-ant-...`) / OpenAI キー (`sk-...`) を `***` に置換 | `security/log_masker.py` |
| F09-01 | | 環境変数検出 | PowerShell (`$env:`) / Shell (`KEY=`) 形式の環境変数値をマスク | `security/log_masker.py` |
| F09-01 | | トークン検出 | Bearer トークン・長いランダム文字列（48 文字以上）をマスク | `security/log_masker.py` |
| F09-01 | | ログフィルタ統合 | Python logging の Filter として統合し、全ログハンドラに自動適用 | `security/log_masker.py` |
| F09-02 | ファイル操作制限 | 書込みホワイトリスト | 保存可能なファイル拡張子をホワイトリストで制限 | `explorer/file_explorer.py` |
| F09-02 | | 読取サイズ制限 | テキスト読取を 1MB に制限し、大容量ファイルによるメモリ枯渇を防止 | `explorer/file_explorer.py` |
| F09-02 | | 画像読取サイズ制限 | 画像の Base64 読取を 5MB に制限 | `explorer/file_explorer.py` |

---

### F10: 設定管理

| ID | 中機能 | 小機能 | 概要 | 関連モジュール |
|----|--------|--------|------|----------------|
| F10-01 | 設定読込・保存 | JSON 管理 | `settings.json` の読込・キャッシュ・アトミック書込み（一時ファイル＋replace） | `config/settings_manager.py` |
| F10-01 | | ドット記法アクセス | `get("terminal.font_size")` のような階層キーでのアクセス | `config/settings_manager.py` |
| F10-01 | | スレッドセーフ | threading.Lock による排他制御 | `config/settings_manager.py` |
| F10-02 | ターミナル設定 | フォント | フォントファミリー・サイズ。ターミナル生成時に xterm.js のオプションへ反映 | `config/settings.json`, `frontend/js/terminal_manager.js` |
| F10-02 | | カーソル | スタイル（bar/block/underline）・点滅有無。ターミナル生成時に xterm.js のオプションへ反映 | `config/settings.json`, `frontend/js/terminal_manager.js` |
| F10-02 | | スクロールバック | バッファ行数（既定: 2000） | `config/settings.json` |
| F10-02 | | カラーテーマ | Catppuccin Mocha 準拠の 16 色 ANSI カラー + 背景・前景。snake_case のキーを xterm.js の ITheme（camelCase）へ変換して反映。未知のキー・非文字列は無視 | `config/settings.json`, `frontend/js/terminal_manager.js` |
| F10-02 | | カーソル配色の CSS 連携 | `theme.cursor` / `theme.cursor_accent` を CSS カスタムプロパティ（`--terminal-cursor` / `--terminal-cursor-accent`）へ反映し、block カーソルの `!important` 指定と配色を揃える | `frontend/js/terminal_manager.js`, `frontend/css/theme.css` |
| F10-02 | | 背景透過 | `allow_transparency` フラグ（ターミナル背景の透過設定） | `config/settings.json` |
| F10-03 | ウィンドウ設定 | サイズ・透明度 | 初期ウィンドウサイズ・透明度 (0.0-1.0) | `config/settings.json` |
| F10-03 | | 最前面表示 | always_on_top フラグ | `config/settings.json` |
| F10-03 | | デバッグモード | DevTools 表示の有効化 | `config/settings.json` |
| F10-03 | | アニメーション抑制 | reduce_motion フラグ（`transition-duration: 0s` + `animation: none`）。duration を 0s にすると最終キーフレームで固定される挙動を避けるため animation 自体を無効化 | `config/settings.json`, `frontend/css/theme.css` |
| F10-04 | 通知設定 | トースト有効化 | toast_enabled フラグ | `config/settings.json` |
| F10-04 | | タスクバー点滅有効化 | taskbar_flash_enabled フラグ | `config/settings.json` |
| F10-04 | | 検出閾値 | `debounce_ms`・`running_threshold_ms`・`waiting_recovery_threshold_ms`・`error_recovery_threshold_ms`・`ctrlc_window_ms` | `config/settings.json` |
| F10-05 | ログ設定 | ログレベル | INFO / DEBUG 等の出力レベル | `config/settings.json` |
| F10-05 | | PTY ログ | PTY 生出力のファイル記録の有効化 | `config/settings.json` |
| F10-05 | | セッション記録 | セッション記録機能の有効化 | `config/settings.json` |
| F10-06 | ステータス色 | 状態別カラー | running / waiting / error / idle の表示色定義 | `config/settings.json` |

---

## 3. 状態遷移

### 3.1 エージェント検出状態

```
  ┌────────────────┐   ゲートパターン検出   ┌────────────────┐
  │  ゲートクローズ  │──────────────────────▶│  ゲートオープン  │
  │  (検出停止)     │◀──────────────────────│  (検出中)       │
  └────────────────┘  Ctrl+C 閾値到達      └────────────────┘
```

> **補足:** シェルプロンプト検出ではゲートを閉じず、`exited_to_shell` フラグを設定するのみ。
> ゲートは開いたまま維持され、次回の AI ツール起動検出に即応できる。
```

### 3.2 セッションステータス遷移

```
         ゲートオープン
              │
              ▼
        ┌──────────┐
        │   idle   │◀──────────── シェルプロンプト検出
        │  (待機)   │◀──────────── デバウンス: パターン無 + running だった
        └────┬─────┘
             │ スループット閾値超過
             ▼
        ┌──────────┐
        │ running  │◀──────────── 出力継続中
        │ (実行中)  │◀──────────── waiting/error 状態で出力再開(1.5秒)
        └────┬─────┘
             │ 出力停止 + デバウンス(3秒)
             ▼
      ┌──────┴──────┐
      │リングバッファ走査│
      └──────┬──────┘
             │
     ┌───────┼───────┐
     ▼       ▼       ▼
┌────────┐┌────────┐┌────────┐
│waiting ││ error  ││  idle  │
│(許可待)││(エラー) ││ (待機) │
└────────┘└────────┘└────────┘
```

---

## 4. 処理フロー

### 4.1 PTY 出力処理パイプライン

```
PTY バイトデータ受信
  │
  ├─▶ AgentDetector.feed()           ── ゲート判定 → ステータス判定（スループット計測含む）
  │     └─▶ on_status_change()       ── コールバック
  │           ├─▶ SessionManager     ── ステータス更新
  │           ├─▶ NotificationManager── 通知判定・発火
  │           ├─▶ SessionRecorder    ── イベント記録
  │           └─▶ JS evaluate        ── フロントエンド UI 更新
  ├─▶ OSC 7 CWD 検出                 ── カレントディレクトリ変更検出
  ├─▶ PTY ログファイル書込み          ── 生出力のファイル記録
  ├─▶ SessionRecorder               ── 出力イベント記録
  └─▶ JS onPtyOutput()              ── xterm.js へ画面描画
```

### 4.2 通知発火フロー

```
ステータス変化
  │
  ├─ ステータスが "waiting" or "error" ?
  │    └─ Yes → フォアグラウンド判定
  │              ├─ フォアグラウンド → UI 内更新のみ
  │              └─ バックグラウンド
  │                   ├─▶ トースト通知 (有効時)
  │                   └─▶ タスクバー点滅 (有効時)
  │
  ├─ ステータスが "idle" かつ event_type が "shell_prompt" or "debounce" ?
  │    └─ Yes → 「処理完了 (completed)」として フォアグラウンド判定
  │              ├─ フォアグラウンド → UI 内更新のみ
  │              └─ バックグラウンド
  │                   ├─▶ トースト通知 (有効時)
  │                   └─▶ タスクバー点滅 (有効時)
  │
  ├─ それ以外 → UI 内更新のみ
  │
  └─ 未読バッジ更新 (非アクティブセッション時)
```

> **補足:** idle 遷移のうち、ゲートオープン直後（`gate_opened`）は初期状態であり通知対象外。
> シェルプロンプト検出（`shell_prompt`）または running からの出力停止確定（`debounce`）のみ、
> エージェントの処理完了とみなして外部通知を発火する。

---

## 5. データ一覧

### 5.1 永続データ

| データ | ファイル | 形式 | 内容 |
|--------|----------|------|------|
| セッション情報 | `data/sessions.json` | JSON 配列 | `id`・`name`・`cwd`・`order`・`agent_key`・`agent_session_named`・`agent_session_id`・`agent_started_at` |
| セッション ID | `data/session_ids.json` | JSON | AI ツール別セッション ID（終了時に生成） |
| ターミナルバッファ | `data/buffers/{session_id}.txt` | テキスト | xterm.js シリアライズデータ |
| アプリ設定 | `source/config/settings.json` | JSON | 全設定パラメータ |
| 記録イベント | `data/recording/{dir}/events.json` | JSON 配列 | タイムスタンプ付きイベント列 |
| 記録画像 | `data/recording/{dir}/screenshots/` | PNG | 状態変化時のターミナルキャプチャ |

### 5.2 ログデータ

| データ | ファイル | 内容 |
|--------|----------|------|
| アプリログ | `logs/{YYYYMMDD}_agent_watch.log` | アプリケーション全般のログ |
| PTY 出力ログ | `logs/{YYYYMMDD}_pty_output.log` | PTY 生出力（セッション ID 付き） |

---

## 6. 外部インタフェース

### 6.1 JS-Python ブリッジ API（pywebview js_api）

| カテゴリ | メソッド | 方向 | 概要 |
|----------|----------|------|------|
| セッション | `add_session(name)` | JS→PY | セッション追加 |
| | `remove_session(session_id)` | JS→PY | セッション削除 |
| | `rename_session(session_id, name)` | JS→PY | セッション名変更 |
| | `set_active_session(session_id)` | JS→PY | アクティブセッション切替 |
| | `mark_session_read(session_id)` | JS→PY | 未読クリア |
| ターミナル | `send_input(session_id, data)` | JS→PY | キー入力送信 |
| | `resize_terminal(session_id, cols, rows)` | JS→PY | ターミナルリサイズ |
| ファイル | `list_files(path)` | JS→PY | ディレクトリ一覧 |
| | `open_file(path)` | JS→PY | OS でファイルを開く |
| | `open_url(url)` | JS→PY | URL を OS 既定ブラウザで開く |
| | `read_file_content(path)` | JS→PY | テキスト読取 |
| | `read_image_base64(path)` | JS→PY | 画像を Base64 data URI で読取（5MB 上限） |
| | `save_file(path, content)` | JS→PY | テキスト保存 |
| クリップボード | `copy_to_clipboard(text)` | JS→PY | コピー |
| | `paste_from_clipboard()` | JS→PY | ペースト |
| 設定 | `get_init_data()` | JS→PY | 初期化バッチ取得（セッション一覧・設定・バッファを一括返却） |
| 復元 | `get_restore_hints()` | JS→PY | 復元ヒント取得 |
| コールバック | `onPtyOutput(sessionId, base64data)` | PY→JS | PTY 出力転送 |
| | `onCwdChange(sessionId, newCwd)` | PY→JS | CWD 変更通知 |
| | `onStatusChange(data)` | PY→JS | ステータス変更通知 |

---

## 7. 非機能要件

| 項目 | 内容 |
|------|------|
| 対応 OS | Windows 11（主対象）、Linux（PTY 層で対応） |
| Python バージョン | 3.12 以上 |
| データ永続化 | アトミック書込み（一時ファイル＋os.replace）による破損防止 |
| スレッド安全性 | threading.Lock による排他制御（設定・セッション）。通知は `ToastNotifier` のみ Lock 使用 |
| 機密情報保護 | ログ出力時の API キー・トークン自動マスキング |
| グレースフルデグラデーション | 通知ライブラリ不在時は no-op で動作継続 |
| UI テーマ | Catppuccin Mocha 統一 |
| パフォーマンス | PTY 読取 4KB/20ms、リサイズデバウンス 50-100ms |
