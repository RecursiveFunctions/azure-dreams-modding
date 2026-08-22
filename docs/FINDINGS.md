# Findings

Reverse engineering notes for *Azure Dreams* (USA, `SLUS-00614`). Written down
because most of the effort here was locating things, not changing them.

## Coordinate systems

Three, and mixing them up wastes hours.

**Raw `.bin` offset.** Byte position in the file. A MODE2/2352 sector is `0x930`
bytes: `0x18` sync + subheader, `0x800` payload, `0x118` EDC/ECC.

**Stream offset.** Payload bytes with the overhead stripped out. This is the
space the game's data is actually laid out in, and the only sane one to record
addresses in. Convert with:

```
lba = stream // 0x800
bin = lba * 0x930 + 0x18 + (stream % 0x800)
```

A patch that ignores this will silently write into the error-correction area.

**RAM address.** Where code sits at runtime. For overlays this is
`0x8001xxxx`, and it is **ambiguous on its own** — see below.

## The disc is five files

```
sector     23  system.cnf
sector     24  slus_006.14        0x80000   main executable, 338 KB of text
sector  1,817  main/main.bin      0x275800
sector  3,077  town/town.bin      0x101b800
sector 12,310  dungeon/dungeon.bin 0x1a86800
```

(From the file table in [forestbelton/azure](https://github.com/forestbelton/azure),
a WIP decompilation.)

This explains two things that are otherwise just puzzling observations. Every
shop sector is in `town.bin`, which is where shops are. And the item tables
exist **twice** — once in `main.bin`, once in `dungeon.bin` — because town and
tower each need their own copy. Any item edit has to be mirrored, or the value
silently changes depending on where the player is standing.

## Overlays, and why RAM addresses aren't identifiers

The window `0x80010000`–`0x8001ffff` is reused. Barry's shop, the monster shop
and Fur's shop each load different code into the same addresses. "The routine at
`0x800165c4`" means nothing without saying which building you were standing in.

Worse, a single overlay is **stitched together from more than one disc region**.
The monster shop's overlay needs two different deltas:

```
code chunk    stream = (ram & 0x1fffff) + 0xbeb800
script chunk  stream = (ram & 0x1fffff) + 0xbed2e8
```

Both were derived by lifting a distinctive slice out of a live RAM snapshot and
searching the de-sectored disc stream for it. Deriving one and assuming it
covers the whole overlay produces patches that land in the wrong place.

**So quote disc sectors, not RAM addresses,** when telling someone else where to
look. Better still, quote a byte fingerprint they can search for.

## Barry's shop

Stock is not a table. It is emitted by a hardcoded routine at RAM `0x800165c4`,
stream `0x00c19dc4`, **sector 6195**, which writes a NUL-terminated list of
4-byte entries `{id, category, modifier, flags}` into a display buffer.

Vanilla spells out every byte with its own `sb`, which is why searching the disc
for a "Copper Sword + Medicinal Herb" byte pair finds nothing. Recompiling it as
one `addiu` immediate plus one `sw` per entry fits **10 entries in the same 22
instruction slots**:

```mips
addiu $v1, $zero, (category << 8) | id
sw    $v1, N($a0)
...
jr    $ra
sw    $zero, END($a0)        ; terminator, in the delay slot
```

The caller ignores `$v0` and the buffer has ample zeroed headroom after it, both
checked before extending the list.

Entry 0 must stay `{id=0x01, cat=0x16}` — it is the "Pay" pseudo-row that draws
the menu header, not a purchasable item.

## Items are uniform

Category descriptor table at RAM `0x80073414`, 20-byte stride, with the item
array pointer at `+0x0c`. Item records are also 20 bytes:

| Offset | Field |
| --- | --- |
| `+0x00` | id |
| `+0x04` | name pointer (Shift-JIS) |
| `+0x08` | description pointer |
| `+0x10` | **buy price** (u16) |
| `+0x12` | **sell price** (u16) |

Monster eggs are category `0x12` and are perfectly ordinary items — that is what
makes them stockable anywhere. Their array is stored twice (sectors 1883 in
`main.bin` and 14930 in `dungeon.bin`); editing one copy alone produces prices
that change depending on where you are.

There are 24 egg items, ids `0x01`–`0x18`. Egg `0x01` is "Ultimate"; from `0x02`
up they line up with monster ids (`0x02` KEWNE, `0x03` DRAGON … `0x18` U-BOAT).
The monster roster itself runs to 45 (`0x01`–`0x2d`), so the 21 monsters past
U-BOAT have no egg item and cannot be sold in a shop.

**Vanilla egg buy prices are a trap for anyone adding a shop.** Nearly every egg
costs 100G and sells for far more — Ultimate is 100G buy against 50,000G sell,
and 15 of the 24 are underpriced this way. It costs nothing in vanilla because
no vendor stocks eggs. The moment one does, it is an unlimited money loop, and
pricing only the handful a particular shop lists is not enough: the fix has to
cover every egg any patched shop can reach.

## The monster shop

### The dialogue is bytecode, not MIPS

The Master's menu ends in a 3-way jump table. In vanilla the buy arm at RAM
`0x8001a1b0` unconditionally prints "I must apologize, we haven't had enough top
monster hunters around…" and exits. There is no disabled buy path — the script
simply never goes there.

Opcodes used by this patch, all inferred from the working **sell** branch at
`0x8001a079`:

| Bytes | Meaning |
| --- | --- |
| `08` | open message window |
| `0a` | newline |
| `0b` | choice row marker |
| `11` | end page / wait for input |
| `15 <addr>` | gosub script subroutine |
| `16` | return |
| `17 <addr>` | jump |
| `1a <addr>…` | jump table, one address per choice |
| `2c <n>` | present `n` choices |
| `3e 0e <addr>` | branch if the last result was zero |
| `4c <addr>` | call native routine; result feeds `fd 0f` |
| `fd 0f` | print the last result as a number |

Text is full-width Shift-JIS: `A`–`Z` at `0x8260+`, `a`–`z` at `0x8281+`,
`0`–`9` at `0x824f+`, space `0x8140`, `.` `0x8144`, `[` `0x816d`, `]` `0x816e`,
`(` `0x8169`, `)` `0x816a`, `'` `0x8166`.

### The overlay already contained the whole buy side

This was the surprise. The monster shop overlay carries a complete buy-side
library that mirrors the sell side it sits next to, with **zero references from
anywhere**. It belongs to the furniture shop, which shares the overlay.

| Purpose | Sell | Buy |
| --- | --- | --- |
| price into `AMOUNT` | `0x8001679c` | `0x80016770` |
| gold | `0x80016818` (add) | `0x800167f0` (subtract) |
| affordability (`gold >= AMOUNT`) | — | `0x800167c8` |
| inventory transfer | `0x80016850` | `0x800170e8` |
| read `AMOUNT` | `0x80016840` | `0x80016840` |

Each pair is usually the same core routine called with a different mode flag:
`0x80016a4c(ctx, mode)` picks buy or sell price via vtable `+0x50` / `+0x6c`,
and `0x8001702c(list, mode)` adds or removes items.

Shop globals, all `lui 0x8002` plus a negative `addiu`:

```
0x800188d0  display list (entry 0 is the "Pay" header row)
0x800188c8  AMOUNT -- price in, and what fd0f prints
0x800189d0  menu object returned by the create call
0x800189d4  selected index
```

### What the patch does

1. **Sector 6147** — the list builder at `0x800165c4` emits category `0x18`
   (furniture). Three words change it: category `0x18` → `0x12`, `nop` out the
   `ori $v0, $v0, 0x80` that greys every row out, and lower the loop bound to
   24.
2. **Sector 6149** — `0x800170e8` is the buy-mode inventory wrapper, but it
   expects the list in `$a0` while script opcode `0x4c` calls with no arguments.
   It has no callers, so it is rewritten as a four-instruction nullary tail call
   supplying the list itself:

   ```mips
   lui   $a0, 0x8002
   move  $a1, $zero          ; mode 0 = buy
   j     0x8001702c          ; tail call, returns to the interpreter
   addiu $a0, $a0, -0x772c   ; = 0x800188d4, list + 4 (skip the header row)
   ```
3. **Sector 6158** — the buy arm is rewritten as a structural mirror of the sell
   branch: gosub the picker, quote the price, three-way confirm
   (`[I'll buy.] [My mistake.] [Not buying.]`), then check affordability,
   subtract gold, hand over the eggs. "My mistake" loops back to the picker,
   exactly as it does when selling. 276 bytes, into 524 bytes of freed apology
   text with no inbound jumps.

The affordability check matters: the gold subtraction is an unguarded `subu`, so
overspending without it would wrap gold to about four billion.

## How the game loads a place

Every disc read in the game passes through one chain. There are no special cases
for towns, shops, or dungeons -- a location is just data someone asked for.

```
game code            builds an 8-byte {sector, packed} descriptor
  |
load_chunk           0x80053cfc   ceil(length/2048), packs the destination
  |                               exactly one call site in the whole game
dispatcher           0x8003e4fc   43 call sites, command 0x06 = load
  |
enqueue              0x8003e39c   writes a 24-byte record
  |
request ring         0x80083968   32 entries, head index at gp+0x14d1
  |
pump                 0x8003e758   takes no arguments; drains the ring
  |
libcd CdRead         0x80063224
```

The useful place to watch is the pump, immediately before it calls `CdRead`,
where three saved registers hold everything at once:

```
8003eaa4  lw    $a0, 4($s1)   ; $s1 = ring entry, +4 is an integer LBA
8003eaa8  jal   0x8006124c    ; CdIntToPos, confirming it is an LBA
8003eae4  move  $a0, $s2      ; $s2 = sector count
8003eae8  move  $a1, $s0      ; $s0 = destination
8003eaec  jal   0x80063224    ; CdRead
```

`tools/trace_cd.py` hooks exactly this and logs each read. A second hook on the
dispatcher records the caller, since the pump runs asynchronously and its return
address says nothing about who wanted the data.

**A location load is recognisable by its destination.** Every location's code
and script chunk is read to `0x80016000`. Traced from a real playthrough:

| location | code sectors | count |
| --- | --- | --- |
| Monsbaiya | 4444-4462 | 19 |
| the house | 5071-5090 | 20 |
| Barry's shop | 6195-6200 | 6 |
| monster shop | 6147-6152 | 6 |
| tower entrance | 13660-13678 | 19 |
| dungeon floor | 14931-14949 | 19 |

Monsbaiya's full footprint is sectors 4444-4546 (103 sectors, about 210 KB);
sectors 4205-4214 load before *every* transition and are shared, not part of the
town. Leaving a shop reloads the town from disc in full, so the town is not
resident or privileged -- it is loaded on demand exactly like a shop.

Note that Monsbaiya lives in `TOWN.BIN` while the tower entrance lives in
`DUNGEON.BIN`, and both arrive through the same path distinguished only by an
absolute sector. **The loader is archive-agnostic**, so new location data can be
placed anywhere on the disc.

## The location table

Each location carries its own table of the places reachable from it, stored in
its code chunk. Monsbaiya's is 38 entries of 8 bytes at **disc sector 4456,
offset 0x1e8** (chunk offset 0x61e8):

```
struct entry {          // 8 bytes
    u32 sector;         // absolute LBA on the disc
    u32 packed;         // (length << 23) | (destination & 0x7fffff)
};                      // destination is always 0x80016000 for a location
```

Confirmed entries:

| index | offset in chunk | sector | what |
| --- | --- | --- | --- |
| 1 | 0x61f0 | 5071 | the house |
| 18 | 0x6278 | 6147 | monster shop |
| 19 | 0x6280 | 6195 | Barry's shop |

All 38 entries carry the same destination, `0x80016000`, so `packed` varies only
in its high bits: the low half is always `0x16000`. The high field looks like a
sector count and takes plausible values (3 to 20), but it does not agree with
what was actually read — Monsbaiya's entry says 8 where the trace saw 19, and
Barry's says 12 where the trace saw 6. The sectors and destinations match ground
truth exactly, so only this one field is still unexplained.

That is worth knowing but not worth blocking on. To load a clone of an existing
location, copy its `packed` verbatim: the clone is byte-identical and the same
size, so whatever the field means it means the same thing for both.

**Changing where a door leads is an 8-byte edit to this table.** Adding a new
destination means pointing an entry at new data; it does not require any new
engine code.

Note the field order. Read as `{sector, packed}` the table is still convincing —
the sectors line up with real locations and the counts look plausible — but every
count is silently paired with the wrong door. The giveaway is that a clone loads
the *neighbouring* door's sector count.

## A location is a chunk plus an identity

Relocating a location takes more than pointing a door at new sectors, because a
door announces two independent things: which chunk to load, and *which location
this is*. The identity comes from the script behind the door, not from the table:

```
0x8003bb44  lhu $v0, 2($s0)        ; second field of the door's warp descriptor
0x8003bb54  sb  $v0, 0x381a($at)   ; the current location id
```

That id indexes a table of 32-byte records at `0x800d2fb4`, whose first byte
selects an artwork set:

```
lbu   $v0, 0x381a($v0)   ; current location id
sll   $v0, $v0, 5        ; 32-byte records at 0x800d2fb4
lbu   $a0, ($v0)         ; byte 0 = asset id
jal   0x80046e38         ; load that set
```

That call site is the *only* reference to `0x80046e38` in the executable, which
is the thing that makes the id far less powerful than it first appears. It runs
during scene setup, not on every transition, so most of what a location shows is
driven by its chunk rather than by this id.

Two earlier conclusions here were wrong, and both cost several test cycles:

- **The id is not "where you are."** Hooking the store at `0x8003bb54` and
  logging every write shows it reading `1` both inside the house *and* while
  standing outside in Monsbaiya, with only two writes across a whole session.
  Walking out of the house does not update it. Treat it as "which interior was
  last entered", stale everywhere else.
- **`0x8006e7f0` is not an asset table.** Its entries point at 16-byte records
  of counts and halfword pairs, not `{packed, sector}` lists. The earlier
  reading of it as `{list, count}` was an artifact of dereferencing one level
  too many. The id-to-artwork mapping quoted above (0 Monsbaiya, 5 house, and so
  on) was inferred from that mistake and should not be trusted.

Consequently the freeze after repointing door 1 at a Monsbaiya clone was *not*
mismatched artwork, and the conditional remap stub built to fix it was treating
a symptom that did not exist. It has been removed. The real problem is simpler:
a door into an interior sets up interior-shaped state, and a town is not an
interior. Record 1 even carries a chunk-relative entry point at +24
(`0x8001ed78`) where town records carry zero, so the engine hands control to the
house's routine at an address that holds unrelated data in a Monsbaiya chunk.

Relocation itself is sound, proven twice. Cloning the house and repointing its
own door at the copy works: identical door, identical state, only the sectors
different. And cloning Monsbaiya works through the *exit* — see below.

## Redirecting a town: the descriptor in slus

Walking out of the house is the game's own route into Monsbaiya, so everything
on that path is already town-shaped. Redirecting it changes exactly one
variable, which sector the town is read from.

The house's chunk does contain a matching record, at chunk offset `0x2b74`
(`0x80018b74` in RAM, LBA 5076 +0x374), holding `19 sectors -> 0x80016000` and
sector 4444 — and editing it does nothing. The engine reads a **second copy**,
in `slus_006.14` at `0x800812f8`, which is LBA 193 +0x2f8 on disc. It is the
last entry of a descriptor table; the records before it load other things to
other addresses, and Shift-JIS text begins immediately after. Being in `slus` it
is uncompressed and editable, unlike the record table above.

Pointing it at a clone at sector 31133 produces a complete, correct load:

```
 42   4205   10   companion chunk
 43  31133   19   0x80016000    the clone, from reclaimed padding
 44   4463   16   Monsbaiya's map pieces
 ...
 52   4556    5   0x801c4640
```

Read 52 is the tell. Every door-hijack attempt died before it; here the sequence
finishes and the game runs normally.

### Why the other 33 copies are dead

Redirecting Barry's copy instead, and leaving slus alone, changes nothing: the
exit still reads 4444. The reason is a fallback in the warp handler:

```
8003bc38  lw    $a1, 8($s0)          ; the descriptor's own destination record
8003bc40  beqz  $a1, 8003bc6c        ; null: leave the destination alone
8003bc44  addiu $v0, $zero, 0xc
8003bc48  lh    $v1, 0x18($a0)       ; the warp kind, stashed earlier
8003bc50  beq   $v1, $v0, 8003bc68   ; kind 12: use the descriptor's record
8003bc58  lui   $v0, 0x8008          ; otherwise: Monsbaiya, hardcoded
8003bc5c  addiu $v0, $v0, 0x12f8
8003bc64  sw    $v0, 4($a0)
8003bc68  sw    $a1, 4($a0)
```

A warp descriptor's word at +8 points at its own `{packed, sector}` record --
the house's `0x8006aed8` has `+8 -> 0x800d4360`, which is `{20 sectors, 5071}`,
the house. So **entering** a place names its destination, and **leaving** one
does not: every exit is kind != 12 and falls through to `0x800812f8`. The copies
sitting in each interior chunk are never consulted.

This is the lever for a second town. The descriptor's +8 pointer lives in slus
and is editable, so a warp can be given a record we author in slus scratch
space naming any sector. What is still unproven is which location id such a warp
must announce: id 1 works fine for a town when arrived at by exiting (the id is
not updated on the way out and Monsbaiya runs happily with the house's id), but
a kind-12 warp *does* set it, and that is the configuration that froze.

### Two towns at once

Redirecting `0x800812f8` replaces Monsbaiya rather than adding to it, since it
is the only entrance. Two towns need one exit to disagree with the others, and
the discriminator is already there: each interior's exit descriptor announces
its own location id, and the store that records it (`8003bb54`) runs earlier in
the same function that later picks the destination.

```
the house         kind 11  location  1   arrive (384, 800)
Barry's shop      kind 11  location 21   arrive (384, 416)
the monster shop  kind 11  location 20   arrive (384, 416)
```

So the fallback becomes a choice. The two instructions that build the constant
are replaced with a jump to a stub that returns to `8003bc60`, leaving the
original store untouched; all the stub does is decide what sits in `$v0`. It
defaults to `0x800812f8` and substitutes a record of ours when the id matches.
`$v0` and `$v1` are both dead at that point and `$at` is the assembler's, so
nothing needs saving.

Confirmed working: leaving Barry's shop loads the clone with the correct town
sequence -- companion chunk 4205, then the chunk from 31133, then the four map
pieces and the dialogue -- with NPCs present and conversation working, while
leaving the house still loads the original at 4444.

This also sidesteps the problem that killed the kind-12 approach. A warp into a
town needs the right chunk, location id, arrival coordinates *and* companion
chunk; the last is chosen by code reading tables that exist only in RAM. Going
out through a door gets all four for free, because it is the path the engine
already uses to reach a town.

### Why the kind-12 route was abandoned

Worth recording, since it very nearly worked and the failures were each
informative. Pointing the house's four location-1 descriptors at an authored
record produced, in order:

1. *An empty house.* The record was placed at `0x8007ac00`, which is zero on
   disc but holds live variables at runtime, so the descriptor read `packed = 0`
   and loaded no chunk at all. Scratch must survive from disc to runtime, not
   merely read as zero; the band around `0x80079800` does, verified by diffing a
   running session against the image.
2. *Town code under house scenery.* With the record fixed the clone loaded, but
   the warp still announced location 1, and a kind-12 warp does set the id --
   which selects the artwork. Changing it to 4, one of the four records whose
   asset id is 0, brought in the correct map.
3. *A black screen.* The load was then complete and correct, but arrival
   coordinates `+4`/`+6` still pointed at a spot inside the house.
4. *A freeze,* once the coordinates were corrected to the town's (384, 800):
   the only remaining difference was companion chunk 4126 instead of 4205.

### A clone shares the original's assets

Only the 19-sector code chunk comes from the cloned sectors. Every asset read in
the clone's trace is at its original absolute sector -- the map pieces at 4463,
4479, 4495, 4511, the dialogue at 4513, the closing read at 4556. So the earlier
belief that four asset reads are addressed relative to the chunk is wrong, and
the 103-sector span it motivated is larger than needed; 19 would do.

The practical consequence is that editing town dialogue, which lives at LBA 4513
onward, changes *both* towns. Making a clone independent means redirecting its
asset reads as well, and those are not simple static records: of the nine asset
sectors, only two appear anywhere as `{packed, sector}` pairs (4556 at
`0x800810c8` and 4543 at `0x80081568`, both in slus). The map pieces are named
some other way, still to be found.

What a clone *does* own is its location table, since that lives in the chunk at
offset `0x61e8`. So the twin's doors can be repointed without touching the
original's, and that is the seam along which the two towns can be made to
differ.

### The chunk also carries a dialogue directory

Immediately before the location table, at chunk offset `0x6178`, is a second
table: fourteen 8-byte entries whose first word is an absolute sector and whose
second is zero. These are not `{packed, sector}` records. They are *boundaries* —
entry n names a block running from its own sector up to the next entry's, so the
length is implied rather than stored:

```
entry  0   4513, next 4516   ->  traced read 4513 x3
entry  8   4538, next 4539   ->  traced read 4538 x1
entry 10   4540, next 4543   ->  traced read 4540 x3
entry 11   4543, next 4547   ->  traced read 4543 x4
```

All four traced counts equal `next - current`, and the fourteenth entry, 4548,
is the closing boundary rather than a block.

The directory is in the chunk, so a clone owns it: copy the blocks into padding,
add the offset to the twin's fourteen entries, and the twin reads its own. The
trace confirms it — the twin's reads are 31250, 31275, 31277 and 31280 where the
original's are 4513, 4538, 4540 and 4543.

The town's words arrive *twice*, which matters when editing them. The bulk read
that fetches the map covers sectors 4463-4526, and 4513 onward is this same
dialogue: the staging buffer at `0x80126804` holds block 0's opening line at
`+0x1009`, exactly 4513's offset within the read that starts at 4511. The
directory's own four loads all target `0x8001dcd0`, one address, so only the
last of them survives. Which copy ordinary conversation is drawn from is still
undetermined, so `--own-dialogue` clones and remaps the bulk region too and
edits every copy of a block. Only sectors 4513-4526 are covered by the bulk
read, so blocks from 4527 on exist solely in the directory copy.

Confirmed working: with every line replaced, Aunt, Fon and the player's own reply
options all speak the twin's text while the original town is unchanged. Two
things survive the substitution and are worth noting as separate mechanisms —
the player's name, which is inserted by a control code rather than stored as
text, and bracketed menu options like `[No.]`.

**A warning about how this looked while being debugged.** Two rounds of "the
text is unchanged" appeared to mean the redirect had failed, and prompted a
wrong conclusion about which copy is live. It had not failed. The trace showed
the twin reading 31250, 31275, 31277 and 31280 where the original reads 4513,
4538, 4540 and 4543, and RAM contained the edited strings with the originals
entirely absent. The edit was only being applied to the *first* text run of each
block — 17 lines out of about 1500 — so the odds of any given NPC speaking one
were slim. When verifying a data redirect by eye, change enough of the data that
you cannot miss it; a negative result from a sparse edit says nothing at all.

The text is plain full-width Shift-JIS on disc, uncompressed, so it can be
edited in place. Nothing is length-prefixed, which cuts both ways: substitutions
are trivial but must not change the byte count, so pad with the full-width space
`0x8140`. Renaming the town in the twin's copy — it names itself three times —
is a nine-character swap.

### The global asset table

The map pieces come from somewhere else again: a table of 82-odd `{packed,
sector}` records at RAM `0x800d1bf4`, which **is** on disc, at LBA 3245 offset
`0x494`. An earlier note here claimed this data was RAM-only; that was about the
record table at `0x800d2fb4`, which is a different structure.

It reads as `{sector, size}` pairs if you start four bytes late, which is how it
was first misread. Taken with the correct alignment it is the same format as
every other descriptor, and the counts match the trace exactly:

```
[ 1]  16 sectors  sector 4463    the town's map
[13]   2 sectors  sector 6201    Barry's shop assets
[37]  10 sectors  sector 4205    the companion chunk
[53]  32 sectors  sector 6163    loaded before a shop
```

The destination field is zero throughout, which evidently means the staging
buffer at `0x80126804` rather than a fixed address.

Most call sites name a record by its absolute address — `0x8003b120` builds
`0x800d1d54`, and so on for 27 sites — rather than indexing the table, so a
record is a thing code points at, not a slot code looks up.

This makes map sectors editable, but the table is **global**: changing record 1
moves the map for both towns.

### The asset remap hook

The general answer is to intercept the sector on its way to the drive rather
than chase each table. Every read passes one point where the LBA is in a
register:

```
8003eaa4  lw    $a0, 4($s1)      ; the LBA, from the request record
8003eaa8  jal   8006124c         ; CdIntToPos
8003eaac  addiu $a1, $sp, 0x10   ; delay slot
8003eab0  addiu $a0, $zero, 2    ; CdlSetloc
```

Both of the first two instructions must be displaced. Replacing only the `lw`
would leave the `jal` sitting in the jump's delay slot, and two branches in a
row are undefined on the R3000. The stub therefore performs the load, the
`CdIntToPos` call and its argument itself, then resumes at `8003eab0`. `$at`,
`$v0` and `$v1` are dead across the insertion.

The harder half is knowing *which* town is asking, and nothing at the read site
can tell: the clone is byte-for-byte identical, so the chunk signature at
`0x80016008` reads `0x8001a484` for both. But the gate stub already knows, being
the code that chooses the clone, so it writes a flag byte at `0x80079808` — set
on the twin's branch, cleared on every other exit — and the remap consults it.

The table is data rather than unrolled code, at `0x800798d0`, so adding an asset
costs eight bytes and no reassembly. `newtown.py --own-map` uses it to redirect
the town's four 16-sector map reads at a clone; all four starting sectors need
entries, since only the first is the one the global table names.

One wrinkle for debugging: the read tracer takes its LBA from the same
`4($s1)`, so it logs the *original* request and shows no sign of the
substitution. The stub therefore stores whatever it replaced at `0x8007980c`,
which is the thing to read out of a RAM dump to confirm the hook fired.

### A shop of the twin's own

Barry's shop is the best first difference, because its stock is code rather than
data: the routine at chunk offset `0x5c4` writes a NUL-terminated list of 4-byte
`{id, category}` entries, and it is the same routine `patch.py` rewrites. Clone
the six-sector chunk, point the *twin's* door 19 at the copy, rewrite the copy's
routine, and there are two weapon shops that share a building and nothing else.

Confirmed by trace, one continuous session:

```
 43   4444    19   0x80016000   original town    (leaving the house)
 55   6195     6   0x80016000   original Barry   Copper Sword, Medicinal Herb
 61  31133    19   0x80016000   twin town        (leaving Barry's)
 72  31240     6   0x80016000   twin's Barry     nine eggs
 77  31133    19   0x80016000   twin town        (leaving again)
```

Read 77 is the one worth checking. The clone's exit descriptor is a byte-for-byte
copy, so it still announces location 21, and the gate stub sends 21 to the
clone -- you come out in the town you went in from, with no extra work.

Two constraints follow from how a door works. It must point at the same *kind*
of place, since the id it announces selects a chunk-relative entry point; a door
into a shop can be given another shop and nothing else. And the clone shares its
assets exactly as the town does -- reads 73-75 fetch 4126, 6201 and 6203, the
original shop's -- so the twin's shopkeeper still looks and talks like Barry.

Note that selling eggs makes their vanilla flat 100G price an unlimited money
loop, so the price fix is not optional once a shop stocks them.

The record table at `0x800d2fb4` still cannot be edited on disc: `MAIN.BIN` and
`TOWN.BIN` are compressed (their loads use command `0x60`), so it exists only
once expanded into RAM. Searching the image for it byte-for-byte finds nothing.

## The south gate, and why travel is still on a shop door

Reaching the twin by leaving Barry's shop works but is obviously a placeholder.
The natural trigger is the south gate, which in vanilla is scenery — you can
stand on it and nothing happens. Two things block moving the trigger there, and
both are worth stating precisely so the next attempt starts ahead.

**A town cannot be entered by a door.** Doors are kind-12 warps and set up
interior-shaped state; the attempt to force one is documented above and died on
the companion chunk, which is chosen by code reading RAM-only tables. Arriving
in a town means arriving through a kind-11 exit, and exits belong to interiors.
So any trigger has to either be an exit, or solve the kind-12 problem.

The remap hook does not rescue this. The kind-12 route pulled companion chunk
4126 (27 sectors to `0x8011ac98`) where a town needs 4205 (10 sectors to
`0x80126804`); the count and destination come from the descriptor, not the
sector, so substituting the sector alone would read the wrong length to the
wrong address.

**The town's door table has no gate in it.** All 38 entries load an interior to
`0x80016000`; entry 0 is a null with zero sectors. The tower is not in it
either, so walking out of town to the tower is a different mechanism, and that
mechanism is the model we want. The record naming the tower entrance
(`{19 sectors -> 0x80016000, 13660}`) appears exactly once on the disc, at LBA
12809 +0x46c, and is absent from RAM while standing in town — so it belongs to
the tower's own data and is not what the town consults.

**The tower is a module switch, not a warp.** Tracing a walk into it gives:

    11305    4  0x80126804   TOWN.BIN     the last thing town loads
    12628    8  0x80126804   DUNGEON.BIN  the dungeon's companion chunk
    12636  187  0x80088760   DUNGEON.BIN  the dungeon module itself
    13660   19  0x80016000   DUNGEON.BIN  and then its first floor

That 187-sector blob covers `0x80088760`-`0x800e5f60`, and both the code that
announced the new location id (`0x800d4404`) and the record naming the floor
(`{19 sectors -> 0x80016000, 13660}` at `0x800df3cc`) are inside it. The town
never knew the tower's sector: it handed control to the dungeon module, which
loaded its own floor. So the tower is not a template for walking between towns,
and the record found on disc at LBA 12809 is simply the module's own copy.

Note the last two reads have the same shape as a town arrival -- a companion
chunk to `0x80126804`, then 19 sectors to `0x80016000` -- which is more evidence
that the exit path is the right one for a second town. What is missing is only
the trigger. That means cracking the town map's walkable entity format, since
the south gate does nothing in vanilla and so has no trigger to repoint.

Until then `--gate-id` moves the link to any building's exit.

## Location ids are building types, and the town has a plot table

cjaz's `BuildingType` enum and our location ids are the same numbering. The
three ids we watched doors announce line up exactly: 1 is `KohHouse_1`, and
0x14 and 0x15 are the monster shop and Barry's, which his enum skips only
because the shops are always present so nothing ever needs to ask whether they
were built.

The town keeps a plot table at `0x800133a4`: 34 entries of two bytes,
`{type, upgradingType}`. It says what stands on each lot. Reading it out of a
save mid-game gives, for that save, `Koh's House 2` on plot 11, `Fountain` on
plot 16 and `Monster Hut 2` on plot 33, with the rest holding fixed structures.
Named plot indices, from cjaz: racetrack 1, temple 2, Koh's house 0xb,
fountain 0x10, hospital 0x11, library 0x12, monster hut 0x21.

Types `0x2c`-`0x33` are vacant lots, one per buildable plot -- they sit in the
racetrack and library slots of a save where neither is built. Building passes
through a stage-1 type first: `0x04` before `Fountain`, `0x0f` before
`Hospital`, `0x11` before `Temple`. The table survives the switch into the
tower, so it is persistent state rather than part of the town overlay.

Two things follow. Location ids can now be named rather than discovered by
walking, which is what `--gate-id` reports against. And whatever code reads
`0x800133a4` to decide what to place is the town's entity layer -- the best
remaining lead on where a walkable trigger like the south gate would live.

## The building placement table

Scanning RAM for loads whose offset falls inside the plot table finds the
consumer at `0x800b8460`-`0x800b85cc`, in the town module. It loops a counter
to 33, indexes the plot table by `counter * 2`, and compares both `0x33a4`
(type) and `0x33a5` (upgrading) against a type it was handed -- asking "is this
building present anywhere in town", which is the same question cjaz's
`tryToUpdateDynamicBuildingState` asks. Its neighbours read `0x33e6`/`0x33e7`,
the last plot's two bytes, which is how the table's extent was confirmed.

That code works from two tables:

    0x800d2644   67 records of 32 bytes   what a building is, and where
    0x800d2ea4   records of 8 bytes       indexed alongside, fields at +2/+3

In the 32-byte record, `+6` is the building type -- the same numbering as a
location id -- and `+7` is a related type, which for the monster huts chains
upward through 38, 39, 40, 41 and looks like an upgrade target. `+12` and `+14`
are coordinates. They land on a clean grid:

    x   512 640 1536 2304 2432 2560 3200 3584 4096 4224 4608 4992 5632 5888 6656 6784
    y   1536 1792 2816 3968 4096 4736 5504

Seven rows, sixteen columns, everything a multiple of 128 -- a town laid out on
a grid, which is what you would expect of a place whose buildings come and go.

**Neither table is verbatim on the disc.** The same wall we hit with the
location record table: `MAIN.BIN` and `TOWN.BIN` are read with command `0x60`,
compressed, so these are built at runtime. Editing placement therefore means a
runtime hook in the free executable region rather than a disc edit -- the
mechanism we already use for the gate and remap stubs.

This is the shape of the original south gate idea. "A door at the south gate"
becomes "a building placed at the south gate", since a building carries a door
and a door announces a location id, which is exactly what the travel gate
discriminates on. What is missing is the gate's coordinates, and the way to get
them is two RAM dumps from two known standing positions, diffed for the words
that move.

## Unused disc space

The disc is nominally full: only 251 of 126,946 sectors are blank. But the
ISO9660 table lists a 73.9 MB `STR/STAFROLL.STR` and two files named `DUMMY`,
and `DUMMY_.STR` turns out to be 320 sectors that are blank apart from an
interleave header every 32nd sector. That leaves ten independent runs of 31
usable sectors, enough for several location chunks, without resizing the image
or disturbing anything the game reads.

New sectors must be built, not just written into, since the padding is a mix of
Form 1 and Form 2. A location sector is Mode 2 Form 1: the standard sync
pattern, the address as minutes/seconds/frames in BCD at LBA + 150, mode byte 2,
a subheader of `00 00 08 00` twice (submode `0x08` = data), 2048 bytes of
payload, then EDC and ECC. Validate the builder by reconstructing sectors the
game already ships and requiring byte-identical output.

## Method notes

- **Live RAM beats static analysis here.** Barry's stock list turned out to be
  built at runtime, and the overlay's split disc mapping is invisible from the
  file alone. Snapshots came from `/proc/<pid>/mem` of a running DuckStation,
  locating guest RAM by searching writable regions for a code signature lifted
  from the disc.
- **`ptrace_scope`** blocks reading another process's memory. Either launch the
  emulator as a child of your tool, or relax the sysctl briefly.
- **Live patches don't stick.** Writing into emulator RAM gets discarded when
  the overlay reloads from disc, and the recompiler may have cached the old
  code. Patch the image.
- **Verify EDC/ECC by identity.** Recompute the checksums for sectors you have
  *not* touched; the result must be byte-identical to what was already there. If
  it isn't, the implementation is wrong and you must not write anything. Both
  patchers do this before making changes.
- **The R3000 has a load delay slot and no interlock.** The instruction directly
  after a load still sees the register's old value. The first version of the read
  tracer used each loaded register on the very next instruction and silently
  logged stale data -- including reading its own ring cursor as zero every time,
  so every record landed in slot 0. Any injected code must respect this.
- **"Reads as zero" does not mean "unused".** The tracer's log ring was first
  placed in a 30 KB region of RAM that was zero in four separate snapshots. The
  game still reused it, and entering the tower wiped the log. Scratch space must
  go in a gap inside `slus_006.14`, which is loaded once at boot and never
  reloaded; `0x80079580` and `0x80079e00` are two such gaps.
- **Disc space is available despite the disc being full.** Only 251 of 126,946
  sectors are blank, but the ISO9660 table lists `STR/STAFROLL.STR` at 73.9 MB
  (sectors 88956-126796) plus two files literally named `DUMMY` at 640 KB each.
  That is ample room for new location data without resizing the image.
