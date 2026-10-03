# M runtime library: gap analysis and plan

This document lists what is missing to turn the current M runtime into a
complete runtime library, the equivalent of libc plus libm for the M language
on WRM.081632. It records the state of the sources as of 2026-10-04, the
blockers, and a suggested order of work.

The analysis is based on reading the sources and documents. Nothing was
built or run.

## Contents

- [Scope and terminology](#scope-and-terminology)
- [Current state](#current-state)
- [Gaps](#gaps)
  - [1. Language and compiler limits](#1-language-and-compiler-limits)
  - [2. Startup code for Laix user tasks](#2-startup-code-for-laix-user-tasks)
  - [3. Memory for `malloc`](#3-memory-for-malloc)
  - [4. The library itself](#4-the-library-itself)
  - [5. Packaging and linking](#5-packaging-and-linking)
  - [6. Tests](#6-tests)
- [Target environments](#target-environments)
- [Suggested module layout](#suggested-module-layout)
- [Suggested order of work](#suggested-order-of-work)
- [Open questions](#open-questions)

## Scope and terminology

"Runtime library" here means two layers:

- **libc-like**: memory and string functions, number conversion, formatted
  output, character I/O, `errno`, `abort`/`assert`, allocation, sorting,
  random numbers, time.
- **libm-like**: mathematical functions. M has only `Float` (binary32), so
  this layer is single precision only (`sinf`, `sqrtf`, and so on).

The "runtime" that exists today (`mc/runtime/`) is only the minimum the
compiler itself needs: startup code, a trap handler, `memcpy`/`memset` for
aggregate copies, and `puts`.

## Current state

| Piece | Where | Notes |
| --- | --- | --- |
| Boot-image startup | `runtime/crt0.asm` | Clears `.bss`, installs `__trap`, calls `main(0, null)`, powers off with the result. |
| ROM startup | `runtime/rom0.asm` | Copies `.data` from ROM, clears `.bss`, calls `main(0, null)`. |
| Default trap handler | `runtime/trap.asm` | `-ENOSYS` for syscalls, resumes after `breakpoint()`, otherwise reports and exits with 254. |
| Aggregate copy | `runtime/mem.asm` | `memcpy`, `memset` only. |
| UART output | `runtime/rt.m` | `puts` only. `rt.m` is compiled into every program. |
| User syscall wrappers | `laix/user/syscalls.m` | `debugPutChar`, `exit`, `yield`, handle and IPC calls. |
| Kernel formatted output | `laix/src/console/console.m` | `printUnsigned`, `printInteger`, `printHex`, `printFloat`, `prints(text, args: ...)`. Tied to the kernel console. |
| Test formatted output | `mc/tests/codegen/lib/print.m` | A separate copy of number printing. |
| Random numbers | `laix/src/drivers/rnd.m` | Kernel driver, no user interface. |
| Timer | `laix/src/drivers/timer.m` | Kernel driver, no user interface. |

Number printing exists in at least three places. There is no shared
library.

## Gaps

### 1. Language and compiler limits

These constrain how the library can be written.

**No 64-bit integers and no `double`.** M has `Byte`/`Half`/`Word` and their
unsigned forms, `Bool`, and `Float` (binary32) only.

- There is no `long long`, no 64-bit `time_t`, no `strtod`, and no
  double-precision `printf`.
- The libgcc-style functions listed in [ABI.md](../../docs/ABI.md#runtime-functions)
  for 64-bit division and `double` arithmetic are meant for C compilers.
  The M compiler does not call them, so they can be skipped unless C support
  is added later.
- Where a 64-bit quantity is needed (a tick counter, a file size), represent
  it as a pair of `UWord`.

**Missing builtins.** The builtins that reach instructions are `mfcr`, `mtcr`,
`syscall`, `wfi`, `hlt`, `breakpoint`, `tlbi`, `fence`, `clz`, `ctz`,
`popcount`, `bswap`, `rotl`, `rotr`, the `sizeof` family, and the atomics
(`mlang/check.py`). The ISA has operations with no builtin:

| ISA operation | Needed for | Workaround today |
| --- | --- | --- |
| `FSQRT` | `sqrtf` | Newton iteration (slow, less exact), or an assembly stub |
| `FMADD`, `FMSUB` | accurate polynomial evaluation | Separate multiply and add (two roundings) |
| `FMIN`, `FMAX` | `fminf`, `fmaxf` | Comparisons and branches |
| `FSGNJ*` | `fabsf`, `copysignf`, `-x` | Bit-cast through memory |
| `FCLASS` | `isnan`, `isinf`, `fpclassify` | Compare `x != x`; bit tests |
| `MULH`, `MULHU`, `MULHSU` | 64-bit products, division by a constant, `mulDiv` | Assembly stub |
| `FTOI`/`FTOU`/`ITOF`/`UTOF` | casts | Already used by `as` casts |

Options: add the missing builtins to the compiler (preferred, they are
one-instruction mappings), or put a few functions in `.asm` files next to the
module (the compiler already includes a neighboring `name.asm` in the module's
object).

**Bit-casting `Float` and `UWord`.** The only way today is
`*(&number as *UWord)`, as `printFloat` does. A pair of builtins
(`floatBits`, `bitsFloat`) or a documented idiom would make `frexpf`,
`ldexpf`, `isnan` and the like cleaner and let them live in registers.

**No pointer arithmetic, generics or slices.**

- Everything goes through indexing (`p[i]`, `&p[i]`) and casts of `*Void`.
  This is enough for `memcmp`, `memmove`, `strlen`, `qsort`, but an
  allocator needs `UWord` address arithmetic with casts.
- `qsort` and `bsearch` use `*Void` plus an element size plus a comparison
  callback. Function values exist, but closures do not capture locals, so
  context must be passed explicitly.

**Variadic functions.** `args: ...` supports scalars only, read by
`vaArg(args, index, T)`; an existing pack can be forwarded as the single
extra argument. This is enough for a `printf`-like function (`prints` in
the kernel already works this way). There is no `va_list` value, so a
`vprintf` takes the pack the same way. Every `%`-conversion must read the
type the caller actually passed; there is no checking.

**Only the 32-bit integer width for sizes.** `UWord` is the size type, so
objects are limited to 4 GiB, which is irrelevant given the 128 MB RAM
limit.

### 2. Startup code for Laix user tasks

[ABI.md](../../docs/ABI.md#process-start) defines the process start block:
`sp` points to `argc`, then `argv[]`, a null, `envp[]`, a null, and the
auxiliary vector (pairs of words, ended by type 0). The startup code should:

1. set up the TLS block of the first thread and point `tp` at it;
2. zero `.bss` unless the loader did;
3. call `main(argc, argv, envp)`;
4. pass the result to `exit`.

Today none of this exists for user tasks:

- The first-task entry gives `r1` = user data base, `r2` = data size,
  `r3` = task id, an empty stack, all other registers and FCSR zero.
  There is no start block and no TLS (`laix/docs/03_USER_TASK_SYSCALLS.md`,
  `docs/ABI.md`).
- There is no ELF loader; user images are linked into the boot image.
- `main` is declared in M as `main(argc: UWord, argv: *UByte[]): Word`. The
  ABI start block also carries `envp`. Decide whether M programs see it
  (the third argument) or only the library does.

Needed: `runtime/ucrt0.asm` (or `laix/user/crt0.asm`) that adapts the
temporary entry now and the ABI start block later, plus a documented
convention for passing the initial endpoint handles to a task.

For the boot-image target (no OS), `runtime/crt0.asm` already provides
everything required.

### 3. Memory for `malloc`

An allocator needs a source of memory.

- **Boot image (no OS):** the program owns the machine. The heap can be the
  range from `__bss_end` (a linker-defined symbol) up to a limit below the
  stack top. Declare the symbol with `extern` and take its address.
- **Laix:** there is no syscall that grants memory. The existing syscalls
  are `DEBUG_PUT_CHAR`, `EXIT`, `YIELD`, `HANDLE_CLOSE`, `HANDLE_COPY`,
  `ENDPOINT_DESTROY` and the IPC calls (`laix/src/arch/wrm081632/defs.m`).
  Stage 6 lists "define the API for creating tasks / granting memory" as
  not done (`laix/docs/06_USER_SERVICES.md`).

Options for Laix:

- a page-grant syscall (a `SYS_PAGE_ALLOC`-style call returning a
  page-aligned region, with a matching free), or
- a fixed heap region mapped by the loader and described through the
  auxiliary vector (type, base, size).

The allocator itself is plain M:

- a first-fit or segregated free list with block headers and coalescing on
  `free`; or a bump allocator first, to get programs running;
- `malloc`, `calloc`, `realloc`, `free`;
- 8-byte alignment (the stack alignment in the ABI);
- no locking at first (one thread per task); revisit when threads appear.

### 4. The library itself

Nothing below exists yet unless stated.

**Memory and strings**

- `memmove`, `memcmp`, `memchr`. The ABI requires `memmove` and `memcmp`;
  the compiler may emit calls to them. Neither is in `runtime/mem.asm`
  (only `memcpy` and `memset` are).
- `strlen`, `strcmp`, `strncmp`, `strcpy`, `strncpy`, `strcat`, `strchr`,
  `strrchr`, `strstr`, `strdup`.
- UTF-8 helpers: encode and decode one code point, count code points. The
  language already treats source and string literals as UTF-8
  (`mc/tests/test_utf8.py`), and the console decodes UTF-8.

**Numbers and text**

- `utoa`/`itoa` with a base, `atoi`, `strtol`, `strtoul`.
- A single `format` function (a `printf` subset on `args: ...`): `%d %u
  %x %X %c %s %p %f %%`, with width, zero padding and left justification.
  It should write through a sink callback so the same code serves the UART,
  a buffer (`snprintf`) and the Laix console service.
- `Float` conversion: `strtof` and `%f`/`%e`/`%g` printing. `printFloat` in
  `console.m` is a starting point, but it rounds through `UWord` and has a
  limited range.

**Character I/O**

- `putchar`, `puts` (with a newline, as in libc; today's `puts` does not add
  one), `getchar`, line reading.
- Boot image: UART input needs a polling or interrupt read. Check the UART
  register map in [SPECIFICATION.md](../../docs/SPECIFICATION.md).
- Laix: output through the console service by IPC (stage 6). Until the
  service exists, the user path is `debugPutChar`, one byte per syscall.

**Process and diagnostics**

- `exit`, `abort`, `assert` (print file, line and expression, then abort).
- `errno`, as a TLS variable once `tp` is set up, otherwise one global word.
  Errors from syscalls are negative `Word` values; a single mapping from
  those to `errno` should be documented.

**Utilities**

- `qsort`, `bsearch`.
- `abs`, `min`, `max`, integer square root, `ctz`/`clz` wrappers.
- `rand`/`srand`. The kernel has an RNG driver (`rnd.m`) with no user-level
  interface; a deterministic PRNG (xorshift) is enough to start.
- `memswap`, `hash` helpers if wanted.

**Time**

- The timer is a kernel driver. A user-level read needs a syscall (a tick
  counter, perhaps a monotonic clock) before `clock`, `sleep` and `yield`
  loops with deadlines make sense. `yield` already exists.

**Math (`Float`, "libm")**

All functions are single precision. Names end in `f` to leave room for a
future `double` layer.

| Group | Functions | Approach |
| --- | --- | --- |
| Classification | `isnanf`, `isinff`, `isfinitef`, `signbitf` | Bit tests, or `FCLASS` once exposed |
| Sign, rounding | `fabsf`, `copysignf`, `floorf`, `ceilf`, `truncf`, `roundf`, `fmodf` | Bit manipulation and `FTOI` |
| Min/max | `fminf`, `fmaxf` | `FMIN`/`FMAX` or branches |
| Root | `sqrtf` | `FSQRT` (builtin or stub) |
| Decomposition | `frexpf`, `ldexpf`, `modff` | Exponent field manipulation |
| Exponential | `expf`, `exp2f`, `logf`, `log2f`, `log10f`, `powf` | Range reduction plus a polynomial |
| Trigonometry | `sinf`, `cosf`, `tanf`, `atanf`, `atan2f`, `asinf`, `acosf` | Reduction by pi/2 plus a minimax polynomial |
| Hyperbolic | `sinhf`, `coshf`, `tanhf` | Via `expf` |
| Constants | `M_PI` and related | Plain `let` constants |

Notes:

- With one FP rounding mode and no traps, edge cases follow IEEE: NaN for
  invalid operations, infinities on overflow and division by zero.
- Accuracy target: document it (for example, within 2 ulp) and test against a
  reference computed in Python.
- Argument reduction for large `sinf` arguments needs care. Without 64-bit
  integers, use Cody-Waite reduction with split constants, and document the
  accepted range.
- `FMADD` makes the polynomials both faster and more accurate; the builtin is
  worth adding before writing them.

### 5. Packaging and linking

- **`rt.m` is compiled in full into every program.** Putting the whole
  library there makes every image larger. Use separate modules imported on
  demand with `-I mc/runtime` (or a `lib/` directory), and keep `rt.m` as
  the minimum.
- **Archives.** `ld.py` reads `ar` archives and pulls in a member only if it
  defines a still-unresolved symbol, which gives per-member granularity.
  Nothing in the toolchain creates an archive: `elf.py` has only
  `read_archive`. To ship a `libm.a`, add an archive writer (in `elf.py`,
  with a small `ar.py` or a `mc.py` flag).
- **Granularity.** Because a module is one object, one function per module
  is the unit that links independently. Either keep related functions in
  one module and accept the cost, or add per-function sections and garbage
  collection to `ld.py`.
- **Exports.** There are no headers. A module's `export { ... }` list is its
  interface, and names must be unique across linked objects. Choose a naming
  rule for the library: libc names as they are (`strlen`, `memcpy`), or a
  prefix. Private helpers get the module-name prefix automatically.
- **Two targets.** The boot-image target and the Laix user target need
  different low-level parts (startup, heap source, character I/O, exit).
  Keep the portable code in the common modules and isolate these in a thin
  layer, for example `sys_boot.m` and `sys_laix.m`, selected by which object
  is linked.

### 6. Tests

- The integration runner is `mc/tests/run.py`, with `@output`, `@exit`,
  `@input` and similar directives in comments. Put runtime tests in
  `mc/tests/runtime/` (assembly) and `mc/tests/codegen/` (M programs that
  print results), and add a `mc/tests/lib/` group for the library.
- Math tests should compare against values generated from a reference
  (Python `struct`/`math` rounded to binary32) over edge cases: 0, -0,
  subnormals, large and small exponents, infinities, NaN, and a dense sweep
  of the normal range.
- Allocator tests: fragmentation patterns, `realloc` growth and shrink,
  alignment, exhaustion returning `null`.
- Formatting tests: every conversion with every flag combination, field
  width edge cases, buffer truncation in `snprintf`.
- Note: the repository rule is not to verify WRM sources by building them.
  The tests in this document apply to the M runtime and are run through the
  emulator by the project owner or through the existing runner.

## Target environments

| Aspect | Boot image (no OS) | Laix user task |
| --- | --- | --- |
| Startup | `crt0.asm`, exists | `ucrt0`, to write |
| `main` arguments | `(0, null)` | ABI start block, later |
| Heap | `__bss_end` to the stack limit | needs a page-grant syscall or fixed region |
| Character output | UART data register | `debugPutChar`, then console service by IPC |
| Character input | UART, polling | input service (stage 6, later) |
| `exit` | power controller | `SYS_EXIT` |
| `errno` | one global word | TLS via `tp` |
| Time | device registers | needs a syscall |

## Suggested module layout

```text
mc/runtime/
  crt0.asm  rom0.asm  trap.asm  mem.asm   (exist)
  rt.m                                    (keep minimal: puts)
  lib/
    string.m      mem* and str* functions, UTF-8 helpers
    convert.m     itoa, atoi, strtol, strtof
    format.m      printf-like formatter with a sink callback
    stdio.m       putchar, puts, getchar (target layer below)
    alloc.m       malloc, calloc, realloc, free
    stdlib.m      abs, qsort, bsearch, rand, abort, assert
    math.m        single-precision math
    sys_boot.m    boot target: UART I/O, heap from __bss_end, exit
    sys_laix.m    Laix target: syscalls, TLS errno, heap syscall
laix/user/
  ucrt0.asm       user startup
```

## Suggested order of work

1. **Share the formatter.** Move number printing out of the kernel console
   and the test helper into one module with a sink callback, and add
   `memmove`, `memcmp` and the string functions. This is independent of the
   OS and covers most day-to-day needs.
2. **Compiler builtins.** Add `fsqrt`, `fabs`, `fmin`, `fmax`, `fmadd`,
   `fclass`, `mulhu` and float bit-cast builtins (or assembly stubs). Then
   write the single-precision math module.
3. **Startup and memory for Laix.** Write the user startup code, decide the
   start block and the heap source, add the page-grant syscall, then the
   allocator.
4. **Library packaging.** Add an archive writer so the library can be linked
   as `libm.a` with per-member linking.
5. **User I/O.** Add `putchar`/`getchar` over the console service when stage
   6 provides it, plus a time syscall.

Steps 1 and 2 need no OS changes and can start now.

## Open questions

- Are `double` and 64-bit integers wanted in M itself? The ABI describes
  them for C, but the language specification excludes them. This decides
  whether any of the libgcc-style functions are ever needed.
- Does `main` receive `envp`, and does the library own `argv` parsing?
- Naming: libc names as they are, or a prefix or namespace?
- Is `malloc` required in the first release, or is a static or arena
  allocator enough for Laix services?
- Where does the Laix user library live: `mc/runtime/lib/` (shared) or
  `laix/user/` (OS-specific)? The split above puts portable code in `mc` and
  the system layer in each target.
