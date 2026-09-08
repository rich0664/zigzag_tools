// Browser-only: fill the zig-zag "Add a release" form.
// Selectors use stable data-testid / placeholder / aria-label hooks only.
// React-generated ids (_r_41_…) are NEVER used.
// This module NEVER clicks Submit for review / Save draft / Discard.

function zzRoot() {
  return document.querySelector('[data-testid="contribute-track-form"]');
}

function setNativeValue(el, value) {
  const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, value);
  el.dispatchEvent(new Event('input', { bubbles: true }));
  el.dispatchEvent(new Event('change', { bubbles: true }));
}

function setReactText(el, value) {
  el.focus();
  setNativeValue(el, value == null ? '' : String(value));
  el.dispatchEvent(new FocusEvent('blur', { bubbles: true }));
  el.blur();
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function sectionFor(testid) {
  return zzRoot()?.querySelector(`[data-testid="${testid}"]`) || null;
}

function inputByPlaceholder(section, placeholder) {
  return section?.querySelector(`input[placeholder="${placeholder}"]`) || null;
}

// Search-style inputs (country/format/genre): type, wait for the chip button
// with exact text, click it. Returns true when a chip was clicked.
async function pickChip(sectionTestid, ariaLabel, wanted, log) {
  const section = sectionFor(sectionTestid);
  if (!section) return false;
  const input = section.querySelector(`input[aria-label="${ariaLabel}"]`);
  if (!input) {
    log(`chip input missing: ${ariaLabel}`);
    return false;
  }
  setReactText(input, wanted);
  await sleep(900);
  const chips = [...section.querySelectorAll('button')].filter(
    (b) => b.textContent.trim().toLowerCase() === wanted.trim().toLowerCase(),
  );
  if (!chips.length) {
    log(`no chip for "${wanted}" (${ariaLabel}) — left for manual pick`);
    return false;
  }
  chips[0].click();
  await sleep(400);
  return true;
}

async function fillRelease(release, log) {
  const section = sectionFor('contribute-release-section');
  if (!section) {
    log('release section not found');
    return false;
  }
  const title = inputByPlaceholder(section, 'Release title');
  if (title && release.title) {
    setReactText(title, release.title);
    await sleep(250);
  }
  const year = inputByPlaceholder(section, 'Missing year');
  if (year && release.year) {
    setReactText(year, release.year);
    await sleep(250);
  }
  if (release.country) await pickChip('contribute-release-section', 'Search countries', release.country, log);
  if (release.format) await pickChip('contribute-release-section', 'Search formats', release.format, log);
  if (release.genres && release.genres.length) {
    // Order slightly affects map weight: fill in given order.
    for (const g of release.genres) await pickChip('contribute-release-section', 'Search genres', g, log);
  }
  return true;
}

async function fillCover(blob, filename, log) {
  const input = document.querySelector('input[data-testid="image-upload-input"]');
  if (!input) {
    log('cover file input not found');
    return false;
  }
  if (!blob) {
    log('no cover blob — pick manually via Choose an image');
    return false;
  }
  const file = new File([blob], filename || 'cover.jpg', { type: blob.type || 'image/jpeg' });
  const dt = new DataTransfer();
  dt.items.add(file);
  input.files = dt.files;
  input.dispatchEvent(new Event('change', { bubbles: true }));
  await sleep(500);
  return true;
}

function trackRows() {
  return [...(zzRoot()?.querySelectorAll('[data-testid^="track-row-"]') || [])];
}

async function ensureTrackRows(n, log) {
  const add = zzRoot()?.querySelector('[data-testid="add-track"]');
  if (!add) {
    log('add-track button not found');
    return false;
  }
  for (let guard = 0; guard < 60 && trackRows().length < n; guard++) {
    add.click();
    await sleep(450);
  }
  if (trackRows().length < n) {
    log(`only ${trackRows().length}/${n} track rows created`);
    return false;
  }
  return true;
}

async function fillTracks(tracks, log) {
  if (!tracks.length) {
    log('no tracks parsed — nothing to fill (see debug dump)');
    return true;
  }
  log(`track rows before: ${trackRows().length}, need ${tracks.length}`);
  if (!(await ensureTrackRows(tracks.length, log))) return false;
  const rows = trackRows();
  for (let i = 0; i < tracks.length; i++) {
    const row = rows[i];
    const title = row.querySelector('input[placeholder="Track title"]');
    const link = [...row.querySelectorAll('input')].find((el) =>
      (el.placeholder || '').startsWith('https://www.youtube.com/watch?v='),
    );
    // Discogs title wins on mismatch (per zig-zag mods).
    if (title && tracks[i].title) setReactText(title, tracks[i].title);
    if (link && tracks[i].url) setReactText(link, tracks[i].url);
    await sleep(200);
  }
  log(`filled ${tracks.length} track row(s)`);
  return true;
}

async function fillSupport(urls, log) {
  const section = sectionFor('contribute-support-section');
  const input = section?.querySelector('input[placeholder="https://…"]');
  if (!input) {
    log('support link input not found');
    return false;
  }
  if (urls && urls.length) {
    setReactText(input, urls[0]);
    await sleep(250);
    if (urls.length > 1) log('extra support links ignored (form takes one): ' + urls.slice(1).join(', '));
  }
  return true;
}

// Combobox inputs (artists/labels): type the name and try to pick an exact
// dropdown option; otherwise leave the typed text for manual pick.
async function pickCombobox(sectionTestid, placeholder, wanted, log) {
  const section = sectionFor(sectionTestid);
  const input = section && inputByPlaceholder(section, placeholder);
  if (!input) {
    log(`combobox missing: ${placeholder}`);
    return false;
  }
  setReactText(input, wanted);
  await sleep(1000);
  const opts = [...(section.querySelectorAll('[role="option"]') || [])];
  const hit = opts.find((o) => o.textContent.trim().toLowerCase() === wanted.trim().toLowerCase())
    || opts.find((o) => o.textContent.trim().toLowerCase().includes(wanted.trim().toLowerCase()));
  if (hit) {
    hit.click();
    await sleep(400);
    return true;
  }
  log(`no dropdown hit for "${wanted}" — left typed for manual pick`);
  return false;
}

async function fillArtists(names, log) {
  const add = zzRoot()?.querySelector('[data-testid="add-artist"]');
  for (let i = 0; i < names.length; i++) {
    if (i > 0) {
      if (!add) {
        log('add-artist button missing');
        break;
      }
      add.click();
      await sleep(450);
    }
    await pickCombobox('contribute-artists-section', 'Search existing artists', names[i], log);
  }
  return true;
}

async function fillLabel(name, log) {
  if (!name) return true;
  return pickCombobox('contribute-label-section', 'Search existing labels', name, log);
}

async function fillNotes(text, log) {
  const ta = zzRoot()?.querySelector('textarea[placeholder^="Anything a moderator"]');
  if (!ta) {
    log('notes textarea not found');
    return false;
  }
  if (text) setReactText(ta, text);
  return true;
}

function remainingChecklist() {
  return [...(zzRoot()?.querySelectorAll('[data-testid="checklist-unmet"]') || [])].map((li) =>
    li.textContent.replace(/\s+/g, ' ').trim(),
  );
}

// plan: {release, coverBlob, coverName, tracks:[{title,url}], support:[], artists:[], label, notes}
// onLog(msg) receives progress lines. Returns {ok, remaining}.
async function fillAll(plan, onLog) {
  const log = onLog || (() => {});
  if (!zzRoot()) {
    log('add-release form not open (open it via Add a release first)');
    return { ok: false, remaining: [] };
  }
  await fillRelease(plan.release || {}, log);
  await fillCover(plan.coverBlob, plan.coverName, log);
  await fillTracks(plan.tracks || [], log);
  await fillSupport(plan.support || [], log);
  await fillArtists(plan.artists || [], log);
  await fillLabel(plan.label, log);
  if (plan.notes) await fillNotes(plan.notes, log);
  await sleep(600);
  const remaining = remainingChecklist();
  log(remaining.length ? `still needed (${remaining.length}): ${remaining.join(' | ')}` : 'checklist clear — review and submit manually');
  return { ok: true, remaining };
}

export { fillAll, remainingChecklist, zzRoot };
