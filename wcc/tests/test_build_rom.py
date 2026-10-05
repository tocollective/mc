"""Check the complete C-to-ROM path with a prebuilt WRM emulator."""

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
import wcc


class RomBuilderTests(unittest.TestCase):
    def test_package_bounds(self):
        code = bytes.fromhex("13000000")
        for payload in (b"", b"abc", bytes(0x80000)):
            with self.assertRaises(wcc.TranslationError):
                build_rom.package_rom(code, payload)

    def test_source_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "main.c"
            source.write_text("int main(void) { return 0; }\n")
            with self.assertRaisesRegex(wcc.TranslationError, "overwrite"):
                build_rom.build([source], source)
            self.assertIn("return 0", source.read_text())

    def test_disk_header_and_memory_bounds(self):
        code = bytes.fromhex("13000000")
        image = build_rom.package_disk(code, code)
        magic, sectors, entry, flags = struct.unpack_from("<IIII", image)
        self.assertEqual(len(image), 512)
        self.assertEqual((magic, entry, flags), (0x424D5257, 16, 0))
        self.assertEqual(sectors, 1)
        self.assertEqual(image[sectors * 512:], bytes(len(image) - sectors * 512))
        large_code = code * 12000
        large_image = build_rom.package_disk(large_code, large_code)
        self.assertGreater(len(large_image), 1440 * 1024)
        self.assertEqual(len(large_image) % 512, 0)
        self.assertEqual(struct.unpack_from("<I", large_image, 4)[0] * 512, len(large_image))
        overlapping_code = code * 20000
        with self.assertRaisesRegex(wcc.TranslationError, "overlap"):
            build_rom.package_disk(overlapping_code, overlapping_code)

    @unittest.skipUnless(os.environ.get("WCC_EMULATOR") and shutil.which("riscv64-unknown-elf-gcc"),
                         "requires a RISC-V GNU toolchain and WCC_EMULATOR")
    def test_c_programs_on_existing_emulator(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            data_test = directory / "data.c"
            data_test.write_text("""
extern void puts(const char *);
static volatile int seed = 40;
static volatile int empty[4];
static void (*volatile printer)(const char *) = puts;
int main(void) {
    if (empty[3] != 0 || seed != 40) { puts("FAIL"); return 1; }
    empty[1] = 2;
    seed += empty[1];
    if (seed == 42) printer("DATA OK"); else puts("FAIL");
    return 0;
}
""")
            for source, expected in ((build_rom.ROOT / "examples/hello.c", "Hi there!\n"),
                                     (data_test, "DATA OK\n")):
                firmware = Path(os.environ.get("WCC_FIRMWARE", build_rom.ROOT.parent.parent / "bin/firmware.rom"))
                formats = ("rom", "disk") if firmware.is_file() else ("rom",)
                for image_format in formats:
                    with self.subTest(source=source.name, image_format=image_format):
                        output = directory / ("program.rom" if image_format == "rom" else "program.img")
                        build_rom.build([source], output, image_format=image_format)
                        image_args = ["--rom", str(output)] if image_format == "rom" else ["--rom", str(firmware), "--hdd", str(output)]
                        completed = subprocess.run([os.environ["WCC_EMULATOR"], *image_args,
                                                    "--headless", "--mute", "--no-net", "--deterministic", "--debug"],
                                                   capture_output=True, text=True, timeout=10)
                        self.assertEqual(completed.returncode, 0, completed.stderr)
                        self.assertEqual(completed.stdout, expected)
                        self.assertIn("halted (HLT)", completed.stderr)


if __name__ == "__main__":
    unittest.main()
