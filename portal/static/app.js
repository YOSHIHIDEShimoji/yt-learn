document.addEventListener("DOMContentLoaded", () => {
  const tabs  = document.querySelectorAll(".tab-btn");
  const panes = document.querySelectorAll(".pane");

  // ── 状態変数 ─────────────────────────────────────────────
  let _channels = [];
  let _statusData = null;
  let _statusEventSource = null;
  let _logEventSource = null;
  let _isWsl = null;
  let _pendingLogPath = null;
  const _gpuHistory = [];
  const GPU_MAX_POINTS = 60;
  let _gpuPollTimer = null;
  let _selectedProcessId = null;   // 選択中プロセス id
  let _latestProcesses = [];       // 最新の processes リスト（SSE 更新）

  const SESSION_TYPE_LABELS = {
    autonomous: "autonomous.sh",
    process:    "URL処理",
    summarize:  "Summarize",
    sync:       "Drive Sync",
    transcribe: "文字起こし",
    idle:       "idle",
  };

  function switchTab(id) {
    tabs.forEach(t  => t.classList.toggle("active", t.dataset.tab === id));
    panes.forEach(p => p.classList.toggle("active", p.id === `pane-${id}`));
    history.replaceState(null, "", `#${id}`);
    if (id === "home")   { loadChannels(); loadRunPanel(); }
    if (id === "status") { loadStatus(); startStatusSSE(); }
    else                 stopStatusSSE();
    if (id === "readme") loadReadme();
    if (id === "logs")   loadLogs();
    else                 stopLogStream();
    if (id === "library") {
      initLibrary();
    } else {
      const fab = document.getElementById("lib-chat-fab");
      if (fab) fab.style.display = "none";
      closeLibChat();
    }
  }

  tabs.forEach(t => t.addEventListener("click", () => switchTab(t.dataset.tab)));
  window.goHome = () => switchTab("home");
  const initial = location.hash.replace("#", "") || "home";
  switchTab(initial);
  loadEnvBadge();

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
          <button class="delete-btn" data-name="${esc(ch.name)}" title="削除">×</button>
        </div>`).join("");
      el.querySelectorAll(".delete-btn").forEach(btn => {
        btn.addEventListener("click", e => { e.stopPropagation(); deleteChannel(btn.dataset.name); });
      });
      el.dataset.loaded = "1";
      _updateChannelSelect(channels);
      fetchChannelDriveLinks();
    } catch { el.innerHTML = placeholder("⚠️", "読み込み失敗"); }
  }

  window.reloadChannels = function() {
    const el = document.getElementById("channel-list");
    if (el) { delete el.dataset.loaded; el.innerHTML = placeholder("⏳", "読み込み中…"); }
    loadChannels();
  };

  function _updateChannelSelect(channels) {
    const sel = document.getElementById("proc-channel");
    if (!sel) return;
    const prev = sel.value;
    sel.innerHTML = channels.map(ch => `<option value="${esc(ch.name)}">${esc(ch.name)}</option>`).join("") +
      `<option value="misc">misc</option>`;
    if ([...sel.options].some(o => o.value === prev)) sel.value = prev;
  }

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
          const deleteBtn = item.querySelector(".delete-btn");
          deleteBtn ? item.insertBefore(a, deleteBtn) : item.appendChild(a);
        }
      });
    } catch (e) {}
  }

  // ── STATUS: SSE ─────────────────────────────────────────
  function startStatusSSE() {
    if (_statusEventSource) return;
    _statusEventSource = new EventSource("/api/events");
    _statusEventSource.onmessage = async e => {
      try {
        const d = JSON.parse(e.data);
        if (d.error) return;
        _latestProcesses = d.processes || [];

        if (_selectedProcessId) {
          const still = _latestProcesses.find(p => p.id === _selectedProcessId);
          if (still) {
            if (still.log_file) {
              const fresh = await api(`/api/status-summary?log=${encodeURIComponent(still.log_file)}`);
              renderStatusPanels(fresh);
              renderProcessHeader(still, fresh);
              _statusData = fresh;
            } else if (still.type === "summarize") {
              const fresh = await api(`/api/summarize-session?started=${encodeURIComponent(still.started_at || "")}`);
              renderStatusPanels(fresh);
              renderProcessHeader(still, { status: "running", phase: "summarizing" });
              _statusData = fresh;
            } else {
              renderStatusPanels(_noLogPanels());
              renderProcessHeader(still, { status: "running", phase: "—" });
            }
          } else {
            _selectedProcessId = null;
            renderStatusData(d);
            _statusData = d;
          }
        } else {
          renderStatusData(d);
          _statusData = d;
        }
        renderProcessTabs(_latestProcesses);
      } catch {}
    };
    _statusEventSource.onerror = () => {};
    startGpuPoll();
  }

  function stopStatusSSE() {
    if (_statusEventSource) {
      _statusEventSource.close();
      _statusEventSource = null;
    }
    stopGpuPoll();
  }

  // ── GPU 1秒ポーリング ────────────────────────────────────
  function startGpuPoll() {
    if (_gpuPollTimer) return;
    _pollGpu();
    _gpuPollTimer = setInterval(_pollGpu, 1000);
  }

  function stopGpuPoll() {
    if (_gpuPollTimer) { clearInterval(_gpuPollTimer); _gpuPollTimer = null; }
  }

  async function _pollGpu() {
    try {
      const gpu = await api("/api/gpu", 3000);
      updateGpuGraph(gpu);
    } catch {}
  }

  // ── プロセスタブ描画 ────────────────────────────────────────
  function renderProcessTabs(procs) {
    const headerEl = document.getElementById("status-header-card");
    if (!headerEl) return;

    // タブ行を更新（既存のタブ行があれば差し替え）
    let tabsRow = headerEl.querySelector(".process-tabs");
    if (!procs || procs.length === 0) {
      if (tabsRow) tabsRow.remove();
      return;
    }
    if (!tabsRow) {
      tabsRow = document.createElement("div");
      tabsRow.className = "process-tabs";
      headerEl.prepend(tabsRow);
    }
    tabsRow.innerHTML = procs.map(p => `
      <button class="process-tab${_selectedProcessId === p.id ? " active" : ""}"
              onclick="selectProcess(${JSON.stringify(p).replace(/"/g, '&quot;')})">
        <span class="proc-dot"></span>${esc(p.label)}
      </button>`).join("");
  }

  // ── プロセス選択 ─────────────────────────────────────────────
  window.selectProcess = async function(proc) {
    _selectedProcessId = proc.id;
    renderProcessTabs(_latestProcesses);
    if (proc.log_file) {
      try {
        const d = await api(`/api/status-summary?log=${encodeURIComponent(proc.log_file)}`);
        renderStatusPanels(d);
        renderProcessHeader(proc, d);
        _statusData = d;
      } catch {}
    } else if (proc.type === "summarize") {
      try {
        const d = await api(`/api/summarize-session?started=${encodeURIComponent(proc.started_at || "")}`);
        renderStatusPanels(d);
        renderProcessHeader(proc, { status: "running", phase: "summarizing" });
        _statusData = d;
      } catch {}
    } else {
      renderStatusPanels(_noLogPanels());
      renderProcessHeader(proc, { status: "running", phase: "—" });
    }
  };

  // ── プロセスヘッダー（選択中プロセスの詳細行）────────────────
  function renderProcessHeader(proc, d) {
    const headerEl = document.getElementById("status-header-card");
    if (!headerEl) return;
    let detailRow = headerEl.querySelector(".proc-detail-row");
    if (!detailRow) {
      detailRow = document.createElement("div");
      detailRow.className = "status-header-inner proc-detail-row";
      headerEl.appendChild(detailRow);
    }
    const statusCls = d.status === "running" ? "badge-green"
      : d.status === "rate-limit" ? "badge-warn" : "badge-gray";
    const stopBtn = proc.is_external ? ""
      : proc.type === "autonomous"
        ? `<button class="btn-danger btn-sm" onclick="stopRun()">■ 停止</button>`
        : proc.id
          ? `<button class="btn-danger btn-sm" onclick="stopJob('${esc(proc.id)}')">■ 中止</button>`
          : "";
    const startedStr = proc.started_at ? `started: ${proc.started_at.slice(11,16)}` : "";
    detailRow.innerHTML = `
      <div class="status-header-left">
        <div class="status-script">${esc(proc.label)}</div>
        <div class="status-session">${esc(startedStr)}</div>
      </div>
      <div class="status-header-right">
        <span class="badge ${statusCls}">${esc(d.status)}</span>
        <span class="status-phase">${esc(d.phase)}</span>
        ${stopBtn}
      </div>`;
  }

  // ── ログなし時の空パネルデータ ──────────────────────────────
  function _noLogPanels() {
    return { done_videos: [], running_video: null, done_count: 0, warn_count: 0,
             error_count: 0, rate_limit_count: 0, queue_count: 0,
             phase: "—", status: "running", log_file: "(手動起動 — ログなし)",
             log_file_path: "", drive_folder_url: "" };
  }

  // ── STATUS 全体描画（idle / プロセスなし時）─────────────────
  function renderStatusData(d) {
    const headerEl = document.getElementById("status-header-card");
    if (!headerEl) return;

    const procs = d.processes || [];
    if (procs.length === 0) {
      // idle
      headerEl.innerHTML = `
        <div class="status-header-inner">
          <div class="status-header-left">
            <div class="status-script">idle</div>
          </div>
          <div class="status-header-right">
            <span class="badge badge-gray">${esc(d.status || "idle")}</span>
            <span class="status-phase">${esc(d.phase)}</span>
          </div>
        </div>`;
      renderStatusPanels(d);
    } else {
      // プロセスあり
      const firstSelect = !_selectedProcessId;
      if (!_selectedProcessId) _selectedProcessId = procs[0].id;
      const sel = procs.find(p => p.id === _selectedProcessId) || procs[0];
      headerEl.innerHTML = `<div class="proc-detail-row status-header-inner"></div>`;
      renderProcessTabs(procs);
      renderProcessHeader(sel, { status: "running", phase: "—" });
      // 初回自動選択時のみ即時フェッチ（それ以降は SSE が更新）
      if (firstSelect) selectProcess(sel);
    }
  }

  // ── 動画・統計パネル描画（プロセス切り替え共通）─────────────
  function renderStatusPanels(d) {
    const videosEl = document.getElementById("status-videos");
    const statsEl  = document.getElementById("status-stats");
    if (!videosEl || !statsEl) return;

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
      cards.push(placeholder("🎞️", "no videos this session"));
    }
    videosEl.innerHTML = cards.join("");

    statsEl.innerHTML = `
      <div class="stat-grid">
        <div class="stat-item"><span class="stat-label">queue</span><span class="stat-val">${d.queue_count}</span></div>
        <div class="stat-item stat-clickable" onclick="showLogFilter('done')" title="ログでフィルタ">
          <span class="stat-label">done ↗</span><span class="stat-val stat-green">${d.done_count}</span>
        </div>
        <div class="stat-item stat-clickable" onclick="showLogFilter('warn')" title="ログでフィルタ">
          <span class="stat-label">warn ↗</span><span class="stat-val stat-warn">${d.warn_count}</span>
        </div>
        <div class="stat-item stat-clickable" onclick="showLogFilter('error')" title="ログでフィルタ">
          <span class="stat-label">error ↗</span><span class="stat-val stat-err">${d.error_count}</span>
        </div>
        <div class="stat-item stat-clickable" onclick="showLogFilter('rate-limit')" title="ログでフィルタ">
          <span class="stat-label">rate-limit ↗</span><span class="stat-val">${d.rate_limit_count}</span>
        </div>
        <div class="stat-item"><span class="stat-label">phase</span><span class="stat-val" style="font-size:13px">${esc(d.phase)}</span></div>
      </div>
      <div style="margin-top:12px;display:flex;justify-content:space-between;align-items:center;font-size:11px;color:var(--text-faint)">
        <span title="${esc(d.log_file_path || "")}">ログ: ${esc(d.log_file || "—")}</span>
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

  // ── STATUS: ジョブ停止 ───────────────────────────────────
  window.stopJob = async function(jobId) {
    const ok = await showConfirm("処理を中止しますか？", "中止");
    if (!ok) return;
    try {
      await api(`/api/jobs/${encodeURIComponent(jobId)}/stop`, 5000, "POST");
    } catch (e) {
      await showConfirm(`中止失敗: ${e.message}`, "OK", false);
    }
  };

  // ── STATUS: ログフィルタ ─────────────────────────────────
  function lineColorClass(line) {
    return line.includes("[error]") || line.includes("ERROR") ? "log-error"
         : line.includes("[warn]")  || line.includes("WARN")  ? "log-warn"
         : line.includes("[done]")  || line.includes("Done")  ? "log-done"
         : line.includes("[info]")                             ? "log-info" : "";
  }

  window.openLogByPath = function(path) {
    closeLogFilter();
    _pendingLogPath = path;
    switchTab("logs");
  };

  window.showLogFilter = async function(filterType) {
    if (!_statusData?.log_file_path) return;
    const filterMap = {
      done: "[done]",
      warn: "[warn]",
      error: "[error]",
      "rate-limit": "rate-limit",
    };
    const tag = filterMap[filterType];
    if (!tag) return;

    const logPath = _statusData.log_file_path;
    const logName = logPath.split("/").pop();

    const modal    = document.getElementById("log-filter-modal");
    const titleEl  = document.getElementById("log-filter-title");
    const countEl  = document.getElementById("log-filter-count");
    const content  = document.getElementById("log-filter-content");
    const openBtn  = document.getElementById("log-filter-open-btn");
    if (!modal) return;

    titleEl.textContent = `${filterType} — ${logName}`;
    countEl.textContent = "";
    content.textContent = "読み込み中…";
    if (openBtn) openBtn.onclick = () => openLogByPath(logPath);
    modal.style.display = "flex";

    try {
      const d = await api(`/api/log-content?path=${encodeURIComponent(logPath)}`);
      const allLines = d.content.split("\n");
      const matchIndices = [];
      allLines.forEach((line, i) => { if (line.includes(tag)) matchIndices.push(i); });

      countEl.textContent = `${matchIndices.length} 件`;
      if (!matchIndices.length) { content.textContent = "該当行なし"; return; }

      // ±10行コンテキスト、重複ウィンドウをマージ
      const CONTEXT = 10;
      const groups = [];
      let cur = null;
      for (const idx of matchIndices) {
        const s = Math.max(0, idx - CONTEXT);
        const e = Math.min(allLines.length - 1, idx + CONTEXT);
        if (!cur || s > cur.e + 1) {
          cur = { s, e, matches: new Set([idx]) };
          groups.push(cur);
        } else {
          cur.e = Math.max(cur.e, e);
          cur.matches.add(idx);
        }
      }

      content.innerHTML = "";
      const frag = document.createDocumentFragment();
      groups.forEach((g, gi) => {
        if (gi > 0) {
          const sep = document.createElement("div");
          sep.className = "log-filter-sep";
          frag.appendChild(sep);
        }
        for (let i = g.s; i <= g.e; i++) {
          const line = allLines[i];
          if (i > g.s) frag.appendChild(document.createTextNode("\n"));
          if (g.matches.has(i)) {
            const arrow = document.createElement("span");
            arrow.className = "log-filter-arrow";
            arrow.textContent = "▸ ";
            frag.appendChild(arrow);
            const span = document.createElement("span");
            const colorCls = lineColorClass(line);
            span.className = colorCls ? `${colorCls} log-match` : "log-match";
            span.textContent = line;
            frag.appendChild(span);
          } else {
            const span = document.createElement("span");
            span.className = "log-ctx";
            span.textContent = line;
            frag.appendChild(span);
          }
        }
      });
      content.appendChild(frag);
    } catch (e) {
      content.textContent = `読み込み失敗: ${e.message}`;
    }
  };

  window.closeLogFilter = function() {
    const modal = document.getElementById("log-filter-modal");
    if (modal) modal.style.display = "none";
  };

  // ── GPU グラフ ───────────────────────────────────────────
  function updateGpuGraph(gpu) {
    const initEl = document.getElementById("gpu-init");
    const wrapEl = document.getElementById("gpu-canvas-wrap");
    if (!initEl || !wrapEl) return;

    if (!gpu || !gpu.available) {
      initEl.style.display = "";
      initEl.innerHTML = `<div class="placeholder-icon">🖥️</div><span>GPU データ取得不可</span>`;
      wrapEl.style.display = "none";
      return;
    }

    initEl.style.display = "none";
    wrapEl.style.display = "";

    _gpuHistory.push(gpu.util);
    if (_gpuHistory.length > GPU_MAX_POINTS) _gpuHistory.shift();

    const metaEl = document.getElementById("gpu-meta-row");
    if (metaEl) {
      let html = `<span class="gpu-util-val">${gpu.util}</span><span class="gpu-util-unit">%</span>`;
      if (gpu.mem_total > 0) {
        const pct = Math.round(gpu.mem_used / gpu.mem_total * 100);
        html += `<span class="gpu-meta-item">VRAM ${gpu.mem_used} / ${gpu.mem_total} MiB (${pct}%)</span>`;
      }
      if (gpu.temp > 0) {
        html += `<span class="gpu-meta-item">${gpu.temp}°C</span>`;
      }
      metaEl.innerHTML = html;
    }

    drawGpuSparkline();
  }

  function drawGpuSparkline() {
    const canvas = document.getElementById("gpu-canvas");
    if (!canvas || _gpuHistory.length < 2) return;

    const W = canvas.offsetWidth || 800;
    const H = 80;
    if (canvas.width !== W) canvas.width = W;
    canvas.height = H;

    const ctx = canvas.getContext("2d");
    ctx.clearRect(0, 0, W, H);

    const padT = 6, padB = 6, padL = 2, padR = 2;
    const gW = W - padL - padR;
    const gH = H - padT - padB;

    ctx.strokeStyle = "rgba(255,255,255,0.05)";
    ctx.lineWidth = 1;
    [0.25, 0.5, 0.75].forEach(f => {
      const y = padT + gH * (1 - f);
      ctx.beginPath();
      ctx.moveTo(padL, y);
      ctx.lineTo(padL + gW, y);
      ctx.stroke();
    });

    const offset = GPU_MAX_POINTS - _gpuHistory.length;
    const xOf = i => padL + ((offset + i) / (GPU_MAX_POINTS - 1)) * gW;
    const yOf = v => padT + gH * (1 - v / 100);

    const grad = ctx.createLinearGradient(0, padT, 0, padT + gH);
    grad.addColorStop(0, "rgba(10,132,255,0.30)");
    grad.addColorStop(1, "rgba(10,132,255,0.02)");

    ctx.beginPath();
    _gpuHistory.forEach((v, i) => {
      const x = xOf(i), y = yOf(v);
      i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
    });
    ctx.lineTo(xOf(_gpuHistory.length - 1), padT + gH);
    ctx.lineTo(xOf(0), padT + gH);
    ctx.closePath();
    ctx.fillStyle = grad;
    ctx.fill();

    ctx.beginPath();
    _gpuHistory.forEach((v, i) => {
      const x = xOf(i), y = yOf(v);
      i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
    });
    ctx.strokeStyle = "rgba(10,132,255,0.9)";
    ctx.lineWidth = 1.5;
    ctx.lineJoin = "round";
    ctx.stroke();

    const lastV = _gpuHistory[_gpuHistory.length - 1];
    const dotX = xOf(_gpuHistory.length - 1);
    const dotY = yOf(lastV);
    ctx.beginPath();
    ctx.arc(dotX, dotY, 3, 0, Math.PI * 2);
    ctx.fillStyle = "rgba(10,132,255,1)";
    ctx.fill();
    ctx.strokeStyle = "rgba(255,255,255,0.5)";
    ctx.lineWidth = 1;
    ctx.stroke();
  }

  window.addEventListener("resize", drawGpuSparkline);

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
    if (!el) return;
    el.innerHTML = placeholder("⏳", "読み込み中…");
    try {
      const { logs } = await api("/api/logs");
      if (!logs.length) { el.innerHTML = placeholder("📭", "ログファイルなし"); return; }
      el.innerHTML = logs.map(l => {
        const badgeCls = !l.is_done ? "badge-blue" : l.has_error ? "badge-err" : "badge-green";
        const badgeText = !l.is_done ? "live" : l.has_error ? "error" : "done";
        return `
        <div class="channel-item log-file-item" data-path="${esc(l.path)}" data-is-done="${l.is_done}" onclick="openLog(this)">
          <span class="badge ${badgeCls}" data-live-badge>${badgeText}</span>
          <span class="channel-name">${esc(l.path)}</span>
          <span style="color:var(--text-faint);font-size:11px;flex-shrink:0">${(l.size/1024).toFixed(1)} KB</span>
        </div>`;
      }).join("");
      if (_pendingLogPath) {
        const target = el.querySelector(`[data-path="${CSS.escape(_pendingLogPath)}"]`);
        if (target) { _pendingLogPath = null; openLog(target); }
        else _pendingLogPath = null;
      }
    } catch { el.innerHTML = placeholder("⚠️", "読み込み失敗"); }
  }

  window.reloadLogs = function() { stopLogStream(); loadLogs(); };

  window.openLog = async function(el) {
    document.querySelectorAll(".log-file-item").forEach(e => e.classList.remove("active-log"));
    el.classList.add("active-log");
    const path   = el.dataset.path;
    const isDone = el.dataset.isDone === "true";
    const card    = document.getElementById("log-viewer-card");
    const titleEl = document.getElementById("log-viewer-title");
    const content = document.getElementById("log-viewer-content");
    card.style.display = "flex";
    titleEl.textContent = path.split("/").pop();
    content.textContent = "読み込み中…";

    stopLogStream();

    if (!isDone) {
      // live ログ: SSE ストリームで tail -f 相当
      startLogStream(path, content, el);
    } else {
      try {
        const d = await api(`/api/log-content?path=${encodeURIComponent(path)}`);
        renderLog(content, d.content.split("\n"));
      } catch { content.textContent = "読み込み失敗"; }
    }
  };

  function startLogStream(path, contentEl, listEl) {
    contentEl.textContent = "";
    const es = new EventSource(`/api/log-stream?path=${encodeURIComponent(path)}`);
    _logEventSource = es;

    es.onmessage = e => {
      try {
        const d = JSON.parse(e.data);
        if (d.lines) {
          if (d.init) {
            renderLog(contentEl, d.lines);
          } else {
            appendLogLines(contentEl, d.lines);
          }
        }
        if (d.done) {
          stopLogStream();
          // バッジを done に更新
          if (listEl) {
            const badge = listEl.querySelector("[data-live-badge]");
            if (badge) {
              badge.className = "badge badge-green";
              badge.textContent = "done";
            }
            listEl.dataset.isDone = "true";
          }
        }
      } catch {}
    };
    es.onerror = () => {
      // 接続エラーは自動再接続に任せる
    };
  }

  function stopLogStream() {
    if (_logEventSource) {
      _logEventSource.close();
      _logEventSource = null;
    }
  }

  function appendLogLines(el, lines) {
    const fragment = document.createDocumentFragment();
    lines.forEach(l => {
      if (!(el.textContent === "" && el.children.length === 0)) {
        fragment.appendChild(document.createTextNode("\n"));
      }
      const span = document.createElement("span");
      const cls = lineColorClass(l);
      if (cls) span.className = cls;
      span.textContent = l;
      fragment.appendChild(span);
    });
    el.appendChild(fragment);
    el.scrollTop = el.scrollHeight;
  }

  window.closeLogViewer = function() {
    stopLogStream();
    document.getElementById("log-viewer-card").style.display = "none";
    document.querySelectorAll(".log-file-item").forEach(e => e.classList.remove("active-log"));
  };

  // ── utils ────────────────────────────────────────────────
  async function api(url, timeoutMs = 10000, method = "GET", body = null) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeoutMs);
    try {
      const opts = { method, signal: ctrl.signal };
      if (body !== null) {
        opts.headers = { "Content-Type": "application/json" };
        opts.body = JSON.stringify(body);
      }
      const r = await fetch(url, opts);
      if (!r.ok) {
        let msg = String(r.status);
        try { const j = await r.json(); msg = j.error || msg; } catch {}
        throw new Error(msg);
      }
      return r.json();
    } finally {
      clearTimeout(timer);
    }
  }

  function renderLog(el, lines) {
    el.innerHTML = "";
    el.textContent = "";
    const fragment = document.createDocumentFragment();
    lines.forEach((l, i) => {
      if (i > 0) fragment.appendChild(document.createTextNode("\n"));
      const span = document.createElement("span");
      const cls = lineColorClass(l);
      if (cls) span.className = cls;
      span.textContent = l;
      fragment.appendChild(span);
    });
    el.appendChild(fragment);
    el.scrollTop = el.scrollHeight;
  }

  function placeholder(icon, text) {
    return `<div class="placeholder"><div class="placeholder-icon">${icon}</div><span>${text}</span></div>`;
  }

  // ── HOME: 実行パネル ────────────────────────────────────────
  async function loadRunPanel() {
    if (_isWsl === null) {
      try { const d = await api("/api/env"); _isWsl = d.is_wsl; }
      catch { _isWsl = false; }
      if (!_isWsl) {
        const warn = document.getElementById("run-wsl-warn");
        if (warn) warn.style.display = "block";
        const startBtn = document.getElementById("run-start-btn");
        if (startBtn) startBtn.disabled = true;
        const badge = document.getElementById("run-badge");
        if (badge) { badge.className = "badge badge-gray"; badge.textContent = "WSL only"; }
        return;
      }
    }
    await _updateRunBadge();
  }

  async function _updateRunBadge() {
    if (!_isWsl) return;
    try {
      const { running, session } = await api("/api/run/status");
      const badge    = document.getElementById("run-badge");
      const startBtn = document.getElementById("run-start-btn");
      const stopBtn  = document.getElementById("run-stop-btn");
      if (!badge) return;
      if (running) {
        badge.className   = "badge badge-green";
        badge.textContent = session ? `running: ${session}` : "running";
        if (startBtn) startBtn.style.display = "none";
        if (stopBtn)  stopBtn.style.display  = "";
      } else {
        badge.className   = "badge badge-gray"; badge.textContent = "stopped";
        if (startBtn) startBtn.style.display = "";
        if (stopBtn)  stopBtn.style.display  = "none";
      }
    } catch {}
  }

  window.startRun = async function() {
    const startBtn = document.getElementById("run-start-btn");
    const origText = startBtn.textContent;
    startBtn.disabled = true; startBtn.textContent = "starting…";
    try {
      await api("/api/run", 10000, "POST", { limit: 10, model: "large-v3" });
      await _updateRunBadge();
    } catch (e) {
      await showConfirm(`起動失敗: ${e.message}`, "OK", false);
      startBtn.disabled = false; startBtn.textContent = origText;
    }
  };

  window.stopRun = async function() {
    const ok = await showConfirm("autonomous.sh を停止しますか？", "停止");
    if (!ok) return;
    const stopBtn = document.getElementById("run-stop-btn");
    if (stopBtn) stopBtn.disabled = true;
    try {
      await api("/api/run/stop", 10000, "POST");
      await _updateRunBadge();
    } catch (e) {
      await showConfirm(`停止失敗: ${e.message}`, "OK", false);
    } finally {
      if (stopBtn) stopBtn.disabled = false;
    }
  };

  // ── HOME: URL 処理 ──────────────────────────────────────
  window.processUrl = async function() {
    const raw     = document.getElementById("proc-urls").value;
    const urls    = raw.split("\n").map(s => s.trim()).filter(Boolean);
    const channel = document.getElementById("proc-channel").value || "misc";
    const lang    = document.getElementById("proc-lang").value;
    const resultEl = document.getElementById("proc-result");
    const btn      = document.getElementById("proc-submit-btn");
    if (!urls.length) {
      resultEl.style.color = "var(--err)"; resultEl.textContent = "URL を入力してください"; return;
    }
    btn.disabled = true; resultEl.style.color = "var(--text-dim)"; resultEl.textContent = "送信中…";
    try {
      const res = await api("/api/process-url", 10000, "POST", { urls, channel, lang });
      resultEl.style.color = "var(--green)";
      resultEl.textContent = res.message || "処理を開始しました";
      document.getElementById("proc-urls").value = "";
    } catch (e) {
      resultEl.style.color = "var(--err)"; resultEl.textContent = String(e.message) || "エラー";
    } finally {
      btn.disabled = false;
    }
  };

  // ── HOME: CLI コマンド ──────────────────────────────────
  async function _runCmd(endpoint, payload, resultId, btnEl) {
    const resultEl = document.getElementById(resultId);
    const origText = btnEl.textContent;
    btnEl.disabled = true;
    if (resultEl) { resultEl.style.color = "var(--text-dim)"; resultEl.textContent = "送信中…"; }
    try {
      const res = await api(endpoint, 30000, "POST", payload);
      if (resultEl) { resultEl.style.color = "var(--green)"; resultEl.textContent = res.message || "開始しました"; }
    } catch (e) {
      if (resultEl) { resultEl.style.color = "var(--err)"; resultEl.textContent = String(e.message) || "エラー"; }
    } finally {
      btnEl.disabled = false; btnEl.textContent = origText;
    }
  }

  window.transcribeSync = async function() {
    const btn = event.currentTarget;
    await _runCmd("/api/transcribe/sync", {}, "batch-result", btn);
  };

  window.summarizeAll = async function() {
    const threshold = parseInt(document.getElementById("batch-threshold").value) || 20;
    const btn       = event.currentTarget;
    await _runCmd("/api/summarize", { threshold }, "batch-result", btn);
  };

  // ── HOME: チャンネル管理モーダル ───────────────────────────
  window.openAddChannelModal = function() {
    document.getElementById("modal-error").textContent = "";
    document.getElementById("add-channel-modal").style.display = "flex";
    document.getElementById("modal-name").focus();
  };

  window.closeAddChannelModal = function() {
    document.getElementById("add-channel-modal").style.display = "none";
  };

  window.submitAddChannel = async function() {
    const name  = document.getElementById("modal-name").value.trim();
    const url   = document.getElementById("modal-url").value.trim();
    const lang  = document.getElementById("modal-lang").value;
    const errEl = document.getElementById("modal-error");
    if (!name || !url) { errEl.textContent = "名前と URL を入力してください"; return; }
    try {
      await api("/api/channels", 10000, "POST", { name, url, lang });
      closeAddChannelModal();
      document.getElementById("modal-name").value = "";
      document.getElementById("modal-url").value  = "";
      reloadChannels();
    } catch (e) { errEl.textContent = String(e.message) || "追加失敗"; }
  };

  window.deleteChannel = async function(name) {
    const ok = await showConfirm(`"${name}" を削除しますか？`);
    if (!ok) return;
    try {
      const r = await fetch(`/api/channels?name=${encodeURIComponent(name)}`, { method: "DELETE" });
      if (!r.ok) {
        const j = await r.json().catch(() => ({}));
        throw new Error(j.error || r.status);
      }
      reloadChannels();
    } catch (e) { await showConfirm(`削除失敗: ${e.message}`, "OK", false); }
  };

  // ── 汎用確認ダイアログ ─────────────────────────────────────
  function showConfirm(message, okLabel = "削除", showCancel = true) {
    return new Promise(resolve => {
      const modal  = document.getElementById("confirm-modal");
      const msgEl  = document.getElementById("confirm-message");
      const okBtn  = document.getElementById("confirm-ok");
      const canBtn = document.getElementById("confirm-cancel");
      msgEl.textContent   = message;
      okBtn.textContent   = okLabel;
      canBtn.style.display = showCancel ? "" : "none";
      modal.style.display = "flex";
      const cleanup = (result) => {
        modal.style.display = "none";
        okBtn.removeEventListener("click", onOk);
        canBtn.removeEventListener("click", onCancel);
        resolve(result);
      };
      const onOk     = () => cleanup(true);
      const onCancel = () => cleanup(false);
      okBtn.addEventListener("click",  onOk);
      canBtn.addEventListener("click", onCancel);
    });
  }

  document.addEventListener("keydown", e => {
    if (e.key === "Escape") {
      closeAddChannelModal();
      closeLogFilter();
      closeLibViewer();
      document.getElementById("confirm-modal").style.display = "none";
    }
  });

  function esc(s) {
    return String(s ?? "").replace(/[&<>"']/g, c =>
      ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  }

  // ── Library: 状態変数 ────────────────────────────────────────
  let _libSelectedChannels = new Set();
  let _libCurrentPage = 1;
  let _libCurrentQuery = "";
  let _libCurrentScope = "points";
  let _libTotalPages = 1;
  let _libCheckedPaths = new Set();
  let _libChatMessages = [];
  let _libFileChatMessages = [];
  let _libCurrentFilePath = "";
  let _libChatPanelOpen = false;
  let _libChatSSE = null;
  let _libInitialized = false;

  // ── Library: 初期化 ──────────────────────────────────────────
  async function initLibrary() {
    if (_isWsl === null) {
      try { const d = await api("/api/env"); _isWsl = d.is_wsl; } catch { _isWsl = false; }
    }
    const warn = document.getElementById("lib-wsl-warn");
    const card = document.getElementById("lib-search-card");
    const fab  = document.getElementById("lib-chat-fab");
    if (!_isWsl) {
      if (warn) warn.style.display = "";
      if (card) card.style.display = "none";
      if (fab)  fab.style.display  = "none";
      return;
    }
    if (warn) warn.style.display = "none";
    if (card) card.style.display = "";
    if (fab && !_libChatPanelOpen) fab.style.display = "";
    if (!_libInitialized) {
      _libInitialized = true;
      await loadLibraryChannels();
    }
    _updateGpuWarn();
  }

  async function loadLibraryChannels() {
    try {
      const data = await api("/api/library/channels");
      renderChannelChips(data.channels || []);
    } catch {}
  }

  function renderChannelChips(channels) {
    const el = document.getElementById("lib-channel-chips");
    if (!el) return;
    el.innerHTML = "";
    channels.forEach(ch => {
      const btn = document.createElement("button");
      btn.className = "lib-chip" + (_libSelectedChannels.has(ch.name) ? " selected" : "");
      btn.textContent = `${ch.name} (${ch.count})`;
      btn.dataset.channel = ch.name;
      btn.onclick = () => toggleLibChip(ch.name);
      el.appendChild(btn);
    });
  }

  function toggleLibChip(name) {
    if (_libSelectedChannels.has(name)) _libSelectedChannels.delete(name);
    else _libSelectedChannels.add(name);
    document.querySelectorAll(".lib-chip").forEach(el => {
      el.classList.toggle("selected", _libSelectedChannels.has(el.dataset.channel));
    });
    _libCurrentPage = 1;
    if (_libCurrentQuery || _libSelectedChannels.size > 0) fetchLibResults();
    else _libClearResults();
  }

  let _libSearchTimer = null;
  window.libSearchInstant = function() {
    const input = document.getElementById("lib-search-input");
    const scope = document.getElementById("lib-scope");
    _libCurrentQuery = input ? input.value.trim() : "";
    _libCurrentScope = scope ? scope.value : "points";
    _libCurrentPage = 1;
    clearTimeout(_libSearchTimer);
    if (!_libCurrentQuery && _libSelectedChannels.size === 0) { _libClearResults(); return; }
    _libSearchTimer = setTimeout(fetchLibResults, 300);
  };

  window.libSearch = window.libSearchInstant;

  window.selectAllLib = function() {
    document.querySelectorAll(".lib-chip").forEach(btn => {
      _libSelectedChannels.add(btn.dataset.channel);
      btn.classList.add("selected");
    });
    _libCurrentPage = 1;
    if (_libSelectedChannels.size > 0) fetchLibResults();
  };

  window.libClear = function() {
    _libSelectedChannels.clear();
    _libCurrentQuery = "";
    _libCurrentPage = 1;
    const input = document.getElementById("lib-search-input");
    if (input) input.value = "";
    document.querySelectorAll(".lib-chip").forEach(el => el.classList.remove("selected"));
    _libClearResults();
  };

  function _libClearResults() {
    const el = document.getElementById("lib-results");
    if (el) el.innerHTML = "";
    const pg = document.getElementById("lib-pagination");
    if (pg) pg.style.display = "none";
  }

  async function fetchLibResults() {
    if (!_libCurrentQuery && _libSelectedChannels.size === 0) { _libClearResults(); return; }
    const resultsEl = document.getElementById("lib-results");
    const paginEl = document.getElementById("lib-pagination");
    if (!resultsEl) return;
    resultsEl.innerHTML = placeholder("⏳", "読み込み中…");
    try {
      const chParam = [..._libSelectedChannels].join(",");
      let data;
      if (_libCurrentQuery) {
        const params = new URLSearchParams({
          q: _libCurrentQuery, channels: chParam,
          page: _libCurrentPage, scope: _libCurrentScope,
        });
        data = await api(`/api/library/search?${params}`, 30000);
      } else {
        const params = new URLSearchParams({
          channels: chParam, page: _libCurrentPage, per_page: 20,
        });
        data = await api(`/api/library/files?${params}`, 30000);
      }
      _libTotalPages = data.pages || 1;
      renderLibResults(data.results || []);
      updateLibPagination(data.total || 0, data.page || 1, data.pages || 1);
    } catch (e) {
      resultsEl.innerHTML = placeholder("❌", `エラー: ${e.message}`);
      if (paginEl) paginEl.style.display = "none";
    }
  }

  function renderLibResults(results) {
    const el = document.getElementById("lib-results");
    if (!el) return;
    if (!results.length) { el.innerHTML = placeholder("🔍", "結果がありません"); return; }

    const groups = {};
    results.forEach(r => { if (!groups[r.channel]) groups[r.channel] = []; groups[r.channel].push(r); });

    let html = "";
    for (const [ch, items] of Object.entries(groups)) {
      html += `<div class="lib-group-header">${esc(ch)}</div><div class="lib-cards-grid">`;
      items.forEach(r => {
        const isChecked = _libCheckedPaths.has(r.path);
        const pts = (r.points || []).slice(0, 3)
          .map(p => `<div>• ${esc(p.replace(/^- /, ""))}</div>`).join("");
        const safePath = r.path.replace(/'/g, "\\'");
        html += `
          <div class="lib-result-card${isChecked ? " selected" : ""}"
               onclick="openLibViewer('${safePath}')">
            <div class="lib-card-title">${esc(r.title)}</div>
            <div class="lib-card-meta">${esc(r.date || "")}</div>
            <div class="lib-card-points">${pts}</div>
            <input type="checkbox" class="lib-card-cb"${isChecked ? " checked" : ""}
                   onclick="event.stopPropagation();toggleLibCardCheck('${safePath}', this)">
          </div>`;
      });
      html += "</div>";
    }
    el.innerHTML = html;
  }

  function updateLibPagination(total, page, pages) {
    const el = document.getElementById("lib-pagination");
    const info = document.getElementById("lib-page-info");
    if (!el) return;
    if (!total) { el.style.display = "none"; return; }
    el.style.display = "flex";
    if (info) info.textContent = `${page} / ${pages} ページ（計 ${total} 件）`;
    const btns = el.querySelectorAll("button");
    if (btns[0]) btns[0].disabled = page <= 1;
    if (btns[1]) btns[1].disabled = page >= pages;
  }

  window.libPagePrev = function() {
    if (_libCurrentPage > 1) { _libCurrentPage--; fetchLibResults(); }
  };
  window.libPageNext = function() {
    if (_libCurrentPage < _libTotalPages) { _libCurrentPage++; fetchLibResults(); }
  };

  // ── Library: チェックボックス管理 ───────────────────────────
  window.toggleLibCardCheck = function(path, el) {
    if (el.checked) _libCheckedPaths.add(path);
    else            _libCheckedPaths.delete(path);
    el.closest(".lib-result-card").classList.toggle("selected", el.checked);
    _updateChatContextLabel();
  };

  function _updateChatContextLabel() {
    const label = document.getElementById("lib-chat-ctx-label");
    if (!label) return;
    const n = _libCheckedPaths.size;
    label.textContent = n > 0 ? `選択 ${n} 件` : "ライブラリ全体";
  }

  // ── Library: ビューアーモーダル ─────────────────────────────
  window.openLibViewer = async function(path) {
    const modal = document.getElementById("lib-viewer-modal");
    const contentEl = document.getElementById("lib-viewer-content");
    const chEl = document.getElementById("lib-viewer-ch");
    const ytEl = document.getElementById("lib-viewer-yt");
    if (!modal || !contentEl) return;

    _libCurrentFilePath = path;
    _libFileChatMessages = [];
    const fileChatEl = document.getElementById("lib-file-chat-messages");
    if (fileChatEl) fileChatEl.innerHTML = "";

    contentEl.innerHTML = placeholder("⏳", "読み込み中…");
    modal.style.display = "flex";

    try {
      const data = await api(`/api/library/transcript?path=${encodeURIComponent(path)}`, 15000);
      if (chEl) chEl.textContent = data.meta?.channel || path.split("/")[1] || "";
      if (ytEl) {
        if (data.meta?.url) { ytEl.href = data.meta.url; ytEl.style.display = ""; }
        else ytEl.style.display = "none";
      }
      const content = (data.content || "")
        .replace(/^(チャンネル:.*)$/m, "$1  ")
        .replace(/^(URL:.*)$/m, "$1  ")
        .replace(/^(モデル:.*)$/m, "$1  ")
        .replace(/^(処理日時:.*)$/m, "$1  ");
      if (typeof marked !== "undefined") {
        contentEl.innerHTML = marked.parse(content);
      } else {
        contentEl.textContent = content;
      }
    } catch (e) {
      contentEl.innerHTML = placeholder("❌", `エラー: ${e.message}`);
    }
  };

  function closeLibViewer() {
    const modal = document.getElementById("lib-viewer-modal");
    if (modal) modal.style.display = "none";
    _libCurrentFilePath = "";
  }
  window.closeLibViewer = closeLibViewer;

  // ── Library: SSE チャットストリーム ─────────────────────────
  async function _libStreamChat(messages, paths, messagesEl) {
    if (_libChatSSE) { _libChatSSE.abort(); _libChatSSE = null; }
    const ctrl = new AbortController();
    _libChatSSE = ctrl;
    const bubble = _appendChatBubble("ai", "", messagesEl);
    bubble.classList.add("lib-loading");
    bubble.innerHTML = '<span class="lib-typing-dots"><span></span><span></span><span></span></span>';
    let fullText = "";
    try {
      const resp = await fetch("/api/library/chat", {
        method: "POST", signal: ctrl.signal,
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ messages, paths }),
      });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const reader = resp.body.getReader();
      const dec = new TextDecoder();
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        for (const line of dec.decode(value, { stream: true }).split("\n")) {
          if (!line.startsWith("data:")) continue;
          let d;
          try { d = JSON.parse(line.slice(5).trim()); } catch { continue; }
          if (d.chunk) {
            fullText += d.chunk;
            bubble.classList.remove("lib-loading");
            bubble.innerHTML = typeof marked !== "undefined" ? marked.parse(fullText) : fullText;
            messagesEl.scrollTop = 999999;
          }
          if (d.error) {
            const msg = String(d.error);
            const short = msg.includes("429") || msg.toLowerCase().includes("resource_exhausted")
              ? "レート制限（Gemini 429）— しばらくしてから再試行してください"
              : msg.length > 120 ? msg.slice(0, 120) + "…" : msg;
            bubble.textContent = `エラー: ${short}`;
            break;
          }
          if (d.done) break;
        }
      }
    } catch (e) {
      if (e.name !== "AbortError") bubble.textContent = `エラー: ${e.message}`;
    } finally {
      _libChatSSE = null;
    }
    return fullText;
  }

  function _appendChatBubble(role, text, containerEl) {
    const div = document.createElement("div");
    div.className = `lib-bubble lib-bubble-${role}`;
    if (role === "ai") {
      div.className += " markdown-body";
      div.innerHTML = (text && typeof marked !== "undefined") ? marked.parse(text) : (text || "");
    } else {
      div.textContent = text;
    }
    containerEl.appendChild(div);
    containerEl.scrollTop = 999999;
    return div;
  }

  // ── Library: 右パネル（全体チャット） ───────────────────────
  window.toggleLibChat = function() {
    const panel = document.getElementById("lib-chat-panel");
    const fab   = document.getElementById("lib-chat-fab");
    if (!panel) return;
    _libChatPanelOpen = !_libChatPanelOpen;
    panel.classList.toggle("open", _libChatPanelOpen);
    if (fab) fab.style.display = _libChatPanelOpen ? "none" : "";
    if (_libChatPanelOpen) {
      const input = document.getElementById("lib-chat-input");
      if (input) input.focus();
    }
  };

  function closeLibChat() {
    const panel = document.getElementById("lib-chat-panel");
    const fab   = document.getElementById("lib-chat-fab");
    if (panel) panel.classList.remove("open");
    if (fab && _isWsl) fab.style.display = "";
    _libChatPanelOpen = false;
  }
  window.closeLibChat = closeLibChat;

  window.libChatClear = function() {
    _libChatMessages = [];
    const el = document.getElementById("lib-chat-messages");
    if (el) el.innerHTML = "";
  };

  window.libChatSend = async function() {
    const input = document.getElementById("lib-chat-input");
    if (!input) return;
    const text = input.value.trim();
    if (!text) return;
    input.value = "";
    const messagesEl = document.getElementById("lib-chat-messages");
    _libChatMessages.push({ role: "user", content: text });
    _appendChatBubble("user", text, messagesEl);
    _updateChatContextLabel();
    const paths = _libCheckedPaths.size > 0 ? [..._libCheckedPaths] : [];
    const aiText = await _libStreamChat([..._libChatMessages], paths, messagesEl);
    if (aiText) _libChatMessages.push({ role: "assistant", content: aiText });
  };

  // ── Library: ファイルチャット（ビューアー内） ────────────────
  window.libFileChatClear = function() {
    _libFileChatMessages = [];
    const el = document.getElementById("lib-file-chat-messages");
    if (el) el.innerHTML = "";
  };

  window.libFileChatSend = async function() {
    const input = document.getElementById("lib-file-chat-input");
    if (!input) return;
    const text = input.value.trim();
    if (!text) return;
    input.value = "";
    const messagesEl = document.getElementById("lib-file-chat-messages");
    _libFileChatMessages.push({ role: "user", content: text });
    _appendChatBubble("user", text, messagesEl);
    const paths = _libCurrentFilePath ? [_libCurrentFilePath] : [];
    const aiText = await _libStreamChat([..._libFileChatMessages], paths, messagesEl);
    if (aiText) _libFileChatMessages.push({ role: "assistant", content: aiText });
  };

  // ── Library: GPU 警告バッジ ──────────────────────────────────
  function _updateGpuWarn() {
    const warn = document.getElementById("lib-gpu-warn");
    if (!warn) return;
    const hasGpu = _latestProcesses.some(p =>
      p.type === "autonomous" || p.type === "transcribe" || p.type === "loop"
    );
    warn.style.display = hasGpu ? "" : "none";
  }

  // ── ヘッダー環境バッジ ───────────────────────────────────────
  async function loadEnvBadge() {
    try {
      const { is_wsl } = await api("/api/env");
      const el = document.getElementById("env-badge");
      if (!el) return;
      const img = document.createElement("img");
      img.src = is_wsl ? "/static/linux.png" : "/static/apple.png";
      img.className = is_wsl ? "env-icon" : "env-icon env-icon-mac";
      img.alt = is_wsl ? "WSL" : "Mac";
      const label = document.createElement("span");
      label.className = "env-label";
      label.textContent = is_wsl ? "WSL" : "Mac";
      el.appendChild(img);
      el.appendChild(label);
    } catch {}
  }
});
