// Multi-line string literals: every line break inside the quotes becomes
// one '\n' byte, escapes still work.
// @output "1\n2\n  3\n\nend\n"
// @exit 0

import { puts } from "../../examples/externs.m"

let TEXT: *UByte = "1
2
  3
\n"

let main(): Word {
    puts(TEXT)
    puts("end
")
    return 0
}
