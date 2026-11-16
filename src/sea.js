/* Sea depth overlay for the zig~zag exploration map.
 *
 * zig~zag draws its map with deck.gl into #deckgl-overlay, which clears to
 * transparent (the black page background shows through). We insert a canvas
 * directly behind it and paint an ocean into the gaps, using the same
 * OrthographicViewport maths deck uses, read live from React's viewState.
 *
 * Two renderers share one mode switch:
 *   static    - the pre-rendered bathymetry WebP, blitted as before (one texel
 *               per map tile, smoothing off).
 *   animated  - a WebGL2 full-screen triangle running the ported sea shader
 *               (src/sea-glsl.js) over the full-map distance/aux/flow/speed
 *               bakes. Textures load on the first animated request, never for
 *               static. A throwaway context probes WebGL2 and MAX_TEXTURE_SIZE
 *               first; failures (probe, texture load/upload, context loss) fall
 *               back to static with a reason in the menu and a retry.
 */
(() => {
  'use strict';

  const MAP_W = seaConsts.map.w;   // main map extent, in tiles
  const MAP_H = seaConsts.map.h;
  const OV_ID = 'zz-sea';
  const GL_ID = 'zz-sea-gl';
  const WARP_MAX = 3.0;            // tiles of distortion at warp = 1

  const assetBase = window.__ZZ_SEA_ASSET_BASE || undefined;
  const url = name => assetUrl(name, assetBase);
  const errText = e => String((e && e.message) || e);

  // ---- capability probe ----------------------------------------------------
  // A throwaway context reads the texture limit, then is released so the page
  // keeps its context budget. Nothing is fetched for the shader before this.
  function probeShader() {
    try {
      const canvas = document.createElement('canvas');
      const gl = canvas.getContext('webgl2');
      if (!gl) return { webgl2: false, maxTextureSize: 0 };
      const maxTextureSize = Number(gl.getParameter(gl.MAX_TEXTURE_SIZE)) || 0;
      const lose = gl.getExtension('WEBGL_lose_context');
      if (lose) { try { lose.loseContext(); } catch {} }
      return { webgl2: true, maxTextureSize };
    } catch {
      return { webgl2: false, maxTextureSize: 0 };
    }
  }

  const probe = probeShader();
  const canUseShader = shaderCapable(probe.webgl2, probe.maxTextureSize);

  // Saved values live under the zz-sea-* keys; the pure core parses and clamps
  // them.
  const saved = parseState({
    mode: localStorage.getItem('zz-sea-mode'),
    on: localStorage.getItem('zz-sea-on'),
    opacity: localStorage.getItem('zz-sea-opacity'),
    brightness: localStorage.getItem('zz-sea-brightness'),
    fps30: localStorage.getItem('zz-sea-fps'),
  });

  const env = {
    webgl2: probe.webgl2, maxTextureSize: probe.maxTextureSize,
    coarsePointer: !!(window.matchMedia && matchMedia('(pointer: coarse)').matches),
    narrow: window.innerWidth < 700,
  };
  // A saved mode still wins over the weak-device default, but the probe's hard
  // limits beat everything: without a usable shader the overlay starts static
  // and the animated entries are disabled in the menu.
  const startupMode = canUseShader ? pickMode(saved.mode, env) : 'static';

  const state = {
    on: saved.on,
    mode: 'static',                // the live mode; the saved one is applied when ready
    img: null, raf: 0, sig: '', draws: 0,
    opacity: saved.opacity, brightness: saved.brightness, fps30: saved.fps30,
    glReady: false, error: null, loading: false, moving: false,
    colourFormat: null,            // 'avif' or 'webp', whichever decoded
    canUseShader, maxTextureSize: probe.maxTextureSize,
    whalePose: null,               // {x, y, depth, heading} or null (Task 4 feeds it)
  };
  window.__zzSea = state;

  // ---- whale clock and route ----------------------------------------------
  // The whale is a pure function of wall-clock time: a small HEAD to this page
  // gives the server's Date, which anchors performance.now() to one clock for
  // every client. The route is a separate tiny fetch requested only by the
  // first animated build. Neither can fail the sea.
  const CLOCK_TIMEOUT_MS = 1500;
  const CLOCK_EASE_MS = 2000;
  const CLOCK_JUMP_SEC = 1;

  let whaleRoute = null;        // parsed route, cached across context restores
  let whaleLoad = null;         // an in-flight route load
  let whaleEnabled = true;      // __zzSea.whale(false) disables it
  let whalePinSec = null;       // frozen route time, or null for the wall clock
  let clockSource = 'local';    // 'network' after a successful HEAD

  // wall epoch = anchor + performance.now()/1000. Start on the local clock; a
  // large late correction eases from the old anchor over 2 s.
  let wallAnchorFrom = Date.now() / 1000 - performance.now() / 1000;
  let wallAnchorTo = wallAnchorFrom;
  let wallAnchorEaseStart = 0;
  let wallAnchorEasing = false;

  function wallAnchorAt(nowMs) {
    if (!wallAnchorEasing) return wallAnchorTo;
    const u = clamp((nowMs - wallAnchorEaseStart) / CLOCK_EASE_MS, 0, 1);
    if (u >= 1) {
      wallAnchorEasing = false;
      wallAnchorFrom = wallAnchorTo;
      return wallAnchorTo;
    }
    return wallAnchorFrom + (wallAnchorTo - wallAnchorFrom) * u;
  }

  function wallNow(nowMs) {
    return wallAnchorAt(nowMs) + nowMs / 1000;
  }

  function setWallAnchor(anchorSec, source, immediate) {
    if (!isFinite(anchorSec)) return;
    const now = performance.now();
    const current = wallAnchorAt(now);
    clockSource = source;
    if (immediate) {
      wallAnchorFrom = wallAnchorTo = anchorSec;
      wallAnchorEasing = false;
      return;
    }
    // The Date header has second resolution; a sub-second correction is noise,
    // and a larger one eases rather than teleporting the whale.
    if (Math.abs(anchorSec - current) < CLOCK_JUMP_SEC) return;
    wallAnchorFrom = current;
    wallAnchorTo = anchorSec;
    wallAnchorEaseStart = now;
    wallAnchorEasing = true;
  }

  function anchorClock(immediate) {
    const t0 = performance.now();
    let timer = 0;
    try {
      const ctrl = typeof AbortController === 'function' ? new AbortController() : null;
      if (ctrl) timer = setTimeout(() => ctrl.abort(), CLOCK_TIMEOUT_MS);
      fetch(location.href, {
        method: 'HEAD',
        cache: 'no-store',
        signal: ctrl ? ctrl.signal : undefined,
      }).then(res => {
        const t1 = performance.now();
        const date = res.headers.get('Date');
        const serverEpochSec = date ? Date.parse(date) / 1000 : NaN;
        // The abort alone is not a hard deadline: a response whose headers
        // landed just before it can still resolve. Treat anything past the
        // timeout as a failure and keep the local clock.
        if (!isFinite(serverEpochSec) || t1 - t0 > CLOCK_TIMEOUT_MS) {
          throw new Error('clock: HEAD timeout or no Date header');
        }
        const localEpochSec = Date.now() / 1000;
        const serverNow = localEpochSec
          + clockOffset(serverEpochSec, t1 - t0, localEpochSec);
        setWallAnchor(serverNow - t1 / 1000, 'network', immediate);
      }).catch(() => {
        // Keep the local (or previous) anchor; the whale still runs.
      }).finally(() => {
        if (timer) clearTimeout(timer);
      });
    } catch {
      if (timer) clearTimeout(timer);
    }
  }

  function loadWhale() {
    if (whaleRoute) return Promise.resolve(whaleRoute);
    if (whaleLoad) return whaleLoad;
    try {
      whaleLoad = fetch(assetUrl('zigzag-whale.bin', assetBase, seaConsts.whaleVersion))
        .then(res => {
          if (!res.ok) throw new Error(`whale route: HTTP ${res.status}`);
          return res.arrayBuffer();
        })
        .then(buf => {
          whaleRoute = parseWhaleRoute(buf);
          return whaleRoute;
        })
        .catch(() => null)          // a failed route disables the whale only
        .finally(() => { whaleLoad = null; });
    } catch {
      whaleLoad = null;
    }
    return whaleLoad || Promise.resolve(null);
  }

  function updateWhalePose(nowMs) {
    if (state.mode === 'static' || !whaleEnabled || !whaleRoute) {
      state.whalePose = null;
      return;
    }
    const tSec = whalePinSec !== null ? whalePinSec : wallNow(nowMs);
    setWhalePose(sampleWhale(whaleRoute, tSec));
  }

  function whaleStats() {
    const p = state.whalePose;
    return {
      loaded: !!whaleRoute,
      timeSource: clockSource,
      x: p ? p.x : null,
      y: p ? p.y : null,
      depth: p ? p.depth : null,
      heading: p ? p.heading : null,
    };
  }

  // Debug clock/route controls: {t} freezes the route time, false hides the
  // whale, null resumes the wall clock. Returns the same fields as stats().
  function setWhale(arg) {
    if (arg === false) {
      whaleEnabled = false;
      whalePinSec = null;
      state.whalePose = null;
      return whaleStats();
    }
    if (arg === null) {
      whaleEnabled = true;
      whalePinSec = null;
      updateWhalePose(performance.now());
      return whaleStats();
    }
    if (arg && typeof arg === 'object' && arg.t !== undefined) {
      const t = Number(arg.t);
      if (isFinite(t)) {
        whaleEnabled = true;
        whalePinSec = t;
        updateWhalePose(performance.now());
      }
    }
    return whaleStats();
  }


  // The requested mode (menu choice / saved preference), kept across a
  // fallback so context restore and retry know what to rebuild.
  let wantedMode = startupMode;

  // The HUD is mounted lazily (the SPA header may not exist yet); the runtime
  // pushes state changes it makes outside the menu back into it.
  let seaUi = null;
  function refreshUi() {
    if (seaUi) seaUi.refresh(uiState());
  }

  // An animated mode requested before the deck canvas exists. It is applied by
  // the watcher below; it is never turned into `static` just because the app is
  // still rendering.
  let pendingMode = null;
  let deckObserver = null;
  let deckTimer = 0;
  let deckWatchStart = 0;

  // The first animated build takes seconds. Until its first GL frame the 2D
  // canvas stays visible (and keeps repainting with the camera) so the map is
  // never left blank.
  let holdStatic = false;
  const staticVisible = () => state.mode === 'static' || holdStatic;

  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));

  const deckCanvas = () => document.getElementById('deckgl-overlay');

  // The app hands its Deck instance to the map canvas through an imperative
  // handle, not through props, so it has to be found in the fiber tree: the
  // fiber's stateNode may carry it, otherwise a hook's current ref does. Only a
  // Deck whose own canvas is this canvas will do; the find is cached because
  // the fibers are walked on every frame.
  let deckInstance = null;
  function findDeck(canvas) {
    if (deckInstance && deckInstance.canvas === canvas) return deckInstance;
    const key = Object.keys(canvas).find(k => k.startsWith('__reactFiber'));
    if (!key) return null;
    const isDeck = v => !!(v && v.canvas === canvas && v.viewManager);
    for (let f = canvas[key]; f; f = f.return) {
      if (isDeck(f.stateNode && f.stateNode.deck)) return (deckInstance = f.stateNode.deck);
      for (let hook = f.memoizedState; hook; hook = hook.next) {
        if (isDeck(hook.memoizedState && hook.memoizedState.current && hook.memoizedState.current.deck)) {
          return (deckInstance = hook.memoizedState.current.deck);
        }
      }
    }
    return null;
  }

  // The app's splash ("Navigating to the exploration archipelagos") covers the
  // map until its layer store says ready, and MapCanvas carries that state as
  // props: {status, layers, viewState}. The intro must not run underneath it.
  // The fiber is found once per canvas; its memoizedProps are the live props.
  let mapFiber = null;
  let mapFiberCanvas = null;
  function mapStatus() {
    const canvas = deckCanvas();
    if (!canvas) return null;
    if (mapFiberCanvas !== canvas) { mapFiberCanvas = canvas; mapFiber = null; }
    const usable = f => !!(f && f.memoizedProps && 'status' in f.memoizedProps);
    if (!usable(mapFiber)) {
      mapFiber = null;
      const key = Object.keys(canvas).find(k => k.startsWith('__reactFiber'));
      if (!key) return null;
      for (let f = canvas[key]; f; f = f.return) {
        const p = f.memoizedProps;
        if (p && 'status' in p && 'layers' in p && 'viewState' in p) { mapFiber = f; break; }
      }
    }
    if (!mapFiber) return null;
    const s = mapFiber.memoizedProps.status;
    return s === undefined ? null : s;
  }

  // The controller updates deck.viewManager.viewState synchronously, while the
  // app's React state (the viewState prop) commits a frame or more later.
  // Reading the prop made the sea trail the map while panning, so the live
  // manager state wins and the prop is only a fallback. Every camera consumer
  // (2D draw, GL draw, bench, debugRender's live default) goes through here.
  function viewState(canvas) {
    const deck = findDeck(canvas);
    const propsState = () => {
      const key = Object.keys(canvas).find(k => k.startsWith('__reactFiber'));
      if (key) {
        for (let f = canvas[key]; f; f = f.return) {
          const vs = f.memoizedProps && f.memoizedProps.viewState;
          if (vs) return vs;
        }
      }
      return (deck && deck.props && deck.props.viewState) || null;
    };
    const live = deck && deck.viewManager && deck.viewManager.viewState;
    // the controller always publishes a complete viewState: take it as is and
    // leave the React fibers alone
    if (live && live.target && isFinite(live.zoom) && live.padding) {
      return { source: 'viewManager', target: live.target, zoom: live.zoom, padding: live.padding };
    }
    const fallback = propsState() || {};
    // a partial live update still wins for the fields it carries
    if (live && (live.target || isFinite(live.zoom))) {
      return {
        source: 'viewManager',
        target: live.target || fallback.target,
        zoom: isFinite(live.zoom) ? live.zoom : fallback.zoom,
        padding: live.padding || fallback.padding,
      };
    }
    return fallback.target
      ? { source: 'props', target: fallback.target, zoom: fallback.zoom, padding: fallback.padding }
      : null;
  }

  function liveViewState() {
    const c = deckCanvas();
    const vs = c && viewState(c);
    if (!vs) return null;
    return { source: vs.source, target: vs.target, zoom: vs.zoom, padding: vs.padding };
  }

  // ---- static 2D path ------------------------------------------------------

  function overlay(deck) {
    let ov = document.getElementById(OV_ID);
    if (!ov) {
      ov = document.createElement('canvas');
      ov.id = OV_ID;
    }
    // the loading noise sits behind the sea canvas; the 2D placeholder is
    // hidden while it is up during an animated build (the noise replaces it)
    const noise = document.getElementById(NOISE_ID);
    const marker = deck;
    if (ov.parentNode !== deck.parentNode || ov.nextElementSibling !== marker) {
      deck.parentNode.insertBefore(ov, marker);
    }
    ov.style.cssText = (deck.getAttribute('style') || '') + ';pointer-events:none';
    // cssText replaced every style above, including the hide applied by
    // setOn(false); re-apply it so a rebuild while off cannot reveal the sea.
    ov.style.display =
      state.on && !(noise && state.mode !== 'static') ? '' : 'none';
    if (!staticFadeAt) ov.style.opacity = String(state.opacity);
    ov.style.filter = `brightness(${state.brightness})`;
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

  function drawStatic(force) {
    if (!state.on || !staticVisible()) return;
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
    if (!force && sig === state.sig) return;
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

    const t0 = performance.now();
    c.setTransform(1, 0, 0, 1, 0, 0);
    c.clearRect(0, 0, W, H);
    c.imageSmoothingEnabled = false;               // one texel, one map cell
    c.setTransform(s, 0, 0, -s, ox, oy);
    c.drawImage(img, sx0, sy0, sx1 - sx0, sy1 - sy0, sx0, sy0, sx1 - sx0, sy1 - sy0);
    lastDrawMs = performance.now() - t0;
    const now = performance.now();
    staticDraws.push(now);
    while (staticDraws.length && now - staticDraws[0] > 1000) staticDraws.shift();

    state.draws++;

    // The first painted 2D sea is the end of the first load: the noise fades
    // out under it while the canvas itself fades in from transparent.
    if (state.mode === 'static') {
      firstLoad = false;
      if (!staticFadeAt && noiseVisible()) {
        fadeStaticIn(ov);
        hideNoise(NOISE_FADE_MS);
      }
    }

    // A lost WebGL canvas keeps its last composited frame. Hide (never remove)
    // it only now that the 2D replacement has painted, so there is never a
    // frame with no visible sea layer; webglcontextrestored still finds it.
    if (glc && glc.lost && glc.canvas && glc.canvas.style.display !== 'none') {
      glc.canvas.style.display = 'none';
    }
  }

  function staticTick() {
    state.raf = 0;
    if (!state.on || document.hidden || !staticVisible()) return;
    checkDeckRemount();
    try { drawStatic(); } catch (e) { state.error = errText(e); }
    state.raf = requestAnimationFrame(staticTick);
  }

  function startStatic() {
    if (state.raf || !state.on || document.hidden || !staticVisible()) return;
    state.raf = requestAnimationFrame(staticTick);
  }

  function stopStatic() {
    if (state.raf) cancelAnimationFrame(state.raf);
    state.raf = 0;
  }

  function removeStaticCanvas() {
    const ov = document.getElementById(OV_ID);
    if (ov) ov.remove();
  }

  // The colour map is the only texture static needs. It also feeds the GL
  // colour texture, so it is decoded once; releasing it while animated keeps a
  // 40 MPx bitmap out of memory next to the GPU copy.
  // AVIF q95 4:4:4 is the shipped colour map; the old lossy WebP stays as the
  // fallback for any browser that cannot decode AVIF in an <img>.
  const COLOUR_SOURCES = [
    ['avif', 'zigzag-sea.avif'],
    ['webp', 'zigzag-sea.webp'],
  ];
  let colourPromise = null;

  function loadColour() {
    if (state.img) return Promise.resolve(state.img);
    if (colourPromise) return colourPromise;
    colourPromise = new Promise((resolve, reject) => {
      let i = 0;
      const tryNext = () => {
        const [format, name] = COLOUR_SOURCES[i++];
        const img = new Image();
        img.crossOrigin = 'anonymous';
        img.onload = () => {
          colourPromise = null;
          state.colourFormat = format;
          state.img = img;
          state.sig = '';
          if (staticVisible()) startStatic();
          // A ready animated renderer already has the colour pixels in its
          // texture; a pending static/fallback decode must not leave the bitmap
          // resident after an animated mode resumes.
          if (glc && glc.ready && !glc.lost && state.mode !== 'static') state.img = null;
          resolve(img);
        };
        img.onerror = () => {
          if (i < COLOUR_SOURCES.length) { tryNext(); return; }
          colourPromise = null;
          state.error = 'texture: colour';
          hideNoise(0);
          refreshUi();
          reject(new Error(state.error));
        };
        img.src = url(name);
      };
      tryNext();
    });
    return colourPromise;
  }

  // ---- loading noise and the depth reveal ----------------------------------
  // While a first load has no sea layer to look at, a dark pixel-noise canvas
  // sits immediately behind the sea canvas, clipped to the map rectangle. It is
  // refreshed on a ~9 fps timer (not per frame), never takes pointer events and
  // is removed as soon as there is something to show.
  const NOISE_ID = 'zz-sea-noise';
  const NOISE_MS = 110;
  const NOISE_FADE_MS = 400;
  const STATIC_FADE_MS = 600;
  let noiseTimer = 0;
  let noiseFade = 0;
  let noiseBuf = null;
  let staticFadeAt = 0;
  let firstLoad = true;           // no sea layer has been painted yet
  let retryPending = false;

  const noiseVisible = () => !!document.getElementById(NOISE_ID);

  // The intro ramps run once per fresh build that showed the noise. The debug
  // hook can pin them for deterministic sampling.
  const NEUTRAL_RAMPS = { reveal: 1, wave: 1, grain: 1, flow: 1 };
  const ZERO_RAMPS = { reveal: 0, wave: 0, grain: 0, flow: 0 };
  const INTRO_REVEAL_MS = 1600;
  const INTRO_WAVE_START_MS = 1500;
  const INTRO_WAVE_MS = 1400;
  const INTRO_FLOW_MS = 2200;
  let introStart = 0;             // 0 = not running
  let introPending = false;       // set at the first frame of a fresh build
  let introOverride = null;
  let introNoiseHidden = false;

  function introRamps(now) {
    if (introOverride) return introOverride;
    if (!introStart) return NEUTRAL_RAMPS;
    const t = now - introStart;
    return {
      reveal: clamp(t / INTRO_REVEAL_MS, 0, 1),
      wave: clamp((t - INTRO_WAVE_START_MS) / INTRO_WAVE_MS, 0, 1),
      grain: clamp(t / INTRO_REVEAL_MS, 0, 1),
      flow: clamp(t / INTRO_FLOW_MS, 0, 1),
    };
  }

  function startIntro() {
    introStart = 0;
    introPending = true;
    introNoiseHidden = false;
  }

  // The ramps actually in effect. A pending intro has not started: zero until
  // its first frame (the app's splash may still be up), and the noise is kept.
  function liveRamps(now) {
    if (introOverride) return introOverride;
    if (introPending) return ZERO_RAMPS;
    return introRamps(now);
  }

  function stopIntro() {
    introPending = false;
    introStart = 0;
  }

  // `__zzSea.intro({reveal, waveRamp, grainRamp, flowRamp})` pins the ramps;
  // `intro(null)` lets the automatic timeline resume.
  function setIntro(v) {
    if (v === null || v === undefined) {
      introOverride = null;
      return introRamps(performance.now());
    }
    if (typeof v !== 'object') return introRamps(performance.now());
    const n = (x, d) => (isFinite(Number(x)) ? clamp(Number(x), 0, 1) : d);
    introOverride = {
      reveal: n(v.reveal, 1),
      wave: n(v.waveRamp, 1),
      grain: n(v.grainRamp, 1),
      flow: n(v.flowRamp, 1),
    };
    return introOverride;
  }

  function noiseCanvas() {
    let n = document.getElementById(NOISE_ID);
    if (!n) {
      n = document.createElement('canvas');
      n.id = NOISE_ID;
      n.style.pointerEvents = 'none';
    }
    return n;
  }

  function paintNoise() {
    const n = document.getElementById(NOISE_ID);
    const deck = deckCanvas();
    if (!n || !deck || !state.on || document.hidden) { hideNoise(0); return; }
    const W = n.width, H = n.height;
    if (!W || !H) return;
    const c = n.getContext('2d');
    c.setTransform(1, 0, 0, 1, 0, 0);
    c.clearRect(0, 0, W, H);
    c.imageSmoothingEnabled = false;

    // clip to the map rectangle, with the same geometry the 2D path uses
    const vs = viewState(deck);
    const pad = (vs && vs.padding) || {};
    const z = vs ? Math.pow(2, vs.zoom) : 0;
    if (vs && z > 0) {
      const dx = clamp(((pad.left || 0) + W - (pad.right || 0)) / 2, 0, W) - W / 2;
      const dy = clamp(((pad.top || 0) + H - (pad.bottom || 0)) / 2, 0, H) - H / 2;
      const tx = vs.target[0] + 0.5, ty = vs.target[1] + 0.5;
      const ox = W / 2 + dx - tx * z;
      const oy = H / 2 + dy + (MAP_H - 0.5 - ty) * z;
      c.beginPath();
      c.rect(ox, oy - MAP_H * z, MAP_W * z, MAP_H * z);
      c.clip();
    }

    // 1/8 resolution noise, scaled up with smoothing off: blocky, near black
    const nw = Math.max(1, Math.ceil(W / 8)), nh = Math.max(1, Math.ceil(H / 8));
    if (!noiseBuf || noiseBuf.width !== nw || noiseBuf.height !== nh) {
      noiseBuf = document.createElement('canvas');
      noiseBuf.width = nw;
      noiseBuf.height = nh;
    }
    const b = noiseBuf.getContext('2d');
    const img = b.createImageData(nw, nh);
    const d = img.data;
    for (let i = 0; i < d.length; i += 4) {
      const v = 8 + ((Math.random() * 29) | 0);
      d[i] = v; d[i + 1] = v; d[i + 2] = v; d[i + 3] = 255;
    }
    b.putImageData(img, 0, 0);
    c.drawImage(noiseBuf, 0, 0, W, H);
  }

  // The noise sits immediately behind the sea canvas, so it covers neither the
  // map nor the sea and never takes a pointer event.
  function showNoise() {
    if (!state.on) return false;
    const deck = deckCanvas();
    if (!deck || !deck.parentNode) return false;
    if (noiseFade) { clearTimeout(noiseFade); noiseFade = 0; }
    const n = noiseCanvas();
    const gl = document.getElementById(GL_ID);
    const ov = document.getElementById(OV_ID);
    const sea = gl && gl.parentNode === deck.parentNode ? gl
      : ov && ov.parentNode === deck.parentNode ? ov : null;
    const before = sea || deck;
    if (n.parentNode !== deck.parentNode || n.nextElementSibling !== before) {
      deck.parentNode.insertBefore(n, before);
    }
    // same CSS box as the sea canvas
    n.style.cssText = (deck.getAttribute('style') || '') + ';pointer-events:none';
    n.style.opacity = String(state.opacity);
    const w = deck.clientWidth || deck.width;
    const h = deck.clientHeight || deck.height;
    if (n.width !== w || n.height !== h) {
      n.width = w;
      n.height = h;
    }
    if (!noiseTimer) noiseTimer = setInterval(paintNoise, NOISE_MS);
    paintNoise();
    return true;
  }

  function hideNoise(fadeMs) {
    if (noiseTimer) { clearInterval(noiseTimer); noiseTimer = 0; }
    if (noiseFade) { clearTimeout(noiseFade); noiseFade = 0; }
    const n = document.getElementById(NOISE_ID);
    if (!n) return;
    const remove = () => {
      const el = document.getElementById(NOISE_ID);
      if (el) el.remove();
    };
    if (!fadeMs) { remove(); return; }
    const t0 = performance.now();
    const step = () => {
      const el = document.getElementById(NOISE_ID);
      if (!el) return;
      const t = Math.min(1, (performance.now() - t0) / fadeMs);
      el.style.opacity = String(state.opacity * (1 - t));
      if (t < 1) noiseFade = setTimeout(step, 50);
      else { noiseFade = 0; remove(); }
    };
    step();
  }

  // The 2D path has no shader intro: it fades in under the noise.
  function fadeStaticIn(ov) {
    staticFadeAt = performance.now();
    ov.style.opacity = '0';
    const step = () => {
      if (!staticFadeAt) return;
      const t = Math.min(1, (performance.now() - staticFadeAt) / STATIC_FADE_MS);
      if (ov.isConnected) ov.style.opacity = String(state.opacity * t);
      if (t < 1) setTimeout(step, 50);
      else staticFadeAt = 0;
    };
    step();
  }

  // ---- map remounts --------------------------------------------------------
  // The app can tear the map view down and build it again inside the SPA: a new
  // #deckgl-overlay element appears. That is a fresh load for the sea, so the
  // noise and the reveal replay over the new map while the GL textures are
  // reused. Only a change of element identity triggers it.
  let deckAnchor = null;

  function checkDeckRemount() {
    const deck = deckCanvas();
    if (!deck || deck === deckAnchor) return false;
    const first = !deckAnchor;
    deckAnchor = deck;
    if (first) return false;
    // a new map view: repaint into it and replay the first-load sequence
    firstLoad = true;
    state.sig = '';
    hideNoise(0);
    if (state.mode !== 'static' && state.on) {
      showNoise();                        // re-anchors over the new map
      if (glc && glc.ready && !glc.lost) startIntro();   // replay, no rebuild
    }
    return true;
  }

  // ---- GL path -------------------------------------------------------------

  let glc = null;
  let nextAt = 0;                 // fps gate clock, ms; 0 admits the first frame
  let lastDrawnSig = null;        // live view of the last drawn frame

  // The camera fields that make the map move: target, zoom and the side
  // padding. Any change (drag, wheel, controller) opens the adaptive gate.
  function viewSig(vs) {
    const pad = vs.padding || {};
    return [vs.target[0], vs.target[1], vs.zoom, pad.left || 0, pad.right || 0].join('|');
  }

  // ---- diagnostics ---------------------------------------------------------
  // Rings of the last 60 rendered GL frames: the CPU time around renderFrame
  // and the gap between drawn frames. Static keeps its draw timestamps instead.
  // Everything here is read on demand by stats(), never per frame.
  const FRAME_RING = 60;
  const frameMs = [];
  const frameGaps = [];
  let lastFrameAt = 0;
  let lastDrawMs = 0;
  let rendererName = '';          // unmasked WebGL renderer, read once per build
  const staticDraws = [];         // timestamps of 2D draws, last second only

  function pushRing(ring, v) {
    ring.push(v);
    if (ring.length > FRAME_RING) ring.shift();
  }

  const meanOf = a => a.reduce((s, v) => s + v, 0) / a.length;

  function compile(gl, type, src) {
    const s = gl.createShader(type);
    gl.shaderSource(s, src);
    gl.compileShader(s);
    if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) {
      const log = gl.getShaderInfoLog(s);
      gl.deleteShader(s);
      throw new Error('shader compile: ' + log);
    }
    return s;
  }

  function link(gl) {
    const prog = gl.createProgram();
    let vs = null;
    let fs = null;
    try {
      vs = compile(gl, gl.VERTEX_SHADER, VERT);
      fs = compile(gl, gl.FRAGMENT_SHADER, FRAG);
      gl.attachShader(prog, vs);
      gl.attachShader(prog, fs);
      gl.linkProgram(prog);
      if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) {
        throw new Error('shader link: ' + gl.getProgramInfoLog(prog));
      }
      gl.deleteShader(vs);
      gl.deleteShader(fs);
      return prog;
    } catch (e) {
      if (vs) gl.deleteShader(vs);
      if (fs) gl.deleteShader(fs);
      gl.deleteProgram(prog);
      throw e;
    }
  }

  const UNIFORM_NAMES = [
    'uColour', 'uAux', 'uDist', 'uFlow', 'uSpeedTex', 'uCurve', 'uSliceCurve', 'uWarpCurve',
    'uTarget', 'uSize', 'uFlowSize', 'uSpeedSize', 'uAuxSpan', 'uCenter', 'uFlowCell',
    'uZoom', 'uTime', 'uDistMax', 'uShoreW', 'uShelf', 'uFlowSpeed', 'uGrain', 'uFoam',
    'uDeep', 'uSwell', 'uSnap', 'uWaveSpeed', 'uFlowMode', 'uZoomCss', 'uCycle',
    'uDeflect', 'uDeflectReach', 'uTravelMax', 'uWarp', 'uWarpBias', 'uDirA', 'uDirB',
    'uSpeedA', 'uSpeedB', 'uScaleA', 'uScaleB', 'uParallax', 'uDeepSlow', 'uWaveIn',
    'uWaveOut', 'uPomLayers', 'uAlongScale', 'uBrightness', 'uNoiseLayers',
    'uReveal', 'uWhalePose', 'uWhaleShape',
  ];

  // The prototype ellipsoid: 5.5 tiles long, 2.7 across. The shader fixes the
  // vertical radius at 1.35; whaleUniforms adds the surf fade and the on flag.
  const WHALE_SHAPE = { halfLen: 2.75, halfWid: 1.35 };

  // modeSettings().uniforms keys that set one float uniform unchanged; the
  // rest (par, pom, cycle, warp, adir, bdir) are transformed below.
  const PLAIN_UNIFORM = {
    shore: 'uShoreW', shelf: 'uShelf', flow: 'uFlowSpeed', wave: 'uWaveSpeed',
    grain: 'uGrain', foam: 'uFoam', deep: 'uDeep', swell: 'uSwell',
    aspeed: 'uSpeedA', ascale: 'uScaleA', bspeed: 'uSpeedB', bscale: 'uScaleB',
    dslow: 'uDeepSlow', win: 'uWaveIn', wout: 'uWaveOut',
    wbias: 'uWarpBias', deflect: 'uDeflect', deflectReach: 'uDeflectReach',
    flowMode: 'uFlowMode', noiseLayers: 'uNoiseLayers', travelMax: 'uTravelMax',
    snap: 'uSnap',
  };

  function setModeUniforms(gl, uni, u) {
    const rad = deg => deg * Math.PI / 180;
    // iterate the table so its key set is the single source of truth (pinned
    // by test/sea-core.test.js)
    for (const k of Object.keys(u)) {
      switch (k) {
        case 'par': gl.uniform1f(uni.uParallax, -u.par); break;
        case 'pom': gl.uniform1f(uni.uPomLayers, Math.min(12, Math.max(1, u.pom))); break;
        case 'cycle': gl.uniform1f(uni.uCycle, Math.max(0.5, u.cycle)); break;
        case 'warp': gl.uniform1f(uni.uWarp, u.warp * WARP_MAX); break;
        case 'adir': gl.uniform2f(uni.uDirA, Math.cos(rad(u.adir)), Math.sin(rad(u.adir))); break;
        case 'bdir': gl.uniform2f(uni.uDirB, Math.cos(rad(u.bdir)), Math.sin(rad(u.bdir))); break;
        default: {
          const name = PLAIN_UNIFORM[k];
          if (name) gl.uniform1f(uni[name], u[k]);
        }
      }
    }
  }

  // Fetch + decode off the main thread where the browser supports it. The four
  // shader textures are 40 MPx; Image decoding on the main thread would stall
  // the page during the first animated switch.
  function decodeTexture(src) {
    if (typeof createImageBitmap === 'function') {
      return fetch(src, { mode: 'cors' })
        .then(r => {
          if (!r.ok) throw new Error(`texture: ${src} HTTP ${r.status}`);
          return r.blob();
        })
        .then(b => createImageBitmap(b));
    }
    return new Promise((resolve, reject) => {
      const img = new Image();
      img.crossOrigin = 'anonymous';
      img.onload = () => resolve(img);
      img.onerror = () => reject(new Error('texture: failed to load ' + src));
      img.src = src;
    });
  }

  // The WebP bakes decode to RGBA, but only the channels the shader reads need
  // to reach the GPU. A sized internal format stores exactly those: RGBA8 for
  // the colour map, R8 for the two scalar fields, RGB8 for the multi-channel
  // ones. (Plain RGBA was ~377 MB; this is ~237 MB with the colour map.)
  const TEX_FORMAT = {
    colour: ['RGBA8', 'RGBA'],
    dist: ['R8', 'RED'],            // shader reads .r
    aux: ['RGB8', 'RGB'],           // .r depth, .gb along
    flow: ['RGB8', 'RGB'],          // .rg direction, .b the coarse-only speed
    speed: ['R8', 'RED'],           // shader reads .r
  };

  function uploadTexture(gl, source, nearest, key) {
    const [internal, format] = TEX_FORMAT[key];
    const tex = gl.createTexture();
    const f = nearest ? gl.NEAREST : gl.LINEAR;
    gl.bindTexture(gl.TEXTURE_2D, tex);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, f);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, f);
    gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);   // required for R8/RGB8 rows
    gl.pixelStorei(gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL, false);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl[internal], gl[format], gl.UNSIGNED_BYTE, source);
    // A browser that rejects this internalformat/source pair must fail loudly:
    // silently uploading RGBA instead would keep 377 MB of VRAM.
    const err = gl.getError();
    const w = source.width;
    const h = source.height;
    if (typeof source.close === 'function') source.close();
    if (err !== gl.NO_ERROR) {
      gl.deleteTexture(tex);
      throw new Error(`texture: ${key} upload GL error 0x${err.toString(16)}`);
    }
    return { tex, w, h };
  }

  // 5-point polyline sampled linearly at 128 texels, as np.interp would
  function curveTexture(gl, pts) {
    const lut = new Uint8Array(128 * 4);
    for (let i = 0; i < 128; i++) {
      const x = (i / 127) * (pts.length - 1);
      const k = Math.min(pts.length - 2, Math.floor(x));
      const f = x - k;
      const v = Math.round((pts[k] * (1 - f) + pts[k + 1] * f) * 255);
      lut[i * 4] = lut[i * 4 + 1] = lut[i * 4 + 2] = v;
      lut[i * 4 + 3] = 255;
    }
    const tex = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, tex);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, 128, 1, 0, gl.RGBA, gl.UNSIGNED_BYTE, lut);
    return tex;
  }

  function drainErrors(gl) {
    for (let i = 0; i < 16 && gl.getError() !== gl.NO_ERROR; i++) { /* clear sticky errors */ }
  }

  function buildGL() {
    const deck = deckCanvas();
    if (!deck || !deck.parentNode) throw new Error('no deck canvas');
    if (!canUseShader) {
      throw new Error('shader unavailable: MAX_TEXTURE_SIZE ' + probe.maxTextureSize);
    }

    // A lost context's canvas is kept for webglcontextrestored; a rebuild owns
    // the slot again and the old one goes.
    const stale = document.getElementById(GL_ID);
    if (stale) stale.remove();

    const canvas = document.createElement('canvas');
    canvas.id = GL_ID;
    if (!state.on) canvas.style.display = 'none';
    deck.parentNode.insertBefore(canvas, deck);

    const gl = canvas.getContext('webgl2', {
      alpha: true, premultipliedAlpha: false, antialias: false, depth: false,
    });
    if (!gl) { canvas.remove(); throw new Error('webgl2 unavailable'); }

    // one read of the unmasked renderer per build; the diagnostics shorten it
    rendererName = '';
    frameMs.length = 0;
    frameGaps.length = 0;
    lastFrameAt = 0;
    try {
      const info = gl.getExtension('WEBGL_debug_renderer_info');
      if (info) rendererName = String(gl.getParameter(info.UNMASKED_RENDERER_WEBGL) || '');
    } catch { rendererName = ''; }

    let prog;
    try { prog = link(gl); } catch (e) { canvas.remove(); throw e; }
    const uni = {};
    for (const name of UNIFORM_NAMES) uni[name] = gl.getUniformLocation(prog, name);

    gl.useProgram(prog);
    gl.uniform1i(uni.uColour, 0);
    gl.uniform1i(uni.uAux, 1);
    gl.uniform1i(uni.uDist, 2);
    gl.uniform1i(uni.uFlow, 3);
    gl.uniform1i(uni.uSpeedTex, 4);
    gl.uniform1i(uni.uCurve, 5);
    gl.uniform1i(uni.uSliceCurve, 6);
    gl.uniform1i(uni.uWarpCurve, 7);

    const c = {
      canvas, gl, prog, uni,
      tex: {
        curve: curveTexture(gl, seaConsts.curves.pc),
        slice: curveTexture(gl, seaConsts.curves.sc),
        warp: curveTexture(gl, seaConsts.curves.wc),
      },
      size: {}, raf: 0, ready: false, css: '', lost: false, first: false,
      onLost: null, onRestored: null,
    };
    glc = c;
    c.onLost = e => onContextLost(c, e);
    c.onRestored = () => onContextRestored(c);
    canvas.addEventListener('webglcontextlost', c.onLost);
    canvas.addEventListener('webglcontextrestored', c.onRestored);

    drainErrors(gl);
    state.loading = true;
    refreshUi();

    // The whale route is a side load: outside the texture Promise.all, and a
    // failure disables only the whale (never state.error or static fallback).
    loadWhale();

    // A first load (or a retry after a failure) has no sea layer to show, so
    // put the noise up and run the depth reveal once the textures are in.
    // Later builds — a static→animated switch or a context-loss restore — keep
    // whatever is on screen and start at full reveal.
    const noisy = (firstLoad || retryPending) && showNoise();
    retryPending = false;
    if (!noisy) stopIntro();

    // colour NEAREST (1 texel per tile); aux/flow/speed LINEAR; dist NEAREST
    // (always sampled on a texel centre because snap is on)
    const specs = [
      ['colour', 'zigzag-sea.webp', true],
      ['dist', 'zigzag-sea-dist.webp', true],
      ['aux', 'zigzag-sea-aux.webp', false],
      ['flow', 'zigzag-sea-flow.webp', false],
      ['speed', 'zigzag-sea-speed.webp', false],
    ];
    Promise.all(specs.map(([key, name, nearest]) =>
      // The colour decode is shared with the 2D path; a second fetch of the
      // same 40 MPx WebP is both slow and memory.
      (key === 'colour' ? loadColour() : decodeTexture(url(name)))
        .then(source => {
          if (glc !== c || c.lost) {
            if (typeof source.close === 'function') source.close();
            return;
          }
          const up = uploadTexture(gl, source, nearest, key);
          c.tex[key] = up.tex;
          c.size[key] = [up.w, up.h];
        }),
    )).then(() => {
      if (glc !== c || c.lost) return;
      // No silent ready: an upload error or an oversized texture means the
      // device cannot hold this mode, even though WebGL2 exists.
      const glErr = gl.getError();
      if (glErr !== gl.NO_ERROR) {
        throw new Error('texture: GL error 0x' + glErr.toString(16));
      }
      const max = Number(gl.getParameter(gl.MAX_TEXTURE_SIZE)) || probe.maxTextureSize;
      for (const key of ['colour', 'dist', 'aux', 'flow', 'speed']) {
        const [w, h] = c.size[key] || [0, 0];
        if (!w || !h || w > max || h > max) {
          throw new Error(`texture: ${key} ${w}x${h} > MAX_TEXTURE_SIZE ${max}`);
        }
      }
      c.ready = true;
      state.glReady = true;
      state.loading = false;
      state.error = null;
      if (noisy) startIntro();
      startGL();
      refreshUi();
    }).catch(e => {
      state.loading = false;
      if (glc === c && !c.lost) fallbackToStatic(e);
      else refreshUi();
    });
  }

  // Paint the 2D path over a lost context. drawStatic hides the lost canvas
  // only after it has actually painted; until then the retained GL frame is
  // the visible sea.
  function paintStaticOverLost() {
    state.sig = '';
    try { drawStatic(); } catch (e) { state.error = errText(e); }
    startStatic();
  }

  // A lost context keeps its canvas and listeners so the browser can restore
  // it. The live mode falls back immediately; the requested animated mode is
  // remembered for the restored event.
  function onContextLost(c, e) {
    if (e && e.preventDefault) e.preventDefault();
    if (glc !== c || c.lost) return;
    c.lost = true;
    c.ready = false;
    stopGL();
    state.glReady = false;
    state.loading = false;
    state.mode = 'static';
    state.sig = '';
    holdStatic = false;
    state.whalePose = null;
    state.error = 'context lost' +
      (e && e.statusMessage ? ': ' + e.statusMessage : '');
    // The lost canvas still shows its last composited frame. Keep it visible
    // until the 2D replacement paints; drawStatic hides it after that.
    if (state.img) {
      paintStaticOverLost();
    } else {
      loadColour().then(() => {
        if (glc !== c || !c.lost) return;
        paintStaticOverLost();
      }).catch(err => {
        if (glc === c) { state.error = errText(err); refreshUi(); }
      });
    }
    refreshUi();
  }

  function onContextRestored(c) {
    if (glc !== c) return;
    const canvas = c.canvas;
    glc = null;
    c.ready = false;
    state.glReady = false;
    if (canvas) {
      canvas.removeEventListener('webglcontextlost', c.onLost);
      canvas.removeEventListener('webglcontextrestored', c.onRestored);
      canvas.remove();
    }
    if (!canUseShader) { refreshUi(); return; }
    if (!wantedMode || wantedMode === 'static') {
      state.error = null;
      refreshUi();
      return;
    }
    try { setMode(wantedMode); } catch (e) { fallbackToStatic(e); }
  }

  function releaseGL() {
    if (!glc) return;
    const c = glc;
    glc = null;
    lastDrawnSig = null;
    if (c.raf) cancelAnimationFrame(c.raf);
    state.glReady = false;
    if (c.canvas) {
      if (c.onLost) c.canvas.removeEventListener('webglcontextlost', c.onLost);
      if (c.onRestored) c.canvas.removeEventListener('webglcontextrestored', c.onRestored);
    }
    try { c.gl.deleteProgram(c.prog); } catch {}
    for (const key of Object.keys(c.tex || {})) {
      try { c.gl.deleteTexture(c.tex[key]); } catch {}
    }
    if (!c.lost) {
      const lose = c.gl.getExtension('WEBGL_lose_context');
      if (lose) { try { lose.loseContext(); } catch {} }
    }
    if (c.canvas) c.canvas.remove();
  }

  function renderFrame(c, w, h, cam, t, settings, ramps) {
    const { gl, uni, tex, size } = c;
    gl.viewport(0, 0, w, h);
    gl.useProgram(c.prog);
    gl.clearColor(0, 0, 0, 0);
    gl.clear(gl.COLOR_BUFFER_BIT);

    gl.uniform2f(uni.uTarget, cam.tx, cam.ty);
    gl.uniform2f(uni.uSize, MAP_W, MAP_H);
    gl.uniform2f(uni.uFlowSize, size.flow[0], size.flow[1]);
    gl.uniform1f(uni.uFlowCell, MAP_W / size.flow[0]);
    gl.uniform2f(uni.uSpeedSize, size.speed[0], size.speed[1]);
    gl.uniform2f(uni.uAuxSpan, size.aux[0] * 2, size.aux[1] * 2);
    gl.uniform2f(uni.uCenter, cam.centerX, cam.centerY);
    gl.uniform1f(uni.uZoom, cam.zoom);
    gl.uniform1f(uni.uZoomCss, cam.zoomCss);
    gl.uniform1f(uni.uTime, t);
    gl.uniform1f(uni.uDistMax, seaConsts.distMax);
    gl.uniform1f(uni.uAlongScale, seaConsts.alongScale);
    gl.uniform1f(uni.uBrightness, state.brightness);
    setModeUniforms(gl, uni, settings.uniforms);

    // The whale is two vec4s: a pose and the shape/on gate. No pose packs a
    // zeroed pose with on = 0, so the shader skips the block entirely.
    const whale = whaleUniforms(state.whalePose, WHALE_SHAPE);
    gl.uniform4fv(uni.uWhalePose, whale.pose);
    gl.uniform4fv(uni.uWhaleShape, whale.shape);

    // intro ramps: the sea reveals by depth, the waves come in behind it, the
    // grain fades up with the reveal and the drift starts fast, settling to
    // the tuned value. At rest these are all 1 and change nothing.
    const r = ramps || NEUTRAL_RAMPS;
    const u = settings.uniforms;
    gl.uniform1f(uni.uReveal, r.reveal);
    if (r.wave < 1) {
      gl.uniform1f(uni.uWaveIn, u.win * r.wave);
      gl.uniform1f(uni.uWaveOut, u.wout * r.wave);
      gl.uniform1f(uni.uFoam, u.foam * r.wave);
    }
    if (r.grain < 1) gl.uniform1f(uni.uGrain, u.grain * r.grain);
    if (r.flow < 1) {
      const k = 1 + 0.5 * (1 - r.flow);
      gl.uniform1f(uni.uFlowSpeed, u.flow * k);
      gl.uniform1f(uni.uSpeedA, u.aspeed * k);
      gl.uniform1f(uni.uSpeedB, u.bspeed * k);
    }

    gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, tex.colour);
    gl.activeTexture(gl.TEXTURE1); gl.bindTexture(gl.TEXTURE_2D, tex.aux);
    gl.activeTexture(gl.TEXTURE2); gl.bindTexture(gl.TEXTURE_2D, tex.dist);
    gl.activeTexture(gl.TEXTURE3); gl.bindTexture(gl.TEXTURE_2D, tex.flow);
    gl.activeTexture(gl.TEXTURE4); gl.bindTexture(gl.TEXTURE_2D, tex.speed);
    gl.activeTexture(gl.TEXTURE5); gl.bindTexture(gl.TEXTURE_2D, tex.curve);
    gl.activeTexture(gl.TEXTURE6); gl.bindTexture(gl.TEXTURE_2D, tex.slice);
    gl.activeTexture(gl.TEXTURE7); gl.bindTexture(gl.TEXTURE_2D, tex.warp);

    gl.drawArrays(gl.TRIANGLES, 0, 3);
  }

  // The backing store follows the deck CSS size and the tier's render scale.
  // Only the drawing buffer changes on a tier switch; the program and every
  // texture survive, so the next frame redraws with new uniforms alone.
  function sizeGL(renderScale) {
    if (!glc) return null;
    const deck = deckCanvas();
    if (!deck) return null;
    const cssW = deck.clientWidth || deck.width;
    const cssH = deck.clientHeight || deck.height;
    if (!cssW || !cssH) return null;

    const { w, h, scale } = renderSize(
      cssW, cssH, window.devicePixelRatio || 1, renderScale,
    );
    const canvas = glc.canvas;
    if (canvas.width !== w || canvas.height !== h) {
      canvas.width = w;
      canvas.height = h;
    }
    return { cssW, cssH, w, h, scale };
  }

  // deck's target[1] is the map's page row (larger = further down the page)
  // and the bakes store the map with south at row 0, so the shader's texture
  // row is MAP_H - 1 - target.y. x is a pixel centre, as the 2D path uses.
  // debugRender takes texture rows directly and never goes through this.
  const seaTarget = vs => [vs.target[0] + 0.5, MAP_H - 1 - vs.target[1]];

  function drawGL(t) {
    if (!glc || !glc.ready || glc.lost) return;
    const deck = deckCanvas();
    if (!deck) return;
    const vs = viewState(deck);
    if (!vs) return;

    const settings = modeSettings(state.mode);
    const sized = sizeGL(settings.renderScale);
    if (!sized) return;
    const { cssW, cssH, w, h, scale } = sized;

    const canvas = glc.canvas;
    if (canvas.parentNode !== deck.parentNode || canvas.nextElementSibling !== deck) {
      deck.parentNode.insertBefore(canvas, deck);
    }
    const css = (deck.getAttribute('style') || '') +
      ';pointer-events:none;opacity:' + state.opacity +
      (state.on ? '' : ';display:none');
    if (glc.css !== css) {
      glc.css = css;
      canvas.style.cssText = css;
    }

    const cam = cameraUniforms(vs, cssW, cssH, w, h);
    const target = seaTarget(vs);
    const now = performance.now();
    // The intro waits for the app's splash to lift: until the layer status is
    // ready (or unknown, as a fallback) the ramps stay at 0 and the noise stays
    // up, so the reveal is not spent behind the splash.
    if (introPending) {
      const status = mapStatus();
      if (status === null || status === 'ready') {
        introPending = false;
        introStart = now;
      }
    }
    const ramps = liveRamps(now);
    const t0 = now;
    // Live whale time comes from the wall clock anchor, or the debug pin. The
    // shader's own uTime stays seconds since load (waves/reveal).
    updateWhalePose(now);
    renderFrame(glc, w, h, {
      tx: target[0], ty: target[1],
      zoom: cam.zoomPx, zoomCss: cam.zoomPx / scale,
      centerX: cam.centerX, centerY: cam.centerY,
    }, t, settings, ramps);
    pushRing(frameMs, performance.now() - t0);
    state.draws++;
    lastDrawnSig = viewSig(vs);

    // Once the reveal has finished the sea stands on its own: fade the noise
    // out underneath it and drop it.
    if (!introNoiseHidden && ramps.reveal >= 1 && noiseVisible()) {
      introNoiseHidden = true;
      hideNoise(NOISE_FADE_MS);
    }

    // The first real frame means the 2D canvas can go: the opaque-enough water
    // is already in the drawing buffer, so removing it cannot show a blank.
    if (!glc.first && state.on) {
      glc.first = true;
      firstLoad = false;
      holdStatic = false;
      removeStaticCanvas();
    }
    // Any live animated frame owns the colour pixels; holding the decoded
    // bitmap as well keeps ~163 MB resident. This also covers a fast
    // static -> animated round trip where a pending static decode resolves
    // after the animated loop has resumed.
    if (state.img && state.mode !== 'static') state.img = null;
  }

  function glTick(now) {
    if (!glc) return;
    glc.raf = 0;
    if (glc.lost || state.mode === 'static' || !state.on || document.hidden) return;
    try {
      checkDeckRemount();
      // The loop still runs every rAF; the adaptive gate decides which frames
      // draw. Movement is measured against the last *drawn* camera, so skipped
      // still frames cannot hide a pan.
      const deck = deckCanvas();
      const vs = deck && viewState(deck);
      const sig = vs ? viewSig(vs) : null;
      state.moving = !!sig && lastDrawnSig !== null && sig !== lastDrawnSig;
      const gate = fpsGate(now, nextAt, state.fps30, state.moving);
      nextAt = gate.nextAt;
      if (gate.draw) {
        if (lastFrameAt) pushRing(frameGaps, now - lastFrameAt);
        lastFrameAt = now;
        drawGL(now / 1000);
      }
    } catch (e) {
      // A GL exception (including a lost context surfacing as one) is a
      // fallback, never a page error.
      fallbackToStatic(e);
      return;
    }
    if (glc) glc.raf = requestAnimationFrame(glTick);
  }

  function startGL() {
    if (!glc || glc.raf || !glc.ready || glc.lost) return;
    if (state.mode === 'static' || !state.on || document.hidden) return;
    glc.raf = requestAnimationFrame(glTick);
  }

  function stopGL() {
    if (glc && glc.raf) cancelAnimationFrame(glc.raf);
    if (glc) glc.raf = 0;
  }

  // The animated renderer is gone; paint the 2D path again. While an
  // animated build was loading, its canvases and the decoded colour image are
  // still around, so this repaints without a gap. Otherwise the colour decode
  // is requested and the dead canvas is dropped once it can be replaced.
  function fallbackToStatic(reason) {
    state.error = errText(reason);
    state.loading = false;
    pendingMode = null;
    state.mode = 'static';
    state.sig = '';
    holdStatic = false;
    state.whalePose = null;
    if (glc && glc.lost) {
      // keep the lost canvas alive for webglcontextrestored; it stays visible
      // until the 2D path paints over it
      stopGL();
      if (state.img) {
        paintStaticOverLost();
      } else {
        loadColour().then(() => {
          if (glc && glc.lost) paintStaticOverLost();
        }).catch(e => {
          state.error = errText(e);
          refreshUi();
        });
      }
      refreshUi();
      return;
    }
    if (state.img) {
      releaseGL();
      try { drawStatic(); } catch (e) { state.error = errText(e); }
      startStatic();
      refreshUi();
      return;
    }
    // The colour image was released while animated: keep the last GL frame up
    // until the cached WebP decodes, then swap the 2D canvas in. A token drops
    // the swap if a newer mode change has taken over.
    const token = ++staticToken;
    stopGL();
    loadColour().then(() => {
      if (token !== staticToken || state.mode !== 'static') return;
      releaseGL();
      state.sig = '';
      try { drawStatic(); } catch (e) { state.error = errText(e); }
      startStatic();
      refreshUi();
    }).catch(e => {
      if (token !== staticToken) return;
      releaseGL();
      state.error = errText(e);
      refreshUi();
    });
  }

  // Switching to static from a running animated mode: the decoded colour image
  // is gone, so the GL canvas stays up until the cached image can replace it.
  let staticToken = 0;

  function enterStatic() {
    const token = ++staticToken;
    stopStatic();
    state.sig = '';
    holdStatic = false;
    if (state.img) {
      releaseGL();
      try { drawStatic(); } catch (e) { state.error = errText(e); }
      startStatic();
      return;
    }
    stopGL();
    loadColour().then(() => {
      if (token !== staticToken || state.mode !== 'static') return;
      releaseGL();
      state.sig = '';
      try { drawStatic(); } catch (e) { state.error = errText(e); }
      startStatic();
      refreshUi();
    }).catch(e => {
      if (token !== staticToken) return;
      releaseGL();
      state.error = errText(e);
      refreshUi();
    });
  }

  // ---- mode switch ---------------------------------------------------------

  function setMode(id) {
    modeSettings(id);                       // validates, throws on unknown ids
    if (id !== 'static' && !canUseShader) {
      wantedMode = id;
      state.error = 'shader unavailable: MAX_TEXTURE_SIZE ' + probe.maxTextureSize;
      state.loading = false;
      state.mode = 'static';
      state.sig = '';
      holdStatic = false;
      state.whalePose = null;
      if (state.img) {
        try { drawStatic(); } catch (e) { state.error = errText(e); }
        startStatic();
      }
      refreshUi();
      return state.mode;
    }

    wantedMode = id;
    if (state.mode === id && id !== 'static' && glc && glc.ready && !glc.lost) return id;

    state.mode = id;
    state.sig = '';
    if (id === 'static') {
      state.error = null;
      state.loading = false;
      state.whalePose = null;         // static draws no shader, so no whale
      enterStatic();
    } else {
      state.error = null;
      stopStatic();
      // Tier switches reuse the program and textures; the first build keeps
      // the 2D canvas visible until a GL frame exists.
      if (glc && glc.ready && !glc.lost) {
        try {
          sizeGL(modeSettings(id).renderScale);
          nextAt = 0;                       // paint on this tick, not the next
          drawGL(performance.now() / 1000);
        } catch (e) {
          fallbackToStatic(e);
          return state.mode;
        }
        startGL();
      } else if (glc && !glc.lost) {
        // a build is already in flight; it will render with the current mode
        startStatic();
        startGL();
      } else {
        holdStatic = true;
        try { buildGL(); } catch (e) { fallbackToStatic(e); }
        // keep the 2D overlay following the camera while the textures load
        startStatic();
      }
    }
    refreshUi();
    return state.mode;
  }

  // ---- deferred mode and the deck watcher ---------------------------------
  // The userscript can start before the SPA has rendered #deckgl-overlay. An
  // animated mode requested then waits here; it is applied as soon as the deck
  // appears, and the saved preference is never rewritten to static for it.

  function stopDeckWatch() {
    if (deckObserver) { deckObserver.disconnect(); deckObserver = null; }
    if (deckTimer) { clearInterval(deckTimer); deckTimer = 0; }
  }

  function applyPendingMode() {
    if (!pendingMode || !state.on) return false;
    if (pendingMode !== 'static' && !deckCanvas()) return false;
    const id = pendingMode;
    pendingMode = null;
    stopDeckWatch();
    try { setMode(id); } catch (e) { fallbackToStatic(e); }
    return true;
  }

  function watchForDeck() {
    if (deckObserver) return;
    deckWatchStart = Date.now();
    const target = document.body || document.documentElement;
    deckObserver = new MutationObserver(() => {
      if (applyPendingMode()) stopDeckWatch();
    });
    deckObserver.observe(target, { childList: true, subtree: true });
    deckTimer = setInterval(() => {
      if (applyPendingMode() || Date.now() - deckWatchStart >= 30000) {
        if (deckTimer) { clearInterval(deckTimer); deckTimer = 0; }
      }
    }, 500);
  }

  // The menu's mode choice: persist the choice, not whatever the live mode
  // settles on if the app is not ready yet.
  function requestMode(id) {
    modeSettings(id);                       // validates, throws on unknown ids
    wantedMode = id;
    if (id !== 'static' && !deckCanvas()) {
      pendingMode = id;
      watchForDeck();
      applyPendingMode();                   // in case it arrived just now
      refreshUi();
      return state.mode;
    }
    pendingMode = null;
    stopDeckWatch();
    try { return setMode(id); } catch (e) { fallbackToStatic(e); return state.mode; }
  }

  // Retry the animated mode that failed. The saved/requested id was left
  // intact by fallbackToStatic, so a texture or context failure can be tried
  // again after the network or GPU recovers.
  function retry() {
    if (!canUseShader) {
      state.error = 'shader unavailable: MAX_TEXTURE_SIZE ' + probe.maxTextureSize;
      refreshUi();
      return state.mode;
    }
    const target = wantedMode && wantedMode !== 'static'
      ? wantedMode
      : pickMode('', env);
    retryPending = true;       // a retry has nothing on screen: noise + reveal
    return requestMode(target);
  }

  // One frame at an exact size and camera, read back synchronously. Used by the
  // parity check: it is the camera, not the live viewport, that decides.
  function debugRender(opts) {
    opts = opts || {};
    if (!glc || !glc.ready || glc.lost) throw new Error('gl path not ready');
    // no tx/ty: render the live view through the same camera the loop uses
    let live = null;
    if (opts.tx === undefined && opts.ty === undefined) {
      const c = deckCanvas();
      const vs = c && viewState(c);
      if (!vs) throw new Error('debugRender has no live camera');
      live = seaTarget(vs);
      opts = {
        ...opts,
        zoom: opts.zoom === undefined ? 2 ** vs.zoom : opts.zoom,
        width: opts.width === undefined ? (c.clientWidth || c.width) : opts.width,
        height: opts.height === undefined ? (c.clientHeight || c.height) : opts.height,
      };
    }
    const w = Math.max(1, Math.round(Number(opts.width)));
    const h = Math.max(1, Math.round(Number(opts.height)));
    const zoom = Number(opts.zoom);
    const t = opts.t === undefined ? 0 : Number(opts.t);
    const tx = live ? live[0] : Number(opts.tx);
    const ty = live ? live[1] : Number(opts.ty);
    if (![w, h, zoom, t, tx, ty].every(isFinite)) {
      throw new Error('debugRender needs finite tx, ty, width, height, zoom');
    }

    const canvas = glc.canvas;
    const prevW = canvas.width, prevH = canvas.height;
    const prevPose = state.whalePose;
    // width/height are the camera extent in CSS-style pixels; extra-low renders
    // the same view into half the backing store, as the live tier does
    const settings = modeSettings(state.mode);
    const scale = settings.renderScale;
    const bw = Math.max(1, Math.round(w * scale));
    const bh = Math.max(1, Math.round(h * scale));
    try {
      // Parity frames stay bit-identical: the whale is off unless this call
      // pins it. `whaleT` samples the route at that time; `whale: true` keeps
      // the pose supplied through setWhalePose; a pose object (or the
      // `whalePose` alias) renders it for this frame only.
      if (opts.whaleT !== undefined) {
        const whaleT = Number(opts.whaleT);
        if (!isFinite(whaleT)) {
          throw new Error('debugRender needs a finite whaleT');
        }
        if (!whaleRoute) throw new Error('whale route not loaded');
        setWhalePose(sampleWhale(whaleRoute, whaleT));
      } else {
        const pin = opts.whale !== undefined ? opts.whale : opts.whalePose;
        if (pin && typeof pin === 'object') {
          setWhalePose(pin);
        } else if (pin !== true) {
          state.whalePose = null;
        }
      }
      if (canvas.width !== bw || canvas.height !== bh) {
        canvas.width = bw;
        canvas.height = bh;
      }
      renderFrame(glc, bw, bh, {
        tx, ty,
        zoom: zoom * scale, zoomCss: zoom,
        centerX: bw / 2, centerY: bh / 2,
      }, t, settings, liveRamps(performance.now()));

      const px = new Uint8Array(bw * bh * 4);
      glc.gl.readPixels(0, 0, bw, bh, glc.gl.RGBA, glc.gl.UNSIGNED_BYTE, px);

      // flip the bottom-up GL rows into a top-down 2D canvas. readPixels
      // returns the straight-alpha bytes the shader wrote (the context is not
      // premultiplied); putImageData takes straight alpha, so toDataURL keeps
      // the transparent margin transparent instead of flattening it.
      const out = document.createElement('canvas');
      out.width = bw;
      out.height = bh;
      const ctx = out.getContext('2d');
      const img = ctx.createImageData(bw, bh);
      const row = bw * 4;
      for (let y = 0; y < bh; y++) {
        img.data.set(px.subarray((bh - 1 - y) * row, (bh - y) * row), y * row);
      }
      ctx.putImageData(img, 0, 0);
      return out.toDataURL('image/png');
    } finally {
      state.whalePose = prevPose;
      if (canvas.width !== prevW || canvas.height !== prevH) {
        canvas.width = prevW;
        canvas.height = prevH;
      }
      if (state.mode !== 'static' && glc && glc.ready && !glc.lost) {
        try { drawGL(performance.now() / 1000); } catch (e) { state.error = errText(e); }
      }
    }
  }

  // gl.finish() is a no-op on some drivers (headless SwiftShader included), so
  // a one-pixel readback is the portable way to wait for queued frames.
  function syncGL(gl) {
    gl.readPixels(0, 0, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, new Uint8Array(4));
  }

  // 60 frames at the live camera and backing size, then hand the frame back to
  // the loop. Static has no animation clock, so it measures 60 forced 2D
  // blits of the current view instead.
  function bench() {
    const mode = state.mode;
    try {
      if (mode === 'static') {
        if (!state.img) throw new Error('static not ready');
        const t0 = performance.now();
        for (let i = 0; i < 60; i++) drawStatic(true);
        const ms = performance.now() - t0;
        startStatic();
        return { mode, ms, fps: ms > 0 ? 60000 / ms : 0 };
      }

      if (!glc || !glc.ready || glc.lost) throw new Error('gl path not ready');
      const deck = deckCanvas();
      const vs = deck && viewState(deck);
      if (!vs) throw new Error('no camera');
      const settings = modeSettings(mode);
      const sized = sizeGL(settings.renderScale);
      if (!sized) throw new Error('no viewport');

      const cam = cameraUniforms(vs, sized.cssW, sized.cssH, sized.w, sized.h);
      const target = seaTarget(vs);
      const frame = {
        tx: target[0], ty: target[1],
        zoom: cam.zoomPx, zoomCss: cam.zoomPx / sized.scale,
        centerX: cam.centerX, centerY: cam.centerY,
      };
      const start = performance.now();
      let ms;
      try {
        // Warm up, then wait for the GPU so the clock measures work instead of
        // command submission.
        updateWhalePose(performance.now());
        renderFrame(glc, sized.w, sized.h, frame, start / 1000, settings,
          liveRamps(start));
        syncGL(glc.gl);
        const t0 = performance.now();
        for (let i = 0; i < 60; i++) {
          updateWhalePose(performance.now());
          renderFrame(glc, sized.w, sized.h, frame, start / 1000 + i / 60, settings,
            liveRamps(start));
        }
        syncGL(glc.gl);
        ms = performance.now() - t0;
      } finally {
        // Restore the live frame and loop whatever happened during the run.
        nextAt = 0;
        try { drawGL(performance.now() / 1000); } catch (e) { fallbackToStatic(e); }
        startGL();
      }
      return { mode: state.mode, ms, fps: ms > 0 ? 60000 / ms : 0 };
    } catch (e) {
      state.error = errText(e);
      refreshUi();
      return { mode: state.mode, ms: 0, fps: 0, error: state.error };
    }
  }

  // ---- diagnostics ---------------------------------------------------------
  // The unmasked renderer string is long to the point of uselessness in a 190 px
  // panel: "ANGLE (Intel, Intel(R) UHD Graphics 630 ...)" reads as "ANGLE
  // (Intel)", and anything longer than 40 chars is clipped.
  function shortRenderer(raw) {
    if (!raw) return 'WebGL2';
    const m = /^ANGLE \(([^,)]+)/.exec(raw);
    const s = m ? `ANGLE (${m[1]})` : raw;
    return s.length > 40 ? s.slice(0, 39) + '…' : s;
  }

  function stats() {
    if (state.mode === 'static') {
      const now = performance.now();
      while (staticDraws.length && now - staticDraws[0] > 1000) staticDraws.shift();
      return {
        mode: state.mode,
        // no GL textures exist here: the decoded 2D image is the memory
        vramMB: (MAP_W * MAP_H * 4) / 1e6,
        fps: staticDraws.length,      // draws in the last second; 0 when idle
        ms: lastDrawMs,
        msKind: 'cpu',
        renderer: '2D',
        draws: state.draws,
        moving: false,
        intro: { ...liveRamps(performance.now()) },
        mapStatus: mapStatus(),
        colourFormat: state.colourFormat,
        firstLoad,
        introPending,
        whale: whaleStats(),
      };
    }
    return {
      mode: state.mode,
      vramMB: vramBytes(glc ? glc.size : null) / 1e6,
      fps: frameGaps.length >= 2 ? 1000 / meanOf(frameGaps) : 0,
      ms: frameMs.length ? meanOf(frameMs) : 0,
      msKind: 'cpu',
      renderer: shortRenderer(rendererName),
      draws: state.draws,
      moving: !!state.moving,
      intro: { ...liveRamps(performance.now()) },
      mapStatus: mapStatus(),
      colourFormat: state.colourFormat,
      firstLoad,
      introPending,
      whale: whaleStats(),
    };
  }

  // ---- menu-facing setters -------------------------------------------------
  // Brightness is the 0.5..1.5 multiplier, the same unit parseState returns and
  // the shader/CSS use; the menu passes its percentage divided by 100.
  function setBrightness(multiplier) {
    const v = Number(multiplier);
    state.brightness = clamp(isFinite(v) ? v : 1, 0.5, 1.5);
    const ov = document.getElementById(OV_ID);
    if (ov) ov.style.filter = `brightness(${state.brightness})`;
    refreshUi();
    return state.brightness;
  }

  // Opacity is a CSS style on whichever canvas is live; the GL loop rewrites
  // its inline style from `glc.css` each frame, so clear the cache too.
  function setOpacity(value) {
    const v = Number(value);
    state.opacity = clamp(isFinite(v) ? v : 1, 0, 1);
    const ov = document.getElementById(OV_ID);
    if (ov) ov.style.opacity = String(state.opacity);
    if (glc) {
      glc.css = '';
      glc.canvas.style.opacity = String(state.opacity);
    }
    refreshUi();
    return state.opacity;
  }

  // Adaptive fps: 30 while the camera is still, unlimited while it moves. The
  // state field and storage key stay `fps30` / `zz-sea-fps` for compatibility.
  function setAdaptiveFps(on) {
    state.fps30 = !!on;
    nextAt = 0;                    // the next rAF draws immediately
    refreshUi();
    return state.fps30;
  }

  function setFps30(on) {
    return setAdaptiveFps(on);
  }

  // Direct pose hook for the shader checks: accepts {x, y, depth, heading} in
  // texture-row tiles/radians, or null/undefined to turn the whale off. The
  // live route sampler (Task 4) writes the same state field every frame.
  function setWhalePose(pose) {
    if (!pose || typeof pose !== 'object') {
      state.whalePose = null;
      return state.whalePose;
    }
    const p = {
      x: Number(pose.x), y: Number(pose.y),
      depth: Number(pose.depth), heading: Number(pose.heading),
    };
    state.whalePose = [p.x, p.y, p.depth, p.heading].every(isFinite) ? p : null;
    return state.whalePose;
  }

  // Toggling the sea off stops both loops and hides the live canvas; toggling
  // it back on reuses the current mode (a deferred animated build happens here
  // when startup was off, so a sea that is off never loads the textures).
  function setOn(on) {
    state.on = !!on;
    for (const id of [OV_ID, GL_ID]) {
      const c = document.getElementById(id);
      if (c) c.style.display = state.on ? '' : 'none';
    }
    if (!state.on) {
      stopStatic();
      stopGL();
      hideNoise(0);
      refreshUi();
      return state.on;
    }
    if (pendingMode && applyPendingMode()) return state.on;
    if (state.mode === 'static') {
      if (!state.img) loadColour().catch(() => {});
      startStatic();
    } else if (glc && glc.ready && !glc.lost) {
      startGL();
    } else {
      setMode(state.mode);
    }
    refreshUi();
    return state.on;
  }

  state.setMode = setMode;
  state.debugRender = debugRender;
  state.liveViewState = liveViewState;
  state.stats = stats;
  state.intro = setIntro;
  state.bench = bench;
  state.retry = retry;
  state.setFps30 = setFps30;
  state.setAdaptiveFps = setAdaptiveFps;
  state.setBrightness = setBrightness;
  state.setOpacity = setOpacity;
  state.setOn = setOn;
  state.setWhalePose = setWhalePose;
  state.whale = setWhale;

  // ---- menu wiring and persistence ----------------------------------------
  // The menu reports a patch; this applies it, writes the zz-sea-* key and
  // returns the state the menu should paint.
  function uiState() {
    return {
      on: state.on, mode: state.mode, opacity: state.opacity,
      brightness: state.brightness, fps30: state.fps30,
      canUseShader: state.canUseShader, maxTextureSize: state.maxTextureSize,
      loading: state.loading, error: state.error,
    };
  }

  function applyChange(patch) {
    if (!patch) return uiState();
    if ('on' in patch) {
      localStorage.setItem('zz-sea-on', setOn(patch.on) ? '1' : '0');
    }
    if ('mode' in patch) {
      requestMode(patch.mode);       // may wait for the deck instead of demoting
      localStorage.setItem('zz-sea-mode', patch.mode);
    }
    if ('retry' in patch) {
      retry();
    }
    if ('opacity' in patch) {
      localStorage.setItem('zz-sea-opacity', String(setOpacity(patch.opacity)));
    }
    if ('brightness' in patch) {
      const v = setBrightness(patch.brightness);
      localStorage.setItem('zz-sea-brightness', String(Math.round(v * 100)));
    }
    if ('fps30' in patch) {
      localStorage.setItem('zz-sea-fps', setAdaptiveFps(patch.fps30) ? '1' : '0');
    }
    return uiState();
  }

  seaUi = mountSeaUi({
    getState: uiState, onChange: applyChange, getStats: stats, bench,
  });

  document.addEventListener('visibilitychange', () => {
    if (document.hidden) { stopStatic(); stopGL(); hideNoise(0); }
    else {
      anchorClock(false);            // re-anchor the whale clock after a sleep
      if (state.mode === 'static') {
        if (!state.img) loadColour().catch(() => {});
        startStatic();
      } else {
        if (holdStatic) startStatic();
        startGL();
      }
    }
  });

  // ---- startup -------------------------------------------------------------

  // The whale clock anchors immediately; the route waits for the first animated
  // build, so static never requests it. A failed HEAD leaves the local clock.
  anchorClock(true);

  // Static needs the colour map immediately; the animated build reuses this
  // decode instead of fetching the same 40 MPx WebP again.
  loadColour().catch(() => {});

  // The saved mode is a preference, not a claim that the app is ready. An
  // animated saved mode waits for #deckgl-overlay (this SPA renders it after
  // document-idle); only a real build failure calls fallbackToStatic. The
  // stored mode is never rewritten just because the app was still rendering.
  if (startupMode !== 'static') {
    pendingMode = startupMode;
    watchForDeck();
    applyPendingMode();            // no-op until the deck exists / the sea is on
  } else if (state.on) {
    showNoise();                   // nothing to show until the colour decodes
    startStatic();
  }
})();
