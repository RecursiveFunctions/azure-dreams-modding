#!/usr/bin/env python3
"""Clone Monsbaiya into unused disc space and point a door at the copy.

This is the first step towards a second town. It proves the whole pipeline --
that a location is just data at an absolute sector, that the sector can be
anywhere on the disc, and that redirecting a door is a table edit -- without
needing to author any new content yet.

Run it, then walk into the house. Instead of the house interior you arrive in a
second copy of Monsbaiya, loaded from a part of the disc that previously held
nothing but padding.

    python3 tools/newtown.py "Azure Dreams (USA).bin" -o "Azure Dreams (Twin Towns).bin"

See docs/FINDINGS.md, "How the game loads a place" and "The location table".
"""

from __future__ import annotations

import argparse
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from patch import (A0, A1, DATA, HDR, JR_RA, SECTOR, ZERO, Image,  # noqa: E402
                   addiu, apply_egg_prices, lui, regen_sector, sw, write_cue)

# ---------------------------------------------------------------------------
# What we are copying
#
# What can be cloned, and through which door.
#
# A door does not merely name a chunk. The script behind it also announces a
# *location id*, which the engine uses to look up that location's artwork in a
# table of its own. Point a door at a different chunk and the id does not
# follow: you get the new code with the old scenery, which hangs on entry.
#
# So a template is only valid through the door whose id already matches it.
# "house" reuses door 1 exactly as the game does, leaving relocation as the only
# variable. "monsbaiya" is the goal but needs door 1's id to say Monsbaiya, and
# that link is not yet patchable -- see docs/FINDINGS.md.
#
# count is what the door's table entry asks for; for monsbaiya the clone spans
# further because four of its asset reads are addressed relative to the chunk
# rather than by absolute sector.
#
# "twin" avoids the mismatch entirely by choosing a different door. Walking out
# of the house is the game's own way of arriving in Monsbaiya, so everything the
# engine sets up on that path -- the record it selects, the companion chunk it
# pulls in, the order it does things -- is already town-shaped. Sending that
# exit to a Monsbaiya clone changes one thing only, which sector the town is
# read from, and that is exactly the variable we want to test.
TEMPLATES = {
    "house":     dict(lba=5071, count=20, span=20,  door=1, packed=(20 << 23) | 0x16000),
    "monsbaiya": dict(lba=4444, count=19, span=103, door=1, packed=(19 << 23) | 0x16000),
    "twin":      dict(lba=4444, count=19, span=103, exit="slus",
                      packed=(19 << 23) | 0x16000),
    # Same clone, but reached by leaving Barry's shop instead, so the original
    # town keeps its own entrances and the two coexist. Whether this works turns
    # on a question the "twin" test left open -- see SHOP_EXIT below.
    "twin-shop": dict(lba=4444, count=19, span=103, exit="shop",
                      packed=(19 << 23) | 0x16000),
    # The coexistence test. Rather than redirect an exit, give the *entrance* to
    # the house a destination record of our own naming the clone. Exits are
    # untouched, so leaving the house still goes to the original at 4444 while
    # entering it goes to the copy at 31133 -- two towns at once, if it works.
    "twin-house": dict(lba=4444, count=19, span=103, warp=True, location=4,
                       packed=(19 << 23) | 0x16000),
    # Two towns at once. Rather than fight a kind-12 warp into carrying a town,
    # make the exit fallback choose. Exits are the one path that demonstrably
    # arrives in a town correctly, and each one announces its own location id
    # before the choice is made, so the id is a free discriminator.
    "twotowns":  dict(lba=4444, count=19, span=103, gate=21,
                      packed=(19 << 23) | 0x16000),
}

# Somewhere to put it. DUMMY_.STR is 320 sectors of padding named, helpfully,
# after its own purpose: blank but for a sector of XA silence every 32nd, which
# we overwrite too in order to get one contiguous run.
DST_LBA = 31133
RECLAIM = range(31132, 31452)  # DUMMY_.STR, per the ISO9660 table

# Monsbaiya's location table, in its own chunk at sector 4456. 38 entries of
# eight bytes, laid out as:
#
#     struct entry { u32 packed; u32 sector; };
#
# where packed is (sector_count << 23) | (destination & 0x7fffff), and the
# destination is always 0x80016000. Note the order: the count comes *first*.
# Reading it the other way round is very convincing -- the sectors still line up
# with real locations -- but it silently pairs every count with the wrong door.
#
# TABLE is the offset of entry 0's sector field within the chunk, so entry n's
# record begins four bytes before it. The chunk is a parameter because a clone
# carries its own copy of the table, and editing the clone's is how the two
# towns come to have different doors.
TABLE = 0x61E8
N_ENTRIES = 38

DOORS = {1: ("the house", 5071), 18: ("the monster shop", 6147), 19: ("Barry's shop", 6195)}

# The way back out. The house does not carry a table like Monsbaiya's -- it has
# a single standalone record, at offset 0x2b74 in its own chunk, which is RAM
# 0x80018b74 once loaded and LBA 5076 +0x374 on disc. Same layout as a table
# entry: packed 0x09816000 (19 sectors to 0x80016000), then sector 4444.
HOUSE_EXIT = 5071 * DATA + 0x2B74
HOUSE_EXIT_SECTOR = 4444

# That record is not the one the engine reads, though. Patching it alone and
# walking out still loaded sector 4444, and a search of live RAM found why:
# there is a second copy, in slus_006.14 at 0x800812f8, and that is the one the
# exit uses. It is the last entry of a descriptor table -- the records before it
# load other things to other addresses, and Shift-JIS text begins right after --
# so it is static, uncompressed, and editable on disc at LBA 193 +0x2f8.
#
# Both get redirected. The chunk-local copy may well be dead, but leaving the
# two disagreeing about where Monsbaiya lives is asking for a puzzling bug later.
SLUS_MONSBAIYA = 0x800812F8

# Scanning the whole disc for that same record -- 19 sectors to 0x80016000,
# sector 4444 -- finds 34 of them: one in slus and one in each interior's chunk,
# each at roughly 0x80018a00-0x80018c00 once loaded. So every interior carries
# its own way back to town.
#
# Whether the chunk-local ones are live is the open question. The house's was
# not: editing it alone changed nothing and the engine read slus instead. If
# they are all dead then slus is the single entrance and redirecting it replaces
# Monsbaiya outright; if Barry's is live, the two towns can coexist. One test
# settles it, and either answer is worth knowing.
SHOP_EXIT = 6200 * DATA + 0x2D8       # Barry's shop chunk, 0x80018ad8 loaded


def entry(door: int, chunk: int = 4444) -> int:
    """Stream offset of a door's 8-byte record in a Monsbaiya chunk."""
    return chunk * DATA + TABLE + door * 8 - 4


# ---------------------------------------------------------------------------
# Giving a warp its own destination
#
# When the warp kind is 12 the handler uses the descriptor's own record instead
# of the Monsbaiya fallback, so an entrance can be sent anywhere by editing one
# pointer. The house family has thirteen kind-12 descriptors: four entry paths
# times three upgrade levels, plus a group of three for a second room. Ten share
# the record 0x800d4360 = {20 sectors, 5071}, the main room; the 0x8006af64
# group names {7 sectors, 5125} instead and is left alone. Only the four
# location-1 entries are redirected, one per entry path, since missing one would
# leave a route that still reaches the real house.
#
# The record we substitute has to survive from disc to runtime, which is a
# stricter requirement than "reads as zero". The first attempt put it at
# 0x8007ac00, which is zero on disc but holds live variables once the game is
# running, so the descriptor found packed = 0, loaded zero sectors, and the
# house appeared with its artwork but no NPCs -- no chunk had been read at all.
#
# 0x80079800 is inside the same band as the stubs tools/trace_cd.py installs,
# and those demonstrably keep their contents: comparing a running session
# against the image shows the band byte-identical, while 0x8007ac00 differs.
# ---------------------------------------------------------------------------
DEST_RECORD = 0x80079800
HOUSE_DESCRIPTORS = (0x8006AED8, 0x8006AEEC, 0x8006AF28, 0x8006AFA0)
HOUSE_DEST = 0x800D4360          # {20 sectors -> 0x80016000, sector 5071}

# Halfwords +4 and +6 are where you materialise. The house's entrances land you
# at (144, 224), a spot inside the house, which in a town map is nowhere at all
# -- the load completed perfectly and the screen stayed black.
#
# The right numbers come from the house's *exit* descriptor, at 0x80018a84 in
# its chunk: kind 11, arriving at (384, 800). That is the game's own answer to
# "where do you stand when you appear in Monsbaiya from the house". Note kind 11
# rather than 12, which is why its +8 (pointing at the chunk-local record) is
# ignored in favour of the slus fallback.
ARRIVE = (384, 800)


# ---------------------------------------------------------------------------
# Two towns, chosen by which door you came out of
#
# The fallback is two instructions that build a constant:
#
#     8003bc58  lui   $v0, 0x8008
#     8003bc5c  addiu $v0, $v0, 0x12f8      ; the Monsbaiya record
#     8003bc60  j     8003bc6c
#     8003bc64  sw    $v0, 4($a0)           ; delay slot, stores the choice
#
# Replacing the pair with a jump to a stub that returns to 8003bc60 leaves the
# store untouched, so all the stub has to do is leave the right pointer in $v0.
# $v0 and $v1 are both dead here -- 8003bc6c overwrites $v0 immediately after
# the store, and $v1 was only the kind comparison -- and $at is the assembler's.
#
# The location id has already been written by this point, back at 8003bb54 in
# the same function, so it reflects the door being used right now.
# ---------------------------------------------------------------------------
GATE_STUB = 0x80079810
GATE_HOOK = 0x8003BC58
GATE_RESUME = 0x8003BC60
GATE_ORIGINAL = (0x3C028008, 0x244212F8)     # lui $v0,0x8008 ; addiu $v0,$v0,0x12f8

AT, V0, V1, SP = 1, 2, 3, 29

# A location id is a *building type*. The town keeps a plot table at 0x800133a4
# -- 34 plots, two bytes each, {type, upgradingType} -- saying what stands on
# each lot, and the id a door announces is the type of the building it belongs
# to. Names below are cjaz's BuildingType enum, plus the two shops we watched
# announce their own ids (his enum skips them: they are always present, so
# nothing ever needed to ask whether they were built).
#
# Types 0x2c-0x33 are vacant lots, one per buildable plot; a lot becomes its
# building's type when built, having passed through a stage-1 type on the way
# (0x04 fountain, 0x0f hospital, 0x11 temple).
EXITS = {
    1: "Koh's house", 2: "Koh's house, upgraded once",
    3: "Koh's house, upgraded twice", 5: "the fountain", 6: "the casino",
    7: "the library", 8: "the gym", 9: "the arcade", 10: "the racetrack",
    11: "the theater", 12: "the alley", 16: "the hospital", 18: "the temple",
    20: "the monster shop", 21: "Barry's shop",
    37: "monster hut 1", 38: "monster hut 2", 39: "monster hut 3",
    40: "monster hut 4", 41: "monster hut 5",
}


def exit_name(gate: int) -> str:
    return EXITS.get(gate, f"the building announcing location id {gate}")


def _sb(rt, base, off):
    return (0x28 << 26) | (base << 21) | (rt << 16) | (off & 0xFFFF)


def _lw(rt, base, off):
    return (0x23 << 26) | (base << 21) | (rt << 16) | (off & 0xFFFF)


def _beq(rs, rt, words):
    return (0x04 << 26) | (rs << 21) | (rt << 16) | (words & 0xFFFF)


def _lbu(rt, base, off):
    return (0x24 << 26) | (base << 21) | (rt << 16) | (off & 0xFFFF)


def _bne(rs, rt, words):
    return (0x05 << 26) | (rs << 21) | (rt << 16) | (words & 0xFFFF)


def _j(addr):
    return (0x02 << 26) | ((addr & 0x0FFFFFFF) >> 2)


def _split(addr):
    lo = addr & 0xFFFF
    return (addr >> 16) + (1 if lo & 0x8000 else 0), lo


def build_gate(gate: int) -> list[int]:
    hi, lo = _split(DEST_RECORD)
    fhi, flo = _split(TWIN_FLAG)
    return [
        lui(V0, 0x8008),
        addiu(V0, V0, 0x12F8),       # default: the original town
        lui(AT, fhi),
        _sb(ZERO, AT, flo),          # and by default we are not in the twin
        lui(V1, 0x800D),
        _lbu(V1, V1, 0x381A),        # the id of the door we just used
        addiu(AT, ZERO, gate),       # covers the load delay on $v1
        _bne(V1, AT, 6),             # not our door: keep the default
        0,
        lui(V0, hi),
        addiu(V0, V0, lo),           # our clone
        lui(AT, fhi),
        addiu(V1, ZERO, 1),
        _sb(V1, AT, flo),            # remember it, for the asset remap
        _j(GATE_RESUME),
        0,
    ]


def apply_gate(img: Image, dst: int, packed: int, gate: int, log) -> None:
    rec = slus(DEST_RECORD)
    if any(img.read(rec, 8)):
        raise SystemExit(f"scratch at 0x{DEST_RECORD:08x} is not free")
    img.write(rec, struct.pack("<II", packed, dst))
    log(f"  record        0x{DEST_RECORD:08x}  LBA {rec // DATA} +0x{rec % DATA:03x}  "
        f"{packed >> 23} sectors -> 0x{packed & 0x7FFFFF:x}, sector {dst}")

    code = build_gate(gate)
    stub = slus(GATE_STUB)
    payload = b"".join(struct.pack("<I", c) for c in code)
    if img.read(stub, len(payload)) not in (bytes(len(payload)), payload):
        raise SystemExit(f"scratch at 0x{GATE_STUB:08x} is not free")
    img.write(stub, payload)
    log(f"  stub          0x{GATE_STUB:08x}  LBA {stub // DATA} +0x{stub % DATA:03x}  "
        f"{len(code)} instructions")

    for k, was in enumerate(GATE_ORIGINAL):
        img.patch_u32(slus(GATE_HOOK) + k * 4, was, _j(GATE_STUB) if k == 0 else 0)
    log(f"  hook          0x{GATE_HOOK:08x}  the exit fallback now asks the stub")
    log(f"  gate          leaving {EXITS.get(gate, gate)} (location {gate}) "
        f"arrives at sector {dst}; every other exit is unchanged")


# ---------------------------------------------------------------------------
# Remapping shared assets for the twin
#
# Some of what a town loads is named neither by its chunk nor by any table the
# clone owns. The map comes from a global table of {packed, sector} records at
# LBA 3245 +0x494, shared by both towns, so editing it there would move the map
# for both. Records are named by absolute address from 27 call sites rather than
# indexed, so there is no id to intercept either.
#
# The general answer is to intercept the sector on its way to the drive. Every
# read passes one point where the LBA sits in a register:
#
#     8003eaa4  lw    $a0, 4($s1)      ; the LBA, from the request record
#     8003eaa8  jal   8006124c         ; CdIntToPos
#     8003eaac  addiu $a1, $sp, 0x10   ; delay slot
#     8003eab0  addiu $a0, $zero, 2    ; CdlSetloc
#
# Both instructions have to be displaced, because a jump's delay slot would
# otherwise hold the `jal` and two branches in a row are undefined on the R3000.
# The stub performs both itself and resumes at 8003eab0.
#
# The remaining problem is knowing *which* town is asking. The clone is a
# byte-for-byte copy, so the chunk signature at 0x80016008 reads 0x8001a484 for
# both and cannot distinguish them. But the gate stub already knows -- it is the
# code that chooses the clone -- so it records the answer in a flag byte, and
# the remap consults that. The table is data rather than unrolled code, so
# adding an asset later costs eight bytes and no reassembly.
# ---------------------------------------------------------------------------
# The hole in slus_006.14 runs 0x80079550-0x80079958, zero on disc and stable at
# runtime. tools/trace_cd.py takes the front of it, so everything here starts
# past 0x800797f8 and the two can be applied together.
TWIN_FLAG = 0x80079808           # one byte, between the record and the gate stub
WITNESS = 0x8007980C             # the last sector actually substituted
REMAP_STUB = 0x80079860          # past the gate stub's sixteen instructions
REMAP_TABLE = 0x800798D0         # {from, to} pairs, terminated by a zero
REMAP_LIMIT = 0x80079958

READ_HOOK = 0x8003EAA4
READ_RESUME = 0x8003EAB0

MAP_SRC, MAP_COUNT, MAP_READ = 4463, 64, 16
MAP_DST = 31286


def build_remap(stolen: tuple[int, int]) -> list[int]:
    """Consult the table if the twin is loading, then do the work we displaced.

    $at, $v0 and $v1 are all dead here -- the next thing to set $v0 is the
    CdIntToPos call below, and $v1 is untouched until well after. $s1 and $sp
    carry the caller's state and are left alone. Every load is separated from
    its first use, for the R3000's load delay slot.
    """
    fhi, flo = _split(TWIN_FLAG)
    thi, tlo = _split(REMAP_TABLE)
    whi, wlo = _split(WITNESS)
    loop, done = REMAP_STUB + 7 * 4, REMAP_STUB + 21 * 4
    return [
        stolen[0],                   # lw    $a0, 4($s1)   -- the LBA
        lui(AT, fhi),                # covers the load delay on $a0
        _lbu(V0, AT, flo),
        lui(V1, thi),                # covers the load delay on $v0
        addiu(V1, V1, tlo),
        _beq(V0, ZERO, 15),          # not the twin: leave the sector alone
        0,
        _lw(V0, V1, 0),              # loop: the sector this entry replaces
        0,
        _beq(V0, ZERO, 11),          # a zero ends the table
        0,
        _bne(V0, A0, 6),             # not this one
        0,
        _lw(A0, V1, 4),              # the replacement
        lui(AT, whi),                # covers the load delay on $a0
        sw(A0, AT, wlo),             # leave a witness, since the read tracer
        _j(done),                    # only ever sees the original request
        0,
        addiu(V1, V1, 8),            # next:
        _j(loop),
        0,
        addiu(A1, SP, 0x10),         # done: $a1 = $sp + 0x10, the delay slot we lost
        stolen[1],                   # jal   CdIntToPos
        0,
        _j(READ_RESUME),
        0,
    ]


def apply_remap(img: Image, entries, log) -> None:
    stolen = (img.read_u32(slus(READ_HOOK)), img.read_u32(slus(READ_HOOK + 4)))
    if stolen[0] != 0x8E240004 or stolen[1] >> 26 != 0x03:
        raise SystemExit(f"unexpected code at the read hook: {stolen[0]:08x} "
                         f"{stolen[1]:08x}")

    code = build_remap(stolen)
    stub = slus(REMAP_STUB)
    payload = b"".join(struct.pack("<I", c) for c in code)
    if img.read(stub, len(payload)) not in (bytes(len(payload)), payload):
        raise SystemExit(f"scratch at 0x{REMAP_STUB:08x} is not free")
    img.write(stub, payload)

    table = slus(REMAP_TABLE)
    data = b"".join(struct.pack("<II", a, b) for a, b in entries) + b"\0" * 8
    if REMAP_TABLE + len(data) > REMAP_LIMIT:
        raise SystemExit(f"{len(entries)} remap entries overrun the scratch space")
    if img.read(table, len(data)) not in (bytes(len(data)), data):
        raise SystemExit(f"scratch at 0x{REMAP_TABLE:08x} is not free")
    img.write(table, data)

    img.patch_u32(slus(READ_HOOK), stolen[0], _j(REMAP_STUB))
    img.patch_u32(slus(READ_HOOK + 4), stolen[1], 0)

    log(f"  remap stub    0x{REMAP_STUB:08x}  LBA {stub // DATA} +0x{stub % DATA:03x}  "
        f"{len(code)} instructions")
    log(f"  remap hook    0x{READ_HOOK:08x}  every disc read now passes the table")
    for a, b in entries:
        log(f"  remap         sector {a} -> {b} while the twin is loading")


def apply_map(img: Image, town: int, dst: int, log):
    """Clone the town's map and return the remap entries that redirect it."""
    if not (dst + MAP_COUNT <= town or dst >= town + 103):
        raise SystemExit(f"the map clone at {dst} overlaps the town clone at {town}")
    selftest(img, MAP_SRC, MAP_COUNT)
    for i in range(MAP_COUNT):
        lba = dst + i
        payload = img.buf[lba * SECTOR + HDR:lba * SECTOR + HDR + DATA]
        if any(payload) and not (lba in RECLAIM and set(payload) <= {0x00, 0x0C}):
            raise SystemExit(f"LBA {lba} holds real content -- refusing to overwrite it")

    for i in range(MAP_COUNT):
        payload = bytes(img.buf[(MAP_SRC + i) * SECTOR + HDR:
                                (MAP_SRC + i) * SECTOR + HDR + DATA])
        img.buf[(dst + i) * SECTOR:(dst + i + 1) * SECTOR] = make_data_sector(dst + i, payload)
    log(f"  map           sectors {MAP_SRC}-{MAP_SRC + MAP_COUNT - 1} -> "
        f"{dst}-{dst + MAP_COUNT - 1}")

    # The town's map arrives as four consecutive 16-sector reads, so all four
    # starting sectors need an entry, not just the one the table names.
    return ([(MAP_SRC + i * MAP_READ, dst + i * MAP_READ)
             for i in range(MAP_COUNT // MAP_READ)],
            (dst, MAP_SRC, MAP_COUNT))


def apply_warp(img: Image, dst: int, packed: int, location: int, log) -> None:
    rec = slus(DEST_RECORD)
    if any(img.read(rec, 8)):
        raise SystemExit(f"scratch at 0x{DEST_RECORD:08x} is not free")
    img.write(rec, struct.pack("<II", packed, dst))
    log(f"  record        0x{DEST_RECORD:08x}  LBA {rec // DATA} +0x{rec % DATA:03x}  "
        f"{packed >> 23} sectors -> 0x{packed & 0x7FFFFF:x}, sector {dst}")
    for a in HOUSE_DESCRIPTORS:
        img.patch_u32(slus(a) + 8, HOUSE_DEST, DEST_RECORD)
        log(f"  descriptor    0x{a:08x} +8: 0x{HOUSE_DEST:08x} -> 0x{DEST_RECORD:08x}")
        # The chunk is only half of it. A kind-12 warp also announces a location
        # id, which is what picks the artwork -- leaving it at 1 loaded the town's
        # code and dialogue underneath the house's geometry, with no NPCs and no
        # doors that line up. Record `location` is one of the four whose asset id
        # is 0, Monsbaiya's map.
        was = struct.unpack("<H", img.read(slus(a) + 2, 2))[0]
        if was not in (1, location):
            raise SystemExit(f"0x{a:08x} announces location {was}, expected 1")
        img.write(slus(a) + 2, struct.pack("<H", location))
        log(f"  descriptor    0x{a:08x} +2: location {was} -> {location}")

        old = struct.unpack("<HH", img.read(slus(a) + 4, 4))
        img.write(slus(a) + 4, struct.pack("<HH", *ARRIVE))
        log(f"  descriptor    0x{a:08x} +4: arrive {old} -> {ARRIVE}")


def targets(t: dict) -> list[tuple[int, str, int]]:
    """Every record to redirect: stream offset, description, expected sector."""
    if t.get("warp") or t.get("gate"):
        return []
    if t.get("exit") == "slus":
        return [
            (SLUS_MONSBAIYA - SLUS_DELTA, "the exit record in slus_006.14",
             HOUSE_EXIT_SECTOR),
            (HOUSE_EXIT, "the copy in the house's own chunk", HOUSE_EXIT_SECTOR),
        ]
    if t.get("exit") == "shop":
        return [(SHOP_EXIT, "Barry's shop's exit to Monsbaiya", HOUSE_EXIT_SECTOR)]
    name, sector = DOORS[t["door"]]
    return [(entry(t["door"]), f"door entry {t['door']}, {name}", sector)]


# ---------------------------------------------------------------------------
# Giving the twin town a shop of its own
#
# The clone owns its 19-sector chunk, and the chunk holds the location table, so
# the twin's doors can be changed without touching the original's. That is the
# whole of what a clone independently owns -- the map and the dialogue are still
# read from their original sectors -- but it is enough for the towns to differ
# in a way you can walk into.
#
# Barry's shop is the natural first difference because its stock is not data but
# code: a routine at chunk offset 0x5c4 that writes a NUL-terminated list of
# 4-byte {id, category} entries. Clone the six-sector chunk, point the *twin's*
# door 19 at the copy, and rewrite the copy's routine. Two weapon shops, same
# building, different stock, neither aware of the other.
#
# Pointing a door at a different *kind* of place would not work: the door also
# announces a location id, and the engine hands control to a chunk-relative
# entry point chosen by that id (see docs/FINDINGS.md). A shop clone reached
# through the shop's own door keeps every one of those invariants, so the only
# variable is again which sector it came from.
#
# The exit stays untouched, which matters: leaving the twin's shop announces
# location 21 exactly as the original's does, and the gate stub sends 21 to the
# clone -- so you come out in the town you went in from.
# ---------------------------------------------------------------------------
SHOP_SRC, SHOP_COUNT, SHOP_DOOR = 6195, 6, 19
SHOP_DST = 31240

BUILDER = 0x5C4          # the stock routine's offset inside the shop's chunk
BUILDER_SLOTS = 22
BUILDER_HEAD = (0x00801021, 0x3C038002, 0x24678A98)

CAT_EGG = 0x12

# Entry 0 is the "Pay" pseudo-row that renders the menu header, not an item.
# The rest are eggs Barry does not stock, cheapest first, so the two shops have
# nothing in common. Names and prices are the game's own -- see the egg array in
# patch.py; the Ultimate egg at the end is the most expensive item in the game.
TWIN_STOCK = [
    (0x01, 0x16, "Pay (header row)"),
    (0x15, CAT_EGG, "PULUNPA egg"),
    (0x06, CAT_EGG, "FLAME egg"),
    (0x17, CAT_EGG, "NOISE egg"),
    (0x18, CAT_EGG, "U-BOAT egg"),
    (0x12, CAT_EGG, "UNICORN egg"),
    (0x14, CAT_EGG, "BLOCK egg"),
    (0x0A, CAT_EGG, "SNOWMAN egg"),
    (0x04, CAT_EGG, "KID egg"),
    (0x01, CAT_EGG, "Ultimate egg"),
]


def build_stock(stock) -> bytes:
    """The same one-immediate-one-store form patch.py uses for Barry."""
    code = []
    for i, (iid, cat, _) in enumerate(stock):
        word = (cat << 8) | iid
        if word >= 0x8000:
            raise SystemExit(f"entry {i}: immediate 0x{word:04x} would sign-extend")
        code += [addiu(V1, ZERO, word), sw(V1, A0, i * 4)]
    code += [JR_RA, sw(ZERO, A0, len(stock) * 4)]
    if len(code) > BUILDER_SLOTS:
        raise SystemExit(f"stock needs {len(code)} slots, budget is {BUILDER_SLOTS}")
    return b"".join(struct.pack("<I", c) for c in code)


def apply_shop(img: Image, town: int, dst: int, log) -> set[int]:
    """Clone Barry's shop, give the twin town's door 19 the copy, restock it.

    Returns the sectors deliberately left differing from their source, so the
    caller's clone-integrity check can account for them.
    """
    if not (dst + SHOP_COUNT <= town or dst >= town + 103):
        raise SystemExit(f"the shop clone at {dst} overlaps the town clone at {town}")

    selftest(img, SHOP_SRC, SHOP_COUNT)
    for i in range(SHOP_COUNT):
        lba = dst + i
        payload = img.buf[lba * SECTOR + HDR:lba * SECTOR + HDR + DATA]
        if any(payload) and not (lba in RECLAIM and set(payload) <= {0x00, 0x0C}):
            raise SystemExit(f"LBA {lba} holds real content -- refusing to overwrite it")

    source = [bytes(img.buf[(SHOP_SRC + i) * SECTOR + HDR:
                            (SHOP_SRC + i) * SECTOR + HDR + DATA])
              for i in range(SHOP_COUNT)]
    for i, payload in enumerate(source):
        img.buf[(dst + i) * SECTOR:(dst + i + 1) * SECTOR] = make_data_sector(dst + i, payload)
    log(f"  shop clone    sectors {SHOP_SRC}-{SHOP_SRC + SHOP_COUNT - 1} -> "
        f"{dst}-{dst + SHOP_COUNT - 1}")

    rec = entry(SHOP_DOOR, town)
    was_packed, was_sector = struct.unpack("<II", img.read(rec, 8))
    if was_sector not in (SHOP_SRC, dst):
        raise SystemExit(f"the twin's door {SHOP_DOOR} points at sector {was_sector}, "
                         f"expected {SHOP_SRC}. Wrong image?")
    img.write(rec, struct.pack("<II", was_packed, dst))
    log(f"  twin's door   {SHOP_DOOR} ({DOORS[SHOP_DOOR][0]}): sector {was_sector} -> {dst}"
        f"   LBA {rec // DATA} +0x{rec % DATA:03x}")

    off = dst * DATA + BUILDER
    if off % DATA + BUILDER_SLOTS * 4 > DATA:
        raise SystemExit("the stock routine straddles a sector boundary")
    payload = build_stock(TWIN_STOCK)
    head = struct.unpack("<3I", img.read(off, 12))
    if head != BUILDER_HEAD:
        raise SystemExit(f"unexpected bytes at the cloned stock routine: "
                         f"{[f'{x:08x}' for x in head]}")
    img.write(off, payload)
    log(f"  stock routine LBA {off // DATA} +0x{off % DATA:03x}  "
        f"{len(payload) // 4}/{BUILDER_SLOTS} slots, {len(TWIN_STOCK) - 1} items")

    # Eggs sell for far more than the flat 100G they cost, which is harmless
    # while no shop sells them and an unlimited money loop the moment one does.
    apply_egg_prices(img, log)

    # The shop clone must equal its source everywhere except the stock routine.
    for i, want in enumerate(source):
        got = bytes(img.buf[(dst + i) * SECTOR + HDR:(dst + i) * SECTOR + HDR + DATA])
        if i == 0:
            lo, hi = BUILDER, BUILDER + len(payload)
            got = got[:lo] + want[lo:hi] + got[hi:]
        if got != want:
            raise SystemExit(f"shop clone sector {dst + i} differs from its source "
                             f"outside the stock routine")

    return {town + TABLE // DATA}


# ---------------------------------------------------------------------------
# Giving the twin town its own dialogue
#
# Just before the location table, at chunk offset 0x6178, sits a second table:
# fourteen 8-byte entries whose first word is an absolute sector and whose
# second is zero. They are not {packed, sector} records -- they are boundaries.
# Entry n names a block running from its own sector up to the next entry's, and
# every read the tracer saw confirms it:
#
#     entry  0   4513, next 4516   ->  read 4513 x3
#     entry  8   4538, next 4539   ->  read 4538 x1
#     entry 10   4540, next 4543   ->  read 4540 x3
#     entry 11   4543, next 4547   ->  read 4543 x4
#
# So the town carries a directory of its own dialogue, and the directory is in
# the chunk -- which the clone owns. Copy the blocks into padding, add the
# offset to the twin's fourteen entries, and the two towns stop sharing words.
#
# The text itself is plain full-width Shift-JIS on disc, uncompressed, so it can
# simply be edited in place. Replacements must not change the byte count, since
# nothing here is length-prefixed; pad with the full-width space instead.
# ---------------------------------------------------------------------------
DIRECTORY = 0x6178
DIR_ENTRIES = 14

DIALOGUE_DST = 31250

# What the twin says by default. The town names itself in three places, and
# "Twinbaiya" is the same length, so renaming it costs nothing. The rest are
# whole lines, chosen so the twin acknowledges what it is; use --lines to see
# every line with its budget, since a replacement can shorten but never grow.
DEFAULT_RENAMES = [
    ("Monsbaiya", "Twinbaiya"),
    ("They say children's songs are", "They say this town is a copy"),
    ("messages from God, I wonder", "of somewhere else. I wonder"),
    ("This place is so nice and ", "This place is new and "),
    ("peaceful.", "empty."),
    ("Did you hear the news ?", "You look lost, friend."),
]

# Marker lines for --stamp, longest first. Each block opens with a real line of
# dialogue, but they vary from six characters to forty, so take the longest that
# fits and pad the rest out with spaces.
STAMPS = ["This is the twin town.", "The twin town.", "Twin town.", "Twin."]

_FULLWIDTH = {" ": "\u3000", "'": "\u2019"}


def fw(s: str) -> bytes:
    """ASCII to the full-width Shift-JIS the game's text uses."""
    return "".join(_FULLWIDTH.get(c, chr(0xFF00 + ord(c) - 0x20))
                   for c in s).encode("shift_jis")


def text_runs(blob: bytes, minlen: int = 6):
    """Offsets and lengths of full-width Shift-JIS runs, in bytes."""
    i = 0
    while i + 1 < len(blob):
        if 0x8140 <= (blob[i] << 8 | blob[i + 1]) <= 0x9FFF:
            j = i
            while j + 1 < len(blob) and 0x8140 <= (blob[j] << 8 | blob[j + 1]) <= 0x9FFF:
                j += 2
            if (j - i) // 2 >= minlen:
                yield i, j - i
            i = j
        else:
            i += 1


def clone_of(regions, sector: int):
    """Where a source sector ended up, in every clone that covers it."""
    return [base + (sector - src) for base, src, count in regions
            if src <= sector < src + count]


def apply_text(img: Image, secs, regions, renames, stamp: bool, log) -> None:
    """Edit the twin's copies of the dialogue, wherever they ended up.

    The town's words arrive twice. The directory blocks are read individually to
    0x8001dcd0, and the whole region is *also* pulled in by the bulk read that
    fetches the map -- sectors 4511-4526 of it are the same dialogue. The bulk
    copy is the one NPCs actually speak from: the staging buffer at 0x80126804
    holds block 0's text at +0x1009, which is 4513's offset within that read,
    and the individual loads all target one address so only the last survives.

    Editing only the directory clone therefore changes nothing you can hear,
    which is exactly what the first attempt did. So every clone gets the edit.
    """
    for old, new in renames:
        a, b = fw(old), fw(new)
        if len(b) > len(a):
            raise SystemExit(f"{new!r} is longer than {old!r}; the text is not "
                             "length-prefixed, so it cannot grow")
        b += fw(" ") * ((len(a) - len(b)) // 2)
        n = 0
        for lo, hi in zip(secs, secs[1:]):
            for base in clone_of(regions, lo):
                start, size = base * DATA, (hi - lo) * DATA
                blob = img.read(start, size)
                i = blob.find(a)
                while i >= 0:
                    img.write(start + i, b)
                    n += 1
                    i = blob.find(a, i + 1)
        log(f"  renamed       {old!r} -> {new!r} in {n} place(s)")

    if stamp:
        # Every line, not just the first of each block. Stamping only openings
        # changed 17 lines out of some hundreds, and the twin read out its
        # ordinary dialogue because nobody happened to speak a stamped one --
        # which looked exactly like the redirect having failed.
        n = 0
        for lo, hi in zip(secs, secs[1:]):
            for base in clone_of(regions, lo):
                start, size = base * DATA, (hi - lo) * DATA
                for off, length in list(text_runs(img.read(start, size))):
                    marker = next((fw(m) for m in STAMPS if len(fw(m)) <= length), None)
                    if marker is None:
                        marker = fw(STAMPS[-1])[:length]
                    img.write(start + off, marker + fw(" ") * ((length - len(marker)) // 2))
                    n += 1
        log(f"  stamped       {n} line(s) of the twin's dialogue")


def apply_dialogue(img: Image, town: int, dst: int, log):
    """Clone the town's dialogue blocks and point the twin's directory at them."""
    table = town * DATA + DIRECTORY
    secs = [struct.unpack("<I", img.read(table + i * 8, 4))[0] for i in range(DIR_ENTRIES)]
    if secs != sorted(secs) or not (4400 < secs[0] < 4600):
        raise SystemExit(f"the twin's directory does not look right: {secs}")

    src, span = secs[0], secs[-1] - secs[0] + 1
    if not (dst + span <= town or dst >= town + 103):
        raise SystemExit(f"the dialogue clone at {dst} overlaps the town clone at {town}")
    selftest(img, src, span)
    for i in range(span):
        lba = dst + i
        payload = img.buf[lba * SECTOR + HDR:lba * SECTOR + HDR + DATA]
        if any(payload) and not (lba in RECLAIM and set(payload) <= {0x00, 0x0C}):
            raise SystemExit(f"LBA {lba} holds real content -- refusing to overwrite it")

    source = [bytes(img.buf[(src + i) * SECTOR + HDR:(src + i) * SECTOR + HDR + DATA])
              for i in range(span)]
    for i, payload in enumerate(source):
        img.buf[(dst + i) * SECTOR:(dst + i + 1) * SECTOR] = make_data_sector(dst + i, payload)
    log(f"  dialogue      sectors {src}-{src + span - 1} -> {dst}-{dst + span - 1}")

    shift = dst - src
    for i, s in enumerate(secs):
        img.write(table + i * 8, struct.pack("<I", s + shift))
    log(f"  directory     {DIR_ENTRIES} entries at LBA {table // DATA} "
        f"+0x{table % DATA:03x}, each moved {shift:+}")

    return {town + DIRECTORY // DATA}, secs, (dst, src, span)


SLUS_DELTA = 0x80020800          # ram address -> payload-stream offset


def slus(ram_addr: int) -> int:
    return ram_addr - SLUS_DELTA



def bcd(n: int) -> int:
    return ((n // 10) << 4) | (n % 10)


def make_data_sector(lba: int, payload: bytes) -> bytes:
    """Build a complete Mode 2 Form 1 data sector from scratch."""
    if len(payload) != DATA:
        raise ValueError(f"payload is {len(payload)} bytes, expected {DATA}")
    total = lba + 150  # sector addresses are stored as MSF, offset by 2 seconds
    head = (b"\x00" + b"\xff" * 10 + b"\x00"
            + bytes([bcd(total // (75 * 60)), bcd((total // 75) % 60), bcd(total % 75), 2])
            + bytes([0, 0, 0x08, 0]) * 2)  # file 0, channel 0, submode = data
    sec = bytearray(head + payload + b"\x00" * 0x118)
    regen_sector(sec)
    return bytes(sec)


def selftest(img: Image, src: int, span: int) -> None:
    """Rebuild real sectors from their own payloads and require identical bytes.

    Sector synthesis touches the sync pattern, the MSF address, the submode and
    both error-correction layers. Any one of them being wrong would produce a
    disc that looks fine until the drive refuses to read it, so this proves all
    of it against sectors the game already ships.
    """
    for lba in (src, src + span // 2, src + span - 1):
        want = bytes(img.buf[lba * SECTOR:(lba + 1) * SECTOR])
        got = make_data_sector(lba, want[HDR:HDR + DATA])
        if got != want:
            bad = next(i for i in range(SECTOR) if got[i] != want[i])
            raise SystemExit(
                f"sector synthesis self-test failed on LBA {lba}, first differing "
                f"byte at 0x{bad:x}. Refusing to write."
            )


def show_lines(img: Image, chunk: int = 4444) -> None:
    """List the town's dialogue, so it can be rewritten line by line.

    Authoring is done with --rename, which matches on the original text and
    pads the replacement out to the same byte count. The budget column is what
    you have to work with: nothing here is length-prefixed, so a line can be
    shortened but never grown.
    """
    secs = [struct.unpack("<I", img.read(chunk * DATA + DIRECTORY + i * 8, 4))[0]
            for i in range(DIR_ENTRIES)]
    for b, (lo, hi) in enumerate(zip(secs, secs[1:])):
        blob = img.read(lo * DATA, (hi - lo) * DATA)
        runs = list(text_runs(blob))
        print(f"\n  block {b}, sectors {lo}-{hi - 1}, {len(runs)} lines")
        for r, (off, length) in enumerate(runs):
            text = blob[off:off + length].decode("shift_jis", "replace")
            print(f"    [{r:>3}] {length // 2:>3}  {text}")


def show_table(img: Image) -> None:
    print(f"Monsbaiya's location table -- {N_ENTRIES} entries at LBA "
          f"{entry(0) // DATA}, offset 0x{entry(0) % DATA:x}\n")
    print(f"  {'door':>4}  {'sector':>7}  {'packed':>10}  {'sectors':>7}  destination   what")
    for i in range(N_ENTRIES):
        packed, sec = struct.unpack("<II", img.read(entry(i), 8))
        name = DOORS.get(i, ("", 0))[0]
        print(f"  {i:>4}  {sec:>7}  0x{packed:08x}  {packed >> 23:>7}  "
              f"0x{0x80000000 | (packed & 0x7FFFFF):08x}    {name}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("image", help="vanilla or already-patched .bin")
    ap.add_argument("-o", "--output", help="output .bin (default: alongside input)")
    ap.add_argument("--template", choices=sorted(TEMPLATES), default="house",
                    help="which location to clone (default: house)")
    ap.add_argument("--at", type=int, default=DST_LBA, help="sector to place the clone at")
    ap.add_argument("--gate-id", type=int, metavar="N",
                    help="location id of the building whose exit leads to the "
                         "twin (default: Barry's shop, 21)")
    ap.add_argument("--own-shop", action="store_true",
                    help="also clone Barry's shop, point the twin town's door at "
                         "the copy, and give it its own stock")
    ap.add_argument("--shop-at", type=int, default=SHOP_DST,
                    help="sector to place the shop clone at")
    ap.add_argument("--own-dialogue", action="store_true",
                    help="clone the town's dialogue and point the twin's directory "
                         "at the copy, so the two towns stop sharing words")
    ap.add_argument("--dialogue-at", type=int, default=DIALOGUE_DST,
                    help="sector to place the dialogue clone at")
    ap.add_argument("--rename", action="append", metavar="OLD=NEW", default=[],
                    help="length-preserving text substitution in the twin's "
                         "dialogue (default: Monsbaiya=Twinbaiya)")
    ap.add_argument("--own-map", action="store_true",
                    help="clone the town's map and redirect the twin's reads to "
                         "it, via the asset remap hook")
    ap.add_argument("--map-at", type=int, default=MAP_DST,
                    help="sector to place the map clone at")
    ap.add_argument("--stamp", action="store_true",
                    help="overwrite the opening line of every dialogue block, so "
                         "the twin is obvious whoever you talk to")
    ap.add_argument("--lines", action="store_true",
                    help="print the town's dialogue with its length budget and exit")
    ap.add_argument("--list", action="store_true", help="print the location table and exit")
    args = ap.parse_args(argv)

    img = Image(open(args.image, "rb").read())
    if args.list:
        show_table(img)
        return 0
    if args.lines:
        show_lines(img)
        return 0

    t = dict(TEMPLATES[args.template])
    if args.gate_id is not None:
        if not t.get("gate"):
            raise SystemExit(f"--gate-id needs a template that uses the exit "
                             f"fallback; try --template twotowns")
        t["gate"] = args.gate_id
    src, span = t["lba"], t["span"]
    dst = args.at
    redirects = targets(t)

    print(f"source     {args.template}, sectors {src}-{src + span - 1} "
          f"({span} sectors, {span * DATA:,} bytes)")
    print(f"clone      sectors {dst}-{dst + span - 1}, inside DUMMY_.STR")
    for _, name, _ in redirects:
        print(f"door       {name}")
    if t.get("warp"):
        print("door       the house's entrance, via its own destination record")
    if t.get("gate"):
        print(f"door       leaving {exit_name(t['gate'])}, via the exit fallback")
    print()

    selftest(img, src, span)
    print("  sector synthesis verified against three real sectors")

    # The destination must be padding. Anything else and we would be destroying
    # content, which no amount of correct checksums would make acceptable.
    # Blank sectors qualify, and so do DUMMY_.STR's XA silence sectors, whose
    # payload is nothing but 0x00 and 0x0c filler.
    silence = 0
    for i in range(span):
        lba = dst + i
        payload = img.buf[lba * SECTOR + HDR:lba * SECTOR + HDR + DATA]
        if not any(payload):
            continue
        if lba in RECLAIM and set(payload) <= {0x00, 0x0C}:
            silence += 1
            continue
        raise SystemExit(f"LBA {lba} holds real content -- refusing to overwrite it")
    print(f"  destination sectors {dst}-{dst + span - 1} are padding "
          f"({span - silence} blank, {silence} XA silence)")

    # Snapshot the source before touching anything. For the monsbaiya template the
    # table edit below lands in sector 4456, inside the copied span, and the clone
    # must keep the original's table -- otherwise its own door would point at itself.
    source = [bytes(img.buf[(src + i) * SECTOR + HDR:(src + i) * SECTOR + HDR + DATA])
              for i in range(span)]
    for i, payload in enumerate(source):
        img.buf[(dst + i) * SECTOR:(dst + i + 1) * SECTOR] = make_data_sector(dst + i, payload)
    print(f"  copied {span} sectors")

    for rec, name, want in redirects:
        was_packed, was_sector = struct.unpack("<II", img.read(rec, 8))
        if was_sector not in (want, dst):
            raise SystemExit(f"{name} points at sector {was_sector}, "
                             f"expected {want}. Wrong image?")
        img.write(rec, struct.pack("<II", t["packed"], dst))
        print(f"  {name}: sector {was_sector} -> {dst}, "
              f"{was_packed >> 23} sectors -> {t['packed'] >> 23}")

    if t.get("warp"):
        apply_warp(img, dst, t["packed"], t["location"], print)
    if t.get("gate"):
        apply_gate(img, dst, t["packed"], t["gate"], print)

    exempt: set[int] = set()
    if args.own_shop:
        if src != 4444:
            raise SystemExit("--own-shop needs a Monsbaiya clone to put the shop in")
        exempt |= apply_shop(img, dst, args.shop_at, print)
    # The bulk read that fetches the map also carries the dialogue, and that is
    # the copy NPCs speak from -- so words and map are one job, not two.
    regions, secs = [], None
    if args.own_dialogue:
        if src != 4444:
            raise SystemExit("--own-dialogue needs a Monsbaiya clone to speak for")
        got, secs, region = apply_dialogue(img, dst, args.dialogue_at, print)
        exempt |= got
        regions.append(region)
    if args.own_map or args.own_dialogue:
        if not t.get("gate"):
            raise SystemExit("--own-map needs the gate, which is what knows "
                             "which town is loading")
        entries, region = apply_map(img, dst, args.map_at, print)
        regions.append(region)
        apply_remap(img, entries, print)
    if secs is not None:
        renames = [tuple(r.split("=", 1)) for r in args.rename] or DEFAULT_RENAMES
        apply_text(img, secs, regions, renames, args.stamp, print)

    img.finalize()

    # Verify: the clone must match the source as it was, and every sector we
    # touched must carry checksums consistent with its own contents.
    for i, payload in enumerate(source):
        if dst + i in exempt:
            continue
        if img.buf[(dst + i) * SECTOR + HDR:(dst + i) * SECTOR + HDR + DATA] != payload:
            raise SystemExit(f"clone sector {dst + i} does not match its source")
    bad = img.check_sectors()
    if bad:
        raise SystemExit(f"bad checksums on sectors: {bad}")
    note = f" ({len(exempt)} deliberately changed)" if exempt else ""
    print(f"  clone verified against the original{note}, checksums consistent")

    out = args.output or os.path.join(
        os.path.dirname(os.path.abspath(args.image)),
        os.path.splitext(os.path.basename(args.image))[0] + " [Relocated].bin")
    with open(out, "wb") as f:
        f.write(img.buf)
    write_cue(os.path.splitext(out)[0] + ".cue", os.path.basename(out))
    print(f"\nwrote {out}")
    print(f"      {os.path.splitext(out)[0]}.cue")
    if args.template == "house":
        print(f"\nWalk into {redirects[0][1]}. It should look and behave exactly as")
        print("before, but every byte of it now comes from the clone.")
        return 0

    via = (f"the door out of {exit_name(t['gate'])}" if t.get("gate") else
           redirects[0][1] if redirects else "the house's entrance")
    print(f"\nGo through {via}. "
          f"You should arrive in a second Monsbaiya, read from sector {dst}.")
    if args.own_shop:
        print("\nIts weapon shop is now a different shop from the one in the original")
        print(f"town, loaded from sector {args.shop_at}, stocking:")
        for _, _, label in TWIN_STOCK[1:]:
            print(f"  {label}")
    if args.own_dialogue:
        print(f"\nIts dialogue is its own too, from sector {args.dialogue_at}. Talk to")
        print("anyone in either town: the words are no longer shared.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
