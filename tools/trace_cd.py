#!/usr/bin/env python3
"""Log every disc read the game performs, so we can see what a location loads.

Azure Dreams funnels all disc I/O through one path:

    43 call sites -> dispatcher 0x8003e4fc -> enqueue 0x8003e39c
                  -> 24-byte records in a 32-entry ring at 0x80083968
                  -> pump 0x8003e758 -> libcd CdRead

The pump is the useful place to watch, because just before it calls CdRead all
three interesting values sit in saved registers at once:

    8003eaa4  lw    $a0, 4($s1)   <- $s1 is the ring entry, +4 is an integer LBA
    8003eaa8  jal   0x8006124c    <- CdIntToPos, which confirms it is an LBA
    ...
    8003eae4  move  $a0, $s2      <- $s2 = sector count
    8003eae8  move  $a1, $s0      <- $s0 = destination address
    8003eaec  jal   0x80063224    <- CdRead

So we replace the two `move`s with a jump to a stub, record
(lba, count, destination, command) into a ring in unused RAM, then perform the
two moves and drop back in front of the CdRead.

Usage:
    trace_cd.py patch  <vanilla.bin> -o <traced.bin>
    trace_cd.py decode <ramdump.bin>

Produce the ram dump with tools/../../ad_ramdump.py while the game is running.
"""
import argparse
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from patch import (SECTOR, DATA, Image, selftest, write_cue,  # noqa: E402
                   addiu, lui, move, jump, stream_to_bin)

# --------------------------------------------------------------------------
# Address spaces
#
# slus_006.14 starts at disc sector 24 and its text loads at 0x8002d000 from
# 0x800 into the file, so a runtime address maps to the de-sectored stream by
# subtracting a constant.
# --------------------------------------------------------------------------
SLUS_DELTA = 0x80020800


def slus_s(ram: int) -> int:
    return ram - SLUS_DELTA


# --------------------------------------------------------------------------
# Real estate
#
# Everything lives inside holes in the executable's own text. That matters: the
# first version put the log in a 30 KB region of general RAM that read as zero
# in four snapshots, and the game still reused it -- entering the tower reset
# the cursor and destroyed the town records. Reading as zero only proves nothing
# was there at dump time, not that the game leaves it alone.
#
# slus_006.14 is loaded once at boot and never reloaded, and none of the loads
# we have observed target its address range, so gaps in it are genuinely stable.
# The stub has survived a scene change here, which is direct evidence.
# --------------------------------------------------------------------------
STUB = 0x80079580
STUB_BUDGET = 960

REC = 16                  # bytes per record, in both rings

LOG_PTR = 0x800795D0      # u32: running total of bytes written, never reset
LOG_BASE = 0x80079E00     # 3.3 KB gap, zero on disc and in every snapshot
LOG_SIZE = 0x400          # 1 KB = 64 records
RING_MASK = (LOG_SIZE - 1) & ~(REC - 1)

# Second hook, on the CD dispatcher at 0x8003e4fc.
#
# The pump only sees a request filed earlier, so its return address says nothing
# about who wanted the data. Hooking the enqueue one level down is no better:
# the enqueue is only ever reached from the wrapper band inside the dispatcher,
# so $ra just points back into the CD layer. The dispatcher is the outermost
# entry point -- 43 call sites spread across the engine -- so $ra there is
# genuine game code, which is what we need to find where a location's sector
# number comes from. It takes (command, argument), and for a read the argument
# is the sector.
ENQ_STUB = 0x80079600
ENQ_STUB_BUDGET = 0x50
ENQ_PTR = 0x80079650
ENQ_BASE = 0x8007A200
ENQ_SIZE = 0x800          # 2 KB = 128 records
ENQ_MASK = (ENQ_SIZE - 1) & ~(REC - 1)

ENQ_HOOK = 0x8003E4FC     # addiu $sp, $sp, -0x18
ENQ_HOOK2 = 0x8003E500    # sw    $s0, 0x10($sp)
ENQ_RESUME = 0x8003E504
ENQ_ORIGINAL = (0x27BDFFE8, 0xAFB00010)

HOOK = 0x8003EAE4         # move $a0, $s2
HOOK2 = 0x8003EAE8        # move $a1, $s0
RESUME = 0x8003EAEC       # jal CdRead
HOOK_ORIGINAL = (0x02402021, 0x02002821)

# Third hook, on the location-id store at 0x8003bb54.
#
# A door hands this function a 20-byte warp descriptor. Its second halfword is
# the location id, and that id selects a 32-byte record which decides not just
# which artwork loads but which routine inside the chunk gets control. Watching
# the store tells us what id each door announces, which is the one fact a disc
# dump cannot give us: most descriptors live in compressed archives.
#
#     8003bb44  lhu  $v0, 2($s0)     <- id, into $v0
#     8003bb50  lui  $at, 0x800d
#     8003bb54  sb   $v0, 0x381a($at)
#     8003bb58  lh   $v1, ($s0)      <- $v0 and $v1 both dead from here
#
# $s0 is the descriptor, so logging it alongside the id identifies the door.
LOC_STUB = 0x80079780
LOC_STUB_BUDGET = 0x78
LOC_PTR = 0x800797F8
LOC_BASE = 0x8007AA00
LOC_SIZE = 0x200          # 512 bytes = 64 records
LOC_REC = 8
LOC_MASK = (LOC_SIZE - 1) & ~(LOC_REC - 1)

LOC_HOOK = 0x8003BB50     # lui $at, 0x800d
LOC_HOOK2 = 0x8003BB54    # sb  $v0, 0x381a($at)
LOC_RESUME = 0x8003BB58
LOC_ORIGINAL = (0x3C01800D, 0xA022381A)

ZERO, AT, V0, V1, A0 = 0, 1, 2, 3, 4
A1, A2, T0, SP, S0, S1, S2, S3, RA = 5, 6, 8, 29, 16, 17, 18, 19, 31


def lw(rt, base, off):
    return (0x23 << 26) | (base << 21) | (rt << 16) | (off & 0xFFFF)


def lbu(rt, base, off):
    return (0x24 << 26) | (base << 21) | (rt << 16) | (off & 0xFFFF)


def andi(rt, rs, imm):
    return (0x0C << 26) | (rs << 21) | (rt << 16) | (imm & 0xFFFF)


def addu(rd, rs, rt):
    return (rs << 21) | (rt << 16) | (rd << 11) | 0x21


def sw_(rt, base, off):
    return (0x2B << 26) | (base << 21) | (rt << 16) | (off & 0xFFFF)


def sb(rt, base, off):
    return (0x28 << 26) | (base << 21) | (rt << 16) | (off & 0xFFFF)


NOP = 0


def hi_lo(addr):
    """Split an address for lui/addiu, accounting for sign-extension of the low half."""
    lo = addr & 0xFFFF
    hi = (addr >> 16) + (1 if lo & 0x8000 else 0)
    return hi, lo


def build_stub():
    """Assemble the logger.

    Only $at, $v0, $v1, $a0 and $a1 are touched. $a0/$a1 are dead here because
    the two instructions we displaced were about to set them, and $v0/$v1 are
    caller-saved across the CdRead call that follows. $s0/$s1/$s2 carry our data
    and are left alone.

    The instruction order is not free: the R3000 has a load delay slot and no
    interlock, so the instruction directly after a load still sees the old
    register. Every load below is therefore separated from its first use by an
    independent instruction. Collapsing these will silently log stale values.
    """
    ptr_hi, ptr_lo = hi_lo(LOG_PTR)
    base_hi, base_lo = hi_lo(LOG_BASE)
    return [
        lui(AT, ptr_hi),
        lw(V0, AT, ptr_lo),          # v0 = running total
        lui(A0, base_hi),            # covers the load delay on v0
        addiu(A0, A0, base_lo),      # a0 = LOG_BASE
        andi(V1, V0, RING_MASK),     # v1 = offset within the ring
        addu(V1, V1, A0),            # v1 = &log[offset]
        lw(A0, S1, 4),               # ring entry +4 = LBA
        sw_(S2, V1, 4),              # sector count; covers the load delay
        sw_(A0, V1, 0),
        lbu(A0, S1, 0),              # ring entry +0 = command byte
        sw_(S0, V1, 8),              # destination; covers the load delay
        sw_(A0, V1, 12),
        addiu(V0, V0, REC),
        sw_(V0, AT, ptr_lo),
        move(A0, S2),                # the two instructions we displaced
        move(A1, S0),
        jump(RESUME),
        NOP,
    ]


def build_enq_stub():
    """Assemble the dispatcher logger: command, argument, caller, third arg.

    Runs before the target's prologue, so $a0-$a2 still hold the arguments and
    $ra still holds the caller. $at, $v0, $v1 and $t0 are all dead here ($t
    registers are caller-saved, so nothing can be relying on them across a
    call). The same R3000 load-delay rule as the other stub applies.
    """
    ptr_hi, ptr_lo = hi_lo(ENQ_PTR)
    base_hi, base_lo = hi_lo(ENQ_BASE)
    return [
        lui(AT, ptr_hi),
        lw(V0, AT, ptr_lo),          # v0 = running total
        lui(V1, base_hi),            # covers the load delay on v0
        addiu(V1, V1, base_lo),      # v1 = ENQ_BASE
        andi(T0, V0, ENQ_MASK),      # t0 = offset within the ring
        addu(V1, V1, T0),            # v1 = &log[offset]
        sw_(A0, V1, 0),              # command
        sw_(A1, V1, 4),              # the sector, for a read command
        sw_(RA, V1, 8),              # who asked for it
        sw_(A2, V1, 12),             # params pointer
        addiu(V0, V0, REC),
        sw_(V0, AT, ptr_lo),
        addiu(SP, SP, -0x18),        # the two instructions we displaced
        sw_(S0, SP, 0x10),
        jump(ENQ_RESUME),
        NOP,
    ]


def build_loc_stub():
    """Assemble the location-id logger: id, descriptor pointer.

    Only $at and $v1 are clobbered, and $v0 only after the displaced store has
    used it -- all three are overwritten by the instructions we return to. The
    saved registers that carry the caller's state, $s0 above all, are untouched.
    The R3000 load-delay rule from the other stubs applies here too.
    """
    ptr_hi, ptr_lo = hi_lo(LOC_PTR)
    base_hi, base_lo = hi_lo(LOC_BASE)
    return [
        lui(AT, ptr_hi),
        lw(V1, AT, ptr_lo),          # v1 = running total
        LOC_ORIGINAL[0],             # lui $at, 0x800d -- covers the load delay
        LOC_ORIGINAL[1],             # sb  $v0, 0x381a($at), the real store
        andi(V1, V1, LOC_MASK),      # v1 = offset within the ring
        lui(AT, base_hi),
        addiu(AT, AT, base_lo),
        addu(V1, V1, AT),            # v1 = &log[offset]
        sw_(V0, V1, 0),              # the location id
        sw_(S0, V1, 4),              # which descriptor announced it
        lui(AT, ptr_hi),
        lw(V0, AT, ptr_lo),
        NOP,                         # load delay on v0
        addiu(V0, V0, LOC_REC),
        sw_(V0, AT, ptr_lo),
        jump(LOC_RESUME),
        NOP,
    ]


def apply_trace(img: Image, log) -> None:
    code = build_stub()
    if len(code) * 4 > STUB_BUDGET:
        raise SystemExit(f"stub needs {len(code) * 4} bytes, hole is {STUB_BUDGET}")

    stub_end = STUB + len(code) * 4
    if LOG_PTR < stub_end:
        raise SystemExit(f"log cursor 0x{LOG_PTR:08x} overlaps the stub, which "
                         f"ends at 0x{stub_end:08x}")
    if LOG_BASE < stub_end < LOG_BASE + LOG_SIZE:
        raise SystemExit("log ring overlaps the stub")
    if LOG_SIZE & (LOG_SIZE - 1):
        raise SystemExit("LOG_SIZE must be a power of two; the ring wraps by masking")

    stub_s = slus_s(STUB)
    existing = img.read(stub_s, STUB_BUDGET)
    payload = b"".join(struct.pack("<I", c) for c in code)
    if existing[:len(payload)] not in (bytes(len(payload)), payload):
        raise SystemExit(f"stub hole at 0x{STUB:08x} is not empty; refusing to overwrite")
    img.write(stub_s, payload)
    off, lba = stream_to_bin(stub_s)
    log(f"  stub          0x{STUB:08x}  .bin 0x{off:08x}  LBA {lba}  "
        f"{len(code)} instructions")

    head = (img.read_u32(slus_s(HOOK)), img.read_u32(slus_s(HOOK2)))
    if head != HOOK_ORIGINAL and head != (jump(STUB), NOP):
        raise SystemExit(
            f"unexpected code at the hook: {head[0]:08x} {head[1]:08x}, "
            f"expected {HOOK_ORIGINAL[0]:08x} {HOOK_ORIGINAL[1]:08x}"
        )
    img.write_u32(slus_s(HOOK), jump(STUB))
    img.write_u32(slus_s(HOOK2), NOP)
    off, lba = stream_to_bin(slus_s(HOOK))
    log(f"  hook          0x{HOOK:08x}  .bin 0x{off:08x}  LBA {lba}")
    log(f"  log ring      0x{LOG_BASE:08x}  {LOG_SIZE // REC} records of {REC} bytes")
    log(f"  write cursor  0x{LOG_PTR:08x}")

    code = build_enq_stub()
    if len(code) * 4 > ENQ_STUB_BUDGET:
        raise SystemExit(f"enqueue stub needs {len(code) * 4} bytes, "
                         f"budget is {ENQ_STUB_BUDGET}")
    if ENQ_BASE < LOG_BASE + LOG_SIZE and LOG_BASE < ENQ_BASE + ENQ_SIZE:
        raise SystemExit("the two log rings overlap")

    enq_s = slus_s(ENQ_STUB)
    payload = b"".join(struct.pack("<I", c) for c in code)
    existing = img.read(enq_s, len(payload))
    if existing not in (bytes(len(payload)), payload):
        raise SystemExit(f"enqueue stub hole at 0x{ENQ_STUB:08x} is not empty")
    img.write(enq_s, payload)
    off, lba = stream_to_bin(enq_s)
    log(f"  enq stub      0x{ENQ_STUB:08x}  .bin 0x{off:08x}  LBA {lba}  "
        f"{len(code)} instructions")

    head = (img.read_u32(slus_s(ENQ_HOOK)), img.read_u32(slus_s(ENQ_HOOK2)))
    if head != ENQ_ORIGINAL and head != (jump(ENQ_STUB), NOP):
        raise SystemExit(
            f"unexpected code at the enqueue hook: {head[0]:08x} {head[1]:08x}"
        )
    img.write_u32(slus_s(ENQ_HOOK), jump(ENQ_STUB))
    img.write_u32(slus_s(ENQ_HOOK2), NOP)
    off, lba = stream_to_bin(slus_s(ENQ_HOOK))
    log(f"  enq hook      0x{ENQ_HOOK:08x}  .bin 0x{off:08x}  LBA {lba}")
    log(f"  enq ring      0x{ENQ_BASE:08x}  {ENQ_SIZE // REC} records")

    code = build_loc_stub()
    if len(code) * 4 > LOC_STUB_BUDGET:
        raise SystemExit(f"location stub needs {len(code) * 4} bytes, "
                         f"budget is {LOC_STUB_BUDGET}")
    if LOC_PTR < LOC_STUB + len(code) * 4:
        raise SystemExit("location cursor overlaps its stub")
    for lo, hi in ((LOG_BASE, LOG_BASE + LOG_SIZE), (ENQ_BASE, ENQ_BASE + ENQ_SIZE)):
        if LOC_BASE < hi and lo < LOC_BASE + LOC_SIZE:
            raise SystemExit("the location ring overlaps another ring")

    loc_s = slus_s(LOC_STUB)
    payload = b"".join(struct.pack("<I", c) for c in code)
    existing = img.read(loc_s, len(payload))
    if existing not in (bytes(len(payload)), payload):
        raise SystemExit(f"location stub hole at 0x{LOC_STUB:08x} is not empty")
    img.write(loc_s, payload)
    off, lba = stream_to_bin(loc_s)
    log(f"  loc stub      0x{LOC_STUB:08x}  .bin 0x{off:08x}  LBA {lba}  "
        f"{len(code)} instructions")

    head = (img.read_u32(slus_s(LOC_HOOK)), img.read_u32(slus_s(LOC_HOOK2)))
    if head != LOC_ORIGINAL and head != (jump(LOC_STUB), NOP):
        raise SystemExit(
            f"unexpected code at the location hook: {head[0]:08x} {head[1]:08x}, "
            f"expected {LOC_ORIGINAL[0]:08x} {LOC_ORIGINAL[1]:08x}"
        )
    img.write_u32(slus_s(LOC_HOOK), jump(LOC_STUB))
    img.write_u32(slus_s(LOC_HOOK2), NOP)
    off, lba = stream_to_bin(slus_s(LOC_HOOK))
    log(f"  loc hook      0x{LOC_HOOK:08x}  .bin 0x{off:08x}  LBA {lba}")
    log(f"  loc ring      0x{LOC_BASE:08x}  {LOC_SIZE // LOC_REC} records")


# --------------------------------------------------------------------------
# Reading the log back out
# --------------------------------------------------------------------------
TOWN_LBA, TOWN_END = 3077, 11324
REGIONS = [
    (24, 279, "slus_006.14"),
    (280, 1815, "OVMOVIE.BIN"),
    (1817, 3076, "MAIN.BIN"),
    (3077, 11324, "TOWN.BIN"),
    (11325, 12308, "STRT/*.STR"),
    (12310, 25890, "DUNGEON.BIN"),
    (25892, 126795, "STR/*.STR"),
]


def region_of(lba):
    for lo, hi, name in REGIONS:
        if lo <= lba <= hi:
            rel = f"  +{lba - lo}" if name.endswith(".BIN") else ""
            return f"{name}{rel}"
    return "?"


def decode(path, show_all=False):
    ram = open(path, "rb").read()
    if len(ram) < 0x200000:
        sys.exit(f"{path} is {len(ram)} bytes; expected a 2 MB guest RAM dump")

    def u32(a):
        return struct.unpack_from("<I", ram, a & 0x1FFFFF)[0]

    total = u32(LOG_PTR)
    if total == 0:
        sys.exit("log is empty. Is this a dump of a traced image, and did the "
                 "game load anything since boot?")
    n = total // REC
    wrapped = total > LOG_SIZE
    print(f"{n} disc reads logged"
          + (f" (ring holds the most recent {LOG_SIZE // REC})" if wrapped else ""))
    print()

    first = max(0, n - LOG_SIZE // REC)
    rows = []
    for i in range(first, n):
        a = LOG_BASE + ((i * REC) & RING_MASK)
        lba, count, dest, cmd = u32(a), u32(a + 4), u32(a + 8), u32(a + 12)
        rows.append((i, lba, count, dest, cmd))

    print(f"  {'#':>5}  {'sector':>7}  {'count':>5}  {'destination':>11}  "
          f"{'cmd':>4}  where")
    prev = None
    for i, lba, count, dest, cmd in rows:
        if not show_all and prev is not None and (lba, count, dest) == prev:
            continue
        prev = (lba, count, dest)
        print(f"  {i:>5}  {lba:>7}  {count:>5}  0x{dest:08x}  0x{cmd:02x}  "
              f"{region_of(lba)}")

    stray = [r for r in rows if not 0 < r[1] < 126946 or r[2] > 4096]
    if stray:
        print(f"\nWARNING: {len(stray)} records have implausible sectors or counts. "
              "The log region is probably being reused by the game.")

    town = [r for r in rows if TOWN_LBA <= r[1] <= TOWN_END]
    if town:
        lo = min(r[1] for r in town)
        hi = max(r[1] + max(1, r[2]) for r in town)
        print(f"\nTOWN.BIN reads: {len(town)}, sectors {lo}-{hi} "
              f"(relative {lo - TOWN_LBA}-{hi - TOWN_LBA})")

    decode_locations(ram)
    decode_enqueue(ram)


LOC_TABLE = 0x800D2FB4    # 32-byte location records, first byte is the asset id
LOC_LIVE = 0x800D381A     # the byte the hooked store writes


def decode_locations(ram):
    """Read the location log: which id each door announced, and what it selects."""
    def u32(a):
        return struct.unpack_from("<I", ram, a & 0x1FFFFF)[0]

    total = u32(LOC_PTR)
    if total == 0:
        print("\n\nno location changes logged")
        return
    n = total // LOC_REC
    first = max(0, n - LOC_SIZE // LOC_REC)
    print(f"\n\n{n} location changes"
          + (f" (ring holds the most recent {LOC_SIZE // LOC_REC})"
             if n > LOC_SIZE // LOC_REC else ""))
    print(f"\n  {'#':>5}  {'loc id':>6}  {'asset':>5}  {'descriptor':>10}  "
          f"{'entry point':>11}  record")
    for i in range(first, n):
        a = LOC_BASE + ((i * LOC_REC) & LOC_MASK)
        loc, desc = u32(a), u32(a + 4)
        if loc < 64:
            rec = ram[(LOC_TABLE + loc * 32) & 0x1FFFFF:
                      (LOC_TABLE + loc * 32 + 32) & 0x1FFFFF]
            asset, entry = rec[0], struct.unpack_from("<I", rec, 24)[0]
            body = rec[:8].hex(" ")
        else:
            asset, entry, body = -1, 0, "out of range"
        print(f"  {i:>5}  {loc:>6}  {asset:>5}  0x{desc:08x}  0x{entry:08x}  {body}")
    print(f"\ncurrent location id = {ram[LOC_LIVE & 0x1FFFFF]}")


def decode_enqueue(ram):
    """Read the enqueue log: which code asked for which sector."""
    def u32(a):
        return struct.unpack_from("<I", ram, a & 0x1FFFFF)[0]

    total = u32(ENQ_PTR)
    if total == 0:
        return
    n = total // REC
    first = max(0, n - ENQ_SIZE // REC)
    print(f"\n\n{n} enqueued requests"
          + (f" (ring holds the most recent {ENQ_SIZE // REC})" if n > ENQ_SIZE // REC
             else ""))
    print(f"\n  {'#':>5}  {'cmd':>4}  {'argument':>10}  {'caller':>10}  "
          f"{'params':>10}  where")
    callers = {}
    for i in range(first, n):
        a = ENQ_BASE + ((i * REC) & ENQ_MASK)
        cmd, arg, ra, params = u32(a), u32(a + 4), u32(a + 8), u32(a + 12)
        where = region_of(arg) if 24 <= arg <= 126946 else ""
        print(f"  {i:>5}  0x{cmd:02x}  {arg:>10}  0x{ra:08x}  0x{params:08x}  {where}")
        if 24 <= arg <= 126946:
            callers.setdefault(ra, set()).add(arg)
    if callers:
        print("\ncallers that requested disc sectors:")
        for ra, sectors in sorted(callers.items()):
            s = sorted(sectors)
            print(f"  0x{ra:08x} -> {len(s)} sector(s): {s[:10]}")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    pp = sub.add_parser("patch", help="build a traced image")
    pp.add_argument("input")
    pp.add_argument("-o", "--output")
    pp.add_argument("--no-cue", action="store_true")

    dp = sub.add_parser("decode", help="read the log out of a RAM dump")
    dp.add_argument("ramdump")
    dp.add_argument("--all", action="store_true",
                    help="show repeated identical reads instead of collapsing them")

    args = p.parse_args(argv)

    if args.cmd == "decode":
        decode(args.ramdump, args.all)
        return 0

    data = open(args.input, "rb").read()
    if len(data) % SECTOR:
        sys.exit(f"{args.input} is not a multiple of {SECTOR} bytes; "
                 "this needs a MODE2/2352 image")
    print(f"input   {args.input}  ({len(data) // SECTOR} sectors)")
    selftest(data)
    print("EDC/ECC self-test passed\n")

    img = Image(data)
    print("CD read tracer")
    apply_trace(img, print)

    img.finalize()
    bad = img.check_sectors()
    print(f"\nrewrote EDC/ECC for sectors {sorted(img.touched)}")
    if bad:
        sys.exit(f"sectors failed validation: {bad}")
    print("all modified sectors valid")

    out = args.output or f"{os.path.splitext(args.input)[0]} [CD trace].bin"
    with open(out, "wb") as f:
        f.write(img.buf)
    print(f"\nwrote  {out}")
    if not args.no_cue:
        cue = f"{os.path.splitext(out)[0]}.cue"
        write_cue(cue, os.path.basename(out))
        print(f"wrote  {cue}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
