# Sea-zag release checklist

Collected 2026-11-16, before the first public release. These are the gaps between
the current branch (`whale`, on top of `sea-shader`) and something to publish.

## 1. Background cost / static when tabbed out

The sea loop already stops on `document.hidden` and the tab's rAF is throttled,
but a visible-but-background window (second monitor, wallpaper mode) still pays
for the animated sea, and the map itself keeps drawing.

Plan: a menu option **static when tabbed out** (default on). When the page loses
visibility *or focus* for more than a couple of seconds, stop the sea loop and
render one static frame (or none); resume on return. Use `visibilitychange` plus
`blur`/`focus` on `window`, debounced so a quick alt-tab does not thrash. The
whale and waves freeze with it.

## 2. An upper frame cap on top of adaptive fps

Adaptive currently draws 30 fps when still and uncapped while moving, which on a
144 Hz display lets a pan or wallpaper mode run at 144.

Plan: the menu gains a **frame cap** choice: `off / 60 / 30`, applied to *both*
the idle and moving cases, with adaptive still deciding whether idle runs at 30.
So "adaptive + 60" = 30 idle, 60 moving; "off" = the current behaviour. Persist
it. The diagnostics already report fps, so the cap is visible.

## 3. Intro timing and easing

The reveal currently runs 1.6 s linearly with the waves at 1.5-2.9 s; it reads
abrupt and the waves can overlap the shore.

Plan: a 5-6 s total with substantial ease-in/out:
- reveal ~3.2-3.6 s with a smoothstep (or cubic ease-in-out) on `uReveal`, so the
  last of the shore decelerates in rather than stopping;
- the wave ramp starts only after the reveal is essentially complete (≥ 0.95),
  over ~1.8-2.2 s with its own ease;
- the grain/current fade spans the whole sequence;
- the noise fade and the reveal overlap as now.
Keep the depth-ordered threshold; only the time curve changes.

## 4. Repository structure: sea-zag

The project is **sea-zag**. It will have a public repo (the userscript now, a
browser extension later) and a private repo for the generator, presets,
bakes and experiments; only the final userscript and the shipped assets are
published. GitHub Actions later.

Plan: make the split possible without a rewrite:
- keep `src/sea*`, `dist/`, the userscript header and the docs separable from the
  filler script and from `tools/`/`out/`/`prototype/`;
- asset URLs come from a single constant (`assetBase`) so the public repo can be
  switched without code changes;
- the public repo ships `dist/zigzag-sea.user.js` and the asset files only;
  the private repo owns `tools/`, `out/`, `prototype/` and the bakes, and pushes
  the built files to the public one (Actions later);
- versioning already separates the script version, the sea asset version and
  `whaleVersion`.
Add a short `REPO.md` describing the split once the repos exist.

## 5. Only the main exploration map

The userscript matches the whole site and attaches to any deck canvas, so the
sea currently appears on the record shop, community/supporters maps and any
other map view.

Plan: detect the map variant and render only for the exploration map. The app
already has a `MapSplash`/`MapCanvas` variant and distinct loading labels
(`Navigating to the exploration archipelagos` vs the record shop/community/
supporters variants), and distinct routes. Prefer a stable signal (route or a
map-variant store read from the React fiber) over the label text; fail closed
(no sea) when unknown. The menu and the whale follow the same gate. Later, add
the other map types deliberately.

## 6. The sea is half a texel higher than the map

Reported with a screenshot at tile scale. The live camera passes
`ty = MAP_H - 1 - target.y`; the 2D path places the source texel *edge* at the
view centre while the shader samples the texel *centre* (`floor(p)+0.5`), so the
sea sits half a texel off vertically. Fix: `ty = MAP_H - 0.5 - target.y` in the
live camera (and the bench/live-default paths). `debugRender`'s explicit
texture-row contract stays as is.

Verify against the 2D overlay at zoom 8 over a coast: the chosen conversion
should align the shader's shoreline with the tile edge (cross-correlate the two
renders; the correct one is at 0 shift). The half-texel change will shift the
sea relative to the previous parity frames, so the sea reference images need a
one-time refresh.

## Decisions (2026-11-16)

- **History:** clean snapshot import into `sea-zag-core`; the old repo keeps the full history and is tagged `pre-migration`.
- **Source data:** Git LFS for the canonical inputs only — `out/sea/map.png`, `height_full.npy`, `full_interesting_v1.png`, `currents_uv.npy` (and optionally the prototype assets). Everything else in `out/` (dist fields, experiment renders, sheets) is derived and stays out of git.
- **Public repo:** shipped artifacts only — `dist/sea-zag.user.js`, the assets, README, LICENSE, CHANGELOG. No source, no tools.
- **Assets are not releases:** they live at stable paths in the public repo with their own `version` / `whaleVersion` query bumps. A map or look update is an assets-only workflow dispatch, never a tag. Script releases are `vX.Y.Z` tags.
- **Public push:** direct push to `main` from the release workflow for now; no release PR. Revisit if a second look is wanted.
- **Private main:** protected, PR + CI required; agents work on feature branches and open PRs.
- **Rename:** the script becomes `sea-zag` (`@name`, `@namespace`, `dist/sea-zag.user.js`, asset names). Pre-release testers uninstall the old "zig~zag sea depth" script once, because Tampermonkey treats the rename as a new script.
- **Sequence:** finish the basic whale → snapshot-migrate → stand up CI + deploy key + release workflow → dry-run the pipeline → finish whale polish and these checklist items in the private repo → public release.
- **Agent PAT:** private Contents RW, Pull requests RW (if agents open PRs), Workflows RW while setting up Actions; Administration on the public repo only for deploy-key setup.
