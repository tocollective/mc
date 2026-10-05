    .option norvc
    .option norelax
    .section .text.start, "ax", @progbits
    .globl _start
_start:
    lui sp, %hi(__stack_top)
    addi sp, sp, %lo(__stack_top)
    call main
    j __text_end

    .section .text, "ax", @progbits
    .globl puts
    .type puts, @function
puts:
    mv t2, a0
    li t0, 0xfd002000
.Lnext:
    lbu t3, 0(t2)
    beqz t3, .Lnewline
.Lready:
    lw t1, 4(t0)
    andi t1, t1, 2
    beqz t1, .Lready
    sw t3, 0(t0)
    addi t2, t2, 1
    j .Lnext
.Lnewline:
    lw t1, 4(t0)
    andi t1, t1, 2
    beqz t1, .Lnewline
    li t3, 10
    sw t3, 0(t0)
    li a0, 0
    ret
    .size puts, .-puts

    .section .note.GNU-stack, "", @progbits
