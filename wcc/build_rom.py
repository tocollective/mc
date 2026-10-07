#!/usr/bin/env python3
"""Compile freestanding RV32IM C into a WRM ROM or bootable disk image."""

import argparse
from pathlib import Path
import re
import struct
import subprocess
import sys
import tempfile

import wcc


ROOT = Path(__file__).resolve().parent
ROM_BASE = 0xFE000000
BOOT_BYTES = 128
SOURCE_BASE = 0x10000
STACK_TOP = 0x80000
BOOT_LOAD = 0x10000
DISK_SOURCE_BASE = 0x200000
DISK_STACK_TOP = 0x300000
SECTOR_BYTES = 512
BOOT_MAGIC = 0x424D5257


def run(command):
    completed = subprocess.run(command, capture_output=True, text=True)
    if completed.returncode:
        message = f"{command[0]} failed:\n{completed.stderr.strip()}"
        if re.search(r"undefined reference to `__\w*(?:sf|df)\d", completed.stderr):
            message += "\nhint: floating point is supported by the native path only: use --native"
        raise wcc.TranslationError(message)
    return completed.stdout


def package_program(code, load_image, base, source_base, stack_top, scratch=0x1000):
    """Copy the original code/data image to RAM, then enter translated code."""
    config = wcc.Config(source_base=source_base, output_base=base + BOOT_BYTES, scratch=scratch)
    translated = wcc.translate(code, config).binary
    if len(load_image) % 4 or len(load_image) < len(code):
        raise wcc.TranslationError("invalid linked load image")
    if source_base + len(load_image) > stack_top - 8192:
        raise wcc.TranslationError("linked image overlaps the reserved 8 KiB stack")
    payload_address = base + BOOT_BYTES + len(translated)
    program_end = payload_address + len(load_image)
    if base < source_base + len(load_image) and source_base < program_end:
        raise wcc.TranslationError("packaged program overlaps the original RAM image")
    bootstrap = wcc.constant(1, payload_address)
    bootstrap += wcc.constant(2, source_base)
    bootstrap += wcc.constant(3, source_base + len(load_image))
    loop = len(bootstrap)
    bootstrap += [wcc.i_type(0x44, 4, 1, 0), wcc.i_type(0x4A, 4, 2, 0),
                  wcc.i_type(0x20, 1, 1, 4), wcc.i_type(0x20, 2, 2, 4)]
    bootstrap.append(wcc.i_type(0x54, 2, 3, loop - len(bootstrap)))
    bootstrap += wcc.constant(31, config.output_base)
    bootstrap.append(wcc.i_type(0x61, 0, 31, 0))
    if len(bootstrap) * 4 > BOOT_BYTES:
        raise wcc.TranslationError("program bootstrap is too large")
    bootstrap += [1] * (BOOT_BYTES // 4 - len(bootstrap))
    return struct.pack(f"<{len(bootstrap)}I", *bootstrap) + translated + load_image


def package_rom(code, load_image):
    rom = package_program(code, load_image, ROM_BASE, SOURCE_BASE, STACK_TOP)
    if len(rom) > 32 * 1024 * 1024:
        raise wcc.TranslationError("translated ROM exceeds WRM's 32 MiB ROM limit")
    return rom


def package_disk(code, load_image):
    # Firmware loads the header and program at BOOT_LOAD. Original RV data
    # must stay above the loaded disk image, so copying cannot replace code.
    program = package_program(code, load_image, BOOT_LOAD + 16,
                              DISK_SOURCE_BASE, DISK_STACK_TOP, scratch=0x800)
    size = 16 + len(program)
    sectors = (size + SECTOR_BYTES - 1) // SECTOR_BYTES
    header = struct.pack("<IIII", BOOT_MAGIC, sectors, 16, 0)
    return header + program + bytes(sectors * SECTOR_BYTES - size)


def build(sources, output, prefix="riscv64-unknown-elf-", image_format="rom", native=False):
    if image_format not in ("rom", "disk"):
        raise wcc.TranslationError("image format must be rom or disk")
    import native_objects
    if native or any(source.suffix == ".m" or native_objects.machine(source) == native_objects.elf.EM_WRM
                     for source in sources):
        from native_link import link_native
        link_native(sources, output, prefix, image_format)
        return
    source_base = SOURCE_BASE if image_format == "rom" else DISK_SOURCE_BASE
    stack_top = STACK_TOP if image_format == "rom" else DISK_STACK_TOP
    if output.resolve() in {source.resolve() for source in sources}:
        raise wcc.TranslationError("output must not overwrite a source file")
    flags = ["-march=rv32im", "-mabi=ilp32", "-mno-relax", "-msmall-data-limit=0",
             "-ffreestanding", "-fno-builtin", "-fno-pic", "-fno-pie",
             "-fno-stack-protector", "-fno-unwind-tables", "-fno-asynchronous-unwind-tables",
             "-nostdlib", "-nostartfiles", "-O1"]
    with tempfile.TemporaryDirectory(prefix="wt-image-") as directory:
        directory = Path(directory)
        elf, code_path, image_path = directory / "program.elf", directory / "text.bin", directory / "image.bin"
        run([prefix + "gcc", *flags, str(ROOT / "runtime/start.S"),
             *(str(source) for source in sources), "-Wl,--no-relax",
             f"-Wl,--defsym=__source_base={source_base}", f"-Wl,--defsym=__stack_top={stack_top}",
             "-Wl,--orphan-handling=error", "-Wl,-T," + str(ROOT / "runtime/rom.ld"),
             "-o", str(elf)])
        run([prefix + "objcopy", "-O", "binary", "-j", ".text", str(elf), str(code_path)])
        run([prefix + "objcopy", "-O", "binary", "-j", ".text", "-j", ".rodata",
             "-j", ".data", str(elf), str(image_path)])
        symbols = {}
        for line in run([prefix + "nm", "--defined-only", str(elf)]).splitlines():
            fields = line.split()
            if len(fields) == 3:
                symbols[fields[2]] = int(fields[0], 16)
        code, load_image = code_path.read_bytes(), image_path.read_bytes()
        if symbols.get("_start") != source_base or symbols.get("__text_end") != source_base + len(code):
            raise wcc.TranslationError("unexpected linked text layout")
        image_size = symbols["__image_end"] - source_base
        if not len(load_image) <= image_size <= stack_top - 8192 - source_base:
            raise wcc.TranslationError("invalid linked data/BSS layout")
        # objcopy omits NOBITS BSS; initialize it explicitly in the RAM payload.
        load_image += bytes(image_size - len(load_image))
        package = package_rom if image_format == "rom" else package_disk
        wcc.write_atomic(output, package(code, load_image))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sources", type=Path, nargs="+", help="C/assembly sources or RV32IM objects")
    parser.add_argument("-o", "--output", type=Path, required=True, help="WRM ROM or bootable disk image")
    formats = parser.add_mutually_exclusive_group()
    formats.add_argument("--format", choices=("rom", "disk"), default="rom", help="image format (default: rom)")
    formats.add_argument("--disk", dest="format", action="store_const", const="disk", default=argparse.SUPPRESS,
                         help="create a bootable disk image (same as --format disk)")
    parser.add_argument("--native", action="store_true", help="compile C to the native WRM/M object ABI")
    parser.add_argument("--toolchain-prefix", default="riscv64-unknown-elf-", help="GNU RISC-V tool prefix")
    args = parser.parse_args(argv)
    try:
        build(args.sources, args.output, args.toolchain_prefix, args.format, args.native)
    except (OSError, wcc.TranslationError) as error:
        print(f"build_rom: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
