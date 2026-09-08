# zigzag filler

Tampermonkey userscript that fills the **zig-zag.fm** "Add a release" form
from a **Discogs release link** + a **YouTube playlist link**.
It fills fields only — it never submits anything.

## Install

1. Install [Tampermonkey](https://www.tampermonkey.net/) in your browser.
2. Open
   [`dist/zigzag-fill.user.js`](https://raw.githubusercontent.com/rich0664/zigzag_tools/master/dist/zigzag-fill.user.js)
   — Tampermonkey will offer to install it (with auto-updates).

## Use

Open **Add a release** on zig-zag.fm, paste both links into the panel,
**Fetch & preview**, review the track matches, then **Fill form**.
