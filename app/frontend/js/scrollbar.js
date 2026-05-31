/**
 * scrollbar.js -- Agent Watch Terminal: custom scrollbar handler
 *
 * Responsibility:
 *   - Create / destroy custom scrollbar overlays for xterm.js terminals
 *   - Synchronize thumb position with terminal scroll state
 *   - Handle thumb drag and track click to scroll the terminal
 *
 * Dependencies:
 *   - TerminalManager (terminal_manager.js) -- getTerminal()
 *   - scrollbar.css -- visual styling & hover transitions
 */

/* ------------------------------------------------------------------ */
/*  CustomScrollbar                                                    */
/* ------------------------------------------------------------------ */

const CustomScrollbar = {

  /** @type {Object.<string, {scrollbar: HTMLElement, track: HTMLElement, thumb: HTMLElement, dragging: boolean, dragStartY: number, dragStartTop: number}>} */
  scrollbars: {},

  /* ---- create ---------------------------------------------------- */

  /**
   * Create and attach a custom scrollbar to the terminal wrapper
   * identified by `terminal-{sessionId}`.
   *
   * @param {string} sessionId - Session whose terminal receives the scrollbar.
   */
  create(sessionId) {
    var wrapper = document.getElementById('terminal-' + sessionId);
    if (!wrapper) return;

    // -- Build DOM elements --
    var scrollbar = document.createElement('div');
    scrollbar.className = 'custom-scrollbar';

    var track = document.createElement('div');
    track.className = 'custom-scrollbar-track';

    var thumb = document.createElement('div');
    thumb.className = 'custom-scrollbar-thumb';

    track.appendChild(thumb);
    scrollbar.appendChild(track);
    wrapper.appendChild(scrollbar);

    // -- Internal state --
    var state = {
      scrollbar:    scrollbar,
      track:        track,
      thumb:        thumb,
      dragging:     false,
      dragStartY:   0,
      dragStartTop: 0,
    };

    this.scrollbars[sessionId] = state;

    // -- Terminal reference --
    var term = TerminalManager.getTerminal(sessionId);
    if (!term) return;

    // -- Sync thumb on scroll / new content --
    var self = this;

    term.onScroll(function () {
      self._updateThumb(sessionId);
    });

    term.onLineFeed(function () {
      self._updateThumb(sessionId);
    });

    // -- Thumb drag --
    thumb.addEventListener('mousedown', function (e) {
      e.preventDefault();
      state.dragging     = true;
      state.dragStartY   = e.clientY;
      state.dragStartTop = parseFloat(thumb.style.top) || 0;
      thumb.classList.add('dragging');
      scrollbar.classList.add('active');

      var onMouseMove = function (e) {
        if (!state.dragging) return;

        var delta       = e.clientY - state.dragStartY;
        var trackHeight = track.offsetHeight;
        var thumbHeight = thumb.offsetHeight;
        var maxTop      = trackHeight - thumbHeight;
        var newTop      = Math.max(0, Math.min(maxTop, state.dragStartTop + delta));

        thumb.style.top = newTop + 'px';

        // Translate thumb position to terminal scroll offset
        var scrollFraction = newTop / maxTop;
        var buffer         = term.buffer.active;
        var maxScroll      = buffer.baseY;
        var targetLine     = Math.round(scrollFraction * maxScroll);
        term.scrollToLine(targetLine);
      };

      var onMouseUp = function () {
        state.dragging = false;
        thumb.classList.remove('dragging');
        scrollbar.classList.remove('active');
        document.removeEventListener('mousemove', onMouseMove);
        document.removeEventListener('mouseup', onMouseUp);
      };

      document.addEventListener('mousemove', onMouseMove);
      document.addEventListener('mouseup', onMouseUp);
    });

    // -- Track click -> jump scroll --
    track.addEventListener('click', function (e) {
      if (e.target === thumb) return;

      var trackRect      = track.getBoundingClientRect();
      var clickY         = e.clientY - trackRect.top;
      var trackHeight    = trackRect.height;
      var scrollFraction = clickY / trackHeight;

      var buffer     = term.buffer.active;
      var maxScroll  = buffer.baseY;
      var targetLine = Math.round(scrollFraction * maxScroll);
      term.scrollToLine(targetLine);
    });

    // -- Initial thumb position --
    this._updateThumb(sessionId);
  },

  /* ---- _updateThumb (private) ----------------------------------- */

  /**
   * Recalculate thumb size and position based on the terminal's
   * current scroll state.  Skipped while the user is dragging.
   *
   * @param {string} sessionId
   */
  _updateThumb(sessionId) {
    var state = this.scrollbars[sessionId];
    if (!state || state.dragging) return;

    var term = TerminalManager.getTerminal(sessionId);
    if (!term) return;

    var buffer       = term.buffer.active;
    var totalRows    = buffer.baseY + term.rows;
    var viewportRows = term.rows;

    // Hide thumb when all content fits in the viewport
    if (totalRows <= viewportRows) {
      state.thumb.style.display = 'none';
      return;
    }

    state.thumb.style.display = 'block';

    var trackHeight    = state.track.offsetHeight;
    var thumbHeight    = Math.max(30, (viewportRows / totalRows) * trackHeight);
    var maxTop         = trackHeight - thumbHeight;
    var scrollFraction = buffer.viewportY / buffer.baseY;
    var thumbTop       = scrollFraction * maxTop;

    state.thumb.style.height = thumbHeight + 'px';
    state.thumb.style.top    = thumbTop + 'px';
  },

  /* ---- destroy -------------------------------------------------- */

  /**
   * Remove scrollbar DOM elements and clean up stored state
   * for the given session.
   *
   * @param {string} sessionId
   */
  destroy(sessionId) {
    var state = this.scrollbars[sessionId];
    if (state) {
      state.scrollbar.remove();
      delete this.scrollbars[sessionId];
    }
  },
};
