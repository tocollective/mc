#!/usr/bin/env python3
"""Compile C to native WRM objects, or translate a raw RV32IM code image."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import struct
import sys
import tempfile


MASK = 0xFFFFFFFF
SLOT_WORDS = 32
SLOT_BYTES = SLOT_WORDS * 4
HEADER_BYTES = 16

REGISTER_OPS = {
    "add": 0x10, "sub": 0x11, "and": 0x12, "or": 0x13,
    "xor": 0x14, "sll": 0x15, "srl": 0x16, "sra": 0x17,
    "slt": 0x18, "sltu": 0x19, "mul": 0x1A, "div": 0x1B,
    "divu": 0x1C, "rem": 0x1D, "remu": 0x1E, "mulh": 0x1F,
    "mulhu": 0x2A, "mulhsu": 0x2B,
}
IMMEDIATE_OPS = {
    "addi": 0x20, "andi": 0x22, "ori": 0x23, "xori": 0x24,
    "slli": 0x25, "srli": 0x26, "srai": 0x27,
    "slti": 0x28, "sltiu": 0x29,
}
MEMORY_OPS = {
    "lb": 0x40, "lbu": 0x41, "lh": 0x42, "lhu": 0x43,
    "lw": 0x44, "sb": 0x48, "sh": 0x49, "sw": 0x4A,
}
BRANCH_OPS = {"beq": 0x50, "bne": 0x51, "blt": 0x52,
              "bge": 0x53, "bltu": 0x54, "bgeu": 0x55}
# WRM floating point (binary32 in the general registers), R-type. The
# native C path uses these in place of the soft-float helpers of libgcc.
FLOAT_OPS = {
    "fadd": 0x70, "fsub": 0x71, "fmul": 0x72, "fdiv": 0x73, "fsqrt": 0x74,
    "fmin": 0x75, "fmax": 0x76, "fsgnjn": 0x7A, "fsgnjx": 0x7B,
    "feq": 0x80, "flt": 0x81, "fle": 0x82,
    "ftoi": 0x84, "ftou": 0x85, "itof": 0x86, "utof": 0x87,
}


class TranslationError(ValueError):
    pass


def signed(value: int, bits: int) -> int:
    return (value & ((1 << (bits - 1)) - 1)) - (value & (1 << (bits - 1)))


@dataclass(frozen=True)
class Instruction:
    op: str
    rd: int = 0
    rs1: int = 0
    rs2: int = 0
    imm: int = 0


def decode(word: int, pc: int = 0) -> Instruction:
    opcode = word & 0x7F
    rd, rs1, rs2 = (word >> 7) & 31, (word >> 15) & 31, (word >> 20) & 31
    f3, f7 = (word >> 12) & 7, word >> 25
    imm = signed(word >> 20, 12)
    op = None
    if opcode == 0x33:
        if f7 == 0:
            op = ("add", "sll", "slt", "sltu", "xor", "srl", "or", "and")[f3]
        elif f7 == 0x20:
            op = {0: "sub", 5: "sra"}.get(f3)
        elif f7 == 1:
            op = ("mul", "mulh", "mulhsu", "mulhu", "div", "divu", "rem", "remu")[f3]
    elif opcode == 0x13:
        op = {0: "addi", 2: "slti", 3: "sltiu", 4: "xori",
              6: "ori", 7: "andi"}.get(f3)
        if f3 == 1 and f7 == 0:
            op, imm = "slli", rs2
        elif f3 == 5 and f7 in (0, 0x20):
            op, imm = ("srli" if f7 == 0 else "srai"), rs2
    elif opcode == 0x03:
        op = {0: "lb", 1: "lh", 2: "lw", 4: "lbu", 5: "lhu"}.get(f3)
    elif opcode == 0x23:
        op = {0: "sb", 1: "sh", 2: "sw"}.get(f3)
        imm = signed(((word >> 25) << 5) | ((word >> 7) & 31), 12)
    elif opcode == 0x63:
        op = {0: "beq", 1: "bne", 4: "blt", 5: "bge",
              6: "bltu", 7: "bgeu"}.get(f3)
        imm = signed(((word >> 31) << 12) | (((word >> 7) & 1) << 11)
                     | (((word >> 25) & 63) << 5) | (((word >> 8) & 15) << 1), 13)
    elif opcode in (0x37, 0x17):
        op, imm = ("lui" if opcode == 0x37 else "auipc"), word & 0xFFFFF000
    elif opcode == 0x6F:
        op = "jal"
        imm = signed(((word >> 31) << 20) | (((word >> 12) & 255) << 12)
                     | (((word >> 20) & 1) << 11) | (((word >> 21) & 1023) << 1), 21)
    elif opcode == 0x67 and f3 == 0:
        op = "jalr"
    elif opcode == 0x0F and f3 == 0:
        # A full WRM fence also satisfies weaker predecessor/successor masks.
        op = "fence"
    elif word in (0x00000073, 0x00100073):
        op = "ecall" if word == 0x73 else "ebreak"
    if op is None:
        raise TranslationError(f"0x{pc:08x}: unsupported or invalid RV32IM instruction 0x{word:08x}")
    return Instruction(op, rd, rs1, rs2, imm)


def r_type(op: int, rd: int, rs1: int, rs2: int) -> int:
    return op | (rd << 8) | (rs1 << 13) | (rs2 << 18)


def i_type(op: int, rd: int, rs1: int, imm: int) -> int:
    if not -8192 <= imm <= 16383:
        raise TranslationError("WRM immediate does not fit 14 bits")
    return op | (rd << 8) | (rs1 << 13) | ((imm & 0x3FFF) << 18)


def jump(offset: int) -> int:
    if not -(1 << 18) <= offset < (1 << 18):
        raise TranslationError("WRM direct jump is out of range")
    return 0x60 | ((offset & 0x7FFFF) << 13)


def constant(rd: int, value: int) -> list[int]:
    value &= MASK
    if rd == 0:
        return []
    if -8192 <= signed(value, 32) <= 8191:
        return [i_type(0x20, rd, 0, signed(value, 32))]
    words = [0x30 | (rd << 8) | ((value >> 13) << 13)]
    if value & 0x1FFF:
        words.append(i_type(0x23, rd, rd, value & 0x1FFF))
    return words


@dataclass(frozen=True)
class Config:
    source_base: int = 0x10000
    output_base: int = 0x100000
    scratch: int = 0x1000
    entry: int | None = None


@dataclass(frozen=True)
class Translation:
    binary: bytes
    address_map: dict[int, int]
    config: Config


def translate(data: bytes, config: Config = Config()) -> Translation:
    if data.startswith(b"\x7fELF"):
        raise TranslationError(
            "ELF input detected; raw translation accepts RV32IM code, not .o/.elf files. "
            "Compile with -march=rv32im -mabi=ilp32, link all symbols, then "
            "extract .text with objcopy -O binary -j .text. "
            "For a runnable C program, use build_rom.py source.c -o program.rom."
        )
    if not data or len(data) % 4:
        raise TranslationError("input must contain a nonempty sequence of 4-byte instructions")
    count = len(data) // 4
    output_size = HEADER_BYTES + (count + 1) * SLOT_BYTES
    for name, base, size in (("source", config.source_base, len(data)),
                             ("output", config.output_base, output_size),
                             ("scratch", config.scratch, 8)):
        if base < 0 or base % 4 or base + size > 1 << 32:
            raise TranslationError(f"{name} range must be aligned and fit the 32-bit address space")
    if config.scratch + 4 > 8191:
        raise TranslationError("scratch words must fit WRM signed offsets from r0 (0..8184)")
    ranges = [(config.source_base, config.source_base + len(data)),
              (config.output_base, config.output_base + output_size),
              (config.scratch, config.scratch + 8)]
    for index, (start, end) in enumerate(ranges):
        for other_start, other_end in ranges[index + 1:]:
            if start < other_end and other_start < end:
                raise TranslationError("source, output and scratch ranges must not overlap")
    entry = config.source_base if config.entry is None else config.entry
    if entry % 4 or not config.source_base <= entry < config.source_base + len(data):
        raise TranslationError("entry must name an aligned instruction in the source image")
    instructions = [decode(word[0], config.source_base + index * 4)
                    for index, word in enumerate(struct.iter_unpack("<I", data))]
    text_base = config.output_base + HEADER_BYTES
    mapping = {config.source_base + index * 4: text_base + index * SLOT_BYTES
               for index in range(count + 1)}
    save31 = i_type(0x4A, 31, 0, config.scratch)
    restore31 = i_type(0x44, 31, 0, config.scratch)

    def absolute_jump(target: int) -> list[int]:
        return constant(31, target) + [i_type(0x61, 0, 31, 0)]

    # Enter through the header exactly once, preserving the initial x31.
    header = [save31] + absolute_jump(mapping[entry])
    header += [0x01] * (HEADER_BYTES // 4 - len(header))
    words = header
    for index, ins in enumerate(instructions):
        pc = config.source_base + index * 4
        block_pc = mapping[pc]
        block = [restore31]
        op, rd, a, b, imm = ins.op, ins.rd, ins.rs1, ins.rs2, ins.imm
        terminated = False
        if op in REGISTER_OPS:
            block.append(r_type(REGISTER_OPS[op], rd, a, b))
        elif op in IMMEDIATE_OPS:
            if op in ("andi", "ori", "xori") and imm < 0 and rd:
                # WRM logical immediates are zero-extended; RV immediates are signed.
                temp = next(reg for reg in range(1, 31) if reg not in (rd, a))
                block.append(i_type(0x4A, temp, 0, config.scratch + 4))
                block += constant(temp, imm)
                block.append(r_type(REGISTER_OPS[op[:-1]], rd, a, temp))
                block.append(i_type(0x44, temp, 0, config.scratch + 4))
            elif op in ("andi", "ori", "xori") and rd == 0:
                block.append(0x01)
            else:
                block.append(i_type(IMMEDIATE_OPS[op], rd, a, imm))
        elif op in MEMORY_OPS:
            block.append(i_type(MEMORY_OPS[op], b if op in ("sb", "sh", "sw") else rd, a, imm))
        elif op in ("lui", "auipc"):
            block += constant(rd, imm + (pc if op == "auipc" else 0))
        elif op in BRANCH_OPS or op == "jal":
            target = (pc + imm) & MASK
            if target not in mapping:
                raise TranslationError(f"0x{pc:08x}: {op} target 0x{target:08x} is outside the image or misaligned")
            if op == "jal":
                block += constant(rd, pc + 4)
            block.append(save31)
            transfer = absolute_jump(mapping[target])
            if op in BRANCH_OPS:
                block.append(i_type(BRANCH_OPS[op] ^ 1, a, b, len(transfer) + 1))
            block += transfer
            terminated = op == "jal"
        elif op == "jalr":
            temp = next(reg for reg in range(1, 31) if reg not in (rd, a))
            block += [i_type(0x4A, temp, 0, config.scratch + 4),
                      i_type(0x20, temp, a, imm),
                      i_type(0x26, temp, temp, 1), i_type(0x25, temp, temp, 1), save31,
                      i_type(0x22, 31, temp, 3)]
            alignment_branch = len(block)
            block.append(0)
            block += constant(31, config.source_base)
            block.append(r_type(0x11, temp, temp, 31))
            # Include the terminal sentinel as a valid target.
            block += constant(31, len(data) + 1)
            block.append(r_type(0x19, 31, temp, 31))
            range_branch = len(block)
            block.append(0)
            if rd == 31:
                block += constant(31, pc + 4)
                block.append(save31)
            else:
                block += constant(rd, pc + 4)
            block.append(i_type(0x25, temp, temp, 5))
            block += constant(31, text_base)
            block.append(r_type(0x10, 31, temp, 31))
            block += [i_type(0x44, temp, 0, config.scratch + 4), i_type(0x61, 0, 31, 0)]
            fault = len(block)
            block += [i_type(0x44, temp, 0, config.scratch + 4), restore31, 0x09, jump(-1)]
            block[alignment_branch] = i_type(0x51, 31, 0, fault - alignment_branch)
            block[range_branch] = i_type(0x50, 31, 0, fault - range_branch)
            terminated = True
        elif op in ("ecall", "ebreak", "fence"):
            block.append({"ecall": 0x07, "ebreak": 0x09, "fence": 0x08}[op])
        if not terminated:
            block.append(save31)
            next_pc = mapping[pc + 4]
            block.append(jump((next_pc - (block_pc + len(block) * 4)) // 4))
        if len(block) > SLOT_WORDS:
            raise TranslationError(f"0x{pc:08x}: internal error: translation exceeds its slot")
        words += block + [0x01] * (SLOT_WORDS - len(block))
    words += [restore31, 0x00] + [0x01] * (SLOT_WORDS - 2)
    return Translation(struct.pack(f"<{len(words)}I", *words), mapping, config)


def write_atomic(path: Path, data: bytes) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def number(value: str) -> int:
    try:
        return int(value, 0)
    except ValueError as error:
        raise argparse.ArgumentTypeError("expected a decimal or 0x-prefixed integer") from error


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "-c" in argv:
        from native_objects import object_main
        return object_main(argv)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="raw little-endian RV32IM instructions")
    parser.add_argument("-o", "--output", type=Path, required=True, help="raw WRM output image")
    parser.add_argument("--source-base", type=number, default=0x10000, help="original RV address (default: 0x10000)")
    parser.add_argument("--output-base", type=number, default=0x100000, help="WRM load/entry address (default: 0x100000)")
    parser.add_argument("--scratch", type=number, default=0x1000, help="two reserved writable words (default: 0x1000)")
    parser.add_argument("--entry", type=number, help="original RV entry address (default: source base)")
    parser.add_argument("--map", type=Path, help="write a JSON address map")
    args = parser.parse_args(argv)
    try:
        paths = [args.input.resolve(), args.output.resolve()]
        if args.map is not None:
            paths.append(args.map.resolve())
        if len(set(paths)) != len(paths):
            raise TranslationError("input, output and map paths must be different")
        result = translate(args.input.read_bytes(), Config(args.source_base, args.output_base, args.scratch, args.entry))
        map_data = json.dumps({
            "source_base": args.source_base, "output_base": args.output_base,
            "entry": args.output_base, "source_entry": args.entry if args.entry is not None else args.source_base,
            "scratch": args.scratch, "scratch_size": 8, "slot_bytes": SLOT_BYTES,
            "addresses": {f"0x{source:08x}": f"0x{target:08x}" for source, target in result.address_map.items()},
        }, indent=2).encode() + b"\n"
        write_atomic(args.output, result.binary)
        if args.map is not None:
            write_atomic(args.map, map_data)
    except (OSError, TranslationError) as error:
        print(f"wcc: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
