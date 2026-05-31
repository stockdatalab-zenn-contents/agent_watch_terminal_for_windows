/**
 * session_ui.js -- Session list rendering and management
 *
 * Responsibility:
 *   - Render the session list in the sidebar (#session-list)
 *   - Add / remove / rename individual session items
 *   - Track active session highlight and unread badges
 *   - Display agent status labels ([agent:status])
 *
 * All backend communication goes through window.pywebview.api.
 */

/* ------------------------------------------------------------------ */
/*  SessionUI                                                         */
/* ------------------------------------------------------------------ */

const SessionUI = {

  /* ---------------------------------------------------------------- */
  /*  Full list rendering                                             */
  /* ---------------------------------------------------------------- */

  /**
   * Clear and re-render the entire session list.
   * @param {Array} sessions - Array of session objects from backend.
   */
  renderSessionList(sessions) {
    const container = document.getElementById('session-list');
    container.innerHTML = '';
    sessions.forEach(session => this.addSessionItem(session));
  },

  /* ---------------------------------------------------------------- */
  /*  Single item CRUD                                                */
  /* ---------------------------------------------------------------- */

  /**
   * Append a single session item to the list.
   * @param {Object} session - { id, name, status?, agent_name?, unread? }
   */
  addSessionItem(session) {
    const container = document.getElementById('session-list');
    const item = document.createElement('div');
    item.className = 'session-item';
    item.id = `session-item-${session.id}`;
    item.dataset.sessionId = session.id;
    item.dataset.status = session.status || 'idle';

    // Status label: [agent:status] or empty
    const statusLabel = document.createElement('span');
    statusLabel.className = 'session-status-label';
    statusLabel.textContent = this._formatStatusLabel(session);

    // Session name
    const nameSpan = document.createElement('span');
    nameSpan.className = 'session-name';
    nameSpan.textContent = session.name;

    // Unread badge
    const badge = document.createElement('span');
    badge.className = 'session-unread-badge';
    badge.textContent = '';
    if (session.unread) {
      badge.textContent = '\u25CF';
      item.classList.add('unread');
    }

    // Close button
    const closeBtn = document.createElement('button');
    closeBtn.className = 'session-close-btn';
    closeBtn.textContent = '\u00D7';
    closeBtn.title = '\u9589\u3058\u308B';
    closeBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      removeSession(session.id);
    });

    item.appendChild(statusLabel);
    item.appendChild(nameSpan);
    item.appendChild(badge);
    item.appendChild(closeBtn);

    // Click to switch session
    item.addEventListener('click', () => switchSession(session.id));

    // Double-click session name to rename
    nameSpan.addEventListener('dblclick', (e) => {
      e.stopPropagation();
      this._startRename(session.id, nameSpan);
    });

    container.appendChild(item);
  },

  /**
   * Remove a session item from the DOM.
   * @param {string} sessionId
   */
  removeSessionItem(sessionId) {
    const item = document.getElementById(`session-item-${sessionId}`);
    if (item) item.remove();
  },

  /* ---------------------------------------------------------------- */
  /*  Active session                                                  */
  /* ---------------------------------------------------------------- */

  /**
   * Highlight the active session and clear its unread state.
   * @param {string} sessionId
   */
  setActiveSession(sessionId) {
    document.querySelectorAll('.session-item').forEach(el => {
      el.classList.toggle('active', el.dataset.sessionId === sessionId);
    });

    // Clear unread for this session
    const item = document.getElementById(`session-item-${sessionId}`);
    if (item) {
      item.classList.remove('unread');
      const badge = item.querySelector('.session-unread-badge');
      if (badge) badge.textContent = '';
    }
  },

  /* ---------------------------------------------------------------- */
  /*  Status updates                                                  */
  /* ---------------------------------------------------------------- */

  /**
   * Update the status label and unread indicator for a session.
   * @param {string} sessionId
   * @param {string} status    - e.g. 'running', 'waiting', 'idle'
   * @param {string} agentName - e.g. 'Claude Code'
   */
  updateSessionStatus(sessionId, status, agentName) {
    const item = document.getElementById(`session-item-${sessionId}`);
    if (!item) return;

    item.dataset.status = status;

    const label = item.querySelector('.session-status-label');
    if (label) {
      if (agentName) {
        // ゲート開放中: 全ステータス（idle 含む）でラベル表示
        label.textContent = `[${agentName.toLowerCase().split(' ')[0]}:${status}]`;
      } else {
        // ゲート閉鎖（agentName=None）: ラベル消去
        label.textContent = '';
      }
    }

    // If not the active session, mark as unread
    if (sessionId !== AppState.activeSessionId) {
      item.classList.add('unread');
      const badge = item.querySelector('.session-unread-badge');
      if (badge) badge.textContent = '\u25CF';
    }
  },

  /* ---------------------------------------------------------------- */
  /*  Internal helpers                                                */
  /* ---------------------------------------------------------------- */

  /**
   * Build the status label string from a session object.
   * @param {Object} session
   * @returns {string}
   */
  _formatStatusLabel(session) {
    // Python 側は agent、onStatusChange 経由は agent_name
    const agentName = session.agent_name || session.agent;
    if (agentName && session.status) {
      return `[${agentName.toLowerCase().split(' ')[0]}:${session.status}]`;
    }
    return '';
  },

  /**
   * Replace the session name span with an input for inline renaming.
   * @param {string} sessionId
   * @param {HTMLElement} nameSpan
   */
  _startRename(sessionId, nameSpan) {
    const currentName = nameSpan.textContent;
    const input = document.createElement('input');
    input.type = 'text';
    input.className = 'session-name-input';
    input.value = currentName;

    nameSpan.replaceWith(input);
    input.focus();
    input.select();

    const finishRename = async () => {
      const newName = input.value.trim() || currentName;
      const newSpan = document.createElement('span');
      newSpan.className = 'session-name';
      newSpan.textContent = newName;

      // Re-attach dblclick for future renames
      newSpan.addEventListener('dblclick', (e) => {
        e.stopPropagation();
        SessionUI._startRename(sessionId, newSpan);
      });

      input.replaceWith(newSpan);

      if (newName !== currentName) {
        await window.pywebview.api.rename_session(sessionId, newName);
        const session = AppState.sessions.find(s => s.id === sessionId);
        if (session) session.name = newName;
      }
    };

    input.addEventListener('blur', finishRename);
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { input.blur(); }
      if (e.key === 'Escape') { input.value = currentName; input.blur(); }
    });
  },
};
