# zigzagTools

Tampermonkey userscript that fills the **zig-zag.fm** "Add a release" form
from a **Discogs release link** + a **YouTube playlist link**.

## Status: v0.1 in progress (not installable yet)

## Layout

- `src/` — ESM modules with pure logic (testable in Node, no browser deps):
  - `discogs.js` — parse a Discogs release page into release data
  - `youtube.js` — parse a YouTube playlist page (`ytInitialData`) into videos
  - `match.js` — fuzzy-match Discogs tracks to playlist videos
  - `fill.js` — browser-only: fill the zig-zag form (stable `data-testid` selectors)
  - `ui.js` — browser-only: floating panel + preview/match review table
  - `main.js` — browser-only: wiring (`GM_xmlhttpRequest` fetching)
- `test/` — `node --test` suites + fixtures (public data only, no credentials)
- `build.mjs` — concatenates modules into `dist/zigzag-fill.user.js`
- `dist/` — built userscript (install this URL in Tampermonkey). Committed so it
  has a stable raw URL for auto-updates.

## Dev

```sh
npm test     # run parser/matcher unit tests
npm run build  # rebuild dist/zigzag-fill.user.js
```

## Rules (from zig-zag mods/devs)

- Track titles: prefer the **Discogs** name when it differs from YouTube.
- Country: the **artist's** country, not the label's.
- Strip YouTube tracking params, keep `watch?v=ID`.
- Genres: order slightly affects map weight; pick closest + suggest the rest in notes.
- The script **never** clicks "Submit for review" or "Save draft". Draft creation is
  the app's own autosave when the form opens; filling fields is all we do.
