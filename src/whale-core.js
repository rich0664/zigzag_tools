// Pure core of the whale easter egg: the baked route format, a fixed-Δt
// Catmull-Rom sampler, the uniform packer and the network clock offset. No DOM
// and no GL, so node --test imports it directly and build.mjs inlines it into
// the sea bundle.

const WHALE_MAGIC = 0x5748414c;   // "WHAL"
const WHALE_VERSION = 1;
const WHALE_HEADER = 16;
const WHALE_RECORD = 8;

const whaleClamp = (v, a, b) => Math.max(a, Math.min(b, v));

// Little-endian header (16 bytes) + count fixed-Δt records (8 bytes each).
// x/y are 1/8 tile, depth is 1/16 tile; the returned views keep the raw units
// and maxDepth is decoded to tiles like every other output of the sampler.
function parseWhaleRoute(buf) {
  const bytes = buf instanceof ArrayBuffer
    ? new Uint8Array(buf)
    : new Uint8Array(buf.buffer, buf.byteOffset, buf.byteLength);
  if (bytes.byteLength < WHALE_HEADER) {
    throw new Error(`whale route: ${bytes.byteLength} bytes is shorter than the ${WHALE_HEADER}-byte header`);
  }
  const dv = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);

  const magic = dv.getUint32(0, true);
  if (magic !== WHALE_MAGIC) {
    throw new Error(`whale route: bad magic 0x${magic.toString(16)}`);
  }
  const version = dv.getUint16(4, true);
  if (version !== WHALE_VERSION) {
    throw new Error(`whale route: unsupported version ${version}`);
  }
  const dtSec = dv.getUint16(6, true);
  if (!(dtSec > 0)) throw new Error('whale route: dtSec must be positive');
  const count = dv.getUint32(8, true);
  if (!(count > 0)) throw new Error('whale route: count must be positive');
  const expected = WHALE_HEADER + WHALE_RECORD * count;
  if (bytes.byteLength !== expected) {
    throw new Error(
      `whale route: ${bytes.byteLength} bytes, expected ${expected} for ${count} records`,
    );
  }

  const x = new Uint16Array(count);
  const y = new Uint16Array(count);
  const depth = new Uint8Array(count);
  for (let i = 0; i < count; i++) {
    const off = WHALE_HEADER + WHALE_RECORD * i;
    x[i] = dv.getUint16(off, true);
    y[i] = dv.getUint16(off + 2, true);
    depth[i] = dv.getUint8(off + 4);
    // off + 5 is the reserved pad byte, ignored
  }

  return {
    dtSec,
    count,
    maxDepth: dv.getUint16(12, true) / 16,
    seed: dv.getUint16(14, true),
    loopDuration: count * dtSec,
    x,
    y,
    depth,
  };
}

// Uniform Catmull-Rom, in the u parameter (0..1 across one record interval).
function catmull(p0, p1, p2, p3, u) {
  const u2 = u * u;
  const u3 = u2 * u;
  return 0.5 * (
    2 * p1 +
    (p2 - p0) * u +
    (2 * p0 - 5 * p1 + 4 * p2 - p3) * u2 +
    (3 * p1 - p0 - 3 * p2 + p3) * u3
  );
}

function catmullSlope(p0, p1, p2, p3, u) {
  const u2 = u * u;
  return 0.5 * (
    (p2 - p0) +
    2 * (2 * p0 - 5 * p1 + 4 * p2 - p3) * u +
    3 * (3 * p1 - p0 - 3 * p2 + p3) * u2
  );
}

// t wraps over the loop; the four neighbouring records are taken modulo count,
// which makes the seam C1 in both position and heading. Positions decode to
// tiles; depth is linear between records; heading is the tangent in
// texture-row space.
function sampleWhale(route, tSec) {
  const { dtSec, count, loopDuration } = route;
  let t = tSec % loopDuration;
  if (t < 0) t += loopDuration;

  const fi = t / dtSec;
  const i0 = Math.floor(fi);
  // i0 can only reach count through rounding at t ≈ loopDuration; mod folds
  // that back onto record 0 with u ≈ 0.
  const i = i0 % count;
  const u = fi - i0;

  const im1 = i === 0 ? count - 1 : i - 1;
  const i1 = i + 1 === count ? 0 : i + 1;
  const i2 = i1 + 1 === count ? 0 : i1 + 1;

  const x = (k) => route.x[k] / 8;
  const y = (k) => route.y[k] / 8;

  const dx = catmullSlope(x(im1), x(i), x(i1), x(i2), u);
  const dy = catmullSlope(y(im1), y(i), y(i1), y(i2), u);
  const d0 = route.depth[i] / 16;
  const d1 = route.depth[i1] / 16;

  return {
    x: catmull(x(im1), x(i), x(i1), x(i2), u),
    y: catmull(y(im1), y(i), y(i1), y(i2), u),
    depth: d0 + (d1 - d0) * u,
    heading: Math.atan2(dy, dx),
  };
}

// Pack for the two shader uniforms. surf fades the surface ring from the
// waterline (depth 0 → 1) to 1.5 tiles down (→ 0); on gates the whole block,
// with a zeroed pose whenever there is no pose.
function whaleUniforms(pose, shape) {
  const present = pose !== null && pose !== undefined;
  const depth = present ? pose.depth : 0;
  return {
    pose: present ? [pose.x, pose.y, pose.heading, pose.depth] : [0, 0, 0, 0],
    shape: [shape.halfLen, shape.halfWid, whaleClamp(1 - depth / 1.5, 0, 1), present ? 1 : 0],
  };
}

// Seconds to add to the local epoch clock: the server's Date header plus half
// the round trip (the request left midway through it) minus the local clock.
function clockOffset(serverEpochSec, rttMs, localEpochSec) {
  return serverEpochSec + rttMs / 2000 - localEpochSec;
}

export {
  parseWhaleRoute, sampleWhale, whaleUniforms, clockOffset,
};
