document.addEventListener("DOMContentLoaded", () => {
  const tabs  = document.querySelectorAll(".tab-btn");
  const panes = document.querySelectorAll(".pane");

  function switchTab(id) {
    tabs.forEach(t  => t.classList.toggle("active", t.dataset.tab === id));
    panes.forEach(p => p.classList.toggle("active", p.id === `pane-${id}`));
    history.replaceState(null, "", `#${id}`);
    if (id === "home")   loadChannels();
    if (id === "status") loadStatus();
    if (id === "readme") loadReadme();
    if (id === "logs")   loadLogs();
  }

  tabs.forEach(t => t.addEventListener("click", () => switchTab(t.dataset.tab)));
  const initial = location.hash.replace("#", "") || "home";
  switchTab(initial);

  // ── HOME: channels ──────────────────────────────────────
  async function loadChannels() {
    const el = document.getElementById("channel-list");
    if (!el || el.dataset.loaded) return;
    try {
      const { channels } = await api("/api/channels");
      if (!channels.length) { el.innerHTML = placeholder("📭", "チャンネルなし"); return; }
      el.innerHTML = channels.map(ch => `
        <div class="channel-item">
          <span class="channel-lang">${esc(ch.lang)}</span>
          <span class="channel-name">${esc(ch.name)}</span>
          <a class="channel-link" href="${esc(ch.url)}" target="_blank" rel="noopener">↗ YouTube</a>
        </div>`).join("");
      el.dataset.loaded = "1";
    } catch { el.innerHTML = placeholder("⚠️", "読み込み失敗"); }
  }

  // ── STATUS ───────────────────────────────────────────────
  async function loadStatus() {
    const headerEl = document.getElementById("status-header-card");
    const videosEl = document.getElementById("status-videos");
    const statsEl  = document.getElementById("status-stats");
    const logEl    = document.getElementById("status-log");
    if (!headerEl) return;

    try {
      const d = await api("/api/status-summary");

      // ヘッダーカード
      const statusCls = d.status === "稼働中" ? "badge-green"
        : d.status === "rate-limit 中" ? "badge-warn"
        : d.status === "停止" ? "badge-gray" : "badge-gray";
      headerEl.innerHTML = `
        <div class="status-header-inner">
          <div class="status-header-left">
            <div class="status-script">autonomous.sh</div>
            <div class="status-session">${esc(d.last_session || "セッション情報なし")}</div>
          </div>
          <div class="status-header-right">
            <span class="badge ${statusCls}">${esc(d.status)}</span>
            <span class="status-phase">${esc(d.phase)}</span>
          </div>
        </div>`;

      // 最近の動画カード
      if (!d.recent_videos || !d.recent_videos.length) {
        videosEl.innerHTML = placeholder("🎞️", "処理済み動画なし");
      } else {
        videosEl.innerHTML = d.recent_videos.map(v => `
          <div class="video-card">
            <span class="badge badge-green" style="flex-shrink:0">done</span>
            <div class="video-info">
              <div class="video-title">${esc(v.title)}</div>
              <div class="video-channel">${esc(v.channel)}</div>
            </div>
          </div>`).join("");
      }

      // 統計パネル
      statsEl.innerHTML = `
        <div class="stat-grid">
          <div class="stat-item"><span class="stat-label">queue</span><span class="stat-val">${d.queue_count} 件</span></div>
          <div class="stat-item"><span class="stat-label">完了</span><span class="stat-val stat-green">${d.done_count} 件</span></div>
          <div class="stat-item"><span class="stat-label">警告</span><span class="stat-val stat-warn">${d.warn_count} 件</span></div>
          <div class="stat-item"><span class="stat-label">エラー</span><span class="stat-val stat-err">${d.error_count} 件</span></div>
          <div class="stat-item"><span class="stat-label">rate-limit</span><span class="stat-val">${d.rate_limit_count} 回</span></div>
          <div class="stat-item"><span class="stat-label">フェーズ</span><span class="stat-val">${esc(d.phase)}</span></div>
        </div>
        <div style="margin-top:12px;font-size:11px;color:var(--text-faint)">参照: ${esc(d.log_file || "—")}</div>`;

      // ログ末尾
      renderLog(logEl, d.lines || []);

    } catch (e) {
      headerEl.innerHTML = placeholder("⚠️", "読み込み失敗");
    }
  }

  window.reloadStatus = function() {
    const els = ["status-header-card","status-videos","status-stats","status-log"];
    els.forEach(id => {
      const el = document.getElementById(id);
      if (el) el.innerHTML = placeholder("⏳", "更新中…");
    });
    loadStatus();
  };

  // ── README ───────────────────────────────────────────────
  async function loadReadme() {
    const el = document.getElementById("readme-body");
    if (!el || el.dataset.loaded) return;
    el.textContent = "読み込み中…";
    try {
      const { content } = await api("/api/readme");
      if (typeof marked !== "undefined") {
        el.innerHTML = marked.parse(content);
      } else {
        el.textContent = content;
      }
      el.dataset.loaded = "1";
    } catch { el.textContent = "読み込み失敗"; }
  }

  // ── LOGS ────────────────────────────────────────────────
  async function loadLogs() {
    const el = document.getElementById("log-list");
    if (!el || el.dataset.loaded) return;
    el.innerHTML = placeholder("⏳", "読み込み中…");
    try {
      const { logs } = await api("/api/logs");
      if (!logs.length) { el.innerHTML = placeholder("📭", "ログファイルなし"); return; }
      el.innerHTML = logs.map(l => `
        <div class="channel-item log-file-item" data-path="${esc(l.path)}" onclick="openLog(this)">
          <span class="badge badge-gray">LOG</span>
          <span class="channel-name">${esc(l.path)}</span>
          <span style="color:var(--text-faint);font-size:11px;flex-shrink:0">${(l.size/1024).toFixed(1)} KB</span>
        </div>`).join("");
      el.dataset.loaded = "1";
    } catch { el.innerHTML = placeholder("⚠️", "読み込み失敗"); }
  }

  window.openLog = async function(el) {
    document.querySelectorAll(".log-file-item").forEach(e => e.classList.remove("active-log"));
    el.classList.add("active-log");
    const path = el.dataset.path;
    const card    = document.getElementById("log-viewer-card");
    const titleEl = document.getElementById("log-viewer-title");
    const content = document.getElementById("log-viewer-content");
    card.style.display = "block";
    titleEl.textContent = path.split("/").pop();
    content.textContent = "読み込み中…";
    try {
      const d = await api(`/api/log-content?path=${encodeURIComponent(path)}`);
      renderLog(content, d.content.split("\n"));
    } catch { content.textContent = "読み込み失敗"; }
  };

  window.closeLogViewer = function() {
    document.getElementById("log-viewer-card").style.display = "none";
    document.querySelectorAll(".log-file-item").forEach(e => e.classList.remove("active-log"));
  };

  // ── utils ────────────────────────────────────────────────
  async function api(url) {
    const r = await fetch(url);
    if (!r.ok) throw new Error(r.status);
    return r.json();
  }

  function renderLog(el, lines) {
    el.innerHTML = lines.map(l => {
      const cls = l.includes("[error]") || l.includes("ERROR") ? "log-error"
        : l.includes("[warn]")  || l.includes("WARN")  ? "log-warn"
        : l.includes("[done]")  || l.includes("Done")  ? "log-done"
        : l.includes("[info]")                          ? "log-info" : "";
      return `<span class="${cls}">${esc(l)}</span>`;
    }).join("\n");
    el.scrollTop = el.scrollHeight;
  }

  function placeholder(icon, text) {
    return `<div class="placeholder"><div class="placeholder-icon">${icon}</div><span>${text}</span></div>`;
  }

  function esc(s) {
    return String(s ?? "").replace(/[&<>"']/g, c =>
      ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  }
});
