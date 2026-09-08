// Browser-only: floating panel + preview/match review + debug dump.
const ZZ = (globalThis.ZZ ??= {});
ZZ.state = ZZ.state || { discogs: null, videos: [], matches: [], log: [] };

function zzLog(msg) {
  ZZ.state.log.push(msg);
  const el = document.querySelector('[data-testid="zz-log"]');
  if (el) {
    const div = document.createElement('div');
    div.textContent = msg;
    el.appendChild(div);
    el.scrollTop = el.scrollHeight;
  }
}

function panelHtml() {
  return `
  <div data-testid="zz-panel" style="position:fixed;top:8px;right:338px;z-index:99999;width:380px;max-height:92vh;overflow:auto;background:#111;color:#eee;border:1px solid #555;border-radius:8px;padding:10px;font:12px sans-serif;">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;">
      <b>zig-zag filler (never submits)</b>
      <button data-testid="zz-collapse" style="background:#333;color:#eee;border:1px solid #555;border-radius:4px;">–</button>
    </div>
    <div data-testid="zz-body">
      <input data-testid="zz-discogs" placeholder="Discogs release URL" style="width:100%;margin-bottom:4px;background:#222;color:#eee;border:1px solid #555;border-radius:4px;padding:4px;" />
      <input data-testid="zz-playlist" placeholder="YouTube playlist URL" style="width:100%;margin-bottom:4px;background:#222;color:#eee;border:1px solid #555;border-radius:4px;padding:4px;" />
      <div style="display:flex;gap:4px;margin-bottom:6px;">
        <button data-testid="zz-fetch" style="flex:1;background:#234;color:#fff;border:1px solid #555;border-radius:4px;padding:5px;">Fetch &amp; preview</button>
        <button data-testid="zz-fill" disabled style="flex:1;background:#333;color:#888;border:1px solid #555;border-radius:4px;padding:5px;">Fill form</button>
      </div>
      <div data-testid="zz-preview" style="margin-bottom:6px;"></div>
      <div data-testid="zz-log" style="max-height:120px;overflow:auto;background:#000;border:1px solid #333;border-radius:4px;padding:4px;margin-bottom:6px;"></div>
      <button data-testid="zz-dump" style="width:100%;background:#333;color:#eee;border:1px solid #555;border-radius:4px;padding:4px;">Copy debug dump</button>
    </div>
  </div>`;
}

function renderPreview() {
  const { discogs, matches } = ZZ.state;
  const box = document.querySelector('[data-testid="zz-preview"]');
  if (!box || !discogs) return;
  const rows = matches.map((m, i) => {
    const opts = ZZ.state.videos
      .map((v) => `<option value="${v.id}" ${m.video && m.video.id === v.id ? 'selected' : ''}>${escapeHtml(v.title)}</option>`)
      .join('');
    return `<div style="display:flex;gap:4px;align-items:center;margin-bottom:2px;">
      <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${escapeHtml(m.track)}">${i + 1}. ${escapeHtml(m.track)}</span>
      <select data-testid="zz-match-${i}" style="flex:1;background:#222;color:#eee;border:1px solid #555;">${opts}</select>
      <span style="color:${m.video ? '#8f8' : '#f88'}">${m.video ? m.score.toFixed(2) : '—'}</span>
    </div>`;
  }).join('');
  box.innerHTML = `<div style="margin-bottom:4px;"><b>${escapeHtml(discogs.title || '?')}</b> · ${escapeHtml((discogs.artists || []).join(', '))} · ${escapeHtml(discogs.year || '?')}</div>${rows}`;
  const fill = document.querySelector('[data-testid="zz-fill"]');
  if (fill) {
    fill.disabled = false;
    fill.style.background = '#263';
    fill.style.color = '#fff';
  }
}

function escapeHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function selectorCheck() {
  const ids = [
    'contribute-track-form', 'contribute-release-section', 'contribute-tracklist-section',
    'contribute-support-section', 'contribute-artists-section', 'contribute-label-section',
    'contribute-submit-section', 'add-track', 'add-artist', 'image-upload-input', 'draft-checklist',
  ];
  const out = {};
  for (const id of ids) out[id] = !!document.querySelector(`[data-testid="${id}"]`);
  return out;
}

function copyDebugDump() {
  const dump = {
    url: location.href,
    selectors: selectorCheck(),
    discogs: ZZ.state.discogs && { ...ZZ.state.discogs, tracks: ZZ.state.discogs.tracks?.length },
    videos: ZZ.state.videos.length,
    matches: ZZ.state.matches.map((m) => ({ track: m.track, video: m.video?.id || null, score: m.score })),
    htmlSnippet: ZZ.state.discogsHtmlSnippet || null,
    log: ZZ.state.log,
  };
  const text = JSON.stringify(dump, null, 2);
  (navigator.clipboard?.writeText(text) || Promise.reject()).then(
    () => zzLog('debug dump copied — paste it back to the dev'),
    () => {
      window.prompt('Copy debug dump:', text);
    },
  );
}

function mountPanel(handlers) {
  if (document.querySelector('[data-testid="zz-panel"]')) return;
  const wrap = document.createElement('div');
  wrap.innerHTML = panelHtml();
  document.body.appendChild(wrap);
  wrap.querySelector('[data-testid="zz-fetch"]').addEventListener('click', handlers.onFetch);
  wrap.querySelector('[data-testid="zz-fill"]').addEventListener('click', handlers.onFill);
  wrap.querySelector('[data-testid="zz-dump"]').addEventListener('click', copyDebugDump);
  const body = wrap.querySelector('[data-testid="zz-body"]');
  const btn = wrap.querySelector('[data-testid="zz-collapse"]');
  btn.addEventListener('click', () => {
    const hidden = body.style.display === 'none';
    body.style.display = hidden ? '' : 'none';
    btn.textContent = hidden ? '–' : '+';
  });
  for (const line of ZZ.state.log) zzLog(line);
}

export { zzLog, renderPreview, mountPanel, selectorCheck };
