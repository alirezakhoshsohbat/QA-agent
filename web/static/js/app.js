/* QA Agent — Gherkin Test Studio */

(function () {
  "use strict";

  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => document.querySelectorAll(sel);

  let currentGherkin = "";
  let currentRunId = null;
  let activeEventSource = null;
  let activeJobKind = "generate";
  let lastActivitySig = "";
  let allRuns = [];
  let lastStatus = null;

  const ICONS = {
    search: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><path d="M21 21l-4.35-4.35"/></svg>',
    read: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M2 3h6a4 4 0 014 4v14a3 3 0 00-3-3H2z"/><path d="M22 3h-6a4 4 0 00-4 4v14a3 3 0 013-3h7z"/></svg>',
    diff: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M16 3h5v5"/><path d="M8 3H3v5"/><path d="M12 22v-8.3a4 4 0 00-1.172-2.872L3 3"/><path d="M15 9l6-6"/></svg>',
    file: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><path d="M14 2v6h6"/></svg>',
    graph: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><path d="M8.59 13.51l6.83 3.98M15.41 6.51l-6.82 3.98"/></svg>',
    delegate: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 00-3-3.87"/><path d="M16 3.13a4 4 0 010 7.75"/></svg>',
    write: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 013 3L7 19l-4 1 1-4L16.5 3.5z"/></svg>',
    plan: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 01-2 2H5a2 2 0 01-2-2V5a2 2 0 012-2h11"/></svg>',
    tool: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14.7 6.3a1 1 0 000 1.4l1.6 1.6a1 1 0 001.4 0l3.77-3.77a6 6 0 01-7.94 7.94l-6.91 6.91a2.12 2.12 0 01-3-3l6.91-6.91a6 6 0 017.94-7.94l-3.76 3.76z"/></svg>',
  };

  const CHECK = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><polyline points="20 6 9 17 4 12"/></svg>';

  // ── API ──────────────────────────────────────────────────────────

  async function api(path, options = {}) {
    const res = await fetch(path, {
      headers: { "Content-Type": "application/json", ...options.headers },
      ...options,
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(err.detail || res.statusText);
    }
    return res.json();
  }

  // ── Theme ────────────────────────────────────────────────────────

  function applyTheme(theme) {
    const isLight = theme === "light";
    document.documentElement.setAttribute("data-theme", isLight ? "light" : "dark");
    $(".icon-moon").classList.toggle("hidden", isLight);
    $(".icon-sun").classList.toggle("hidden", !isLight);
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute("content", isLight ? "#f5f7fb" : "#0a0d15");
  }

  function initTheme() {
    const saved = localStorage.getItem("qa-theme") || "dark";
    applyTheme(saved);
    $("#themeBtn").addEventListener("click", () => {
      const next = document.documentElement.getAttribute("data-theme") === "light" ? "dark" : "light";
      localStorage.setItem("qa-theme", next);
      applyTheme(next);
    });
  }

  // ── Toast ────────────────────────────────────────────────────────

  function toast(message, type = "success") {
    const icon = type === "error"
      ? '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>'
      : '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 11.08V12a10 10 0 11-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>';
    const el = document.createElement("div");
    el.className = `toast ${type}`;
    el.innerHTML = `${icon}<span></span>`;
    el.querySelector("span").textContent = message;
    $("#toastWrap").appendChild(el);
    setTimeout(() => {
      el.style.opacity = "0";
      setTimeout(() => el.remove(), 250);
    }, 3600);
  }

  function escapeHtml(str) {
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  // ── Gherkin rendering (gutter + highlight) ─────────────────────────

  function highlightLine(line) {
    let escaped = escapeHtml(line);
    if (/^\s*Feature:/i.test(line)) return `<span class="gk-feature">${escaped}</span>`;
    if (/^\s*(Scenario Outline|Scenario|Background|Rule|Example):/i.test(line))
      return `<span class="gk-scenario">${escaped}</span>`;
    if (/^\s*@[\w-]/.test(line)) return `<span class="gk-tag">${escaped}</span>`;
    if (/^\s*#/.test(line)) return `<span class="gk-comment">${escaped}</span>`;
    if (/^\s*(Given|When|Then|And|But)\s/i.test(line)) {
      escaped = escaped.replace(/&quot;([^&]*)&quot;/g, '<span class="gk-string">&quot;$1&quot;</span>');
      escaped = escaped.replace(/^(\s*)(Given|When|Then|And|But)\b/i, '$1<span class="gk-keyword">$2</span>');
      return `<span class="gk-step">${escaped}</span>`;
    }
    return escaped;
  }

  function renderGherkin(text) {
    const empty = $("#gherkinEmpty");
    const view = $("#gherkinView");
    if (!text || text.startsWith("خروجی") || text.startsWith("درخواست")) {
      empty.classList.remove("hidden");
      view.classList.add("hidden");
      return;
    }
    empty.classList.add("hidden");
    view.classList.remove("hidden");
    const lines = text.split("\n");
    $("#gherkinGutter").textContent = lines.map((_, i) => i + 1).join("\n");
    $("#gherkinBody").innerHTML = lines.map(highlightLine).join("\n");
  }

  // ── Status & integrations ──────────────────────────────────────────

  async function loadStatus() {
    try {
      renderStatus(await api("/api/status"));
    } catch (e) {
      const cred = $("#credStatus");
      cred.className = "status-pill error";
      cred.querySelector(".status-text").textContent = "سرور در دسترس نیست";
    }
  }

  function renderStatus(data) {
    lastStatus = data;
    const cred = $("#credStatus");
    cred.className = "status-pill " + (data.credentials_ok ? "ok" : "warn");
    cred.querySelector(".status-text").textContent = data.credentials_ok
      ? "Credentials OK"
      : `کمبود: ${data.missing_credentials.join(", ")}`;

    $("#modelStats").innerHTML = `
      <div class="row"><span class="k">Profile</span><span class="v mono">${escapeHtml(data.profile)}</span></div>
      <div class="row"><span class="k">Nano</span><span class="v mono">${escapeHtml(data.nano_model || "—")}</span></div>
      <div class="row"><span class="k">Research</span><span class="v mono">${escapeHtml(data.research_model)}</span></div>
      <div class="row"><span class="k">Generate</span><span class="v mono">${escapeHtml(data.generate_model)}</span></div>
      <div class="row"><span class="k">Pro</span><span class="v mono">${escapeHtml(data.pro_model || "—")}</span></div>`;

    if (data.token_budget_default) {
      syncBudgetSelect(data.token_budget_default);
    }

    const graphBadge = data.graph.exists && data.graph.nodes > 0
      ? `<span class="badge ok">${data.graph.nodes} nodes</span>`
      : `<span class="badge warn">${data.graph.note ? "placeholder" : "empty"}</span>`;

    const azureRepos = data.azure_repos || [];
    $("#integrationStats").innerHTML = `
      <div class="integ-item"><span class="name">GitHub</span>${data.github_repos.length ? `<span class="badge ok">${data.github_repos.length} repos</span>` : '<span class="badge warn">none</span>'}</div>
      <div class="integ-item"><span class="name">Outline</span>${data.outline_configured ? '<span class="badge ok">connected</span>' : '<span class="badge warn">not set</span>'}</div>
      <div class="integ-item"><span class="name">Confluence</span>${data.confluence_configured ? '<span class="badge ok">connected</span>' : '<span class="badge warn">not set</span>'}</div>
      <div class="integ-item"><span class="name">Azure</span>${data.azure_configured ? `<span class="badge ok">${azureRepos.length || "connected"}${azureRepos.length ? " repos" : ""}</span>` : '<span class="badge warn">not set</span>'}</div>
      <div class="integ-item"><span class="name">OpenAPI</span>${data.openapi_configured ? `<span class="badge ok">${(data.openapi_specs || []).length || "connected"}${(data.openapi_specs || []).length ? " specs" : ""}</span>` : '<span class="badge warn">not set</span>'}</div>
      <div class="integ-item"><span class="name">Graph</span>${graphBadge}</div>`;

    renderConnectorBadges(data);
  }

  const CONNECTOR_STATE = {
    outline: (d) => d.outline_configured,
    confluence: (d) => d.confluence_configured,
    github: (d) => (d.github_repos || []).length > 0,
    azure: (d) => d.azure_configured,
    openapi: (d) => d.openapi_configured,
  };

  function renderConnectorBadges(data) {
    Object.entries(CONNECTOR_STATE).forEach(([conn, isOn]) => {
      const badge = document.querySelector(`[data-conn-status="${conn}"]`);
      if (!badge) return;
      const on = !!isOn(data);
      badge.className = "connector-status " + (on ? "ok" : "off");
      badge.textContent = on ? "متصل" : "تنظیم نشده";
    });
  }

  function syncBudgetSelect(budget) {
    const sel = $("#budgetInput");
    if (!sel) return;
    const value = String(budget);
    let opt = Array.from(sel.options).find((o) => o.value === value);
    if (!opt) {
      opt = document.createElement("option");
      opt.value = value;
      opt.textContent = value;
      sel.appendChild(opt);
    }
    sel.value = value;
  }

  // ── Runs / history ──────────────────────────────────────────────────

  async function loadRuns() {
    try {
      const data = await api("/api/runs");
      allRuns = data.runs || [];
      renderRuns();
    } catch (_) { /* ignore */ }
  }

  function renderRuns() {
    const list = $("#runList");
    const q = ($("#runSearch").value || "").trim().toLowerCase();
    const runs = q ? allRuns.filter((r) => (r.query || "").toLowerCase().includes(q)) : allRuns;

    if (!runs.length) {
      list.innerHTML = `
        <div class="rail-empty">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><path d="M14 2v6h6"/></svg>
          <p>${q ? "نتیجه‌ای یافت نشد" : "هنوز تستی تولید نشده"}</p>
        </div>`;
      return;
    }

    list.innerHTML = runs.map((r) => `
      <button class="run-item${currentRunId === r.id ? " active" : ""}" data-id="${escapeHtml(r.id)}">
        <div class="run-item-query">${escapeHtml(r.query)}</div>
        <div class="run-item-meta">
          <span>${escapeHtml(r.timestamp || "—")}</span>
          <span class="dot"></span>
          <span>${r.sources_count} source</span>
        </div>
      </button>`).join("");

    list.querySelectorAll(".run-item").forEach((btn) => {
      btn.addEventListener("click", () => loadRun(btn.dataset.id));
    });
  }

  async function loadRun(runId) {
    try {
      const data = await api(`/api/runs/${encodeURIComponent(runId)}`);
      currentRunId = runId;
      displayResult({
        gherkin: data.gherkin,
        meta: data.meta,
        cost: data.cost,
        validation_errors: data.validation_errors,
        quality_warnings: data.quality_warnings || data.meta?.quality_warnings || [],
        coverage: data.coverage || data.meta?.coverage || {},
        write_blocked: data.meta?.blocked || false,
        blocked_output: data.meta?.blocked_output || "",
        feature_path: data.id ? `${data.id}.feature` : null,
      });
      renderRuns();
      switchToTab("gherkin");
      closeRail();
    } catch (e) {
      toast(e.message, "error");
    }
  }

  // ── Display result ─────────────────────────────────────────────────

  function displayResult(result) {
    currentGherkin = result.gherkin || "";
    renderGherkin(currentGherkin);

    const errors = result.validation_errors || [];
    const qualityWarnings = result.quality_warnings || result.meta?.quality_warnings || [];
    const coverage = result.coverage || result.meta?.coverage || {};
    const writeBlocked = Boolean(result.write_blocked);
    const blockedOutput = result.blocked_output || "";
    const ribbon = $("#validationRibbon");

    const setRibbon = (cls, text) => {
      ribbon.className = "validation-ribbon " + cls;
      ribbon.innerHTML = (cls === "ok"
        ? '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 11.08V12a10 10 0 11-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>'
        : '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>') +
        `<span></span>`;
      ribbon.querySelector("span").textContent = text;
      ribbon.classList.remove("hidden");
    };

    if (writeBlocked && !result.feature_path) {
      setRibbon("error", blockedOutput ? blockedOutput.split("\n")[0] : "تولید block شد — validation ناموفق بود");
    } else if (errors.length) {
      setRibbon("error", "خطای نحوی: " + errors.join("، "));
    } else if (qualityWarnings.length) {
      setRibbon("warn", `نحو معتبر · ${qualityWarnings.length} هشدار کیفیت`);
    } else if (currentGherkin && !currentGherkin.startsWith("خروجی")) {
      setRibbon("ok", "نحو Gherkin معتبر است");
    } else {
      ribbon.classList.add("hidden");
    }

    updateTabBadge(qualityWarnings, coverage, writeBlocked);
    renderCoverage(coverage, qualityWarnings, writeBlocked, blockedOutput);
    renderSources(result.meta?.sources || []);
    renderCost(result.cost || {});
  }

  function updateTabBadge(qualityWarnings, coverage, writeBlocked = false) {
    const badge = $("#gherkinBadge");
    const total = (coverage.missing || []).length + qualityWarnings.length + (writeBlocked ? 1 : 0);
    if (total > 0) {
      badge.textContent = String(total);
      badge.classList.remove("hidden");
    } else {
      badge.classList.add("hidden");
    }
  }

  // ── Coverage ────────────────────────────────────────────────────────

  function renderCoverage(coverage, qualityWarnings, writeBlocked = false, blockedOutput = "") {
    const summary = $("#coverageSummary");
    const missingSection = $("#covMissingSection");
    const wipSection = $("#covWipSection");
    const qualitySection = $("#covQualitySection");
    const total = coverage.checklist_total || 0;
    const covered = coverage.covered || 0;
    const missing = coverage.missing || [];
    const wip = coverage.wip || [];
    const pct = total ? Math.round((covered / total) * 100) : 0;

    if (writeBlocked && blockedOutput) {
      summary.innerHTML = `<div class="cov-blocked">${escapeHtml(blockedOutput).replace(/\n/g, "<br>")}</div>`;
    } else if (!total && !qualityWarnings.length) {
      summary.innerHTML = '<p class="empty-msg">پوشش acceptance criteria پس از تولید تست اینجا نمایش داده می‌شود.</p>';
    } else if (total) {
      summary.innerHTML = `
        <div class="cov-hero">
          <div class="cov-ring" style="--pct:${pct}"><span>${pct}%</span></div>
          <div class="cov-hero-info">
            <h4>${covered} از ${total} معیار پوشش داده شد</h4>
            <p>acceptance criteria تگ‌گذاری‌شده در این اجرا</p>
          </div>
        </div>`;
    } else {
      summary.innerHTML = '<p class="empty-msg">هیچ acceptance checklist به این اجرا پیوست نشده بود.</p>';
    }

    if (missing.length) {
      missingSection.classList.remove("hidden");
      $("#covMissingCount").textContent = missing.length;
      $("#covMissingList").innerHTML = missing
        .map((id) => `<button type="button" class="cov-chip missing" data-ac-id="${escapeHtml(id)}">@acceptance-${escapeHtml(id)}</button>`)
        .join("");
      $("#covMissingList").querySelectorAll(".cov-chip").forEach((chip) => {
        chip.addEventListener("click", () => scrollToTag(chip.dataset.acId));
      });
    } else {
      missingSection.classList.add("hidden");
    }

    if (wip.length) {
      wipSection.classList.remove("hidden");
      $("#covWipCount").textContent = wip.length;
      $("#covWipList").innerHTML = wip.map((id) => `<span class="cov-chip wip">@wip-${escapeHtml(id)}</span>`).join("");
    } else {
      wipSection.classList.add("hidden");
    }

    if (qualityWarnings.length) {
      qualitySection.classList.remove("hidden");
      $("#covQualityCount").textContent = qualityWarnings.length;
      $("#covQualityList").innerHTML = qualityWarnings.map((w) => `<li>${escapeHtml(w)}</li>`).join("");
    } else {
      qualitySection.classList.add("hidden");
    }
  }

  function scrollToTag(acId) {
    switchToTab("gherkin");
    const body = $("#gherkinBody");
    const text = body.textContent || "";
    const tag = `@acceptance-${acId}`;
    if (text.indexOf(tag) === -1) {
      toast(`تگ ${tag} در Gherkin پیدا نشد`, "error");
      return;
    }
    const lineNumber = text.slice(0, text.indexOf(tag)).split("\n").length - 1;
    $("#gherkinWrap").scrollTop = Math.max(0, lineNumber * 24 - 60);
  }

  // ── Sources ─────────────────────────────────────────────────────────

  function renderSources(sources) {
    const grid = $("#sourcesGrid");
    if (!sources.length) {
      grid.innerHTML = '<p class="empty-msg">منابع استفاده‌شده (مستندات Outline و PRها) اینجا نمایش داده می‌شوند.</p>';
      return;
    }
    grid.innerHTML = sources.map((s) => `
      <div class="source-card">
        <span class="source-kind">${escapeHtml(s.kind || "source")}</span>
        <div class="source-title">${escapeHtml(s.title || s.id || "—")}</div>
        ${s.url ? `<a class="source-link" href="${escapeHtml(s.url)}" target="_blank" rel="noopener">${escapeHtml(s.url)}</a>` : ""}
      </div>`).join("");
  }

  // ── Cost ─────────────────────────────────────────────────────────────

  function renderCost(cost) {
    $("#costUsd").textContent = "$" + (cost.estimated_usd ?? 0).toFixed(4);
    $("#costLlmIn").textContent = formatNum(cost.llm_input_tokens);
    $("#costLlmOut").textContent = formatNum(cost.llm_output_tokens);
    $("#costGraphify").textContent = formatNum(cost.graphify_query_tokens);
  }

  function formatNum(n) {
    if (n == null) return "0";
    return Number(n).toLocaleString("en-US");
  }

  // ── Activity feed ────────────────────────────────────────────────────

  const RUNNING_DOTS = '<span class="act-dots"><i></i><i></i><i></i></span>';
  const PULSE_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M22 12h-4l-3 9L9 3l-3 9H2"/></svg>';

  function activityId(a, index) {
    return a.id || a.tool || ("act-" + index);
  }

  function activityItemHtml(a, index) {
    const cat = a.category || "tool";
    const icon = ICONS[a.icon] || ICONS.tool;
    const status = a.status || "done";
    const state = status === "running"
      ? `<span class="act-state running">${RUNNING_DOTS}<span>در حال اجرا</span></span>`
      : `<span class="act-state done" title="انجام شد">${CHECK}</span>`;
    const catLabel = {
      outline: "مستندات",
      github: "کد",
      confluence: "Confluence",
      azure: "Azure",
      openapi: "OpenAPI",
      graph: "گراف",
      agent: "ایجنت",
      plan: "برنامه",
      output: "خروجی",
      index: "ایندکس",
      tool: "ابزار",
    }[cat] || "";
    return `
      <div class="act ${status}" data-id="${escapeHtml(activityId(a, index))}">
        <div class="act-rail"><div class="act-node ${escapeHtml(cat)}">${icon}</div></div>
        <div class="act-card">
          <div class="act-card-top">
            <span class="act-title">${escapeHtml(a.title || a.tool || "Activity")}</span>
            ${catLabel ? `<span class="act-cat">${escapeHtml(catLabel)}</span>` : ""}
            ${state}
          </div>
          ${a.detail ? `<div class="act-detail">${escapeHtml(a.detail)}</div>` : ""}
          ${a.result_preview && status === "done" ? `<div class="act-result">${escapeHtml(a.result_preview)}</div>` : ""}
        </div>
      </div>`;
  }

  function activityEmptyHtml(msg) {
    return `
      <div class="act-empty">
        <div class="act-empty-ic">${PULSE_ICON}</div>
        <p>${escapeHtml(msg)}</p>
      </div>`;
  }

  // Incremental render: append new steps and patch changed ones so items
  // animate in one-by-one instead of the whole list re-rendering each poll.
  function renderActivities(activities) {
    if (!activities || !activities.length) return;
    const log = $("#activityLog");
    let timeline = log.querySelector(".act-timeline");
    if (!timeline) {
      log.innerHTML = '<div class="act-timeline"></div>';
      timeline = log.querySelector(".act-timeline");
    }
    const existing = new Map();
    timeline.querySelectorAll(".act").forEach((el) => existing.set(el.dataset.id, el));

    activities.forEach((a, i) => {
      const id = activityId(a, i);
      const el = existing.get(id);
      if (el) {
        const fresh = document.createRange().createContextualFragment(activityItemHtml(a, i)).firstElementChild;
        if (el.outerHTML !== fresh.outerHTML) el.replaceWith(fresh);
        existing.delete(id);
      } else {
        timeline.insertAdjacentHTML("beforeend", activityItemHtml(a, i));
      }
    });
    existing.forEach((el) => el.remove());
    log.scrollTop = log.scrollHeight;
  }

  function clearActivities(idleMessage = "Agent در حال کار است…") {
    lastActivitySig = "";
    $("#activityLog").innerHTML = activityEmptyHtml(idleMessage);
  }

  function displayIndexResult(result) {
    if (!result) return;
    const req = result.requested || {};
    const lines = [];
    if (req.outline_sync || req.confluence_sync || req.openapi_sync) {
      lines.push(`اسناد همگام‌شده: ${result.docs_synced ?? 0}`);
    } else {
      lines.push("اسناد: تیک همگام‌سازی زده نشده بود");
    }
    if (req.github_clone || req.azure_clone) {
      const n = (result.code_dirs || []).length;
      const azureN = (result.azure_code_dirs || []).length;
      lines.push(`پوشه‌های کد: ${n}` + (req.azure_clone ? ` (Azure: ${azureN})` : ""));
    } else {
      lines.push("کد: تیک Clone/pull زده نشده بود");
    }
    lines.push(`گراف ساخته شد: ${result.graph_built ? "بله" : "خیر"}`);
    if (result.docs_indexed != null) {
      lines.push(`ایندکس بازیابی: ${result.docs_indexed} سند`);
    }
    const summary = lines.join("\n");
    const el = $("#indexResult");
    if (el) {
      el.classList.remove("hidden");
      el.textContent = summary;
    }
    // Append a completion card — don't wipe the live activity timeline.
    const log = $("#activityLog");
    let timeline = log.querySelector(".act-timeline");
    if (!timeline) {
      log.innerHTML = '<div class="act-timeline"></div>';
      timeline = log.querySelector(".act-timeline");
    }
    timeline.insertAdjacentHTML("beforeend", `
      <div class="act done">
        <div class="act-rail"><div class="act-node index">${ICONS.graph}</div></div>
        <div class="act-card">
          <div class="act-card-top">
            <span class="act-title">ایندکس کامل شد</span>
            <span class="act-cat">ایندکس</span>
            <span class="act-state done" title="انجام شد">${CHECK}</span>
          </div>
          <div class="act-result">${escapeHtml(summary)}</div>
        </div>
      </div>`);
    log.scrollTop = log.scrollHeight;
  }

  // ── Tabs ─────────────────────────────────────────────────────────────

  function switchToTab(name) {
    $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
    $$(".panel").forEach((p) => p.classList.toggle("active", p.id === "panel-" + name));
  }

  function initTabs() {
    $$(".tab").forEach((tab) => {
      tab.addEventListener("click", () => switchToTab(tab.dataset.tab));
    });
  }

  // ── Live progress ─────────────────────────────────────────────────────

  function showPartialGherkin(gherkin) {
    if (!gherkin) return;
    currentGherkin = gherkin;
    renderGherkin(gherkin);
    const ribbon = $("#validationRibbon");
    ribbon.className = "validation-ribbon ok";
    ribbon.innerHTML = '<span class="spinner"></span><span>پیش‌نمایش زنده — در حال تکمیل…</span>';
    ribbon.classList.remove("hidden");
  }

  function updateProgress(job) {
    $("#progressPhase").textContent = phaseLabel(job.phase);
    $("#progressPct").textContent = job.progress + "%";
    $("#progressBar").style.width = job.progress + "%";
    $("#progressMessage").textContent =
      (job.status === "failed" && job.error) ? job.error : (job.message || "");

    const activities = job.activities || [];
    const sig = JSON.stringify(activities);
    if (sig !== lastActivitySig) {
      lastActivitySig = sig;
      renderActivities(activities);
    }
    if (job.partial_gherkin) showPartialGherkin(job.partial_gherkin);
  }

  function phaseLabel(phase) {
    const labels = {
      queued: "در صف",
      init: "آماده‌سازی",
      triage: "ارزیابی پیچیدگی",
      planning: "برنامه‌ریزی",
      delegating: "واگذاری به متخصص",
      research: "تحقیق",
      graph: "گراف دانش",
      writing: "نوشتن Gherkin",
      generate: "تولید",
      running: "در حال اجرا",
      template: "قالب آزمایشی",
      indexing: "ایندکس",
      done: "تمام",
      error: "خطا",
    };
    return labels[phase] || phase || "…";
  }

  // ── Generate ───────────────────────────────────────────────────────────

  async function startGenerate() {
    const query = $("#queryInput").value.trim();
    if (!query) {
      toast("لطفاً درخواست QA را وارد کنید", "error");
      $("#queryInput").focus();
      return;
    }

    const budget = parseInt($("#budgetInput").value, 10) || 1500;
    const dryRun = $("#dryRunToggle").checked;

    $("#generateBtn").disabled = true;
    $("#progressCard").classList.remove("hidden");
    activeJobKind = "generate";
    clearActivities();
    switchToTab("activity");

    try {
      const { job_id } = await api("/api/generate", {
        method: "POST",
        body: JSON.stringify({ query, budget, dry_run: dryRun }),
      });
      watchJob(job_id);
    } catch (e) {
      toast(e.message, "error");
      $("#progressCard").classList.add("hidden");
      $("#generateBtn").disabled = false;
    }
  }

  function finishJob() {
    $("#generateBtn").disabled = false;
    $("#indexStart").disabled = false;
    $("#indexStart").textContent = "شروع ایندکس";
    setTimeout(() => $("#progressCard").classList.add("hidden"), 900);
  }

  function watchJob(jobId, kind) {
    if (kind) activeJobKind = kind;
    if (activeEventSource) activeEventSource.close();
    activeEventSource = new EventSource(`/api/jobs/${jobId}/stream`);

    activeEventSource.onmessage = (event) => {
      const job = JSON.parse(event.data);
      if (job.kind) activeJobKind = job.kind;
      updateProgress(job);

      if (job.status === "completed") {
        activeEventSource.close();
        activeEventSource = null;
        finishJob();
        handleCompletion(job);
      } else if (job.status === "failed") {
        activeEventSource.close();
        activeEventSource = null;
        finishJob();
        toast(job.error || (job.kind === "index" ? "ایندکس ناموفق بود" : "تولید ناموفق بود"), "error");
      }
    };

    activeEventSource.onerror = () => {
      if (activeEventSource) activeEventSource.close();
      activeEventSource = null;
      pollJob(jobId);
    };
  }

  function handleCompletion(job) {
    if (job.kind === "index") {
      displayIndexResult(job.result);
      switchToTab("activity");
      toast("ایندکس کامل شد");
      loadStatus();
      return;
    }
    if (job.result) {
      displayResult({ ...job.result, feature_path: job.result.feature_path });
      if (job.result.write_blocked && !job.result.feature_path) {
        switchToTab("coverage");
        toast("تولید block شد — خطاهای validation را ببین", "error");
      } else {
        switchToTab("gherkin");
        toast("تست‌کیس با موفقیت تولید شد");
      }
      loadRuns();
    }
  }

  async function pollJob(jobId) {
    try {
      const job = await api(`/api/jobs/${jobId}`);
      if (job.kind) activeJobKind = job.kind;
      updateProgress(job);
      if (job.status === "running" || job.status === "queued") {
        setTimeout(() => pollJob(jobId), 800);
      } else if (job.status === "completed") {
        finishJob();
        handleCompletion(job);
      } else if (job.status === "failed") {
        finishJob();
        toast(job.error || (job.kind === "index" ? "ایندکس ناموفق بود" : "تولید ناموفق بود"), "error");
      }
    } catch (_) {
      finishJob();
    }
  }

  // ── Index modal ──────────────────────────────────────────────────────────

  function openIndexModal() {
    $("#indexModal").classList.remove("hidden");
    $("#indexResult").classList.add("hidden");
  }
  function closeIndexModal() { $("#indexModal").classList.add("hidden"); }

  async function startIndex() {
    const btn = $("#indexStart");
    btn.disabled = true;
    btn.textContent = "در حال اجرا…";
    closeIndexModal();

    $("#progressCard").classList.remove("hidden");
    activeJobKind = "index";
    clearActivities("ایندکس در حال اجراست…");
    switchToTab("activity");

    try {
      const { job_id } = await api("/api/index", {
        method: "POST",
        body: JSON.stringify({
          outline_sync: $("#idxOutline").checked,
          github_clone: $("#idxGithub").checked,
          with_graph: $("#idxGraph").checked,
          confluence_sync: $("#idxConfluence").checked,
          azure_clone: $("#idxAzure").checked,
          openapi_sync: $("#idxOpenapi").checked,
        }),
      });
      watchJob(job_id, "index");
    } catch (e) {
      toast(e.message, "error");
      $("#progressCard").classList.add("hidden");
      btn.disabled = false;
      btn.textContent = "شروع ایندکس";
    }
  }

  // ── Copy / Download / New ──────────────────────────────────────────────────

  function copyGherkin() {
    if (!currentGherkin) return toast("چیزی برای کپی نیست", "error");
    navigator.clipboard.writeText(currentGherkin).then(
      () => toast("در کلیپ‌بورد کپی شد"),
      () => toast("کپی ناموفق بود", "error")
    );
  }

  function downloadGherkin() {
    if (!currentGherkin) return toast("چیزی برای دانلود نیست", "error");
    const blob = new Blob([currentGherkin], { type: "text/plain;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = (currentRunId || "feature") + ".feature";
    a.click();
    URL.revokeObjectURL(a.href);
  }

  function newTest() {
    currentRunId = null;
    currentGherkin = "";
    $("#queryInput").value = "";
    renderGherkin("");
    $("#validationRibbon").classList.add("hidden");
    $("#gherkinBadge").classList.add("hidden");
    switchToTab("gherkin");
    renderRuns();
    closeRail();
    $("#queryInput").focus();
  }

  // ── Settings modal ──────────────────────────────────────────────────────────

  const SETTINGS_MAP = {
    set_llm_base_url: "llm_base_url",
    set_llm_api_key: "llm_api_key",
    set_research_base_url: "qa_agent_research_base_url",
    set_generate_base_url: "qa_agent_generate_base_url",
    set_outline_base_url: "outline_base_url",
    set_outline_api_key: "outline_api_key",
    set_outline_subagent: "qa_agent_outline_subagent",
    set_github_repos: "github_repos",
    set_github_base_url: "github_base_url",
    set_github_token: "github_token",
    set_github_subagent: "qa_agent_github_subagent",
    set_confluence_base_url: "confluence_base_url",
    set_confluence_email: "confluence_email",
    set_confluence_api_token: "confluence_api_token",
    set_confluence_space: "confluence_space",
    set_confluence_auth_mode: "confluence_auth_mode",
    set_confluence_subagent: "qa_agent_confluence_subagent",
    set_azure_org: "azure_devops_org",
    set_azure_project: "azure_devops_project",
    set_azure_repos: "azure_devops_repos",
    set_azure_wiki: "azure_devops_wiki",
    set_azure_base_url: "azure_devops_base_url",
    set_azure_api_version: "azure_devops_api_version",
    set_azure_pat: "azure_devops_pat",
    set_azure_subagent: "qa_agent_azure_subagent",
    set_openapi_specs: "openapi_specs",
    set_openapi_token: "openapi_token",
    set_openapi_subagent: "qa_agent_openapi_subagent",
    set_verify_tls: "qa_agent_verify_tls",
    set_nano_model: "qa_agent_nano_model",
    set_research_model: "qa_agent_research_model",
    set_generate_model: "qa_agent_generate_model",
    set_pro_model: "qa_agent_pro_model",
    set_embedding_model: "qa_agent_embedding_model",
    set_embedding_base_url: "qa_agent_embedding_base_url",
    set_embedding_api_key: "qa_agent_embedding_api_key",
    set_query_expansion: "qa_agent_query_expansion",
    set_triage: "qa_agent_triage",
    set_escalation: "qa_agent_escalation",
    set_max_write_attempts: "qa_agent_max_write_attempts",
    set_max_reprompts: "qa_agent_max_reprompts",
    set_quality_refine_rounds: "qa_agent_quality_refine_rounds",
    set_token_budget: "qa_agent_token_budget",
    set_max_docs: "qa_agent_max_docs",
    set_max_prs_deep: "qa_agent_max_prs_deep",
    set_max_prs: "qa_agent_max_prs",
    set_pr_scan_limit: "qa_agent_pr_scan_limit",
    set_max_diff_lines: "qa_agent_max_diff_lines",
  };

  const SETTINGS_BOOL = new Set([
    "qa_agent_query_expansion",
    "qa_agent_triage",
    "qa_agent_escalation",
    "qa_agent_outline_subagent",
    "qa_agent_github_subagent",
    "qa_agent_confluence_subagent",
    "qa_agent_azure_subagent",
    "qa_agent_openapi_subagent",
    "qa_agent_verify_tls",
  ]);

  const SETTINGS_SECRET = new Set([
    "llm_api_key",
    "outline_api_key",
    "github_token",
    "qa_agent_embedding_api_key",
    "confluence_api_token",
    "azure_devops_pat",
    "openapi_token",
  ]);

  function openSettingsModal() {
    $("#settingsModal").classList.remove("hidden");
    loadSettingsForm();
  }

  function closeSettingsModal() {
    $("#settingsModal").classList.add("hidden");
  }

  function updateSecretHint(elId, configured) {
    const hint = $("#" + elId + "_hint");
    if (!hint) return;
    if (elId === "set_llm_api_key" || elId === "set_embedding_api_key") {
      hint.textContent = configured
        ? "تنظیم‌شده — خالی = بدون تغییر · پاک‌کردن از فایل تنظیمات"
        : "خالی = مدل لوکال (بدون کلید)";
      return;
    }
    hint.textContent = configured
      ? "تنظیم‌شده — خالی = بدون تغییر"
      : "هنوز تنظیم نشده";
  }

  async function loadSettingsForm() {
    try {
      const data = await api("/api/settings");
      const values = data.values || {};
      Object.entries(SETTINGS_MAP).forEach(([elId, key]) => {
        const el = $("#" + elId);
        if (!el) return;
        if (SETTINGS_BOOL.has(key)) {
          el.checked = !!values[key];
        } else if (SETTINGS_SECRET.has(key)) {
          el.value = "";
          updateSecretHint(elId, !!values[key + "_set"]);
        } else {
          el.value = values[key] == null ? "" : String(values[key]);
        }
      });
      syncAllConnectorCards();
      if (lastStatus) renderConnectorBadges(lastStatus);
    } catch (e) {
      toast(e.message || "بارگذاری تنظیمات ناموفق بود", "error");
    }
  }

  // ── Connector connection test ────────────────────────────────────────────

  const CONNECTOR_LABELS = {
    outline: "Outline",
    confluence: "Confluence",
    github: "GitHub",
    azure: "Azure DevOps",
    openapi: "OpenAPI",
  };

  function setConnectorResult(name, state, message, detail) {
    const card = document.querySelector(`[data-conn-card="${name}"]`);
    if (!card) return;
    const config = card.querySelector(".connector-config");
    let box = card.querySelector(".connector-test-result");
    if (!box) {
      box = document.createElement("div");
      box.className = "connector-test-result";
      const bar = config && config.querySelector(".connector-config-bar");
      if (bar) bar.insertAdjacentElement("afterend", box);
      else if (config) config.prepend(box);
      else card.appendChild(box);
    }
    box.className = "connector-test-result " + state;
    const icon = state === "ok"
      ? '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"><polyline points="20 6 9 17 4 12"/></svg>'
      : state === "loading"
        ? '<span class="spinner"></span>'
        : '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>';
    box.innerHTML = `${icon}<div class="ctest-text"><b></b>${detail ? '<small></small>' : ''}</div>`;
    box.querySelector("b").textContent = message;
    if (detail) box.querySelector("small").textContent = detail;

    const badge = document.querySelector(`[data-conn-status="${name}"]`);
    if (badge && state !== "loading") {
      badge.className = "connector-status " + (state === "ok" ? "ok" : "off");
      badge.textContent = state === "ok" ? "متصل" : "ناموفق";
    }
  }

  function syncConnectorCard(card) {
    if (!card) return;
    const toggle = card.querySelector("[data-conn-enable]");
    const config = card.querySelector(".connector-config");
    const label = card.querySelector(".connector-enable-label");
    const on = !!(toggle && toggle.checked);
    card.classList.toggle("is-on", on);
    if (config) {
      config.hidden = !on;
      config.setAttribute("aria-hidden", on ? "false" : "true");
    }
    if (label) {
      label.textContent = on
        ? (label.dataset.on || "فعال")
        : (label.dataset.off || "غیرفعال");
    }
  }

  function syncAllConnectorCards() {
    $$("[data-conn-card]").forEach(syncConnectorCard);
  }

  async function testConnector(name, btn) {
    if (btn) btn.disabled = true;
    setConnectorResult(name, "loading", "در حال بررسی اتصال…");
    try {
      const res = await api(`/api/connectors/${name}/test`, {
        method: "POST",
        body: JSON.stringify({ values: collectSettingsForm() }),
      });
      const label = CONNECTOR_LABELS[name] || name;
      if (res.ok) {
        setConnectorResult(name, "ok", res.message || "اتصال برقرار شد", res.detail || "");
        toast(`${label}: اتصال برقرار شد`);
      } else {
        const state = res.configured === false ? "warn" : "error";
        setConnectorResult(name, state, res.message || "اتصال ناموفق بود", res.detail || "");
        toast(`${label}: ${res.message || "اتصال ناموفق بود"}`, "error");
      }
    } catch (e) {
      setConnectorResult(name, "error", e.message || "تست اتصال ناموفق بود");
      toast(e.message || "تست اتصال ناموفق بود", "error");
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  function initConnectorMarketplace() {
    $$("[data-conn-enable]").forEach((toggle) => {
      toggle.addEventListener("change", () => {
        syncConnectorCard(toggle.closest("[data-conn-card]"));
      });
    });
    $$(".connector-test").forEach((btn) => {
      btn.addEventListener("click", () => testConnector(btn.dataset.connTest, btn));
    });
    syncAllConnectorCards();
  }

  function initSettingsTabs() {
    $$(".settings-tab").forEach((tab) => {
      tab.addEventListener("click", () => {
        const name = tab.dataset.stab;
        $$(".settings-tab").forEach((t) => t.classList.toggle("active", t === tab));
        $$(".settings-tabpanel").forEach((p) =>
          p.classList.toggle("active", p.dataset.spanel === name)
        );
      });
    });
  }

  function collectSettingsForm() {
    const values = {};
    Object.entries(SETTINGS_MAP).forEach(([elId, key]) => {
      const el = $("#" + elId);
      if (!el) return;
      if (SETTINGS_BOOL.has(key)) {
        values[key] = el.checked;
      } else if (SETTINGS_SECRET.has(key)) {
        const typed = el.value.trim();
        // Blank secret = keep current; only send when the user typed a new key.
        if (typed) values[key] = typed;
      } else if (el.type === "number") {
        const n = parseInt(el.value, 10);
        if (!Number.isNaN(n)) values[key] = n;
      } else {
        values[key] = el.value.trim();
      }
    });
    return values;
  }

  async function saveSettingsForm() {
    const btn = $("#settingsSave");
    btn.disabled = true;
    try {
      await api("/api/settings", {
        method: "PUT",
        body: JSON.stringify({ values: collectSettingsForm() }),
      });
      toast("تنظیمات ذخیره شد");
      closeSettingsModal();
      await loadStatus();
    } catch (e) {
      toast(e.message || "ذخیره تنظیمات ناموفق بود", "error");
    } finally {
      btn.disabled = false;
    }
  }

  // ── Rail (mobile) ────────────────────────────────────────────────────────────

  function openRail() { $("#rail").classList.add("open"); $("#scrim").classList.add("open"); }
  function closeRail() { $("#rail").classList.remove("open"); $("#scrim").classList.remove("open"); }

  // ── Init ──────────────────────────────────────────────────────────────────────

  function init() {
    initTheme();
    initTabs();
    initSettingsTabs();
    initConnectorMarketplace();
    loadStatus();
    loadRuns();
    renderGherkin("");

    $("#generateBtn").addEventListener("click", startGenerate);
    $("#queryInput").addEventListener("keydown", (e) => {
      if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) startGenerate();
    });
    $("#queryInput").addEventListener("input", (e) => {
      e.target.style.height = "auto";
      e.target.style.height = Math.min(e.target.scrollHeight, 200) + "px";
    });

    $("#copyBtn").addEventListener("click", copyGherkin);
    $("#downloadBtn").addEventListener("click", downloadGherkin);
    $("#newBtn").addEventListener("click", newTest);
    $("#runSearch").addEventListener("input", renderRuns);

    $("#refreshBtn").addEventListener("click", () => {
      loadStatus();
      loadRuns();
      toast("بازخوانی شد");
    });

    $("#indexBtn").addEventListener("click", openIndexModal);
    $("#indexModalClose").addEventListener("click", closeIndexModal);
    $("#indexCancel").addEventListener("click", closeIndexModal);
    $("#indexStart").addEventListener("click", startIndex);
    $("#indexModal").addEventListener("click", (e) => {
      if (e.target === $("#indexModal")) closeIndexModal();
    });

    $("#settingsBtn").addEventListener("click", openSettingsModal);
    $("#settingsModalClose").addEventListener("click", closeSettingsModal);
    $("#settingsCancel").addEventListener("click", closeSettingsModal);
    $("#settingsSave").addEventListener("click", saveSettingsForm);
    $("#settingsModal").addEventListener("click", (e) => {
      if (e.target === $("#settingsModal")) closeSettingsModal();
    });

    $("#menuBtn").addEventListener("click", openRail);
    $("#scrim").addEventListener("click", closeRail);

    $$(".chip").forEach((btn) => {
      btn.addEventListener("click", () => {
        $("#queryInput").value = btn.dataset.prompt;
        $("#queryInput").focus();
        $("#queryInput").dispatchEvent(new Event("input"));
        closeRail();
      });
    });

    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        closeIndexModal();
        closeSettingsModal();
        closeRail();
      }
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
