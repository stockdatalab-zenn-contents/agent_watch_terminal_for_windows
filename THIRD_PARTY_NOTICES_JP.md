# サードパーティ通知

本プロジェクトは以下のサードパーティライブラリおよびアセットを使用しています。

## Python 依存ライブラリ

| 名前 | バージョン | ライセンス | URL |
|---|---|---|---|
| pywebview | >= 5.0 | BSD-3-Clause | https://pypi.org/project/pywebview/ |
| pywinpty | >= 2.0 | MIT | https://pypi.org/project/pywinpty/ |
| winotify | >= 1.1 | MIT | https://pypi.org/project/winotify/ |
| pyperclip | >= 1.8 | BSD-3-Clause | https://github.com/asweigart/pyperclip/blob/master/LICENSE.txt |
| Markdown | >= 3.5 | BSD-3-Clause | https://pypi.org/project/Markdown/ |
| Pillow | >= 10.0 | MIT-CMU | https://pypi.org/project/pillow/ |

## フロントエンド依存ライブラリ

| 名前 | バージョン | ライセンス | URL |
|---|---|---|---|
| xterm.js | 5.3.0 | MIT | https://www.npmjs.com/package/@xterm/xterm |
| xterm-addon-fit | 0.8.0 | MIT | https://www.npmjs.com/package/@xterm/addon-fit |
| xterm-addon-web-links | 0.9.0 | MIT | https://www.npmjs.com/package/@xterm/addon-web-links |
| xterm-addon-serialize | 0.11.0 | MIT | https://www.npmjs.com/package/@xterm/addon-serialize |
| marked | 9.1.0 | MIT | https://www.npmjs.com/package/marked |

## 備考

- 上記すべてのライセンスは、本プロジェクトが採用する MIT License と互換性がある。
- フロントエンドライブラリは `app/frontend/lib/` にバンドルされている。詳細は `app/frontend/lib/LICENSE_THIRD_PARTY.md` を参照。
- 本ターミナル内で使用する外部AIツール（Claude Code、Codex CLI、GitHub Copilot CLI、Bob Shell）は、本プロジェクトのライセンス範囲**外**。各ツールの利用規約に従うこと。
