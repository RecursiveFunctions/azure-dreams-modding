#!/usr/bin/env python3
"""Dump PSX guest RAM out of every running DuckStation process.

Requires kernel.yama.ptrace_scope=0 (caller relaxes and restores it).
Locates guest RAM by scanning host-writable regions for a code signature
lifted straight out of the disc image.
"""
import os, re, struct, subprocess, sys

BIN = "/mnt/Games/ROMs/psx/roms/Azure Dreams (USA).bin"
SECTOR, HDR, DATA, DELTA = 0x930, 0x18, 0x800, 0x80020800
SIG_ADDR = 0x8004eebc

raw = open(BIN, "rb").read()
def log2bin(l):
    s, w = divmod(l, DATA)
    return s * SECTOR + HDR + w
SIG = bytes(raw[log2bin(SIG_ADDR - DELTA + i)] for i in range(96))


def rw_regions(pid):
    out = []
    for line in open(f"/proc/{pid}/maps"):
        m = re.match(r"([0-9a-f]+)-([0-9a-f]+) (\S+) \S+ \S+ \S+\s*(.*)", line)
        if not m:
            continue
        a, b, perm, path = int(m.group(1), 16), int(m.group(2), 16), m.group(3), m.group(4).strip()
        if "r" in perm and "w" in perm:
            out.append((a, b, path))
    return out


def grab(pid):
    try:
        mem = open(f"/proc/{pid}/mem", "rb", 0)
    except OSError as e:
        return None, f"open failed: {e}"
    cands = [r for r in rw_regions(pid) if not r[2] or r[2].startswith("/memfd")]
    cands.sort(key=lambda r: r[1] - r[0])
    for a, b, path in cands:
        size = b - a
        if size < 0x200000 or size > 0x10000000:
            continue
        try:
            mem.seek(a)
            buf = mem.read(size)
        except Exception:
            continue
        i = buf.find(SIG)
        if i < 0:
            continue
        base = a + i - (SIG_ADDR & 0x1FFFFF)
        try:
            mem.seek(base)
            ram = mem.read(0x200000)
        except Exception as e:
            return None, f"read failed: {e}"
        if len(ram) == 0x200000:
            return ram, f"region 0x{a:x}+0x{size:x} {path or '[anon]'} base=0x{base:x}"
    return None, "signature not found"


pids = []
for line in subprocess.run(["pgrep", "-f", "DuckStation-x64"],
                           capture_output=True, text=True).stdout.split():
    pids.append(int(line))

for pid in pids:
    try:
        cmd = open(f"/proc/{pid}/cmdline", "rb").read().replace(b"\0", b" ").decode()
    except OSError:
        continue
    disp = "?"
    try:
        for kv in open(f"/proc/{pid}/environ", "rb").read().split(b"\0"):
            if kv.startswith(b"DISPLAY="):
                disp = kv.decode().split("=", 1)[1]
    except OSError:
        pass
    ram, note = grab(pid)
    if ram:
        path = f"/tmp/ad_ram_{pid}.bin"
        with open(path, "wb") as f:
            f.write(ram)
        print(f"pid {pid} DISPLAY={disp} -> {path}  ({note})")
    else:
        print(f"pid {pid} DISPLAY={disp} -> no RAM ({note})")
