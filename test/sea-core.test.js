import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import {
  MODE_IDS, modeSettings, pickMode, parseState, renderSize,
  cameraUniforms, fpsGate, assetUrl, shaderCapable,
} from '../src/sea-core.js';
import seaConsts from '../src/sea-consts.json' with { type: 'json' };

const near = (a, b, eps = 1e-9) => Math.abs(a - b) <= eps;

describe('modeSettings', () => {
  it('baseline carries the tuned values', () => {
    const s = modeSettings('baseline');
    assert.equal(s.shader, true);
    assert.equal(s.renderScale, 1);
    assert.equal(s.uniforms.par, 0.976);
    assert.equal(s.uniforms.pom, 5);
    assert.equal(s.uniforms.warp, 1.6);
    assert.equal(s.uniforms.flowMode, 1);   // LOD
    assert.equal(s.uniforms.noiseLayers, 2);
  });

  it('medium only removes parallax', () => {
    assert.equal(modeSettings('medium').uniforms.par, 0);
    assert.equal(modeSettings('medium').uniforms.pom, 1);
    assert.equal(modeSettings('medium').uniforms.warp, 1.6);
  });

  it('low drops LOD, one noise layer and warp', () => {
    const u = modeSettings('low').uniforms;
    assert.equal(u.flowMode, 0);
    assert.equal(u.noiseLayers, 1);
    assert.equal(u.warp, 0);
    assert.equal(u.par, 0);
  });

  it('extra-low is low at half scale', () => {
    assert.equal(modeSettings('extra-low').renderScale, 0.5);
    assert.deepEqual(modeSettings('extra-low').uniforms, modeSettings('low').uniforms);
  });

  it('static needs no shader', () => {
    const s = modeSettings('static');
    assert.equal(s.shader, false);
    assert.equal(s.renderScale, 1);
    assert.equal(s.uniforms, null);
  });

  it('rejects unknown ids', () => {
    assert.throws(() => modeSettings('nope'));
  });

  it('exposes the five mode ids', () => {
    assert.deepEqual(MODE_IDS, ['baseline', 'medium', 'low', 'extra-low', 'static']);
  });

  it('uniforms carry exactly the shader keys, from the baseline table', () => {
    const keys = [
      'shore', 'shelf', 'flow', 'wave', 'grain', 'foam', 'deep', 'swell',
      'adir', 'aspeed', 'ascale', 'bdir', 'bspeed', 'bscale', 'par', 'pom',
      'dslow', 'win', 'wout', 'cycle', 'warp', 'wbias', 'deflect', 'deflectReach',
      'flowMode', 'noiseLayers', 'travelMax', 'snap',
    ];
    const u = modeSettings('baseline').uniforms;
    assert.deepEqual(Object.keys(u).sort(), keys.slice().sort());
    for (const [k, v] of Object.entries(seaConsts.baseline)) assert.equal(u[k], v);
    assert.equal(u.travelMax, 0);
    assert.equal(u.snap, 1);
  });

  it('animated tiers differ from baseline only in the tier keys', () => {
    const tierKeys = ['par', 'pom', 'warp', 'flowMode', 'noiseLayers'];
    const base = modeSettings('baseline').uniforms;
    for (const id of ['medium', 'low', 'extra-low']) {
      const u = modeSettings(id).uniforms;
      assert.deepEqual(Object.keys(u).sort(), Object.keys(base).sort(), `${id} key set`);
      for (const k of Object.keys(base)) {
        if (tierKeys.includes(k)) continue;
        assert.equal(u[k], base[k], `${id} changes ${k}`);
      }
    }
    // only par/pom differ at medium; low adds warp/flowMode/noiseLayers
    assert.deepEqual(
      Object.keys(base).filter(k => modeSettings('medium').uniforms[k] !== base[k]).sort(),
      ['par', 'pom'],
    );
    assert.deepEqual(
      Object.keys(base).filter(k => modeSettings('low').uniforms[k] !== base[k]).sort(),
      ['flowMode', 'noiseLayers', 'par', 'pom', 'warp'],
    );
  });

  it('only extra-low changes the render scale', () => {
    for (const id of MODE_IDS) {
      assert.equal(modeSettings(id).renderScale, id === 'extra-low' ? 0.5 : 1, id);
    }
  });

  it('returns a fresh object each call', () => {
    const a = modeSettings('baseline');
    a.uniforms.shore = -1;
    assert.equal(modeSettings('baseline').uniforms.shore, seaConsts.baseline.shore);
    assert.notEqual(modeSettings('baseline').uniforms, modeSettings('baseline').uniforms);
  });
});

describe('shaderCapable', () => {
  it('needs a WebGL2 context', () => {
    assert.equal(shaderCapable(false, 16384), false);
    assert.equal(shaderCapable(null, 16384), false);
    assert.equal(shaderCapable(true, 16384), true);
  });

  it('needs MAX_TEXTURE_SIZE to hold the 7749 px map', () => {
    assert.equal(shaderCapable(true, 0), false);
    assert.equal(shaderCapable(true, undefined), false);
    assert.equal(shaderCapable(true, 4096), false);
    assert.equal(shaderCapable(true, 8191), false);
    assert.equal(shaderCapable(true, 8192), true);
    assert.equal(shaderCapable(true, 16384), true);
  });
});

describe('pickMode', () => {
  const good = { webgl2: true, maxTextureSize: 16384, coarsePointer: false, narrow: false };

  it('defaults across every env combination', () => {
    const envs = [];
    for (const webgl2 of [true, false]) {
      for (const maxTextureSize of [8191, 8192]) {
        for (const coarsePointer of [false, true]) {
          for (const narrow of [false, true]) {
            envs.push({ webgl2, maxTextureSize, coarsePointer, narrow });
          }
        }
      }
    }
    assert.equal(envs.length, 16);
    for (const env of envs) {
      const expected = env.webgl2 && env.maxTextureSize >= 8192 &&
        !env.coarsePointer && !env.narrow ? 'baseline' : 'static';
      assert.equal(pickMode('', env), expected, JSON.stringify(env));
    }
  });

  it('lets a saved id win', () => {
    assert.equal(pickMode('low', { ...good, webgl2: false }), 'low');
    assert.equal(pickMode('static', good), 'static');
  });

  it('falls back to static without WebGL2', () => {
    assert.equal(pickMode('', { ...good, webgl2: false }), 'static');
  });

  it('falls back to static on small texture limits', () => {
    assert.equal(pickMode('', { ...good, maxTextureSize: 4096 }), 'static');
    assert.equal(pickMode('', { ...good, maxTextureSize: 8191 }), 'static');
    assert.equal(pickMode('', { ...good, maxTextureSize: 8192 }), 'baseline');
  });

  it('falls back to static on touch and narrow viewports', () => {
    assert.equal(pickMode('', { ...good, coarsePointer: true }), 'static');
    assert.equal(pickMode('', { ...good, narrow: true }), 'static');
  });

  it('uses baseline when everything is fine, and ignores unknown saved ids', () => {
    assert.equal(pickMode('', good), 'baseline');
    assert.equal(pickMode('nope', good), 'baseline');
    assert.equal(pickMode(null, good), 'baseline');
  });
});

describe('parseState', () => {
  it('applies defaults for nulls and missing keys', () => {
    assert.deepEqual(parseState({ mode: null, on: null, opacity: null, brightness: null, fps30: null }), {
      mode: '', on: true, opacity: 1, brightness: 1, fps30: false,
    });
    assert.deepEqual(parseState({}), { mode: '', on: true, opacity: 1, brightness: 1, fps30: false });
  });

  it('drops unparseable numbers and unknown modes', () => {
    const s = parseState({ mode: 'nope', opacity: 'NaN', brightness: 'NaN' });
    assert.equal(s.mode, '');
    assert.equal(s.opacity, 1);
    assert.equal(s.brightness, 1);
  });

  it('treats case, whitespace and out-of-range strings as corrupt', () => {
    // MODE_IDS are exact ids: a near-miss must not select a mode
    assert.equal(parseState({ mode: 'BASELINE' }).mode, '');
    assert.equal(parseState({ mode: ' baseline' }).mode, '');
    assert.equal(parseState({ mode: 42 }).mode, '');
    // a non-number falls back; Infinity is not finite
    assert.equal(parseState({ opacity: 'Infinity' }).opacity, 1);
    assert.equal(parseState({ brightness: '1e999' }).brightness, 1);
    assert.equal(parseState({ fps30: 'yes' }).fps30, false);
    assert.equal(parseState({ on: 'maybe' }).on, true);   // only 0/false turn it off
  });

  it('keeps valid values and clamps them', () => {
    assert.equal(parseState({ mode: 'extra-low' }).mode, 'extra-low');
    assert.equal(parseState({ opacity: '150' }).opacity, 1);
    assert.equal(parseState({ opacity: '-1' }).opacity, 0);
    assert.equal(parseState({ opacity: '0.4' }).opacity, 0.4);
    // saved brightness is a percentage (menu 50–150), returned as a multiplier
    assert.equal(parseState({ brightness: '20' }).brightness, 0.5);
    assert.equal(parseState({ brightness: '120' }).brightness, 1.2);
    assert.equal(parseState({ brightness: '200' }).brightness, 1.5);
  });

  it('reads the fps flag and the on/off switch', () => {
    assert.equal(parseState({ fps30: '1' }).fps30, true);
    assert.equal(parseState({ fps30: 'true' }).fps30, true);
    assert.equal(parseState({ fps30: true }).fps30, true);
    assert.equal(parseState({ fps30: '0' }).fps30, false);
    assert.equal(parseState({ on: '0' }).on, false);
    assert.equal(parseState({ on: 'false' }).on, false);
    assert.equal(parseState({ on: '1' }).on, true);
  });
});

describe('renderSize', () => {
  it('caps the long edge and reports the backing scale', () => {
    const s = renderSize(1920, 1080, 2, 1);
    assert.deepEqual([s.w, s.h], [2560, 1440]);
    assert.ok(near(s.scale, 2560 / 1920));
  });

  it('leaves a phone-sized viewport alone when under the cap', () => {
    const s = renderSize(390, 844, 3, 1);
    assert.deepEqual([s.w, s.h], [1170, 2532]);
    assert.equal(s.scale, 3);
  });

  it('caps tall viewports on the height', () => {
    const s = renderSize(1000, 3000, 1, 1);
    assert.deepEqual([s.w, s.h], [853, 2560]);
  });

  it('halves the backing store for extra-low', () => {
    const s = renderSize(1920, 1080, 2, 0.5);
    assert.deepEqual([s.w, s.h], [1280, 720]);
    assert.ok(near(s.scale, 1280 / 1920));
  });
});

describe('cameraUniforms', () => {
  const vs = (zoom, padding) => ({ zoom, target: [100, 200], padding });

  it('maps an unpadded view centre to the canvas centre', () => {
    const c = cameraUniforms(vs(3, { left: 0, right: 0, top: 0, bottom: 0 }), 1000, 500, 2000, 1000);
    assert.equal(c.zoomPx, 16);          // 2**3 * (2000 / 1000)
    assert.equal(c.centerX, 1000);
    assert.equal(c.centerY, 500);
    assert.equal(c.viewW, 2000);
    assert.equal(c.viewH, 1000);
  });

  it('keeps the padded centre on the canvas centre', () => {
    const c = cameraUniforms(vs(0, { left: 68, right: 68 }), 1000, 500, 1000, 500);
    assert.equal(c.centerX, 500);
    assert.equal(c.zoomPx, 1);
  });

  it('shifts the centre by half of the asymmetric padding', () => {
    const c = cameraUniforms(vs(0, { left: 68 }), 1000, 500, 1000, 500);
    assert.equal(c.centerX, 534);
    const c2 = cameraUniforms(vs(0, { top: 40 }), 1000, 500, 2000, 1000);
    // bottom-origin: a top inset moves the centre up, i.e. below the middle in
    // gl_FragCoord's y (1000 - (250 + 20) * 2)
    assert.equal(c2.centerY, 460);
  });

  // The shader's y is bottom-origin while deck measures padding from the top;
  // an asymmetric top/bottom pair is the case that catches a missing flip.
  it('flips asymmetric top/bottom padding into the shader space', () => {
    const c = cameraUniforms(vs(0, { top: 40, bottom: 100 }), 1000, 500, 2000, 1000);
    assert.equal(c.centerY, 560);          // 1000 - ((250 - 30) * 2)
    const back = cameraUniforms(vs(0, { top: 100, bottom: 40 }), 1000, 500, 2000, 1000);
    assert.equal(back.centerY, 440);       // 1000 - ((250 + 30) * 2)
    assert.equal(c.centerY + back.centerY, 1000);   // mirrored padding mirrors the centre
  });
});

describe('fpsGate', () => {
  it('draws every frame without the gate', () => {
    assert.equal(fpsGate(0, 0, false).draw, true);
    assert.equal(fpsGate(100, 200, false).draw, true);
  });

  it('admits one frame per 33.3 ms with the gate', () => {
    const first = fpsGate(0, 0, true);
    assert.equal(first.draw, true);
    assert.ok(near(first.nextAt, 1000 / 30));

    const early = fpsGate(16.7, first.nextAt, true);
    assert.equal(early.draw, false);
    assert.equal(early.nextAt, first.nextAt);

    const due = fpsGate(33.4, first.nextAt, true);
    assert.equal(due.draw, true);
    assert.ok(near(due.nextAt, 33.4 + 1000 / 30));
  });

  it('admits 30 frames over 60 exact 60 Hz ticks', () => {
    let nextAt = 0;
    let draws = 0;
    for (let i = 1; i <= 60; i++) {
      const frame = fpsGate(i * (1000 / 60), nextAt, true);
      nextAt = frame.nextAt;
      if (frame.draw) draws++;
    }
    assert.ok(draws >= 29 && draws <= 31, `draws=${draws}`);
  });
});

describe('assetUrl', () => {
  it('carries the asset base and version', () => {
    assert.equal(
      assetUrl('zigzag-sea-dist.webp'),
      `${seaConsts.assetBase}/zigzag-sea-dist.webp?v=${seaConsts.version}`,
    );
    assert.match(assetUrl('zigzag-sea-dist.webp'), /\?v=0\.3\.0$/);
  });

  it('versions every shader texture', () => {
    for (const name of ['dist', 'aux', 'flow', 'speed']) {
      assert.match(assetUrl(`zigzag-sea-${name}.webp`), /\?v=0\.3\.0$/);
    }
  });

  it('accepts a base override for local builds', () => {
    assert.equal(
      assetUrl('zigzag-sea-dist.webp', 'http://127.0.0.1:3006'),
      'http://127.0.0.1:3006/zigzag-sea-dist.webp?v=0.3.0',
    );
    assert.equal(
      assetUrl('zigzag-sea-dist.webp', undefined),
      `${seaConsts.assetBase}/zigzag-sea-dist.webp?v=${seaConsts.version}`,
    );
  });
});
