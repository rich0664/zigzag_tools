/* Sea depth overlay for the zig~zag exploration map.
 *
 * zig~zag draws its map with deck.gl into #deckgl-overlay, which clears to
 * transparent (the black page background shows through). We insert a canvas
 * directly behind it and paint an ocean into the gaps, using the same
 * OrthographicViewport maths deck uses, read live from React's viewState.
 *
 * The water itself is a pre-rendered bathymetry: distance-from-land with a
 * shelf/slope/abyss profile, regional depth variation, ridge/fracture noise
 * and a subtle NW hillshade. It is 1 px per map tile and drawn with smoothing
 * off, so one texel == one map cell and the grain matches the map's own tiles.
 */
(() => {
  'use strict';

  const SEA_URL = window.__ZZ_SEA_URL ||
    'https://raw.githubusercontent.com/rich0664/zigzag_tools/master/dist/zigzag-sea.webp?v=0.2.14';
  const MAP_W = 7749;          // main map extent, in tiles
  const MAP_H = 5254;
  const OV_ID = 'zz-sea';

  const state = { on: true, img: null, raf: 0, sig: '', draws: 0,
                  opacity: (() => {
                    const v = parseFloat(localStorage.getItem('zz-sea-opacity'));
                    return isFinite(v) ? Math.max(0, Math.min(1, v)) : 1;
                  })() };
  window.__zzSea = state;

  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));

  const deckCanvas = () => document.getElementById('deckgl-overlay');

  // deck's view state lives on the React fiber of its canvas
  function viewState(deck) {
    const key = Object.keys(deck).find(k => k.startsWith('__reactFiber'));
    if (!key) return null;
    let f = deck[key];
    while (f && !(f.memoizedProps && f.memoizedProps.viewState)) f = f.return;
    return f ? f.memoizedProps.viewState : null;
  }

  function overlay(deck) {
    let ov = document.getElementById(OV_ID);
    if (!ov) {
      ov = document.createElement('canvas');
      ov.id = OV_ID;
    }
    if (ov.parentNode !== deck.parentNode || ov.nextElementSibling !== deck) {
      deck.parentNode.insertBefore(ov, deck);
    }
    ov.style.cssText = (deck.getAttribute('style') || '') + ';pointer-events:none';
    ov.style.opacity = String(state.opacity);
    // work in CSS pixels: quality settings and devicePixelRatio change the
    // canvas backing store, which would otherwise shift the projection
    const cw = deck.clientWidth || deck.width;
    const ch = deck.clientHeight || deck.height;
    if (ov.width !== cw || ov.height !== ch) {
      ov.width = cw;
      ov.height = ch;
    }
    return ov;
  }

  function draw() {
    const deck = deckCanvas();
    const img = state.img;
    if (!deck || !img) return;

    const vs = viewState(deck);
    if (!vs) return;

    const pad = vs.padding || {};
    const z = Math.pow(2, vs.zoom);
    const W = deck.clientWidth || deck.width;
    const H = deck.clientHeight || deck.height;

    const sig = [vs.zoom, vs.target[0], vs.target[1], pad.left, pad.right, W, H].join('|');
    if (sig === state.sig) return;
    state.sig = sig;

    const ov = overlay(deck);
    const c = ov.getContext('2d');

    // deck centres the view on target inside the padded area
    const dx = clamp(((pad.left || 0) + W - (pad.right || 0)) / 2, 0, W) - W / 2;
    const dy = clamp(((pad.top || 0) + H - (pad.bottom || 0)) / 2, 0, H) - H / 2;

    const pxt = img.width / MAP_W;                 // image px per tile
    const s = z / pxt;                             // screen px per image px
    const tx = vs.target[0] + 0.5, ty = vs.target[1] + 0.5;
    const ox = W / 2 + dx - tx * z;
    const oy = H / 2 + dy + (MAP_H - 0.5 - ty) * z;   // raster runs top-down

    // only the visible slice of the texture
    const wx0 = (0 - W / 2 - dx) / z + vs.target[0];
    const wx1 = (W - W / 2 - dx) / z + vs.target[0];
    const wy0 = (0 - H / 2 - dy) / z + vs.target[1];
    const wy1 = (H - H / 2 - dy) / z + vs.target[1];

    const sx0 = clamp(Math.floor((wx0 + 0.5) * pxt) - 1, 0, img.width);
    const sx1 = clamp(Math.ceil((wx1 + 0.5) * pxt) + 1, 0, img.width);
    const sy0 = clamp(Math.floor((MAP_H - 0.5 - wy1) * pxt) - 1, 0, img.height);
    const sy1 = clamp(Math.ceil((MAP_H - 0.5 - wy0) * pxt) + 1, 0, img.height);
    if (sx1 <= sx0 || sy1 <= sy0) return;

    c.setTransform(1, 0, 0, 1, 0, 0);
    c.clearRect(0, 0, W, H);
    c.imageSmoothingEnabled = false;               // one texel, one map cell
    c.setTransform(s, 0, 0, -s, ox, oy);
    c.drawImage(img, sx0, sy0, sx1 - sx0, sy1 - sy0, sx0, sy0, sx1 - sx0, sy1 - sy0);

    state.draws++;
  }

  function tick() {
    if (!state.on) { state.raf = 0; return; }
    try { draw(); } catch (e) { state.error = String(e); }
    state.raf = requestAnimationFrame(tick);
  }

  // opacity slider: the texture is a full canvas, so dimming it is a style
  // change on that canvas (and it is re-applied on every draw, which resets
  // cssText)
  function setOpacity(v) {
    v = clamp(v, 0, 1);
    state.opacity = v;
    localStorage.setItem('zz-sea-opacity', String(v));
    const ov = document.getElementById(OV_ID);
    if (ov) ov.style.opacity = String(v);
    const r = document.getElementById('zz-sea-op');
    if (r && Math.abs(r.value / 100 - v) > 0.001) r.value = String(Math.round(v * 100));
  }

  function slider() {
    if (document.getElementById('zz-sea-ui')) return;
    const box = document.createElement('div');
    box.id = 'zz-sea-ui';
    box.style.cssText = [
      'position:fixed', 'right:10px', 'bottom:10px', 'z-index:2147483647',
      'display:flex', 'gap:6px', 'align-items:center',
      'background:rgba(10,16,22,.82)', 'color:#c9d6e2',
      'font:11px/1.4 ui-monospace,monospace', 'padding:5px 8px',
      'border-radius:4px', 'opacity:.5', 'transition:opacity .15s',
      'pointer-events:auto', 'user-select:none',
    ].join(';');
    box.onmouseenter = () => { box.style.opacity = '1'; };
    box.onmouseleave = () => { box.style.opacity = '.5'; };
    const lab = document.createElement('span');
    lab.textContent = 'sea';
    lab.title = 'opacity of the bathymetry drawn behind the map';
    const r = document.createElement('input');
    r.id = 'zz-sea-op';
    r.type = 'range';
    r.min = '0'; r.max = '100'; r.step = '1';
    r.value = String(Math.round(state.opacity * 100));
    r.style.cssText = 'width:96px;accent-color:#4f93bb;cursor:pointer';
    r.title = lab.title;
    r.oninput = () => setOpacity(r.value / 100);
    box.append(lab, r);
    document.body.appendChild(box);
  }

  const img = new Image();
  img.onload = () => { state.img = img; state.sig = ''; if (!state.raf) tick(); slider(); };
  img.onerror = () => { state.error = 'sea texture failed to load'; };
  img.src = SEA_URL;
})();
