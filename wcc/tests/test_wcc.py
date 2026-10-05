"""Semantic checks using small independent RV32IM and WRM execution models."""

import json
import os
from pathlib import Path
import random
import re
import struct
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import wcc


MASK = 0xFFFFFFFF


def s32(value):
    return value if value < 0x80000000 else value - 0x100000000


def sext(value, bits):
    return value - (1 << bits) if value & (1 << (bits - 1)) else value


def arithmetic(op, a, b):
    sa, sb = s32(a), s32(b)
    if op == "add": return a + b
    if op == "sub": return a - b
    if op == "and": return a & b
    if op == "or": return a | b
    if op == "xor": return a ^ b
    if op == "sll": return a << (b & 31)
    if op == "srl": return a >> (b & 31)
    if op == "sra": return sa >> (b & 31)
    if op == "slt": return int(sa < sb)
    if op == "sltu": return int(a < b)
    if op == "mul": return a * b
    if op == "mulh": return (sa * sb) >> 32
    if op == "mulhu": return (a * b) >> 32
    if op == "mulhsu": return (sa * b) >> 32
    if op in ("div", "rem"):
        q = -1 if not b else (abs(sa) // abs(sb)) * (-1 if (sa < 0) != (sb < 0) else 1)
        return q if op == "div" else (sa if not b else sa - q * sb)
    if op == "divu": return a // b if b else MASK
    if op == "remu": return a % b if b else a
    raise AssertionError(op)


def condition(op, a, b):
    return {"beq": a == b, "bne": a != b, "blt": s32(a) < s32(b),
            "bge": s32(a) >= s32(b), "bltu": a < b, "bgeu": a >= b}[op]


# Test descriptions and encoders intentionally do not use the production decoder.
def ins(op, rd=0, a=0, b=0, imm=0):
    return op, rd, a, b, imm


def encode(instruction):
    op, rd, a, b, imm = instruction
    regs = {"add": (0, 0), "sub": (0, 32), "sll": (1, 0), "slt": (2, 0),
            "sltu": (3, 0), "xor": (4, 0), "srl": (5, 0), "sra": (5, 32),
            "or": (6, 0), "and": (7, 0), "mul": (0, 1), "mulh": (1, 1),
            "mulhsu": (2, 1), "mulhu": (3, 1), "div": (4, 1),
            "divu": (5, 1), "rem": (6, 1), "remu": (7, 1)}
    if op in regs:
        f3, f7 = regs[op]
        return 0x33 | rd << 7 | f3 << 12 | a << 15 | b << 20 | f7 << 25
    immediate = {"addi": 0, "slli": 1, "slti": 2, "sltiu": 3,
                 "xori": 4, "srli": 5, "srai": 5, "ori": 6, "andi": 7}
    if op in immediate:
        value = imm | (0x400 if op == "srai" else 0)
        return 0x13 | rd << 7 | immediate[op] << 12 | a << 15 | (value & 4095) << 20
    if op in ("lui", "auipc"):
        return (0x37 if op == "lui" else 0x17) | rd << 7 | (imm & 0xFFFFF000)
    if op in ("lb", "lh", "lw", "lbu", "lhu"):
        f3 = {"lb": 0, "lh": 1, "lw": 2, "lbu": 4, "lhu": 5}[op]
        return 3 | rd << 7 | f3 << 12 | a << 15 | (imm & 4095) << 20
    if op in ("sb", "sh", "sw"):
        f3 = {"sb": 0, "sh": 1, "sw": 2}[op]
        return 0x23 | (imm & 31) << 7 | f3 << 12 | a << 15 | b << 20 | ((imm & 4095) >> 5) << 25
    if op in ("beq", "bne", "blt", "bge", "bltu", "bgeu"):
        f3 = {"beq": 0, "bne": 1, "blt": 4, "bge": 5, "bltu": 6, "bgeu": 7}[op]
        value = imm & 8191
        return (0x63 | ((value >> 11) & 1) << 7 | ((value >> 1) & 15) << 8
                | f3 << 12 | a << 15 | b << 20 | ((value >> 5) & 63) << 25
                | (value >> 12) << 31)
    if op == "jal":
        value = imm & 0x1FFFFF
        return (0x6F | rd << 7 | ((value >> 12) & 255) << 12 | ((value >> 11) & 1) << 20
                | ((value >> 1) & 1023) << 21 | (value >> 20) << 31)
    if op == "jalr":
        return 0x67 | rd << 7 | a << 15 | (imm & 4095) << 20
    return {"ecall": 0x73, "ebreak": 0x100073, "fence": 0x0FF0000F}[op]


def image(program):
    return b"".join(struct.pack("<I", encode(instruction)) for instruction in program)


def read(memory, address, size):
    if address % size:
        raise AssertionError("unaligned memory access")
    return sum(memory.get(address + offset, 0) << (8 * offset) for offset in range(size))


def store(memory, address, value, size):
    if address % size:
        raise AssertionError("unaligned memory access")
    for offset in range(size):
        memory[address + offset] = (value >> (offset * 8)) & 255


def run_rv(program, config, initial, memory):
    regs = initial.copy()
    memory = memory.copy()
    pc = config.source_base if config.entry is None else config.entry
    for _ in range(10000):
        index = (pc - config.source_base) // 4
        if index == len(program): return regs, memory, "halt"
        if pc % 4 or not 0 <= index < len(program): raise AssertionError("invalid reference PC")
        op, rd, a, b, imm = program[index]
        a, b = regs[a], regs[b]
        next_pc = pc + 4
        if op in ("ecall", "ebreak"): return regs, memory, op
        if op in ("beq", "bne", "blt", "bge", "bltu", "bgeu"):
            if condition(op, a, b): next_pc = (pc + imm) & MASK
        elif op == "jal":
            regs[rd] = next_pc & MASK
            next_pc = (pc + imm) & MASK
        elif op == "jalr":
            target = ((a + imm) & MASK) & ~1
            regs[rd] = next_pc & MASK
            next_pc = target
        elif op in ("lui", "auipc"):
            regs[rd] = (imm + (pc if op == "auipc" else 0)) & MASK
        elif op.startswith("l"):
            size = {"lb": 1, "lbu": 1, "lh": 2, "lhu": 2, "lw": 4}[op]
            value = read(memory, (a + imm) & MASK, size)
            regs[rd] = (sext(value, size * 8) if op in ("lb", "lh") else value) & MASK
        elif op in ("sb", "sh", "sw"):
            store(memory, (a + imm) & MASK, b, {"sb": 1, "sh": 2, "sw": 4}[op])
        elif op != "fence":
            immediate = op in ("addi", "andi", "ori", "xori", "slti", "sltiu", "slli", "srli", "srai")
            base_op = "sltu" if op == "sltiu" else (op[:-1] if immediate else op)
            regs[rd] = arithmetic(base_op, a, imm & MASK if immediate else b) & MASK
        regs[0] = 0
        pc = next_pc
    raise AssertionError("reference execution did not finish")


def run_wrm(binary, config, initial, memory):
    regs = initial.copy()
    memory = memory.copy()
    code = dict(enumerate(binary, config.output_base))
    pc = config.output_base
    alu = {0x10: "add", 0x11: "sub", 0x12: "and", 0x13: "or", 0x14: "xor",
           0x15: "sll", 0x16: "srl", 0x17: "sra", 0x18: "slt", 0x19: "sltu",
           0x1A: "mul", 0x1B: "div", 0x1C: "divu", 0x1D: "rem", 0x1E: "remu",
           0x1F: "mulh", 0x2A: "mulhu", 0x2B: "mulhsu"}
    immediate = {0x20: "add", 0x22: "and", 0x23: "or", 0x24: "xor",
                 0x25: "sll", 0x26: "srl", 0x27: "sra", 0x28: "slt", 0x29: "sltu"}
    for _ in range(100000):
        if pc % 4 or pc not in code: raise AssertionError(f"invalid WRM PC: {pc:#x}")
        word = read(code, pc, 4)
        op, rd, rs1, rs2 = word & 255, (word >> 8) & 31, (word >> 13) & 31, (word >> 18) & 31
        a, b = regs[rs1], regs[rs2]
        imm = sext(word >> 18, 14)
        next_pc = pc + 4
        if op in (0, 7, 9): return regs, memory, {0: "halt", 7: "ecall", 9: "ebreak"}[op]
        if op in alu:
            if word >> 23: raise AssertionError("reserved R bits set")
            regs[rd] = arithmetic(alu[op], a, b) & MASK
        elif op in immediate:
            value = word >> 18 if op in (0x22, 0x23, 0x24, 0x25, 0x26, 0x27) else imm & MASK
            regs[rd] = arithmetic(immediate[op], a, value) & MASK
        elif op == 0x30:
            regs[rd] = word & 0xFFFFE000
        elif 0x40 <= op <= 0x44:
            size = {0x40: 1, 0x41: 1, 0x42: 2, 0x43: 2, 0x44: 4}[op]
            value = read(memory, (a + imm) & MASK, size)
            regs[rd] = (sext(value, size * 8) if op in (0x40, 0x42) else value) & MASK
        elif op in (0x48, 0x49, 0x4A):
            store(memory, (a + imm) & MASK, regs[rd], {0x48: 1, 0x49: 2, 0x4A: 4}[op])
        elif 0x50 <= op <= 0x55:
            if condition(("beq", "bne", "blt", "bge", "bltu", "bgeu")[op - 0x50], regs[rd], a):
                next_pc = (pc + imm * 4) & MASK
        elif op == 0x60:
            regs[rd] = next_pc & MASK
            next_pc = (pc + sext(word >> 13, 19) * 4) & MASK
        elif op == 0x61:
            regs[rd] = next_pc & MASK
            next_pc = ((a + imm) & MASK) & ~3
        elif op not in (1, 8): raise AssertionError(f"unknown WRM opcode {op:#x}")
        regs[0] = 0
        pc = next_pc
    raise AssertionError("WRM execution did not finish")


class TranslatorTests(unittest.TestCase):
    def compare(self, program, initial=None, config=wcc.Config(), memory=None):
        initial = initial or [0] + [(reg * 0x1234567) & MASK for reg in range(1, 32)]
        memory = (memory or {}).copy()
        data = image(program)
        memory.update(dict(enumerate(data, config.source_base)))
        result = wcc.translate(data, config)
        expected_regs, expected_mem, expected_stop = run_rv(program, config, initial, memory)
        regs, actual_mem, stop = run_wrm(result.binary, config, initial, memory)
        self.assertEqual(stop, expected_stop)
        self.assertEqual(regs, expected_regs)
        for address in range(config.scratch, config.scratch + 8): actual_mem.pop(address, None)
        self.assertEqual(actual_mem, expected_mem)
        return result

    def test_all_register_operations(self):
        rng = random.Random(81732)
        values = [0, 1, MASK, 0x80000000, 0x7FFFFFFF, 31, 32] + [rng.getrandbits(32) for _ in range(8)]
        for op in wcc.REGISTER_OPS:
            for a in values:
                for b in values:
                    regs = [0] + [rng.getrandbits(32) for _ in range(31)]
                    regs[30], regs[31] = a, b
                    with self.subTest(op=op, a=a, b=b):
                        self.compare([ins(op, 30, 30, 31)], regs)
                        self.compare([ins(op, 31, 30, 31)], regs)

    def test_immediates_and_zero_register(self):
        for op in wcc.IMMEDIATE_OPS:
            values = [0, 1, 31] if op in ("slli", "srli", "srai") else [-2048, -1, 0, 1, 2047]
            for value in values:
                for rd, a in ((1, 1), (31, 31), (1, 31), (0, 31), (31, 0)):
                    with self.subTest(op=op, imm=value, rd=rd, a=a):
                        self.compare([ins(op, rd, a, imm=value)])

    def test_upper_immediates(self):
        for base in (0x10000, 0x81234000, 0xFFFFF000):
            for value in (0, 0x1000, 0x80000000, 0xFFFFF000):
                self.compare([ins("lui", 31, imm=value), ins("auipc", 1, imm=value)], config=wcc.Config(source_base=base))

    def test_memory_and_code_reads(self):
        program = [ins("addi", 2, imm=0x700), ins("addi", 31, imm=-128),
                   ins("sb", a=2, b=31, imm=-4), ins("lb", 3, 2, imm=-4), ins("lbu", 4, 2, imm=-4),
                   ins("sh", a=2, b=31, imm=-2), ins("lh", 5, 2, imm=-2), ins("lhu", 6, 2, imm=-2),
                   ins("sw", a=2, b=31, imm=4), ins("lw", 31, 2, imm=4), ins("lw", 0, 2, imm=4),
                   ins("auipc", 7), ins("lw", 8, 7, imm=-44), ins("fence")]
        self.compare(program)

    def test_branches_both_outcomes_and_loop(self):
        for op in wcc.BRANCH_OPS:
            for a, b in ((0, 0), (MASK, 1), (1, MASK)):
                regs = [0] * 32
                regs[30], regs[31] = a, b
                self.compare([ins(op, a=30, b=31, imm=8), ins("addi", 1, imm=9), ins("addi", 2, imm=7)], regs)
        self.compare([ins("addi", 31, imm=10), ins("addi", 31, 31, imm=-1),
                      ins("bne", a=31, imm=-4)])

    def test_calls_returns_and_original_links(self):
        self.compare([ins("jal", 31, imm=12), ins("addi", 2, 31), ins("jal", imm=12),
                      ins("addi", 1, imm=42), ins("jalr", a=31)])
        # AUIPC + ADDI constructs an original function pointer with bit 0 set.
        self.compare([ins("auipc", 31), ins("addi", 31, 31, imm=17),
                      ins("jalr", 31, 31), ins("jal", imm=8), ins("jalr", a=31)])

    def test_jalr_aliases_offsets_and_entry(self):
        for rd, a in ((0, 31), (31, 31), (1, 1), (31, 1), (1, 31)):
            for offset in (-2048, -1, 0, 2047):
                regs = [0] * 32
                regs[a] = (0x10008 - offset) & MASK
                self.compare([ins("jalr", rd, a, imm=offset), ins("addi", 7, imm=999), ins("addi", 8, imm=42)], regs)
        self.compare([ins("addi", 1, imm=99), ins("addi", 2, imm=42)], config=wcc.Config(entry=0x10004))

    def test_invalid_indirect_targets_trap_and_restore(self):
        for target in (0x10002, 0xFFFF, 0x10008, 0xFFFFFFFC):
            regs = [0] + [reg * 73 for reg in range(1, 32)]
            regs[31] = target
            config = wcc.Config()
            result = wcc.translate(image([ins("jalr", 31, 31)]), config)
            actual, _, stop = run_wrm(result.binary, config, regs, {})
            self.assertEqual(stop, "ebreak")
            self.assertEqual(actual, regs)

    def test_far_control_flow(self):
        # An RV branch can expand beyond the WRM branch range.
        program = [ins("beq", imm=4092)] + [ins("addi", 0)] * 1022 + [ins("addi", 31, imm=42)]
        self.compare(program)
        # An RV JAL can expand beyond the WRM JAL range as well.
        program = [ins("jal", 31, imm=40000)] + [ins("addi", 0)] * 9999 + [ins("addi", 31, imm=42)]
        self.compare(program)

    def test_system_instructions(self):
        for op in ("ecall", "ebreak"):
            self.compare([ins("addi", 31, imm=42), ins(op), ins("addi", 31, imm=99)])

    def test_decoder_boundaries(self):
        for op, limits in (("beq", (-4096, 4094)), ("jal", (-1048576, 1048574)),
                           ("sw", (-2048, 2047)), ("addi", (-2048, 2047))):
            for value in limits:
                decoded = wcc.decode(encode(ins(op, 31, 30, 29, value)))
                self.assertEqual(decoded.op, op)
                self.assertEqual(decoded.imm, value)
        for word in (0, 0xFFFFFFFF, 0x02001013, 0x40001013, 0x00003003, 0x30002073, 0x0000100F):
            with self.subTest(word=word), self.assertRaises(wcc.TranslationError): wcc.decode(word)

    def test_input_validation(self):
        with self.assertRaisesRegex(wcc.TranslationError, "ELF input detected"):
            wcc.translate(b"\x7fELF" + bytes(60))
        for data in (b"", b"\x13", b"\0" * 4):
            with self.assertRaises(wcc.TranslationError): wcc.translate(data)
        for config in (wcc.Config(source_base=1), wcc.Config(output_base=3),
                       wcc.Config(scratch=8192), wcc.Config(scratch=0x10000),
                       wcc.Config(entry=0x10002), wcc.Config(entry=0x20000),
                       wcc.Config(output_base=0x10000), wcc.Config(output_base=0xFFFFFFF0)):
            with self.assertRaises(wcc.TranslationError): wcc.translate(image([ins("addi")]), config)
        for op, offset in (("jal", 2), ("beq", -4), ("jal", 8)):
            with self.assertRaises(wcc.TranslationError): wcc.translate(image([ins(op, imm=offset)]))

    def test_cli_and_atomic_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output, map_path = root / "rv.bin", root / "wrm.bin", root / "map.json"
            source.write_bytes(image([ins("addi", 1, imm=42)]))
            command = [sys.executable, "-B", str(Path(wcc.__file__)), str(source), "-o", str(output)]
            completed = subprocess.run(command + ["--map", str(map_path)], capture_output=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(len(output.read_bytes()), 16 + 2 * 128)
            self.assertEqual(json.loads(map_path.read_text())["addresses"]["0x00010000"], "0x00100010")
            previous = output.read_bytes()
            source.write_bytes(b"\0" * 4)
            completed = subprocess.run(command, capture_output=True)
            self.assertEqual(completed.returncode, 1)
            self.assertIn(b"0x00010000", completed.stderr)
            self.assertEqual(output.read_bytes(), previous)
            completed = subprocess.run(command + ["--map", str(source)], capture_output=True)
            self.assertEqual(completed.returncode, 1)
            self.assertEqual(source.read_bytes(), b"\0" * 4)

    @unittest.skipUnless(os.environ.get("WCC_EMULATOR"), "set WCC_EMULATOR to an existing WRM executable")
    def test_existing_emulator(self):
        config = wcc.Config(output_base=0xFE000000)
        program = [ins("addi", 31, imm=-128), ins("andi", 31, 31, imm=-1),
                   ins("lui", 30, imm=0x80000000), ins("addi", 29, imm=-1),
                   ins("div", 28, 30, 29), ins("rem", 27, 30, 29), ins("mulh", 26, 30, 29),
                   ins("jal", 1, imm=12), ins("addi", 2, 1), ins("jal", imm=12),
                   ins("addi", 3, imm=42), ins("jalr", a=1), ins("auipc", 4), ins("fence")]
        expected, _, _ = run_rv(program, config, [0] * 32, {})
        with tempfile.TemporaryDirectory() as directory:
            rom = Path(directory) / "translated.rom"
            rom.write_bytes(wcc.translate(image(program), config).binary)
            completed = subprocess.run([os.environ["WCC_EMULATOR"], "--rom", str(rom),
                                        "--headless", "--mute", "--no-net", "--deterministic", "--debug"],
                                       capture_output=True, text=True, timeout=10)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("halted (HLT)", completed.stderr)
        values = {int(reg): int(value, 16) for reg, value in re.findall(r"\br(\d+)\s+([0-9A-F]{8})", completed.stderr)}
        self.assertEqual(values, dict(enumerate(expected)))


if __name__ == "__main__":
    unittest.main()
