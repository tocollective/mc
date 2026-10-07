// https://rosettacode.org/wiki/Mandelbrot_set
// python3 mc/mc.py mc/examples/mandelbrot.m -o ./bin/mandelbrot.img
// bin/wrm081632 --floppy bin/mandelbrot.img --rom bin/firmware.rom
import { puts, putc } from "externs.m"

type KbdRegs {
    status: UWord,
    data: UWord,
    control: UWord,
}

type VideoRegs {
    status: UWord,
    control: UWord,
    mode: UWord,
    width: UWord,
    height: UWord,
    bpp: UWord,
    pitch: UWord,
    vramSize: UWord,
    start: UWord,
    frame: UWord,
    paletteIndex: UWord,
    paletteData: UWord,
    reserved: UWord[4],
    command: UWord,
    error: UWord,
    dstBase: UWord,
    dstPitch: UWord,
    dstXY: UWord,
    srcBase: UWord,
    srcPitch: UWord,
    srcXY: UWord,
    size: UWord,
    fg: UWord,
    bg: UWord,
    address: UWord,
    count: UWord,
}

let kbd: *volatile mut KbdRegs = 0xFD00_1000 as *volatile mut KbdRegs

type TimerRegs {
    countLo: UWord,
    countHi: UWord,
    frequency: UWord,
}

let timer: *volatile mut TimerRegs = 0xFD00_3000 as *volatile mut TimerRegs
let KBD_READY: UWord = 1

let video: *volatile mut VideoRegs = 0xFD00_7000 as *volatile mut VideoRegs
let VIDEO_VRAM_SIZE: UWord = 0x40_0000
let VIDEO_ENABLE: UWord = 1
let VIDEO_320X240: UWord = 0
let VIDEO_640X480: UWord = 1
let VIDEO_8BPP: UWord = 2 << 4
let VIDEO_FILL: UWord = 1
let VIDEO_COPY: UWord = 2
let VIDEO_EXPAND: UWord = 3
let VIDEO_LOAD: UWord = 4
let VIDEO_MEMORY: UWord = 1 << 9
let VIDEO_BUSY: UWord = 1 << 0
let VIDEO_DONE: UWord = 1 << 1
let VIDEO_ERROR: UWord = 1 << 2
let VIDEO_VBLANK: UWord = 1 << 3

let videoWaitVblank(): Void {
	video.status = VIDEO_VBLANK // clear the old flag
	while ((video.status & VIDEO_VBLANK) == 0) {}
}

let videoBlit8(data: *UByte, w: UWord, h: UWord, x: UWord, y: UWord): Bool {
    while ((video.status & VIDEO_BUSY) != 0) {}
    fence() // finish RAM writes before the DMA reads them
    for row: UWord in 0..h {
        video.address = (data as UWord) + row * w
        video.dstBase = video.start + (y + row) * video.pitch + x
        video.count = w
        video.command = VIDEO_LOAD
        while ((video.status & VIDEO_DONE) == 0) {}
        if video.error != 0 return false
    }
    return true
}

// A pixel is the number of iterations before |z| > 2, and the palette turns
// it into a colour. MAX_ITERS is the main cost knob: points inside the set
// always take all of them. The value must not exceed 255 (8 bpp).
let MAX_ITERS: UWord = 255

/// Iterations of z = z^2 + c for c = (cx, cy), up to MAX_ITERS.
// mc does not optimise: every variable lives in memory, so the loop keeps
// the work per iteration small, and the cheap tests below skip the points
// that would otherwise run to MAX_ITERS.
let mandelIters(cx: Float, cy: Float): UWord {
    let cy2: Float = cy * cy

    // the main cardioid
    let xq: Float = cx - 0.25
    let q: Float = xq * xq + cy2
    if q * (q + xq) < 0.25 * cy2 return MAX_ITERS

    // the period-2 bulb
    let xp: Float = cx + 1.0
    if xp * xp + cy2 < 0.0625 return MAX_ITERS

    let mut zx: Float = 0.0
    let mut zy: Float = 0.0
    let mut zx2: Float = 0.0
    let mut zy2: Float = 0.0
    let mut n: UWord = 0
    while zx2 + zy2 <= 4.0 && n < MAX_ITERS {
        zy = 2.0 * zx * zy + cy
        zx = zx2 - zy2 + cx
        zx2 = zx * zx
        zy2 = zy * zy
        n++
    }
    return n
}

// One line of the widest video mode (1024 pixels): the picture is computed
// and drawn line by line, so no buffer depends on the screen size.
align(4) let mut line: UByte[1024]

/// Computes line y of the picture into 'line', centred on (px, py);
/// 'step' is the size of a pixel on the plane.
let renderLine(line: *mut UByte, y: UWord, width: UWord, height: UWord,
               px: Float, py: Float, step: Float): Void {
    let hw: UWord = width / 2
    let hh: UWord = height / 2
    let cy: Float = ((y as Float) - (hh as Float)) * step + py
    let mut cx: Float = (0.0 - (hw as Float)) * step + px
    for x: UWord in 0..width {
        line[x] = mandelIters(cx, cy) as UByte
        cx = cx + step
    }
}

/// v clamped to 0..255.
let channel(v: Word): UWord {
    if v < 0 return 0
    if v > 255 return 255
    return v as UWord
}

// Black, red, yellow, white by the number of iterations; the points inside
// the set (MAX_ITERS) are black.
let resetPalette(): Void {
    video.paletteIndex = 0
    for n: UWord in 0..MAX_ITERS {
        let v: Word = ((n as Word) * 765) / (MAX_ITERS as Word)
        video.paletteData = channel(v) << 16 | channel(v - 255) << 8 | channel(v - 510)
    }
    video.paletteData = 0
}

// USB HID usage IDs, keyboard page 0x07.
let KEY_RIGHT: UWord = 0x4F
let KEY_LEFT: UWord = 0x50
let KEY_DOWN: UWord = 0x51
let KEY_UP: UWord = 0x52
let KEY_Q: UWord = 0x14
let KEY_E: UWord = 0x08
let PAN_PIXELS: Float = 32.0        // an arrow moves the view by this many pixels
let ZOOM_FACTOR: Float = 1.5        // Q and E change the pixel size this many times

// The centre of the picture, the size of a pixel on the plane (the smaller,
// the closer) and whether it has to be drawn again.
type View {
    x: Float,
    y: Float,
    step: Float,
    redraw: Bool,
}

let CHAR_A: UWord = 'a' as UWord
let CHAR_1: UWord = '1' as UWord
let CHAR_0: UWord = '0' as UWord
let CHAR_NEWLINE: UWord = '\n' as UWord
let CHAR_SPACE: UWord = ' ' as UWord

/// The character of a HID usage ID: letters (lower case), digits, Enter and
/// Space. 0 for any other key, e.g. the arrows.
let hidToChar(key: UWord): UWord {
    if key >= 0x04 && key <= 0x1D return CHAR_A + (key - 0x04)
    if key >= 0x1E && key <= 0x26 return CHAR_1 + (key - 0x1E)
    if key == 0x27 return CHAR_0
    if key == 0x28 return CHAR_NEWLINE
    if key == 0x2C return CHAR_SPACE
    return 0
}

/// Reads all queued key events; the arrows move the view, E zooms in, Q out.
let handleKeys(v: *mut View): Void {
    while ((kbd.status & KBD_READY) != 0) {
        let event: UWord = kbd.data
        // puts("key event\n")                     // debug: any event, press or release
        if event & 0x8000_0000 != 0 continue    // key release
        let key: UWord = event & 0xFFFF
        let ch: UWord = hidToChar(key)
        // if ch != 0 {
        //     putc('\'' as UWord)
        //     putc(ch)
        //     putc('\'' as UWord)
        //     putc('\n' as UWord)
        // }

        if key == KEY_RIGHT {
            v.x += PAN_PIXELS * v.step
            v.redraw = true
        }
        if key == KEY_LEFT {
            v.x -= PAN_PIXELS * v.step
            v.redraw = true
        }
        if key == KEY_DOWN {
            v.y += PAN_PIXELS * v.step
            v.redraw = true
        }
        if key == KEY_UP {
            v.y -= PAN_PIXELS * v.step
            v.redraw = true
        }
        if key == KEY_E {
            v.step = v.step / ZOOM_FACTOR
            v.redraw = true
        }
        if key == KEY_Q {
            v.step = v.step * ZOOM_FACTOR
            v.redraw = true
        }

        // if v.redraw puts("Redraw!\n")
    }
}

/// v rounded to the nearest integer.
let roundToWord(v: Float): Word {
    if v < 0.0 return (v - 0.5) as Word
    return (v + 0.5) as Word
}

/// Draws the picture centred on (px, py) with pixels of size 'step', line by
/// line. The set is symmetric about the real axis (cy = 0), so a line and its
/// mirror image are one computation, drawn twice. The axis is moved to the
/// nearest half-pixel to make the mirror line an exact line of the screen.
/// Between lines the keyboard is read: when a key changes the view, v.redraw
/// is set, the drawing stops and the caller starts it again with the new view.
/// False if the video card reports an error.
let drawFrame(width: UWord, height: UWord, v: *mut View): Bool {
    let px: Float = v.x
    let py: Float = v.y
    let step: Float = v.step
    // 2 * py / step: where the axis is, in half pixels from the centre
    let shift: Word = roundToWord(2.0 * py / step)
    let axisY: Float = (shift as Float) * step * 0.5
    // the mirror of line y is 'sum' - y
    let sum: Word = 2 * ((height / 2) as Word) - shift      // 2 * hh, as in renderLine
    for y: UWord in 0..height {
        let mirror: Word = sum - (y as Word)
        let hasMirror: Bool = mirror >= 0 && mirror < (height as Word)
        // drawn already as the mirror of an earlier line
        if hasMirror && mirror < (y as Word) continue

        handleKeys(v)
        if v.redraw return true         // the view has changed: this picture is stale
        renderLine(line, y, width, height, px, axisY, step)
        if (videoBlit8(&line[0], width, 1, 0, y) == false) return false
        if hasMirror && mirror != (y as Word) {
            if (videoBlit8(&line[0], width, 1, 0, mirror as UWord) == false) return false
        }
    }
    return true
}

/// Writes v in decimal to the UART.
let putDec(v: UWord): Void {
    if v >= 10 putDec(v / 10)
    putc(CHAR_0 + v % 10)
}

let main(): Word {
    puts("Mandelbrot!\n")

    // 320x240, 8 bpp: a quarter of the pixels of the console mode
    video.control = 0
    video.mode = VIDEO_320X240 | VIDEO_8BPP
    video.start = 0
    video.control = VIDEO_ENABLE

    let width: UWord = video.width
    let height: UWord = video.height
    // 8 bpp: a pixel is a palette index, a line is 'width' bytes
    if video.bpp != 8 || width > 1024 {
        puts("Unexpected video mode!\n")
        hlt()
    }

    resetPalette()

    let mut view: View = {}
    view.step = 4.0 / (width as Float)      // 4 units of the plane across the screen
    view.redraw = true
    while true {
        handleKeys(&mut view)

        if view.redraw {
            view.redraw = false     // set again by a key pressed while drawing
            let start: UWord = timer.countLo
            if !drawFrame(width, height, &mut view) {
                puts("Error!\n")
                hlt()
            }
            if !view.redraw {
                // a finished picture, not an interrupted one
                let ticks: UWord = timer.countLo - start
                puts("frame: ")
                putDec(ticks)
                puts(" ticks, ")
                putDec(ticks / (timer.frequency / 1000))
                puts(" ms\n")
            }
        }

        videoWaitVblank()
    }

    return 0
}
