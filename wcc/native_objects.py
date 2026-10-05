"""Compile RV32IM C into relocatable WRM objects using the M register ABI."""

from pathlib import Path
import struct
import subprocess
import sys
import tempfile

import wcc

MC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(MC))
import elf


# Keep RV argument, return, stack and link registers in their WRM ABI roles.
# RV saved registers occupy r16..r27. The six extra RV temporaries would use
# WRM saved registers, so GCC must not allocate them.
REGISTERS = {0: 0, 1: 31, 2: 30, 3: 29, 4: 28, 5: 9, 6: 10, 7: 11,
             8: 16, 9: 17, **{r: r - 9 for r in range(10, 18)},
             **{r: r for r in range(18, 28)}, 28: 12, 29: 13, 30: 14, 31: 15}
FIXED = (6, 7, 28, 29, 30, 31)
FLAGS = ["-march=rv32im", "-mabi=ilp32", "-mno-relax", "-msmall-data-limit=0",
         *(f"-ffixed-x{r}" for r in FIXED), "-ffreestanding", "-fno-builtin", "-fsigned-char",
         "-fno-pic", "-fno-pie", "-fno-stack-protector", "-fno-common",
         "-fno-optimize-sibling-calls", "-fno-jump-tables",
         "-fno-unwind-tables", "-fno-asynchronous-unwind-tables", "-O1"]


def machine(path):
    data = Path(path).read_bytes()
    if len(data) < 20 or data[:4] != b"\x7fELF":
        return None
    return struct.unpack_from("<H", data, 18)[0]


def read_riscv(data, name):
    if len(data) < elf.EHDR.size or data[:7] != b"\x7fELF\x01\x01\x01":
        raise wcc.TranslationError(f"{name}: expected a little-endian ELF32 RISC-V object")
    header = elf.EHDR.unpack_from(data)
    if header[1] != elf.ET_REL or header[2] != 243:
        raise wcc.TranslationError(f"{name}: expected a relocatable RISC-V object")
    if header[7] != 0:
        raise wcc.TranslationError(f"{name}: RV32IM soft-float objects without RVC are required")
    if header[3] != 1 or header[8] != elf.EHDR.size or header[11] != elf.SHDR.size or not header[12]:
        raise wcc.TranslationError(f"{name}: unsupported ELF header")
    if header[6] + header[12] * elf.SHDR.size > len(data):
        raise wcc.TranslationError(f"{name}: truncated ELF section headers")
    for index in range(header[12]):
        section_header = elf.SHDR.unpack_from(data, header[6] + index * elf.SHDR.size)
        if section_header[1] != elf.SHT_NOBITS and section_header[4] + section_header[5] > len(data):
            raise wcc.TranslationError(f"{name}: truncated ELF section {index}")
        if section_header[2] & elf.SHF_ALLOC and section_header[1] not in (elf.SHT_PROGBITS, elf.SHT_NOBITS):
            raise wcc.TranslationError(f"{name}: unsupported allocated section type {section_header[1]}")
    # Reuse the project's ELF container reader; translate its sections and
    # relocation numbers before emitting any object for the WRM linker.
    container = bytearray(data)
    struct.pack_into("<H", container, 18, elf.EM_WRM)
    try:
        return elf.read_object(container, name)
    except (elf.ElfError, ValueError, IndexError, struct.error) as error:
        raise wcc.TranslationError(f"{name}: invalid ELF object: {error}") from error


def used_registers(ins):
    if ins.op in wcc.REGISTER_OPS:
        return (ins.rd, ins.rs1, ins.rs2)
    if ins.op in ("sb", "sh", "sw") or ins.op in wcc.BRANCH_OPS:
        return (ins.rs1, ins.rs2)
    if ins.op in wcc.IMMEDIATE_OPS or ins.op in wcc.MEMORY_OPS or ins.op == "jalr":
        return (ins.rd, ins.rs1)
    if ins.op in ("lui", "auipc", "jal"):
        return (ins.rd,)
    return ()


def convert(data, name="<RISC-V object>"):
    obj = read_riscv(data, name)
    sections, section_indices, code_maps = [], {}, {}
    pending = []
    symbols = [elf.Symbol(s.name, s.value, s.shndx, s.bind, s.type, s.size) for s in obj.symbols]
    symbols.append(elf.Symbol(".wcc.native", 1, elf.SHN_ABS))

    def fail(message):
        raise wcc.TranslationError(f"{name}: {message}")

    def local_target(section_index, value):
        symbols.append(elf.Symbol(f".wcc.target.{len(symbols)}", value, section_index))
        return len(symbols) - 1

    for index, original in enumerate(obj.sections):
        if original is None or not original.flags & elf.SHF_ALLOC:
            continue
        if original.flags & elf.SHF_TLS:
            fail("TLS sections are not supported by native C object conversion")
        section_indices[index] = len(sections) + 1
        new = elf.Section(original.name, original.type, original.flags & ~0x30, original.align,
                          None if original.nobits else bytearray(original.data), original.size)
        sections.append(new)
        if not original.flags & elf.SHF_EXECINSTR:
            for offset, kind, symbol, addend in original.relocs:
                if kind == 0:
                    continue
                if kind != 1:
                    fail(f"unsupported data relocation {kind} in {original.name}+0x{offset:x}")
                pending.append((new, offset, "R_WRM_32", symbol, addend))
            continue
        if original.nobits or original.size % 4:
            fail(f"{original.name}: code must contain aligned 32-bit instructions")
        relocations = {}
        for relocation in original.relocs:
            offset, kind, _, _ = relocation
            if kind in (0, 51):  # NONE and RELAX
                continue
            if offset in relocations:
                fail(f"multiple instruction relocations at {original.name}+0x{offset:x}")
            relocations[offset] = relocation
        words, offsets = [], {}
        consumed = set()
        for offset in range(0, original.size, 4):
            offsets[offset] = len(words) * 4
            if offset in consumed:
                continue
            word = struct.unpack_from("<I", original.data, offset)[0]
            ins = wcc.decode(word, offset)
            reserved = set(used_registers(ins)) & set(FIXED)
            if reserved:
                fail(f"{original.name}+0x{offset:x}: uses reserved RV registers {sorted(reserved)}; "
                     "recompile C with wcc.py -c (the native WRM ABI flags are required)")
            rd, a, b = REGISTERS[ins.rd], REGISTERS[ins.rs1], REGISTERS[ins.rs2]
            relocation = relocations.get(offset)
            kind, symbol, addend = relocation[1:] if relocation else (None, None, None)
            start = len(words) * 4

            def emit_reloc(position, relocation_name, target=symbol, extra=addend):
                pending.append((new, start + position * 4, relocation_name, target, extra))

            if kind in (18, 19):  # CALL / CALL_PLT: AUIPC + JALR
                if ins.op != "auipc" or offset + 8 > original.size:
                    fail("invalid RISC-V call relocation")
                next_word = struct.unpack_from("<I", original.data, offset + 4)[0]
                following = wcc.decode(next_word, offset + 4)
                if following.op != "jalr" or following.rs1 != ins.rd or following.imm != 0:
                    fail("invalid AUIPC/JALR call pair")
                if offset + 4 in relocations:
                    fail("unexpected relocation inside an AUIPC/JALR call pair")
                if set(used_registers(following)) & set(FIXED):
                    fail("call pair uses a reserved register; recompile with wcc.py -c")
                words += [0x30 | rd << 8, wcc.i_type(0x23, rd, rd, 0),
                          wcc.i_type(0x61, REGISTERS[following.rd], rd, 0)]
                emit_reloc(0, "R_WRM_HI19")
                emit_reloc(1, "R_WRM_LO13")
                consumed.add(offset + 4)
            elif kind in (23, 26):  # PCREL_HI20 / HI20
                if ins.op != ("auipc" if kind == 23 else "lui"):
                    fail("upper relocation does not match its instruction")
                # Static native objects use absolute addressing. A matching
                # low relocation supplies WRM's low 13 bits to its consumer.
                words.append(0x30 | rd << 8)
                emit_reloc(0, "R_WRM_HI19")
            elif ins.op in wcc.REGISTER_OPS:
                words.append(wcc.r_type(wcc.REGISTER_OPS[ins.op], rd, a, b))
            elif ins.op in wcc.IMMEDIATE_OPS:
                if ins.op in ("andi", "ori", "xori") and ins.imm < 0 and rd:
                    mask = ins.imm & wcc.MASK
                    cleared = (~mask) & wcc.MASK
                    if ins.op == "andi" and cleared & (cleared + 1) == 0:
                        shift = cleared.bit_length()
                        words += [wcc.i_type(0x26, rd, a, shift), wcc.i_type(0x25, rd, rd, shift)]
                    else:
                        if 30 in (rd, a):
                            fail("unsupported logical stack-pointer operation")
                        temp = next(r for r in range(1, 10) if r not in (rd, a))
                        words += [wcc.i_type(0x20, 30, 30, -16), wcc.i_type(0x4A, temp, 30, 0)]
                        words += wcc.constant(temp, ins.imm)
                        words += [wcc.r_type(wcc.REGISTER_OPS[ins.op[:-1]], rd, a, temp),
                                  wcc.i_type(0x44, temp, 30, 0), wcc.i_type(0x20, 30, 30, 16)]
                elif rd == 0 and ins.op in ("andi", "ori", "xori"):
                    words.append(1)
                else:
                    words.append(wcc.i_type(wcc.IMMEDIATE_OPS[ins.op], rd, a, ins.imm))
            elif ins.op in wcc.MEMORY_OPS:
                words.append(wcc.i_type(wcc.MEMORY_OPS[ins.op], b if ins.op in ("sb", "sh", "sw") else rd,
                                        a, ins.imm))
            elif ins.op in ("lui", "auipc"):
                if ins.op == "lui":
                    words += wcc.constant(rd, ins.imm) or [1]
                else:
                    words.append(0x31 | rd << 8 | (ins.imm & 0xFFFFE000))
                    if ins.imm & 0x1000:
                        words.append(wcc.i_type(0x20, rd, rd, 4096))
            elif ins.op in wcc.BRANCH_OPS or ins.op == "jal":
                words.append(wcc.i_type(wcc.BRANCH_OPS[ins.op], a, b, 0) if ins.op != "jal" else 0x60 | rd << 8)
                if kind is None:
                    symbol, addend = local_target(index, offset + ins.imm), 0
                elif kind != (17 if ins.op == "jal" else 16):
                    fail("branch relocation does not match its instruction")
                emit_reloc(0, "R_WRM_JAL19" if ins.op == "jal" else "R_WRM_BRANCH14", symbol, addend)
            elif ins.op == "jalr":
                words.append(wcc.i_type(0x61, rd, a, ins.imm))
            else:
                words.append({"fence": 8, "ecall": 7, "ebreak": 9}[ins.op])

            if kind in (24, 25, 27, 28):  # PCREL_LO12_I/S / LO12_I/S
                expected_store = kind in (25, 28)
                valid_consumer = ins.op in wcc.MEMORY_OPS or ins.op in ("addi", "jalr")
                if not valid_consumer or expected_store != (ins.op in ("sb", "sh", "sw")) or len(words) * 4 - start != 4:
                    fail("low relocation does not match a single immediate instruction")
                if kind in (24, 25):
                    anchor = obj.symbols[symbol]
                    if anchor.shndx != index or addend != 0:
                        fail("PC-relative low relocation must reference an anchor in the same code section")
                    upper = relocations.get(anchor.value)
                    if upper is None or upper[1] != 23:
                        fail("PC-relative low relocation has no matching upper relocation")
                    symbol, addend = upper[2:]
                pending.append((new, start, "R_WRM_LO13", symbol, addend))
            elif kind not in (None, 16, 17, 18, 19, 23, 26):
                fail(f"unsupported instruction relocation {kind} at {original.name}+0x{offset:x}")
        offsets[original.size] = len(words) * 4
        code_maps[index] = offsets
        new.data = bytearray(struct.pack(f"<{len(words)}I", *words))
        new.size = len(new.data)

    def adjusted(symbol, addend):
        old_section = symbol.shndx
        if old_section in code_maps:
            offsets = code_maps[old_section]
            target = symbol.value + addend
            if target not in offsets:
                fail(f"code relocation for '{symbol.name}' points inside an instruction or outside its section")
            return offsets[target] - offsets[symbol.value]
        return addend

    for new, offset, kind, symbol_index, addend in pending:
        if symbol_index >= len(symbols):
            fail("relocation refers to an invalid symbol")
        new.relocs.append((offset, elf.R[kind], symbol_index, adjusted(symbols[symbol_index], addend)))
    dropped = set()
    used_symbols = {symbol for section in sections for _, _, symbol, _ in section.relocs}
    for symbol_index, symbol in enumerate(symbols[1:], 1):
        old_section = symbol.shndx
        if old_section in code_maps:
            offsets = code_maps[old_section]
            if symbol.value not in offsets:
                fail(f"symbol '{symbol.name}' points inside an instruction")
            old_value = symbol.value
            symbol.value = offsets[old_value]
            if symbol.size:
                if old_value + symbol.size not in offsets:
                    fail(f"invalid code size for symbol '{symbol.name}'")
                symbol.size = offsets[old_value + symbol.size] - symbol.value
        if old_section not in (elf.SHN_UNDEF, elf.SHN_ABS):
            if old_section not in section_indices:
                if symbol_index in used_symbols or symbol.bind != elf.STB_LOCAL:
                    fail(f"symbol '{symbol.name}' uses an unsupported section")
                dropped.add(symbol_index)
                continue
            symbol.shndx = section_indices[old_section]
    order = sorted((i for i in range(1, len(symbols)) if i not in dropped),
                   key=lambda i: symbols[i].bind != elf.STB_LOCAL)
    remap = {0: 0, **{old: new for new, old in enumerate(order, 1)}}
    for section in sections:
        section.relocs = [(off, kind, remap[sym], add) for off, kind, sym, add in section.relocs]
    return elf.write_relocatable(sections, [symbols[i] for i in order])


def compile_object(source, output, prefix="riscv64-unknown-elf-", extra_flags=()):
    source, output = Path(source), Path(output)
    if source.resolve() == output.resolve():
        raise wcc.TranslationError("output must not overwrite its source")
    with tempfile.TemporaryDirectory(prefix="wcc-object-") as directory:
        raw_object = Path(directory) / "riscv.o"
        command = [prefix + "gcc", *extra_flags, *FLAGS, "-c", str(source), "-o", str(raw_object)]
        completed = subprocess.run(command, capture_output=True, text=True)
        if completed.returncode:
            raise wcc.TranslationError(completed.stderr.strip())
        result = convert(raw_object.read_bytes(), str(source))
        wcc.write_atomic(output, result)


def object_main(argv):
    import argparse
    parser = argparse.ArgumentParser(description="Compile C into a native WRM object compatible with M")
    parser.add_argument("-c", action="store_true", required=True)
    parser.add_argument("source", type=Path)
    parser.add_argument("-o", "--output", type=Path, required=True)
    parser.add_argument("-I", dest="includes", action="append", default=[])
    parser.add_argument("-D", dest="defines", action="append", default=[])
    parser.add_argument("--toolchain-prefix", default="riscv64-unknown-elf-")
    args = parser.parse_args(argv)
    flags = [flag for value in args.includes for flag in ("-I", value)]
    flags += [flag for value in args.defines for flag in ("-D", value)]
    try:
        compile_object(args.source, args.output, args.toolchain_prefix, flags)
    except (OSError, wcc.TranslationError) as error:
        print(f"wcc: {error}", file=sys.stderr)
        return 1
    return 0
