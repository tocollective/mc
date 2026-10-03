// Syntax error: line numbers stay right after a multi-line string
// @error 7: unknown escape

let S: *UByte = "a
b
c"
let T: *UByte = "\q"
