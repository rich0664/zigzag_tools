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
import json
import os
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
PORT = 3007

_lock = threading.Lock()
_full = None          # whole-map land mask
_cache = {}           # node id -> (signature, value)
_version = 0


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
                           curve=1.0, shelf_t=0.10, slope_t=0.32, abyss_t=1.0),
            "noise": dict(enabled=False, amp=0.15, scale=350, octaves=4, seed=11,
                          blend="add", masks=[]),
            "features": dict(enabled=False, amp=0.12, scale=70, density=0.70,
                             octaves=4, seed=9, masks=[]),
            "erode": dict(enabled=True, window=6, layers=20, mix=1.0, masks=[]),
            "shelf": dict(enabled=False, gain=1.0, power=1.0, masks=[]),
            "shade": dict(enabled=False, amp=1.45, lo=0.76, hi=1.24),
            "palette": dict(enabled=True),
            "coast": dict(enabled=False, levels=4, strength=0.13,
                          r=95, g=155, b=195),
            "output": dict(enabled=True, land_t=0.58),
        },
        "masks": {
            "depth_band": dict(source="height", scale=400, lo=0.15, hi=0.70,
                               feather=0.1, invert=False),
            "open_water": dict(source="distance", scale=400, lo=40, hi=160,
                               feather=1.0, invert=False),
            "big_noise": dict(source="noise", scale=900, lo=0.3, hi=0.7,
                              feather=1.0, invert=False),
        },
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


def mask_field(mask, ctx):
    src = mask["source"]
    if src == "distance":
        v = ctx["d"]
    elif src == "height":
        v = ctx["h"]
    else:
        v = sc.fbm(ctx["d"].shape[1], ctx["d"].shape[0], max(8.0, float(mask["scale"])),
                   4, seed=int(mask.get("seed", 3)))
    lo, hi = float(mask["lo"]), float(mask["hi"])
    t = sc.smoothstep((v - lo) / max(1e-6, hi - lo))
    return 1.0 - t if mask["invert"] else t


def mask_weight(node, ctx):
    ids = node.get("masks") or []
    if not ids:
        return 1.0
    w = 1.0
    for mid in ids:
        m = STATE["masks"].get(mid)
        if m:
            w = w * mask_field(m, ctx)
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


ORDER = [
    ("distance", n_distance), ("warp", n_warp), ("height", n_height),
    ("noise", n_noise), ("features", n_features), ("erode", n_erode),
    ("shelf", n_shelf), ("shade", n_shade), ("palette", n_palette),
    ("coast", n_coast), ("output", n_output),
]


def evaluate():
    """run the chain, reusing cache entries whose signature still matches"""
    global _cache
    region = STATE["region"]
    land, d = load_region(region)
    ctx = {"land": land, "d": d, "h": None, "color": None}
    chain_sig = "0"
    new_cache = {}

    for nid, fn in ORDER:
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
            _cache[nid] = (chain_sig, val)
        new_cache[nid] = (chain_sig, val)

        if nid == "distance":
            ctx["d"] = val if val is not None else ctx["d"]
        elif nid in ("warp",):
            ctx["d"] = val if val is not None else ctx["d"]
        elif nid in ("height", "noise", "features", "erode", "shelf", "shade"):
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


def preview_png():
    with _lock:
        ctx = evaluate()
        rgb = channel_image(ctx, STATE.get("channel", "output"))
    buf = io.BytesIO()
    Image.fromarray(rgb, "RGB").save(buf, "PNG")
    return buf.getvalue()


# --------------------------------------------------------------------------
# http
# --------------------------------------------------------------------------
PAGE = r"""<!doctype html><meta charset=utf-8><title>sea editor</title>
<style>
:root{--b:#1c2530;--p:#0d141b;--t:#c9d6e2;--a:#3f7fa6}
*{box-sizing:border-box} body{margin:0;background:var(--p);color:var(--t);
font:12px/1.4 ui-monospace,monospace;display:flex;height:100vh;overflow:hidden}
#left,#right{width:250px;background:var(--b);overflow:auto;padding:8px}
#mid{flex:1;display:flex;flex-direction:column;min-width:0}
#top{display:flex;gap:8px;padding:6px;background:var(--b);align-items:center;flex-wrap:wrap}
#prev{flex:1;overflow:hidden;position:relative;cursor:grab;display:flex;
align-items:center;justify-content:center;background:#0a1016}
#prev img{position:static;image-rendering:pixelated;max-width:100%;max-height:100%}
#top input[type=number]{width:64px}
.node{padding:3px 6px;cursor:pointer;border-radius:3px;display:flex;gap:6px;align-items:center}
.node.sel{background:#24405a}.node input{cursor:pointer}
label{display:block;margin:6px 0 2px;color:#8fa6ba}
input[type=range]{width:100%} input[type=number],input[type=text],select{width:100%;
background:#0b1117;color:var(--t);border:1px solid #2a3a4b;padding:2px 4px}
button{background:#24405a;color:var(--t);border:0;padding:4px 8px;cursor:pointer;border-radius:3px}
h3{margin:8px 0 4px;font-size:11px;color:#8fa6ba;text-transform:uppercase;letter-spacing:.08em}
</style>
<div id=left><h3>pipeline</h3><div id=nodes></div><h3>masks</h3><div id=masks></div></div>
<div id=mid>
  <div id=top>
    x<input id=rx type=number size=5> y<input id=ry type=number size=5>
    w<input id=rw type=number size=5> h<input id=rh type=number size=5>
    <select id=scale><option value=1>1 px/tile<option value=0.5>0.5 px/tile</select>
    <select id=chan></select>
    <button id=save>save preset</button><button id=load>load preset</button>
  </div>
  <div id=prev><img id=img></div>
</div>
<div id=right><h3 id=nodeTitle>select a node</h3><div id=params></div><h3>masks wired</h3><div id=wired></div></div>
<script>
let S=null, sel=null;
async function api(path, body){const r=await fetch(path,{method:body?'POST':'GET',
  headers:{'content-type':'application/json'},body:body?JSON.stringify(body):undefined});
  return r.headers.get('content-type')?.includes('json')?r.json():r.text();}
function refresh(){document.getElementById('img').src='/api/preview?v='+Date.now();}
async function init(){
  S=await api('/api/state');
  renderNodes(); renderMasks(); renderTop(); select(S.last||'erode'); refresh();
}
function renderNodes(){
  const el=document.getElementById('nodes'); el.innerHTML='';
  for(const id of S.order){
    const n=S.nodes[id]; if(!n) continue;
    const d=document.createElement('div'); d.className='node'+(sel===id?' sel':'');
    const cb=document.createElement('input'); cb.type='checkbox'; cb.checked=n.enabled;
    cb.onclick=e=>{e.stopPropagation(); setParam(id,'enabled',cb.checked);};
    d.appendChild(cb); d.appendChild(document.createTextNode(id));
    d.onclick=()=>select(id); el.appendChild(d);
  }
}
function renderMasks(){
  const el=document.getElementById('masks'); el.innerHTML='';
  for(const id of Object.keys(S.masks)){
    const d=document.createElement('div'); d.className='node'+(sel===id?' sel':'');
    d.textContent=id+" ("+S.masks[id].source+")";
    d.onclick=()=>select(id); el.appendChild(d);
  }
}
function renderTop(){
  const r=S.region;
  rx.value=r.x0; ry.value=r.y0; rw.value=r.w; rh.value=r.h; scale.value=String(r.scale);
  chan.innerHTML=S.channels.map(c=>`<option ${c===S.channel?'selected':''}>${c}</option>`).join('');
}
async function select(id){sel=id; await api('/api/select',{id}); renderParams();
  renderNodes(); renderMasks();}
function renderParams(){
  const el=document.getElementById('params'); el.innerHTML='';
  document.getElementById('nodeTitle').textContent=sel;
  const n=S.nodes[sel]||S.masks[sel];
  if(!n) return;
  for(const [k,v] of Object.entries(n)){
    if(k==='enabled'||k==='masks') continue;
    const lab=document.createElement('label'); lab.textContent=k; el.appendChild(lab);
    let inp;
    if(typeof v==='boolean'){inp=document.createElement('input');inp.type='checkbox';inp.checked=v;
      inp.onchange=()=>setParam(sel,k,inp.checked);}
    else if(typeof v==='number'){inp=document.createElement('input');inp.type='number';inp.step='any';inp.value=v;
      inp.onchange=()=>setParam(sel,k,parseFloat(inp.value));}
    else if(typeof v==='string'){inp=document.createElement('input');inp.type='text';inp.value=v;
      inp.onchange=()=>setParam(sel,k,inp.value);}
    else {inp=document.createElement('input');inp.type='text';inp.value=JSON.stringify(v);inp.disabled=true;}
    el.appendChild(inp);
  }
  const wired=document.getElementById('wired'); wired.innerHTML='';
  if(S.nodes[sel]&&S.nodes[sel].masks!==undefined){
    for(const mid of Object.keys(S.masks)){
      const l=document.createElement('label'); const cb=document.createElement('input'); cb.type='checkbox';
      cb.checked=S.nodes[sel].masks.includes(mid);
      cb.onchange=()=>{const arr=new Set(S.nodes[sel].masks);
        cb.checked?arr.add(mid):arr.delete(mid); setParam(sel,'masks',[...arr]);};
      l.appendChild(cb); l.appendChild(document.createTextNode(mid)); wired.appendChild(l);
    }
  }
}
async function setParam(id,k,v){
  await api('/api/set',{id,k,v}); S=await api('/api/state'); refresh();
  if(id===sel) renderParams(); renderNodes();
}
for(const id of ['rx','ry','rw','rh']) document.getElementById(id).onchange=e=>{
  const r={x0:+rx.value,y0:+ry.value,w:+rw.value,h:+rh.value,scale:+scale.value};
  api('/api/set',{id:'region',k:'all',v:r}).then(()=>{S.region=r;refresh();});};
scale.onchange=()=>{const r={...S.region,scale:+scale.value};
  api('/api/set',{id:'region',k:'all',v:r}).then(()=>{S.region=r;refresh();});};
chan.onchange=()=>{api('/api/set',{id:'channel',k:'all',v:chan.value}).then(refresh);};
save.onclick=()=>{const n=prompt('preset name','preset'); if(n) api('/api/preset/save',{name:n}).then(refresh);};
load.onclick=async()=>{const n=prompt('preset name','preset'); if(n){await api('/api/preset/load',{name:n});
  S=await api('/api/state'); renderNodes(); renderMasks(); renderTop(); refresh();}};
let drag=null;
prev.onmousedown=e=>{drag={x:e.clientX,y:e.clientY,x0:+rx.value,y0:+ry.value};prev.style.cursor='grabbing';};
addEventListener('mousemove',e=>{if(!drag)return;const dx=e.clientX-drag.x,dy=e.clientY-drag.y;
  rx.value=drag.x0-Math.round(dx/S.region.scale); ry.value=drag.y0-Math.round(dy/S.region.scale);});
addEventListener('mouseup',()=>{if(!drag)return;drag=null;prev.style.cursor='grab';
  const r={x0:+rx.value,y0:+ry.value,w:+rw.value,h:+rh.value,scale:+scale.value};
  api('/api/set',{id:'region',k:'all',v:r}).then(()=>{S.region=r;refresh();});});
init();
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
            try:
                self._png(preview_png())
            except Exception as exp:  # surface errors in the UI instead of dying
                self.send_response(500)
                self.send_header("content-type", "text/plain")
                self.end_headers()
                self.wfile.write(str(exp).encode())
        elif self.path == "/api/state":
            with _lock:
                st = {"region": STATE["region"], "channel": STATE["channel"],
                      "nodes": STATE["nodes"], "masks": STATE["masks"],
                      "order": [n for n, _ in ORDER],
                      "channels": ["output", "h", "d", "color", "shade"],
                      "last": STATE.get("last", "erode")}
            self._json(st)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        n = int(self.headers.get("content-length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        with _lock:
            if self.path == "/api/set":
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
        self._json({"ok": True})


def main():
    load_map()
    threading.Timer(0.6, lambda: webbrowser.open(f"http://127.0.0.1:{PORT}")).start()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"sea editor on http://127.0.0.1:{PORT}  (ctrl-c to stop)")
    srv.serve_forever()


if __name__ == "__main__":
    main()
