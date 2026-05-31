/**
 * app.js -- Agent Watch Terminal main entry point
 *
 * Responsibility:
 *   - Application state management
 *   - pywebview ready detection and initialization
 *   - Session switching / add / remove orchestration
 *   - Global callbacks for Python backend (onPtyOutput, onStatusChange)
 */

/* ------------------------------------------------------------------ */
/*  App State                                                         */
/* ------------------------------------------------------------------ */

const AppState = {
  sessions:        [],
  activeSessionId: null,
  terminals:       {},   // sessionId -> Terminal instance
  ready:           false,
};

/* ------------------------------------------------------------------ */
/*  Initialization                                                    */
/* ------------------------------------------------------------------ */

window.addEventListener('pywebviewready', async function () {
  AppState.ready = true;
  await initApp();
});

/**
 * Bootstrap sequence executed once pywebview API is available.
 */
async function initApp() {
  // 0. 初期化データを 1 回の IPC で一括取得
  const initData = await window.pywebview.api.get_init_data();
  const settings = initData.settings || {};
  const sessions = initData.sessions || [];
  const buffers  = initData.buffers || {};
  const hints    = initData.restore_hints || {};

  // 1. 設定をフロントエンドに反映
  if (settings.terminal) {
    TerminalManager.applySettings(settings.terminal);
  }
  if (settings.window && settings.window.reduce_motion) {
    document.body.classList.add('reduced-motion');
  }
  AppState.sessions = sessions;

  // 2. セッション一覧を描画
  SessionUI.renderSessionList(sessions);

  // 3. xterm.js インスタンス生成 + バッファ復元 + ヒント表示
  for (const session of sessions) {
    TerminalManager.createTerminal(session.id);
    CustomScrollbar.create(session.id);

    if (buffers[session.id]) {
      TerminalManager.restoreBuffer(session.id, buffers[session.id]);
    }
    if (hints[session.id]) {
      var term = TerminalManager.getTerminal(session.id);
      if (term) {
        term.write(hints[session.id]);
      }
    }
  }

  // 4. 先頭セッションをアクティブ化
  if (sessions.length > 0) {
    await switchSession(sessions[0].id);
  }

  // 5. リサイズハンドラーを初期化
  ResizeHandler.init();
}

/* ------------------------------------------------------------------ */
/*  Global callbacks (called from Python via evaluate_js)             */
/* ------------------------------------------------------------------ */

/**
 * Python backend calls this when PTY produces output.
 * Overwrites the stub defined in index.html.
 */
window.onPtyOutput = function (sessionId, data) {
  const term = TerminalManager.getTerminal(sessionId);
  if (term) {
    // data is base64-encoded UTF-8 bytes from Python backend.
    // atob() returns a Latin-1 binary string -- we must convert it
    // back to a Uint8Array so xterm.js decodes it as UTF-8 (otherwise
    // multi-byte chars like box-drawing become "â" sequences).
    try {
      var binary = atob(data);
      var bytes = new Uint8Array(binary.length);
      for (var i = 0; i < binary.length; i++) {
        bytes[i] = binary.charCodeAt(i);
      }
      term.write(bytes);
    } catch (e) {
      term.write(data);
    }
  }
};

/**
 * Python backend calls this when the working directory changes (OSC 7).
 * Refreshes the file explorer if the changed session is active.
 */
window.onCwdChange = function (sessionId, newCwd) {
  if (sessionId === AppState.activeSessionId) {
    FileExplorerUI.refresh(newCwd);
  }
};

/**
 * Python backend calls this when AI agent status changes.
 * data = { session_id, status, agent, agent_name }
 */
window.onStatusChange = function (data) {
  SessionUI.updateSessionStatus(data.session_id, data.status, data.agent_name);

  // Sync AppState
  const session = AppState.sessions.find(function (s) {
    return s.id === data.session_id;
  });
  if (session) {
    session.status     = data.status;
    session.agent      = data.agent;
    session.agent_name = data.agent_name;
  }
};

/* ------------------------------------------------------------------ */
/*  Session switching                                                 */
/* ------------------------------------------------------------------ */

/**
 * Switch the active session.
 * Updates backend, UI, and terminal focus.
 */
async function switchSession(sessionId) {
  AppState.activeSessionId = sessionId;

  // バックエンドへ並列通知
  await Promise.all([
    window.pywebview.api.set_active_session(sessionId),
    window.pywebview.api.mark_session_read(sessionId),
  ]);

  // Update UI
  TerminalManager.showTerminal(sessionId);
  SessionUI.setActiveSession(sessionId);

  // Refresh file explorer
  await FileExplorerUI.refresh();

  // Focus terminal
  // ダブルクリック時は click → click → dblclick の順で発火するため、
  // 2 回の click で起動した非同期 switchSession が dblclick（rename 開始）
  // 後に resolve して term.focus() でフォーカスを奪ってしまう。
  // rename 中の input が存在する場合はフォーカス奪取をスキップする。
  const renaming = document.querySelector('.session-name-input');
  const term = TerminalManager.getTerminal(sessionId);
  if (term && !renaming) {
    term.focus();
  }
}

/* ------------------------------------------------------------------ */
/*  Add / Remove session                                              */
/* ------------------------------------------------------------------ */

/**
 * Create a new session via backend and activate it.
 */
async function addSession() {
  const session = await window.pywebview.api.add_session();
  AppState.sessions.push(session);
  SessionUI.addSessionItem(session);
  TerminalManager.createTerminal(session.id);
  CustomScrollbar.create(session.id);
  await switchSession(session.id);
}

/**
 * Remove a session. At least one session must remain.
 */
async function removeSession(sessionId) {
  if (AppState.sessions.length <= 1) return; // Keep at least 1

  await window.pywebview.api.remove_session(sessionId);
  AppState.sessions = AppState.sessions.filter(function (s) {
    return s.id !== sessionId;
  });
  CustomScrollbar.destroy(sessionId);
  TerminalManager.destroyTerminal(sessionId);
  SessionUI.removeSessionItem(sessionId);

  // Switch to another session if the removed one was active
  if (AppState.activeSessionId === sessionId && AppState.sessions.length > 0) {
    await switchSession(AppState.sessions[0].id);
  }
}

/* ------------------------------------------------------------------ */
/*  DOM event bindings                                                */
/* ------------------------------------------------------------------ */

document.getElementById('add-session-btn').addEventListener('click', addSession);

document.getElementById('refresh-explorer-btn').addEventListener('click', function (e) {
  e.stopPropagation(); // アコーディオン開閉を防止
  FileExplorerUI.refresh(FileExplorerUI.currentPath);
});

document.getElementById('sort-explorer-btn').addEventListener('click', function (e) {
  e.stopPropagation(); // アコーディオン開閉を防止
  FileExplorerUI.cycleSort();
});
