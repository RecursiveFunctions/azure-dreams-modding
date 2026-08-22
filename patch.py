#!/usr/bin/env python3
"""Azure Dreams (USA) shop patcher.

Two changes, either of which can be applied on its own:

  Barry's shop      stocks 9 items instead of 2 (adds seven monster eggs)
  Monster shop      "I've come to buy." opens a real egg store instead of an
                    apology, with price quote, confirm prompt, payment and
                    delivery

Works on a MODE2/2352 .bin. Every modified sector gets fresh EDC/ECC, so the
result is a valid disc image.

    ./patch.py "Azure Dreams (USA).bin"
    ./patch.py in.bin -o out.bin --no-barry
    ./patch.py in.bin --verify known-good.bin

No dependencies beyond the standard library.
"""
from __future__ import annotations

import argparse
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
    for lba in (1883, 5000, 6147, 6149, 6158, 6195, 14930):
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
ZERO, AT, V0, V1, A0, A1 = 0, 1, 2, 3, 4, 5


def addiu(rt, rs, imm):
    return (0x09 << 26) | (rs << 21) | (rt << 16) | (imm & 0xFFFF)


def sw(rt, base, off):
    return (0x2B << 26) | (base << 21) | (rt << 16) | (off & 0xFFFF)


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
# Patch A: Barry's shop
#
# Barry's stock comes from a hardcoded routine (RAM 0x800165c4, stream
# 0x00c19dc4) that writes a NUL-terminated list of 4-byte entries. Vanilla
# spells out each byte individually; one immediate plus one word-store per
# entry fits 10 entries in the same 22 instruction slots.
# ---------------------------------------------------------------------------
BARRY_BUILDER = 0x00C19DC4
BARRY_SLOTS = 22
BARRY_ORIGINAL_HEAD = (0x00801021, 0x3C038002, 0x24678A98)

CAT_EGG = 0x12

# Entry 0 must stay the "Pay" pseudo-row that renders the menu header.
BARRY_STOCK = [
    (0x01, 0x16, "Pay (header row)"),
    (0x02, 0x0F, "Copper Sword"),
    (0x01, 0x01, "Medicinal Herb"),
    (0x02, CAT_EGG, "KEWNE egg"),
    (0x16, CAT_EGG, "TROLL egg"),
    (0x10, CAT_EGG, "CLOWN egg"),
    (0x0E, CAT_EGG, "NYUEL egg"),
    (0x08, CAT_EGG, "GRIFFON egg"),
    (0x0C, CAT_EGG, "ARACHNE egg"),
    (0x03, CAT_EGG, "DRAGON egg"),
]

# The egg item array lives in main.bin and again in dungeon.bin, because both
# need item data. Price edits must be mirrored or the value would change
# depending on whether you are in town or up the tower.
EGG_ARRAYS = (0x003ADA68, 0x01D29268)
ITEM_RECORD, BUY_PRICE_OFF, SELL_PRICE_OFF = 20, 0x10, 0x12
N_EGG_ITEMS = 24     # egg ids 0x01..0x18. The monster roster has 45 entries,
                     # but only these have egg items.


def egg_prices(img, iid):
    rec = EGG_ARRAYS[0] + iid * ITEM_RECORD
    return (struct.unpack("<H", img.read(rec + BUY_PRICE_OFF, 2))[0],
            struct.unpack("<H", img.read(rec + SELL_PRICE_OFF, 2))[0])


def apply_egg_prices(img: Image, log) -> None:
    """Raise every egg's buy price to its sell value.

    Vanilla prices nearly every egg at 100G while some sell for thousands -- an
    Ultimate egg costs 100G and sells for 50000G. That is harmless in vanilla,
    where no shop sells eggs, but both patches here do, so leaving it alone
    would hand the player an unlimited money loop.

    Values are read out of the image rather than hardcoded, so this cannot drift
    out of step with the game's own table.
    """
    raised = []
    for iid in range(1, N_EGG_ITEMS + 1):
        buy, sell = egg_prices(img, iid)
        if buy >= sell:
            continue
        for arr in EGG_ARRAYS:
            s = arr + iid * ITEM_RECORD + BUY_PRICE_OFF
            if s % DATA > DATA - 2:
                raise SystemExit(f"price field for egg 0x{iid:02x} straddles a sector")
            img.write(s, struct.pack("<H", sell))
        raised.append((iid, buy, sell))

    if raised:
        log(f"  raised {len(raised)} of {N_EGG_ITEMS} egg buy prices to sell value")
        for iid, buy, sell in raised:
            log(f"    egg 0x{iid:02x}  {buy}G -> {sell}G")
    else:
        log(f"  all {N_EGG_ITEMS} eggs already cost at least their sell value")


def apply_barry(img: Image, log) -> None:
    code = []
    for i, (iid, cat, _) in enumerate(BARRY_STOCK):
        word = (cat << 8) | iid
        if word >= 0x8000:
            raise SystemExit(f"entry {i}: immediate 0x{word:04x} would sign-extend")
        code += [addiu(V1, ZERO, word), sw(V1, A0, i * 4)]
    code += [JR_RA, sw(ZERO, A0, len(BARRY_STOCK) * 4)]

    if len(code) > BARRY_SLOTS:
        raise SystemExit(f"stock list needs {len(code)} slots, budget is {BARRY_SLOTS}")
    if (BARRY_BUILDER % DATA) + BARRY_SLOTS * 4 > DATA:
        raise SystemExit("builder straddles a sector boundary")

    head = struct.unpack("<3I", img.read(BARRY_BUILDER, 12))
    payload = b"".join(struct.pack("<I", c) for c in code)
    if head != BARRY_ORIGINAL_HEAD and img.read(BARRY_BUILDER, len(payload)) != payload:
        raise SystemExit(
            f"unexpected bytes at Barry's builder: {[f'{x:08x}' for x in head]}"
        )
    img.write(BARRY_BUILDER, payload)
    off, lba = stream_to_bin(BARRY_BUILDER)
    log(f"  stock builder  .bin 0x{off:08x}  LBA {lba}  "
        f"{len(code)}/{BARRY_SLOTS} slots, {len(BARRY_STOCK) - 1} items")



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


N_EGGS = 24               # egg ids 0x01..0x18

# The list builder at 0x800165c4 emits furniture; retarget it at eggs.
LIST_BUILDER_PATCHES = [
    (0x80016600, 0x24130018, 0x24130000 | CAT_EGG,
     "category: furniture -> egg"),
    (0x80016628, 0x34420080, 0x00000000,
     "stop flagging every entry unavailable"),
    (0x80016634, 0x2A220021, 0x2A220000 | (N_EGGS + 1),
     f"loop bound: 32 entries -> {N_EGGS}"),
]

# 0x800170e8 is the buy-mode inventory wrapper, but it expects the list in $a0
# while script opcode 0x4c calls with no arguments. It has zero callers, so it
# is rewritten as a nullary tail call that supplies CTX+4 itself.
GIVE_WRAPPER = 0x800170E8
XFER_CORE = 0x8001702C
CTX_PLUS_4_LO = 0x88D4     # 0x80020000 - 0x772c = 0x800188d4

GIVE_WRAPPER_PATCHES = [
    (GIVE_WRAPPER + 0x0, 0x27BDFFE8, lui(A0, 0x8002), "lui   $a0, 0x8002"),
    (GIVE_WRAPPER + 0x4, 0xAFBF0010, move(A1, ZERO), "move  $a1, $zero  (mode 0 = buy)"),
    (GIVE_WRAPPER + 0x8, 0x0C005C0B, jump(XFER_CORE), f"j     0x{XFER_CORE:08x}"),
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
    s.gosub(PICKER)                      # let the player mark eggs
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


def apply_monster_shop(img: Image, log) -> None:
    for ram, expect, new, note in LIST_BUILDER_PATCHES:
        img.patch_u32(code_s(ram), expect, new, note)
    off, lba = stream_to_bin(code_s(LIST_BUILDER_PATCHES[0][0]))
    log(f"  egg list builder  .bin 0x{off:08x}  LBA {lba}  ({N_EGGS} eggs)")

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


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Patch Azure Dreams (USA) shops.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Works on")[0],
    )
    p.add_argument("input", help="source .bin (MODE2/2352)")
    p.add_argument("-o", "--output", help="destination .bin "
                                          "(default: '<input> [Shops].bin')")
    p.add_argument("--no-barry", action="store_true",
                   help="leave Barry's shop alone")
    p.add_argument("--no-monster-shop", action="store_true",
                   help="leave the monster shop alone")
    p.add_argument("--no-cue", action="store_true", help="skip writing a .cue")
    p.add_argument("--verify", metavar="REFERENCE",
                   help="compare the result against a known-good image "
                        "instead of writing it")
    args = p.parse_args(argv)

    if args.no_barry and args.no_monster_shop:
        p.error("nothing to do: both patches disabled")

    data = open(args.input, "rb").read()
    if len(data) % SECTOR:
        sys.exit(f"{args.input} is not a multiple of {SECTOR} bytes; "
                 "this needs a MODE2/2352 image, not MODE1/2048")
    print(f"input   {args.input}  ({len(data) // SECTOR} sectors)")

    selftest(data)
    print("EDC/ECC self-test passed")

    img = Image(data)
    if not args.no_barry:
        print("\nBarry's shop")
        apply_barry(img, print)
    if not args.no_monster_shop:
        print("\nMonster shop")
        apply_monster_shop(img, print)

    # Either shop makes eggs purchasable, so pricing has to run for both.
    print("\nEgg prices")
    apply_egg_prices(img, print)

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

    if not args.no_barry:
        print("\nBarry now stocks:")
        for iid, cat, label in BARRY_STOCK[1:]:
            price = egg_prices(img, iid)[0] if cat == CAT_EGG else None
            print(f"  {label}" + (f"  ({price}G)" if price else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
