/*
 * Azure Dreams (USA) shop patcher -- patch logic.
 *
 * A direct port of patch.py. Pure computation, no DOM, so it runs equally well
 * in a browser or under node (see ../tools/compare.mjs, which uses that to
 * prove both implementations emit identical bytes).
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

/** Recompute EDC/ECC on untouched sectors; they must come out identical. */
function selftest(buf) {
  for (const lba of [1883, 5000, 6147, 6149, 6158, 6195, 14930]) {
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
const ZERO = 0, V1 = 3, A0 = 4, A1 = 5;
const addiu = (rt, rs, imm) => (((0x09 << 26) | (rs << 21) | (rt << 16) | (imm & 0xffff)) >>> 0);
const sw = (rt, base, off) => (((0x2b << 26) | (base << 21) | (rt << 16) | (off & 0xffff)) >>> 0);
const lui = (rt, imm) => (((0x0f << 26) | (rt << 16) | (imm & 0xffff)) >>> 0);
const move = (rd, rs) => (((rs << 21) | (rd << 11) | 0x21) >>> 0);
const jmp = (target) => ((0x08000000 | ((target >>> 2) & 0x03ffffff)) >>> 0);
const JR_RA = 0x03e00008;

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

// ---------------------------------------------------------------------------
// Patch A: Barry's shop
//
// Barry's stock comes from a hardcoded routine (RAM 0x800165c4, stream
// 0x00c19dc4) that writes a NUL-terminated list of 4-byte entries. Vanilla
// spells out each byte individually; one immediate plus one word-store per
// entry fits 10 entries in the same 22 instruction slots.
// ---------------------------------------------------------------------------
const BARRY_BUILDER = 0x00c19dc4;
const BARRY_SLOTS = 22;
const BARRY_ORIGINAL_HEAD = [0x00801021, 0x3c038002, 0x24678a98];

const CAT_EGG = 0x12;

// Entry 0 must stay the "Pay" pseudo-row that renders the menu header.
export const BARRY_STOCK = [
  [0x01, 0x16, 'Pay (header row)'],
  [0x02, 0x0f, 'Copper Sword'],
  [0x01, 0x01, 'Medicinal Herb'],
  [0x02, CAT_EGG, 'KEWNE egg'],
  [0x16, CAT_EGG, 'TROLL egg'],
  [0x10, CAT_EGG, 'CLOWN egg'],
  [0x0e, CAT_EGG, 'NYUEL egg'],
  [0x08, CAT_EGG, 'GRIFFON egg'],
  [0x0c, CAT_EGG, 'ARACHNE egg'],
  [0x03, CAT_EGG, 'DRAGON egg'],
];

// The egg item array lives in main.bin and again in dungeon.bin, because both
// need item data. Price edits must be mirrored or the value would change
// depending on whether you are in town or up the tower.
const EGG_ARRAYS = [0x003ada68, 0x01d29268];
const ITEM_RECORD = 20;
const BUY_PRICE_OFF = 0x10;
const SELL_PRICE_OFF = 0x12;
const N_EGG_ITEMS = 24; // egg ids 0x01..0x18. The monster roster has 45
                        // entries, but only these have egg items.

function eggPrices(img, iid) {
  const rec = EGG_ARRAYS[0] + iid * ITEM_RECORD;
  const b = img.read(rec + BUY_PRICE_OFF, 4);
  return { buy: b[0] | (b[1] << 8), sell: b[2] | (b[3] << 8) };
}

/**
 * Raise every egg's buy price to its sell value.
 *
 * Vanilla prices nearly every egg at 100G while some sell for thousands -- an
 * Ultimate egg costs 100G and sells for 50000G. Harmless in vanilla, where no
 * shop sells eggs, but both patches here do, so leaving it alone would hand the
 * player an unlimited money loop.
 *
 * Values are read out of the image rather than hardcoded, so this cannot drift
 * out of step with the game's own table.
 */
function applyEggPrices(img, log) {
  const price = new Uint8Array(2);
  let raised = 0;
  for (let iid = 1; iid <= N_EGG_ITEMS; iid++) {
    const { buy, sell } = eggPrices(img, iid);
    if (buy >= sell) continue;
    price[0] = sell & 0xff;
    price[1] = (sell >>> 8) & 0xff;
    for (const arr of EGG_ARRAYS) {
      img.write(arr + iid * ITEM_RECORD + BUY_PRICE_OFF, price);
    }
    raised++;
  }
  log(raised
    ? `egg prices      raised ${raised} of ${N_EGG_ITEMS} to sell value, both copies`
    : `egg prices      all ${N_EGG_ITEMS} already at or above sell value`);
}

function applyBarry(img, log) {
  const code = [];
  BARRY_STOCK.forEach(([iid, cat], i) => {
    const word = (cat << 8) | iid;
    if (word >= 0x8000) throw new PatchError(`Entry ${i} would sign-extend.`);
    code.push(addiu(V1, ZERO, word), sw(V1, A0, i * 4));
  });
  code.push(JR_RA, sw(ZERO, A0, BARRY_STOCK.length * 4));

  if (code.length > BARRY_SLOTS) {
    throw new PatchError(`Stock list needs ${code.length} slots, budget is ${BARRY_SLOTS}.`);
  }

  const payload = new Uint8Array(code.length * 4);
  code.forEach((c, i) => putU32(payload, i * 4, c));

  const head = img.read(BARRY_BUILDER, 12);
  const headWords = [getU32(head, 0), getU32(head, 4), getU32(head, 8)];
  const matchesOriginal = headWords.every((w, i) => w === BARRY_ORIGINAL_HEAD[i]);
  if (!matchesOriginal) {
    const cur = img.read(BARRY_BUILDER, payload.length);
    let same = true;
    for (let i = 0; i < payload.length; i++) if (cur[i] !== payload[i]) { same = false; break; }
    if (!same) {
      throw new PatchError(
        `Unexpected data at Barry's stock builder (${headWords.map(hex32).join(' ')}). ` +
        'Is this an unmodified Azure Dreams (USA) image?'
      );
    }
  }
  img.write(BARRY_BUILDER, payload);
  const b = streamToBin(BARRY_BUILDER);
  log(`stock builder    ${hexOff(b.off)}  sector ${b.lba}  ` +
      `${code.length}/${BARRY_SLOTS} slots, ${BARRY_STOCK.length - 1} items`);
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

const N_EGGS = 24; // egg ids 0x01..0x18

// The list builder at 0x800165c4 emits furniture; retarget it at eggs.
const LIST_BUILDER_PATCHES = [
  [0x80016600, 0x24130018, (0x24130000 | CAT_EGG) >>> 0, 'category: furniture -> egg'],
  [0x80016628, 0x34420080, 0x00000000, 'stop flagging every entry unavailable'],
  [0x80016634, 0x2a220021, (0x2a220000 | (N_EGGS + 1)) >>> 0, `loop bound -> ${N_EGGS}`],
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

function buildBuyArm() {
  const s = new Script(BUY_ARM);

  s.op(0x08);
  s.label('retry');
  s.gosub(PICKER);            // let the player mark eggs
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

function applyMonsterShop(img, log) {
  for (const [ram, expect, next, note] of LIST_BUILDER_PATCHES) {
    img.patchU32(codeS(ram), expect, next, note);
  }
  let b = streamToBin(codeS(LIST_BUILDER_PATCHES[0][0]));
  log(`egg list builder ${hexOff(b.off)}  sector ${b.lba}  ${N_EGGS} eggs`);

  for (const [ram, expect, next, note] of GIVE_WRAPPER_PATCHES) {
    img.patchU32(codeS(ram), expect, next, note);
  }
  b = streamToBin(codeS(GIVE_WRAPPER));
  log(`give-item hook   ${hexOff(b.off)}  sector ${b.lba}`);

  const arm = buildBuyArm();
  if (BUY_ARM + arm.length > BUY_ARM_LIMIT) {
    throw new PatchError(`Buy flow is ${arm.length} bytes; only ${BUY_ARM_LIMIT - BUY_ARM} fit.`);
  }
  const cur = img.read(scriptS(BUY_ARM), arm.length);
  let same = true;
  for (let i = 0; i < arm.length; i++) if (cur[i] !== arm[i]) { same = false; break; }
  if (!same) {
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
}

// ---------------------------------------------------------------------------
// Entry point
// ---------------------------------------------------------------------------
/**
 * Patch a disc image in place.
 *
 * @param {Uint8Array} buf   the whole .bin, modified in place
 * @param {{barry:boolean, monsterShop:boolean}} opts
 * @param {(line:string)=>void} log
 * @returns {{touched:number[], totalSectors:number}}
 */
export function patchImage(buf, opts, log = () => {}) {
  if (buf.length % SECTOR !== 0) {
    throw new PatchError(
      `This image is ${buf.length} bytes, which is not a whole number of ` +
      '2352-byte sectors. The patcher needs a MODE2/2352 .bin, not a 2048-byte-per-sector rip.'
    );
  }
  if (!opts.barry && !opts.monsterShop) {
    throw new PatchError('Pick at least one change to apply.');
  }

  selftest(buf);
  log('error-correction self-test passed');

  const img = new Image(buf);
  if (opts.barry) applyBarry(img, log);
  if (opts.monsterShop) applyMonsterShop(img, log);
  // Either shop makes eggs purchasable, so pricing has to run for both.
  applyEggPrices(img, log);

  img.finalize();
  const bad = img.checkSectors();
  if (bad.length) {
    throw new PatchError(`Sectors failed validation: ${bad.join(', ')}`);
  }

  const touched = [...img.touched].sort((a, b) => a - b);
  log(`rewrote error-correction for sectors ${touched.join(', ')}`);
  return { touched, totalSectors: buf.length / SECTOR };
}

export function cueFor(binName) {
  return `FILE "${binName}" BINARY\n  TRACK 01 MODE2/2352\n    FLAGS DCP\n    INDEX 01 00:00:00\n`;
}

export { PatchError };
