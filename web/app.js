/* Azure Dreams Shop Patcher -- interface wiring. */
import { patchImage, planSectors, cueFor, PatchError, SECTOR, MAX_STOCK } from './patcher.js';
import { ITEMS, CATEGORIES } from './items.js';
import {
  CAT_EGG, PRICE_MAX, key, itemByKey, categoryByCat,
  BARRY_DEFAULT_STOCK, defaultPrice, defaultQuality, quotedPrice, defaultConfig,
} from './config.js';

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
const rows = $('rows');
const chips = $('chips');
const search = $('search');
const visibleCount = $('visibleCount');

const NOMINAL_TOTAL = 126946;
const STORAGE_KEY = 'azure-dreams-shop-patcher.config';
const TOWER_EGG_MAX = 0x18; // eggs past U-BOAT belong to monsters the tower never drops

let chosen = null;          // { file, buf }
let objectUrls = [];
let total = NOMINAL_TOTAL;

const nf = new Intl.NumberFormat('en-US');

/* ---------------------------------------------------------------- state */

// Working state is per item: which shops list it, its prices, its quality.
// The config the patcher takes is derived from this on demand.
const state = new Map();     // key -> { barry, monster, buy, sell, quality }
const filter = { text: '', cats: new Set() };

function freshEntry(item) {
  const p = defaultPrice(item);
  return { barry: false, monster: false, buy: p.buy, sell: p.sell, quality: defaultQuality(item) };
}

function loadState(config) {
  state.clear();
  for (const it of ITEMS) state.set(key(it.cat, it.id), freshEntry(it));
  const shop = (list, flag) => {
    for (const e of list || []) {
      const s = state.get(key(e.cat, e.id));
      if (!s) continue;
      s[flag] = true;
      if (Number.isInteger(e.quality)) s.quality = e.quality;
    }
  };
  shop(config.barry?.stock, 'barry');
  shop(config.monsterShop?.stock, 'monster');
  for (const [k, p] of Object.entries(config.prices || {})) {
    const s = state.get(k);
    if (!s) continue;
    if (Number.isInteger(p.buy)) s.buy = p.buy;
    if (Number.isInteger(p.sell)) s.sell = p.sell;
  }
  optBarry.checked = config.barry?.enabled !== false;
  optMonster.checked = config.monsterShop?.enabled !== false;
}

/** Only stocked items, and items whose prices were changed, reach the disc. */
function currentConfig() {
  const barry = [];
  const monster = [];
  const prices = {};
  for (const it of ITEMS) {
    const k = key(it.cat, it.id);
    const s = state.get(k);
    const entry = { cat: it.cat, id: it.id, quality: s.quality };
    if (s.barry) barry.push(entry);
    if (s.monster) monster.push(entry);
    const d = defaultPrice(it);
    const edited = s.buy !== d.buy || s.sell !== d.sell;
    if (s.barry || s.monster || edited) prices[k] = { buy: s.buy, sell: s.sell };
  }
  return {
    barry: { enabled: optBarry.checked, stock: barry },
    monsterShop: { enabled: optMonster.checked, stock: monster },
    prices,
  };
}

function persist() {
  try { localStorage.setItem(STORAGE_KEY, JSON.stringify(currentConfig())); } catch { /* private mode */ }
}

function restore() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) return JSON.parse(raw);
  } catch { /* fall through */ }
  return null;
}

const counts = () => {
  let b = 0, m = 0;
  for (const s of state.values()) { if (s.barry) b++; if (s.monster) m++; }
  return { b, m };
};

/* ------------------------------------------------------------ sector map */

function renderPlan() {
  const cfg = currentConfig();
  const plan = planSectors(cfg, CATEGORIES);

  ticks.replaceChildren();
  manifest.replaceChildren();

  for (const s of plan) {
    const tick = document.createElement('span');
    tick.className = 'tick';
    tick.style.left = `${(s.lba / total) * 100}%`;
    ticks.append(tick);

    const li = document.createElement('li');
    li.dataset.lba = String(s.lba);
    const lba = document.createElement('span');
    lba.className = 'm-lba';
    lba.textContent = nf.format(s.lba);
    const role = document.createElement('span');
    role.className = 'm-role';
    role.textContent = s.role;
    li.append(lba, role);
    manifest.append(li);
  }

  touchedCountEl.textContent = String(plan.length);
  totalSectorsEl.textContent = nf.format(total);
  bandEnd.textContent = nf.format(total);
}

/** Light the sectors that were actually rewritten, in disc order. */
function igniteSectors(touched) {
  const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;
  const tickEls = [...ticks.querySelectorAll('.tick')];
  const cells = touched
    .map((lba) => manifest.querySelector(`li[data-lba="${lba}"]`))
    .filter(Boolean);

  cells.forEach((el, i) => setTimeout(() => el.classList.add('lit'), reduce ? 0 : 90 * i));
  tickEls.forEach((el, i) => setTimeout(() => el.classList.add('lit'), reduce ? 0 : 90 * i));
  setTimeout(() => band.classList.remove('working'), reduce ? 0 : 1000);
}

/* --------------------------------------------------------------- catalog */

function visibleItems() {
  const q = filter.text.trim().toLowerCase();
  return ITEMS.filter((it) =>
    (!filter.cats.size || filter.cats.has(it.cat)) &&
    (!q || it.name.toLowerCase().includes(q) || categoryByCat.get(it.cat).label.toLowerCase().includes(q)));
}

function renderChips() {
  chips.replaceChildren();
  for (const c of CATEGORIES) {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'chip' + (filter.cats.has(c.cat) ? ' on' : '');
    b.dataset.cat = String(c.cat);
    b.setAttribute('aria-pressed', filter.cats.has(c.cat) ? 'true' : 'false');
    const n = ITEMS.filter((it) => it.cat === c.cat && state.get(key(it.cat, it.id)).barry).length;
    b.textContent = c.label;
    if (n) {
      const s = document.createElement('span');
      s.className = 'chip-n';
      s.textContent = String(n);
      b.append(s);
    }
    chips.append(b);
  }
}

function numberInput(value, { min, max, cls, title }) {
  const inp = document.createElement('input');
  inp.type = 'number';
  inp.inputMode = 'numeric';
  inp.className = cls;
  inp.min = String(min);
  inp.max = String(max);
  inp.step = '1';
  inp.value = String(value);
  if (title) inp.title = title;
  return inp;
}

function rowNotes(it, s) {
  const notes = [];
  const c = categoryByCat.get(it.cat);
  if ((s.barry || s.monster) && s.buy < s.sell) notes.push({ kind: 'warn', text: 'sells for more than it costs' });
  if (c.scaled && s.quality && (s.barry || s.monster)) {
    notes.push({ kind: 'info', text: `quotes ${nf.format(quotedPrice(it, s.buy, s.quality))}G` });
  }
  if (it.sell * 2 > PRICE_MAX && s.buy === PRICE_MAX) notes.push({ kind: 'info', text: 'capped at 65,535' });
  return notes;
}

function renderRows() {
  const items = visibleItems();
  visibleCount.textContent = String(items.length);
  const frag = document.createDocumentFragment();

  for (const it of items) {
    const k = key(it.cat, it.id);
    const s = state.get(k);
    const c = categoryByCat.get(it.cat);
    const tr = document.createElement('tr');
    tr.dataset.key = k;
    if (s.barry || s.monster) tr.className = 'stocked';

    const cell = (cls) => { const td = document.createElement('td'); td.className = cls; tr.append(td); return td; };

    const tdB = cell('c-shop');
    const cbB = document.createElement('input');
    cbB.type = 'checkbox'; cbB.checked = s.barry; cbB.dataset.flag = 'barry';
    cbB.setAttribute('aria-label', `Barry stocks ${it.name}`);
    tdB.append(cbB);

    const tdM = cell('c-shop');
    if (c.monster) {
      const cbM = document.createElement('input');
      cbM.type = 'checkbox'; cbM.checked = s.monster; cbM.dataset.flag = 'monster';
      cbM.setAttribute('aria-label', `Monster shop stocks ${it.name}`);
      tdM.append(cbM);
    } else {
      tdM.textContent = '–';
      tdM.classList.add('na');
    }

    cell('c-name').textContent = it.name;
    const tdC = cell('c-cat');
    const tag = document.createElement('span');
    tag.className = 'tag';
    tag.textContent = c.label;
    tdC.append(tag);

    const tdS = cell('c-price');
    tdS.append(numberInput(s.sell, { min: 0, max: PRICE_MAX, cls: 'price', title: `vanilla ${it.sell}G` }));
    tdS.lastChild.dataset.field = 'sell';

    const tdBuy = cell('c-price');
    tdBuy.append(numberInput(s.buy, { min: 0, max: PRICE_MAX, cls: 'price', title: `vanilla ${it.buy}G` }));
    tdBuy.lastChild.dataset.field = 'buy';

    const tdQ = cell('c-q');
    if (c.scaled) {
      const q = numberInput(s.quality, { min: -9, max: 99, cls: 'quality',
        title: it.cat === 0x04 ? 'charges' : 'plus value' });
      q.dataset.field = 'quality';
      tdQ.append(q);
    } else {
      tdQ.textContent = '–';
      tdQ.classList.add('na');
    }

    const tdN = cell('c-note');
    for (const n of rowNotes(it, s)) {
      const sp = document.createElement('span');
      sp.className = `note ${n.kind}`;
      sp.textContent = n.text;
      tdN.append(sp);
    }
    frag.append(tr);
  }
  rows.replaceChildren(frag);
}

function renderCounts() {
  const { b, m } = counts();
  $('barryCount').textContent = String(b);
  $('monsterCount').textContent = String(m);
  $('barryMax').textContent = String(MAX_STOCK);
  $('monsterMax').textContent = String(MAX_STOCK);
  $('shopBarry').classList.toggle('over', b > MAX_STOCK);
  $('shopMonster').classList.toggle('over', m > MAX_STOCK);
  $('shopBarry').classList.toggle('off', !optBarry.checked);
  $('shopMonster').classList.toggle('off', !optMonster.checked);
}

function renderAll() {
  renderChips();
  renderRows();
  renderCounts();
  renderPlan();
}

/** Anything that changes the config: re-render, forget the last output. */
function changed({ rows: redraw = true } = {}) {
  resetOutput();
  persist();
  if (redraw) renderRows();
  renderChips();
  renderCounts();
  renderPlan();
  syncButton();
}

/* ------------------------------------------------------ catalog events */

rows.addEventListener('change', (e) => {
  const tr = e.target.closest('tr');
  if (!tr) return;
  const s = state.get(tr.dataset.key);
  const it = itemByKey.get(tr.dataset.key);
  if (e.target.dataset.flag) {
    s[e.target.dataset.flag] = e.target.checked;
    changed();
    return;
  }
  const field = e.target.dataset.field;
  if (!field) return;
  let v = Math.round(Number(e.target.value));
  if (!Number.isFinite(v)) v = field === 'quality' ? defaultQuality(it) : defaultPrice(it)[field];
  if (field === 'quality') v = Math.max(-128, Math.min(127, v));
  else v = Math.max(0, Math.min(PRICE_MAX, v));
  s[field] = v;
  e.target.value = String(v);
  changed({ rows: false });
  // Notes for this row only; a full redraw would steal focus mid-edit.
  const tdN = tr.querySelector('.c-note');
  tdN.replaceChildren(...rowNotes(it, s).map((n) => {
    const sp = document.createElement('span');
    sp.className = `note ${n.kind}`;
    sp.textContent = n.text;
    return sp;
  }));
  tr.classList.toggle('stocked', s.barry || s.monster);
});

chips.addEventListener('click', (e) => {
  const b = e.target.closest('.chip');
  if (!b) return;
  const cat = Number(b.dataset.cat);
  if (filter.cats.has(cat)) filter.cats.delete(cat); else filter.cats.add(cat);
  renderChips();
  renderRows();
});

search.addEventListener('input', () => {
  filter.text = search.value;
  renderRows();
});

$('bulk').addEventListener('click', (e) => {
  const b = e.target.closest('[data-bulk]');
  if (!b) return;
  const items = visibleItems();
  switch (b.dataset.bulk) {
    case 'barry-on': items.forEach((it) => { state.get(key(it.cat, it.id)).barry = true; }); break;
    case 'barry-off': items.forEach((it) => { state.get(key(it.cat, it.id)).barry = false; }); break;
    case 'monster-on': items.forEach((it) => { if (it.cat === CAT_EGG) state.get(key(it.cat, it.id)).monster = true; }); break;
    case 'monster-off': items.forEach((it) => { state.get(key(it.cat, it.id)).monster = false; }); break;
    case 'prices-double': items.forEach((it) => {
      const s = state.get(key(it.cat, it.id));
      s.buy = s.sell === 0 ? s.buy : Math.min(s.sell * 2, PRICE_MAX);
    }); break;
    case 'prices-reset': items.forEach((it) => {
      const s = state.get(key(it.cat, it.id));
      Object.assign(s, defaultPrice(it));
      s.quality = defaultQuality(it);
    }); break;
    default: return;
  }
  changed();
});

document.querySelector('.shops').addEventListener('click', (e) => {
  const b = e.target.closest('[data-preset]');
  if (!b) return;
  e.preventDefault();
  const set = (flag, pred) => { for (const it of ITEMS) state.get(key(it.cat, it.id))[flag] = pred(it); };
  const def = new Set(BARRY_DEFAULT_STOCK.map(([c, i]) => key(c, i)));
  switch (b.dataset.preset) {
    case 'barry-vanilla': set('barry', (it) => def.has(key(it.cat, it.id))); break;
    case 'barry-equipment': set('barry', (it) => [0x0f, 0x10, 0x11].includes(it.cat)); break;
    case 'barry-none': set('barry', () => false); break;
    case 'monster-all': set('monster', (it) => it.cat === CAT_EGG); break;
    case 'monster-tower': set('monster', (it) => it.cat === CAT_EGG && it.id <= TOWER_EGG_MAX); break;
    case 'monster-none': set('monster', () => false); break;
    default: return;
  }
  changed();
});

optBarry.addEventListener('change', () => changed({ rows: false }));
optMonster.addEventListener('change', () => changed({ rows: false }));

/* ------------------------------------------------------------ config io */

$('exportCfg').addEventListener('click', () => {
  const blob = new Blob([JSON.stringify(currentConfig(), null, 1)], { type: 'application/json' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'azure-dreams-shops.json';
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
});

$('importCfg').addEventListener('change', async (e) => {
  const f = e.target.files[0];
  if (!f) return;
  try {
    loadState(JSON.parse(await f.text()));
    setStatus(`loaded ${f.name}`);
  } catch (err) {
    setStatus(`Could not read that config: ${err.message}`, 'err');
  }
  e.target.value = '';
  changed();
});

$('resetCfg').addEventListener('click', () => {
  loadState(defaultConfig());
  filter.text = '';
  filter.cats.clear();
  search.value = '';
  changed();
});

/* ------------------------------------------------------------ file input */

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
}

function problems() {
  const { b, m } = counts();
  if (!optBarry.checked && !optMonster.checked) return 'pick at least one shop';
  if (optBarry.checked && b > MAX_STOCK) return `Barry can list at most ${MAX_STOCK} items`;
  if (optMonster.checked && m > MAX_STOCK) return `the monster shop can list at most ${MAX_STOCK} items`;
  return null;
}

function syncButton() {
  const p = problems();
  go.disabled = !chosen || !!p;
  go.title = p || '';
}

async function accept(file) {
  if (!file) return;
  resetOutput();
  setStatus(`reading ${file.name}…`);
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
    `${nf.format(total)} sectors · ${(buf.length / 1048576).toFixed(1)} MB`;
  fileInfo.hidden = false;

  renderPlan();
  setStatus('ready to patch');
  syncButton();
}

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

/* ------------------------------------------------------------------ patch */

go.addEventListener('click', async () => {
  if (!chosen) return;
  resetOutput();
  renderPlan();
  go.disabled = true;
  setStatus('patching…');
  band.classList.add('working');

  // Work on a copy so the same file can be re-patched with other options.
  const out = chosen.buf.slice();
  const lines = [];
  const cfg = currentConfig();

  await new Promise((r) => requestAnimationFrame(() => setTimeout(r, 0)));

  let result;
  try {
    result = patchImage(out, cfg, (l) => lines.push(l));
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

  const warnings = lines.filter((l) => l.startsWith('warning')).length;
  setStatus(`patched — ${result.touched.length} sectors rewritten, all valid` +
            (warnings ? `, ${warnings} price warning${warnings > 1 ? 's' : ''} in the log` : ''), 'ok');
  go.disabled = false;
});

/* ------------------------------------------------------------------- boot */

loadState(restore() || defaultConfig());
renderAll();
syncButton();
window.addEventListener('pagehide', revokeUrls);
