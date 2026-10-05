OUTPUT_ARCH(riscv)
ENTRY(_start)

SECTIONS
{
    . = __source_base;
    .text : {
        KEEP(*(.text.start))
        *(.text .text.*)
        . = ALIGN(4);
        __text_end = .;
    }
    .rodata : { *(.rodata .rodata.* .srodata .srodata.*) }
    .data : { *(.data .data.* .sdata .sdata.*) }
    .bss (NOLOAD) : {
        *(.bss .bss.* .sbss .sbss.*)
        *(COMMON)
    }
    __image_end = ALIGN(4);
    .riscv.attributes 0 : { *(.riscv.attributes) }
    .rela.text 0 : { *(.rela.text .rela.text.*) }
    .rela.rodata 0 : { *(.rela.rodata .rela.rodata.*) }
    .rela.data 0 : { *(.rela.data .rela.data.*) }
    /DISCARD/ : { *(.comment .note.* .eh_frame .eh_frame.*) }
}
