#!/usr/bin/env python3
"""Sea texture editor.

    python3 tools/sea_editor.py           -> http://127.0.0.1:3007

A small local editor for the sea depth texture. The pipeline is a fixed chain
of nodes, each with its own parameters, an enable toggle and a cached output;
mask nodes can be wired into any effect node to modulate it by distance,
height or noise.

Change a parameter and only that node (and the ones downstream) recompute, so
iterating on a late stage does not redo the expensive distance/erosion work.
"""
import hashlib
import io
import argparse
import json
import os
import time
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
from PIL import Image

import sea_core as sc

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
WORK = os.path.join(ROOT, "out", "sea")
MAP_PNG = os.path.join(WORK, "map.png")
PRESETS = os.path.join(WORK, "presets")
PORT = 3007   # override with --port

_lock = threading.Lock()          # guards STATE mutation + preview bytes
_render_lock = threading.Lock()   # one render at a time
_full = None                      # whole-map land mask
_cache = {}                       # node id -> (signature, value)
_version = 0
_png = None
_render = {"running": False, "stage": 0, "total": 0, "node": "",
           "started": 0.0, "elapsed": 0.0, "stamp": 0, "last_ms": 0.0,
           "error": None, "auto": False}


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------
def default_state():
    return {
        "region": {"x0": 2800, "y0": 2150, "w": 1200, "h": 800, "scale": 1.0},
        "channel": "output",
        "nodes": {
            "distance": dict(enabled=True, down=2),
            "warp": dict(enabled=False, tiles=46.0, scale=700),
            "height": dict(enabled=True, mode="profile", deep_dist=1600.0,
                           curve=1.0, shelf_t=0.10, slope_t=0.32, abyss_t=1.0,
                           out_curve=[[0, 0], [1, 1]]),
            "noise": dict(enabled=False, amp=0.15, scale=350, octaves=4, seed=11,
                          blend="add", masks=[], out_curve=[[0, 0], [1, 1]]),
            "features": dict(enabled=False, amp=0.12, scale=70, density=0.70,
                             octaves=4, seed=9, masks=[],
                             out_curve=[[0, 0], [1, 1]]),
            "erode": dict(enabled=True, window=6, layers=20, mix=1.0, masks=[],
                          out_curve=[[0, 0], [1, 1]]),
            "drainage": dict(enabled=False, scale=4, steps=4, k=2.0, m=0.5,
                             n=1.0, cap=0.15, strength=1.0, masks=[],
                             out_curve=[[0, 0], [1, 1]]),
            "shelf": dict(enabled=False, gain=1.0, power=1.0, masks=[],
                          out_curve=[[0, 0], [1, 1]]),
            "shade": dict(enabled=False, amp=1.45, lo=0.76, hi=1.24),
            "palette": dict(enabled=True),
            "coast": dict(enabled=False, levels=4, strength=0.13,
                          r=95, g=155, b=195),
            "output": dict(enabled=True, land_t=0.58),
        },
        "masks": {
            "depth_band": dict(source="height", tap="", scale=400, lo=0.15, hi=0.70,
                               feather=0.1, invert=False),
            "open_water": dict(source="distance", tap="", scale=400, lo=40, hi=160,
                               feather=1.0, invert=False),
            "big_noise": dict(source="noise", tap="", scale=900, lo=0.3, hi=0.7,
                              feather=1.0, invert=False),
        },
        "next_mask": 1,
    }


STATE = default_state()


# --------------------------------------------------------------------------
# region + fields
# --------------------------------------------------------------------------
def load_map():
    global _full
    if _full is None:
        if not os.path.exists(MAP_PNG):
            raise SystemExit("map raster missing: run tools/build_sea.py once")
        _full = np.array(Image.open(MAP_PNG).convert("RGBA"))[:, :, 3] > 0
    return _full


def load_region(region):
    full = load_map()
    s = float(region.get("scale", 1.0))
    x0, y0 = int(region["x0"]), int(region["y0"])
    w = max(32, int(int(region["w"]) * s))
    h = max(32, int(int(region["h"]) * s))
    margin = 200
    H, W = full.shape
    xs = max(0, x0 - margin)
    ys = max(0, y0 - margin)
    xe = min(W, x0 + w + margin)
    ye = min(H, y0 + h + margin)
    sub = full[ys:ye, xs:xe]
    d_sub = sc.edt(sub, down=int(STATE["nodes"]["distance"]["down"]))
    d = d_sub[(y0 - ys):(y0 - ys) + min(h, H - y0), (x0 - xs):(x0 - xs) + min(w, W - x0)]
    land = sub[(y0 - ys):(y0 - ys) + d.shape[0], (x0 - xs):(x0 - xs) + d.shape[1]]
    return land, d


def sig(*parts):
    return hashlib.sha1(json.dumps(parts, sort_keys=True, default=float).encode()).hexdigest()[:16]


def mask_field(mask, ctx, fallback=None):
    src = mask["source"]
    if src == "distance":
        v = ctx["d"]
    elif src == "height":
        tap = mask.get("tap") or ""
        v = ctx.get("fields", {}).get(tap)
        if v is None:
            v = fallback if fallback is not None else ctx["h"]
    else:
        v = sc.fbm(ctx["d"].shape[1], ctx["d"].shape[0], max(8.0, float(mask["scale"])),
                   4, seed=int(mask.get("seed", 3)))
    lo, hi = float(mask["lo"]), float(mask["hi"])
    t = sc.smoothstep((v - lo) / max(1e-6, hi - lo))
    return 1.0 - t if mask["invert"] else t


def mask_weight(node, ctx, nid=None):
    ids = node.get("masks") or []
    if not ids:
        return 1.0
    w = 1.0
    for mid in ids:
        m = STATE["masks"].get(mid)
        if m:
            w = w * mask_field(m, ctx, ctx.get("h"))
    return w


# --------------------------------------------------------------------------
# nodes
# --------------------------------------------------------------------------
def n_distance(node, ctx, prev_sig):
    return ctx["d"], None


def n_warp(node, ctx, prev_sig):
    d = ctx["d"]
    H, W = d.shape
    w1 = (sc.fbm(W, H, float(node["scale"]), 3, seed=21) - 0.5) * float(node["tiles"])
    w2 = (sc.fbm(W, H, float(node["scale"]), 3, seed=22) - 0.5) * float(node["tiles"])
    yi, xi = np.mgrid[0:H, 0:W]
    out = d[np.clip((yi + w1).astype(np.int32), 0, H - 1),
            np.clip((xi + w2).astype(np.int32), 0, W - 1)]
    return out.astype(np.float32), None


def n_height(node, ctx, prev_sig):
    d = ctx["d"]
    if node.get("mode", "profile") == "ramp":
        h = np.clip(1.0 - d / float(node["deep_dist"]), 0, 1) ** float(node["curve"])
    else:
        # piecewise margin profile: flat shelf, a fast shelf break, a slope,
        # then the abyssal plain
        dmax = float(node["deep_dist"])
        xs = np.array([0.0, float(node["shelf_t"]) * dmax,
                       float(node["slope_t"]) * dmax, dmax], dtype=np.float32)
        ys = np.array([1.0, 0.93, 0.45, 0.0], dtype=np.float32)
        h = np.interp(np.clip(d, 0, dmax), xs, ys).astype(np.float32)
    return h, None


def n_noise(node, ctx, prev_sig):
    h = ctx["h"]
    H, W = h.shape
    n = sc.fbm(W, H, float(node["scale"]), int(node["octaves"]), seed=int(node["seed"])) - 0.5
    w = mask_weight(node, ctx) * float(node["amp"])
    return np.clip(h + 2.0 * w * n, 0, 1), None


def n_features(node, ctx, prev_sig):
    h = ctx["h"]
    H, W = h.shape
    n = sc.fbm(W, H, float(node["scale"]), int(node["octaves"]), seed=int(node["seed"]))
    bumps = np.clip((n - float(node["density"])) / max(1e-3, 1.0 - float(node["density"])), 0, 1) ** 1.6
    w = mask_weight(node, ctx) * float(node["amp"])
    return np.clip(h + w * bumps, 0, 1), None


def n_erode(node, ctx, prev_sig):
    h = ctx["h"]
    p = 1.0 / 2.0
    H, W = h.shape
    SW, SH = max(8, int(W * p)), max(8, int(H * p))
    hs = np.array(Image.fromarray(h, mode="F").resize((SW, SH), Image.BICUBIC))
    he = sc.slab_erode(hs, window=max(2, int(round(float(node["window"]) * p))),
                       layers=int(node["layers"]))
    he = sc.upscale(he, (W, H))
    mix = float(node["mix"]) * mask_weight(node, ctx)
    return np.clip(h * (1 - mix) + he * mix, 0, 1), None


def n_shelf(node, ctx, prev_sig):
    h = ctx["h"]
    w = mask_weight(node, ctx)
    gain = 1.0 + (float(node["gain"]) - 1.0) * w
    return np.clip(h ** float(node["power"]) * gain, 0, 1), None


def n_shade(node, ctx, prev_sig):
    return ctx["h"], None


def n_palette(node, ctx, prev_sig):
    t = np.clip(1.0 - ctx["h"], 0, 1)
    rgb = np.stack([np.interp(t, sc.PALETTE_STOPS, sc.PALETTE_COLS[:, i]) for i in range(3)], -1)
    return rgb, None


def n_coast(node, ctx, prev_sig):
    rgb = ctx["color"]
    levels = max(1, int(node["levels"]))
    band = np.zeros(ctx["land"].shape, dtype=np.float32)
    cur = ctx["land"].copy()
    for k in range(1, levels + 1):
        nxt = sc.dilate1(cur)
        band = np.where(nxt & ~cur, np.float32(k), band)
        cur = nxt
    rim = np.clip(1.0 - (band - 1.0) / max(1.0, levels - 1.0), 0, 1)
    add = np.array([node["r"], node["g"], node["b"]], dtype=np.float32) * float(node["strength"])
    return rgb + add * rim[:, :, None], None


def n_output(node, ctx, prev_sig):
    rgb = ctx["color"].copy()
    land = ctx["land"]
    if node.get("land_t") is not None:
        t = np.clip(1.0 - np.where(land, 1.0 - float(node["land_t"]), 1.0), 0, 1)
        # land fill keeps the top of the ramp so the shore stays the brightest
        fill = np.array([sc.PALETTE_COLS[0][i] for i in range(3)], dtype=np.float32)
        rgb = np.where(land[:, :, None], fill, rgb)
    return np.clip(rgb, 0, 255).astype(np.uint8), None



# --------------------------------------------------------------------------
# stream-power drainage
# --------------------------------------------------------------------------
def flow_dirs(h):
    """D8 steepest descent. Returns (receiver flat index or -1, slope)."""
    H, W = h.shape
    idx = np.arange(H * W, dtype=np.int64).reshape(H, W)
    best = np.full((H, W), -1, dtype=np.int64)
    bestdrop = np.zeros((H, W), dtype=np.float32)
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            if dy == 0 and dx == 0:
                continue
            y0, y1 = max(0, -dy), H - max(0, dy)
            x0, x1 = max(0, -dx), W - max(0, dx)
            nb = h[y0 + dy:y1 + dy, x0 + dx:x1 + dx]
            ni = idx[y0 + dy:y1 + dy, x0 + dx:x1 + dx]
            dist = 1.41421356 if (dy and dx) else 1.0
            slope = (h[y0:y1, x0:x1] - nb) / dist
            b = slope > bestdrop[y0:y1, x0:x1]
            bestdrop[y0:y1, x0:x1] = np.where(b, slope, bestdrop[y0:y1, x0:x1])
            best[y0:y1, x0:x1] = np.where(b, ni, best[y0:y1, x0:x1])
    return best.ravel(), bestdrop


def flow_accum(rec, order):
    """upstream cell count, accumulated downhill (order = high to low)"""
    acc = np.ones(rec.size, dtype=np.float32)
    for i in order:
        r = rec[i]
        if r >= 0:
            acc[r] += acc[i]
    return acc


def n_drainage(node, ctx, prev_sig):
    """stream-power incision: carve where drainage area and slope are high"""
    h = ctx["h"]
    H, W = h.shape
    sc_ = max(1, int(node["scale"]))
    SW, SH = max(8, W // sc_), max(8, H // sc_)
    cur = np.array(Image.fromarray(h, mode="F").resize((SW, SH), Image.BICUBIC))
    dh = np.zeros((SH, SW), dtype=np.float32)

    for _ in range(max(1, int(node["steps"]))):
        rec, slope = flow_dirs(cur)
        order = np.argsort(-cur.ravel(), kind="stable")
        acc = flow_accum(rec, order)
        A = (acc / max(1.0, float(acc.max()))) ** float(node["m"])
        S = np.clip(slope.ravel() / max(1e-6, float(slope.max())), 0, 1) ** float(node["n"])
        step = np.clip(float(node["k"]) * A * S, 0, float(node.get("cap", 0.1))).reshape(SH, SW)
        cur = np.clip(cur - step, 0, 1)
        dh += step

    dh_up = np.array(Image.fromarray(dh, mode="F").resize((W, H), Image.BICUBIC))
    w = mask_weight(node, ctx) * float(node.get("strength", 1.0))
    return np.clip(h - dh_up * w, 0, 1), None


SCALAR_NODES = {"height", "noise", "features", "erode", "drainage", "shelf"}

ORDER = [
    ("distance", n_distance), ("warp", n_warp), ("height", n_height),
    ("noise", n_noise), ("features", n_features), ("erode", n_erode),
    ("drainage", n_drainage), ("shelf", n_shelf), ("shade", n_shade), ("palette", n_palette),
    ("coast", n_coast), ("output", n_output),
]


def evaluate(progress=None):
    """run the chain, reusing cache entries whose signature still matches"""
    global _cache
    region = STATE["region"]
    land, d = load_region(region)
    ctx = {"land": land, "d": d, "h": None, "color": None}
    chain_sig = "0"
    new_cache = {}

    total = len(ORDER)
    for i, (nid, fn) in enumerate(ORDER):
        if progress:
            progress(i, total, nid)
        _render["node"] = nid
        node = STATE["nodes"][nid]
        chain_sig = sig(chain_sig, nid, node, [STATE["masks"][m] for m in node.get("masks", [])],
                        region.get("x0"), region.get("y0"), region.get("w"),
                        region.get("h"), region.get("scale"))
        cached = _cache.get(nid)
        if cached and cached[0] == chain_sig:
            val = cached[1]
        else:
            if not node.get("enabled", True):
                val = None
            else:
                val, _ = fn(node, ctx, chain_sig)
                pts = node.get("out_curve")
                if (isinstance(val, np.ndarray) and nid in SCALAR_NODES
                        and pts and len(pts) >= 2):
                    xs = [float(p[0]) for p in pts]
                    ys = [float(p[1]) for p in pts]
                    val = np.interp(val, xs, ys).astype(np.float32)
            _cache[nid] = (chain_sig, val)
        new_cache[nid] = (chain_sig, val)

        ctx.setdefault("fields", {})
        if isinstance(val, np.ndarray) and val.ndim == 2:
            ctx["fields"][nid] = val
        if nid == "distance":
            ctx["d"] = val if val is not None else ctx["d"]
        elif nid in ("warp",):
            ctx["d"] = val if val is not None else ctx["d"]
        elif nid in ("height", "noise", "features", "erode", "drainage", "shelf", "shade"):
            ctx["h"] = val if val is not None else ctx["h"]
        elif nid == "palette":
            ctx["color"] = val if val is not None else np.zeros((*land.shape, 3), np.float32)
        elif nid == "coast":
            ctx["color"] = val if val is not None else ctx["color"]
        elif nid == "output":
            ctx["out"] = val

    _cache = new_cache
    return ctx


def channel_image(ctx, channel):
    if channel in STATE["masks"]:
        v = mask_field(STATE["masks"][channel], ctx, ctx.get("h"))
        v = np.asarray(v, dtype=np.float32)
        return np.stack([v * 255] * 3, -1).astype(np.uint8)
    if channel == "output":
        rgb = ctx.get("out")
        if rgb is None:
            rgb = np.clip(ctx["color"], 0, 255).astype(np.uint8)
        return rgb

    val = ctx.get(channel)
    if val is None:
        if channel == "d":
            val = ctx["d"]
        else:
            val = ctx.get("h")
    if val is None:
        val = np.zeros(ctx["land"].shape, np.float32)
    v = np.asarray(val, dtype=np.float32)
    lo, hi = float(v.min()), float(v.max())
    v = (v - lo) / max(1e-6, hi - lo)
    return np.stack([v * 255] * 3, -1).astype(np.uint8)


def _progress(stage, total, node):
    _render.update(stage=stage, total=total, node=node,
                   elapsed=round(time.time() - _render["started"], 2))


def render_now(auto=False):
    """render once into the in-memory PNG (serialised by _render_lock)"""
    global _png
    if _render["running"]:
        return False
    with _render_lock:
        _render.update(running=True, stage=0, total=len(ORDER), node="start",
                       started=time.time(), elapsed=0.0, error=None)
        t0 = time.time()
        try:
            ctx = evaluate(_progress)
            rgb = channel_image(ctx, STATE.get("channel", "output"))
            buf = io.BytesIO()
            Image.fromarray(rgb, "RGB").save(buf, "PNG")
            with _lock:
                _png = buf.getvalue()
                _render["stamp"] += 1
        except Exception as exp:
            _render["error"] = str(exp)
        finally:
            _render.update(running=False, last_ms=int((time.time() - t0) * 1000),
                           elapsed=round(time.time() - t0, 2), node="done")
    return True


def render_async():
    t = threading.Thread(target=render_now, daemon=True)
    t.start()
    return True


_auto_timer = None


def schedule_render(delay=0.35):
    """auto mode: debounce, and re-queue if a render is already running"""
    global _auto_timer
    if _auto_timer is not None:
        _auto_timer.cancel()

    def fire():
        if not render_now():
            schedule_render(0.3)

    _auto_timer = threading.Timer(delay, fire)
    _auto_timer.daemon = True
    _auto_timer.start()


# --------------------------------------------------------------------------
# http
# --------------------------------------------------------------------------
PAGE = r"""<!doctype html><meta charset=utf-8><title>sea editor</title>
<style>
:root{--b:#1c2530;--p:#0d141b;--t:#c9d6e2;--a:#3f7fa6}
*{box-sizing:border-box} body{margin:0;background:var(--p);color:var(--t);
font:12px/1.4 ui-monospace,monospace;display:flex;height:100vh;overflow:hidden}
#left,#right{width:260px;background:var(--b);overflow:auto;padding:8px;flex:0 0 auto}
#mid{flex:1;display:flex;flex-direction:column;min-width:0}
#top{display:flex;gap:6px;padding:6px;background:var(--b);align-items:center;flex-wrap:wrap}
#prev{flex:1;overflow:hidden;position:relative;cursor:grab;display:flex;
align-items:center;justify-content:center;background:#0a1016}
#prev img{position:static;image-rendering:pixelated;max-width:100%;max-height:100%}
.node{padding:2px 5px;cursor:pointer;border-radius:3px;display:flex;gap:6px;align-items:center}
.node.sel{background:#24405a}.node input{cursor:pointer}
.x{margin-left:auto;color:#8fa6ba;padding:0 4px}.x:hover{color:#e08a8a}
label{display:block;margin:5px 0 2px;color:#8fa6ba}
input[type=number],input[type=text],select{width:100%;background:#0b1117;color:var(--t);
border:1px solid #2a3a4b;padding:2px 4px}
button{background:#24405a;color:var(--t);border:0;padding:4px 8px;cursor:pointer;border-radius:3px}
h3{margin:8px 0 4px;font-size:11px;color:#8fa6ba;text-transform:uppercase;letter-spacing:.08em}
#barwrap{height:18px;background:#0a1016;position:relative;flex:0 0 auto}
#bar{height:100%;width:0;background:#2f6a8c;transition:width .12s linear}
#prog{position:absolute;left:8px;top:0;line-height:18px;font-size:11px;color:#cfe0ee}
canvas.curve{background:#0b1117;border:1px solid #2a3a4b;display:block;margin-top:4px}
.small{color:#7d93a6}
</style>
<div id=left><h3>pipeline</h3><div id=nodes></div>
  <h3>masks <button id=addmask style="padding:1px 6px">+</button></h3><div id=masks></div></div>
<div id=mid>
  <div id=top>
    x<input id=rx type=number size=5> y<input id=ry type=number size=5>
    w<input id=rw type=number size=5> h<input id=rh type=number size=5>
    <select id=scale><option value=1>1 px/tile<option value=0.5>0.5 px/tile</select>
    <select id=chan></select>
    <button id=go>render</button>
    <label style="display:inline;color:var(--t)"><input id=auto type=checkbox> auto</label>
    <select id=presets style="width:auto;min-width:90px"></select>
    <button id=load>load</button><button id=save>save</button><button id=reset>reset</button>
  </div>
  <div id=barwrap><div id=bar></div><span id=prog></span></div>
  <div id=prev><img id=img></div>
</div>
<div id=right><h3 id=nodeTitle>select a node</h3><div id=params></div>
  <div id=curvewrap></div><h3>masks wired</h3><div id=wired></div></div>
<script>
let S=null, sel=null, lastStamp=-1, curveDrag=-1, panDrag=null;
async function api(path, body){const r=await fetch(path,{method:body?'POST':'GET',
  headers:{'content-type':'application/json'},body:body?JSON.stringify(body):undefined});
  return r.headers.get('content-type')?.includes('json')?r.json():r.text();}
async function poll(){
  const s=await api('/api/status');
  const pct=s.running&&s.total?Math.round(100*s.stage/s.total):100;
  document.getElementById('bar').style.width=pct+'%';
  document.getElementById('prog').textContent=s.running
    ?`${s.node} ${s.stage}/${s.total} ~${s.elapsed}s`
    :(s.error?('error: '+s.error):(s.last_ms?`${s.last_ms} ms`:'idle'));
  if(s.stamp!==lastStamp){lastStamp=s.stamp;
    document.getElementById('img').src='/api/preview?t='+s.stamp;}
  if(s.running)setTimeout(poll,300);
}
function render(){api('/api/render',{}).then(()=>setTimeout(poll,120));}
async function boot(){
  S=await api('/api/state');
  document.getElementById('auto').checked=!!S.auto;
  rx.value=S.region.x0; ry.value=S.region.y0; rw.value=S.region.w; rh.value=S.region.h;
  scale.value=String(S.region.scale);
  renderTop(); renderNodes(); renderMasks(); select(S.last||'erode');
  loadPresets(); render();
}
function renderTop(){
  chan.innerHTML=S.channels.map(c=>`<option ${c===S.channel?'selected':''}>${c}</option>`).join('');
}
function renderNodes(){
  const el=document.getElementById('nodes'); el.innerHTML='';
  for(const id of S.order){
    const n=S.nodes[id]; if(!n) continue;
    const d=document.createElement('div'); d.className='node'+(sel===id?' sel':'');
    const cb=document.createElement('input'); cb.type='checkbox'; cb.checked=n.enabled;
    cb.onclick=e=>{e.stopPropagation(); set('nodes',id,'enabled',cb.checked);};
    d.appendChild(cb); d.appendChild(document.createTextNode(id));
    d.onclick=()=>select(id); el.appendChild(d);
  }
}
function renderMasks(){
  const el=document.getElementById('masks'); el.innerHTML='';
  for(const id of Object.keys(S.masks)){
    const m=S.masks[id];
    const d=document.createElement('div'); d.className='node'+(sel===id?' sel':'');
    d.appendChild(document.createTextNode(id+' · '+m.source));
    const x=document.createElement('span'); x.className='x'; x.textContent='×';
    x.onclick=e=>{e.stopPropagation(); api('/api/mask/del',{id}).then(reload);};
    d.appendChild(x); d.onclick=()=>select(id); el.appendChild(d);
  }
}
async function loadPresets(){
  const r=await api('/api/presets');
  document.getElementById('presets').innerHTML=r.presets.map(n=>`<option>${n}</option>`).join('');
}
async function reload(){S=await api('/api/state'); renderTop(); renderNodes();
  renderMasks(); renderParams();}
async function select(id){sel=id; await api('/api/select',{id});
  document.getElementById('nodeTitle').textContent=id;
  renderParams(); renderNodes(); renderMasks();}
function field(kind,id,k,v,opts){
  const lab=document.createElement('label'); lab.textContent=k;
  let inp;
  if(kind==='mask'&&k==='tap'||opts&&opts.select){
    inp=document.createElement('select');
    inp.innerHTML=(opts&&opts.options||[]).map(o=>`<option ${o===v?'selected':''}>${o}</option>`).join('');
    inp.onchange=()=>set(kind,id,k,inp.value);
  } else if(typeof v==='boolean'){
    inp=document.createElement('input'); inp.type='checkbox'; inp.checked=v;
    inp.onchange=()=>set(kind,id,k,inp.checked);
  } else if(typeof v==='number'){
    inp=document.createElement('input'); inp.type='number'; inp.step='any'; inp.value=v;
    inp.onchange=()=>set(kind,id,k,parseFloat(inp.value));
  } else {
    inp=document.createElement('input'); inp.type='text'; inp.value=v;
    inp.onchange=()=>set(kind,id,k,inp.value);
  }
  return [lab,inp];
}
function renderParams(){
  const el=document.getElementById('params'); el.innerHTML='';
  document.getElementById('curvewrap').innerHTML='';
  const node=S.nodes[sel], mask=S.masks[sel];
  if(mask){
    for(const [k,v] of Object.entries(mask)){
      if(k==='tap'){
        const o=[''].concat(S.taps);
        el.append(...field('mask',sel,k,v,{select:true,options:o}));
      } else if(k==='source'){
        el.append(...field('mask',sel,k,v,{select:true,options:['height','distance','noise']}));
      } else {
        el.append(...field('mask',sel,k,v));
      }
    }
  } else if(node){
    for(const [k,v] of Object.entries(node)){
      if(k==='masks'||k==='out_curve') continue;
      el.append(...field('nodes',sel,k,v));
    }
    if(node.out_curve){
      const w=document.getElementById('curvewrap');
      const h=document.createElement('h3'); h.textContent='output curve';
      w.appendChild(h); w.appendChild(curveUI(node.out_curve,
        pts=>set('nodes',sel,'out_curve',pts)));
    }
    const wired=document.getElementById('wired'); wired.innerHTML='';
    for(const mid of Object.keys(S.masks)){
      const l=document.createElement('label'); const cb=document.createElement('input');
      cb.type='checkbox'; cb.checked=(node.masks||[]).includes(mid);
      cb.onchange=()=>{const arr=new Set(node.masks||[]);
        cb.checked?arr.add(mid):arr.delete(mid); set('nodes',sel,'masks',[...arr]);};
      l.appendChild(cb); l.appendChild(document.createTextNode(mid)); wired.appendChild(l);
    }
  } else { document.getElementById('wired').innerHTML=''; }
}
function curveUI(pts, cb){
  const size=180, c=document.createElement('canvas'); c.width=c.height=size; c.className='curve';
  const g=c.getContext('2d');
  function draw(){
    g.fillStyle='#0b1117'; g.fillRect(0,0,size,size);
    g.strokeStyle='#22303d'; g.lineWidth=1;
    for(let i=1;i<4;i++){g.beginPath();g.moveTo(i*size/4,0);g.lineTo(i*size/4,size);
      g.moveTo(0,i*size/4);g.lineTo(size,i*size/4);g.stroke();}
    const P=[...pts].sort((a,b)=>a[0]-b[0]);
    g.strokeStyle='#4f93bb'; g.lineWidth=2; g.beginPath();
    P.forEach(([x,y],i)=>{const px=x*size,py=(1-y)*size;i?g.lineTo(px,py):g.moveTo(px,py);});
    g.stroke(); g.fillStyle='#dbe7f1';
    P.forEach(([x,y])=>{g.beginPath();g.arc(x*size,(1-y)*size,4,0,7);g.fill();});
  }
  const pos=e=>{const r=c.getBoundingClientRect();
    return [(e.clientX-r.left)/size,1-(e.clientY-r.top)/size];};
  c.onmousedown=e=>{ if(e.button!==0) return; const [x,y]=pos(e);
    let best=-1,bd=1e9; pts.forEach((p,i)=>{const d=(p[0]-x)**2+(p[1]-y)**2; if(d<bd){bd=d;best=i;}});
    if(bd<0.02){curveDrag=best;} else {pts.push([x,y]);curveDrag=pts.length-1;}
    draw(); };
  c.oncontextmenu=e=>{e.preventDefault(); const [x,y]=pos(e);
    let best=-1,bd=1e9; pts.forEach((p,i)=>{const d=(p[0]-x)**2+(p[1]-y)**2; if(d<bd){bd=d;best=i;}});
    if(bd<0.02&&pts.length>2){pts.splice(best,1);draw();cb(pts);}};
  addEventListener('mousemove',e=>{ if(curveDrag<0)return; const [x,y]=pos(e);
    pts[curveDrag]=[Math.max(0,Math.min(1,x)),Math.max(0,Math.min(1,y))]; draw(); });
  addEventListener('mouseup',()=>{ if(curveDrag>=0){curveDrag=-1;cb(pts);} });
  draw(); return c;
}
async function set(kind,id,k,v){
  await api('/api/set',{id,k,v}); S=await api('/api/state');
  if(S.auto) setTimeout(poll,120); else poll();
}
document.getElementById('go').onclick=render;
document.getElementById('addmask').onclick=()=>api('/api/mask/add',{}).then(reload);
document.getElementById('auto').onchange=e=>api('/api/auto',{on:e.target.checked});
document.getElementById('reset').onclick=async()=>{ if(!confirm('reset to defaults?'))return;
  await api('/api/reset',{}); await boot(); };
const presets=document.getElementById('presets');
document.getElementById('save').onclick=()=>{const n=prompt('save preset as',
  presets.value||'preset'); if(n) api('/api/preset/save',{name:n}).then(loadPresets);};
document.getElementById('load').onclick=async()=>{const n=presets.value;
  if(n){await api('/api/preset/load',{name:n}); await boot();}};
const region=()=>({x0:+rx.value,y0:+ry.value,w:+rw.value,h:+rh.value,scale:+scale.value});
for(const id of ['rx','ry','rw','rh']) document.getElementById(id).onchange=()=>{
  const r=region(); api('/api/set',{id:'region',k:'all',v:r}).then(()=>{S.region=r;
    if(S.auto)render();else poll();});};
document.getElementById('scale').onchange=()=>{const r=region();
  api('/api/set',{id:'region',k:'all',v:r}).then(()=>{S.region=r;
    if(S.auto)render();else poll();});};
document.getElementById('chan').onchange=()=>{api('/api/set',{id:'channel',k:'all',v:chan.value})
  .then(()=>{ if(S.auto)render(); else poll();});};
const prev=document.getElementById('prev');
prev.onmousedown=e=>{panDrag={x:e.clientX,y:e.clientY,x0:+rx.value,y0:+ry.value};prev.style.cursor='grabbing';};
addEventListener('mousemove',e=>{if(!panDrag)return;const dx=e.clientX-panDrag.x,dy=e.clientY-panDrag.y;
  rx.value=panDrag.x0-Math.round(dx/S.region.scale); ry.value=panDrag.y0-Math.round(dy/S.region.scale);});
addEventListener('mouseup',()=>{if(!panDrag)return;panDrag=null;prev.style.cursor='grab';
  const r=region(); api('/api/set',{id:'region',k:'all',v:r}).then(()=>{S.region=r;
    if(S.auto)render();else poll();});});
boot();
</script>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _png(self, data):
        self.send_response(200)
        self.send_header("content-type", "image/png")
        self.send_header("content-length", str(len(data)))
        self.send_header("cache-control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/":
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/api/preview"):
            if _png is None:
                self.send_response(204)
                self.end_headers()
            else:
                self._png(_png)
        elif self.path == "/api/presets":
            names = sorted(f[:-5] for f in os.listdir(PRESETS)) if os.path.isdir(PRESETS) else []
            self._json({"presets": names})
        elif self.path == "/api/status":
            with _lock:
                data = dict(_render)
                data["has_image"] = _png is not None
            self._json(data)
        elif self.path == "/api/state":
            with _lock:
                st = {"region": STATE["region"], "channel": STATE["channel"],
                      "nodes": STATE["nodes"], "masks": STATE["masks"],
                      "order": [n for n, _ in ORDER],
                      "channels": ["output", "h", "d", "color"] + list(STATE["masks"].keys()),
                      "taps": [n for n, _ in ORDER if n in SCALAR_NODES],
                      "last": STATE.get("last", "erode"), "auto": _render["auto"]}
            self._json(st)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        n = int(self.headers.get("content-length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        with _lock:
            if self.path == "/api/mask/add":
                n = STATE.get("next_mask", 1)
                State_mid = f"mask_{n}"
                STATE["next_mask"] = n + 1
                STATE["masks"][State_mid] = dict(
                    source="height", tap="", scale=400, lo=0.2, hi=0.8,
                    feather=1.0, invert=False)
            elif self.path == "/api/mask/del":
                mid = body.get("id")
                STATE["masks"].pop(mid, None)
                for node in STATE["nodes"].values():
                    if isinstance(node.get("masks"), list) and mid in node["masks"]:
                        node["masks"].remove(mid)
            elif self.path == "/api/reset":
                STATE.clear()
                STATE.update(default_state())
                _cache.clear()
                _png = None
            elif self.path == "/api/auto":
                _render["auto"] = bool(body.get("on"))
            elif self.path == "/api/set":
                nid, key, val = body["id"], body["k"], body["v"]
                if nid == "region":
                    STATE["region"] = val
                elif nid == "channel":
                    STATE["channel"] = val
                elif nid in STATE["nodes"]:
                    if key == "all":
                        STATE["nodes"][nid].update(val)
                    else:
                        STATE["nodes"][nid][key] = val
                elif nid in STATE["masks"]:
                    if key == "all":
                        STATE["masks"][nid].update(val)
                    else:
                        STATE["masks"][nid][key] = val
            elif self.path == "/api/render":
                pass          # started outside the lock, below
            elif self.path == "/api/select":
                STATE["last"] = body.get("id")
            elif self.path == "/api/preset/save":
                os.makedirs(PRESETS, exist_ok=True)
                path = os.path.join(PRESETS, body["name"] + ".json")
                with open(path, "w") as f:
                    json.dump({"region": STATE["region"], "nodes": STATE["nodes"],
                               "masks": STATE["masks"]}, f, indent=1)
            elif self.path == "/api/preset/load":
                path = os.path.join(PRESETS, body["name"] + ".json")
                with open(path) as f:
                    data = json.load(f)
                STATE["region"] = data["region"]
                for k in ("nodes", "masks"):
                    for nid, params in data[k].items():
                        STATE[k].setdefault(nid, {}).update(params)
                _cache.clear()
        # outside the state lock: never hold it while rendering
        if self.path == "/api/render":
            render_async()
        elif self.path == "/api/set" and _render["auto"]:
            schedule_render()
        self._json({"ok": True})


def main():
    global PORT
    ap = argparse.ArgumentParser(description="sea texture editor")
    ap.add_argument("--port", type=int, default=PORT,
                    help="port to serve on (default 3007); use a second port to "
                         "keep an agent's experiments off your own session")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    PORT = args.port
    load_map()
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(f"http://127.0.0.1:{PORT}")).start()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"sea editor on http://127.0.0.1:{PORT}  (ctrl-c to stop)")
    srv.serve_forever()


if __name__ == "__main__":
    main()
