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

    const icon = seaUiEl('span', 'font-size:15px;line-height:1;pointer-events:none');
    icon.textContent = '≈';
    icon.setAttribute('aria-hidden', 'true');

    const label = seaUiEl('span', [
      'position:absolute', 'right:34px', 'top:50%', 'transform:translateY(-50%)',
      'white-space:nowrap', 'opacity:0', 'transition:opacity .2s',
      'pointer-events:none',
    ].join(';'));
    label.className = 'zz-sea-label';
    label.textContent = 'sea';
    applyFont(label);

    button.append(icon, label);

    // ---- menu --------------------------------------------------------------

    const rowCss = 'display:flex;align-items:center;gap:6px;margin:0 0 5px;' +
      'line-height:1.4;cursor:pointer';
    const groupCss = 'margin:7px 0 5px;border:0;padding:0';
    const titleCss = 'font-size:10px;letter-spacing:.08em;text-transform:uppercase;' +
      'opacity:.55;margin:0 0 4px';

    function row(input, text) {
      const el = seaUiEl('label', rowCss);
      el.append(input, document.createTextNode(text));
      return el;
    }

    const menu = seaUiEl('div', [
      'position:absolute', 'right:0', 'top:36px', 'width:190px',
      'box-sizing:border-box', 'background:#0a1016ee',
      'border:1px solid #ffffff22', 'border-radius:6px', 'padding:12px',
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
    onBox.style.accentColor = '#fff';
    const onRow = row(onBox, 'sea on/off');
    onRow.style.marginBottom = '6px';

    const quality = seaUiEl('fieldset', groupCss);
    const qualityTitle = seaUiEl('legend', titleCss);
    qualityTitle.textContent = 'quality';
    quality.append(qualityTitle);
    const radios = [];
    const labels = { 'extra-low': 'extra low' };
    for (const id of MODE_IDS) {
      const input = document.createElement('input');
      input.type = 'radio';
      input.name = 'zz-sea-mode';
      input.value = id;
      input.style.margin = '0';
      input.style.accentColor = '#fff';
      radios.push(input);
      quality.append(row(input, labels[id] || id));
    }
    // Task 8 sets __zzSea.canUseShader. Undefined means "not probed yet", so
    // the animated entries stay selectable and a failed build falls back.
    const note = seaUiEl('div', 'font-size:11px;line-height:1.35;opacity:.6;margin:2px 0 0');
    note.textContent = 'WebGL2 is unavailable, animated modes are off.';
    note.hidden = true;
    quality.append(note);

    function rangeRow(text, min, max) {
      const el = seaUiEl('label', rowCss);
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
      input.style.accentColor = '#fff';
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
    fpsBox.style.accentColor = '#fff';
    const fpsRow = row(fpsBox, '30 fps limit');
    fpsRow.style.marginBottom = '0';

    const status = document.createElement('div');
    status.id = 'zz-sea-status';
    status.style.cssText = 'font-size:11px;line-height:1.35;opacity:.75;margin-top:2px';

    menu.append(onRow, quality, opacity.el, brightness.el, fpsRow, status);

    wrap.append(button, menu);
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
        reposition();
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

export { mountSeaUi, seaUiPlacement };
