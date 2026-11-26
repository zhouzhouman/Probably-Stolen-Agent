# -*- coding: utf-8 -*-
"""RVA → 方法名解析器：从 dump.cs 建索引，解析反汇编调用目标。

用法: python tools/rva_resolve.py 0x6BAA90 [size_hex]
"""
import re
import sys

import capstone
import pefile

DLL = r"D:\SteamLibrary\steamapps\common\Probably Stolen Demo\GameAssembly.dll"
DUMP = r"D:/WorkBuddyData/深空当铺aiagent/tools/Il2CppDumper/dump.cs"
IMAGE_BASE = 0x180000000


def load_index():
    """dump.cs: '// RVA: 0xXXXX ...' 的下一行是方法签名。"""
    idx = {}
    text = open(DUMP, encoding="utf-8").read()
    for m in re.finditer(r"//\s*RVA:\s*(0x[0-9A-Fa-f]+)[^\n]*\n\s*(?:\[[^\]]*\]\s*\n\s*)*([A-Za-z_][^\n{]*)\{", text):
        rva = int(m.group(1), 16)
        sig = m.group(2).strip()
        name = re.sub(r"\s*\(.*", "", sig)
        idx[rva] = name
    return idx


def main():
    rva = int(sys.argv[1], 16)
    size = int(sys.argv[2], 16) if len(sys.argv) > 2 else 0x200
    idx = load_index()

    pe = pefile.PE(DLL)
    # RVA → 文件偏移
    fo = None
    for s in pe.sections:
        if s.VirtualAddress <= rva < s.VirtualAddress + max(s.Misc_VirtualSize, s.SizeOfRawData):
            fo = s.PointerToRawData + (rva - s.VirtualAddress)
            break
    if fo is None:
        print("RVA not in any section")
        return
    data = pe.get_memory_mapped_image()[IMAGE_BASE + rva - IMAGE_BASE:] if False else None
    with open(DLL, "rb") as f:
        f.seek(fo)
        code = f.read(size)

    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
    md.detail = False
    print(f"== RVA 0x{rva:X} ==")
    for ins in md.disasm(code, rva):
        line = f"0x{ins.address:X}: {ins.mnemonic} {ins.op_str}"
        if ins.mnemonic in ("call", "jmp") and "0x" in ins.op_str:
            m = re.search(r"0x([0-9a-f]+)", ins.op_str)
            if m:
                target = int(m.group(1), 16)
                name = idx.get(target)
                if name:
                    line += f"  => {name}"
        print(line)


if __name__ == "__main__":
    main()
