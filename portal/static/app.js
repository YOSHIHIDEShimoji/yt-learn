document.addEventListener("DOMContentLoaded", () => {
  const tabs  = document.querySelectorAll(".tab-btn");
  const panes = document.querySelectorAll(".pane");

  // ── 状態変数（switchTab 呼び出し前に初期化、TDZ 回避）─────
  let _channels = [];
  let _statusData = null;
  let _statusPollTimer = null;

  function switchTab(id) {
    tabs.forEach(t  => t.classList.toggle("active", t.dataset.tab === id));
    panes.forEach(p => p.classList.toggle("active", p.id === `pane-${id}`));
    history.replaceState(null, "", `#${id}`);
    if (id === "home")   loadChannels();
    if (id === "status") { loadStatus(); startStatusPolling(); }
    else                 stopStatusPolling();
    if (id === "readme") loadReadme();
    if (id === "logs")   loadLogs();
  }

  tabs.forEach(t => t.addEventListener("click", () => switchTab(t.dataset.tab)));
  window.goHome = () => switchTab("home");
  const initial = location.hash.replace("#", "") || "home";
  switchTab(initial);

  // ── HOME: channels ──────────────────────────────────────
  async function loadChannels() {
    const el = document.getElementById("channel-list");
    if (!el || el.dataset.loaded) return;
    try {
      const { channels } = await api("/api/channels");
      _channels = channels;
      if (!channels.length) { el.innerHTML = placeholder("📭", "チャンネルなし"); return; }
      el.innerHTML = channels.map((ch, i) => `
        <div class="channel-item" data-idx="${i}">
          <span class="channel-lang">${esc(ch.lang)}</span>
          <span class="channel-name">${esc(ch.name)}</span>
          <a class="channel-link" href="${esc(ch.url)}" target="_blank" rel="noopener">↗ YouTube</a>
        </div>`).join("");
      el.dataset.loaded = "1";
      fetchChannelDriveLinks();
    } catch { el.innerHTML = placeholder("⚠️", "読み込み失敗"); }
  }

  window.reloadChannels = function() {
    const el = document.getElementById("channel-list");
    if (el) { delete el.dataset.loaded; el.innerHTML = placeholder("⏳", "読み込み中…"); }
    loadChannels();
  };

  async function fetchChannelDriveLinks() {
    try {
      const { drive_urls } = await api("/api/channel-drive-urls", 20000);
      _channels.forEach((ch, i) => {
        const url = drive_urls[ch.name];
        if (!url) return;
        const item = document.querySelector(`#channel-list [data-idx="${i}"]`);
        if (item && !item.querySelector("[data-drive]")) {
          const a = document.createElement("a");
          a.className = "channel-link drive-link-popin";
          a.href = url;
          a.target = "_blank";
          a.rel = "noopener";
          a.dataset.drive = "1";
          a.textContent = "↗ Drive";
          item.appendChild(a);
        }
      });
    } catch (e) {}
  }

  // ── STATUS ───────────────────────────────────────────────
  function startStatusPolling() {
    if (_statusPollTimer) return;
    _statusPollTimer = setInterval(pollStatusUpdates, 15000);
  }

  function stopStatusPolling() {
    if (_statusPollTimer) { clearInterval(_statusPollTimer); _statusPollTimer = null; }
  }

  async function pollStatusUpdates() {
    if (!_statusData) return;
    try {
      const d = await api("/api/status-summary");
      const videoCountChanged = d.done_videos.length !== _statusData.done_videos.length;
      const runningChanged = (d.running_video?.title ?? null) !== (_statusData.running_video?.title ?? null);
      const metaChanged = d.status !== _statusData.status
        || d.phase !== _statusData.phase
        || d.done_count !== _statusData.done_count
        || d.drive_folder_url !== _statusData.drive_folder_url;

      if (videoCountChanged || runningChanged || metaChanged) {
        renderStatusData(d);
      } else {
        // Drive リンクだけ外科的に追加
        d.done_videos.forEach((v, i) => {
          if (v.drive_url && !(_statusData.done_videos[i]?.drive_url)) {
            const card = document.querySelector(`#status-videos .video-card[data-idx="${i}"]`);
            if (card && !card.querySelector(".channel-link")) {
              const a = document.createElement("a");
              a.className = "channel-link drive-link-popin";
              a.href = v.drive_url;
              a.target = "_blank";
              a.rel = "noopener";
              a.style.flexShrink = "0";
              a.textContent = "↗ Drive";
              card.appendChild(a);
            }
          }
        });
      }
      _statusData = d;
    } catch (e) {}
  }

  function renderStatusData(d) {
    const headerEl = document.getElementById("status-header-card");
    const videosEl = document.getElementById("status-videos");
    const statsEl  = document.getElementById("status-stats");
    if (!headerEl) return;

    const statusCls = d.status === "稼働中" ? "badge-green"
      : d.status === "rate-limit 中" ? "badge-warn"
      : "badge-gray";
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

    const cards = [];
    if (d.running_video) {
      cards.push(`
        <div class="video-card">
          <span class="badge badge-blue" style="flex-shrink:0">running</span>
          <div class="video-info">
            <div class="video-title">${esc(d.running_video.title)}</div>
            <div class="video-channel">${esc(d.running_video.channel || "—")}</div>
          </div>
        </div>`);
    }
    if (d.done_videos && d.done_videos.length) {
      d.done_videos.forEach((v, i) => {
        cards.push(`
          <div class="video-card" data-idx="${i}">
            <span class="badge badge-green" style="flex-shrink:0">done</span>
            <div class="video-info">
              <div class="video-title">${esc(v.title)}</div>
              <div class="video-channel">${esc(v.channel)}</div>
            </div>
            ${v.drive_url ? `<a class="channel-link" href="${esc(v.drive_url)}" target="_blank" rel="noopener" style="flex-shrink:0">↗ Drive</a>` : ""}
          </div>`);
      });
    } else if (!d.running_video) {
      cards.push(placeholder("🎞️", "処理済み動画なし"));
    }
    videosEl.innerHTML = cards.join("");

    statsEl.innerHTML = `
      <div class="stat-grid">
        <div class="stat-item"><span class="stat-label">queue</span><span class="stat-val">${d.queue_count} 件</span></div>
        <div class="stat-item"><span class="stat-label">完了</span><span class="stat-val stat-green">${d.done_count} 件</span></div>
        <div class="stat-item"><span class="stat-label">警告</span><span class="stat-val stat-warn">${d.warn_count} 件</span></div>
        <div class="stat-item"><span class="stat-label">エラー</span><span class="stat-val stat-err">${d.error_count} 件</span></div>
        <div class="stat-item"><span class="stat-label">rate-limit</span><span class="stat-val">${d.rate_limit_count} 回</span></div>
        <div class="stat-item"><span class="stat-label">フェーズ</span><span class="stat-val">${esc(d.phase)}</span></div>
      </div>
      <div style="margin-top:12px;display:flex;justify-content:space-between;align-items:center;font-size:11px;color:var(--text-faint)">
        <span>参照: ${esc(d.log_file || "—")}</span>
        ${d.drive_folder_url ? `<a class="channel-link" href="${esc(d.drive_folder_url)}" target="_blank" rel="noopener" style="opacity:1;font-size:12px">↗ Google Drive</a>` : ""}
      </div>`;
  }

  async function loadStatus() {
    const headerEl = document.getElementById("status-header-card");
    if (!headerEl) return;
    try {
      const d = await api("/api/status-summary");
      renderStatusData(d);
      _statusData = d;
    } catch (e) {
      headerEl.innerHTML = placeholder("⚠️", "読み込み失敗");
    }
  }

  window.reloadStatus = function() {
    ["status-header-card", "status-videos", "status-stats"].forEach(id => {
      const el = document.getElementById(id);
      if (el) el.innerHTML = placeholder("⏳", "更新中…");
    });
    loadStatus();
  };

  // ── README ───────────────────────────────────────────────
  async function loadReadme() {
    const el = document.getElementById("readme-body");
    if (!el) return;
    if (el.dataset.loading) return;
    el.dataset.loading = "1";
    el.textContent = "読み込み中…";
    try {
      const { content } = await api("/api/readme");
      el.innerHTML = marked.parse(content);
    } catch {
      el.textContent = "読み込み失敗";
    } finally {
      delete el.dataset.loading;
    }
  }

  window.reloadReadme = loadReadme;

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
          <span class="badge ${l.is_done ? 'badge-gray' : 'badge-blue'}">${l.is_done ? 'done' : 'live'}</span>
          <span class="channel-name">${esc(l.path)}</span>
          <span style="color:var(--text-faint);font-size:11px;flex-shrink:0">${(l.size/1024).toFixed(1)} KB</span>
        </div>`).join("");
      el.dataset.loaded = "1";
    } catch { el.innerHTML = placeholder("⚠️", "読み込み失敗"); }
  }

  window.reloadLogs = function() {
    const el = document.getElementById("log-list");
    if (el) { delete el.dataset.loaded; el.innerHTML = placeholder("⏳", "読み込み中…"); }
    loadLogs();
  };

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
  async function api(url, timeoutMs = 10000) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeoutMs);
    try {
      const r = await fetch(url, { signal: ctrl.signal });
      if (!r.ok) throw new Error(r.status);
      return r.json();
    } finally {
      clearTimeout(timer);
    }
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
