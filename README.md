# Azure Dreams Modding

Tools and reverse engineering for *Azure Dreams* (USA, `SLUS-00614`), a 1997
PlayStation game. Two things live here: a finished patcher for the game's shops,
and the work-in-progress machinery for adding **a second town** to the disc.

Everything patches a MODE2/2352 disc image in place, rewriting EDC/ECC for each
touched sector so the result stays a valid disc. Nothing here needs the game
decompiled, and none of it depends on an emulator at runtime.

The reverse engineering is written up in [`docs/FINDINGS.md`](docs/FINDINGS.md),
including the wrong turns and what they cost — the loader, the disc's several
descriptor formats, the script bytecode, and the R3000 quirks that bite when you
inject code.

## The shop patcher

- **Barry's shop stocks what you choose.** Any item or piece of equipment,
  up to 62 of them. The default keeps the Copper Sword and Medicinal Herb and
  adds seven monster eggs.
- **The monster shop actually sells monsters.** In vanilla, picking
  "I've come to buy." leads to an apology and nothing else. Now it opens a
  table of eggs, quotes a price, asks you to confirm, takes your gold and
  puts the egg in your bag. The default is one egg for every monster in the
  game, all 45, not only the 24 the tower drops.
- **Prices are yours to set.** Buy and sell for every item, with the game's
  own sell values as the default and buying at double. Sand is 1000G to buy.

Eight sectors change for the defaults. Everything else is byte-for-byte the
original.

**In a browser** — open `web/index.html`. Pick your `.bin`, tick what each shop
should sell, edit prices, download the result. There is no server; the file
never leaves your machine. Your stock and prices are remembered by the browser
and can be exported as JSON. If your browser blocks ES modules on `file://`,
serve the folder:

```sh
cd web && python3 -m http.server 8000   # then visit http://localhost:8000
```

**On the command line** — `patch.py` needs only the standard library and takes
the same JSON the browser exports:

```sh
./patch.py "Azure Dreams (USA).bin"                    # the defaults
./patch.py in.bin --dump-config shops.json             # write the defaults, to edit
./patch.py in.bin -o out.bin --config shops.json
./patch.py in.bin --verify known-good.bin              # compare, don't write
```

The config is three parts: a stock list per shop, each entry `{cat, id,
quality}` (quality is the +N on equipment or the charges on a ball), and a
`prices` map of `"cat:id"` to `{buy, sell}`. Only listed prices are written.

| Sector | Contents | Patch |
| --- | --- | --- |
| 164 | item prices (slus) | prices |
| 182 | Barry's stock table | Barry |
| 1883 | egg prices | prices |
| 6147 | list builder | monster shop |
| 6149 | give-item hook | monster shop |
| 6158 | buy flow script and stock table | monster shop |
| 6195 | Barry's stock builder | Barry |
| 14930 | egg prices, second copy | prices |

Each shop's stock used to be a routine that spelled its list out byte by byte.
It is replaced with a nine-instruction loop that copies a table: Barry's from
spare room in the executable, the monster shop's from the apology text its own
buy flow no longer needs. Barry can list 62 goods, the monster shop 60. Price edits go into the game's item records; eggs are written
twice because the table exists once for the town and once for the tower.

A stocked item that sells for more than it costs is a money loop. The patcher
warns rather than refuses, since the default already prices everything at
twice its sell value.

**Start from a memory card save or the title screen, not a save state.** The
stock tables live in `slus_006.14`, which is loaded once at boot; a save state
restores the pre-patch copy and the shops come up empty.

## A second town

`tools/newtown.py` builds a disc with **two Monsbaiyas on it**. The original is
where it always was; the twin lives in sectors that shipped as padding inside
`DUMMY_.STR`, and you reach it through a house at the south gate.

```sh
python3 tools/newtown.py "Azure Dreams (USA).bin" \
    --template twotowns --own-shop --own-dialogue --move-house
```

The twin is not a mirror. It has its own weapon shop with its own stock, and its
own dialogue, both independent of the original town's. What makes that possible:

- **A location is data at an absolute sector.** The loader does not care which
  archive it comes from, so a location can be cloned anywhere there is room.
- **A clone owns its chunk**, and the chunk holds the town's door table and its
  dialogue directory. Those can be edited for one town without touching the
  other.
- **Everything else is shared**, and for that there is an asset remap hook: a
  flag records which town you are in, and a stub on the disc read path
  substitutes sectors while it is set. Adding an asset costs eight bytes in a
  data table and no reassembly.

Travel is a building, not a special case. `--move-house` relocates one house to
the south gate — its position lives in a table built at runtime from compressed
data, so a stub rewrites it rather than the disc — and that house's door is the
one door that crosses between the towns. It works in both directions, and every
*other* building returns you to the town you entered it from, so the twin is a
place you can walk around in rather than a single room.

Some flags are for testing rather than play. `--stamp` replaces every line of
the twin's dialogue with a marker, which is how you prove a redirect took effect
— a sparse edit proves nothing, since NPCs mostly speak lines you did not touch.

`tools/trace_cd.py` injects logging stubs that record every disc read, every CD
dispatcher call, and every location-id write into scratch RAM, which is how most
of the above was found:

```sh
python3 tools/trace_cd.py patch game.bin -o traced.bin
python3 tools/trace_cd.py decode ramdump.bin
```

## What you need

An unmodified *Azure Dreams (USA)* image in **MODE2/2352** format — 2352 bytes
per sector, which is what essentially every PlayStation rip is. A 2048-byte
MODE1 image will be rejected, because the error-correction data the patcher
rewrites isn't there.

Other regions are not supported. The addresses are specific to the USA release.

No game data is included or distributed here.

## Layout

```
patch.py            command-line shop patcher, no dependencies
web/index.html      browser patcher
web/patcher.js      patch logic (also runs under node)
web/app.js          interface wiring
web/config.js       default stock and price rules
web/items.js        item names and vanilla prices, generated
tools/gen_items.py  regenerate web/items.js from a disc
tools/newtown.py    clone a location, add a second town
tools/trace_cd.py   inject disc-read logging, decode the result
tools/ramdump.py    pull guest RAM out of a running DuckStation
tools/compare.mjs   proves the JS and Python emit identical bytes
docs/FINDINGS.md    the reverse engineering: addresses, opcodes, method
```

## Checking it still works

The two shop-patcher implementations are kept honest against each other, and
both against a known-good image:

```sh
./patch.py vanilla.bin --verify reference.bin
node tools/compare.mjs vanilla.bin reference.bin [shops.json]
```

Both print `MATCH` when the output is byte-identical. Any divergence shows up
here instead of in an emulator.

`newtown.py` verifies itself as it runs: it rebuilds real sectors from their own
payloads and refuses to write unless the result is byte-identical, checks that
every destination sector is genuinely padding, and compares each clone against a
snapshot of its source taken before any patching.

## Credit and prior art

The sector-skipping and EDC/ECC approach follows the technique used by the
[Kohmunity Azure Dreams randomizer](https://github.com/Kohmunity/Kohmunity.github.io),
which patches a different shop (Fur's item store). The routines here were found
independently; see `docs/FINDINGS.md`.
