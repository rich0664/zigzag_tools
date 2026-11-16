import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import {
  parseWhaleRoute, sampleWhale, whaleUniforms, clockOffset,
} from '../src/whale-core.js';

const MAGIC = 0x5748414c;
const TAU = Math.PI * 2;

// Eight records around a square, in tiles. build() encodes x/y as tiles × 8
// and depth as tiles × 16; depth 1.5 is the deepest representable here.
const SQUARE = [
  { x: [10, 10], depth: 0 },
  { x: [30, 10], depth: 0.25 },
  { x: [50, 10], depth: 0.5 },
  { x: [50, 30], depth: 0.75 },
  { x: [50, 50], depth: 1.5 },
  { x: [30, 50], depth: 0.75 },
  { x: [10, 50], depth: 0.5 },
  { x: [10, 30], depth: 0.25 },
];

function build(records, opts = {}) {
  const dtSec = opts.dtSec === undefined ? 20 : opts.dtSec;
  const count = opts.count === undefined ? records.length : opts.count;
  const buf = new ArrayBuffer(16 + 8 * records.length);
  const dv = new DataView(buf);
  dv.setUint32(0, opts.magic === undefined ? MAGIC : opts.magic, true);
  dv.setUint16(4, opts.version === undefined ? 1 : opts.version, true);
  dv.setUint16(6, dtSec, true);
  dv.setUint32(8, count, true);
  dv.setUint16(12, opts.maxDepth === undefined ? 24 : opts.maxDepth, true);
  dv.setUint16(14, opts.seed === undefined ? 7 : opts.seed, true);
  records.forEach((r, k) => {
    const off = 16 + 8 * k;
    dv.setUint16(off, Math.round(r.x[0] * 8), true);
    dv.setUint16(off + 2, Math.round(r.x[1] * 8), true);
    dv.setUint8(off + 4, Math.round(r.depth * 16));
    dv.setUint8(off + 5, 0);
  });
  return buf;
}

const squareRoute = () => parseWhaleRoute(build(SQUARE));

const dist = (a, b) => Math.hypot(a.x - b.x, a.y - b.y);
const angDiff = (a, b) => {
  let d = (a - b) % TAU;
  if (d > Math.PI) d -= TAU;
  if (d < -Math.PI) d += TAU;
  return Math.abs(d);
};

describe('parseWhaleRoute', () => {
  it('parses a synthetic route', () => {
    const r = squareRoute();
    assert.equal(r.dtSec, 20);
    assert.equal(r.count, 8);
    assert.equal(r.seed, 7);
    assert.equal(r.maxDepth, 1.5);          // raw 24 / 16, in tiles
    assert.equal(r.loopDuration, 160);
    assert.ok(r.x instanceof Uint16Array);
    assert.ok(r.y instanceof Uint16Array);
    assert.ok(r.depth instanceof Uint8Array);
    assert.equal(r.x.length, 8);
    assert.equal(r.x[0], 80);               // raw 1/8 tiles
    assert.equal(r.depth[4], 24);           // raw 1/16 tiles
  });

  it('accepts a typed-array view with a byte offset', () => {
    const raw = new Uint8Array(build(SQUARE).slice(0));
    const padded = new Uint8Array(raw.length + 3);
    padded.set(raw, 3);
    const r = parseWhaleRoute(padded.subarray(3, 3 + raw.length));
    assert.equal(r.count, 8);
    assert.equal(r.x[0], 80);
  });

  it('rejects a short header', () => {
    assert.throws(() => parseWhaleRoute(new ArrayBuffer(15)));
  });

  it('rejects a bad magic', () => {
    assert.throws(() => parseWhaleRoute(build(SQUARE, { magic: 0x12345678 })));
  });

  it('rejects a bad version', () => {
    assert.throws(() => parseWhaleRoute(build(SQUARE, { version: 2 })));
  });

  it('rejects count 0', () => {
    assert.throws(() => parseWhaleRoute(build(SQUARE, { count: 0 })));
  });

  it('rejects dtSec 0', () => {
    assert.throws(() => parseWhaleRoute(build(SQUARE, { dtSec: 0 })));
  });

  it('rejects a byte length that disagrees with count', () => {
    // count says 8 but the buffer holds 7 records
    assert.throws(() => parseWhaleRoute(build(SQUARE.slice(0, 7), { count: 8 })));
    // count says 8 and the buffer has a trailing record
    assert.throws(() => parseWhaleRoute(build([...SQUARE, SQUARE[0]], { count: 8 })));
    // count says 9 with only 8 records
    assert.throws(() => parseWhaleRoute(build(SQUARE, { count: 9 })));
  });
});

describe('sampleWhale', () => {
  it('returns each record position at its record time', () => {
    const r = squareRoute();
    for (let k = 0; k < r.count; k++) {
      const p = sampleWhale(r, k * r.dtSec);
      assert.equal(p.x, SQUARE[k].x[0], `record ${k} x`);
      assert.equal(p.y, SQUARE[k].x[1], `record ${k} y`);
      assert.equal(p.depth, SQUARE[k].depth, `record ${k} depth`);
    }
  });

  it('decodes raw units into tiles', () => {
    const r = parseWhaleRoute(build([
      { x: [1, 0.125], depth: 1 / 16 },       // raw x 8, raw y 1, raw depth 1
      { x: [1, 0.125], depth: 1 / 16 },
    ], { dtSec: 10 }));
    assert.equal(r.x[0], 8);
    assert.equal(r.y[0], 1);
    const p0 = sampleWhale(r, 0);
    assert.equal(p0.x, 1);
    assert.equal(p0.y, 0.125);
    assert.equal(p0.depth, 1 / 16);
  });

  it('interpolates depth linearly between records', () => {
    const r = parseWhaleRoute(build([
      { x: [0, 0], depth: 0 },
      { x: [10, 0], depth: 2 },
    ], { dtSec: 20 }));
    assert.equal(sampleWhale(r, 10).depth, 1);
    assert.equal(sampleWhale(r, 5).depth, 0.5);
  });

  it('is continuous across every record', () => {
    const r = squareRoute();
    for (let k = 0; k < r.count; k++) {
      const t = k * r.dtSec;
      const before = sampleWhale(r, t - 1e-6);
      const after = sampleWhale(r, t + 1e-6);
      assert.ok(dist(before, after) < 1e-3, `record ${k}: ${dist(before, after)}`);
    }
  });

  it('is C1 across the loop seam', () => {
    const r = squareRoute();
    const head = sampleWhale(r, 1e-6);
    const tail = sampleWhale(r, r.loopDuration - 1e-6);
    assert.ok(dist(head, tail) < 1e-3, `position ${dist(head, tail)}`);
    assert.ok(angDiff(head.heading, tail.heading) < 1e-3,
      `heading ${head.heading} vs ${tail.heading}`);
  });

  it('wraps t by the loop duration', () => {
    const r = squareRoute();
    const base = sampleWhale(r, 12.5);
    assert.deepEqual(sampleWhale(r, 12.5 + r.loopDuration), base);
    assert.deepEqual(sampleWhale(r, 12.5 + 2 * r.loopDuration), base);
    assert.deepEqual(sampleWhale(r, 12.5 - r.loopDuration), base);
  });

  it('heads along the tangent, in texture-row space', () => {
    const east = parseWhaleRoute(build([
      { x: [10, 10], depth: 0 }, { x: [20, 10], depth: 0 },
      { x: [30, 10], depth: 0 }, { x: [40, 10], depth: 0 },
    ]));
    assert.ok(angDiff(sampleWhale(east, 30).heading, 0) < 1e-9);

    const north = parseWhaleRoute(build([
      { x: [10, 10], depth: 0 }, { x: [10, 20], depth: 0 },
      { x: [10, 30], depth: 0 }, { x: [10, 40], depth: 0 },
    ]));
    assert.ok(angDiff(sampleWhale(north, 30).heading, Math.PI / 2) < 1e-9);
  });

  it('is deterministic', () => {
    const r = squareRoute();
    assert.deepEqual(sampleWhale(r, 33.333), sampleWhale(r, 33.333));
  });
});

describe('whaleUniforms', () => {
  const shape = { halfLen: 2.75, halfWid: 1.35 };

  it('packs pose and shape', () => {
    const u = whaleUniforms({ x: 100, y: 200, heading: 0.5, depth: 0.25 }, shape);
    assert.deepEqual(u.pose, [100, 200, 0.5, 0.25]);
    assert.equal(u.shape[0], 2.75);
    assert.equal(u.shape[1], 1.35);
    assert.ok(Math.abs(u.shape[2] - (1 - 0.25 / 1.5)) < 1e-12);
    assert.equal(u.shape[3], 1);
  });

  it('clamps surf between 1 at the waterline and 0 by 1.5 tiles', () => {
    assert.equal(whaleUniforms({ x: 0, y: 0, heading: 0, depth: 0 }, shape).shape[2], 1);
    assert.equal(whaleUniforms({ x: 0, y: 0, heading: 0, depth: 1.5 }, shape).shape[2], 0);
    assert.equal(whaleUniforms({ x: 0, y: 0, heading: 0, depth: 9 }, shape).shape[2], 0);
  });

  it('disables the whale and zeroes the pose when pose is null', () => {
    const u = whaleUniforms(null, shape);
    assert.deepEqual(u.pose, [0, 0, 0, 0]);
    assert.equal(u.shape[3], 0);
    assert.equal(u.shape[0], 2.75);
    assert.equal(u.shape[1], 1.35);
  });
});

describe('clockOffset', () => {
  it('adds the header and half the round trip to the server epoch', () => {
    assert.ok(Math.abs(clockOffset(1000, 200, 999.9) - 0.2) < 1e-9);
  });

  it('is just the clock difference with no round trip', () => {
    assert.equal(clockOffset(1000, 0, 1000), 0);
    assert.equal(clockOffset(1000, 0, 1001), -1);
  });
});
