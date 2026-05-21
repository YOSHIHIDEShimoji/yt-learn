document.addEventListener("DOMContentLoaded", () => {
  const tabs = document.querySelectorAll(".tab-btn");
  const panes = document.querySelectorAll(".pane");

  function switchTab(id) {
    tabs.forEach(t => t.classList.toggle("active", t.dataset.tab === id));
    panes.forEach(p => p.classList.toggle("active", p.id === `pane-${id}`));
    history.replaceState(null, "", `#${id}`);
    if (id === "home") loadChannels();
    if (id === "status") loadStatus();
    if (id === "readme") loadReadme();
    if (id === "logs") loadLogs();
  }

  tabs.forEach(t => t.addEventListener("click", () => switchTab(t.dataset.tab)));

  // Initial tab from hash or default
  const initial = location.hash.replace("#", "") || "home";
  switchTab(initial);

  // ── HOME: channels ──
  async function loadChannels() {
    const el = document.getElementById("channel-list");
    if (!el || el.dataset.loaded) return;
    el.innerHTML = '<div class="placeholder"><div class="placeholder-icon">⏳</div><span>読み込み中…</span></div>';
    try {
      const { channels } = await fetch("/api/channels").then(r => r.json());
      if (!channels.length) {
        el.innerHTML = '<div class="placeholder"><div class="placeholder-icon">📭</div><span>チャンネルなし</span></div>';
        return;
      }
      el.innerHTML = channels.map(ch => `
        <div class="channel-item">
          <span class="channel-lang">${escHtml(ch.lang)}</span>
          <span class="channel-name">${escHtml(ch.name)}</span>
          <a class="channel-link" href="${escHtml(ch.url)}" target="_blank" rel="noopener">↗ YouTube</a>
        </div>
      `).join("");
      el.dataset.loaded = "1";
    } catch {
      el.innerHTML = '<div class="placeholder"><div class="placeholder-icon">⚠️</div><span>読み込み失敗</span></div>';
    }
  }

  // ── STATUS: recent log ──
  async function loadStatus() {
    const el = document.getElementById("status-log");
    if (!el) return;
    try {
      const { lines } = await fetch("/api/status").then(r => r.json());
      el.innerHTML = lines.map(l => {
        const cls = l.includes("[error]") || l.includes("ERROR") ? "log-error"
          : l.includes("[warn]") || l.includes("WARN") ? "log-warn"
          : l.includes("[done]") || l.includes("Done") ? "log-done"
          : l.includes("[info]") ? "log-info" : "";
        return `<span class="${cls}">${escHtml(l)}</span>`;
      }).join("\n");
      el.scrollTop = el.scrollHeight;
    } catch {
      el.textContent = "ログ取得失敗";
    }
  }

  // ── README ──
  async function loadReadme() {
    const el = document.getElementById("readme-body");
    if (!el || el.dataset.loaded) return;
    el.textContent = "読み込み中…";
    try {
      const { content } = await fetch("/api/readme").then(r => r.json());
      el.textContent = content;
      el.dataset.loaded = "1";
    } catch {
      el.textContent = "読み込み失敗";
    }
  }

  // ── LOGS ──
  async function loadLogs() {
    const el = document.getElementById("log-list");
    if (!el || el.dataset.loaded) return;
    el.innerHTML = '<div class="placeholder"><div class="placeholder-icon">⏳</div><span>読み込み中…</span></div>';
    try {
      const { logs } = await fetch("/api/logs").then(r => r.json());
      if (!logs.length) {
        el.innerHTML = '<div class="placeholder"><div class="placeholder-icon">📭</div><span>ログファイルなし</span></div>';
        return;
      }
      el.innerHTML = logs.map(l => `
        <div class="channel-item">
          <span class="badge badge-gray">LOG</span>
          <span class="channel-name">${escHtml(l.path)}</span>
          <span style="color:var(--text-faint);font-size:11px">${(l.size / 1024).toFixed(1)} KB</span>
        </div>
      `).join("");
      el.dataset.loaded = "1";
    } catch {
      el.innerHTML = '<div class="placeholder"><div class="placeholder-icon">⚠️</div><span>読み込み失敗</span></div>';
    }
  }

  function escHtml(s) {
    return String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  }
});
