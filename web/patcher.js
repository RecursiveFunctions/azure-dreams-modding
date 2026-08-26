/*
 * Azure Dreams (USA) shop patcher -- patch logic.
 *
 * A direct port of patch.py. Pure computation, no DOM, so it runs equally well
 * in a browser or under node (see ../tools/compare.mjs, which uses that to
 * prove both implementations emit identical bytes).
 *
 * Everything is driven by one config object, the same JSON the command-line
 * patcher accepts:
 *
 *   {
 *     barry:       { enabled: true, stock: [{ cat, id, quality }, ...] },
 *     monsterShop: { enabled: true, stock: [{ cat, id, quality }, ...] },
 *     prices:      { "<cat>:<id>": { buy, sell }, ... }
 *   }
 *
 * A shop's stock is written as a table into free space in slus_006.14, and the
 * routine that used to hardcode the shop's list becomes a loop that copies the
 * table. Prices are written into the game's own item records, and only for the
 * entries listed -- everything else stays byte-for-byte original.
 */

// ---------------------------------------------------------------------------
// Disc geometry.
//
// A MODE2/2352 sector is 0x930 bytes: 0x18 of sync+subheader, 0x800 of payload,
// then 0x118 of EDC/ECC. "Stream offset" below means an offset into the payload
// with that overhead removed -- the coordinate space the game's data is laid
// out in, and the one every address here is expressed in.
// ---------------------------------------------------------------------------
export const SECTOR = 0x930;
export const HDR = 0x18;
export const DATA = 0x800;

function streamToBin(s) {
  const lba = Math.floor(s / DATA);
  return { off: lba * SECTOR + HDR + (s % DATA), lba };
}

// ---------------------------------------------------------------------------
// EDC / ECC for Mode 2 Form 1
// ---------------------------------------------------------------------------
const eccF = new Uint8Array(256);
const eccB = new Uint8Array(256);
const edcL = new Uint32Array(256);
for (let i = 0; i < 256; i++) {
  const j = ((i << 1) ^ (i & 0x80 ? 0x11d : 0)) & 0xff;
  eccF[i] = j;
  eccB[i ^ j] = i;
  let e = i;
  for (let k = 0; k < 8; k++) e = ((e >>> 1) ^ (e & 1 ? 0xd8018001 : 0)) >>> 0;
  edcL[i] = e;
}

function edcBlock(buf, off, size) {
  let e = 0;
  for (let k = off; k < off + size; k++) {
    e = ((e >>> 8) ^ edcL[(e ^ buf[k]) & 0xff]) >>> 0;
  }
  return e >>> 0;
}

function eccBlock(sec, src, majorCount, minorCount, majorMult, minorInc, dest) {
  const size = majorCount * minorCount;
  for (let major = 0; major < majorCount; major++) {
    let index = (major >> 1) * majorMult + (major & 1);
    let a = 0;
    let b = 0;
    for (let n = 0; n < minorCount; n++) {
      const t = sec[src + index];
      index += minorInc;
      if (index >= size) index -= size;
      a ^= t;
      b ^= t;
      a = eccF[a];
    }
    a = eccB[eccF[a] ^ b];
    sec[dest + major] = a;
    sec[dest + major + majorCount] = a ^ b;
  }
}

function putU32(buf, off, val) {
  buf[off] = val & 0xff;
  buf[off + 1] = (val >>> 8) & 0xff;
  buf[off + 2] = (val >>> 16) & 0xff;
  buf[off + 3] = (val >>> 24) & 0xff;
}

function getU32(buf, off) {
  return (buf[off] | (buf[off + 1] << 8) | (buf[off + 2] << 16) |
          (buf[off + 3] << 24)) >>> 0;
}

/** Recompute EDC and ECC in place for one Mode 2 Form 1 sector. */
export function regenSector(sec) {
  sec.copyWithin(0x14, 0x10, 0x14);
  putU32(sec, 0x818, edcBlock(sec, 0x10, 0x808));
  const saved = sec.slice(12, 16);
  sec.fill(0, 12, 16);
  eccBlock(sec, 0xc, 86, 24, 2, 86, 0x81c);
  eccBlock(sec, 0xc, 52, 43, 86, 88, 0x8c8);
  sec.set(saved, 12);
}

// ---------------------------------------------------------------------------
// Image
// ---------------------------------------------------------------------------
class PatchError extends Error {}

class Image {
  constructor(buf) {
    this.buf = buf;
    this.touched = new Set();
  }

  read(streamOff, n) {
    const out = new Uint8Array(n);
    let i = 0;
    while (i < n) {
      const { off } = streamToBin(streamOff + i);
      const k = Math.min(DATA - ((streamOff + i) % DATA), n - i);
      out.set(this.buf.subarray(off, off + k), i);
      i += k;
    }
    return out;
  }

  write(streamOff, data) {
    let i = 0;
    while (i < data.length) {
      const { off, lba } = streamToBin(streamOff + i);
      const k = Math.min(DATA - ((streamOff + i) % DATA), data.length - i);
      this.buf.set(data.subarray(i, i + k), off);
      this.touched.add(lba);
      i += k;
    }
  }

  readU16(streamOff) {
    const b = this.read(streamOff, 2);
    return b[0] | (b[1] << 8);
  }

  writeU16(streamOff, val) {
    this.write(streamOff, Uint8Array.of(val & 0xff, (val >>> 8) & 0xff));
  }

  readU32(streamOff) {
    const b = this.read(streamOff, 4);
    return (b[0] | (b[1] << 8) | (b[2] << 16) | (b[3] << 24)) >>> 0;
  }

  writeU32(streamOff, val) {
    const b = new Uint8Array(4);
    putU32(b, 0, val >>> 0);
    this.write(streamOff, b);
  }

  /** Write a word, tolerating the case where it is already applied. */
  patchU32(streamOff, expect, next, note) {
    const cur = this.readU32(streamOff);
    if (cur === (next >>> 0)) return false;
    if (cur !== (expect >>> 0)) {
      const { lba } = streamToBin(streamOff);
      throw new PatchError(
        `Unexpected data in sector ${lba}: found ${hex32(cur)}, ` +
        `expected ${hex32(expect)}. ${note || ''}`.trim()
      );
    }
    this.writeU32(streamOff, next);
    return true;
  }

  finalize() {
    for (const lba of [...this.touched].sort((a, b) => a - b)) {
      const sec = this.buf.subarray(lba * SECTOR, (lba + 1) * SECTOR);
      regenSector(sec);
    }
  }

  checkSectors() {
    const bad = [];
    for (const lba of [...this.touched].sort((a, b) => a - b)) {
      const orig = this.buf.slice(lba * SECTOR, (lba + 1) * SECTOR);
      const chk = orig.slice();
      regenSector(chk);
      for (let i = 0; i < chk.length; i++) {
        if (chk[i] !== orig[i]) { bad.push(lba); break; }
      }
    }
    return bad;
  }
}

function hex32(v) {
  return (v >>> 0).toString(16).padStart(8, '0');
}

function hexOff(v) {
  return '0x' + v.toString(16).padStart(8, '0');
}

function wordsToBytes(words) {
  const out = new Uint8Array(words.length * 4);
  words.forEach((w, i) => putU32(out, i * 4, w));
  return out;
}

function bytesEqual(a, b) {
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return false;
  return true;
}

/** Recompute EDC/ECC on untouched sectors; they must come out identical. */
function selftest(buf) {
  for (const lba of [182, 1883, 5000, 6147, 6149, 6158, 6195, 14930]) {
    const base = lba * SECTOR;
    if (base + SECTOR > buf.length) continue;
    const orig = buf.slice(base, base + SECTOR);
    if (orig[0x12] & 0x20) continue;
    const sec = orig.slice();
    regenSector(sec);
    for (let i = 0; i < sec.length; i++) {
      if (sec[i] !== orig[i]) {
        throw new PatchError(
          `Error-correction self-test failed on sector ${lba}. ` +
          'This does not look like a MODE2/2352 image.'
        );
      }
    }
  }
}

// ---------------------------------------------------------------------------
// MIPS
// ---------------------------------------------------------------------------
const ZERO = 0, V0 = 2, V1 = 3, A0 = 4, A1 = 5, RA = 31;
const addiu = (rt, rs, imm) => (((0x09 << 26) | (rs << 21) | (rt << 16) | (imm & 0xffff)) >>> 0);
const lw = (rt, base, off) => (((0x23 << 26) | (base << 21) | (rt << 16) | (off & 0xffff)) >>> 0);
const sw = (rt, base, off) => (((0x2b << 26) | (base << 21) | (rt << 16) | (off & 0xffff)) >>> 0);
const lui = (rt, imm) => (((0x0f << 26) | (rt << 16) | (imm & 0xffff)) >>> 0);
const bne = (rs, rt, off) => (((0x05 << 26) | (rs << 21) | (rt << 16) | (off & 0xffff)) >>> 0);
const move = (rd, rs) => (((rs << 21) | (rd << 11) | 0x21) >>> 0);
const jmp = (target) => ((0x08000000 | ((target >>> 2) & 0x03ffffff)) >>> 0);
const jr = (rs) => (((rs << 21) | 0x08) >>> 0);
const NOP = 0;

// ---------------------------------------------------------------------------
// Addresses
// ---------------------------------------------------------------------------

// slus_006.14 is loaded once at boot and stays resident, so a RAM address in
// it maps to the disc by a constant.
const SLUS_DELTA = 0x80020800;
const slusS = (ram) => ram - SLUS_DELTA;

// Category descriptor table in slus: 20-byte records, item array pointer at
// +0x0c. Item records are 20 bytes with u16 buy at +0x10 and sell at +0x12.
const CAT_TABLE = 0x80073414;
const CAT_STRIDE = 20;
const CAT_ARR_OFF = 0x0c;
const ITEM_RECORD = 20;
const BUY_PRICE_OFF = 0x10;
const SELL_PRICE_OFF = 0x12;

// The egg array is not in slus. It lives in main.bin and again in
// dungeon.bin, because town and tower each need item data; a price edit has
// to be mirrored or the value changes depending on where the player stands.
const CAT_EGG = 0x12;
const EGG_ARRAY_RAM = 0x8002ca68;
const EGG_ARRAYS = [0x003ada68, 0x01d29268];

// Where the stock tables go. A shop's list buffer is 0x100 bytes, so 64
// entries: the header row, the goods, and the terminator.
//
// Barry's table lives in slus_006.14, in the free block at 0x8007bcb0 whose
// lower part is reserved for newtown.py's extended gate stub (docs/FINDINGS.md,
// "Where a stub can live"). The monster shop's table lives in its own script
// chunk, in the apology text the buy flow freed, right after the buy flow.
// The other slus block, 0x800815b4, reads as zero on disc but is cleared at
// runtime and cannot hold anything.
const LIST_CAPACITY = 64;
export const MAX_STOCK = LIST_CAPACITY - 2;
const BARRY_TABLE = 0x8007bdf0;           // 256 bytes, to 0x8007bef0
const BARRY_TABLE_WORDS = LIST_CAPACITY;

// The "Pay" pseudo-row that draws the menu header. Both shops copy the same
// four bytes from their own chunk; it has to stay entry 0.
const HEADER_ROW = 0x00001601;

function stockWord(entry, i) {
  const { cat, id } = entry;
  const q = entry.quality | 0;
  if (!(id > 0 && id < 256) || !(cat > 0 && cat < 256)) {
    throw new PatchError(`Stock entry ${i}: id/category out of range.`);
  }
  if (q < -128 || q > 127) {
    throw new PatchError(`Stock entry ${i}: quality ${q} does not fit a byte.`);
  }
  return ((id) | (cat << 8) | ((q & 0xff) << 16)) >>> 0;
}

/** The table a shop's builder copies: header, entries, zero terminator. */
function buildStockTable(stock, maxStock = MAX_STOCK) {
  if (stock.length > maxStock) {
    throw new PatchError(`${stock.length} items; this shop can list at most ${maxStock}.`);
  }
  const seen = new Set();
  const words = [HEADER_ROW];
  stock.forEach((e, i) => {
    const key = `${e.cat}:${e.id}`;
    if (seen.has(key)) throw new PatchError(`Stock entry ${i} duplicates ${key}.`);
    seen.add(key);
    words.push(stockWord(e, i));
  });
  words.push(0);
  return words;
}

/**
 * The builder that replaces a shop's hardcoded list: copy words from TABLE
 * to the list buffer in $a0 until the zero terminator has been copied too.
 * Nine instructions, leaf, clobbers only $v0/$v1/$a0. The load in the loop
 * is two instructions ahead of its use, which the R3000's delay slot needs.
 */
function buildCopyLoop(table) {
  const hi = (table >>> 16) + ((table & 0x8000) ? 1 : 0);
  return [
    lui(V1, hi),
    addiu(V1, V1, table & 0xffff),
    lw(V0, V1, 0),             // loop:
    addiu(V1, V1, 4),
    sw(V0, A0, 0),
    bne(V0, ZERO, -4),         // back to loop (offset from the delay slot)
    addiu(A0, A0, 4),
    jr(RA),
    NOP,
  ];
}

// ---------------------------------------------------------------------------
// Prices
// ---------------------------------------------------------------------------

/** Stream offsets of every copy of an item's price fields. */
function priceRecords(img, cat, id) {
  const arr = img.readU32(slusS(CAT_TABLE + cat * CAT_STRIDE + CAT_ARR_OFF));
  if (cat === CAT_EGG) {
    if (arr !== EGG_ARRAY_RAM) {
      throw new PatchError(`Egg array pointer is ${hex32(arr)}, expected ${hex32(EGG_ARRAY_RAM)}.`);
    }
    return EGG_ARRAYS.map((a) => a + id * ITEM_RECORD);
  }
  if (arr < 0x8002d000 || arr >= 0x80081800) {
    throw new PatchError(`Category ${cat} item array pointer ${hex32(arr)} is not in slus.`);
  }
  return [slusS(arr) + id * ITEM_RECORD];
}

function parseKey(key) {
  const m = /^(\d+):(\d+)$/.exec(key);
  if (!m) throw new PatchError(`Bad price key "${key}"; expected "<cat>:<id>".`);
  return { cat: +m[1], id: +m[2] };
}

function checkPrice(v, what, key) {
  if (!Number.isInteger(v) || v < 0 || v > 0xffff) {
    throw new PatchError(`${what} price for ${key} must be 0..65535, got ${v}.`);
  }
}

function applyPrices(img, prices, stocked, log) {
  let changed = 0;
  let same = 0;
  const keys = Object.keys(prices || {}).sort((a, b) => {
    const ka = parseKey(a), kb = parseKey(b);
    return ka.cat - kb.cat || ka.id - kb.id;
  });
  for (const key of keys) {
    const { cat, id } = parseKey(key);
    const { buy, sell } = prices[key];
    checkPrice(buy, 'buy', key);
    checkPrice(sell, 'sell', key);
    if (stocked.has(key) && buy < sell) {
      log(`warning: ${key} buys for ${buy}G and sells for ${sell}G; that is a money loop`);
    }
    let wrote = false;
    for (const rec of priceRecords(img, cat, id)) {
      if ((rec + BUY_PRICE_OFF) % DATA > DATA - 4) {
        throw new PatchError(`Price fields for ${key} straddle a sector.`);
      }
      if (img.readU16(rec + BUY_PRICE_OFF) !== buy) { img.writeU16(rec + BUY_PRICE_OFF, buy); wrote = true; }
      if (img.readU16(rec + SELL_PRICE_OFF) !== sell) { img.writeU16(rec + SELL_PRICE_OFF, sell); wrote = true; }
    }
    if (wrote) changed++; else same++;
  }
  log(`item prices      ${changed} written, ${same} already as requested`);
}

// ---------------------------------------------------------------------------
// Patch A: Barry's shop
//
// Barry's stock comes from a hardcoded routine (RAM 0x800165c4, stream
// 0x00c19dc4) that writes a NUL-terminated list of 4-byte entries into a
// 0x100-byte buffer. Vanilla spells out each byte individually. It becomes the
// copy loop above, reading BARRY_TABLE.
// ---------------------------------------------------------------------------
const BARRY_BUILDER = 0x00c19dc4;
const BARRY_SLOTS = 22;

// Whatever occupies the builder before patching: vanilla, the earlier
// nine-item revision of this patch, or this one.
const BARRY_KNOWN_HEADS = [0x00801021, 0x24031601, 0x3c038008];

function applyBarry(img, stock, log) {
  const table = buildStockTable(stock);
  const code = buildCopyLoop(BARRY_TABLE);
  while (code.length < BARRY_SLOTS) code.push(NOP);

  const head = img.readU32(BARRY_BUILDER);
  if (!BARRY_KNOWN_HEADS.includes(head)) {
    throw new PatchError(
      `Unexpected data at Barry's stock builder (${hex32(head)}). ` +
      'Is this an unmodified Azure Dreams (USA) image?'
    );
  }
  img.write(BARRY_BUILDER, wordsToBytes(code));
  let b = streamToBin(BARRY_BUILDER);
  log(`stock builder    ${hexOff(b.off)}  sector ${b.lba}  copy loop, ${stock.length} items`);

  img.write(slusS(BARRY_TABLE), wordsToBytes(padTable(table, BARRY_TABLE_WORDS)));
  b = streamToBin(slusS(BARRY_TABLE));
  log(`stock table      ${hexOff(b.off)}  sector ${b.lba}  ${table.length * 4} of ${BARRY_TABLE_WORDS * 4} bytes`);
}

/** Zero the rest of the table's block so a shorter list leaves no stale tail. */
function padTable(words, size) {
  const out = words.slice();
  while (out.length < size) out.push(0);
  return out;
}

// ---------------------------------------------------------------------------
// Patch B: monster shop
//
// The monster shop overlay occupies RAM 0x8001xxxx but is stitched together
// from two different disc regions, so code and script need separate deltas.
//
// The overlay carries a complete but entirely unreferenced buy-side library,
// mirroring the sell side it sits next to:
//
//     purpose               sell         buy
//     price -> AMOUNT       0x8001679c   0x80016770   (same core, a1=0)
//     gold                  0x80016818   0x800167f0   (add / subtract)
//     affordability            --        0x800167c8
//     inventory transfer    0x80016850   0x800170e8   (same core, mode=0)
//     read AMOUNT           0x80016840   0x80016840
//
// So the buy flow below is a structural mirror of the known-good sell branch.
// ---------------------------------------------------------------------------
const CODE_DELTA = 0xbeb800;
const SCRIPT_DELTA = 0xbed2e8;
const codeS = (ram) => (ram & 0x1fffff) + CODE_DELTA;
const scriptS = (ram) => (ram & 0x1fffff) + SCRIPT_DELTA;

// The list builder at 0x800165c4 emits furniture; it becomes the copy loop
// reading MONSTER_TABLE. Vanilla begins with a stack frame, this patch with
// the loop's lui. (An earlier revision of this patch edited three words
// inside the vanilla function instead; its head is still vanilla's.)
const MONSTER_BUILDER = 0x800165c4;
const MONSTER_KNOWN_HEADS = [0x27bdffd8, 0x3c038008];

// The three words that earlier revision changed, restored to vanilla so an
// image patched twice comes out identical to one patched once.
const MONSTER_OLD_PATCH = [
  [0x80016600, 0x24130012, 0x24130018],
  [0x80016628, 0x00000000, 0x34420080],
  [0x80016634, 0x2a220019, 0x2a220021],
];

// 0x800170e8 is the buy-mode inventory wrapper, but it expects the list in $a0
// while script opcode 0x4c calls with no arguments. It has zero callers, so it
// is rewritten as a nullary tail call that supplies CTX+4 itself.
const GIVE_WRAPPER = 0x800170e8;
const XFER_CORE = 0x8001702c;
const CTX_PLUS_4_LO = 0x88d4; // 0x80020000 - 0x772c = 0x800188d4

const GIVE_WRAPPER_PATCHES = [
  [GIVE_WRAPPER + 0x0, 0x27bdffe8, lui(A0, 0x8002), 'lui   $a0, 0x8002'],
  [GIVE_WRAPPER + 0x4, 0xafbf0010, move(A1, ZERO), 'move  $a1, $zero'],
  [GIVE_WRAPPER + 0x8, 0x0c005c0b, jmp(XFER_CORE), 'j     transfer core'],
  [GIVE_WRAPPER + 0xc, 0x00002821, addiu(A0, A0, CTX_PLUS_4_LO), 'addiu $a0, $a0, -0x772c'],
];

const BUY_ARM = 0x8001a1b0;
const BUY_ARM_LIMIT = 0x8001a3bc; // the "just looking" branch starts here

// The buy flow is 276 bytes; the stock table takes the rest of the freed
// apology, word-aligned, which is 62 words: header, 60 goods, terminator.
const BUY_ARM_BYTES = 276;
const MONSTER_TABLE = BUY_ARM + BUY_ARM_BYTES;                    // 0x8001a2c4
const MONSTER_TABLE_WORDS = Math.floor((BUY_ARM_LIMIT - MONSTER_TABLE) / 4);
export const MAX_MONSTER_STOCK = MONSTER_TABLE_WORDS - 2;

const PICKER = 0x80018d1c;
const BUY_PRICE = 0x80016770;
const CAN_AFFORD = 0x800167c8;
const GOLD_SUB = 0x800167f0;
const READ_AMOUNT = 0x80016840;
const SFX = 0x800164a0;
const EXIT_CHAIN = 0x8001a1a6;

// Whatever occupies the buy arm before patching: the vanilla apology, or an
// earlier revision of this patch.
const BUY_ARM_KNOWN_HEADS = [
  [0x08, 0x57, 0x26],
  [0x11, 0x08, 0x15],
  [0x08, 0x15],
];

// ---------------------------------------------------------------------------
// Script bytecode
//
// The shop dialogue is an interpreted bytecode, not MIPS. Opcodes used here,
// all inferred from the working sell branch at RAM 0x8001a079:
//
//   0x08          open message window       0x0a  newline
//   0x11          end page / wait           0x0b  choice row marker
//   0x15 <addr>   gosub script subroutine   0x16  return
//   0x17 <addr>   jump                      0x1a  jump table (n x addr)
//   0x2c <n>      present n choices         0x4c <addr>  call native routine
//   0x3e 0x0e <a> branch if last result 0   0xfd 0x0f    print last result
//
// Text is full-width Shift-JIS.
// ---------------------------------------------------------------------------
const PUNCT = {
  ' ': 0x8140, '.': 0x8144, ',': 0x8143, "'": 0x8166,
  '[': 0x816d, ']': 0x816e, '(': 0x8169, ')': 0x816a,
  '!': 0x8149, '?': 0x8148,
};

function text(s) {
  const out = [];
  for (const ch of s) {
    let v;
    if (ch >= 'A' && ch <= 'Z') v = 0x8260 + (ch.charCodeAt(0) - 0x41);
    else if (ch >= 'a' && ch <= 'z') v = 0x8281 + (ch.charCodeAt(0) - 0x61);
    else if (ch >= '0' && ch <= '9') v = 0x824f + (ch.charCodeAt(0) - 0x30);
    else if (ch in PUNCT) v = PUNCT[ch];
    else throw new PatchError(`No full-width mapping for ${JSON.stringify(ch)}`);
    out.push(v >> 8, v & 0xff);
  }
  return Uint8Array.from(out);
}

/** Two-pass script assembler so forward labels resolve. */
class Script {
  constructor(base) {
    this.base = base;
    this.buf = [];
    this.fixups = [];
    this.labels = new Map();
  }
  label(name) { this.labels.set(name, this.base + this.buf.length); }
  op(...vals) { this.buf.push(...vals); }
  raw(bytes) { this.buf.push(...bytes); }
  addr(target) {
    if (typeof target === 'string') {
      this.fixups.push([this.buf.length, target]);
      this.buf.push(0, 0, 0, 0);
    } else {
      this.buf.push(target & 0xff, (target >>> 8) & 0xff,
                    (target >>> 16) & 0xff, (target >>> 24) & 0xff);
    }
  }
  call(fn) { this.op(0x4c); this.addr(fn); }
  gosub(a) { this.op(0x15); this.addr(a); }
  goto(a) { this.op(0x17); this.addr(a); }
  ifZero(a) { this.op(0x3e, 0x0e); this.addr(a); }
  assemble() {
    for (const [off, name] of this.fixups) {
      const v = this.labels.get(name);
      this.buf[off] = v & 0xff;
      this.buf[off + 1] = (v >>> 8) & 0xff;
      this.buf[off + 2] = (v >>> 16) & 0xff;
      this.buf[off + 3] = (v >>> 24) & 0xff;
    }
    return Uint8Array.from(this.buf);
  }
}

function buildBuyArm() {
  const s = new Script(BUY_ARM);

  s.op(0x08);
  s.label('retry');
  s.gosub(PICKER);            // let the player mark goods
  s.ifZero(EXIT_CHAIN);       // backed out of the table

  s.op(0x08);
  s.call(BUY_PRICE);          // AMOUNT = total, also returned
  s.raw(text("That'll be "));
  s.op(0xfd, 0x0f);
  s.raw(text('G.'));
  s.op(0x0a, 0x57, 0x01, 0x0b);
  s.raw(text("[I'll buy.]     [My mistake.]"));
  s.op(0x0a, 0x0b);
  s.raw(text('[Not buying.]'));
  s.op(0x2c, 0x03);
  s.op(0x1a);
  s.addr('confirm');
  s.addr('retry');
  s.addr(EXIT_CHAIN);

  s.label('confirm');
  s.call(CAN_AFFORD);
  s.ifZero('poor');
  s.call(SFX);
  s.call(GOLD_SUB);
  s.call(GIVE_WRAPPER);
  s.call(READ_AMOUNT);
  s.op(0x08, 0x57, 0x26);
  s.raw(text('(Pays '));
  s.op(0xfd, 0x0f);
  s.raw(text('G.)'));
  s.op(0x11);
  s.goto(EXIT_CHAIN);

  s.label('poor');
  s.op(0x08, 0x57, 0x26);
  s.raw(text("You don't have enough money."));
  s.op(0x11);
  s.goto(EXIT_CHAIN);

  return s.assemble();
}

function applyMonsterShop(img, stock, log) {
  const table = buildStockTable(stock, MAX_MONSTER_STOCK);
  const code = buildCopyLoop(MONSTER_TABLE);

  const head = img.readU32(codeS(MONSTER_BUILDER));
  if (!MONSTER_KNOWN_HEADS.includes(head)) {
    throw new PatchError(
      `Unexpected data at the monster shop's list builder (${hex32(head)}). ` +
      'Is this an unmodified Azure Dreams (USA) image?'
    );
  }
  img.write(codeS(MONSTER_BUILDER), wordsToBytes(code));
  for (const [ram, old, vanilla] of MONSTER_OLD_PATCH) {
    if (img.readU32(codeS(ram)) === old) img.writeU32(codeS(ram), vanilla);
  }
  let b = streamToBin(codeS(MONSTER_BUILDER));
  log(`list builder     ${hexOff(b.off)}  sector ${b.lba}  copy loop, ${stock.length} items`);

  for (const [ram, expect, next, note] of GIVE_WRAPPER_PATCHES) {
    img.patchU32(codeS(ram), expect, next, note);
  }
  b = streamToBin(codeS(GIVE_WRAPPER));
  log(`give-item hook   ${hexOff(b.off)}  sector ${b.lba}`);

  const arm = buildBuyArm();
  if (arm.length !== BUY_ARM_BYTES) {
    throw new PatchError(`Buy flow is ${arm.length} bytes, expected ${BUY_ARM_BYTES}; the stock table sits after it.`);
  }
  const cur = img.read(scriptS(BUY_ARM), arm.length);
  if (!bytesEqual(cur, arm)) {
    const known = BUY_ARM_KNOWN_HEADS.some((h) => h.every((v, i) => cur[i] === v));
    if (!known) {
      throw new PatchError(
        'Unexpected data at the monster shop dialogue. ' +
        'Is this an unmodified Azure Dreams (USA) image?'
      );
    }
  }
  img.write(scriptS(BUY_ARM), arm);
  b = streamToBin(scriptS(BUY_ARM));
  log(`buy flow script  ${hexOff(b.off)}  sector ${b.lba}  ` +
      `${arm.length} of ${BUY_ARM_LIMIT - BUY_ARM} free bytes`);

  img.write(scriptS(MONSTER_TABLE), wordsToBytes(padTable(table, MONSTER_TABLE_WORDS)));
  b = streamToBin(scriptS(MONSTER_TABLE));
  log(`stock table      ${hexOff(b.off)}  sector ${b.lba}  ${table.length * 4} of ${MONSTER_TABLE_WORDS * 4} bytes, after the buy flow`);
}

// ---------------------------------------------------------------------------
// Entry point
// ---------------------------------------------------------------------------
function normalize(config) {
  const shop = (s) => ({
    enabled: !!(s && s.enabled),
    stock: (s && Array.isArray(s.stock) ? s.stock : []).map((e) => ({
      cat: e.cat | 0, id: e.id | 0, quality: e.quality | 0,
    })),
  });
  return {
    barry: shop(config.barry),
    monsterShop: shop(config.monsterShop),
    prices: config.prices || {},
  };
}

/**
 * Patch a disc image in place.
 *
 * @param {Uint8Array} buf   the whole .bin, modified in place
 * @param {object} config    see the header comment
 * @param {(line:string)=>void} log
 * @returns {{touched:number[], totalSectors:number}}
 */
export function patchImage(buf, config, log = () => {}) {
  if (buf.length % SECTOR !== 0) {
    throw new PatchError(
      `This image is ${buf.length} bytes, which is not a whole number of ` +
      '2352-byte sectors. The patcher needs a MODE2/2352 .bin, not a 2048-byte-per-sector rip.'
    );
  }
  const cfg = normalize(config);
  if (!cfg.barry.enabled && !cfg.monsterShop.enabled) {
    throw new PatchError('Pick at least one shop to patch.');
  }

  selftest(buf);
  log('error-correction self-test passed');

  const img = new Image(buf);
  const stocked = new Set();
  if (cfg.barry.enabled) {
    applyBarry(img, cfg.barry.stock, log);
    cfg.barry.stock.forEach((e) => stocked.add(`${e.cat}:${e.id}`));
  }
  if (cfg.monsterShop.enabled) {
    applyMonsterShop(img, cfg.monsterShop.stock, log);
    cfg.monsterShop.stock.forEach((e) => stocked.add(`${e.cat}:${e.id}`));
  }
  applyPrices(img, cfg.prices, stocked, log);

  img.finalize();
  const bad = img.checkSectors();
  if (bad.length) {
    throw new PatchError(`Sectors failed validation: ${bad.join(', ')}`);
  }

  const touched = [...img.touched].sort((a, b) => a - b);
  log(`rewrote error-correction for sectors ${touched.join(', ')}`);
  return { touched, totalSectors: buf.length / SECTOR };
}

/**
 * The sectors a config would rewrite, and why, without needing the image.
 * Uses the category array pointers the caller supplies (web/items.js carries
 * them); patchImage re-reads them from the disc and checks they agree.
 */
export function planSectors(config, categories) {
  const cfg = normalize(config);
  const roles = new Map();
  const add = (stream, role) => {
    const { lba } = streamToBin(stream);
    if (!roles.has(lba)) roles.set(lba, role);
  };
  if (cfg.barry.enabled) {
    add(BARRY_BUILDER, 'Barry’s stock builder');
    add(slusS(BARRY_TABLE), 'Barry’s stock table');
  }
  if (cfg.monsterShop.enabled) {
    add(codeS(MONSTER_BUILDER), 'monster shop list builder');
    add(codeS(GIVE_WRAPPER), 'give-item hook');
    add(scriptS(BUY_ARM), 'buy flow script and stock table');
  }
  const arrOf = new Map(categories.map((c) => [c.cat, c.arr]));
  for (const key of Object.keys(cfg.prices)) {
    const { cat, id } = parseKey(key);
    if (cat === CAT_EGG) {
      EGG_ARRAYS.forEach((a, i) => add(a + id * ITEM_RECORD + BUY_PRICE_OFF,
        i ? 'egg prices, second copy' : 'egg prices'));
    } else if (arrOf.has(cat)) {
      add(slusS(arrOf.get(cat)) + id * ITEM_RECORD + BUY_PRICE_OFF, 'item prices');
    }
  }
  return [...roles].sort((a, b) => a[0] - b[0]).map(([lba, role]) => ({ lba, role }));
}

export function cueFor(binName) {
  return `FILE "${binName}" BINARY\n  TRACK 01 MODE2/2352\n    FLAGS DCP\n    INDEX 01 00:00:00\n`;
}

export { PatchError };
