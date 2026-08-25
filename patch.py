#!/usr/bin/env python3
"""Azure Dreams (USA) shop patcher.

Two changes, either of which can be applied on its own:

  Barry's shop      stocks whatever you choose (default: the vanilla two plus
                    seven monster eggs) instead of two fixed items
  Monster shop      "I've come to buy." opens a real store instead of an
                    apology, with price quote, confirm prompt, payment and
                    delivery (default: every monster's egg)

Stock and prices come from a JSON config, the same one web/index.html
exports. Without --config the built-in defaults apply: sell prices stay
vanilla, buy prices are twice the sell price, sand is 1000G.

Works on a MODE2/2352 .bin. Every modified sector gets fresh EDC/ECC, so the
result is a valid disc image.

    ./patch.py "Azure Dreams (USA).bin"
    ./patch.py in.bin -o out.bin --config shops.json
    ./patch.py in.bin --dump-config shops.json      # the defaults, to edit
    ./patch.py in.bin --verify known-good.bin

No dependencies beyond the standard library.
"""
from __future__ import annotations

import argparse
import json
import os
import struct
import sys

# ---------------------------------------------------------------------------
# Disc geometry
#
# A MODE2/2352 sector is 0x930 bytes: 0x18 of sync+subheader, 0x800 of payload,
# then 0x118 of EDC/ECC. "Stream offset" below means an offset into the payload
# bytes with all that overhead removed -- the coordinate space the game's own
# data is laid out in, and the one every address here is expressed in.
# ---------------------------------------------------------------------------
SECTOR, HDR, DATA = 0x930, 0x18, 0x800


def stream_to_bin(s: int) -> tuple[int, int]:
    """Payload-stream offset -> (raw .bin offset, LBA)."""
    lba, w = divmod(s, DATA)
    return lba * SECTOR + HDR + w, lba


# ---------------------------------------------------------------------------
# EDC / ECC for Mode 2 Form 1
# ---------------------------------------------------------------------------
_eccF = bytearray(256)
_eccB = bytearray(256)
_edcL = [0] * 256
for _i in range(256):
    _j = ((_i << 1) ^ (0x11D if _i & 0x80 else 0)) & 0xFF
    _eccF[_i] = _j
    _eccB[_i ^ _j] = _i
    _e = _i
    for _ in range(8):
        _e = (_e >> 1) ^ (0xD8018001 if _e & 1 else 0)
    _edcL[_i] = _e


def _edc(buf, off, size):
    e = 0
    for k in range(off, off + size):
        e = (e >> 8) ^ _edcL[(e ^ buf[k]) & 0xFF]
    return e


def _ecc(sec, src, major_count, minor_count, major_mult, minor_inc, dest):
    size = major_count * minor_count
    for major in range(major_count):
        index = (major >> 1) * major_mult + (major & 1)
        a = b = 0
        for _ in range(minor_count):
            t = sec[src + index]
            index += minor_inc
            if index >= size:
                index -= size
            a ^= t
            b ^= t
            a = _eccF[a]
        a = _eccB[_eccF[a] ^ b]
        sec[dest + major] = a
        sec[dest + major + major_count] = a ^ b


def regen_sector(sec: bytearray) -> None:
    """Recompute EDC and ECC in place for one Mode 2 Form 1 sector."""
    sec[0x14:0x18] = sec[0x10:0x14]
    struct.pack_into("<I", sec, 0x818, _edc(sec, 0x10, 0x808))
    saved = bytes(sec[12:16])
    sec[12:16] = b"\0\0\0\0"
    _ecc(sec, 0xC, 86, 24, 2, 86, 0x81C)
    _ecc(sec, 0xC, 52, 43, 86, 88, 0x8C8)
    sec[12:16] = saved


# ---------------------------------------------------------------------------
# Image
# ---------------------------------------------------------------------------
class Image:
    def __init__(self, data: bytes):
        self.buf = bytearray(data)
        self.touched: set[int] = set()

    def read(self, stream_off: int, n: int) -> bytes:
        out = bytearray()
        i = 0
        while i < n:
            off, _ = stream_to_bin(stream_off + i)
            k = min(DATA - ((stream_off + i) % DATA), n - i)
            out += self.buf[off:off + k]
            i += k
        return bytes(out)

    def write(self, stream_off: int, data: bytes) -> None:
        i = 0
        while i < len(data):
            off, lba = stream_to_bin(stream_off + i)
            k = min(DATA - ((stream_off + i) % DATA), len(data) - i)
            self.buf[off:off + k] = data[i:i + k]
            self.touched.add(lba)
            i += k

    def read_u32(self, stream_off):
        return struct.unpack("<I", self.read(stream_off, 4))[0]

    def write_u32(self, stream_off, val):
        self.write(stream_off, struct.pack("<I", val))

    def patch_u32(self, stream_off, expect, new, note=""):
        """Write a word, tolerating the case where it is already applied."""
        cur = self.read_u32(stream_off)
        if cur == new:
            return False
        if cur != expect:
            _, lba = stream_to_bin(stream_off)
            raise SystemExit(
                f"unexpected data at stream 0x{stream_off:08x} (LBA {lba}): "
                f"found {cur:08x}, expected {expect:08x}. {note}"
            )
        self.write_u32(stream_off, new)
        return True

    def finalize(self) -> None:
        for lba in sorted(self.touched):
            sec = bytearray(self.buf[lba * SECTOR:(lba + 1) * SECTOR])
            regen_sector(sec)
            self.buf[lba * SECTOR:(lba + 1) * SECTOR] = sec

    def check_sectors(self) -> list[int]:
        bad = []
        for lba in sorted(self.touched):
            sec = bytearray(self.buf[lba * SECTOR:(lba + 1) * SECTOR])
            chk = bytearray(sec)
            regen_sector(chk)
            if bytes(chk) != bytes(sec):
                bad.append(lba)
        return bad


def selftest(data: bytes) -> None:
    """Recompute EDC/ECC on untouched sectors; they must come out identical."""
    for lba in (182, 193, 1883, 5000, 6147, 6149, 6158, 6195, 14930):
        base = lba * SECTOR
        orig = bytes(data[base:base + SECTOR])
        if len(orig) < SECTOR or orig[0x12] & 0x20:
            continue
        sec = bytearray(orig)
        regen_sector(sec)
        if bytes(sec) != orig:
            raise SystemExit(
                f"EDC/ECC self-test failed on LBA {lba}. Refusing to patch. "
                "Is this a MODE2/2352 image?"
            )


# ---------------------------------------------------------------------------
# MIPS
# ---------------------------------------------------------------------------
ZERO, AT, V0, V1, A0, A1, RA = 0, 1, 2, 3, 4, 5, 31
NOP = 0


def addiu(rt, rs, imm):
    return (0x09 << 26) | (rs << 21) | (rt << 16) | (imm & 0xFFFF)


def lw(rt, base, off):
    return (0x23 << 26) | (base << 21) | (rt << 16) | (off & 0xFFFF)


def sw(rt, base, off):
    return (0x2B << 26) | (base << 21) | (rt << 16) | (off & 0xFFFF)


def bne(rs, rt, off):
    return (0x05 << 26) | (rs << 21) | (rt << 16) | (off & 0xFFFF)


def jr(rs):
    return (rs << 21) | 0x08


def lui(rt, imm):
    return (0x0F << 26) | (rt << 16) | (imm & 0xFFFF)


def move(rd, rs):
    return (rs << 21) | (rd << 11) | 0x21


def jump(target):
    return 0x08000000 | ((target >> 2) & 0x03FFFFFF)


JR_RA = 0x03E00008


# ---------------------------------------------------------------------------
# Script bytecode
#
# The shop dialogue is an interpreted bytecode, not MIPS. Opcodes used here,
# all inferred from the working sell branch at RAM 0x8001a079:
#
#   0x08            open message window        0x0a  newline
#   0x11            end page / wait            0x0b  choice row marker
#   0x15 <addr>     gosub script subroutine    0x16  return
#   0x17 <addr>     jump                       0x1a  jump table (n x addr)
#   0x2c <n>        present n choices          0x4c <addr>  call native routine
#   0x3e 0x0e <a>   branch if last result 0    0xfd 0x0f    print last result
#
# Text is full-width Shift-JIS.
# ---------------------------------------------------------------------------
_PUNCT = {" ": 0x8140, ".": 0x8144, ",": 0x8143, "'": 0x8166,
          "[": 0x816D, "]": 0x816E, "(": 0x8169, ")": 0x816A,
          "!": 0x8149, "?": 0x8148}


def text(s: str) -> bytes:
    out = bytearray()
    for ch in s:
        if "A" <= ch <= "Z":
            v = 0x8260 + (ord(ch) - 0x41)
        elif "a" <= ch <= "z":
            v = 0x8281 + (ord(ch) - 0x61)
        elif "0" <= ch <= "9":
            v = 0x824F + (ord(ch) - 0x30)
        elif ch in _PUNCT:
            v = _PUNCT[ch]
        else:
            raise ValueError(f"no full-width mapping for {ch!r}")
        out += bytes((v >> 8, v & 0xFF))
    return bytes(out)


class Script:
    """Two-pass script assembler so forward labels resolve."""

    def __init__(self, base: int):
        self.base = base
        self.buf = bytearray()
        self.fixups: list[tuple[int, str]] = []
        self.labels: dict[str, int] = {}

    def label(self, name):
        self.labels[name] = self.base + len(self.buf)

    def op(self, *vals):
        self.buf += bytes(vals)

    def raw(self, data):
        self.buf += data

    def addr(self, target):
        if isinstance(target, str):
            self.fixups.append((len(self.buf), target))
            self.buf += b"\0\0\0\0"
        else:
            self.buf += struct.pack("<I", target)

    def call(self, fn):
        self.op(0x4C)
        self.addr(fn)

    def gosub(self, a):
        self.op(0x15)
        self.addr(a)

    def goto(self, a):
        self.op(0x17)
        self.addr(a)

    def if_zero(self, a):
        self.op(0x3E, 0x0E)
        self.addr(a)

    def assemble(self) -> bytes:
        for off, name in self.fixups:
            struct.pack_into("<I", self.buf, off, self.labels[name])
        return bytes(self.buf)


# ---------------------------------------------------------------------------
# Addresses
# ---------------------------------------------------------------------------

# slus_006.14 is loaded once at boot and stays resident, so a RAM address in
# it maps to the disc by a constant.
SLUS_DELTA = 0x80020800


def slus_s(ram):
    return ram - SLUS_DELTA


# Category descriptor table in slus: 20-byte records, item array pointer at
# +0x0c. Item records are 20 bytes with u16 buy at +0x10 and sell at +0x12.
CAT_TABLE = 0x80073414
CAT_STRIDE, CAT_ARR_OFF = 20, 0x0C
ITEM_RECORD, BUY_PRICE_OFF, SELL_PRICE_OFF = 20, 0x10, 0x12
NAME_PTR_OFF = 0x04

# The egg array is not in slus. It lives in main.bin and again in dungeon.bin,
# because town and tower each need item data; a price edit has to be mirrored
# or the value changes depending on where the player stands.
CAT_EGG = 0x12
EGG_ARRAY_RAM = 0x8002CA68
EGG_ARRAYS = (0x003ADA68, 0x01D29268)

# Where the stock tables go. Both blocks are zero on disc and untouched in
# every RAM dump taken across town, both shops, both towns and the tower
# (docs/FINDINGS.md, "Where a stub can live"). 0x8007bcb0-0x8007bdf0 below
# Barry's table is reserved for newtown.py's extended gate stub.
LIST_CAPACITY = 64                 # the shop's list buffer is 0x100 bytes
MAX_STOCK = LIST_CAPACITY - 2      # minus the header row and terminator
BARRY_TABLE = 0x8007BDF0           # 256 bytes, to 0x8007bef0
MONSTER_TABLE = 0x800815B4         # 256 bytes, to 0x800816b4

# The "Pay" pseudo-row that draws the menu header. Both shops copy the same
# four bytes from their own chunk; it has to stay entry 0.
HEADER_ROW = 0x00001601

CAT_SAND, CAT_BALL = 0x0A, 0x04
PRICE_MAX = 0xFFFF

# Vanilla Barry plus the seven eggs the first version of this patch added.
BARRY_DEFAULT_STOCK = [
    (0x0F, 0x02), (0x01, 0x01),
    (CAT_EGG, 0x02), (CAT_EGG, 0x16), (CAT_EGG, 0x10), (CAT_EGG, 0x0E),
    (CAT_EGG, 0x08), (CAT_EGG, 0x0C), (CAT_EGG, 0x03),
]
MAX_EGG = 0x2D   # eggs past this are NPCs and scenery with a row for the book


def stock_word(entry, i):
    cat, iid, q = entry["cat"], entry["id"], entry.get("quality", 0)
    if not (0 < iid < 256 and 0 < cat < 256):
        raise SystemExit(f"stock entry {i}: id/category out of range")
    if not -128 <= q <= 127:
        raise SystemExit(f"stock entry {i}: quality {q} does not fit a byte")
    return iid | (cat << 8) | ((q & 0xFF) << 16)


def build_stock_table(stock):
    """The table a shop's builder copies: header, entries, zero terminator."""
    if len(stock) > MAX_STOCK:
        raise SystemExit(f"{len(stock)} items; a shop can list at most {MAX_STOCK}")
    seen = set()
    words = [HEADER_ROW]
    for i, e in enumerate(stock):
        k = (e["cat"], e["id"])
        if k in seen:
            raise SystemExit(f"stock entry {i} duplicates {k[0]}:{k[1]}")
        seen.add(k)
        words.append(stock_word(e, i))
    words.append(0)
    return words


def pad_table(words):
    """Zero the rest of the table's block so a shorter list leaves no stale tail."""
    return words + [0] * (LIST_CAPACITY - len(words))


def build_copy_loop(table):
    """The builder that replaces a shop's hardcoded list.

    Copy words from TABLE to the list buffer in $a0 until the zero terminator
    has been copied too. Nine instructions, leaf, clobbers only $v0/$v1/$a0.
    The load in the loop is two instructions ahead of its use, which the
    R3000's delay slot needs.
    """
    hi = (table >> 16) + (1 if table & 0x8000 else 0)
    return [
        lui(V1, hi),
        addiu(V1, V1, table & 0xFFFF),
        lw(V0, V1, 0),              # loop:
        addiu(V1, V1, 4),
        sw(V0, A0, 0),
        bne(V0, ZERO, -4),          # back to loop (offset from the delay slot)
        addiu(A0, A0, 4),
        jr(RA),
        NOP,
    ]


def words_bytes(words):
    return b"".join(struct.pack("<I", w & 0xFFFFFFFF) for w in words)


# ---------------------------------------------------------------------------
# Items and prices
# ---------------------------------------------------------------------------
def _ram_to_stream(ram):
    if ram >= 0x8002D000:
        return slus_s(ram)
    if ram >= 0x80020000:
        return ram - EGG_ARRAY_RAM + EGG_ARRAYS[0]
    raise SystemExit(f"pointer 0x{ram:08x} is in neither slus nor main.bin")


def category_array(img, cat):
    arr = img.read_u32(slus_s(CAT_TABLE + cat * CAT_STRIDE + CAT_ARR_OFF))
    count = img.read(slus_s(CAT_TABLE + cat * CAT_STRIDE), 3)[2]
    return arr, count


def item_name(img, cat, iid):
    arr, _ = category_array(img, cat)
    ptr = img.read_u32(_ram_to_stream(arr) + iid * ITEM_RECORD + NAME_PTR_OFF)
    if not ptr:
        return ""
    raw = img.read(_ram_to_stream(ptr), 48)
    raw = raw.split(b"\0", 1)[0]
    import unicodedata
    s = unicodedata.normalize("NFKC", raw.decode("shift_jis", errors="replace"))
    return s.replace("−", "-").replace("’", "'").strip()


def item_prices(img, cat, iid):
    rec = price_records(img, cat, iid)[0]
    return (struct.unpack("<H", img.read(rec + BUY_PRICE_OFF, 2))[0],
            struct.unpack("<H", img.read(rec + SELL_PRICE_OFF, 2))[0])


def price_records(img, cat, iid):
    """Stream offsets of every copy of an item's record."""
    arr, _ = category_array(img, cat)
    if cat == CAT_EGG:
        if arr != EGG_ARRAY_RAM:
            raise SystemExit(f"egg array pointer is {arr:08x}, expected {EGG_ARRAY_RAM:08x}")
        return [a + iid * ITEM_RECORD for a in EGG_ARRAYS]
    if not 0x8002D000 <= arr < 0x80081800:
        raise SystemExit(f"category {cat} item array pointer {arr:08x} is not in slus")
    return [slus_s(arr) + iid * ITEM_RECORD]


def default_price(img, cat, iid):
    """Sell stays what the game says, buy is twice that. Sand is the exception
    the patch was asked for: 1000G to buy, 500G to sell. An item that sells for
    nothing keeps its vanilla buy price rather than being given away."""
    if cat == CAT_SAND:
        return {"buy": 1000, "sell": 500}
    buy, sell = item_prices(img, cat, iid)
    return {"buy": buy if sell == 0 else min(sell * 2, PRICE_MAX), "sell": sell}


def default_quality(cat):
    return 5 if cat == CAT_BALL else 0


def default_config(img):
    """Matches web/config.js defaultConfig(), by hand-kept agreement."""
    prices = {}

    def stock_of(pairs):
        out = []
        for cat, iid in pairs:
            prices[f"{cat}:{iid}"] = default_price(img, cat, iid)
            out.append({"cat": cat, "id": iid, "quality": default_quality(cat)})
        return out

    barry = stock_of(BARRY_DEFAULT_STOCK)
    _, count = category_array(img, CAT_EGG)
    eggs = [(CAT_EGG, i) for i in range(1, min(count, MAX_EGG) + 1)
            if item_name(img, CAT_EGG, i)]
    monster = stock_of(eggs)
    return {"barry": {"enabled": True, "stock": barry},
            "monsterShop": {"enabled": True, "stock": monster},
            "prices": prices}


def _parse_key(key):
    try:
        cat, iid = key.split(":")
        return int(cat), int(iid)
    except ValueError:
        raise SystemExit(f'bad price key "{key}"; expected "<cat>:<id>"')


def apply_prices(img, prices, stocked, log):
    changed = same = 0
    for key in sorted(prices, key=_parse_key):
        cat, iid = _parse_key(key)
        buy, sell = prices[key]["buy"], prices[key]["sell"]
        for what, v in (("buy", buy), ("sell", sell)):
            if not isinstance(v, int) or not 0 <= v <= PRICE_MAX:
                raise SystemExit(f"{what} price for {key} must be 0..65535, got {v!r}")
        if key in stocked and buy < sell:
            log(f"  warning: {key} buys for {buy}G and sells for {sell}G; that is a money loop")
        wrote = False
        for rec in price_records(img, cat, iid):
            if (rec + BUY_PRICE_OFF) % DATA > DATA - 4:
                raise SystemExit(f"price fields for {key} straddle a sector")
            cur = img.read(rec + BUY_PRICE_OFF, 4)
            new = struct.pack("<HH", buy, sell)
            if cur != new:
                img.write(rec + BUY_PRICE_OFF, new)
                wrote = True
        if wrote:
            changed += 1
        else:
            same += 1
    log(f"  item prices  {changed} written, {same} already as requested")


# ---------------------------------------------------------------------------
# Patch A: Barry's shop
#
# Barry's stock comes from a hardcoded routine (RAM 0x800165c4, stream
# 0x00c19dc4) that writes a NUL-terminated list of 4-byte entries into a
# 0x100-byte buffer. Vanilla spells out each byte individually. It becomes the
# copy loop above, reading BARRY_TABLE.
# ---------------------------------------------------------------------------
BARRY_BUILDER = 0x00C19DC4
BARRY_SLOTS = 22

# Whatever occupies the builder before patching: vanilla, the earlier
# nine-item revision of this patch, or this one.
BARRY_KNOWN_HEADS = (0x00801021, 0x24031601, 0x3C038008)


def apply_barry(img: Image, stock, log) -> None:
    table = build_stock_table(stock)
    code = build_copy_loop(BARRY_TABLE)
    code += [NOP] * (BARRY_SLOTS - len(code))
    if (BARRY_BUILDER % DATA) + BARRY_SLOTS * 4 > DATA:
        raise SystemExit("builder straddles a sector boundary")

    head = img.read_u32(BARRY_BUILDER)
    if head not in BARRY_KNOWN_HEADS:
        raise SystemExit(f"unexpected bytes at Barry's builder: {head:08x}")
    img.write(BARRY_BUILDER, words_bytes(code))
    off, lba = stream_to_bin(BARRY_BUILDER)
    log(f"  stock builder  .bin 0x{off:08x}  LBA {lba}  copy loop, {len(stock)} items")

    img.write(slus_s(BARRY_TABLE), words_bytes(pad_table(table)))
    off, lba = stream_to_bin(slus_s(BARRY_TABLE))
    log(f"  stock table    .bin 0x{off:08x}  LBA {lba}  "
        f"{len(table) * 4} of {LIST_CAPACITY * 4} bytes")


# ---------------------------------------------------------------------------
# Patch B: monster shop
#
# The monster shop overlay occupies RAM 0x8001xxxx but is stitched together
# from two different disc regions, so code and script need separate deltas.
#
# The overlay carries a complete but entirely unreferenced buy-side library,
# mirroring the sell side it sits next to:
#
#     purpose               sell         buy
#     price -> AMOUNT       0x8001679c   0x80016770   (same core, a1=0)
#     gold                  0x80016818   0x800167f0   (add / subtract)
#     affordability            --        0x800167c8
#     inventory transfer    0x80016850   0x800170e8   (same core, mode=0)
#     read AMOUNT           0x80016840   0x80016840
#
# So the buy flow below is a structural mirror of the known-good sell branch.
# ---------------------------------------------------------------------------
CODE_DELTA = 0xBEB800     # stream = (ram & 0x1fffff) + this, for the code chunk
SCRIPT_DELTA = 0xBED2E8   # ... and for the script chunk


def code_s(ram):
    return (ram & 0x1FFFFF) + CODE_DELTA


def script_s(ram):
    return (ram & 0x1FFFFF) + SCRIPT_DELTA


# The list builder at 0x800165c4 emits furniture; it becomes the copy loop
# reading MONSTER_TABLE. Vanilla begins with a stack frame, this patch with the
# loop's lui. (An earlier revision of this patch edited three words inside the
# vanilla function instead; its head is still vanilla's.)
MONSTER_BUILDER = 0x800165C4
MONSTER_KNOWN_HEADS = (0x27BDFFD8, 0x3C038008)

# The three words that earlier revision changed, restored to vanilla so an
# image patched twice comes out identical to one patched once.
MONSTER_OLD_PATCH = (
    (0x80016600, 0x24130012, 0x24130018),
    (0x80016628, 0x00000000, 0x34420080),
    (0x80016634, 0x2A220019, 0x2A220021),
)

# 0x800170e8 is the buy-mode inventory wrapper, but it expects the list in $a0
# while script opcode 0x4c calls with no arguments. It has zero callers, so it
# is rewritten as a nullary tail call that supplies CTX+4 itself.
GIVE_WRAPPER = 0x800170E8
XFER_CORE = 0x8001702C
CTX_PLUS_4_LO = 0x88D4    # 0x80020000 - 0x772c = 0x800188d4

GIVE_WRAPPER_PATCHES = [
    (GIVE_WRAPPER + 0x0, 0x27BDFFE8, lui(A0, 0x8002), "lui   $a0, 0x8002"),
    (GIVE_WRAPPER + 0x4, 0xAFBF0010, move(A1, ZERO), "move  $a1, $zero"),
    (GIVE_WRAPPER + 0x8, 0x0C005C0B, jump(XFER_CORE), "j     transfer core"),
    (GIVE_WRAPPER + 0xC, 0x00002821, addiu(A0, A0, CTX_PLUS_4_LO), "addiu $a0, $a0, -0x772c"),
]

BUY_ARM = 0x8001A1B0
BUY_ARM_LIMIT = 0x8001A3BC     # the "just looking" branch starts here

PICKER = 0x80018D1C
BUY_PRICE = 0x80016770
CAN_AFFORD = 0x800167C8
GOLD_SUB = 0x800167F0
READ_AMOUNT = 0x80016840
SFX = 0x800164A0
EXIT_CHAIN = 0x8001A1A6

# Whatever occupies the buy arm before patching: the vanilla apology, or an
# earlier revision of this patch.
BUY_ARM_KNOWN_HEADS = (
    bytes([0x08, 0x57, 0x26]),      # vanilla "I must apologize,"
    bytes([0x11, 0x08, 0x15]),      # bare gosub stub
    bytes([0x08, 0x15]),            # this patch
)


def build_buy_arm() -> bytes:
    s = Script(BUY_ARM)

    s.op(0x08)
    s.label("retry")
    s.gosub(PICKER)                      # let the player mark goods
    s.if_zero(EXIT_CHAIN)                # backed out of the table

    s.op(0x08)
    s.call(BUY_PRICE)                    # AMOUNT = total, also returned
    s.raw(text("That'll be "))
    s.op(0xFD, 0x0F)
    s.raw(text("G."))
    s.op(0x0A, 0x57, 0x01, 0x0B)
    s.raw(text("[I'll buy.]     [My mistake.]"))
    s.op(0x0A, 0x0B)
    s.raw(text("[Not buying.]"))
    s.op(0x2C, 0x03)
    s.op(0x1A)
    s.addr("confirm")
    s.addr("retry")
    s.addr(EXIT_CHAIN)

    s.label("confirm")
    s.call(CAN_AFFORD)
    s.if_zero("poor")
    s.call(SFX)
    s.call(GOLD_SUB)
    s.call(GIVE_WRAPPER)
    s.call(READ_AMOUNT)
    s.op(0x08, 0x57, 0x26)
    s.raw(text("(Pays "))
    s.op(0xFD, 0x0F)
    s.raw(text("G.)"))
    s.op(0x11)
    s.goto(EXIT_CHAIN)

    s.label("poor")
    s.op(0x08, 0x57, 0x26)
    s.raw(text("You don't have enough money."))
    s.op(0x11)
    s.goto(EXIT_CHAIN)

    return s.assemble()


def apply_monster_shop(img: Image, stock, log) -> None:
    table = build_stock_table(stock)
    code = build_copy_loop(MONSTER_TABLE)

    head = img.read_u32(code_s(MONSTER_BUILDER))
    if head not in MONSTER_KNOWN_HEADS:
        raise SystemExit(f"unexpected bytes at the monster shop's list builder: {head:08x}")
    img.write(code_s(MONSTER_BUILDER), words_bytes(code))
    for ram, old, vanilla in MONSTER_OLD_PATCH:
        if img.read_u32(code_s(ram)) == old:
            img.write_u32(code_s(ram), vanilla)
    off, lba = stream_to_bin(code_s(MONSTER_BUILDER))
    log(f"  list builder      .bin 0x{off:08x}  LBA {lba}  copy loop, {len(stock)} items")

    img.write(slus_s(MONSTER_TABLE), words_bytes(pad_table(table)))
    off, lba = stream_to_bin(slus_s(MONSTER_TABLE))
    log(f"  stock table       .bin 0x{off:08x}  LBA {lba}  "
        f"{len(table) * 4} of {LIST_CAPACITY * 4} bytes")

    for ram, expect, new, note in GIVE_WRAPPER_PATCHES:
        img.patch_u32(code_s(ram), expect, new, note)
    off, lba = stream_to_bin(code_s(GIVE_WRAPPER))
    log(f"  give-item wrapper .bin 0x{off:08x}  LBA {lba}")

    arm = build_buy_arm()
    if BUY_ARM + len(arm) > BUY_ARM_LIMIT:
        raise SystemExit(
            f"buy arm is {len(arm)} bytes, only {BUY_ARM_LIMIT - BUY_ARM} available"
        )
    cur = img.read(script_s(BUY_ARM), len(arm))
    if cur != arm and not any(cur.startswith(h) for h in BUY_ARM_KNOWN_HEADS):
        raise SystemExit(f"unexpected bytes at buy arm: {cur[:8].hex(' ')}")
    img.write(script_s(BUY_ARM), arm)
    off, lba = stream_to_bin(script_s(BUY_ARM))
    log(f"  buy flow script   .bin 0x{off:08x}  LBA {lba}  "
        f"{len(arm)} of {BUY_ARM_LIMIT - BUY_ARM} free bytes")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def write_cue(path, bin_name):
    with open(path, "w") as f:
        f.write(f'FILE "{bin_name}" BINARY\n'
                "  TRACK 01 MODE2/2352\n"
                "    FLAGS DCP\n"
                "    INDEX 01 00:00:00\n")


def load_config(path, img):
    cfg = json.load(open(path))
    for shop in ("barry", "monsterShop"):
        s = cfg.setdefault(shop, {})
        s.setdefault("enabled", False)
        s.setdefault("stock", [])
        for e in s["stock"]:
            e.setdefault("quality", 0)
    cfg.setdefault("prices", {})
    return cfg


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Patch Azure Dreams (USA) shops.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Works on")[0],
    )
    p.add_argument("input", help="source .bin (MODE2/2352)")
    p.add_argument("-o", "--output", help="destination .bin "
                                          "(default: '<input> [Shops].bin')")
    p.add_argument("--config", metavar="JSON",
                   help="stock and prices, as exported by the browser patcher "
                        "(default: the built-in stock, see --dump-config)")
    p.add_argument("--dump-config", metavar="JSON",
                   help="write the default config for this image and exit")
    p.add_argument("--no-barry", action="store_true",
                   help="leave Barry's shop alone")
    p.add_argument("--no-monster-shop", action="store_true",
                   help="leave the monster shop alone")
    p.add_argument("--no-cue", action="store_true", help="skip writing a .cue")
    p.add_argument("--verify", metavar="REFERENCE",
                   help="compare the result against a known-good image "
                        "instead of writing it")
    args = p.parse_args(argv)

    data = open(args.input, "rb").read()
    if len(data) % SECTOR:
        sys.exit(f"{args.input} is not a multiple of {SECTOR} bytes; "
                 "this needs a MODE2/2352 image, not MODE1/2048")
    print(f"input   {args.input}  ({len(data) // SECTOR} sectors)")

    selftest(data)
    print("EDC/ECC self-test passed")

    img = Image(data)
    if args.dump_config:
        with open(args.dump_config, "w") as f:
            json.dump(default_config(img), f, indent=1)
        print(f"wrote  {args.dump_config}")
        return 0

    cfg = load_config(args.config, img) if args.config else default_config(img)
    if args.no_barry:
        cfg["barry"]["enabled"] = False
    if args.no_monster_shop:
        cfg["monsterShop"]["enabled"] = False
    if not cfg["barry"]["enabled"] and not cfg["monsterShop"]["enabled"]:
        p.error("nothing to do: both shops disabled")

    stocked = set()
    if cfg["barry"]["enabled"]:
        print("\nBarry's shop")
        apply_barry(img, cfg["barry"]["stock"], print)
        stocked |= {f'{e["cat"]}:{e["id"]}' for e in cfg["barry"]["stock"]}
    if cfg["monsterShop"]["enabled"]:
        print("\nMonster shop")
        apply_monster_shop(img, cfg["monsterShop"]["stock"], print)
        stocked |= {f'{e["cat"]}:{e["id"]}' for e in cfg["monsterShop"]["stock"]}

    print("\nPrices")
    apply_prices(img, cfg["prices"], stocked, print)

    img.finalize()
    bad = img.check_sectors()
    print(f"\nrewrote EDC/ECC for sectors {sorted(img.touched)}")
    if bad:
        sys.exit(f"sectors failed validation: {bad}")
    print("all modified sectors valid")

    if args.verify:
        ref = open(args.verify, "rb").read()
        if bytes(img.buf) == ref:
            print(f"\nMATCH: output is byte-identical to {args.verify}")
            return 0
        diffs = [i for i in range(min(len(ref), len(img.buf)))
                 if ref[i] != img.buf[i]]
        print(f"\nDIFFERS from {args.verify}: {len(diffs)} bytes")
        for i in diffs[:16]:
            print(f"  0x{i:08x} (LBA {i // SECTOR})  "
                  f"ref {ref[i]:02x} != got {img.buf[i]:02x}")
        return 1

    out = args.output or f"{os.path.splitext(args.input)[0]} [Shops].bin"
    with open(out, "wb") as f:
        f.write(img.buf)
    print(f"\nwrote  {out}")
    if not args.no_cue:
        cue = f"{os.path.splitext(out)[0]}.cue"
        write_cue(cue, os.path.basename(out))
        print(f"wrote  {cue}")

    for shop, label in (("barry", "Barry"), ("monsterShop", "The monster shop")):
        if not cfg[shop]["enabled"]:
            continue
        print(f"\n{label} now stocks:")
        for e in cfg[shop]["stock"]:
            buy, sell = item_prices(img, e["cat"], e["id"])
            name = item_name(img, e["cat"], e["id"]) or f'{e["cat"]}:{e["id"]}'
            print(f"  {name:24s} buy {buy:5d}G  sell {sell:5d}G")
    return 0


if __name__ == "__main__":
    sys.exit(main())
