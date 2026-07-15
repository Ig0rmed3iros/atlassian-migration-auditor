/* ================================================================
   Atlassian Audit Platform — analysis renderer (Dark Console design)
   Data contract is identical to the previous renderer; only the
   emitted HTML changed. Server pagination/filtering still apply so a
   40k-issue run never ships to the browser at once.

   ONLY real fields from /api/runs/{id}/summary, /projects, /issues,
   /config, /events are mapped. Donut segments are computed from the
   summary's issue counts; if a needed count is absent the viz is
   omitted gracefully — no data is invented.
   ================================================================ */
const Analysis = (() => {
  let R, V, P, SRC, TGT, SRCDEP, TGTDEP;

  /* product vocabulary — server stats keys stay issue-named; the UI relabels
     at render time from the page's data-product attribute. */
  const VOCAB = {
    jira:       {container: 'Project', containers: 'Projects', item: 'issue', items: 'issues', itemTitle: 'Issue'},
    confluence: {container: 'Space',   containers: 'Spaces',   item: 'page',  items: 'pages',  itemTitle: 'Page'},
  };
  let vocab = VOCAB.jira;   // reassigned at boot from data-product
  let PRODUCT = "jira";

  const $ = (s) => document.querySelector(s);
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const get = async (p) => (await fetch(p)).json();
  const num = (n) => Number(n ?? 0).toLocaleString("en-US");
  /* per-product item link: jira issues live at {base}/browse/{key};
     confluence pages at {base}[/wiki]/display/{SPACE}/{title} — the /wiki
     context path is a Cloud-only convention, so each side's deployment
     rides along with its base URL. */
  const itemLink = (key, base, dep, container) => {
    if (!key || !base)
      return `<span class="mono muted">${esc(key ?? "—")}</span>`;
    const href = PRODUCT === "confluence"
      ? `${base}${dep === "dc" ? "" : "/wiki"}/display/${esc(container ?? "")}/${encodeURIComponent(key)}`
      : `${base}/browse/${esc(key)}`;
    return `<a class="mono" href="${href}" target="_blank">${esc(key)}</a>`;
  };
  const kb = (k) => `<span class="kbadge k-${esc(k)}">${esc(k)}</span>`;

  /* Render a finding's detail object as readable "key: value" pairs instead of
     a raw JSON.stringify dump (which is unreadable to a non-developer admin). */
  function fmtDetail(d, cap = 200) {
    if (d == null || typeof d !== "object") return esc(String(d ?? ""));
    const parts = Object.entries(d).map(([k, v]) => {
      const val = (v && typeof v === "object") ? JSON.stringify(v) : String(v);
      return `<b>${esc(k)}</b> ${esc(val)}`;
    });
    const s = parts.join("; ");
    return s.length > cap ? s.slice(0, cap) + "…" : s;
  }

  /* --- inline SVG glyphs (match mockup #s-analysis) --- */
  const SVG = {
    // warning triangle — gaps / critical
    triangle:
      '<svg width="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">' +
      '<path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z"/>' +
      '<path d="M12 9v4m0 4h.01"/></svg>',
    // check — clean
    check:
      '<svg width="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4">' +
      '<circle cx="12" cy="12" r="9"/><path d="M8 12.5l2.5 2.5L16 9"/></svg>',
    // clock — clean with tails
    clock:
      '<svg width="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">' +
      '<circle cx="12" cy="12" r="9"/><path d="M12 8v4l3 2"/></svg>',
    // finding glyphs
    fCrit:
      '<svg width="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">' +
      '<circle cx="12" cy="12" r="9"/><path d="M12 8v4m0 4h.01"/></svg>',
    fInfo:
      '<svg width="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">' +
      '<path d="M12 8v4l3 2"/><circle cx="12" cy="12" r="9"/></svg>',
    // small KPI-tile / alert glyphs (16px viewBox, match mockup)
    triSm:
      '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8">' +
      '<path d="M8 2L14 13H2L8 2z"/><path d="M8 7v2" stroke-linecap="round"/>' +
      '<circle cx="8" cy="11.5" r="0.5" fill="currentColor" stroke="none"/></svg>',
    clockSm:
      '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8">' +
      '<circle cx="8" cy="8" r="5"/><path d="M8 5v3" stroke-linecap="round"/>' +
      '<circle cx="8" cy="11" r="0.5" fill="currentColor" stroke="none"/></svg>',
    deltaSm:
      '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8">' +
      '<path d="M3 8h10M8 3v10"/></svg>',
    cfgSm:
      '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8">' +
      '<rect x="3" y="3" width="10" height="10" rx="1.5"/><path d="M6 8h4M8 6v4"/></svg>',
    collideSm:
      '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8">' +
      '<path d="M4 4l8 8M4 12l8-8"/></svg>',
    eyeSm:
      '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8">' +
      '<path d="M1 8s2.6-4.5 7-4.5S15 8 15 8s-2.6 4.5-7 4.5S1 8 1 8z"/><circle cx="8" cy="8" r="1.8"/></svg>',
  };

  /* verdict → human label, banner variant class, and banner icon */
  const VERDICT = {
    CLEAN:            { word: "CLEAN",        cls: "vb-clean", icon: SVG.check },
    CLEAN_WITH_TAILS: { word: "CLEAN · TAILS", cls: "vb-tails", icon: SVG.clock },
    GAPS_FOUND:       { word: "GAPS FOUND",   cls: "vb-gaps",  icon: SVG.triangle },
    CRITICAL:         { word: "CRITICAL",     cls: "vb-crit",  icon: SVG.triangle },
    // env-audit verdicts (auditor/envaudit/report.py::build_env_summary). The
    // severity ladder there is CRITICAL(high) > NEEDS_ATTENTION(medium/warning)
    // > HEALTHY_WITH_NOTES(low) > HEALTHY(none), so each maps to the banner
    // tone of the migration verdict at the same severity.
    NEEDS_ATTENTION:    { word: "NEEDS ATTENTION", cls: "vb-gaps",  icon: SVG.triangle },
    HEALTHY_WITH_NOTES: { word: "HEALTHY · NOTES", cls: "vb-tails", icon: SVG.clock },
    HEALTHY:            { word: "HEALTHY",         cls: "vb-clean", icon: SVG.check },
  };
  const verdictMeta = (v) =>
    VERDICT[v] || { word: esc(v || "—"), cls: "vb-tails", icon: SVG.clock };

  // a headline that names data loss / blind spots / collisions is critical
  const headlineIsCrit = (h) =>
    /genuine data loss|blind spot|collision|missing in the target/i.test(h || "");

  /* ---- KPI tile (console layout: icon + trend chip + value + sublabel) ---- */
  function kpiTile({ value, label, sub, icon, tone }) {
    // tone ∈ danger | warn | "" (neutral) ; drives tile glow + icon + value color
    const tileCls = tone === "danger" ? "kpi danger"
                  : tone === "warn"   ? "kpi warn" : "kpi";
    const iconCls = tone === "danger" ? "danger"
                  : tone === "warn"   ? "warn"
                  : tone === "cyan"   ? "cyan" : "neutral";
    const trend = tone === "danger"
        ? `<div class="kpi-trend danger">LOSS RISK</div>`
      : tone === "warn"
        ? `<div class="kpi-trend warn">REVIEW</div>`
      : tone === "expected"
        ? `<div class="kpi-trend ok">EXPECTED</div>`
        : `<div class="kpi-trend info">CHECK</div>`;
    // "expected" is a presentation-only tone for tails (neutral tile, ok chip)
    const tileFinal = tone === "expected" ? "kpi" : tileCls;
    const iconFinal = tone === "expected" ? "neutral" : iconCls;
    const numCls = tone === "danger" ? " crit" : tone === "warn" ? " warn" : "";
    return `<div class="card ${tileFinal}">
      <div class="kpi-top">
        <div class="kpi-icon ${iconFinal}">${icon}</div>
        ${trend}
      </div>
      <div class="num${numCls}">${num(value)}</div>
      <div class="lbl">${esc(label)}</div>
      <div class="kpi-sublabel">${esc(sub || "")}</div></div>`;
  }

  /* ---- headline finding card; crit headlines get red rail + optional chips ---- */
  function findingCard(headline, chips) {
    const crit = headlineIsCrit(headline);
    const icon = crit
      ? `<span style="color:var(--red-vivid);flex:0 0 auto;display:flex">${SVG.fCrit}</span>`
      : `<span style="color:var(--slate-vivid);flex:0 0 auto;display:flex">${SVG.fInfo}</span>`;
    const chipRow = (chips && chips.length)
      ? `<div class="chip-grid">${chips.map((c) =>
          `<span class="issue-chip">${esc(c)}</span>`).join("")}</div>`
      : "";
    return `<div class="finding ${crit ? "crit" : ""}">${icon}
      <div style="flex:1"><div class="ttl">${esc(headline)}</div>${chipRow}</div></div>`;
  }

  /* one project-health <tr> for the .heat table */
  function heatRow(r, linked) {
    const key = esc(r.key);
    const name = esc(r.name ?? "");
    const proj = linked
      ? `<a href="/runs/${R}/analysis/projects/${encodeURIComponent(r.key)}"><b>${key}</b></a> <span class="muted">${name}</span>`
      : `<b>${key}</b> <span class="muted">${name}</span>`;
    const holes = r.missing ?? 0;
    const tails = r.tail_count ?? 0;
    const holeCell = holes > 0
      ? `<span class="cell c-crit">${num(holes)}</span>`
      : `<span class="cell c0">0</span>`;
    const tailCell = tails > 0
      ? `<span class="cell c-warn">${num(tails)}</span>`
      : `<span class="cell c0">0</span>`;
    let fid = `<span class="muted">—</span>`;
    // Prefer the corrected CORE fidelity (systematic gaps removed) so the table
    // agrees with the headline + drill-down; fall back to the raw stored value.
    const fidVal = r.fidelity_core != null ? Number(r.fidelity_core)
                 : (r.fidelity_pct != null ? Number(r.fidelity_pct) : null);
    if (fidVal != null) {
      const color = fidVal >= 99 ? "var(--emerald-vivid)" : "var(--amber-vivid)";
      const w = Math.max(0, Math.min(100, fidVal));
      const rawv = r.fidelity_raw != null ? Number(r.fidelity_raw) : null;
      const rawSub = (rawv != null && Math.abs(rawv - fidVal) >= 0.1)
        ? ` <span class="muted" style="font-size:10.5px" title="raw fidelity incl. systematic gaps">(raw ${rawv}%)</span>`
        : "";
      fid = `<span class="bar2"><span class="f" style="width:${w}%;background:${color}"></span></span>
        <span class="mono">${fidVal}%</span>${rawSub}`;
    }
    const blind = r.blind_spot
      ? ` <span class="cell c-crit" title="permission blind spot">blind</span>` : "";
    // `linked` rows are clickable: a stable hook attr + pointer cursor let the
    // delegated handler open the in-page project drill-down (no reload).
    const hook = linked
      ? ` class="proj-row" data-proj-row="${esc(r.key)}" tabindex="0" role="button"` : "";
    return `<tr${hook}>
      <td>${proj}${blind}</td>
      <td class="mono">${num(r.src_count ?? 0)}</td>
      <td class="mono">${num(r.tgt_count ?? 0)}</td>
      <td>${holeCell}</td>
      <td>${tailCell}</td>
      <td>${fid}</td></tr>`;
  }

  function heatTable(rows, linked) {
    const body = rows.length
      ? rows.map((r) => heatRow(r, linked)).join("")
      : `<tr><td colspan="6" class="muted">No ${vocab.containers.toLowerCase()} in scope.</td></tr>`;
    return `<table class="heat">
      <tr><th scope="col">${vocab.container}</th><th scope="col">Source</th><th scope="col">Target</th>
          <th scope="col">Holes</th><th scope="col">Tails</th><th scope="col">Fidelity</th></tr>
      ${body}</table>`;
  }

  /* Wire delegated drill-down clicks on any .heat table inside `el`. Clicking a
     project row (or pressing Enter/Space on it) opens the in-page detail view;
     the inner project <a> link is suppressed so nothing reloads. `restore` is a
     0-arg function that re-renders whatever view was showing. */
  function wireHeatDrill(el, restore) {
    const open = (tr) => {
      const key = tr && tr.getAttribute("data-proj-row");
      if (key) openProjectDetail(el, key, restore);
    };
    el.querySelectorAll(".heat").forEach((tbl) => {
      tbl.addEventListener("click", (ev) => {
        const tr = ev.target.closest("[data-proj-row]");
        if (!tr) return;
        ev.preventDefault();        // suppress the inner project <a> navigation
        open(tr);
      });
      tbl.addEventListener("keydown", (ev) => {
        if (ev.key !== "Enter" && ev.key !== " ") return;
        const tr = ev.target.closest("[data-proj-row]");
        if (!tr) return;
        ev.preventDefault();
        open(tr);
      });
    });
  }

  /* ---- findings table (project / issues views) ---- */
  function issuesTable(d) {
    const rows = (d.rows || []).map((r) => `<tr>
      <td>${kb(r.kind)}</td>
      <td>${itemLink(r.src_key, SRC, SRCDEP, r.project)}</td>
      <td>${itemLink(r.tgt_key, TGT, TGTDEP, r.project)}</td>
      <td class="mono">${esc(r.project ?? "")}</td>
      <td>${esc(r.field ?? "")}</td>
      <td>${esc(r.summary ?? "")}</td>
      <td class="muted mono detail">${fmtDetail(r.detail)}</td>
      <td><button class="find-solutions btn sm"
        data-run="${esc(R)}"
        data-kind="${esc(r.kind ?? "")}"
        data-area="${esc(r.project ?? "")}"
        data-name="${esc(r.src_key ?? r.tgt_key ?? "")}"
        data-srckey="${esc(r.src_key ?? "")}"
        data-tgtkey="${esc(r.tgt_key ?? "")}"
        data-project="${esc(r.project ?? "")}"
        data-field="${esc(r.field ?? "")}"
        data-product="${esc(PRODUCT)}"
        data-deployment-from="${esc(SRCDEP)}">Find solutions</button></td>
    </tr>`);
    return `<table class="data"><thead><tr>
      <th scope="col">Kind</th><th scope="col">Source</th><th scope="col">Target</th><th scope="col">${vocab.container}</th>
      <th scope="col">Field</th><th scope="col">Summary</th><th scope="col">Detail</th><th scope="col"></th></tr></thead>
      <tbody>${rows.join("") ||
      '<tr><td colspan="8" class="muted">No findings.</td></tr>'}</tbody></table>`;
  }

  async function issuesView(el, fixed = {}) {
    let page = 1;
    const render = async () => {
      const q = new URLSearchParams({ page, size: 50, ...fixed });
      const sel = $("#f-kind"), txt = $("#f-q");
      if (sel && sel.value) q.set("kind", sel.value);
      if (txt && txt.value) q.set("q", txt.value);
      const d = await get(`/api/runs/${R}/issues?${q}`);
      $("#tbl").innerHTML = issuesTable(d);
      $("#count").textContent =
        `${num(d.total)} finding(s) · page ${page}/${Math.max(1, Math.ceil(d.total / 50))}`;
    };
    const kinds = await get(`/api/runs/${R}/issues/kinds` +
      (fixed.project ? `?project=${encodeURIComponent(fixed.project)}` : ""));
    el.innerHTML = `
      <div class="card pad">
        <div class="pick-tools" style="margin:0 0 14px">
          <div class="search" style="max-width:320px">
            <svg width="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4-4"/></svg>
            <input id="f-q" placeholder="search key / summary / field">
          </div>
          <select id="f-kind" class="f-kind">
            <option value="">All kinds</option>
            ${Object.entries(kinds).map(([k, n]) =>
              `<option value="${esc(k)}">${esc(k)} (${n})</option>`).join("")}
          </select>
          <span class="spacer" style="flex:1"></span>
          <span id="count" class="muted mono"></span>
          <span class="pager">
            <button id="prev" class="btn ghost sm">‹</button>
            <button id="next" class="btn ghost sm">›</button>
          </span>
        </div>
        <div id="tbl" class="findings-wrap"></div>
      </div>`;
    $("#f-kind").onchange = () => { page = 1; render(); };
    $("#f-q").oninput = () => { page = 1; render(); };
    $("#prev").onclick = () => { page = Math.max(1, page - 1); render(); };
    $("#next").onclick = () => { page += 1; render(); };
    render();
  }

  /* ---- panel chrome (console .panel + header + status dot) ---- */
  function panel(title, dotTone, badge, body, full) {
    const dot = `<span class="panel-title-dot${dotTone ? " " + dotTone : ""}"></span>`;
    const badgeHtml = badge != null
      ? `<span class="panel-badge">${esc(badge)}</span>` : "";
    return `<div class="panel${full ? " panel-full" : ""}">
      <div class="panel-header">
        <span class="panel-title">${dot}${esc(title)}</span>
        ${badgeHtml}
      </div>${body}</div>`;
  }

  /* coerce a number-or-null to a finite Number, else null */
  const numOrNull = (v) =>
    (v == null || Number.isNaN(Number(v))) ? null : Number(v);
  /* one-decimal percent string */
  const pct1 = (v) => `${Number(v).toFixed(1)}%`;
  /* fidelity → bar/text color (emerald near-perfect, else amber) */
  const fidColorOf = (v) => (Number(v) >= 99 ? "var(--emerald-vivid)"
                                             : "var(--amber-vivid)");

  /* ---- issue-distribution donut, computed only from real summary counts ----
     `fid` carries the server's corrected fidelity ({core, raw}); when present
     the prominent number is the CORE value (systematic config gaps excluded)
     and the raw value rides along as subtext. Falls back to the local
     matched/audited arithmetic only when the server number is absent. */
  function distributionPanel(st, fid) {
    const audited = Number(st.issues_src_total ?? 0);
    if (!audited) return "";                       // no basis → omit gracefully
    const holes = Number(st.holes ?? 0);
    const tails = Number(st.tails ?? 0);
    const mism  = Number(st.issues_with_mismatches ?? 0);
    // matched is what's left after every flagged bucket; never below zero.
    const matched = Math.max(0, audited - holes - tails - mism);
    const pct = (n) => (audited ? (n / audited) * 100 : 0);
    const pM = pct(matched), pT = pct(tails), pH = pct(holes);
    // cumulative stops for the conic-gradient
    const s1 = pM, s2 = s1 + pT, s3 = s2 + pH;     // s4 = 100 (mismatch fills rest)
    const donutVar =
      `conic-gradient(` +
      `var(--emerald-vivid) 0 ${s1}%,` +
      `var(--amber-vivid) ${s1}% ${s2}%,` +
      `var(--red-vivid) ${s2}% ${s3}%,` +
      `var(--bg-elevated) ${s3}% 100%)`;
    const legend = `
      <div class="donut-legend">
        <div class="legend-row">
          <span class="legend-dot" style="background:var(--emerald-vivid)"></span>
          <span class="legend-label">Matched (clean)</span>
          <span class="legend-val">${num(matched)}</span></div>
        <div class="legend-row">
          <span class="legend-dot" style="background:var(--amber-vivid)"></span>
          <span class="legend-label">Post-cutover tails</span>
          <span class="legend-val">${num(tails)}</span></div>
        <div class="legend-row">
          <span class="legend-dot" style="background:var(--red-vivid)"></span>
          <span class="legend-label">Genuine holes</span>
          <span class="legend-val">${num(holes)}</span></div>
        <div class="legend-row">
          <span class="legend-dot" style="background:var(--bg-elevated);border:1px solid var(--border-mid)"></span>
          <span class="legend-label">Field mismatches</span>
          <span class="legend-val">${num(mism)}</span></div>
      </div>`;
    // Prefer the server's corrected CORE fidelity (systematic gaps removed);
    // only fall back to local arithmetic when the server didn't supply it.
    const core = fid ? numOrNull(fid.core) : null;
    const raw  = fid ? numOrNull(fid.raw)  : null;
    const fidPct = core != null ? core
                 : audited     ? (matched / audited) * 100 : 0;
    const fidColor = fidColorOf(fidPct);
    // raw subtext only when it meaningfully differs from the headline core
    const rawSub = (raw != null && core != null
                    && Math.abs(raw - core) >= 0.05)
      ? `<div class="fidelity-rawsub">raw incl. systematic gaps: ${pct1(raw)}</div>`
      : "";
    const fidBar = `
      <div class="fidelity-block">
        <div class="between" style="align-items:baseline">
          <span class="fidelity-cap">Overall fidelity</span>
          <span class="fidelity-num" style="color:${fidColor}">${pct1(fidPct)}</span>
        </div>
        ${rawSub}
        <div class="fidelity-rail">
          <div class="fidelity-fill2" style="width:${Math.max(0, Math.min(100, fidPct))}%"></div>
        </div></div>`;
    const body = `<div class="panel-body">
      <div class="donut-wrap">
        <div class="donut" style="--donut:${donutVar}"></div>
        ${legend}
      </div>${fidBar}</div>`;
    return panel(`${vocab.itemTitle} distribution`, "ok",
      `${num(audited)} total`, body, false);
  }

  /* ---- systematic-gaps callout (amber/info — NOT red data-loss) ----
     Each gap is a field that is empty on the target across a large share of a
     project's issues — the migration-config signature of a scheme/mapping that
     was never carried over, not per-issue data loss. Rendered only when the
     summary reports at least one. */
  function systematicGapsPanel(gaps) {
    if (!gaps || !gaps.length) return "";
    const rows = gaps.map((g) => {
      const field = esc(g.field ?? "field");
      const proj  = esc(g.project ?? "");
      const affected = num(g.affected_issues ?? 0);
      const emptyPct = g.target_empty_pct != null
        ? ` (${Number(g.target_empty_pct).toFixed(1)}% empty on target)` : "";
      const top = g.top_pattern
        ? `<div class="sysgap-top">top: <span class="mono">${esc(g.top_pattern)}</span></div>`
        : "";
      return `<div class="sysgap-row">
        <div class="sysgap-icon">${SVG.cfgSm}</div>
        <div class="sysgap-text">
          <div class="sysgap-headline"><b class="mono">${field}</b> absent on target for
            <b>${affected}</b> ${proj ? `<span class="mono">${proj}</span> ` : ""}${vocab.items}${emptyPct}
            <span class="sysgap-cause">— likely scheme / mapping not migrated</span></div>
          ${top}
        </div></div>`;
    }).join("");
    const body = `<div class="panel-body sysgap-body">
      <div class="sysgap-lead">These fields are systematically empty on the target —
        a migration-config signature (scheme / field mapping not carried over),
        not per-${vocab.item} data loss. They are excluded from core fidelity above.</div>
      ${rows}</div>`;
    return panel("Systematic gaps", "warn",
      `${gaps.length} field${gaps.length === 1 ? "" : "s"}`, body, true);
  }

  /* ================================================================
     PROJECT DRILL-DOWN (in-page; no reload)
     Fetches /projects/{key} + the project-scoped issues endpoint, swaps the
     analysis content to a detail view, and restores the prior view on Back.
     ================================================================ */

  /* one project-KPI tile (smaller than the overview kpiTile; no trend chip) */
  function projKpi({ value, label, sub, tone }) {
    const numCls = tone === "danger" ? " crit" : tone === "warn" ? " warn" : "";
    return `<div class="card kpi pj-kpi${tone === "danger" ? " danger"
              : tone === "warn" ? " warn" : ""}">
      <div class="lbl">${esc(label)}</div>
      <div class="num${numCls}">${value}</div>
      <div class="kpi-sublabel">${esc(sub || "")}</div></div>`;
  }

  /* FIELD-MISMATCH BREAKDOWN table; systematic fields carry a badge */
  function fieldBreakdownTable(rows) {
    const body = (rows || []).map((r) => {
      const tag = r.systematic
        ? ` <span class="sys-badge" title="systematically empty on target — config gap">systematic</span>`
        : "";
      const empty = r.target_empty_pct != null
        ? `${Number(r.target_empty_pct).toFixed(1)}%` : "—";
      return `<tr>
        <td>${esc(r.field ?? "")}${tag}</td>
        <td class="mono">${num(r.issues ?? 0)}</td>
        <td class="mono">${empty}</td></tr>`;
    }).join("") ||
      `<tr><td colspan="3" class="muted">No field mismatches.</td></tr>`;
    return `<table class="data"><thead><tr>
      <th scope="col">Field</th><th scope="col">${vocab.itemTitle}s</th><th scope="col">Target empty</th></tr></thead>
      <tbody>${body}</tbody></table>`;
  }

  /* TOP SOURCE → TARGET VALUE PAIRS for the worst field(s) */
  function valuePairsTable(pairs) {
    if (!pairs || !pairs.length)
      return `<div class="muted" style="padding:8px 2px">No value transitions to show.</div>`;
    const field = pairs[0].field;
    const body = pairs.map((p) => `<tr>
      <td class="mono">${esc(p.src ?? "")}</td>
      <td class="mono muted">→</td>
      <td class="mono">${esc(p.tgt ?? "")}</td>
      <td class="mono">${num(p.count ?? 0)}</td></tr>`).join("");
    return `<div class="muted mono" style="font-size:11px;margin:0 0 8px">
        worst field: <b>${esc(field)}</b></div>
      <table class="data"><thead><tr>
        <th scope="col">Source</th><th scope="col"></th><th scope="col">Target</th><th scope="col">${vocab.itemTitle}s</th></tr></thead>
        <tbody>${body}</tbody></table>`;
  }

  /* kind split (field / comment / link / content / attachment) as chips */
  function kindSplit(kb) {
    const entries = Object.entries(kb || {});
    if (!entries.length) return "";
    return `<div class="kind-split">${entries.map(([k, n]) =>
      `<span class="kind-chip">${kb2(k)}<span class="kind-n mono">${num(n)}</span></span>`
    ).join("")}</div>`;
  }
  // kind label without forcing the full kbadge palette weight in this context
  const kb2 = (k) => `<span class="kbadge k-${esc(k)}">${esc(k)}</span>`;

  /* evidence row: src→tgt + field + what differs (from detail.src/detail.tgt) */
  function evidenceTable(d) {
    const rows = (d.rows || []).map((r) => {
      const detail = r.detail || {};
      let diff;
      if ("src" in detail || "tgt" in detail) {
        diff = `<span class="mono">${esc(srcVal(detail.src))}</span>
          <span class="mono muted"> → </span>
          <span class="mono">${esc(srcVal(detail.tgt))}</span>`;
      } else {
        diff = `<span class="muted mono">${esc(
          JSON.stringify(detail)).slice(0, 120)}</span>`;
      }
      return `<tr>
        <td>${kb(r.kind)}</td>
        <td>${itemLink(r.src_key, SRC, SRCDEP, r.project)}</td>
        <td class="mono muted">→</td>
        <td>${itemLink(r.tgt_key, TGT, TGTDEP, r.project)}</td>
        <td>${esc(r.field ?? "")}</td>
        <td>${diff}${detail.cross_dialect
          ? ` <span class="sys-badge" title="Data Center wiki vs Cloud ADF — this difference may be a representation change, not data loss">representation-sensitive</span>`
          : ""}${detail.verify_sensitive
          ? ` <span class="sys-badge" title="${esc(detail.verify_reason || "this custom-field type cannot be normalized reliably across instances — verify before treating as data loss")}">verify-sensitive</span>`
          : ""}</td></tr>`;
    });
    return `<table class="data"><thead><tr>
      <th scope="col">Kind</th><th scope="col">Source</th><th scope="col"></th><th scope="col">Target</th>
      <th scope="col">Field</th><th scope="col">What differs</th></tr></thead>
      <tbody>${rows.join("") ||
      `<tr><td colspan="6" class="muted">No diverging ${vocab.items}.</td></tr>`}</tbody></table>`;
  }
  // render a detail value (string / list / empty) for the evidence column
  const srcVal = (v) => {
    if (v == null || v === "" || (Array.isArray(v) && !v.length)) return "(empty)";
    if (Array.isArray(v)) return v.join(", ");
    return String(v);
  };

  /* Fetch + render the in-page project detail view. `restore` re-renders the
     view we came from when Back is pressed. */
  async function openProjectDetail(el, key, restore) {
    el.innerHTML =
      `<div class="pj-loading muted mono">Loading ${esc(key)}…</div>`;
    let data;
    try {
      data = await get(`/api/runs/${R}/projects/${encodeURIComponent(key)}`);
    } catch (_e) {
      el.innerHTML = `<div class="finding crit"><div style="flex:1">
        <div class="ttl">Could not load ${vocab.container.toLowerCase()} ${esc(key)}.</div></div></div>`;
      return;
    }
    const pj = data.project || {};
    const fb = data.field_breakdown || [];
    const gaps = data.systematic_gaps || [];

    const core = numOrNull(pj.fidelity_core);
    const raw  = numOrNull(pj.fidelity_raw);
    const fidVal = core != null
      ? `<span style="color:${fidColorOf(core)}">${pct1(core)}</span>`
      : raw != null ? `<span style="color:${fidColorOf(raw)}">${pct1(raw)}</span>`
      : "—";
    const fidSub = (raw != null && core != null && Math.abs(raw - core) >= 0.05)
      ? `raw incl. systematic gaps: ${pct1(raw)}`
      : "core fidelity";

    const holes = Number(pj.holes ?? 0);
    const mism  = Number(pj.mismatched ?? 0);
    const tails = Number(pj.tails ?? 0);

    const kpis = [
      projKpi({ value: `${num(pj.src ?? 0)} <span class="muted mono"
                  style="font-size:18px">→</span> ${num(pj.tgt ?? 0)}`,
                label: "Source → Target", sub: `${vocab.itemTitle}s compared: ` +
                  num(pj.common ?? 0) }),
      projKpi({ value: num(holes), label: "Genuine holes",
                sub: "Missing in target", tone: holes > 0 ? "danger" : "" }),
      projKpi({ value: num(tails), label: "Post-cutover tails",
                sub: "Expected drift" }),
      projKpi({ value: num(mism), label: `Mismatched ${vocab.items}`,
                sub: "Value / schema divergence", tone: mism > 0 ? "warn" : "" }),
      projKpi({ value: fidVal, label: "Core fidelity", sub: fidSub }),
    ];

    const blindBadge = pj.blind_spot
      ? ` <span class="cell c-crit" title="permission blind spot">blind spot</span>` : "";

    // per-project systematic gaps reuse the run-level callout, scoped here.
    const gapsPanel = gaps.length ? systematicGapsPanel(gaps) : "";

    const fbBody = `<div class="panel-body">${fieldBreakdownTable(fb)}</div>`;
    const fbPanel = panel("Field-mismatch breakdown", fb.length ? "warn" : "ok",
      `${fb.length} field${fb.length === 1 ? "" : "s"}`, fbBody, false);

    const vpBody = `<div class="panel-body">${
      valuePairsTable(data.top_value_pairs)}</div>`;
    const vpPanel = panel("Top source → target value pairs", "",
      null, vpBody, false);

    const splitBody = `<div class="panel-body">${
      kindSplit(data.kind_breakdown) ||
      '<div class="muted">No divergence kinds.</div>'}</div>`;
    const splitPanel = panel("Divergence by kind", "", null, splitBody, true);

    el.innerHTML = `
      <div class="pj-head">
        <button class="btn ghost sm pj-back" type="button">← Back</button>
        <div class="pj-title">
          <span class="mono pj-key">${esc(pj.key ?? key)}</span>
          <span class="muted">${esc(pj.name ?? "")}</span>${blindBadge}
        </div>
      </div>
      <div class="kpis pj-kpis">${kpis.join("")}</div>
      <div class="muted" style="font-size:11.5px;margin:-6px 0 18px">
        Config gaps are reported at the run level.</div>
      ${gapsPanel}
      <div class="analysis-grid" style="margin-bottom:16px">
        ${fbPanel}
        ${vpPanel}
        ${splitPanel}
      </div>
      <div class="section-h" style="margin-top:6px">Evidence — diverging ${vocab.items}</div>
      <div id="pj-evidence"></div>`;

    el.querySelector(".pj-back").onclick = () => { restore(); };

    // paginated evidence list, scoped to this project at the SQL layer.
    const ev = el.querySelector("#pj-evidence");
    let page = 1;
    const renderEvidence = async () => {
      const q = new URLSearchParams({ page, size: 25, project: key });
      const sel = el.querySelector("#pe-kind");
      if (sel && sel.value) q.set("kind", sel.value);
      const d = await get(`/api/runs/${R}/issues?${q}`);
      el.querySelector("#pe-tbl").innerHTML = evidenceTable(d);
      el.querySelector("#pe-count").textContent =
        `${num(d.total)} ${vocab.item}(s) · page ${page}/${Math.max(1, Math.ceil(d.total / 25))}`;
    };
    let kinds = {};
    try { kinds = await get(`/api/runs/${R}/issues/kinds?project=${encodeURIComponent(key)}`); }
    catch (_e) { kinds = {}; }
    ev.innerHTML = `<div class="card pad">
        <div class="pick-tools" style="margin:0 0 14px">
          <select id="pe-kind" class="f-kind">
            <option value="">All kinds</option>
            ${Object.entries(kinds).map(([k, n]) =>
              `<option value="${esc(k)}">${esc(k)} (${n})</option>`).join("")}
          </select>
          <span class="spacer" style="flex:1"></span>
          <span id="pe-count" class="muted mono"></span>
          <span class="pager">
            <button id="pe-prev" class="btn ghost sm" type="button">‹</button>
            <button id="pe-next" class="btn ghost sm" type="button">›</button>
          </span>
        </div>
        <div id="pe-tbl" class="findings-wrap"></div>
      </div>`;
    el.querySelector("#pe-kind").onchange = () => { page = 1; renderEvidence(); };
    el.querySelector("#pe-prev").onclick = () => { page = Math.max(1, page - 1); renderEvidence(); };
    el.querySelector("#pe-next").onclick = () => { page += 1; renderEvidence(); };
    renderEvidence();
  }

  const views = {
    async overview(el) {
      const s = await get(`/api/runs/${R}/summary`);
      const st = s.stats || {};
      const headlines = s.headlines || [];
      const projRows = await get(`/api/runs/${R}/projects`);

      const meta = verdictMeta(s.verdict);
      const sub = headlines.slice(0, 2).map(esc).join(" ") ||
        `Every audited ${vocab.item} and config object matched.`;
      const audited = num(st.issues_src_total ?? 0);

      const holes = st.holes ?? 0;
      const configGaps = (st.config_missing ?? 0) + (st.config_other ?? 0);

      /* ── top alert: glowing CRITICAL banner, else the verdict banner ── */
      let topAlert;
      if (s.verdict === "CRITICAL") {
        // pull the real genuine-hole keys (server-paginated) for the chips
        let chips = [];
        if (holes > 0) {
          try {
            const d = await get(`/api/runs/${R}/issues?kind=missing_in_tgt&size=6`);
            chips = (d.rows || []).map((r) => r.src_key).filter(Boolean);
          } catch (_e) { chips = []; }
        }
        const critHl = headlines.find(headlineIsCrit) ||
          "Data integrity at risk — review the findings below before proceeding.";
        const chipDetail = chips.length
          ? ` Potential data loss in ${chips.map((c) =>
              `<span class="mono">${esc(c)}</span>`).join(", ")}.` : "";
        topAlert = `
          <div class="alert-crit">
            <div class="alert-icon">${SVG.triSm}</div>
            <div class="alert-body">
              <div class="alert-headline">${esc(critHl)}</div>
              <div class="alert-detail">Investigate before proceeding.${chipDetail}</div>
            </div>
            <div class="verdict-xl">CRITICAL</div>
          </div>`;
      } else {
        topAlert = `
          <div class="verdict-banner ${meta.cls}">
            <div class="vb-icon">${meta.icon}</div>
            <div style="flex:1">
              <div class="vb-word">${esc(meta.word)}</div>
              <div class="vb-sub">${sub}</div>
            </div>
            <div style="text-align:right">
              <div class="muted" style="font-size:11.5px">audited</div>
              <div class="mono" style="font-weight:600">${audited} ${vocab.items}</div>
            </div>
          </div>`;
      }

      /* ── KPI tiles — only real summary counts ── */
      const tiles = [
        kpiTile({ value: holes, label: "Genuine holes",
                  sub: "Missing in target below cutover", icon: SVG.triSm,
                  tone: holes > 0 ? "danger" : "" }),
        kpiTile({ value: st.tails ?? 0, label: "Post-cutover tails",
                  sub: "Expected drift — not data loss", icon: SVG.clockSm,
                  tone: "expected" }),
        kpiTile({ value: st.issues_with_mismatches ?? 0, label: "Field mismatches",
                  sub: "Value or schema divergence", icon: SVG.deltaSm,
                  tone: (st.issues_with_mismatches ?? 0) > 0 ? "warn" : "" }),
        kpiTile({ value: configGaps, label: "Config gaps",
                  sub: "Scheme / workflow discrepancies", icon: SVG.cfgSm,
                  tone: configGaps > 0 ? "warn" : "" }),
      ];
      if ((st.collisions ?? 0) > 0)
        tiles.push(kpiTile({ value: st.collisions, label: "Key collisions",
          sub: `Same key, two ${vocab.items}`, icon: SVG.collideSm, tone: "danger" }));
      if ((st.blind_spots ?? 0) > 0)
        tiles.push(kpiTile({ value: st.blind_spots, label: "Blind spots",
          sub: "Permission-masked counts", icon: SVG.eyeSm, tone: "danger" }));
      if ((st.orphans ?? 0) > 0)
        tiles.push(kpiTile({ value: st.orphans, label: "Orphans",
          sub: `Target-only ${vocab.items}`, icon: SVG.deltaSm, tone: "" }));
      if ((st.comments_uncheckable ?? 0) > 0)
        tiles.push(kpiTile({ value: st.comments_uncheckable,
          label: "Uncheckable comments", sub: "API-truncated", icon: SVG.cfgSm,
          tone: "" }));
      const kpiCount = Math.min(tiles.length, 8);

      /* ── headline findings panel (chips on the genuine-loss line) ── */
      let lossChips = [];
      if (s.verdict === "CRITICAL" && holes > 0) {
        try {
          const d = await get(`/api/runs/${R}/issues?kind=missing_in_tgt&size=8`);
          lossChips = (d.rows || []).map((r) => r.src_key).filter(Boolean);
        } catch (_e) { lossChips = []; }
      }
      const findingsBody = `<div class="panel-body"><div class="finding-stack">${
        headlines.length
          ? headlines.map((h) =>
              findingCard(h, /missing in the target|genuine data loss/i.test(h)
                ? lossChips : null)).join("")
          : `<div class="finding"><span style="color:var(--emerald-vivid);flex:0 0 auto;display:flex">${SVG.check}</span>
             <div style="flex:1"><div class="ttl">Every audited ${vocab.item} and config object matched. Clean migration.</div></div></div>`
      }</div></div>`;
      const findingsDot = s.verdict === "CRITICAL" ? "danger"
        : s.verdict === "GAPS_FOUND" ? "warn"
        : s.verdict === "CLEAN_WITH_TAILS" ? "warn" : "ok";
      const findingsPanel = panel("Headline findings", findingsDot,
        `${headlines.length} finding${headlines.length === 1 ? "" : "s"}`,
        findingsBody, false);

      /* ── distribution donut (omitted if no audited basis) ── */
      const distPanel = distributionPanel(st, s.fidelity);

      /* ── systematic-gaps callout (config-signature, amber — omitted if none) ── */
      const sysGapsPanel = systematicGapsPanel(s.systematic_gaps);

      /* ── container health table (full width) ── */
      const healthBody = `<div class="panel-body" style="padding:4px 6px">${
        heatTable(projRows, true)}</div>`;
      const healthPanel = panel(`${vocab.container} health`, "",
        `${projRows.length} ${(projRows.length === 1 ? vocab.container
                                                     : vocab.containers).toLowerCase()}`,
        healthBody, true);

      el.innerHTML = `
        ${topAlert}
        <div class="run-meta-strip">
          <div class="run-meta-item">
            <span class="run-meta-key">Source</span>
            <span class="run-meta-val">${esc(SRC ? SRC.replace(/^https?:\/\//, "") : "—")}</span>
          </div>
          <span class="run-meta-sep">›</span>
          <div class="run-meta-item">
            <span class="run-meta-key">Target</span>
            <span class="run-meta-val">${esc(TGT ? TGT.replace(/^https?:\/\//, "") : "—")}</span>
          </div>
          <span class="run-meta-sep" style="opacity:.3">|</span>
          <div class="run-meta-item">
            <span class="run-meta-key">Audited</span>
            <span class="run-meta-val">${audited} ${vocab.items}</span>
          </div>
          <div class="run-meta-item">
            <span class="run-meta-key">${vocab.containers}</span>
            <span class="run-meta-val">${num(st.projects ?? projRows.length)}</span>
          </div>
        </div>

        <div class="kpis kpi-console">${tiles.slice(0, kpiCount).join("")}</div>

        <div class="analysis-grid">
          ${findingsPanel}
          ${distPanel}
          ${sysGapsPanel}
          ${healthPanel}
        </div>`;
      // clicking a project-health row drills in-page; Back re-renders overview.
      wireHeatDrill(el, () => views.overview(el));
    },

    async projects(el) {
      const rows = await get(`/api/runs/${R}/projects`);
      const body = `<div class="panel-body" style="padding:4px 6px">${
        heatTable(rows, true)}</div>`;
      el.innerHTML = panel(`${vocab.container} health`, "",
        `${rows.length} ${(rows.length === 1 ? vocab.container
                                             : vocab.containers).toLowerCase()}`,
        body, true);
      wireHeatDrill(el, () => views.projects(el));
    },

    async project(el) { await issuesView(el, { project: P }); },
    async issues(el) { await issuesView(el); },

    async config(el) {
      const { areas, skipped = {} } = await get(`/api/runs/${R}/config`);
      const s = await get(`/api/runs/${R}/summary`);
      const blocks = [];
      // R4 honesty: skipped areas were NOT audited (no API on a side) and
      // emit zero findings by design — render them explicitly, first, so an
      // unaudited area can never read as clean.
      for (const a of Object.keys(skipped).sort()) {
        blocks.push(`<div class="card pad" style="margin-bottom:14px">
          <div class="between" style="margin-bottom:10px">
            <div class="section-h" style="margin:0">${esc(a)}</div>
            ${kb("skipped")}
          </div>
          <div class="muted">Not audited: ${esc(skipped[a])}. This area is not covered by the verdict.</div>
        </div>`);
      }
      for (const a of areas) {
        const { rows } = await get(`/api/runs/${R}/config?area=${encodeURIComponent(a)}`);
        const m = (s.areas || {})[a];
        const body = (rows || []).map((r) => `<tr>
          <td>${esc(r.name ?? "")}</td>
          <td>${kb(r.kind)}</td>
          <td class="muted mono detail">${fmtDetail(r.detail)}</td>
          <td><button class="find-solutions btn sm"
            data-run="${esc(R)}"
            data-kind="${esc(r.kind ?? "")}"
            data-area="${esc(a)}"
            data-name="${esc(r.name ?? "")}"
            data-product="${esc(PRODUCT)}"
            data-deployment-from="${esc(SRCDEP)}">Find solutions</button></td>
        </tr>`).join("") ||
          '<tr><td colspan="4" class="muted">No objects.</td></tr>';
        blocks.push(`<div class="card pad" style="margin-bottom:14px">
          <div class="between" style="margin-bottom:10px">
            <div class="section-h" style="margin:0">${esc(a)}</div>
            ${m ? `<span class="muted mono" style="font-size:12px">src ${num(m.src ?? "?")} · tgt ${num(m.tgt ?? "?")}</span>` : ""}
          </div>
          <table class="data"><thead><tr>
            <th scope="col">Object</th><th scope="col">Kind</th><th scope="col">Detail</th><th scope="col"></th></tr></thead>
            <tbody>${body}</tbody></table></div>`);
      }
      if (!areas.length && blocks.length) {
        // gaps-free audited areas, but skipped ones exist: say the clean
        // part explicitly WITHOUT the unconditional green check.
        blocks.push('<div class="muted" style="margin-top:4px">No config gaps found in the audited areas.</div>');
      }
      el.innerHTML = blocks.join("") ||
        `<div class="finding"><span style="color:var(--emerald-vivid);flex:0 0 auto;display:flex">${SVG.check}</span>
         <div style="flex:1"><div class="ttl">No config gaps found.</div></div></div>`;
    },

    async log(el) {
      const evs = await get(`/api/runs/${R}/events`);
      const lines = (evs || []).map((e) => {
        const ts = e.ts ? new Date(e.ts * 1000).toTimeString().slice(0, 8) : "";
        return `<span class="t">${esc(ts)}</span> ` +
          `<span class="ph2">[${esc(e.phase)}]</span> ` +
          `<span class="${e.level === "warn" ? "wn" : ""}">${esc(e.message)}</span>`;
      });
      el.innerHTML = `<div class="term" style="max-height:none">${lines.join("<br>") ||
        '<span class="t">no events</span>'}</div>`;
    },
  };

  return {
    boot() {
      const el = document.getElementById("app");
      R = el.dataset.run; V = el.dataset.view || "overview";
      P = el.dataset.project; SRC = el.dataset.srcBase; TGT = el.dataset.tgtBase;
      SRCDEP = el.dataset.srcDeployment || "cloud";
      TGTDEP = el.dataset.tgtDeployment || "cloud";
      PRODUCT = el.dataset.product || document.body.dataset.product || "jira";
      vocab = VOCAB[PRODUCT] || VOCAB.jira;
      // Branch: env-audit runs use a dedicated read-only renderer; migration
      // runs use the existing views. The audit_type is surfaced from the
      // migration row via data-audit-type on the #app element.
      if ((el.dataset.auditType || "") === "environment") {
        EnvAnalysis.render(el, R);
        return;
      }
      (views[V] || views.overview)(el);
    },
  };
})();

/* ================================================================
   Environment-audit analysis renderer
   Reads /api/runs/{id}/summary (reuses the same JSON endpoint as
   the migration renderer) and renders:
     – health-score dial (reuses the donut CSS class)
     – verdict pill
     – findings list (headlines grouped by severity; reuses kbadge)
     – AI assessment section with themes, top risks, quick wins
       and an explicit "AI skipped" note when the key is absent.

   All AI-generated text is HTML-escaped before insertion.
   ================================================================ */
const EnvAnalysis = (() => {
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const get = async (p) => (await fetch(p)).json();

  /* severity → badge class (reuse kbadge palette).
     k-low uses the slate/dim palette — informational, not a failure.
     The fallback "k-skipped" (amber) keeps unknown severities visible. */
  const sevClass = (sev) => {
    const s = (sev || "").toLowerCase();
    return s === "high"                  ? "k-missing_in_tgt"
         : s === "medium" || s === "med" ? "k-field_mismatch"
         : s === "low"                   ? "k-low"
         : "k-skipped";
  };

  /* Health dial: reuse .donut CSS with a single solid segment for the score.
     When score is null (AI skipped) render a greyed-out empty ring and "—"
     rather than a fully red 0 % ring that looks like a critical failure. */
  function healthDial(score, grade) {
    if (score === null || score === undefined) {
      const dim = "var(--text-dim)";
      const donutVar = `conic-gradient(var(--bg-elevated) 0 100%)`;
      return `
        <div style="display:flex;align-items:center;gap:28px;flex-wrap:wrap">
          <div class="donut-wrap">
            <div class="donut" style="--donut:${donutVar}"></div>
            <div class="donut-legend">
              <div class="legend-row">
                <span class="legend-dot" style="background:var(--bg-elevated);border:1px solid var(--border-mid)"></span>
                <span class="legend-label">Health score</span>
                <span class="legend-val mono" style="color:${dim};font-weight:700">—</span>
              </div>
            </div>
          </div>
          <div>
            <div class="mono" style="font-size:52px;font-weight:800;line-height:1;color:${dim}">—</div>
            <div class="muted" style="font-size:13px;margin-top:4px">AI skipped — score unavailable</div>
          </div>
        </div>`;
    }
    const pct = Math.max(0, Math.min(100, Number(score)));
    const color = pct >= 80 ? "var(--emerald-vivid)"
                : pct >= 60 ? "var(--amber-vivid)"
                :             "var(--red-vivid)";
    const rest  = 100 - pct;
    const donutVar =
      `conic-gradient(${color} 0 ${pct}%, var(--bg-elevated) ${pct}% 100%)`;
    return `
      <div style="display:flex;align-items:center;gap:28px;flex-wrap:wrap">
        <div class="donut-wrap">
          <div class="donut" style="--donut:${donutVar}"></div>
          <div class="donut-legend">
            <div class="legend-row">
              <span class="legend-dot" style="background:${color}"></span>
              <span class="legend-label">Health score</span>
              <span class="legend-val mono" style="color:${color};font-weight:700">${esc(String(pct))}</span>
            </div>
            <div class="legend-row">
              <span class="legend-dot" style="background:var(--bg-elevated);border:1px solid var(--border-mid)"></span>
              <span class="legend-label">Headroom</span>
              <span class="legend-val">${esc(String(rest))}</span>
            </div>
          </div>
        </div>
        <div>
          <div class="mono" style="font-size:52px;font-weight:800;line-height:1;color:${color}">${esc(String(pct))}</div>
          <div class="muted" style="font-size:13px;margin-top:4px">Grade <b>${esc(grade || "—")}</b> &nbsp;·&nbsp; Health score / 100</div>
        </div>
      </div>`;
  }

  /* Severity inference from headline text — approximates per-finding grouping
     when the payload only carries aggregate by_kind counts. Patterns match the
     language used by build_env_summary and the AI assessment summary field. */
  function _headlineSev(h) {
    const t = (h || "").toLowerCase();
    if (/high.severity|critical|data loss|no backup|security/i.test(t)) return "high";
    if (/medium.severity|require review|needs attention|warning/i.test(t)) return "medium";
    if (/low.severity|minor|note|info/i.test(t)) return "low";
    return null;   // no reliable inference — show no badge
  }

  /* Findings list: reuse .finding + kbadge classes.
     Each finding gets its own severity badge derived from its headline text;
     findings are visually ordered high → medium → low by their appearance
     in the headlines array (the engine already emits them in severity order). */
  function findingsList(headlines) {
    if (!headlines || !headlines.length) {
      return `<div class="finding">
        <span style="color:var(--emerald-vivid);flex:0 0 auto">
          <svg width="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4">
            <circle cx="12" cy="12" r="9"/><path d="M8 12.5l2.5 2.5L16 9"/></svg>
        </span>
        <div style="flex:1"><div class="ttl">No findings — environment looks healthy.</div></div></div>`;
    }
    return headlines.map((h) => {
      const sev = _headlineSev(h);
      const badgeHtml = sev
        ? `<span class="kbadge ${esc(sevClass(sev))}" style="margin-left:8px">${esc(sev)}</span>` : "";
      // null sev = no reliable inference → neutral/dim icon, not amber warning
      const iconColor = sev === "high"   ? "var(--red-vivid)"
                      : sev === "medium" ? "var(--amber-vivid)"
                      : sev === "low"    ? "var(--slate-deep)"
                      :                    "var(--text-dim)";
      return `<div class="finding">
        <span style="color:${iconColor};flex:0 0 auto;display:flex">
          <svg width="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <circle cx="12" cy="12" r="9"/><path d="M12 8v4m0 4h.01"/></svg>
        </span>
        <div style="flex:1"><div class="ttl">${esc(h)}${badgeHtml}</div></div></div>`;
    }).join("");
  }

  /* Render R.findings grouped by category (Performance → Security → Structure →
     DataQuality → Hygiene → Coverage). Each finding shows a severity badge, the
     area/name, and the suggested fix with a colour-coded tier badge + title +
     detail + caveat. All text fields go through esc() — fix text originates
     from live Jira object names and the fix registry. */
  // How many affected-object names to show before collapsing the rest behind a
  // <details>/"show all" affordance. A real instance produced 1600 findings, so
  // an uncapped per-kind name list is the thing that made the flat view unusable.
  const AFFECTED_CAP = 15;

  /* Render findings as PROBLEM-TYPE cards.
     Top-level category sections stay in the fixed orienting order.
     WITHIN a category, findings are GROUPED BY KIND into one problem card each:
       - header: the generic problem label (fix.label, fallback prettyKind),
         the affected count, a severity badge (the group's max severity), and
         the fix-tier badge.
       - the suggested fix shown ONCE (fix.detail + fix.caveat).
       - the affected objects (capped + collapsible).
     Cards within a category sort by severity (high→low) then count (desc).
     ALL label / fix / name text goes through esc(). */
  function findingsByCategory(findings) {
    if (!findings || !findings.length) return "";
    // The 6 top-level category sections in fixed orienting order.
    const ORDER = ["Performance", "Security", "Structure", "DataQuality",
                   "Hygiene", "Coverage"];
    // Severity rank for ordering problem cards (high → low). Mirrors the
    // server's _SEVERITY_RANK in webapp/analysis.py so UI and API agree.
    const SEV_RANK = { high: 0, medium: 1, low: 2, info: 3, warning: 4 };
    // tier → CSS class (the kbadge-style pill).
    const tierClass = (t) =>
      t === "app" ? "tier-app" : t === "unfixable" ? "tier-unfixable" : "tier-human";
    // tier → display label (fall back when tier_label absent). Guard against a
    // finding with no `fix` object (a missing fix must not throw inside render
    // and blank the whole analysis view) — read from a guarded local, never
    // dereference f.fix.tier_label / f.fix.tier directly.
    const tierLabel = (f) => {
      const fix = f.fix || {};
      return fix.tier_label ||
        (fix.tier === "app"        ? "Fixable by the app"
       : fix.tier === "unfixable"  ? "Re-migration suggested"
       :                             "Fixable by a human");
    };
    // prettify a raw kind key as a last-resort problem label (snake_case → Words)
    const prettyKind = (k) =>
      String(k || "Finding").replace(/_/g, " ").replace(/\b\w/g,
        (c) => c.toUpperCase());

    /* Affected-object list for one problem card. The first AFFECTED_CAP names
       render inline; if there are more, ALL names live inside a collapsed
       <details> so the full list is reachable with no JS framework and no extra
       scripting. Every name is escaped — they are live Jira / Confluence object
       names and registry strings. */
    // A chip is a plain badge, OR — when perChipLink is on AND the item carries
    // a space-scoped admin_link — a link straight to that object's admin screen.
    // href/label are esc()'d (site_url is the trusted connection base + a static
    // path; the name is already URL-encoded server-side).
    const affectedList = (items, perChipLink) => {
      const chip = (f) => {
        const n = esc(f.name || "(unnamed)");
        const l = perChipLink && f.admin_link;
        return l
          ? `<a class="affected-chip mono admin-chip" href="${esc(l.url)}" target="_blank" rel="noopener noreferrer" title="${esc(l.label)}">${n}</a>`
          : `<span class="affected-chip mono">${n}</span>`;
      };
      const chips = (arr) => arr.map(chip).join("");
      if (items.length <= AFFECTED_CAP) {
        return `<div class="affected-list">${chips(items)}</div>`;
      }
      const extra = items.length - AFFECTED_CAP;
      // <details> holds the COMPLETE list so opening it never double-renders
      // the first AFFECTED_CAP — the collapsed inline row is replaced.
      return `<div class="affected-list">${chips(items.slice(0, AFFECTED_CAP))}</div>
        <details class="affected-more">
          <summary>show all ${items.length} affected</summary>
          <div class="affected-list" style="margin-top:8px">${chips(items)}</div>
        </details>
        <span class="affected-more-count muted">+${extra} more</span>`;
    };

    // 1) bucket findings by category, then by kind within each category.
    const byCat = {};
    for (const f of findings) {
      const cat = f.category || "Hygiene";
      const kind = f.kind || "_unknown";
      (byCat[cat] || (byCat[cat] = {}));
      (byCat[cat][kind] || (byCat[cat][kind] = [])).push(f);
    }

    let html = "";
    for (const cat of ORDER) {
      const kinds = byCat[cat];
      if (!kinds) continue;

      // 2) build one problem-card descriptor per kind in this category.
      const cards = Object.keys(kinds).map((kind) => {
        const items = kinds[kind];
        const fix = (items.find((f) => f.fix) || {}).fix || {};
        // representative (worst) severity drives the badge + the sort.
        let bestRank = 99, bestSev = items[0] && items[0].severity || "info";
        for (const f of items) {
          const r = SEV_RANK[(f.severity || "info").toLowerCase()] ?? 99;
          if (r < bestRank) { bestRank = r; bestSev = f.severity; }
        }
        return { kind, items, fix, sev: bestSev || "info",
                 rank: bestRank, count: items.length };
      });
      // 3) sort: severity (high→low) then count (desc).
      cards.sort((a, b) => (a.rank - b.rank) || (b.count - a.count));

      const catTotal = cards.reduce((n, c) => n + c.count, 0);
      html += `<div class="cat-group">
        <div class="cat-header">
          <span class="cat-name">${esc(cat)}</span>
          <span class="cat-count muted mono">${catTotal}</span>
        </div>
        <div class="finding-stack">`;

      for (const c of cards) {
        const fix = c.fix;
        const label = fix.label || prettyKind(c.kind);
        const repItem = { fix };   // tierLabel reads .fix; share the group's fix
        const tier = fix.tier;
        const caveat = fix.caveat
          ? `<div class="muted" style="font-size:11.5px;margin-top:3px;font-style:italic">${esc(fix.caveat)}</div>`
          : "";
        // app-tier cards can be auto-fixed on the Fix options screen — hint it
        // (the page already has the Fix options button; we do not build a flow).
        const appHint = tier === "app"
          ? `<div class="app-fix-hint muted">These can be auto-fixed on the Fix options screen.</div>`
          : "";
        // Admin deep-link: a global-scoped kind shares ONE url across all its
        // findings → render a single card-level "Open admin screen" link. A
        // space-scoped kind has a distinct url per object → no card link; each
        // affected chip becomes its own link instead (perChipLink).
        const links = c.items.map((i) => i.admin_link).filter(Boolean);
        const urls = new Set(links.map((l) => l.url));
        const perChipLink = urls.size > 1;
        const cardLink = (links.length && urls.size === 1)
          ? `<a class="admin-link" href="${esc(links[0].url)}" target="_blank" rel="noopener noreferrer">${esc(links[0].label)} &rarr;</a>`
          : "";
        html += `
          <div class="problem-card env-finding">
            <div class="problem-head">
              <span class="problem-label">${esc(label)}</span>
              <span class="problem-count mono">(${c.count})</span>
              <span class="kbadge ${esc(sevClass(c.sev))}">${esc(c.sev || "info")}</span>
              <span class="kbadge ${esc(tierClass(tier))}">${esc(tierLabel(repItem))}</span>
              ${cardLink}
            </div>
            <div class="problem-fix muted">${esc(fix.detail || "")}</div>
            ${caveat}
            ${appHint}
            ${affectedList(c.items, perChipLink)}
          </div>`;
      }
      html += `</div></div>`;
    }
    return html;
  }

  /* AI assessment section */
  function aiSection(ai) {
    if (!ai || ai.skipped) {
      return `
        <div class="card pad" style="margin-top:16px">
          <div class="section-h" style="margin-top:0">AI Assessment</div>
          <div class="finding">
            <span style="flex:0 0 auto;color:var(--text-dim)">
              <svg width="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <circle cx="12" cy="12" r="9"/><path d="M12 8v4m0 4h.01"/></svg>
            </span>
            <div style="flex:1">
              <div class="ttl">AI skipped — add a key in Settings</div>
              <div class="muted" style="font-size:12px;margin-top:4px">
                Configure an Anthropic API key in Settings to enable AI-powered analysis themes, risk identification and quick wins.</div>
            </div>
          </div>
        </div>`;
    }

    const summaryHtml = ai.summary
      ? `<p style="margin:0 0 14px;color:var(--text-secondary);font-size:13px;line-height:1.55">${esc(ai.summary)}</p>`
      : "";

    const themes = (ai.themes || []).map((t) => {
      const sevBadge = `<span class="kbadge ${esc(sevClass(t.severity))}">${esc(t.severity || "info")}</span>`;
      return `
        <div class="finding" style="margin-bottom:10px">
          <span style="color:var(--cyan-vivid);flex:0 0 auto;display:flex">
            <svg width="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M12 8v4l3 2"/><circle cx="12" cy="12" r="9"/></svg>
          </span>
          <div style="flex:1">
            <div class="ttl">${esc(t.title || "")} ${sevBadge}</div>
            ${t.recommendation ? `<div class="muted" style="font-size:12px;margin-top:3px">${esc(t.recommendation)}</div>` : ""}
          </div>
        </div>`;
    }).join("");

    const risks = (ai.top_risks || []).map((r) =>
      `<li style="margin-bottom:6px;color:var(--text-secondary)">${esc(r)}</li>`).join("");
    const wins = (ai.quick_wins || []).map((w) =>
      `<li style="margin-bottom:6px;color:var(--text-secondary)">${esc(w)}</li>`).join("");

    return `
      <div class="card pad" style="margin-top:16px">
        <div class="section-h" style="margin-top:0">AI Assessment</div>
        ${summaryHtml}
        ${themes ? `<div class="finding-stack" style="margin-bottom:16px">${themes}</div>` : ""}
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:8px">
          ${risks ? `<div>
            <div class="muted mono" style="font-size:10px;text-transform:uppercase;letter-spacing:.07em;margin-bottom:8px">Top Risks</div>
            <ul style="margin:0;padding-left:18px">${risks}</ul>
          </div>` : ""}
          ${wins ? `<div>
            <div class="muted mono" style="font-size:10px;text-transform:uppercase;letter-spacing:.07em;margin-bottom:8px">Quick Wins</div>
            <ul style="margin:0;padding-left:18px">${wins}</ul>
          </div>` : ""}
        </div>
      </div>`;
  }

  /* AI-discovered issues section — the AI acting as a SECOND, complementary
     auditor. ai.ai_findings carries issues the deterministic rule engine did
     NOT catch (cross-object inconsistencies, governance/security posture gaps,
     naming smells, risky combinations, scale concerns). Each finding renders as
     its own card with a severity badge + title + area + observation +
     recommendation. The whole section is VISUALLY DISTINCT from the
     deterministic problem-type cards — an "AI" tag/accent on the heading and an
     `ai-finding` class on each card — so the operator can tell AI observations
     apart from rule-based findings. These are advisory/complementary and do NOT
     drive the (rule-driven) verdict. Renders nothing when ai_findings is empty
     or AI was skipped. ALL text is esc()'d. */
  function aiFindingsSection(ai) {
    // Graceful empty: nothing to show when AI was skipped or there are no
    // additional findings — render no empty card shell.
    if (!ai || ai.skipped) return "";
    const findings = Array.isArray(ai.ai_findings) ? ai.ai_findings : [];
    if (!findings.length) return "";

    // A small labelled block, e.g. "ROOT CAUSE — <text>".
    const block = (label, text, color) => text
      ? `<div style="font-size:12.5px;margin-top:6px;line-height:1.5;color:${color || "var(--text-secondary)"}">`
        + `<b class="muted" style="font-size:10px;text-transform:uppercase;letter-spacing:.06em">${label}</b><br>${esc(text)}</div>`
      : "";

    const cards = findings.map((f) => {
      const sev = f.severity;
      const sevBadge =
        `<span class="kbadge ${esc(sevClass(sev))}">${esc(sev || "info")}</span>`;
      const prio = (typeof f.priority === "number")
        ? `<span class="kbadge" title="priority" style="background:var(--bg-3)">P${esc(String(f.priority))}</span>` : "";
      const effort = (f.effort === "S" || f.effort === "M" || f.effort === "L")
        ? `<span class="kbadge mono" title="effort" style="background:var(--bg-3)">${esc(f.effort)}</span>` : "";
      const area = f.area
        ? `<span class="ai-finding-area mono muted">${esc(f.area)}</span>` : "";
      // Prefer the rich fields; fall back to the legacy observation/recommendation.
      const rootCause = block("Root cause", f.root_cause, "var(--text-secondary)");
      const risk = block("Risk", f.risk || f.observation, "var(--text-secondary)");
      const steps = Array.isArray(f.remediation_steps) && f.remediation_steps.length
        ? `<div style="font-size:12.5px;margin-top:6px;line-height:1.5;color:var(--text-secondary)">`
          + `<b class="muted" style="font-size:10px;text-transform:uppercase;letter-spacing:.06em">Remediation</b>`
          + `<ol style="margin:4px 0 0;padding-left:18px">`
          + f.remediation_steps.map((s) => `<li>${esc(s)}</li>`).join("")
          + `</ol></div>`
        : block("Recommendation", f.recommendation);
      return `
        <div class="problem-card ai-finding">
          <div class="problem-head">
            <span class="ai-tag">AI</span>
            <span class="problem-label">${esc(f.title || "Observation")}</span>
            ${area}
            ${prio}
            ${effort}
            ${sevBadge}
          </div>
          ${rootCause}
          ${risk}
          ${steps}
        </div>`;
    }).join("");

    return `
      <div class="card pad ai-findings-card" style="margin-top:16px">
        <div class="section-h" style="margin-top:0">
          AI-discovered issues
          <span class="ai-tag" style="margin-left:8px">AI</span>
        </div>
        <div class="muted" style="font-size:11.5px;margin:-6px 0 12px">
          Additional issues a second AI auditor surfaced beyond the rule-based
          checks above. Advisory — these do not change the verdict.</div>
        <div class="finding-stack ai-finding-stack">${cards}</div>
      </div>`;
  }

  /* Prioritized remediation ROADMAP + "what the rules can't tell you" gaps —
     the highest-value output of the map-reduce synthesis: an ordered, rationale'd
     action plan rather than a flat list. Renders nothing when both are empty. */
  function aiRoadmapSection(ai) {
    if (!ai || ai.skipped) return "";
    const roadmap = Array.isArray(ai.roadmap) ? ai.roadmap : [];
    const gaps = Array.isArray(ai.gaps) ? ai.gaps : [];
    if (!roadmap.length && !gaps.length) return "";

    const steps = roadmap.map((s, i) => {
      const effort = (s.effort === "S" || s.effort === "M" || s.effort === "L")
        ? `<span class="kbadge mono" title="effort" style="background:var(--bg-3);margin-left:6px">${esc(s.effort)}</span>` : "";
      const addresses = Array.isArray(s.addresses) && s.addresses.length
        ? `<div class="muted" style="font-size:11px;margin-top:3px">Addresses: ${esc(s.addresses.join(", "))}</div>` : "";
      const rationale = s.rationale
        ? `<div class="muted" style="font-size:12px;margin-top:3px;line-height:1.45">${esc(s.rationale)}</div>` : "";
      return `
        <div class="finding" style="margin-bottom:10px;align-items:flex-start">
          <span class="mono" style="flex:0 0 auto;color:var(--cyan-vivid);font-weight:700;font-size:13px">${i + 1}.</span>
          <div style="flex:1">
            <div class="ttl">${esc(s.step || s.title || "")}${effort}</div>
            ${rationale}
            ${addresses}
          </div>
        </div>`;
    }).join("");

    const gapList = gaps.map((g) =>
      `<li style="margin-bottom:6px;color:var(--text-secondary)">${esc(g)}</li>`).join("");

    return `
      <div class="card pad" style="margin-top:16px">
        <div class="section-h" style="margin-top:0">
          Remediation roadmap
          <span class="ai-tag" style="margin-left:8px">AI</span>
        </div>
        ${roadmap.length
          ? `<div class="muted" style="font-size:11.5px;margin:-6px 0 12px">Highest-leverage actions first — the order to work the backlog in, not just what's wrong.</div>
             <div class="finding-stack">${steps}</div>`
          : ""}
        ${gapList
          ? `<div style="margin-top:${roadmap.length ? "16px" : "0"}">
               <div class="muted mono" style="font-size:10px;text-transform:uppercase;letter-spacing:.07em;margin-bottom:8px">What the rule engine doesn't check</div>
               <ul style="margin:0;padding-left:18px">${gapList}</ul>
             </div>`
          : ""}
      </div>`;
  }

  /* Verdict pill banner.
     NOTE: EnvAnalysis is a separate IIFE from the outer Analysis module and
     cannot reference its top-level VERDICT constant (lines 89-103). If a new
     verdict key is added to that outer VERDICT map, mirror it here too. */
  function verdictBanner(verdict, score) {
    const v = verdict || "";
    const cls = v === "HEALTHY"            ? "vb-clean"
              : v === "HEALTHY_WITH_NOTES" ? "vb-tails"
              : v === "NEEDS_ATTENTION"    ? "vb-gaps"
              : v === "CRITICAL"           ? "vb-crit"
              : "vb-tails";
    const word = v === "HEALTHY"            ? "HEALTHY"
               : v === "HEALTHY_WITH_NOTES" ? "HEALTHY · NOTES"
               : v === "NEEDS_ATTENTION"    ? "NEEDS ATTENTION"
               : v === "CRITICAL"           ? "CRITICAL"
               : esc(v);
    const icon = (v === "HEALTHY" || v === "HEALTHY_WITH_NOTES")
      ? `<svg width="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"><circle cx="12" cy="12" r="9"/><path d="M8 12.5l2.5 2.5L16 9"/></svg>`
      : `<svg width="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z"/><path d="M12 9v4m0 4h.01"/></svg>`;
    return `
      <div class="verdict-banner ${esc(cls)}">
        <div class="vb-icon">${icon}</div>
        <div style="flex:1">
          <div class="vb-word">${esc(word)}</div>
          <div class="vb-sub">Environment health assessment</div>
        </div>
        <div style="text-align:right">
          <div class="muted" style="font-size:11.5px">health score</div>
          <div class="mono" style="font-weight:700;font-size:20px">${esc(String(score ?? "—"))}</div>
        </div>
      </div>`;
  }

  return {
    async render(el, runId) {
      el.innerHTML = `<div class="muted mono" style="padding:40px 4px">Loading environment analysis…</div>`;
      let s;
      try { s = await get(`/api/runs/${runId}/summary`); }
      catch (_) {
        el.innerHTML = `<div class="finding crit"><div style="flex:1"><div class="ttl">Could not load analysis.</div></div></div>`;
        return;
      }
      const st = s.stats || {};
      // Preserve null when AI was skipped — coercing to 0 renders a fully red
      // ring and shows "0" in the banner, which looks like a critical failure.
      const healthScore = st.health_score ?? null;
      const grade = st.grade || "—";
      const ai = st.ai || null;
      // Headlines live at the top level of the summary response (the endpoint
      // pops them out of stats). build_env_summary prepends the AI summary as
      // the first headline; it has its own slot in aiSection, so drop it here
      // to avoid a duplicate and a misleading regex-inferred badge on prose.
      let headlines = s.headlines || [];
      if (ai && !ai.skipped && ai.summary && headlines[0] === ai.summary) {
        headlines = headlines.slice(1);
      }
      const verdict = s.verdict || "";

      const catFindings = (s.findings && s.findings.length)
        ? findingsByCategory(s.findings)
        : "";

      el.innerHTML = `
        ${verdictBanner(verdict, healthScore)}
        <div class="card pad" style="margin-top:16px">
          <div class="section-h" style="margin-top:0">Health Score</div>
          ${healthDial(healthScore, grade)}
        </div>
        <div class="card pad" style="margin-top:16px">
          <div class="section-h" style="margin-top:0">Findings</div>
          ${catFindings
            ? `<div class="findings-by-cat">${catFindings}</div>`
            : `<div class="finding-stack">${findingsList(headlines)}</div>`}
        </div>
        ${aiSection(ai)}
        ${aiRoadmapSection(ai)}
        ${aiFindingsSection(ai)}`;
    },
  };
})();
