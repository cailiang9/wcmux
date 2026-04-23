(function () {
  const BASE = window.WCMUX_BASE_URL || "";
  const MAX_TABS = 20;

  // Spec §4.10: URL ?cwd=<path> identifies the workspace AND sets the starting
  // cwd for new tabs. No separate workspace id.
  const _urlParams = new URLSearchParams(location.search);
  const WORKSPACE_CWD = _urlParams.get("cwd") || "";  // "" → server HOME default
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
    if (!WORKSPACE_CWD) return path;  // let server use default HOME
    const sep = path.includes("?") ? "&" : "?";
    return path + sep + "cwd=" + encodeURIComponent(WORKSPACE_CWD);
  }

  function api(method, path, body) {
    const opts = { method, headers: {}, credentials: "same-origin" };
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    return fetch(BASE + withWorkspace(path), opts).then(async (r) => {
      if (!r.ok) throw new Error(`${method} ${path}: ${r.status}`);
      return r.json();
    });
  }

  function renderTab(t) {
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

  function connectTab(t) {
    const now = Date.now();
    if (t.ws && (t.ws.readyState === WebSocket.OPEN || t.ws.readyState === WebSocket.CONNECTING)) {
      return;
    }
    if (t.lastConnectAt && now - t.lastConnectAt < RECONNECT_MIN_MS) {
      return;  // throttle per-tab reconnects (spec §4.9)
    }
    t.lastConnectAt = now;

    const proto = location.protocol === "https:" ? "wss" : "ws";
    const url = `${proto}://${location.host}${BASE}/ws/${t.id}`;
    const ws = new WebSocket(url);
    t.ws = ws;
    updateIndicator();

    ws.onopen = () => {
      sendResize(t);
      updateIndicator();
    };
    ws.onclose = (ev) => {
      // backend explicit rejects:
      //   4401 — session expired; redirect to login
      //   4404 — tab no longer exists (5-min retention expired, or shell exited)
      if (ev.code === 4401) {
        window.location.href = BASE + "/login";
        return;
      }
      if (ev.code === 4404) {
        // silently drop this tab locally
        cleanupLocalTab(t.id);
        updateIndicator();
        return;
      }
      updateIndicator();
    };
    ws.onerror = () => updateIndicator();
    ws.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
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

  async function createTab() {
    if (tabs.size >= MAX_TABS) return;
    try {
      // Starting cwd is implied by the workspace id (= ?cwd=), no body needed.
      const meta = await api("POST", "/api/tabs");
      if (!tabs.has(meta.tab_id)) addTab(meta);
      activate(meta.tab_id);
    } catch (e) {
      updateIndicator();
    }
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
      default: return "";
    }
  }
  function sendToActive(data) {
    const t = activeId && tabs.get(activeId);
    if (!t || !t.ws || t.ws.readyState !== WebSocket.OPEN) return;
    t.ws.send(JSON.stringify({ type: "input", data }));
  }
  document.querySelectorAll("#keypad .kp").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.preventDefault();
      const mod = btn.dataset.mod;
      const key = btn.dataset.key;
      if (mod) {
        stickyMods[mod] = !stickyMods[mod];
        btn.classList.toggle("on", stickyMods[mod]);
        return;
      }
      if (key) {
        const data = buildKey(key);
        if (data) sendToActive(data);
        clearStickyMods();
        const t = activeId && tabs.get(activeId);
        if (t && t.term) t.term.focus();
      }
    });
    // avoid the button stealing focus from xterm on touch
    btn.addEventListener("mousedown", (e) => e.preventDefault());
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
        connectTab(t);
      }
    }
    updateIndicator();
  }

  // Spec §4.9: attempt reconnect when the page regains focus / becomes visible / network returns
  window.addEventListener("focus", reconnectStaleTabs);
  window.addEventListener("online", reconnectStaleTabs);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") reconnectStaleTabs();
  });

  bootstrap();
})();
