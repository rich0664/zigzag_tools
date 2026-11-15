/* Sea quality menu for the zig~zag HUD.
 *
 * A 30 px button under the notifications button (same right edge) opens a dark
 * panel with the sea on/off switch, the five quality ids, opacity, brightness
 * and the 30 fps limit. The menu owns no state: it paints getState() and
 * reports every change through onChange(patch). The runtime in sea.js applies
 * the change, persists it and returns the state to paint.
 *
 * The header has `contain: paint` and is 36 px tall, so the container lives on
 * document.body and is positioned `fixed` from the notifications button's live
 * rect. Its position is refreshed on window resize and by a ResizeObserver on
 * the header, never per frame.
 *
 * The userscript runs at document-idle, before this SPA has rendered the map
 * header. Mounting is therefore lazy: a MutationObserver on body (plus a 30 s
 * polling fallback) waits for the notifications button, and the same watcher
 * remounts the HUD if React later replaces or removes it.
 */

const SEA_UI_BTN_ID = 'zz-sea-btn';
const SEA_UI_MENU_ID = 'zz-sea-menu';
const SEA_UI_WRAP_ID = 'zz-sea-hud';
const SEA_UI_GAP = 6;             // px between the notifications button and ours
const SEA_UI_BTN = 30;            // both buttons are 30 x 30
const SEA_UI_MOUNT_MS = 30000;    // fallback polling stops this long after mount

// The stored id stays `baseline`; the HUD's pixel menu calls it fancy.
const SEA_UI_MODE_LABELS = {
  baseline: 'fancy',
  medium: 'medium',
  low: 'low',
  'extra-low': 'extra low',
  static: 'static',
};

// A 16 x 16 pixel wave: 2 px rows of 4 px blocks, as eight crisp rectangles.
const SEA_UI_WAVE = [
  [0, 4, 4, 2], [8, 4, 4, 2],
  [4, 6, 4, 2], [12, 6, 4, 2],
  [0, 8, 4, 2], [8, 8, 4, 2],
  [4, 10, 4, 2], [12, 10, 4, 2],
];

const SEA_UI_INK = '#dbe6ec';
const SEA_UI_TRACK = '#4a5f6b';
const SEA_UI_RULE = '#26323a';

// Native controls do not inherit the page font and carry rounded, hairlined
// skins; this scoped sheet gives the panel the game's pixel HUD geometry.
function seaUiCss() {
  return [
    '#zz-sea-menu, #zz-sea-menu * {',
    '  border-radius: 0 !important;',
    '  box-shadow: none !important;',
    '}',
    `#zz-sea-menu button, #zz-sea-menu label, #zz-sea-menu input { font-family: inherit; }`,
    '#zz-sea-menu button::before, #zz-sea-menu button::after,',
    '#zz-sea-menu input::before, #zz-sea-menu input::after { font-family: inherit; }',
    '#zz-sea-menu input[type="checkbox"], #zz-sea-menu input[type="radio"] {',
    '  appearance: none; -webkit-appearance: none;',
    '  width: 10px; height: 10px; flex: 0 0 10px;',
    '  margin: 0; padding: 0; border: 2px solid ' + SEA_UI_INK + ';',
    '  background-color: transparent; background-repeat: no-repeat;',
    '  background-position: center; box-sizing: border-box; cursor: pointer;',
    '}',
    '#zz-sea-menu input[type="checkbox"]:checked,',
    '#zz-sea-menu input[type="radio"]:checked {',
    '  background-image: linear-gradient(' + SEA_UI_INK + ', ' + SEA_UI_INK + ');',
    '}',
    '#zz-sea-menu input[type="checkbox"]:checked { background-size: 4px 4px; }',
    '#zz-sea-menu input[type="radio"]:checked { background-size: 8px 8px; }',
    '#zz-sea-menu input[type="checkbox"]:disabled,',
    '#zz-sea-menu input[type="radio"]:disabled { opacity: .35; cursor: default; }',
    '#zz-sea-menu input[type="range"] {',
    '  appearance: none; -webkit-appearance: none; height: 12px;',
    '  background: transparent;',
    '}',
    '#zz-sea-menu input[type="range"]::-webkit-slider-runnable-track {',
    '  height: 4px; background: ' + SEA_UI_TRACK + '; border: 0; border-radius: 0;',
    '  font-family: inherit;',
    '}',
    '#zz-sea-menu input[type="range"]::-webkit-slider-thumb {',
    '  -webkit-appearance: none; appearance: none; width: 10px; height: 10px;',
    '  margin-top: -3px; background: ' + SEA_UI_INK + '; border: 0; border-radius: 0;',
    '  font-family: inherit;',
    '}',
    '#zz-sea-menu input[type="range"]::-moz-range-track {',
    '  height: 4px; background: ' + SEA_UI_TRACK + '; border: 0; border-radius: 0;',
    '  font-family: inherit;',
    '}',
    '#zz-sea-menu input[type="range"]::-moz-range-thumb {',
    '  width: 10px; height: 10px; background: ' + SEA_UI_INK + ';',
    '  border: 0; border-radius: 0; font-family: inherit;',
    '}',
    '#zz-sea-menu fieldset { min-width: 0; }',
  ].join('\n');
}

// right/top of the fixed container and whether the panel must open above the
// button. Pure: node --test pins the anchor rule without a DOM.
function seaUiPlacement(rect, viewport, menuHeight, gap) {
  const g = gap === undefined ? SEA_UI_GAP : gap;
  const top = rect.bottom + g;
  return {
    right: viewport.width - rect.right,
    top,
    flipped: top + SEA_UI_BTN + g + menuHeight > viewport.height,
  };
}

function seaUiEl(tag, css) {
  const el = document.createElement(tag);
  if (css) el.style.cssText = css;
  return el;
}

function mountSeaUi(opts) {
  const getState = () => (opts && opts.getState ? opts.getState() : null);
  const onChange = patch => (opts && opts.onChange ? opts.onChange(patch) : null);
  const getStats = () => (opts && opts.getStats ? opts.getStats() : null);
  const runBench = () => {
    if (opts && typeof opts.bench === 'function') return opts.bench();
    const sea = typeof window !== 'undefined' && window.__zzSea;
    return sea && typeof sea.bench === 'function' ? sea.bench() : null;
  };

  let ui = null;                 // the mounted HUD, when the header exists
  let anchor = null;             // the notifications button it is anchored to

  function teardown() {
    if (ui) { ui.destroy(); ui = null; }
    anchor = null;
  }

  // Find / lose the notifications button. Runs on DOM mutations, the bounded
  // poll, and once at startup; it destroys a dead container before rebuilding.
  function tick() {
    if (ui && anchor && anchor.isConnected) return;
    teardown();
    const live = document.querySelector('button[aria-label^="Notifications"]');
    if (!live) return;
    const stale = document.getElementById(SEA_UI_WRAP_ID);
    if (stale) stale.remove();     // never leave two containers behind
    anchor = live;
    ui = build(live);
  }

  function refresh(state) {
    const next = state || getState();
    if (ui && next) ui.sync(next);
  }

  function build(liveAnchor) {
    // The app's Tailwind classes are purged, so copy the live HUD text style
    // from the notifications button once, at mount.
    const hud = getComputedStyle(liveAnchor);
    const applyFont = el => {
      el.style.fontFamily = hud.fontFamily;
      el.style.fontSize = hud.fontSize;
      el.style.letterSpacing = hud.letterSpacing;
      el.style.color = hud.color;
    };

    const wrap = seaUiEl('div',
      'position:fixed;width:30px;height:30px;pointer-events:none;z-index:21');
    wrap.id = SEA_UI_WRAP_ID;
    applyFont(wrap);

    // ---- button ------------------------------------------------------------

    const button = seaUiEl('button', [
      'position:relative', 'width:30px', 'height:30px', 'margin:0', 'padding:0',
      'background:transparent', 'border:0', 'cursor:pointer', 'opacity:.6',
      'display:inline-flex', 'align-items:center', 'justify-content:center',
      'pointer-events:auto', 'transition:opacity .2s',
    ].join(';'));
    button.id = SEA_UI_BTN_ID;
    button.type = 'button';
    button.title = 'sea';
    button.setAttribute('aria-label', 'sea options');
    button.setAttribute('aria-haspopup', 'dialog');
    button.setAttribute('aria-expanded', 'false');
    applyFont(button);

    const wave = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    wave.setAttribute('width', '16');
    wave.setAttribute('height', '16');
    wave.setAttribute('viewBox', '0 0 16 16');
    wave.setAttribute('shape-rendering', 'crispEdges');
    wave.setAttribute('fill', 'currentColor');
    wave.setAttribute('aria-hidden', 'true');
    wave.setAttribute('focusable', 'false');
    wave.style.cssText = 'display:block;width:16px;height:16px;pointer-events:none';
    for (const [x, y, w, h] of SEA_UI_WAVE) {
      const rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
      rect.setAttribute('x', String(x));
      rect.setAttribute('y', String(y));
      rect.setAttribute('width', String(w));
      rect.setAttribute('height', String(h));
      wave.append(rect);
    }

    const label = seaUiEl('span', [
      'position:absolute', 'right:34px', 'top:50%', 'transform:translateY(-50%)',
      'white-space:nowrap', 'opacity:0', 'transition:opacity .2s',
      'pointer-events:none',
    ].join(';'));
    label.className = 'zz-sea-label';
    label.textContent = 'sea';
    applyFont(label);

    // 2 px block underline while a lazy texture load is in flight; the menu's
    // status line carries the words.
    const loadingMark = seaUiEl('span', [
      'position:absolute', 'left:4px', 'right:4px', 'bottom:2px', 'height:2px',
      'background:currentColor', 'opacity:.8', 'pointer-events:none',
      'display:none',
    ].join(';'));
    loadingMark.id = 'zz-sea-loading';

    button.append(wave, label, loadingMark);

    // ---- menu --------------------------------------------------------------

    const rowCss = 'display:flex;align-items:center;gap:6px;margin:0 0 5px;' +
      'line-height:1.4;cursor:pointer';
    const groupCss = 'margin:7px 0 5px;border:0;padding:0';
    const titleCss = 'font-size:10px;letter-spacing:.08em;text-transform:uppercase;' +
      'opacity:.55;margin:0 0 4px';

    function row(input, text) {
      const el = seaUiEl('label', rowCss);
      el.style.fontFamily = 'inherit';
      el.append(input, document.createTextNode(text));
      return el;
    }

    // 2 px block rule with square ends; the only separator in the panel.
    function divider() {
      const el = seaUiEl('div',
        `height:2px;background:${SEA_UI_RULE};margin:8px 0;border:0`);
      el.setAttribute('aria-hidden', 'true');
      return el;
    }

    const menu = seaUiEl('div', [
      'position:absolute', 'right:0', 'top:36px', 'width:190px',
      'box-sizing:border-box', 'background:#0a1016',
      'border:2px solid ' + SEA_UI_RULE, 'border-radius:0', 'padding:12px',
      'overflow-y:auto', 'pointer-events:auto',
    ].join(';'));
    menu.id = SEA_UI_MENU_ID;
    menu.hidden = true;
    menu.setAttribute('role', 'dialog');
    menu.setAttribute('aria-label', 'sea settings');
    applyFont(menu);

    const onBox = document.createElement('input');
    onBox.type = 'checkbox';
    onBox.style.margin = '0';
    onBox.style.fontFamily = 'inherit';
    const onRow = row(onBox, 'sea on/off');
    onRow.style.marginBottom = '6px';

    const quality = seaUiEl('fieldset', groupCss);
    const qualityTitle = seaUiEl('legend', titleCss);
    qualityTitle.textContent = 'quality';
    quality.append(qualityTitle);
    const radios = [];
    for (const id of MODE_IDS) {
      const input = document.createElement('input');
      input.type = 'radio';
      input.name = 'zz-sea-mode';
      input.value = id;
      input.style.margin = '0';
      input.style.fontFamily = 'inherit';
      radios.push(input);
      quality.append(row(input, SEA_UI_MODE_LABELS[id] || id));
    }
    // canUseShader === false is the menu's honest report of a failed probe:
    // the animated entries are disabled and the note explains why.
    const note = seaUiEl('div', 'font-size:11px;line-height:1.35;opacity:.6;margin:2px 0 0');
    note.textContent = 'Animated modes need WebGL2 and an 8192 px texture limit.';
    note.hidden = true;
    quality.append(note);

    function rangeRow(text, min, max) {
      const el = seaUiEl('label', rowCss);
      el.style.fontFamily = 'inherit';
      const name = seaUiEl('span', 'flex:0 0 auto');
      name.textContent = text;
      const input = document.createElement('input');
      input.type = 'range';
      input.min = String(min);
      input.max = String(max);
      input.step = '1';
      input.style.flex = '1';
      input.style.minWidth = '0';
      input.style.margin = '0';
      input.style.fontFamily = 'inherit';
      const out = seaUiEl('span',
        'flex:0 0 34px;text-align:right;opacity:.7;font-variant-numeric:tabular-nums');
      el.append(name, input, out);
      return { el, input, out };
    }

    const opacity = rangeRow('opacity', 0, 100);
    const brightness = rangeRow('brightness', 50, 150);

    const fpsBox = document.createElement('input');
    fpsBox.type = 'checkbox';
    fpsBox.style.margin = '0';
    fpsBox.style.fontFamily = 'inherit';
    const fpsRow = row(fpsBox, '30 fps limit');
    fpsRow.style.marginBottom = '0';

    const status = document.createElement('div');
    status.id = 'zz-sea-status';
    status.hidden = true;
    status.style.cssText = 'font-size:11px;line-height:1.35;opacity:.75;' +
      'margin:0 0 5px;overflow-wrap:anywhere';

    const retryBtn = document.createElement('button');
    retryBtn.type = 'button';
    retryBtn.id = 'zz-sea-retry';
    retryBtn.textContent = 'retry';
    retryBtn.style.cssText = 'display:none;margin:0 0 6px;padding:2px 6px;' +
      'background:#121a21;border:2px solid #3a4a55;border-radius:0;' +
      'color:inherit;font:inherit;font-family:inherit;font-size:10px;cursor:pointer';

    // Diagnostics: the VRAM estimate, the frame rate and the renderer, read on
    // demand from the runtime. The interval only runs while the menu is open.
    const diag = seaUiEl('div', 'display:flex;align-items:flex-start;gap:6px;margin:0');
    const diagText = seaUiEl('div', 'flex:1;min-width:0;font-family:inherit;' +
      'font-size:10px;line-height:1.4;opacity:.6');
    const diagLine1 = seaUiEl('div', 'white-space:normal;overflow-wrap:anywhere');
    const diagLine2 = seaUiEl('div', 'white-space:nowrap;overflow:hidden;text-overflow:ellipsis');
    diagText.append(diagLine1, diagLine2);
    const benchBtn = document.createElement('button');
    benchBtn.type = 'button';
    benchBtn.id = 'zz-sea-bench';
    benchBtn.textContent = 'bench';
    benchBtn.style.cssText = 'flex:0 0 auto;padding:2px 6px;background:#121a21;' +
      'border:2px solid #3a4a55;border-radius:0;color:inherit;font:inherit;' +
      'font-family:inherit;font-size:10px;line-height:1.4;cursor:pointer;opacity:.85';
    diag.append(diagText, benchBtn);

    // The last block after the second rule: status/retry, then the readout and
    // its bench button.
    const diagnostics = seaUiEl('div', 'display:block;margin:0');
    diagnostics.append(status, retryBtn, diag);

    // textContent, not innerHTML: the renderer string is page-controlled.
    function paintStats(s) {
      if (!s) {
        diagLine1.textContent = '';
        diagLine2.textContent = '';
        return;
      }
      const mb = (Number(s.vramMB) || 0).toFixed(1);
      const kind = s.mode === 'static' ? 'image' : 'VRAM';
      const fps = Math.round(Number(s.fps) || 0);
      const ms = (Number(s.ms) || 0).toFixed(1);
      diagLine1.textContent = `${mb} MB ${kind} · ${fps} fps · ${ms} ms ${s.msKind || 'cpu'}`;
      diagLine2.textContent = s.renderer || '';
    }

    let statsTimer = 0;
    function stopStats() {
      if (statsTimer) clearInterval(statsTimer);
      statsTimer = 0;
    }
    function startStats() {
      stopStats();
      paintStats(getStats());
      statsTimer = setInterval(() => paintStats(getStats()), 500);
    }

    benchBtn.addEventListener('click', () => {
      let r = null;
      try { r = runBench(); } catch (e) { r = { error: String((e && e.message) || e) }; }
      if (!r) return;
      diagLine1.textContent = r.error
        ? `bench: ${r.error}`
        : `${r.mode} ${(Number(r.ms) || 0).toFixed(1)} ms / ${Math.round(Number(r.fps) || 0)} fps`;
      diagLine2.textContent = '';
    });

    // Pixel HUD order: on/off, quality, 30 fps, rule, opacity, brightness,
    // rule, diagnostics + bench.
    menu.append(
      onRow, quality, fpsRow, divider(),
      opacity.el, brightness.el, divider(),
      diagnostics,
    );

    const css = document.createElement('style');
    css.textContent = seaUiCss();
    wrap.append(css, button, menu);
    document.body.appendChild(wrap);

    // ---- painting ----------------------------------------------------------

    function sync(s) {
      if (!s) return;
      onBox.checked = !!s.on;
      const shaderOff = s.canUseShader === false;
      for (const input of radios) {
        input.checked = input.value === s.mode;
        if (input.value !== 'static') input.disabled = shaderOff;
      }
      note.hidden = !shaderOff;
      const op = Math.round((Number.isFinite(s.opacity) ? s.opacity : 1) * 100);
      opacity.input.value = String(op);
      opacity.out.textContent = String(op);
      const br = Math.round((Number.isFinite(s.brightness) ? s.brightness : 1) * 100);
      brightness.input.value = String(br);
      brightness.out.textContent = `${br}%`;
      fpsBox.checked = !!s.fps30;

      // The status line carries lazy-load progress and the fallback reason;
      // retry is only offered where a retry can plausibly help.
      const loading = !!s.loading;
      loadingMark.style.display = loading ? 'block' : 'none';
      const err = s.error ? String(s.error) : '';
      status.textContent = loading ? 'loading sea…' : err;
      status.title = err;
      status.hidden = !status.textContent;
      retryBtn.style.display =
        err && !loading && s.canUseShader !== false ? 'inline-block' : 'none';

      // a mode switch changes fps/ms/VRAM: repaint the readout right away
      if (open) paintStats(getStats());
    }

    function emit(patch) {
      const next = onChange(patch);
      if (next) sync(next);
    }

    // ---- position ----------------------------------------------------------

    function reposition() {
      const r = liveAnchor.getBoundingClientRect();
      if (!r.width && !r.height) return;
      let menuHeight = 0;
      if (!menu.hidden) {
        menu.style.maxHeight = 'none';
        menuHeight = menu.getBoundingClientRect().height;
      }
      const place = seaUiPlacement(
        { right: r.right, bottom: r.bottom },
        { width: window.innerWidth, height: window.innerHeight },
        menuHeight,
      );
      wrap.style.right = `${place.right}px`;
      wrap.style.top = `${place.top}px`;
      if (menu.hidden) return;

      // Keep the panel inside the viewport even when neither side has room for
      // its full height: the panel scrolls instead of being clipped.
      const below = Math.max(0, window.innerHeight - (place.top + 36) - 8);
      // flipped: the panel's bottom sits `gap` above the button's top
      const above = Math.max(0, place.top - SEA_UI_GAP - 8);
      menu.style.top = place.flipped ? 'auto' : '36px';
      menu.style.bottom = place.flipped ? '36px' : 'auto';
      menu.style.maxHeight = `${place.flipped ? above : below}px`;
    }

    // ---- open / close ------------------------------------------------------

    let open = false;

    function setOpen(next) {
      open = !!next;
      menu.hidden = !open;
      button.setAttribute('aria-expanded', open ? 'true' : 'false');
      if (open) {
        sync(getState());          // pick up async runtime changes on open
        startStats();              // and only here does the readout poll
        reposition();
      } else {
        stopStats();
      }
    }

    function showLabel(on) {
      label.style.opacity = on ? '.6' : '0';
      button.style.opacity = on ? '1' : '.6';
    }

    button.addEventListener('click', () => setOpen(!open));
    button.addEventListener('mouseenter', () => showLabel(true));
    button.addEventListener('mouseleave', () => showLabel(false));
    button.addEventListener('focus', () => showLabel(true));
    button.addEventListener('blur', () => showLabel(false));

    onBox.addEventListener('change', () => emit({ on: onBox.checked }));
    for (const input of radios) {
      input.addEventListener('change', () => {
        if (input.checked) emit({ mode: input.value });
      });
    }
    opacity.input.addEventListener('input', () => {
      opacity.out.textContent = opacity.input.value;
      emit({ opacity: Number(opacity.input.value) / 100 });
    });
    // brightness is shown in percent; the setter takes the 0.5..1.5 multiplier
    brightness.input.addEventListener('input', () => {
      brightness.out.textContent = `${brightness.input.value}%`;
      emit({ brightness: Number(brightness.input.value) / 100 });
    });
    fpsBox.addEventListener('change', () => emit({ fps30: fpsBox.checked }));
    retryBtn.addEventListener('click', () => emit({ retry: true }));

    const onResize = () => reposition();

    const onPointerDown = e => {
      if (open && !wrap.contains(e.target)) setOpen(false);
    };

    const onKeyDown = e => {
      if (open && e.key === 'Escape') {
        setOpen(false);
        button.focus();
      }
    };

    window.addEventListener('resize', onResize);
    document.addEventListener('pointerdown', onPointerDown, true);
    document.addEventListener('keydown', onKeyDown);

    const header = liveAnchor.closest('header') || liveAnchor.parentElement;
    const observer = new ResizeObserver(() => reposition());
    observer.observe(header);

    sync(getState());
    reposition();

    function destroy() {
      stopStats();
      observer.disconnect();
      window.removeEventListener('resize', onResize);
      document.removeEventListener('pointerdown', onPointerDown, true);
      document.removeEventListener('keydown', onKeyDown);
      wrap.remove();
    }

    return { sync, destroy };
  }

  // ---- lifecycle -----------------------------------------------------------

  const target = document.body || document.documentElement;
  const observer = new MutationObserver(tick);
  observer.observe(target, { childList: true, subtree: true });

  const started = Date.now();
  const interval = setInterval(() => {
    tick();
    if (ui || Date.now() - started >= SEA_UI_MOUNT_MS) clearInterval(interval);
  }, 500);
  tick();

  return {
    refresh,
    destroy() {
      clearInterval(interval);
      observer.disconnect();
      teardown();
    },
  };
}

export { mountSeaUi, seaUiPlacement, SEA_UI_MODE_LABELS };
