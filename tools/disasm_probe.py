# -*- coding: utf-8 -*-
"""Disassemble a function in GameAssembly.dll and list call/jmp targets.

Usage: python tools/disasm_probe.py <rva_hex> [size_hex]
RVA values come from tools/Il2CppDumper/dump.cs comments
(e.g. "// RVA: 0x594E40 Offset: ... VA: ...").

This helps identify what a native-compiled C# method calls, since dump.cs
contains method signatures but no bodies.
"""
import sys
import re
import pefile
from capstone import Cs, CS_ARCH_X86, CS_MODE_64

DLL = r'D:\SteamLibrary\steamapps\common\Probably Stolen Demo\GameAssembly.dll'
IMAGE_BASE = 0x180000000

# Known PlayerStore methods (RVA from dump.cs)
KNOWN = {
    0x180594E40: 'PlayerStore.OnRentPaid',
    0x180594D50: 'PlayerStore.OnRentNotPaid',
    0x180595990: 'PlayerStore.PayMoney',
    0x1805959F0: 'PlayerStore.PayWeeklyMortgage',
    0x180587D50: 'PlayerStore.CanPayRent',
    0x180597070: 'PlayerStore.RentIncrease',
    0x18058D1A0: 'PlayerStore.GetRentDayCounter',
    0x18058D230: 'PlayerStore.GetRentValue',
    0x180594EB0: 'PlayerStore.OnShutterOpened',
    0x180593A90: 'PlayerStore.LogQueuedClient',
    0x18058CDE0: 'PlayerStore.GetNextClient',
    0x180587CB0: 'PlayerStore.CanNextClient',
    0x18059A3F0: 'PlayerStore.TryCallNextClient',
    0x180591F90: 'PlayerStore.IsClientPresent',
    0x180591F50: 'PlayerStore.IsClientDimissable',
}


def main():
    rva = int(sys.argv[1], 16)
    size = int(sys.argv[2], 16) if len(sys.argv) > 2 and sys.argv[2] != 'all' else 0x400
    show_all = 'all' in sys.argv[2:]
    pe = pefile.PE(DLL, fast_load=True)
    off = pe.get_offset_from_rva(rva)
    data = pe.__data__[off:off + size]
    md = Cs(CS_ARCH_X86, CS_MODE_64)
    md.detail = True
    print('== disasm RVA %#x (file off %#x) size %#x %s==' % (rva, off, size, 'ALL' if show_all else ''))
    total = 0
    for ins in md.disasm(data, IMAGE_BASE + rva):
        total += 1
        if show_all or ins.mnemonic in ('call', 'jmp', 'ret'):
            op = ins.op_str
            tgt = None
            if op.startswith('0x'):
                tgt = int(op, 16)
            else:
                m = re.search(r'rip \+ (0x[0-9a-f]+)', op)
                if m:
                    tgt = ins.address + ins.size + int(m.group(1), 16)
            tag = ''
            if tgt is not None:
                tag = ' => ' + KNOWN.get(tgt, 'sub_%x' % tgt)
            print('%#x: %s %s%s' % (ins.address, ins.mnemonic, op, tag))
    print('== %d instructions disassembled ==' % total)


if __name__ == '__main__':
    main()
