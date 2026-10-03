# M

M is a systems programming language for the **WRM.081632** virtual machine.
It combines explicit types, `let` / `let mut` declarations, and named module
imports with direct access to memory, device registers, and CPU instructions.
The project's [firmware](../wfw/README.md) and [Laix OS](../laix/README.md)
are written in M with assembly where needed.

The current compiler, **M0**, is written in Python. It generates WRM assembly,
assembles each module into an ELF object, and links the objects into a boot or
ROM image. M uses the [WRM ABI](../docs/ABI.md); it has no garbage collector or
managed runtime. The supplied runtime provides startup code, basic memory
operations, a default trap handler, and UART output.

## Contents

- [Quick start](#quick-start)
- [Compiler usage](#compiler-usage)
- [Language overview](#language-overview)
- [Modules and assembly](#modules-and-assembly)
- [Hardware access](#hardware-access)
- [Images and runtime](#images-and-runtime)
- [Examples](#examples)
- [Diagnostics and tests](#diagnostics-and-tests)
- [Source layout](#source-layout)
- [Further reading](#further-reading)

## Quick start

The compiler uses Python 3 and the Python standard library. Keep it inside this
repository: it uses the assembler in `mc/` and the runtime in `mc/runtime/`.
Running a generated image also requires a WRM emulator binary and firmware ROM;
see the [project README](../README.md) for emulator setup.

All shell commands below assume the **repository root** as the current directory.

Check an existing program without generating assembly, object files, or an image:

```sh
python3 -B mc/mc.py --check mc/examples/example.m
```

Create a file named `hello.m`:

```m
import { puts } from "rt.m"

let main(argc: UWord, argv: *UByte[]): Word {
    puts("Hello, M!\n")
    return 0
}
```

`puts` writes a zero-terminated string to the UART. It does not add a newline.
The `-I` option below makes its declaration in `mc/runtime/rt.m` available to the
importer. The compiler also includes that module when linking a program.

```sh
# Check the program without producing an image.
python3 -B mc/mc.py -I mc/runtime --check hello.m

# Create a bootable disk image.
python3 mc/mc.py -I mc/runtime hello.m -o hello.img

# Create the firmware ROM if one is not already available.
python3 mc/mc.py --rom wfw/src/main.m -o firmware.rom

# Run using an existing emulator binary; UART output appears on stdout.
bin/wrm081632 --headless --rom firmware.rom --hdd hello.img
```

The program prints `Hello, M!` and exits with code `0`. The supplied startup code
calls `main(0, null)`: it does not pass host command-line arguments to the guest.

## Compiler usage

```text
python3 mc/mc.py [-I DIR]... [-o OUT] [--rom] [-c | -S]
               [--map FILE] [--save-temps DIR] [--ast] [--check] FILE...
```

| Option | Effect |
| --- | --- |
| `-o OUT`, `--output OUT` | Set the output path. By default, replace the first input's extension with `.img`, `.rom`, `.o`, or `.s`, according to the mode. |
| `-I DIR` | Add an import search directory; repeat to add more directories. Also used for assembly includes. |
| `--check` | Load imports and check syntax, types, and program declarations; write no compiler output files. |
| `--ast` | Print the syntax trees of loaded modules and stop before type checking. |
| `-c` | Compile one M module into an ELF object without linking. |
| `-S` | Emit assembly for one M module without assembling or linking. |
| `--rom` | Link a ROM image instead of a boot image. |
| `--map FILE` | Write the linked image's section and symbol map. |
| `--save-temps DIR` | Keep generated assembly and intermediate objects for a full image build; with `-c`, keep the generated assembly. |

Pass one `.m` source, one assembly program, or a list of `.o` / `.a` files to
link. `-c`, `-S`, `--ast`, and `--check` require one `.m` input. `-c` and `-S`
are mutually exclusive.

### Build a complete program

The compiler follows imports, compiles each loaded module, adds the runtime,
and links the result:

```sh
python3 mc/mc.py mc/examples/modules/main.m -o colors.img --map colors.map
python3 mc/mc.py mc/examples/variadic.m -o variadic.img --save-temps /tmp/m-temps
```

### Compile modules separately

Imports are still read and checked for their declarations, but `-c` and `-S`
generate code only for the requested module. Compile imported implementation
modules separately and supply their objects when linking:

```sh
python3 mc/mc.py -c mc/examples/modules/color.m -o color.o
python3 mc/mc.py -c mc/examples/modules/main.m -o main.o
python3 mc/mc.py main.o color.o -o colors.img --map colors.map

# Inspect the generated assembly instead.
python3 mc/mc.py -S mc/examples/modules/color.m -o color.s
```

Object-file linking adds the startup and memory routines and the M runtime
module containing `puts`. An assembly-only program can also be linked through
`mc.py`; it must define a global `main` using the WRM ABI. In that mode, the
assembly runtime is included, but `rt.m` is not.

For custom startup code, section layout, or ELF executables, use
[`ld.py`](ld.py) directly. `mc.py` supplies startup code for its boot and ROM
targets; it does not expose an ELF executable target.

## Language overview

### Declarations and control flow

Variables, parameters, and function results require explicit types. `let`
declares an immutable value; `let mut` allows assignments. Function parameters
are immutable bindings, though a `*mut T` parameter permits modifying its pointee.

```m
let sum(values: UWord[], count: UWord): UWord {
    let mut total: UWord = 0
    for i: UWord in 0..count {
        total += values[i]
    }
    return total
}
```

- There are no semicolons. Grammar determines statement boundaries; indentation
  and newlines are whitespace.
- Conditions must be `Bool`: use `if n != 0` or `if p != null`.
- `if` and `while` accept a block or a single statement.
- `for i: T in a..b` excludes `b`; `a...b` includes it. A constant `by` step
  changes the direction or stride, including `by -1` for unsigned counters.
  Bounds are evaluated once, and the loop's own step does not overflow.
- A loop counter is immutable unless declared with `for mut`.
- `switch` uses constant `case` labels and has C-style fallthrough; use `break`
  to leave it. Put declarations inside a block within a case.
- Assignments, `++`, and `--` are statements without values.
- A function with a result must not reach its closing brace without returning
  a value. An endless `while true` without `break` also satisfies this rule.

Because newlines do not terminate expressions, a statement beginning with `*`,
`&`, `-`, `(`, or `[` may continue the preceding expression. Use blocks where
needed. For example, write `if p != null { *p = 0 }` or `if (p != null) *p = 0`.
If a condition starts with `(`, those parentheses must enclose the whole condition.

Source files use UTF-8; identifiers use ASCII. Comments support `//`, nested
`/* ... */`, and `///` documentation comments. Strings are zero-terminated UTF-8
byte sequences of type `*UByte`; a string may span several lines, and each line
break inside it becomes `\n`. ASCII character literals have type `UByte`;
directly written non-ASCII character literals have type `UWord` and contain a
Unicode code point.

### Types and arithmetic

| Type | Size | Meaning |
| --- | --- | --- |
| `Byte`, `UByte` | 1 byte | Signed / unsigned 8-bit integer |
| `Half`, `UHalf` | 2 bytes | Signed / unsigned 16-bit integer |
| `Word`, `UWord` | 4 bytes | Signed / unsigned 32-bit integer |
| `Bool` | 1 byte | `false` or `true` |
| `Float` | 4 bytes | IEEE 754 binary32 |
| Pointers and function values | 4 bytes | Data or code addresses |
| `Void` | No value | Function result or pointee of an opaque pointer |

M also supports fixed-size arrays, structures, integer-backed enums, and type
aliases. There are no 64-bit integer types or binary64 floating-point types.
`UWord` is the type used for sizes and addresses.

Numeric types do not widen or convert implicitly. Write casts with `as`:

```m
let byte: UByte = 42
let word: UWord = byte as UWord
let scaled: Float = word as Float / 2.0
```

Ordinary integer arithmetic wraps at the type's width, including signed
arithmetic. The operators `+|`, `-|`, and `*|` saturate instead. Literal-only
constant expressions are evaluated exactly and rejected if the result does not
fit the required type. Integer division by zero follows WRM instructions: the
quotient is all one bits and the remainder is the dividend.

Bitwise operators bind more tightly than comparisons, so `flags & MASK == 0`
means `(flags & MASK) == 0`. `&&` and `||` short-circuit; arguments and ordinary
binary operands are evaluated from left to right.

### Arrays and structures

```m
type Color {
    r: UByte,
    g: UByte,
    b: UByte,
}

let red: Color = { .r = 0xFF }
let values: UWord[] = [10, 20, 30]
let count: UWord = sizeof(values) / sizeof(values[0])
```

Omitted fields and elements in aggregate literals are zero-filled. Structures
and fixed arrays are values: assignment, ordinary argument passing, and return
copy them. Structure layout follows the ABI, with fields in declaration order
and natural alignment. `packed type` removes padding; the compiler accesses
unaligned fields byte by byte and rejects taking an unaligned field's address.

`T[N]` is a fixed array. In a variable declaration with an array literal, `T[]`
infers only the length. In a function parameter, `T[]` means a read-only pointer
to the first element, and `mut T[]` means a writable pointer. Pass the length
separately: there are no slices, `.len`, or array bounds checks.

Global initializers must be constant expressions. Uninitialized mutable globals
are zero-filled in `.bss`; uninitialized mutable locals contain existing stack
contents. Use an explicit initializer when a local needs a known value.

### Pointers and callbacks

`*T` permits reading the pointee, and `*mut T` also permits writing it. Pointer
mutability is independent of whether the pointer variable itself is mutable.
Use `&value` for a read-only address and `&mut value` for a writable address.
Writable addresses require mutable storage.

Pointers can be `null`. There is no pointer arithmetic; use indexing and
`&p[i]` for element addresses. `*Void` is an opaque data pointer: cast it to a
concrete pointer type before dereferencing it. String literals are read-only.

Function values are code pointers, with types such as `(value: Word): Word`.
Use the function name itself as the value, without `&`. Nested functions and
function literals are supported, but cannot capture an enclosing function's
locals or parameters; pass context explicitly.

Variadic functions use a final `args: ...` parameter:

```m
let sum(initial: Word, args: ...): Word {
    let mut total: Word = initial
    for i: UWord in 0..vaCount(args) {
        total += vaArg(args, i, Word)
    }
    return total
}
```

Additional arguments must be scalars. `vaCount` gives their count, and `vaArg`
reads an argument's representation using the requested type. The callee is
responsible for the matching type and index. `Float` stays binary32. Forward an
existing pack as the only additional argument, for example `sum(0, args)`.

## Modules and assembly

Each `.m` file is a module. Declarations are private by default; exports and
imports list names explicitly:

```m
// math.m
let twice(value: Word): Word {
    return value * 2
}

export { twice as double }
```

```m
// main.m
import { double as twice } from "math.m"

let main(argc: UWord, argv: *UByte[]): Word {
    return twice(21)
}
```

Imports search relative to the importing file, then through `-I` directories
in order. Cyclic imports are allowed. There are no wildcard imports or
re-exports. `main` is exported automatically and must have the signature shown
above for a complete program.

Exported functions and variables use global ELF symbols. Two modules exporting
the same external symbol cannot be linked together; rename an export with
`export { name as otherName }`. Renaming an import changes only its local name.
Private symbols remain local to their object files.

Declare functions or data supplied elsewhere with `extern`:

```m
extern let putByte(value: UByte): Void
export { putByte }
```

If `uart.m` has a neighboring `uart.asm`, the compiler includes that assembly
in `uart.m`'s object. It can define the declared external symbols using the
[WRM calling convention](../docs/ABI.md). Unresolved external symbols are link
errors; `--check` does not assemble implementations or resolve linker symbols.

Operand-free inline assembly is also supported:

```m
asm {
    "fence"
}
```

Inline assembly must preserve the stack pointer and callee-saved registers and
must not branch outside the block. Use an external assembly function for code
that needs explicit operands or a special section, such as a trap entry.

## Hardware access

Memory-mapped devices use volatile pointers. Each volatile access occurs once,
in source order relative to other volatile accesses, at the width of its type:

```m
let uartData: *volatile mut UWord = 0xFD00_2000 as *volatile mut UWord

let putByte(value: UByte): Void {
    *uartData = value as UWord
}
```

| Built-ins | Purpose |
| --- | --- |
| `mfcr`, `mtcr` | Read / write CPU control registers; register numbers must be constant. |
| `syscall` | Issue a system call with up to six arguments; return a `Word` result. |
| `wfi`, `hlt`, `breakpoint` | Wait for an interrupt, halt, or trigger a breakpoint trap. |
| `tlbi`, `fence` | Invalidate TLB entries or order memory accesses. |
| `clz`, `ctz`, `popcount`, `bswap`, `rotl`, `rotr` | Count bits, swap bytes, or rotate words. |
| `sizeof`, `alignof`, `offsetof` | Query size, alignment, or a structure field's offset. |
| `atomicLoad`, `atomicStore`, `atomicSwap`, `atomicAdd`, `atomicCompareSwap` | Access shared words or pointers atomically. |

Atomic values must be `Word`, `UWord`, or pointers, naturally aligned to four
bytes; `atomicAdd` accepts only the integer types. Use atomics for memory shared
with interrupt handlers and `fence()` when ordering ordinary memory against
device accesses. Volatile access alone does not provide that ordering.

See the [hardware specification](docs/spec/07-hardware.md) for signatures,
register numbers, memory ordering, and inline assembly rules.

## Images and runtime

### Boot images

The default `.img` output starts with a `WRMB` boot header and is padded to
512-byte sectors. It can be attached directly as disk 0 or a floppy image;
no partitioning or filesystem is needed. Firmware loads it at `0x00010000`.

The boot startup code clears `.bss`, installs the default trap handler, and
calls `main(0, null)` in supervisor mode. Returning from `main` writes its result
to the power controller; the emulator exits with the low eight bits as its code.

### ROM images

```sh
python3 mc/mc.py --rom wfw/src/main.m -o firmware.rom --map firmware.map
```

ROM code starts at `0xFE000000`. Startup sets the stack to `0x00100000`, copies
initialized writable data from ROM into RAM at `0x00002000`, clears `.bss`,
installs a trap handler, and calls `main(0, null)`. Code and constants stay in
ROM. The compiler requires `.data` and `.bss` to end below `0x0000C000`.

If the entry module has a neighboring `romtrap.asm`, ROM compilation uses it
instead of the default early trap handler. It must define `__trap`.

### Runtime components

| File | Responsibility |
| --- | --- |
| [`runtime/crt0.asm`](runtime/crt0.asm) | Boot header, startup, and program exit |
| [`runtime/rom0.asm`](runtime/rom0.asm) | ROM startup and writable-data initialization |
| [`runtime/trap.asm`](runtime/trap.asm) | Default trap handling |
| [`runtime/mem.asm`](runtime/mem.asm) | `memcpy` and `memset` used for aggregate operations |
| [`runtime/rt.m`](runtime/rt.m) | `puts`, which writes directly to UART |

The default trap handler returns `-ENOSYS` (`-38`) for system calls, resumes
after `breakpoint()`, and reports other traps to UART before exiting with code
`254`. A kernel or standalone program can install its own handler through `IVEC`.
These defaults describe the supplied runtime; Laix provides its own startup and
trap handling.

## Examples

The [examples directory](examples/) contains small programs with explanations
and commented examples of rejected code.

| Topic | Examples |
| --- | --- |
| First program | [`example.m`](examples/example.m) |
| Variables and initialization | [`globals.m`](examples/globals.m), [`init.m`](examples/init.m) |
| Literals and comments | [`literals.m`](examples/literals.m), [`comments.m`](examples/comments.m) |
| Arithmetic and expressions | [`arith.m`](examples/arith.m), [`float.m`](examples/float.m), [`expressions.m`](examples/expressions.m) |
| Control flow | [`if.m`](examples/if.m), [`loops.m`](examples/loops.m), [`ranges.m`](examples/ranges.m), [`switch.m`](examples/switch.m) |
| Arrays and pointers | [`arrays.m`](examples/arrays.m), [`dynarray.m`](examples/dynarray.m), [`pointers.m`](examples/pointers.m), [`void.m`](examples/void.m) |
| Data layout and enums | [`layout.m`](examples/layout.m), [`enums.m`](examples/enums.m) |
| Functions and callbacks | [`functions.m`](examples/functions.m), [`nested.m`](examples/nested.m), [`variadic.m`](examples/variadic.m) |
| Imports, exports, and externs | [`modules/`](examples/modules/), [`externs.m`](examples/externs.m) |
| Devices and CPU operations | [`mmio.m`](examples/mmio.m), [`cpu.m`](examples/cpu.m) |
| Diagnostics | [`warnings.m`](examples/warnings.m) |

## Diagnostics and tests

Diagnostics are in English and include source locations. Errors reject the
program. Warnings cover unused declarations, unnecessarily mutable variables,
uninitialized local reads, discarded function results, shadowing, unreachable
code, and misleading indentation around a single-statement body.

For checks that do not build anything, use `--check`. Frontend and compiler
helper unit tests can also run without assembling or linking images:

```sh
python3 -B mc/mc.py --check mc/examples/modules/main.m
python3 -B -m unittest discover -s mc/tests -p 'test_*.py'
```

[`tests/run.py`](tests/run.py) is the integration runner. It requires an existing
emulator binary and builds the firmware itself, then compiles and runs tests
that specify output or an exit code. It discovers annotated files under
`mc/tests/` and `mc/examples/` by default.

```sh
# These commands build images and run them in the emulator.
python3 mc/tests/run.py --emulator bin/wrm081632
python3 mc/tests/run.py mc/examples/example.m mc/examples/variadic.m
```

Runner options include `-j` / `--jobs`, `--timeout`, `-v` / `--verbose`, and
`--keep DIR`. Directives in M comments describe expectations:

| Directive | Meaning |
| --- | --- |
| `// @output "Hello, M!\n"` | Expected UART output |
| `// @exit 0` | Expected emulator exit code |
| `// @error 12: message` | Expected error on line 12; the message is matched as a substring |
| `// @warning 7: message` | Expected warning on line 7 |
| `// @args --ram 4M` | Additional emulator options |
| `// @rom` | Run the test as a ROM instead of booting it from disk |
| `// @input "text"` | Send UART input one second after launch |

Tests without `@output` or `@exit` use compiler checking only, although the
integration runner still builds firmware during setup. All reported errors
must match the expectations. Warnings are matched strictly only when a test
contains an `@warning` directive. Assembly tests use `;` comments for directives.

## Source layout

| Path | Contents |
| --- | --- |
| [`mc.py`](mc.py) | Compiler command line and build orchestration |
| [`asm.py`](asm.py) | WRM assembler for flat images and ELF objects |
| [`disasm.py`](disasm.py) | Disassembler for raw WRM images |
| [`mlang/lexer.py`](mlang/lexer.py), [`parser.py`](mlang/parser.py), [`syntax.py`](mlang/syntax.py) | Tokenization, parsing, and syntax tree definitions |
| [`mlang/modules.py`](mlang/modules.py) | Module loading and import resolution |
| [`mlang/typesys.py`](mlang/typesys.py), [`check.py`](mlang/check.py) | Types, constant evaluation, and semantic checks |
| [`mlang/codegen.py`](mlang/codegen.py), [`image.py`](mlang/image.py) | Assembly generation and per-module section emission |
| [`mlang/diag.py`](mlang/diag.py) | Source locations and diagnostics |
| [`elf.py`](elf.py), [`ld.py`](ld.py) | ELF support and linking for WRM |
| [`runtime/`](runtime/) | Boot / ROM startup, traps, memory helpers, and UART output |
| [`examples/`](examples/) | Language examples and integration test programs |
| [`tests/`](tests/) | Syntax, semantic, warning, code generation, and runtime tests |
| [`docs/`](docs/) | Language specification and compiler design |

M0 performs constant folding but otherwise has little optimization. For details
about register allocation, stack frames, argument passing, and generated
symbols, see the [compiler design notes](docs/COMPILER.md).

## Further reading

- [Language specification](docs/spec/README.md): lexical rules, types,
  declarations, expressions, statements, modules, hardware, diagnostics, and grammar.
- [Compiler design](docs/COMPILER.md): the compilation pipeline and runtime choices.
- [WRM ABI](../docs/ABI.md): data layout, calling conventions, object files, and executables.
- [Instruction set](../docs/INSTRUCTIONS.md): CPU operations and control registers.
- [Machine specification](../docs/SPECIFICATION.md): devices, memory map, and boot protocol.
- [Project README](../README.md): emulator usage, debugging, and tooling.

The language specification and compiler design notes are currently in Russian.
