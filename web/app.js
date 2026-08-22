/* Azure Dreams Shop Patcher -- interface wiring. */
import { patchImage, cueFor, PatchError, SECTOR } from './patcher.js';

const $ = (id) => document.getElementById(id);

const fileInput = $('file');
const drop = $('drop');
const fileInfo = $('fileinfo');
const fileName = $('fileName');
const fileMeta = $('fileMeta');
const optBarry = $('optBarry');
const optMonster = $('optMonster');
const go = $('go');
const statusEl = $('status');
const logEl = $('log');
const downloads = $('downloads');
const dlBin = $('dlBin');
const dlCue = $('dlCue');
const band = $('band');
const ticks = $('ticks');
const manifest = $('manifest');
const totalSectorsEl = $('totalSectors');
const touchedCountEl = $('touchedCount');
const bandNote = $('bandNote');
const bandEnd = $('bandEnd');

// Every sector this patch can rewrite, and what lives in each. Known up front,
// so the map says something real before a file is even chosen.
const SECTOR_ROLES = [
  { lba: 1883,  patch: 'barry',   role: 'egg prices' },
  { lba: 6147,  patch: 'monster', role: 'egg list builder' },
  { lba: 6149,  patch: 'monster', role: 'give-item hook' },
  { lba: 6158,  patch: 'monster', role: 'buy flow script' },
  { lba: 6195,  patch: 'barry',   role: 'Barry\u2019s stock builder' },
  { lba: 14930, patch: 'barry',   role: 'egg prices, second copy' },
];
const NOMINAL_TOTAL = 126946;

let chosen = null;          // { file, buf }
let objectUrls = [];
let total = NOMINAL_TOTAL;

const nf = new Intl.NumberFormat('en-US');

function selectedPatches() {
  return { barry: optBarry.checked, monster: optMonster.checked };
}

/** Redraw band and manifest for the sectors the current options will touch. */
function renderPlan() {
  const on = selectedPatches();
  const active = SECTOR_ROLES.filter((s) => on[s.patch]);

  ticks.replaceChildren();
  manifest.replaceChildren();

  for (const s of SECTOR_ROLES) {
    const isOn = on[s.patch];

    const tick = document.createElement('span');
    tick.className = isOn ? 'tick' : 'tick off';
    tick.style.left = `${(s.lba / total) * 100}%`;
    ticks.append(tick);

    const li = document.createElement('li');
    li.dataset.lba = String(s.lba);
    if (!isOn) li.className = 'off';
    const lba = document.createElement('span');
    lba.className = 'm-lba';
    lba.textContent = nf.format(s.lba);
    const role = document.createElement('span');
    role.className = 'm-role';
    role.textContent = s.role;
    li.append(lba, role);
    manifest.append(li);
  }

  touchedCountEl.textContent = String(active.length);
  totalSectorsEl.textContent = nf.format(total);
  bandEnd.textContent = nf.format(total);
}

/** Light the sectors that were actually rewritten, in disc order. */
function igniteSectors(touched) {
  const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;
  const tickEls = [...ticks.querySelectorAll('.tick:not(.off)')];
  const cells = touched
    .map((lba) => manifest.querySelector(`li[data-lba="${lba}"]`))
    .filter(Boolean);

  cells.forEach((el, i) => setTimeout(() => el.classList.add('lit'), reduce ? 0 : 90 * i));
  tickEls.forEach((el, i) => setTimeout(() => el.classList.add('lit'), reduce ? 0 : 90 * i));
  setTimeout(() => band.classList.remove('working'), reduce ? 0 : 1000);
}

function setStatus(msg, kind = '') {
  statusEl.textContent = msg;
  statusEl.className = `status${kind ? ' ' + kind : ''}`;
}

function revokeUrls() {
  objectUrls.forEach(URL.revokeObjectURL);
  objectUrls = [];
}

function resetOutput() {
  revokeUrls();
  downloads.hidden = true;
  logEl.hidden = true;
  logEl.textContent = '';
  bandNote.textContent = 'everything else stays byte-for-byte original';
  renderPlan();
}

function syncButton() {
  go.disabled = !chosen || (!optBarry.checked && !optMonster.checked);
}

async function accept(file) {
  if (!file) return;
  resetOutput();
  setStatus(`reading ${file.name}\u2026`);
  chosen = null;
  syncButton();

  let buf;
  try {
    buf = new Uint8Array(await file.arrayBuffer());
  } catch (err) {
    setStatus(`Could not read that file: ${err.message}`, 'err');
    return;
  }

  if (buf.length % SECTOR !== 0) {
    fileInfo.hidden = true;
    setStatus(
      `${nf.format(buf.length)} bytes is not a whole number of 2352-byte ` +
      'sectors. This needs a MODE2/2352 .bin, not a 2048-byte-per-sector rip.',
      'err'
    );
    return;
  }

  total = buf.length / SECTOR;
  chosen = { file, buf };

  fileName.textContent = file.name;
  fileMeta.textContent =
    `${nf.format(total)} sectors \u00b7 ${(buf.length / 1048576).toFixed(1)} MB`;
  fileInfo.hidden = false;

  renderPlan();
  setStatus('ready to patch');
  syncButton();
}

/* -------------------------------------------------------------- file input */

fileInput.addEventListener('change', () => accept(fileInput.files[0]));

['dragenter', 'dragover'].forEach((ev) =>
  drop.addEventListener(ev, (e) => {
    e.preventDefault();
    drop.classList.add('hot');
  })
);

['dragleave', 'drop'].forEach((ev) =>
  drop.addEventListener(ev, (e) => {
    e.preventDefault();
    drop.classList.remove('hot');
  })
);

drop.addEventListener('drop', (e) => {
  const f = e.dataTransfer?.files?.[0];
  if (f) accept(f);
});

optBarry.addEventListener('change', () => { resetOutput(); syncButton(); });
optMonster.addEventListener('change', () => { resetOutput(); syncButton(); });

/* ------------------------------------------------------------------ patch */

go.addEventListener('click', async () => {
  if (!chosen) return;
  resetOutput();
  go.disabled = true;
  setStatus('patching\u2026');
  band.classList.add('working');

  // Work on a copy so the same file can be re-patched with other options.
  const out = chosen.buf.slice();
  const lines = [];

  await new Promise((r) => requestAnimationFrame(() => setTimeout(r, 0)));

  let result;
  try {
    result = patchImage(
      out,
      { barry: optBarry.checked, monsterShop: optMonster.checked },
      (l) => lines.push(l)
    );
  } catch (err) {
    band.classList.remove('working');
    setStatus(err instanceof PatchError ? err.message : `Unexpected error: ${err.message}`, 'err');
    if (lines.length) {
      logEl.textContent = lines.join('\n');
      logEl.hidden = false;
    }
    go.disabled = false;
    return;
  }

  logEl.textContent = lines.join('\n');
  logEl.hidden = false;

  touchedCountEl.textContent = String(result.touched.length);
  bandNote.textContent = 'error-correction recomputed on each';
  igniteSectors(result.touched);

  const base = chosen.file.name.replace(/\.bin$/i, '');
  const binName = `${base} [Shops].bin`;
  const cueName = `${base} [Shops].cue`;

  const binUrl = URL.createObjectURL(new Blob([out], { type: 'application/octet-stream' }));
  const cueUrl = URL.createObjectURL(new Blob([cueFor(binName)], { type: 'text/plain' }));
  objectUrls.push(binUrl, cueUrl);

  dlBin.href = binUrl;
  dlBin.download = binName;
  dlCue.href = cueUrl;
  dlCue.download = cueName;
  downloads.hidden = false;

  setStatus(`patched \u2014 ${result.touched.length} sectors rewritten, all valid`, 'ok');
  go.disabled = false;
});

/* ------------------------------------------------------------------- boot */

renderPlan();
syncButton();
window.addEventListener('pagehide', revokeUrls);
