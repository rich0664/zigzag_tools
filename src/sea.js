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
 *               bakes. Started only by an explicit setMode(); the startup mode
 *               stays static until the capability probe exists.
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

  // Saved values live under the zz-sea-* keys; the pure core parses and clamps
  // them, and picks the mode. The capability probe arrives with Task 8.
  const saved = parseState({
    mode: localStorage.getItem('zz-sea-mode'),
    on: localStorage.getItem('zz-sea-on'),
    opacity: localStorage.getItem('zz-sea-opacity'),
    brightness: localStorage.getItem('zz-sea-brightness'),
    fps30: localStorage.getItem('zz-sea-fps'),
  });

  // Until the capability probe exists, no saved mode can render: start static
  // so the build cannot claim an animated mode it has not verified. The saved
  // value stays in `saved.mode` for Task 8 to honour.
  const state = {
    on: saved.on,
    mode: pickMode('', {
      webgl2: false, maxTextureSize: 0, coarsePointer: false, narrow: false,
    }),
    img: null, raf: 0, sig: '', draws: 0,
    opacity: saved.opacity, brightness: saved.brightness,
    glReady: false, error: null,
  };
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

  // ---- static 2D path ------------------------------------------------------

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

  function drawStatic() {
    if (state.mode !== 'static') return;
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

  function staticTick() {
    state.raf = 0;
    if (state.mode !== 'static' || !state.on || document.hidden) return;
    try { drawStatic(); } catch (e) { state.error = errText(e); }
    state.raf = requestAnimationFrame(staticTick);
  }

  function startStatic() {
    if (state.raf || state.mode !== 'static' || !state.on || document.hidden) return;
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

  // ---- GL path -------------------------------------------------------------

  let glc = null;

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
    const vs = compile(gl, gl.VERTEX_SHADER, VERT);
    const fs = compile(gl, gl.FRAGMENT_SHADER, FRAG);
    gl.attachShader(prog, vs);
    gl.attachShader(prog, fs);
    gl.linkProgram(prog);
    gl.deleteShader(vs);
    gl.deleteShader(fs);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) {
      const log = gl.getProgramInfoLog(prog);
      gl.deleteProgram(prog);
      throw new Error('shader link: ' + log);
    }
    return prog;
  }

  const UNIFORM_NAMES = [
    'uColour', 'uAux', 'uDist', 'uFlow', 'uSpeedTex', 'uCurve', 'uSliceCurve', 'uWarpCurve',
    'uTarget', 'uSize', 'uFlowSize', 'uSpeedSize', 'uAuxSpan', 'uCenter', 'uFlowCell',
    'uZoom', 'uTime', 'uDistMax', 'uShoreW', 'uShelf', 'uFlowSpeed', 'uGrain', 'uFoam',
    'uDeep', 'uSwell', 'uSnap', 'uWaveSpeed', 'uFlowMode', 'uZoomCss', 'uCycle',
    'uDeflect', 'uDeflectReach', 'uTravelMax', 'uWarp', 'uWarpBias', 'uDirA', 'uDirB',
    'uSpeedA', 'uSpeedB', 'uScaleA', 'uScaleB', 'uParallax', 'uDeepSlow', 'uWaveIn',
    'uWaveOut', 'uPomLayers', 'uAlongScale', 'uBrightness', 'uNoiseLayers',
  ];

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

  function loadTexture(gl, src, nearest) {
    const tex = gl.createTexture();
    const f = nearest ? gl.NEAREST : gl.LINEAR;
    gl.bindTexture(gl.TEXTURE_2D, tex);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, f);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, f);
    return new Promise((resolve, reject) => {
      const img = new Image();
      img.crossOrigin = 'anonymous';
      img.onload = () => {
        gl.bindTexture(gl.TEXTURE_2D, tex);
        gl.pixelStorei(gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL, false);
        gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, img);
        resolve({ tex, w: img.width, h: img.height });
      };
      img.onerror = () => { gl.deleteTexture(tex); reject(new Error('texture failed: ' + src)); };
      img.src = src;
    });
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

  function buildGL() {
    const deck = deckCanvas();
    if (!deck || !deck.parentNode) throw new Error('no deck canvas');

    const canvas = document.createElement('canvas');
    canvas.id = GL_ID;
    deck.parentNode.insertBefore(canvas, deck);

    const gl = canvas.getContext('webgl2', {
      alpha: true, premultipliedAlpha: false, antialias: false, depth: false,
    });
    if (!gl) { canvas.remove(); throw new Error('webgl2 unavailable'); }

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
      size: {}, raf: 0, ready: false, css: '',
    };
    glc = c;

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
      loadTexture(gl, url(name), nearest)
        .then(r => { c.tex[key] = r.tex; c.size[key] = [r.w, r.h]; }),
    )).then(() => {
      if (glc !== c) return;
      c.ready = true;
      state.glReady = true;
      state.error = null;
      startGL();
    }).catch(e => {
      if (glc === c) fallbackToStatic(e);
    });
  }

  function releaseGL() {
    if (!glc) return;
    if (glc.raf) cancelAnimationFrame(glc.raf);
    const { gl, canvas, prog } = glc;
    glc = null;
    state.glReady = false;
    try { gl.deleteProgram(prog); } catch {}
    const lose = gl.getExtension('WEBGL_lose_context');
    if (lose) { try { lose.loseContext(); } catch {} }
    canvas.remove();
  }

  function renderFrame(c, w, h, cam, t, settings) {
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

  function drawGL(t) {
    if (!glc || !glc.ready) return;
    const deck = deckCanvas();
    if (!deck) return;
    const vs = viewState(deck);
    if (!vs) return;

    const cssW = deck.clientWidth || deck.width;
    const cssH = deck.clientHeight || deck.height;
    if (!cssW || !cssH) return;

    const canvas = glc.canvas;
    if (canvas.parentNode !== deck.parentNode || canvas.nextElementSibling !== deck) {
      deck.parentNode.insertBefore(canvas, deck);
    }
    const css = (deck.getAttribute('style') || '') +
      ';pointer-events:none;opacity:' + state.opacity;
    if (glc.css !== css) {
      glc.css = css;
      canvas.style.cssText = css;
    }

    const dpr = window.devicePixelRatio || 1;
    const settings = modeSettings(state.mode);
    const { w, h, scale } = renderSize(cssW, cssH, dpr, settings.renderScale);
    if (canvas.width !== w || canvas.height !== h) {
      canvas.width = w;
      canvas.height = h;
    }
    const cam = cameraUniforms(vs, cssW, cssH, w, h);
    renderFrame(glc, w, h, {
      tx: vs.target[0], ty: vs.target[1],
      zoom: cam.zoomPx, zoomCss: cam.zoomPx / scale,
      centerX: cam.centerX, centerY: cam.centerY,
    }, t, settings);
    state.draws++;
  }

  function glTick(now) {
    if (!glc) return;
    glc.raf = 0;
    if (state.mode === 'static' || !state.on || document.hidden) return;
    try { drawGL(now / 1000); } catch (e) { fallbackToStatic(e); return; }
    glc.raf = requestAnimationFrame(glTick);
  }

  function startGL() {
    if (!glc || glc.raf || !glc.ready) return;
    if (state.mode === 'static' || !state.on || document.hidden) return;
    glc.raf = requestAnimationFrame(glTick);
  }

  function stopGL() {
    if (glc && glc.raf) cancelAnimationFrame(glc.raf);
    if (glc) glc.raf = 0;
  }

  function fallbackToStatic(reason) {
    state.error = errText(reason);
    releaseGL();
    state.mode = 'static';
    state.sig = '';
    try { drawStatic(); } catch (e) { state.error = errText(e); }
    startStatic();
  }

  // ---- mode switch ---------------------------------------------------------

  function setMode(id) {
    modeSettings(id);                       // validates, throws on unknown ids
    if (state.mode === id && (id === 'static' || glc)) return id;

    state.mode = id;
    state.sig = '';
    if (id === 'static') {
      releaseGL();
      try { drawStatic(); } catch (e) { state.error = errText(e); }
      startStatic();
    } else {
      stopStatic();
      removeStaticCanvas();
      if (glc) startGL();
      else {
        try { buildGL(); } catch (e) { fallbackToStatic(e); }
      }
    }
    return state.mode;
  }

  // One frame at an exact size and camera, read back synchronously. Used by the
  // parity check: it is the camera, not the live viewport, that decides.
  function debugRender(opts) {
    opts = opts || {};
    if (!glc || !glc.ready) throw new Error('gl path not ready');
    const w = Math.max(1, Math.round(Number(opts.width)));
    const h = Math.max(1, Math.round(Number(opts.height)));
    const zoom = Number(opts.zoom);
    const t = opts.t === undefined ? 0 : Number(opts.t);
    const tx = Number(opts.tx);
    const ty = Number(opts.ty);
    if (![w, h, zoom, t, tx, ty].every(isFinite)) {
      throw new Error('debugRender needs finite tx, ty, width, height, zoom');
    }

    const canvas = glc.canvas;
    const prevW = canvas.width, prevH = canvas.height;
    try {
      if (canvas.width !== w || canvas.height !== h) {
        canvas.width = w;
        canvas.height = h;
      }
      renderFrame(glc, w, h, {
        tx, ty,
        zoom, zoomCss: zoom, centerX: w / 2, centerY: h / 2,
      }, t, modeSettings(state.mode));

      const px = new Uint8Array(w * h * 4);
      glc.gl.readPixels(0, 0, w, h, glc.gl.RGBA, glc.gl.UNSIGNED_BYTE, px);

      // flip the bottom-up GL rows into a top-down 2D canvas. readPixels
      // returns the straight-alpha bytes the shader wrote (the context is not
      // premultiplied); putImageData takes straight alpha, so toDataURL keeps
      // the transparent margin transparent instead of flattening it.
      const out = document.createElement('canvas');
      out.width = w;
      out.height = h;
      const ctx = out.getContext('2d');
      const img = ctx.createImageData(w, h);
      const row = w * 4;
      for (let y = 0; y < h; y++) {
        img.data.set(px.subarray((h - 1 - y) * row, (h - y) * row), y * row);
      }
      ctx.putImageData(img, 0, 0);
      return out.toDataURL('image/png');
    } finally {
      if (canvas.width !== prevW || canvas.height !== prevH) {
        canvas.width = prevW;
        canvas.height = prevH;
      }
      if (state.mode !== 'static' && glc && glc.ready) {
        try { drawGL(performance.now() / 1000); } catch (e) { state.error = errText(e); }
      }
    }
  }

  state.setMode = setMode;
  state.debugRender = debugRender;

  document.addEventListener('visibilitychange', () => {
    if (document.hidden) { stopStatic(); stopGL(); }
    else if (state.mode === 'static') startStatic();
    else startGL();
  });

  // ---- startup -------------------------------------------------------------

  const img = new Image();
  img.crossOrigin = 'anonymous';
  img.onload = () => {
    state.img = img;
    state.sig = '';
    if (state.mode === 'static') startStatic();
  };
  img.onerror = () => { state.error = 'sea texture failed to load'; };
  img.src = url('zigzag-sea.webp');

  startStatic();
})();
