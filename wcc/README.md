# WCC – WRM C Compiler

## Link C and M objects

`wcc.py -c` compiles C into a **native WRM ELF32 relocatable object** using
the M register convention. These objects can be linked with `.o` files from
`mc.py -c`. The native path uses the M tools in the enclosing `mc/` directory and requires
RISC-V GCC; it does not require building the WRM emulator.

From the parent repository root, build the supplied example, which calls
C → M → C and prints `M + C: 42`:

```sh
python3 -B mc/mc.py -c mc/wcc/examples/mixed/helper.m -o mc/wcc/helper.o
python3 -B mc/wcc/wcc.py -c mc/wcc/examples/mixed/main.c -o mc/wcc/main.o
python3 -B mc/wcc/build_rom.py --disk mc/wcc/main.o mc/wcc/helper.o -o mc/wcc/mixed.img
bin/wrm081632 --rom bin/firmware.rom --hdd mc/wcc/mixed.img --headless --mute --no-net
```

From the `mc/wcc` directory:

```sh
python3 -B ../mc.py -c examples/mixed/helper.m -o helper.o
python3 -B wcc.py -c examples/mixed/main.c -o main.o
python3 -B build_rom.py --disk main.o helper.o -o mixed.img
../../bin/wrm081632 --rom ../../bin/firmware.rom --hdd mixed.img --headless --mute --no-net
```

`build_rom.py` automatically selects native linking when an input is a WRM
object or an M source. It can compile C sources alongside M objects directly:

```sh
python3 -B build_rom.py --disk examples/mixed/main.c helper.o -o mixed.img
python3 -B build_rom.py --native examples/hello.c -o hello-native.rom
```

Omit `--disk` to create a ROM. The native image uses the M startup,
trap handler, memory helpers and `rt.m` from `mc/runtime`; `puts` comes from
`rt.m` (it does not append a newline and returns nothing, so declare it
`void puts(const char *)`). Returning
from `main` powers the machine off with the low eight bits of its result as
the exit status, matching the M runtime. Native disk images are padded to whole 512-byte sectors. They run with the default 4 MiB RAM configuration.

The resulting C objects also work with the standard M compiler/linker:

```sh
python3 -B ../mc.py main.o helper.o -o mixed.img
# Or use ../ld.py with your own native startup/runtime objects.
```

When using `mc.py`, its M runtime supplies library symbols such as `puts`;
their semantics and declarations must match the selected runtime.

Declare M exports as C `extern` functions or variables, and declare C symbols
with M `extern let`. `export { name }` makes M definitions visible to C.
Fixed 32-bit integer arguments, pointers, callbacks, shared globals and
32-bit integer/pointer results use the same registers and stack slots.
Ordinary fixed scalar arguments beyond the first eight are supported on the
stack. An M `Word` corresponds to C `int`, and `UWord` to `unsigned int`.

Use compatible declarations on both sides. Cross-language variadic functions,
by-value aggregates, over-aligned stack objects, and 64-bit arguments split
across the register/stack boundary are outside the supported interface.
A full C standard library is not supplied.

### Floating point

The native path (`wcc.py -c`, or `build_rom.py --native`) supports C `float`.
WRM keeps IEEE binary32 in the general registers, and GCC for RV32IM
(`-mabi=ilp32`, soft float) passes `float` the same way: operands in `a0`
and `a1`, result in `a0`. So `wcc` replaces each call of a libgcc soft-float
helper with the WRM instruction itself, one cycle and no call:

| C | Helper | WRM |
|---|--------|-----|
| `a + b`, `a - b`, `a * b`, `a / b` | `__addsf3`, `__subsf3`, `__mulsf3`, `__divsf3` | `FADD`, `FSUB`, `FMUL`, `FDIV` |
| `-a` | `__negsf2` | `FSGNJN` |
| `a == b`, `!=`, `<`, `<=`, `>`, `>=` | `__eqsf2`, `__nesf2`, `__ltsf2`, `__lesf2`, `__gtsf2`, `__gesf2`, `__unordsf2` | `FEQ`, `FLT`, `FLE` (+ one more instruction to give the helper's result) |
| `(float)int`, `(float)unsigned` | `__floatsisf`, `__floatunsisf` | `ITOF`, `UTOF` |
| `(int)f`, `(unsigned)f` | `__fixsfsi`, `__fixunssfsi` | `FTOI`, `FTOU` (toward zero, saturating) |
| `sqrtf`, `fabsf`, `fminf`, `fmaxf` | (the functions themselves) | `FSQRT`, `FSGNJX`, `FMIN`, `FMAX` |

Comparisons with a NaN are false, except `!=`, as in C. Declare the four
`<math.h>` functions yourself (`extern float sqrtf(float);`): there is no
header.

WRM has no binary64. Write `1.0f`, not `1.0`, which is a `double`: it and
`double`, `long double` and the conversions between `float` and 64-bit
integers need libgcc helpers that do not exist here, so `wcc` rejects them
with a message that names the helper. Mixed C/M programs work as before: M's
`Float` and C's `float` are the same value in the same register.

The plain `build_rom.py file.c` path (the raw translation of a linked RV32IM
image) has no floating point and is about 6 times slower than the native
path, because every RV instruction becomes a 32-word slot there. Use
`--native` for anything that computes.

The native compiler reserves the six RV temporary registers that would map to
WRM callee-saved registers. It remaps `a0`–`a7` to `r1`–`r8`, `sp` to `r30`,
and `ra` to `r31`, translates instructions, and rewrites symbols, branch
targets, call pairs and data relocations. Calls and function pointers use
native WRM addresses; this path needs neither fixed translation slots nor
the raw translator's scratch words. Native code does not preserve the raw
input's original RV PCs.
Plain C `char` is signed, matching the WRM data ABI. TLS and constructor
sections are unsupported. Exceptionally large intra-function jumps beyond
the native JAL range are rejected rather than using a veneer that could
overwrite a live C temporary.

Compile C through `wcc.py -c`, which supplies the required ABI flags.
Precompiled RISC-V objects using reserved temporaries, RVC, a floating-point
ABI, TLS, or unsupported relocations are rejected with a diagnostic. The
raw translation mode below remains available for existing RV32IM binaries.
`-c` also accepts `-I`, `-D` and `--toolchain-prefix`.

## Compile and run the C example

RISC-V GCC's default target may be RV64 with compressed instructions.
The raw translation mode of `wcc.py` requires RV32IM, and an ELF
object (`.o`) is not a raw instruction image: it contains headers, sections,
symbols and unresolved relocations. Renaming `.o` to `.bin` or extracting its
`.text` before linking will not resolve a call to `puts` or a string address.

Use the complete C-to-ROM builder from the parent repository root:

```sh
python3 -B mc/wcc/build_rom.py mc/wcc/examples/hello.c -o mc/wcc/hello.rom
bin/wrm081632 --rom mc/wcc/hello.rom --headless --mute --no-net
```

From the `mc/wcc` directory:

```sh
python3 -B build_rom.py examples/hello.c -o hello.rom
../../bin/wrm081632 --rom hello.rom --headless --mute --no-net
```

The program prints `Hi there!` through the UART and stops with `HLT`.
This builds the C program and its runtime, using an existing WRM emulator.
It does not build WRM.

`build_rom.py` uses the `riscv64-unknown-elf-gcc`, `objcopy` and `nm` tools.
Use `--toolchain-prefix` for another GNU toolchain prefix. It selects
`-march=rv32im -mabi=ilp32`, disables relaxation and small-data GP addressing,
and links all inputs with the startup code and UART `puts` implementation in
`runtime/start.S`. Only linked `.text` is translated; a native WRM reset
bootstrap copies original code, read-only data and initialized globals to RAM
at `0x10000`, zero-initializes BSS, and enters translated code in ROM.
The startup sets the RV stack to `0x80000`; at least 512 KiB of RAM is needed.
The ROM preserves a minimum 8 KiB gap between globals and the stack.

This is a freestanding runtime with `puts`, not a full C library or LA/IX
executable. Returning from `main` halts the machine; the C return value is not
converted to an emulator exit status. Other library functions require their
own RV32IM implementations. The builder also accepts previously compiled
RV32IM objects, for example:

```sh
riscv64-unknown-elf-gcc -march=rv32im -mabi=ilp32 -mno-relax -msmall-data-limit=0 \
    -ffreestanding -fno-builtin -fno-pic -fno-pie -c examples/hello.c -o hello.rv32.o
python3 -B build_rom.py hello.rv32.o -o hello.rom
```

The [GCC target options](https://gcc.gnu.org/onlinedocs/gcc/RISC-V-Options.html)
select the instruction set and ABI. For a separate raw-code workflow, link
first and then use
[objcopy](https://sourceware.org/binutils/docs/binutils/objcopy.html)
`-O binary -j .text` on the linked ELF; loading data and initializing the
execution environment remain the caller's responsibility.

## Boot from a disk

Select `--disk` (or `--format disk`) to build a bootable disk image,
padded to whole 512-byte sectors. From the parent repository root:

```sh
python3 -B mc/wcc/build_rom.py --disk mc/wcc/examples/hello.c -o mc/wcc/hello.img
bin/wrm081632 --rom bin/firmware.rom --hdd mc/wcc/hello.img --headless --mute --no-net
```

From the `mc/wcc` directory:

```sh
python3 -B build_rom.py --disk examples/hello.c -o hello.img
../../bin/wrm081632 --rom ../../bin/firmware.rom --hdd hello.img --headless --mute --no-net
```

Use your firmware ROM path if it differs from `bin/firmware.rom`. Omit
`--headless` to open the machine's window. This example's `puts` writes to
the UART, so `Hi there!` appears in the terminal in either mode.

The image follows the firmware's [boot protocol](../../docs/SPECIFICATION.md#boot-protocol):
a 16-byte `WRMB` header declares the sector count, entry offset and zero flags.
The firmware loads the program at `0x10000` and enters its WRM bootstrap.
The bootstrap copies original RV code and data to `0x200000`; translated code
stays in the loaded image. The RV stack starts at `0x300000`, and the two
scratch words use `0x800`. Use at least 4 MiB of RAM (the emulator's default).
The builder rejects programs whose loaded image overlaps the original RV
code/data or whose data overlaps the stack. The final sector is padded with
zeros. The disk format has no 1.44 MB floppy capacity limit.

This image boots the C program directly through firmware. It does not install
the program in a filesystem or launch it as a LA/IX application. Returning
from `main` executes `HLT`.

## wt

`wt` translates raw, little-endian **RV32IM machine instructions** to a raw
**WRM.081632 machine-code image**. It is a standalone Python 3.10+ utility with
no external dependencies. It does not assemble RISC-V source or read ELF files.

## Usage

From the parent repository root:

```sh
python3 -B mc/wcc/wcc.py program.rv.bin -o program.wrm.bin \
    --source-base 0x10000 --output-base 0x100000 --map program.map.json
```

From the `mc/wcc` directory, use `python3 -B wcc.py` instead.

| Option | Meaning | Default |
| --- | --- | --- |
| `-o`, `--output` | Output WRM binary; required | — |
| `--source-base` | Original address of the first RV instruction | `0x10000` |
| `--output-base` | Address where the WRM image will be loaded | `0x100000` |
| `--entry` | Original RV entry address inside the input image | source base |
| `--scratch` | Address of two reserved writable, word-aligned words | `0x1000` |
| `--map` | Optional JSON mapping original PCs to translated PCs | — |

Addresses accept decimal or `0x` notation. Load the output at `--output-base`
and start execution at that address, which contains an initialization header.
The `--entry` option selects the RV instruction reached by that header.
Entering a translated slot directly bypasses initialization.

A small input can be created without a RISC-V toolchain:

```sh
python3 -c 'import struct; from pathlib import Path; Path("program.rv.bin").write_bytes(struct.pack("<II", 0x02a00513, 0x00550593))'
python3 -B mc/wcc/wcc.py program.rv.bin -o program.wrm.bin --map program.map.json
```

The input is `addi x10, x0, 42; addi x11, x10, 5`. The translated program
finishes with `r10 = 42`, `r11 = 47`, then executes `HLT`.
The generated binary is raw code, not a firmware ROM, disk boot image or ELF;
the host loader must provide the execution environment.

## Supported instructions

- RV32I integer register and immediate operations, `LUI`, `AUIPC`, all six
  conditional branches, `JAL`, `JALR`, byte/halfword/word loads and stores.
- All eight RV32M multiplication/division/remainder operations, including
  multiply-high, division by zero and signed overflow behavior.
- `FENCE` maps to a full WRM `FENCE`; `ECALL` maps to `SYSCALL`, and `EBREAK`
  maps to `BREAK`.

`xN` maps to `rN`, including the zero register. RV signed logical immediates
are expanded when necessary because WRM logical immediates are zero-extended.
RV upper immediates shift by 12, while WRM upper immediates shift by 13;
constants are split into WRM `LUI`/`ORI` sequences. `AUIPC` computes the
**original RV PC**, not the translated PC.

## Addresses and execution contract

Each input word gets a fixed 128-byte WRM slot. A 16-byte header precedes the
slots; one terminal slot follows them. Output size is `16 + 128 * (N + 1)`
bytes for `N` input instructions. Padding is skipped during normal execution.
This layout prioritizes correct indirect jumps over code density:

```text
translated_pc = output_base + 16 + (original_pc - source_base) * 32
```

Register values, data pointers, function pointers and link values remain in
the original RV address space. `JAL` and `JALR` write the original `pc + 4`.
Direct jumps use translated targets; `JALR` clears bit 0, checks word alignment
and the input range, then maps its target at runtime. Neither near nor far
transfers discard a guest register. The two scratch words preserve `x31` and
one temporary register during expanded instructions.

The loader must satisfy these conditions:

- Map the output code and two scratch words at the configured addresses.
  Scratch memory must be ordinary writable memory and must be reserved from
  the guest, devices, interrupt handlers and concurrent translated programs.
  The scratch address must be in `0..8184`. Source, output and scratch ranges
  must be disjoint, aligned and fit the 32-bit address space.
- Initialize registers and data memory for the original program. Memory
  addresses are not relocated. If a program reads its own code or constants
  in the code image, load a read-only copy of the original input at source
  base as well. Separate data sections must be loaded separately.
- Preserve the RV register convention. This translator does not convert to
  the native WRM calling convention, system-call ABI, privilege model or OS
  environment. Supply suitable handlers for `SYSCALL` and `BREAK`. Interrupt
  handlers must preserve guest registers and scratch memory; task switching
  must also preserve the two scratch words.
- Use supervisor mode for the terminal `HLT`, or provide a handler for its
  privileged-instruction fault when running in user mode.

Falling through the input's end, or jumping exactly one word past the last
instruction, reaches the terminal `HLT`. Other direct targets outside the
input and all misaligned direct targets are rejected during translation.
Invalid indirect targets restore the preserved registers and enter a WRM
`BREAK` loop; this is a diagnostic trap, not a RISC-V architectural exception.
Native memory faults likewise use the WRM trap model and translated PCs.

The input must be nonempty, contain only 4-byte instructions, and contain no
inline data. RV64, compressed instructions, atomics, floating-point, CSR and
privileged instructions, and `FENCE.I` are unsupported. Self-modifying code
is unsupported: writing the original image does not retranslate the output.
Unknown or invalid instructions produce an error with the original PC and
instruction word. Failed translation does not overwrite an existing output.

## Tests

```sh
python3 -B -m unittest discover -s mc/wcc/tests -v
```

The tests compare execution against independent RV32IM and WRM models, checking
all registers and guest memory. They cover register aliasing, negative
immediates, multiply/divide corner cases, memory access, original-PC-relative
reads, branches, far jumps, calls, indirect returns, invalid targets, entry
selection and CLI failures. They do not build the WRM emulator.

To also check a translated ROM against an **existing** emulator executable:

```sh
WCC_EMULATOR="$PWD/bin/wrm081632" python3 -B -m unittest discover -s mc/wcc/tests -v
```

This optional test checks the actual final register dump after arithmetic,
a direct call, an indirect return and `AUIPC`. When the GNU RISC-V toolchain is
available, it also compiles and runs `hello.c` and checks initialized globals,
zeroed BSS and an indirect call through a relocated function pointer.
With an existing firmware image at `bin/firmware.rom` (or `WCC_FIRMWARE`),
these C programs are also booted from disk images through the firmware.
It does not rebuild WRM.

The encodings follow the parent project's
[WRM instructions](../../docs/INSTRUCTIONS.md) and the official
[RV32I](https://docs.riscv.org/reference/isa/unpriv/rv32.html) and
[M extension](https://docs.riscv.org/reference/isa/unpriv/m-st-ext.html)
specifications.
