(function () {
  const BASE = window.WCMUX_BASE_URL || "";
  const MAX_TABS = 20;

  // Spec §4.10: URL ?cwd=<path> identifies the workspace AND sets the starting
  // cwd for new tabs. No separate workspace id.
  const _urlParams = new URLSearchParams(location.search);
  const WORKSPACE_CWD = _urlParams.get("cwd") || "";  // "" → server HOME default
  // Set to true once we've learned the URL's cwd is no longer valid server-side;
  // subsequent requests omit the cwd param and land in the server default workspace.
  let _fallback_active = false;
  const statusEl = document.getElementById("status");
  const tabsEl = document.getElementById("tabs");
  const termsEl = document.getElementById("terminals");
  const newTabBtn = document.getElementById("new-tab");

  /** @type {Map<string, {id:string, name:string, cwdDisplay:string, cwdFull:string,
   *                      term:any, fit:any, ws:WebSocket|null, wrap:HTMLElement, tabEl:HTMLElement,
   *                      unread:boolean, sentReplay:boolean}>} */
  const tabs = new Map();
  let activeId = null;

  // Sticky modifiers for the virtual keypad. Apply to the next character
  // coming from xterm (physical or soft keyboard) per spec §4.12.
  const stickyMods = { ctrl: false, alt: false };
  function clearStickyMods() {
    stickyMods.ctrl = false; stickyMods.alt = false;
    document.querySelectorAll(".kp.mod").forEach((b) => b.classList.remove("on"));
  }
  function ctrlOf(ch) {
    // Ctrl+<char> → ASCII control char, else null (caller sends plain char).
    const lo = ch.toLowerCase();
    if (lo >= "a" && lo <= "z") return String.fromCharCode(lo.charCodeAt(0) - 96);
    const map = { "@": "\x00", "[": "\x1b", "\\": "\x1c", "]": "\x1d",
                  "^": "\x1e", "_": "\x1f", "?": "\x7f" };
    return Object.prototype.hasOwnProperty.call(map, ch) ? map[ch] : null;
  }
  function transformStickyFirstChar(data) {
    // Apply sticky Ctrl/Alt to the first code point of `data` (spec §4.12).
    // Returns the transformed string; clears sticky state as a side effect.
    if (!data || (!stickyMods.ctrl && !stickyMods.alt)) return data;
    // first UTF-16 code unit is fine here: control chars we target are ASCII
    const first = data[0];
    const rest = data.slice(1);
    let out = first;
    if (stickyMods.ctrl) {
      const c = ctrlOf(first);
      if (c !== null) out = c;
      // else: no mapping, send as-is
    }
    if (stickyMods.alt) out = "\x1b" + out;
    clearStickyMods();
    return out + rest;
  }

  let _menuRefreshScheduled = false;
  function scheduleMenuRefresh() {
    if (_menuRefreshScheduled) return;
    _menuRefreshScheduled = true;
    queueMicrotask(() => {
      _menuRefreshScheduled = false;
      // renderTabMenu bails out when the menu is hidden, so this is cheap.
      try { renderTabMenu(); } catch {}
    });
  }

  function updateIndicator() {
    let anyOpen = false, anyConnecting = false;
    for (const t of tabs.values()) {
      if (!t.ws) { anyConnecting = true; continue; }
      const rs = t.ws.readyState;
      if (rs === WebSocket.OPEN) anyOpen = true;
      else if (rs === WebSocket.CONNECTING) anyConnecting = true;
    }
    if (tabs.size === 0) {
      statusEl.className = "status warn"; statusEl.title = "loading"; return;
    }
    if (anyOpen) { statusEl.className = "status ok"; statusEl.title = "connected"; }
    else if (anyConnecting) { statusEl.className = "status warn"; statusEl.title = "reconnecting"; }
    else { statusEl.className = "status err"; statusEl.title = "disconnected"; }
  }

  function withWorkspace(path) {
    // Once the URL's cwd has been rejected as invalid, stop sending it.
    if (!WORKSPACE_CWD || _fallback_active) return path;
    const sep = path.includes("?") ? "&" : "?";
    return path + sep + "cwd=" + encodeURIComponent(WORKSPACE_CWD);
  }

  // Spec §4.19: long-lived device token in localStorage. Lets us re-establish
  // the session cookie without prompting the user when the browser drops it.
  const DEVICE_TOKEN_KEY = "wcmux_device_token";
  const DEVICE_ID_KEY = "wcmux_device_id";

  async function tryExchangeDeviceToken() {
    const tok = (() => { try { return localStorage.getItem(DEVICE_TOKEN_KEY); } catch { return null; } })();
    if (!tok) return false;
    try {
      const r = await fetch(BASE + "/api/auth/exchange", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({ token: tok }),
      });
      if (!r.ok) {
        if (r.status === 401) {
          // token revoked or signature dead — drop it so we don't loop.
          try { localStorage.removeItem(DEVICE_TOKEN_KEY); localStorage.removeItem(DEVICE_ID_KEY); } catch {}
        }
        return false;
      }
      return true;
    } catch {
      return false;
    }
  }

  function api(method, path, body, { noWorkspace = false, _retried = false } = {}) {
    const opts = { method, headers: {}, credentials: "same-origin" };
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    const finalPath = noWorkspace ? path : withWorkspace(path);
    return fetch(BASE + finalPath, opts).then(async (r) => {
      if (r.status === 401) {
        // Try once to resurrect the session via a stored device token.
        if (!_retried && await tryExchangeDeviceToken()) {
          return api(method, path, body, { noWorkspace, _retried: true });
        }
        // No token, exchange failed, or already retried: jump to login.
        window.location.href = BASE + "/login";
        throw Object.assign(new Error("auth required"), { status: 401 });
      }
      if (!r.ok) {
        const err = new Error(`${method} ${path}: ${r.status}`);
        err.status = r.status;
        throw err;
      }
      return r.json();
    });
  }

  function renderTab(t) {
    // Whenever any tab's visual state changes, the brand menu (if open) should
    // reflect it. Defer the DOM rebuild to the end of the microtask queue so
    // callers that update name/cwd in a tight loop don't thrash the menu.
    scheduleMenuRefresh();
    t.tabEl.classList.toggle("active", t.id === activeId);
    t.tabEl.classList.toggle("unread", t.unread && t.id !== activeId);
    const nameSpan = t.tabEl.querySelector(".name");
    const cwdSpan = t.tabEl.querySelector(".cwd");
    const sepSpan = t.tabEl.querySelector(".sep");
    nameSpan.textContent = t.name;
    if (t.cwdDisplay) {
      sepSpan.style.display = "";
      cwdSpan.style.display = "";
      cwdSpan.textContent = t.cwdDisplay;
      cwdSpan.title = t.cwdFull || "";
    } else {
      sepSpan.style.display = "none";
      cwdSpan.style.display = "none";
      cwdSpan.title = "";
    }
  }

  function updateNewTabButton() {
    newTabBtn.disabled = tabs.size >= MAX_TABS;
  }

  function buildTabDom(t) {
    const el = document.createElement("div");
    el.className = "tab";
    el.dataset.id = t.id;
    el.innerHTML = '<span class="name"></span><span class="sep"> — </span><span class="cwd"></span><span class="close" title="Close (Ctrl+Alt+W)">×</span>';
    el.addEventListener("click", (e) => {
      if (e.target && e.target.classList.contains("close")) {
        closeTab(t.id);
        return;
      }
      activate(t.id);
    });
    el.addEventListener("dblclick", (e) => {
      if (e.target && e.target.classList.contains("close")) return;
      const nv = prompt("Rename tab:", t.name);
      if (nv && nv.trim()) renameTab(t.id, nv.trim());
    });
    return el;
  }

  function buildTerminal(t) {
    const wrap = document.createElement("div");
    wrap.className = "term-wrap";
    wrap.dataset.id = t.id;
    termsEl.appendChild(wrap);

    const term = new Terminal({
      fontFamily: 'Menlo, Consolas, "DejaVu Sans Mono", monospace',
      fontSize: 13,
      cursorBlink: true,
      convertEol: false,
      theme: { background: "#0c0c0c" },
    });
    const fit = new FitAddon.FitAddon();
    term.loadAddon(fit);
    // Auto-detect URLs and make them tappable; force noopener for safety.
    if (typeof WebLinksAddon !== "undefined" && WebLinksAddon.WebLinksAddon) {
      term.loadAddon(new WebLinksAddon.WebLinksAddon((event, uri) => {
        try { window.open(uri, "_blank", "noopener,noreferrer"); } catch {}
      }));
    }
    term.open(wrap);

    term.onData((d) => {
      const out = transformStickyFirstChar(d);
      if (t.ws && t.ws.readyState === WebSocket.OPEN) {
        t.ws.send(JSON.stringify({ type: "input", data: out }));
      }
    });

    t.term = term;
    t.fit = fit;
    t.wrap = wrap;
  }

  const RECONNECT_MIN_MS = 1000;
  // spec §4.18: app-level heartbeat
  const HEARTBEAT_INTERVAL_MS = 25000;
  const HEARTBEAT_TIMEOUT_MS = 10000;
  // spec §4.9: exponential backoff for persistent retry while visible
  const RETRY_INITIAL_MS = 1000;
  const RETRY_MAX_MS = 30000;

  function stopHeartbeat(t) {
    if (t.pingTimer) { clearInterval(t.pingTimer); t.pingTimer = null; }
    if (t.pongTimer) { clearTimeout(t.pongTimer); t.pongTimer = null; }
  }
  function sendPing(t) {
    if (!t.ws || t.ws.readyState !== WebSocket.OPEN) return;
    try { t.ws.send(JSON.stringify({ type: "ping", ts: Date.now() })); } catch { return; }
    if (t.pongTimer) clearTimeout(t.pongTimer);
    t.pongTimer = setTimeout(() => {
      // No server activity within window → treat as dead, trigger reconnect flow
      try { t.ws && t.ws.close(4000, "heartbeat timeout"); } catch {}
    }, HEARTBEAT_TIMEOUT_MS);
  }
  function startHeartbeat(t) {
    stopHeartbeat(t);
    if (document.visibilityState !== "visible") return;
    t.pingTimer = setInterval(() => sendPing(t), HEARTBEAT_INTERVAL_MS);
  }
  function bumpLiveness(t) {
    // Any server message proves the connection is alive — cancel pending pong wait.
    if (t.pongTimer) { clearTimeout(t.pongTimer); t.pongTimer = null; }
  }

  function cancelRetry(t) {
    if (t.retryTimer) { clearTimeout(t.retryTimer); t.retryTimer = null; }
  }
  function scheduleRetry(t) {
    if (t.retryTimer) return;
    if (document.visibilityState !== "visible") return;  // pause while hidden
    const delay = t.retryDelay || RETRY_INITIAL_MS;
    t.retryDelay = Math.min(delay * 2, RETRY_MAX_MS);
    t.retryTimer = setTimeout(() => {
      t.retryTimer = null;
      connectTab(t);
    }, delay);
  }

  function connectTab(t) {
    const now = Date.now();
    if (t.ws && (t.ws.readyState === WebSocket.OPEN || t.ws.readyState === WebSocket.CONNECTING)) {
      return;
    }
    if (t.lastConnectAt && now - t.lastConnectAt < RECONNECT_MIN_MS) {
      return;  // throttle per-tab reconnects (spec §4.9)
    }
    t.lastConnectAt = now;
    cancelRetry(t);

    const proto = location.protocol === "https:" ? "wss" : "ws";
    const url = `${proto}://${location.host}${BASE}/ws/${t.id}`;
    const ws = new WebSocket(url);
    t.ws = ws;
    updateIndicator();

    ws.onopen = () => {
      t.retryDelay = RETRY_INITIAL_MS;  // reset backoff on success (spec §4.9)
      sendResize(t);
      startHeartbeat(t);
      updateIndicator();
    };
    ws.onclose = (ev) => {
      stopHeartbeat(t);
      // backend explicit rejects:
      //   4401 — session expired; try device-token exchange before falling back to /login
      //   4404 — tab no longer exists (5-min retention expired, or shell exited)
      if (ev.code === 4401) {
        (async () => {
          if (await tryExchangeDeviceToken()) {
            // cookie restored — let the next visible/focus event reconnect
            t.retryDelay = RETRY_INITIAL_MS;
            scheduleRetry(t);
          } else {
            window.location.href = BASE + "/login";
          }
        })();
        return;
      }
      if (ev.code === 4404) {
        // silently drop this tab locally
        cleanupLocalTab(t.id);
        updateIndicator();
        return;
      }
      updateIndicator();
      scheduleRetry(t);  // spec §4.9: persistent retry while page is visible
    };
    ws.onerror = () => updateIndicator();
    ws.onmessage = (ev) => {
      bumpLiveness(t);
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      if (msg.type === "pong") return;  // spec §4.18: heartbeat ack
      if (msg.type === "output" || msg.type === "replay") {
        t.term.write(msg.data);
        if (msg.type === "output" && t.id !== activeId) {
          t.unread = true;
          renderTab(t);
        }
      } else if (msg.type === "cwd") {
        t.cwdDisplay = msg.display || "";
        t.cwdFull = msg.full || "";
        renderTab(t);
      } else if (msg.type === "tabs") {
        // spec §4.11: tab list broadcast from the server — reconcile local state
        reconcileTabs(msg.tabs || []);
      } else if (msg.type === "exit") {
        // Shell exited (Ctrl-D / exit / kill) — auto-close the tab (spec §4.8)
        closeTab(t.id);
      }
    };
  }

  function cleanupLocalTab(id) {
    const t = tabs.get(id);
    if (!t) return;
    stopHeartbeat(t);
    cancelRetry(t);
    try { t.ws && t.ws.close(); } catch {}
    try { t.term.dispose(); } catch {}
    if (t.tabEl) t.tabEl.remove();
    if (t.wrap) t.wrap.remove();
    tabs.delete(id);
    updateNewTabButton();
    if (activeId === id) {
      const first = tabs.keys().next().value;
      if (first) activate(first);
      else createTab();
    }
  }

  function sendResize(t) {
    if (t.ws && t.ws.readyState === WebSocket.OPEN) {
      t.ws.send(JSON.stringify({ type: "resize", rows: t.term.rows, cols: t.term.cols }));
    }
  }

  function reconcileTabs(list) {
    const wantIds = new Set(list.map((m) => m.tab_id));
    // drop tabs that vanished remotely
    for (const id of Array.from(tabs.keys())) {
      if (!wantIds.has(id)) cleanupLocalTab(id);
    }
    // add tabs that appeared remotely
    for (const meta of list) {
      if (!tabs.has(meta.tab_id)) addTab(meta);
      else {
        const t = tabs.get(meta.tab_id);
        t.name = meta.name || t.name;
        t.cwdDisplay = meta.cwd_display || t.cwdDisplay;
        t.cwdFull = meta.cwd_full || t.cwdFull;
        renderTab(t);
      }
    }
    updateNewTabButton();
  }

  function addTab(meta) {
    const t = {
      id: meta.tab_id,
      name: meta.name || meta.tab_id,
      cwdDisplay: meta.cwd_display || "",
      cwdFull: meta.cwd_full || "",
      term: null, fit: null, ws: null, wrap: null, tabEl: null,
      unread: false, sentReplay: false,
    };
    t.tabEl = buildTabDom(t);
    tabsEl.appendChild(t.tabEl);
    buildTerminal(t);
    tabs.set(t.id, t);
    renderTab(t);
    updateNewTabButton();
    connectTab(t);
  }

  function activate(id) {
    activeId = id;
    for (const t of tabs.values()) {
      t.wrap.classList.toggle("active", t.id === id);
      if (t.id === id) {
        t.unread = false;
        // force a fit + resize upstream since hidden xterm doesn't self-fit
        setTimeout(() => {
          try { t.fit.fit(); sendResize(t); t.term.focus(); } catch {}
        }, 0);
      }
      renderTab(t);
    }
  }

  // If the bookmarked ?cwd= refers to a workspace/session that no longer exists
  // (dir removed, server restarted without the path, etc.), POST returns 400.
  // Recover by creating a fresh tab in the server's default workspace (HOME).
  async function createTab() {
    if (tabs.size >= MAX_TABS) return;
    try {
      const meta = await api("POST", "/api/tabs",
                             undefined, { noWorkspace: _fallback_active });
      if (!tabs.has(meta.tab_id)) addTab(meta);
      activate(meta.tab_id);
    } catch (e) {
      if (e && e.status === 400 && WORKSPACE_CWD && !_fallback_active) {
        // cwd is invalid — forget it, retry without it (spec §4.10 fallback)
        _fallback_active = true;
        setStatusNote("workspace directory is missing — opened a new session");
        try {
          const meta = await api("POST", "/api/tabs",
                                 undefined, { noWorkspace: true });
          if (!tabs.has(meta.tab_id)) addTab(meta);
          activate(meta.tab_id);
          return;
        } catch { /* fall through to indicator */ }
      }
      updateIndicator();
    }
  }

  function setStatusNote(text) {
    statusEl.title = text;  // surface via tooltip, keep dot color unchanged
  }

  async function closeTab(id) {
    if (!tabs.has(id)) return;
    cleanupLocalTab(id);
    try { await api("DELETE", "/api/tabs/" + encodeURIComponent(id)); } catch {}
    updateIndicator();
  }

  async function renameTab(id, name) {
    const t = tabs.get(id);
    if (!t) return;
    try {
      await api("PATCH", "/api/tabs/" + encodeURIComponent(id), { name });
      t.name = name;
      renderTab(t);
    } catch (e) {
      /* ignore rename errors */
    }
  }

  function switchRelative(delta) {
    if (tabs.size === 0) return;
    const ids = Array.from(tabs.keys());
    const idx = ids.indexOf(activeId);
    const n = ids.length;
    const next = ids[((idx + delta) % n + n) % n];
    activate(next);
  }

  function switchByIndex(i) {
    const ids = Array.from(tabs.keys());
    if (i < 0 || i >= ids.length) return;
    activate(ids[i]);
  }

  // Hotkeys (Ctrl+Alt+*)
  window.addEventListener("keydown", (e) => {
    if (!(e.ctrlKey && e.altKey) || e.metaKey || e.shiftKey) return;
    let handled = true;
    if (e.code === "ArrowLeft") switchRelative(-1);
    else if (e.code === "ArrowRight") switchRelative(1);
    else if (e.code === "KeyT") createTab();
    else if (e.code === "KeyW") { if (activeId) closeTab(activeId); }
    else if (/^Digit[1-9]$/.test(e.code)) switchByIndex(parseInt(e.code.slice(5), 10) - 1);
    else handled = false;
    if (handled) { e.preventDefault(); e.stopPropagation(); }
  }, true);

  newTabBtn.addEventListener("click", createTab);

  // Spec §4.19: device-token toggle lives inside the brand drop-down menu
  // (renderTabMenu appends it). Toolbar stays icon-only to keep mobile width.
  let _deviceBusy = false;
  async function toggleDeviceToken() {
    if (_deviceBusy) return;
    _deviceBusy = true;
    try {
      let has = null;
      try { has = localStorage.getItem(DEVICE_TOKEN_KEY); } catch {}
      if (has) {
        let id = "";
        try { id = localStorage.getItem(DEVICE_ID_KEY) || ""; } catch {}
        if (id) {
          try { await api("DELETE", "/api/auth/devices/" + encodeURIComponent(id), undefined, { noWorkspace: true }); } catch {}
        }
        try { localStorage.removeItem(DEVICE_TOKEN_KEY); localStorage.removeItem(DEVICE_ID_KEY); } catch {}
      } else {
        const label = navigator.userAgent ? navigator.userAgent.slice(0, 96) : "device";
        const r = await api("POST", "/api/auth/issue-device-token", { label }, { noWorkspace: true });
        try {
          localStorage.setItem(DEVICE_TOKEN_KEY, r.token);
          localStorage.setItem(DEVICE_ID_KEY, r.id);
        } catch {}
      }
    } finally {
      _deviceBusy = false;
    }
  }

  // Spec §4.7: "?" button opens the shortcut reference
  const helpBtn = document.getElementById("help-btn");
  const helpDlg = document.getElementById("help-dialog");
  if (helpBtn && helpDlg) {
    helpBtn.addEventListener("click", () => {
      if (typeof helpDlg.showModal === "function") helpDlg.showModal();
      else helpDlg.setAttribute("open", "");
    });
  }

  // --- bottom keypad: Ctrl/Alt sticky modifiers + Esc/Tab/arrows/PgUp/PgDn ---
  // stickyMods + clearStickyMods are declared near the top of the file.
  function modCode() {
    // xterm modifier: 1 + (shift)+ (alt*2) + (ctrl*4); we only support ctrl+alt
    return 1 + (stickyMods.alt ? 2 : 0) + (stickyMods.ctrl ? 4 : 0);
  }
  function charKey(ch) {
    // spec §4.17: plain character keys honor sticky Ctrl (via ctrlOf map) and
    // sticky Alt (prepend ESC). fire() clears sticky state after this returns.
    let out = ch;
    if (stickyMods.ctrl) {
      const c = ctrlOf(ch);
      if (c !== null) out = c;
    }
    if (stickyMods.alt) out = "\x1b" + out;
    return out;
  }
  function buildKey(name) {
    const m = modCode();
    const modded = m !== 1;
    const csi = (final) => modded ? `\x1b[1;${m}${final}` : `\x1b[${final}`;
    const tilde = (n) => modded ? `\x1b[${n};${m}~` : `\x1b[${n}~`;
    switch (name) {
      case "esc":  return stickyMods.alt ? "\x1b\x1b" : "\x1b";
      case "tab":
        if (stickyMods.ctrl && !stickyMods.alt) return "\t";  // typical terminals ignore ctrl+tab
        return stickyMods.alt ? "\x1b\t" : "\t";
      case "up":    return csi("A");
      case "down":  return csi("B");
      case "right": return csi("C");
      case "left":  return csi("D");
      case "pgup":  return tilde(5);
      case "pgdn":  return tilde(6);
      case "home":  return csi("H");
      case "end":   return csi("F");
      case "altbksp": return "\x1b\x7f";  // spec §4.15: backward-kill-word
      case "ctrlb":   return "\x02";      // spec §4.16: tmux prefix
      case "rbracket":  return charKey("]");   // spec §4.17
      case "backtick":  return charKey("`");
      case "backslash": return charKey("\\");
      default: return "";
    }
  }
  function sendToActive(data) {
    const t = activeId && tabs.get(activeId);
    if (!t || !t.ws || t.ws.readyState !== WebSocket.OPEN) return;
    t.ws.send(JSON.stringify({ type: "input", data }));
  }
  // Mod keys (Ctrl / Alt) stay click-only — toggle sticky state.
  document.querySelectorAll("#keypad .kp.mod").forEach((btn) => {
    const mod = btn.dataset.mod;
    btn.addEventListener("click", (e) => {
      e.preventDefault();
      stickyMods[mod] = !stickyMods[mod];
      btn.classList.toggle("on", stickyMods[mod]);
    });
    btn.addEventListener("mousedown", (e) => e.preventDefault());
  });

  // Spec §4.14: non-modifier keypad buttons support press-and-hold repeat.
  const REPEAT_DELAY_MS = 400;
  const REPEAT_INTERVAL_MS = 75;
  document.querySelectorAll("#keypad .kp[data-key]").forEach((btn) => {
    const key = btn.dataset.key;
    let timer1 = null, timer2 = null, lastPointerAt = 0;

    const fire = () => {
      const data = buildKey(key);
      if (data) sendToActive(data);
      clearStickyMods();
    };
    const stop = () => {
      if (timer1) { clearTimeout(timer1); timer1 = null; }
      if (timer2) { clearInterval(timer2); timer2 = null; }
    };

    btn.addEventListener("pointerdown", (e) => {
      e.preventDefault();
      lastPointerAt = Date.now();
      try { btn.setPointerCapture(e.pointerId); } catch {}
      fire();
      timer1 = setTimeout(() => {
        timer2 = setInterval(fire, REPEAT_INTERVAL_MS);
      }, REPEAT_DELAY_MS);
    });
    btn.addEventListener("pointerup", stop);
    btn.addEventListener("pointercancel", stop);
    btn.addEventListener("pointerleave", stop);
    btn.addEventListener("lostpointercapture", stop);

    // Keyboard-accessibility fallback: Space/Enter synthesize a click but no
    // pointerdown. Fire once, but suppress if a pointer just fired.
    btn.addEventListener("click", (e) => {
      if (Date.now() - lastPointerAt < 500) return;
      e.preventDefault();
      fire();
      const t = activeId && tabs.get(activeId);
      if (t && t.term) t.term.focus();
    });
    btn.addEventListener("mousedown", (e) => e.preventDefault());
  });

  // Spec §4.13: brand button → drop-down menu of all tabs in this workspace.
  const brandBtn = document.getElementById("brand-btn");
  const tabMenu = document.getElementById("tab-menu");
  function renderTabMenu() {
    if (!tabMenu || tabMenu.hidden) return;
    tabMenu.innerHTML = "";
    for (const [id, t] of tabs.entries()) {
      const item = document.createElement("div");
      item.className = "tab-menu-item";
      if (id === activeId) item.classList.add("active");
      if (t.unread && id !== activeId) item.classList.add("unread");
      item.setAttribute("role", "menuitem");
      item.title = t.cwdFull || t.cwdDisplay || "";
      const nameEl = document.createElement("span");
      nameEl.className = "mi-name";
      nameEl.textContent = t.name;
      item.appendChild(nameEl);
      if (t.cwdDisplay) {
        const sep = document.createElement("span");
        sep.className = "mi-sep";
        sep.textContent = " — ";
        item.appendChild(sep);
        const cwdEl = document.createElement("span");
        cwdEl.className = "mi-cwd";
        cwdEl.textContent = t.cwdDisplay;
        item.appendChild(cwdEl);
      }
      item.addEventListener("click", () => {
        activate(id);
        closeTabMenu();
      });
      tabMenu.appendChild(item);
    }
    // Footer: device-token toggle (spec §4.19).
    const sep = document.createElement("div");
    sep.className = "tab-menu-sep";
    tabMenu.appendChild(sep);
    let hasTok = false;
    try { hasTok = !!localStorage.getItem(DEVICE_TOKEN_KEY); } catch {}
    const dev = document.createElement("div");
    dev.className = "tab-menu-item tab-menu-action";
    dev.setAttribute("role", "menuitem");
    dev.title = hasTok
      ? "Forget this device (revoke its token, future visits will require login)"
      : "Remember this device (skip future logins on this browser)";
    dev.textContent = hasTok ? "忘记此设备" : "记住此设备";
    dev.addEventListener("click", async () => {
      await toggleDeviceToken();
      renderTabMenu();
    });
    tabMenu.appendChild(dev);
  }
  function openTabMenu() {
    if (!tabMenu) return;
    tabMenu.hidden = false;
    brandBtn.setAttribute("aria-expanded", "true");
    renderTabMenu();
  }
  function closeTabMenu() {
    if (!tabMenu) return;
    tabMenu.hidden = true;
    brandBtn.setAttribute("aria-expanded", "false");
  }
  function toggleTabMenu() {
    if (!tabMenu) return;
    tabMenu.hidden ? openTabMenu() : closeTabMenu();
  }
  if (brandBtn) {
    brandBtn.addEventListener("click", (e) => { e.stopPropagation(); toggleTabMenu(); });
  }
  // click outside / Esc → close
  document.addEventListener("click", (e) => {
    if (!tabMenu || tabMenu.hidden) return;
    if (tabMenu.contains(e.target) || brandBtn.contains(e.target)) return;
    closeTabMenu();
  });
  document.addEventListener("keydown", (e) => {
    if (!tabMenu || tabMenu.hidden) return;
    if (e.key === "Escape") { e.preventDefault(); closeTabMenu(); }
  });

  function refitActive() {
    for (const t of tabs.values()) {
      if (t.id === activeId) { try { t.fit.fit(); sendResize(t); } catch {} }
    }
  }
  window.addEventListener("resize", refitActive);

  // Float the keypad above the on-screen keyboard on mobile (visualViewport API)
  const vv = window.visualViewport;
  if (vv) {
    const root = document.documentElement;
    let rafId = 0;
    const apply = () => {
      rafId = 0;
      // Distance from visualViewport bottom to layout viewport bottom.
      const gap = Math.max(0, window.innerHeight - (vv.height + vv.offsetTop));
      root.style.setProperty("--kb", gap + "px");
      refitActive();
    };
    const schedule = () => {
      if (rafId) return;
      rafId = requestAnimationFrame(apply);
    };
    vv.addEventListener("resize", schedule);
    vv.addEventListener("scroll", schedule);
    apply();
  }

  async function bootstrap() {
    try {
      const resp = await api("GET", "/api/tabs");
      const existing = resp.tabs || [];
      if (existing.length === 0) {
        await createTab();
      } else {
        for (const meta of existing) addTab(meta);
        activate(existing[0].tab_id);
      }
    } catch (e) {
      updateIndicator();
    }
  }

  function reconnectStaleTabs() {
    for (const t of tabs.values()) {
      if (!t.ws || t.ws.readyState === WebSocket.CLOSED || t.ws.readyState === WebSocket.CLOSING) {
        t.retryDelay = RETRY_INITIAL_MS;  // spec §4.9: focus/visible/online restart from shortest delay
        cancelRetry(t);
        connectTab(t);
      }
    }
    updateIndicator();
  }

  // Spec §4.9: attempt reconnect when the page regains focus / becomes visible / network returns
  window.addEventListener("focus", reconnectStaleTabs);
  window.addEventListener("online", reconnectStaleTabs);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") {
      // Resume heartbeats on already-open tabs and probe once immediately (spec §4.18).
      for (const t of tabs.values()) {
        if (t.ws && t.ws.readyState === WebSocket.OPEN) {
          startHeartbeat(t);
          sendPing(t);
        }
      }
      reconnectStaleTabs();
    } else {
      // Hidden: pause heartbeat + retry timers to save power; they resume on 'visible'.
      for (const t of tabs.values()) {
        stopHeartbeat(t);
        cancelRetry(t);
      }
    }
  });

  bootstrap();
})();
