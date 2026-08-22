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

- **Barry's shop stocks nine items** instead of two. The Copper Sword and
  Medicinal Herb stay; seven monster eggs join them.
- **The monster shop actually sells monsters.** In vanilla, picking
  "I've come to buy." leads to an apology and nothing else. Now it opens a
  table of 24 eggs, quotes a price, asks you to confirm, takes your gold and
  puts the egg in your bag.

Six sectors change. Everything else is byte-for-byte the original.

**In a browser** — open `web/index.html`. Pick your `.bin`, tick the changes you
want, download the result. There is no server; the file never leaves your
machine. If your browser blocks ES modules on `file://`, serve the folder:

```sh
cd web && python3 -m http.server 8000   # then visit http://localhost:8000
```

**On the command line** — `patch.py` needs only the standard library:

```sh
./patch.py "Azure Dreams (USA).bin"
./patch.py in.bin -o out.bin --no-barry          # monster shop only
./patch.py in.bin --verify known-good.bin        # compare, don't write
```

| Sector | Contents | Patch |
| --- | --- | --- |
| 1883 | monster egg prices | Barry |
| 6147 | egg list builder | monster shop |
| 6149 | give-item hook | monster shop |
| 6158 | buy flow script | monster shop |
| 6195 | Barry's stock builder | Barry |
| 14930 | egg prices, second copy | Barry |

Whichever patch you pick, **all 24 eggs are repriced** so none can be bought for
less than it sells for. Vanilla prices nearly every egg at 100G against sell
values up to 50,000G, which costs nothing while no shop sells eggs — but both
patches here do, and without the fix an Ultimate egg would be a 100G purchase
you could immediately resell for 50,000G. Prices are read from the game's own
table rather than hardcoded, so the rule cannot drift out of step with it.

## A second town

`tools/newtown.py` builds a disc with **two Monsbaiyas on it**. The original is
where it always was; the twin lives in sectors that shipped as padding inside
`DUMMY_.STR`, and you reach it by walking out of Barry's shop.

```sh
python3 tools/newtown.py "Azure Dreams (USA).bin" \
    --template twotowns --own-shop --own-dialogue
```

The twin is not a mirror. It has its own weapon shop with its own stock, and its
own dialogue, both independent of the original town's. What makes that possible:

- **A location is data at an absolute sector.** The loader does not care which
  archive it comes from, so a location can be cloned anywhere there is room.
- **A clone owns its chunk**, and the chunk holds the town's door table and its
  dialogue directory. Those can be edited for one town without touching the
  other.
- **Everything else is shared**, and for that there is an asset remap hook: the
  gate that chooses the twin sets a flag, and a stub on the disc read path
  substitutes sectors while that flag is set. Adding an asset costs eight bytes
  in a data table and no reassembly.

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
tools/newtown.py    clone a location, add a second town
tools/trace_cd.py   inject disc-read logging, decode the result
tools/compare.mjs   proves the JS and Python emit identical bytes
docs/FINDINGS.md    the reverse engineering: addresses, opcodes, method
```

## Checking it still works

The two shop-patcher implementations are kept honest against each other, and
both against a known-good image:

```sh
./patch.py vanilla.bin --verify reference.bin
node tools/compare.mjs vanilla.bin reference.bin
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
