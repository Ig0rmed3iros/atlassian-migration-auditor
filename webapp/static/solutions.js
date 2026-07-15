(function () {
  function relTime(ts) {
    if (!ts) return '';
    const s = Math.floor(Date.now() / 1000 - ts);
    if (s < 60) return 'just now';
    if (s < 3600) return Math.floor(s / 60) + 'm ago';
    return Math.floor(s / 3600) + 'h ago';
  }
  function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g,
      c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }
  function safeHref(url) {
    /* Reject non-http(s) URLs to prevent javascript: or data: injection.
       encodeURI alone does not strip the javascript: scheme. */
    if (!url) return '#';
    try {
      const u = new URL(url);
      if (u.protocol !== 'https:' && u.protocol !== 'http:') return '#';
      return u.href;
    } catch (_) { return '#'; }
  }
  function render(panel, data) {
    if (data.error) {
      /* Distinct no-key state: the backend returns this specific message when
         ANTHROPIC_API_KEY is absent.  Surface a direct link to Settings. */
      if (data.error.toLowerCase().indexOf('api key') !== -1
          || data.error.toLowerCase().indexOf('settings') !== -1) {
        panel.innerHTML =
          '<div class="card pad">No Anthropic API key configured — '
          + '<a href="/settings">add your key in Settings</a> to search for solutions.</div>';
        return;
      }
      panel.innerHTML =
        '<div class="card pad">' + escapeHtml(data.error) + '</div>';
      return;
    }
    const sols = data.solutions || [];
    if (!sols.length) { panel.innerHTML =
      '<div class="card pad sub">No external solutions found — see the built-in '
      + 'guidance above.</div>'; return; }
    const cards = sols.map(s =>
      '<div class="card pad" style="margin-top:8px">'
      + '<b>' + escapeHtml(s.title || 'Solution') + '</b>'
      + (s.confidence ? ' <span class="kbadge">' + escapeHtml(s.confidence) + '</span>' : '')
      + '<p class="sub">' + escapeHtml(s.summary || '') + '</p>'
      + (s.applicability
          ? '<p class="sub muted" style="margin-top:2px"><em>Applies to: '
            + escapeHtml(s.applicability) + '</em></p>' : '')
      + (Array.isArray(s.steps) && s.steps.length
          ? '<ol>' + s.steps.map(x => '<li>' + escapeHtml(x) + '</li>').join('') + '</ol>' : '')
      + ((s.sources || []).length
          ? '<div class="sub">Sources: ' + s.sources.map(src =>
              '<a href="' + safeHref(src.url) + '" target="_blank" rel="noopener">'
              + escapeHtml(src.title || src.url) + '</a>').join(' · ') + '</div>' : '')
      + '</div>').join('');
    panel.innerHTML = cards
      + '<div class="sub" style="margin-top:6px">searched ' + relTime(data.searched_at)
      + ' · <a href="#" class="sol-refresh">Refresh</a> · Only finding metadata is '
      + 'sent to Anthropic — never issue or page content.</div>';
  }
  async function run(btn, refresh) {
    const runId = btn.getAttribute('data-run');
    let panel = btn.nextElementSibling;
    if (!panel || !panel.classList.contains('sol-panel')) {
      panel = document.createElement('div'); panel.className = 'sol-panel';
      btn.parentNode.insertBefore(panel, btn.nextSibling);
    }
    panel.innerHTML = '<div class="sub">Searching the web…</div>';
    const body = new URLSearchParams();
    /* Map camelCase keys to their HTML data-attribute names (all kebab-case
       per the HTML spec: data-deployment-from, data-srckey, data-tgtkey). */
    const attrMap = {
      kind: 'data-kind', area: 'data-area', name: 'data-name',
      project: 'data-project', src_key: 'data-srckey', tgt_key: 'data-tgtkey',
      field: 'data-field', product: 'data-product',
      deployment_from: 'data-deployment-from',
    };
    Object.entries(attrMap).forEach(([param, attr]) => {
      const v = btn.getAttribute(attr);
      if (v) body.set(param, v);
    });
    if (refresh) body.set('refresh', '1');
    try {
      const res = await fetch('/runs/' + runId + '/solutions',
        { method: 'POST', body });
      const data = await res.json();
      render(panel, data);
    } catch (e) { panel.innerHTML = '<div class="card pad">Request failed.</div>'; }
  }
  document.addEventListener('click', function (e) {
    const btn = e.target.closest('.find-solutions');
    if (btn) { e.preventDefault(); run(btn, false); return; }
    if (e.target.classList && e.target.classList.contains('sol-refresh')) {
      e.preventDefault();
      const solPanel = e.target.closest('.sol-panel');
      const b = solPanel && solPanel.previousElementSibling;
      if (b) run(b, true);
    }
  });
})();
