// The part of the runtime written in M (mc/docs/COMPILER.md, "Рантайм").
// mc/mc.py compiles it into every program.

let UART_DATA: *volatile mut UWord = 0xFD00_2000 as *volatile mut UWord

/// Writes a zero-terminated string to the UART. TX never blocks, so
/// there is no need to wait for TX ready.
let puts(s: *UByte): Void {
    let mut i: UWord = 0
    while s[i] != 0 {
        *UART_DATA = s[i] as UWord
        i++
    }
}

let putc(c: UWord): Void {
    *UART_DATA = c
}

export { puts, putc }
