# -*- coding: utf-8 -*-
"""Find all direct call sites (E8 rel32) to target RVAs in GameAssembly.dll,
and map caller addresses back to method names parsed from Il2CppDumper dump.cs.

Usage: python tools/find_calls.py <target_rva_hex> [more_rva_hex ...]

Example: python tools/find_calls.py 595990   -> who calls PayMoney
"""
import bisect
import re
import struct
import sys
import pefile

DLL = r'D:\SteamLibrary\steamapps\common\Probably Stolen Demo\GameAssembly.dll'
DUMP = r'D:\WorkBuddyData\深空当铺aiagent\tools\Il2CppDumper\dump.cs'


def parse_dump():
    """Parse dump.cs -> dict rva -> method signature line."""
    rva_map = {}
    pat = re.compile(r'\s*// RVA: 0x([0-9A-Fa-f]+) Offset: 0x[0-9A-Fa-f]+ VA: 0x([0-9A-Fa-f]+)')
    with open(DUMP, 'r', encoding='utf-8', errors='ignore') as f:
        prev_match = None
        for line in f:
            m = pat.match(line)
            if m:
                prev_match = int(m.group(1), 16)
            elif prev_match is not None and line.strip():
                rva_map[prev_match] = line.strip()[:150]
                prev_match = None
    return rva_map


def main():
    targets = [int(a, 16) for a in sys.argv[1:]]
    rva_map = parse_dump()
    keys = sorted(rva_map)
    print('dump methods parsed: %d' % len(rva_map))

    pe = pefile.PE(DLL, fast_load=True)
    secs = []
    for s in pe.sections:
        if s.Characteristics & 0x20000000:  # executable
            secs.append((s.VirtualAddress, min(s.Misc_VirtualSize, s.SizeOfRawData), s.PointerToRawData))
    print('exec sections: %s' % [(hex(v), hex(sz)) for v, sz, _ in secs])
    data = pe.__data__

    def name_for(rva):
        i = bisect.bisect_right(keys, rva) - 1
        if i < 0:
            return '???'
        base = keys[i]
        return '%s (+%#x)' % (rva_map[base], rva - base)

    for tgt in targets:
        print('=== callers of RVA %#x : %s ===' % (tgt, rva_map.get(tgt, '(not a method start)')))
        found = []
        for vaddr, vsize, praw in secs:
            blob = data[praw:praw + vsize]
            pos = 0
            limit = len(blob) - 5
            while True:
                pos = blob.find(b'\xe8', pos)
                if pos < 0 or pos > limit:
                    break
                rel = struct.unpack_from('<i', blob, pos + 1)[0]
                dst = vaddr + pos + 5 + rel
                if dst == tgt:
                    found.append(vaddr + pos)
                pos += 1
        for src in found[:50]:
            print('  %#x  <- %s' % (src, name_for(src)))
        print('  total: %d' % len(found))
        if len(found) > 50:
            print('  (first 50 shown)')


if __name__ == '__main__':
    main()
