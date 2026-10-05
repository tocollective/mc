"""Link C and M native WRM objects using the project's M toolchain."""

from pathlib import Path
import subprocess
import sys
import tempfile

import wcc
import native_objects
from native_objects import elf, MC

import asm as assembler
import ld as linker


def build_native(sources, output, prefix="riscv64-unknown-elf-", image_format="rom"):
    if output.resolve() in {source.resolve() for source in sources}:
        raise wcc.TranslationError("output must not overwrite a source file")
    with tempfile.TemporaryDirectory(prefix="wcc-native-") as directory:
        directory = Path(directory)
        objects = []

        def assemble(path):
            data, _ = assembler.assemble(str(path), obj=True)
            output_path = directory / f"runtime-{len(objects)}.o"
            output_path.write_bytes(data)
            objects.append(str(output_path))

        assemble(MC / "runtime" / ("rom0.asm" if image_format == "rom" else "crt0.asm"))
        assemble(MC / "runtime/trap.asm")
        assemble(MC / "runtime/mem.asm")
        rt_object = directory / "rt.o"
        completed = subprocess.run([sys.executable, "-B", str(MC / "mc.py"), "-c", str(MC / "runtime/rt.m"),
                                    "-o", str(rt_object)], capture_output=True, text=True)
        if completed.returncode:
            raise wcc.TranslationError(completed.stderr.strip())
        objects.append(str(rt_object))
        for index, source in enumerate(sources):
            target = directory / f"input-{index}.o"
            if source.suffix == ".m":
                completed = subprocess.run([sys.executable, "-B", str(MC / "mc.py"), "-c", str(source), "-o", str(target)],
                                           capture_output=True, text=True)
                if completed.returncode:
                    raise wcc.TranslationError(completed.stderr.strip())
            elif source.suffix in (".c", ".S", ".s"):
                native_objects.compile_object(source, target, prefix)
            elif native_objects.machine(source) == elf.EM_WRM:
                target = source
            elif native_objects.machine(source) == 243:
                target.write_bytes(native_objects.convert(source.read_bytes(), str(source)))
            else:
                raise wcc.TranslationError(f"{source}: expected a C/M source or a WRM/RV32IM object")
            objects.append(str(target))
        layout = "rom" if image_format == "rom" else "boot"
        linked = linker.Linker(layout, data=0x2000, ram_limit=0xC000)
        linked.run(objects)
        # The native linker uses r9 for far-call veneers. C can keep its t0
        # (r9) live across an intra-function jump, so reject an out-of-range
        # non-call jump rather than silently losing that value in a veneer.
        for obj in linked.objects:
            if not any(symbol.name == ".wcc.native" for symbol in obj.symbols):
                continue
            for section_index, section in enumerate(obj.sections):
                if section is None or not section.flags & elf.SHF_EXECINSTR:
                    continue
                inp = linked.inputs[(obj, section_index)]
                for offset, kind, symbol, addend in section.relocs:
                    if kind != elf.R["R_WRM_JAL19"]:
                        continue
                    word = int.from_bytes(section.data[offset:offset + 4], "little")
                    target = linked.value(obj, symbol) + addend
                    delta = linker.signed32(target - (inp.addr + offset))
                    if (word >> 8) & 31 != 31 and not -(1 << 20) <= delta < (1 << 20):
                        raise wcc.TranslationError("native C local jump exceeds the 1 MiB range")
        if image_format == "disk" and linked.bss_end > 4 * 1024 * 1024:
            raise wcc.TranslationError("native data/BSS exceeds the default 4 MiB RAM size")
        binary = linked.output()
        linked.check()
        if image_format == "disk":
            binary += bytes((-len(binary)) % 512)
        wcc.write_atomic(output, binary)


def link_native(sources, output, prefix, image_format):
    try:
        build_native(sources, output, prefix, image_format)
    except (elf.ElfError, assembler.AsmErrors, linker.LinkErrors) as error:
        raise wcc.TranslationError(str(error)) from error
