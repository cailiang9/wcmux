(function () {
  const BASE = window.WCMUX_BASE_URL || "";
  const MAX_TABS = 20;
  const statusEl = document.getElementById("status");
  const tabsEl = document.getElementById("tabs");
  const termsEl = document.getElementById("terminals");
  const newTabBtn = document.getElementById("new-tab");

  /** @type {Map<string, {id:string, name:string, cwdDisplay:string, cwdFull:string,
   *                      term:any, fit:any, ws:WebSocket|null, wrap:HTMLElement, tabEl:HTMLElement,
   *                      unread:boolean, sentReplay:boolean}>} */
  const tabs = new Map();
  let activeId = null;

  function setStatus(text, cls) {
    statusEl.textContent = text;
    statusEl.className = "status" + (cls ? " " + cls : "");
  }

  function api(method, path, body) {
    const opts = { method, headers: {}, credentials: "same-origin" };
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    return fetch(BASE + path, opts).then(async (r) => {
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
      if (t.ws && t.ws.readyState === WebSocket.OPEN) {
        t.ws.send(JSON.stringify({ type: "input", data: d }));
      }
    });

    t.term = term;
    t.fit = fit;
    t.wrap = wrap;
  }

  function connectTab(t) {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const url = `${proto}://${location.host}${BASE}/ws/${t.id}`;
    const ws = new WebSocket(url);
    t.ws = ws;
    ws.onopen = () => {
      sendResize(t);
      setStatus("connected", "ok");
    };
    ws.onclose = () => {
      if (tabs.has(t.id)) setStatus("disconnected", "err");
    };
    ws.onerror = () => setStatus("error", "err");
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
      } else if (msg.type === "exit") {
        // tab exited; visual cue only — user can close it.
      }
    };
  }

  function sendResize(t) {
    if (t.ws && t.ws.readyState === WebSocket.OPEN) {
      t.ws.send(JSON.stringify({ type: "resize", rows: t.term.rows, cols: t.term.cols }));
    }
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
      const meta = await api("POST", "/api/tabs");
      addTab(meta);
      activate(meta.tab_id);
    } catch (e) {
      setStatus("create failed", "err");
    }
  }

  async function closeTab(id) {
    const t = tabs.get(id);
    if (!t) return;
    try { t.ws && t.ws.close(); } catch {}
    try { t.term.dispose(); } catch {}
    if (t.tabEl) t.tabEl.remove();
    if (t.wrap) t.wrap.remove();
    tabs.delete(id);
    try { await api("DELETE", "/api/tabs/" + encodeURIComponent(id)); } catch {}
    updateNewTabButton();

    if (activeId === id) {
      const first = tabs.keys().next().value;
      if (first) activate(first);
      else createTab();  // ensure at least one tab
    }
  }

  async function renameTab(id, name) {
    const t = tabs.get(id);
    if (!t) return;
    try {
      await api("PATCH", "/api/tabs/" + encodeURIComponent(id), { name });
      t.name = name;
      renderTab(t);
    } catch (e) {
      setStatus("rename failed", "err");
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

  // --- bottom keypad: Ctrl/Alt sticky modifiers + Esc/Tab/arrows/PgUp/PgDn ---
  const stickyMods = { ctrl: false, alt: false };
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
      default: return "";
    }
  }
  function sendToActive(data) {
    const t = activeId && tabs.get(activeId);
    if (!t || !t.ws || t.ws.readyState !== WebSocket.OPEN) return;
    t.ws.send(JSON.stringify({ type: "input", data }));
  }
  function clearMods() {
    stickyMods.ctrl = false; stickyMods.alt = false;
    document.querySelectorAll(".kp.mod").forEach((b) => b.classList.remove("on"));
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
        clearMods();
        const t = activeId && tabs.get(activeId);
        if (t && t.term) t.term.focus();
      }
    });
    // avoid the button stealing focus from xterm on touch
    btn.addEventListener("mousedown", (e) => e.preventDefault());
  });

  window.addEventListener("resize", () => {
    for (const t of tabs.values()) {
      if (t.id === activeId) { try { t.fit.fit(); sendResize(t); } catch {} }
    }
  });

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
      setStatus("load failed", "err");
    }
  }

  bootstrap();
})();
