
type String = *UByte
type Char = UWord

extern let puts(text: String): Void // "puts:" label
extern let putc(char: Char): Void // "putc:" label

export { puts, putc }