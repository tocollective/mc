"""Native object relocations and C/M interoperability checks."""

import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build_rom
import native_objects as native
import wcc


def rv_object(words, relocations=(), symbols=()):
    section = native.elf.Section(".text", flags=6, align=4,
                                 data=struct.pack(f"<{len(words)}I", *words))
    section.relocs = list(relocations)
    result = bytearray(native.elf.write_relocatable([section], list(symbols)))
    struct.pack_into("<H", result, 18, 243)
    return result


class NativeObjectTests(unittest.TestCase):
    def test_register_abi_and_local_branch_relocation(self):
        # addi a0,a0,1; beq a0,zero,+8; addi a0,a0,2; ret
        data = rv_object([0x00150513, 0x00050463, 0x00250513, 0x00008067])
        obj = native.elf.read_object(native.convert(data), "native.o")
        words = struct.unpack("<4I", obj.sections[1].data)
        self.assertEqual(words[0], wcc.i_type(0x20, 1, 1, 1))
        self.assertEqual(words[3], wcc.i_type(0x61, 0, 31, 0))
        self.assertEqual(obj.sections[1].relocs[0][1], native.elf.R["R_WRM_BRANCH14"])

    def test_calls_keep_undefined_symbols_and_native_links(self):
        data = rv_object([0x00000097, 0x000080E7, 0x00008067],
                         [(0, 19, 1, 0)], [native.elf.Symbol("mFunction", bind=1)])
        obj = native.elf.read_object(native.convert(data), "native.o")
        words = struct.unpack("<4I", obj.sections[1].data)
        self.assertEqual(words[2], wcc.i_type(0x61, 31, 31, 0))
        self.assertTrue(any(symbol.name == "mFunction" and symbol.shndx == native.elf.SHN_UNDEF
                            for symbol in obj.symbols))
        self.assertEqual([obj.symbols[reloc[2]].name for reloc in obj.sections[1].relocs],
                         ["mFunction", "mFunction"])
        self.assertEqual([reloc[1] for reloc in obj.sections[1].relocs],
                         [native.elf.R["R_WRM_HI19"], native.elf.R["R_WRM_LO13"]])

    def test_reserved_registers_and_invalid_relocations_are_rejected(self):
        with self.assertRaisesRegex(wcc.TranslationError, "reserved RV registers"):
            native.convert(rv_object([0x00130313]))  # addi t1,t1,1
        with self.assertRaisesRegex(wcc.TranslationError, "unsupported instruction relocation"):
            native.convert(rv_object([0x00050513], [(0, 99, 1, 0)], [native.elf.Symbol("foo", bind=1)]))
        with self.assertRaises(wcc.TranslationError):
            native.convert(b"not ELF")
        with self.assertRaises(wcc.TranslationError):
            native.convert(rv_object([0x00050513])[:-20])

    def test_soft_float_calls_become_wrm_instructions(self):
        # call __mulsf3; call __ltsf2; ret: auipc ra,0; jalr ra,0(ra) for each
        data = rv_object([0x00000097, 0x000080E7, 0x00000097, 0x000080E7, 0x00008067],
                         [(0, 19, 1, 0), (8, 19, 2, 0)],
                         [native.elf.Symbol("__mulsf3", bind=1), native.elf.Symbol("__ltsf2", bind=1)])
        obj = native.elf.read_object(native.convert(data), "native.o")
        words = struct.unpack(f"<{len(obj.sections[1].data) // 4}I", obj.sections[1].data)
        self.assertEqual(list(words), [wcc.r_type(wcc.FLOAT_OPS["fmul"], 1, 1, 2),
                                       wcc.r_type(wcc.FLOAT_OPS["flt"], 1, 1, 2),
                                       wcc.r_type(0x11, 1, 0, 1),
                                       wcc.i_type(0x61, 0, 31, 0)])
        self.assertEqual(obj.sections[1].relocs, [])

    def test_float_helpers_use_only_known_instructions(self):
        for name, words in native.FLOAT_HELPERS.items():
            self.assertTrue(words, name)
            for word in words:
                self.assertTrue(word & 0xFF in set(wcc.FLOAT_OPS.values()) | {0x11, 0x12, 0x20, 0x24},
                                f"{name}: 0x{word:08x}")

    def test_double_helpers_are_rejected_with_a_hint(self):
        for helper in ("__adddf3", "__extendsfdf2", "__truncdfsf2", "__floatsidf", "__floatdisf"):
            data = rv_object([0x00000097, 0x000080E7, 0x00008067], [(0, 19, 1, 0)],
                             [native.elf.Symbol(helper, bind=1)])
            with self.assertRaisesRegex(wcc.TranslationError, "binary32 floating point only"):
                native.convert(data)

    @unittest.skipUnless(os.environ.get("WCC_EMULATOR") and shutil.which("riscv64-unknown-elf-gcc"),
                         "requires a RISC-V GNU toolchain and WCC_EMULATOR")
    def test_float_arithmetic_comparisons_and_conversions_run_on_the_emulator(self):
        source = """
extern float sqrtf(float);
extern float fabsf(float);
extern float fminf(float, float);
extern float fmaxf(float, float);
volatile float va = 1.5f, vb = 2.5f, vc = -2.75f, vz = 0.0f, vthree = 3.0f;
volatile int vseven = 7;
volatile unsigned vbig = 4000000000u;
int main(void) {
    float a = va, b = vb, c = vc, zero = vz, three = vthree;
    float nan = zero / zero;
    float inf = three / zero;
    if (a + b != 4.0f) return 1;
    if (a - b != -1.0f) return 2;
    if (a * b != 3.75f) return 3;
    if (three / a != 2.0f) return 4;
    if (!(a < b) || !(a <= b) || !(b > a) || !(b >= a)) return 5;
    if (b < a || b <= a || a > b || a >= b) return 6;
    if (!(a == a) || a != a || !(a != b) || a == b) return 7;
    if (!(a <= a) || !(a >= a) || a < a || a > a) return 8;
    if (nan < a || nan <= a || nan > a || nan >= a || nan == a || nan == nan) return 9;
    if (!(nan != nan) || !(nan != a)) return 10;
    if (!(inf > b) || !(-inf < c)) return 11;
    if ((int)c != -2 || (int)b != 2) return 12;
    if ((unsigned)b != 2u || (unsigned)(three * a) != 4u) return 13;
    if ((float)vseven != 7.0f) return 14;
    if ((float)vbig != 4000000000.0f) return 15;
    if (-a != -1.5f || fabsf(c) != 2.75f) return 16;
    if (sqrtf(three * three + 7.0f) != 4.0f) return 17;
    if (fminf(a, b) != a || fmaxf(a, b) != b) return 18;
    if (fminf(nan, b) != b || fmaxf(a, nan) != a) return 19;
    return 0;
}
"""
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            c_source, output = directory / "float.c", directory / "float.rom"
            c_source.write_text(source)
            build_rom.build([c_source], output, native=True)
            completed = subprocess.run([os.environ["WCC_EMULATOR"], "--rom", str(output),
                                        "--headless", "--mute", "--no-net", "--deterministic"],
                                       capture_output=True, text=True, timeout=10)
            self.assertEqual(completed.returncode, 0, f"check {completed.returncode} failed: {completed.stderr}")

    @unittest.skipUnless(shutil.which("riscv64-unknown-elf-gcc"), "requires a RISC-V GNU toolchain")
    def test_compile_cli_outputs_a_wrm_object(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "hello.o"
            completed = subprocess.run([sys.executable, "-B", str(build_rom.ROOT / "wcc.py"),
                                        "-c", str(build_rom.ROOT / "examples/hello.c"), "-o", str(output)],
                                       capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(native.machine(output), native.elf.EM_WRM)

    @unittest.skipUnless(os.environ.get("WCC_EMULATOR") and shutil.which("riscv64-unknown-elf-gcc"),
                         "requires a RISC-V GNU toolchain and WCC_EMULATOR")
    def test_mixed_calls_data_function_pointers_and_stack_arguments(self):
        c_text = """
extern int mAdd(int, int);
extern int mSum9(int,int,int,int,int,int,int,int,int);
extern int mCall9(void);
extern int mCallback(int (*)(int), int);
extern int mPointer(void);
extern int mCounter;
extern void puts(const char *);
int cCounter = 3;
static int (*volatile native_pointer)(int,int) = mAdd;
int cAdjust(int value) { return value + cCounter; }
unsigned cBits(unsigned value) { return (value & 0xfffff7ffu) ^ 0xfffff800u; }
int cSum9(int a,int b,int c,int d,int e,int f,int g,int h,int i) {
    return a+b+c+d+e+f+g+h+i;
}
int main(void) {
    if (native_pointer(19,20) != 42) return 1;
    if (mSum9(1,2,3,4,5,6,7,8,9) != 45) return 2;
    if (mCall9() != 45) return 3;
    if (mCallback(cAdjust,39) != 42 || mPointer() != 42) return 4;
    mCounter = 7;
    cCounter += mCounter;
    if (cCounter != 10) return 5;
    if (cBits(0x12345678u) != 0xedcbae78u) return 6;
    puts("MIXED PASS\\n");
    return 0;
}
"""
        m_text = """
extern let cAdjust(value: Word): Word
extern let cSum9(a: Word,b: Word,c: Word,d: Word,e: Word,f: Word,g: Word,h: Word,i: Word): Word
let mut mCounter: Word = 5
let mAdd(left: Word, right: Word): Word { return cAdjust(left + right) }
let mSum9(a: Word,b: Word,c: Word,d: Word,e: Word,f: Word,g: Word,h: Word,i: Word): Word {
    return a+b+c+d+e+f+g+h+i
}
let mCall9(): Word { return cSum9(1,2,3,4,5,6,7,8,9) }
let mCallback(callback: (arg: Word): Word, value: Word): Word { return callback(value) }
let mPointer(): Word { return cAdjust(39) }
export { mAdd, mSum9, mCall9, mCallback, mPointer, mCounter }
"""
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            c_source, m_source = directory / "main.c", directory / "helper.m"
            c_source.write_text(c_text)
            m_source.write_text(m_text)
            c_object, m_object = directory / "main.o", directory / "helper.o"
            native.compile_object(c_source, c_object)
            completed = subprocess.run([sys.executable, "-B", str(native.MC / "mc.py"),
                                        "-c", str(m_source), "-o", str(m_object)], capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            firmware = Path(os.environ.get("WCC_FIRMWARE", build_rom.ROOT.parent.parent / "bin/firmware.rom"))
            formats = ("rom", "disk") if firmware.is_file() else ("rom",)
            for image_format in formats:
                with self.subTest(image_format=image_format):
                    output = directory / "mixed.img"
                    build_rom.build([c_object, m_object], output, image_format=image_format)
                    image_args = ["--rom", str(output)] if image_format == "rom" else ["--rom", str(firmware), "--hdd", str(output)]
                    completed = subprocess.run([os.environ["WCC_EMULATOR"], *image_args,
                                                "--headless", "--mute", "--no-net", "--deterministic"],
                                               capture_output=True, text=True, timeout=10)
                    self.assertEqual(completed.returncode, 0, completed.stderr)
                    self.assertEqual(completed.stdout, "MIXED PASS\n")

    @unittest.skipUnless(os.environ.get("WCC_EMULATOR") and shutil.which("riscv64-unknown-elf-gcc"),
                         "requires a RISC-V GNU toolchain and WCC_EMULATOR")
    def test_standard_m_compiler_links_native_c_objects(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            c_source, m_source = directory / "library.c", directory / "entry.m"
            c_source.write_text("int cDouble(int value) { return value * 2; }\n")
            m_source.write_text("""
extern let cDouble(value: Word): Word
let main(argc: UWord, argv: *UByte[]): Word {
    if cDouble(21) != 42 return 7
    return 0
}
""")
            c_object, m_object = directory / "library.o", directory / "entry.o"
            native.compile_object(c_source, c_object)
            command = [sys.executable, "-B", str(native.MC / "mc.py")]
            completed = subprocess.run(command + ["-c", str(m_source), "-o", str(m_object)], capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            output = directory / "program.rom"
            completed = subprocess.run(command + ["--rom", str(m_object), str(c_object), "-o", str(output)],
                                       capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            completed = subprocess.run([os.environ["WCC_EMULATOR"], "--rom", str(output),
                                        "--headless", "--mute", "--no-net", "--deterministic"],
                                       capture_output=True, text=True, timeout=10)
            self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":
    unittest.main()
