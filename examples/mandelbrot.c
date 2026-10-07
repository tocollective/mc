// The same program as mandelbrot.m, in C with Q8.24 fixed point (the one with
// 'float' is mandelbrot_float.c). Build it with --native: the plain path
// translates every instruction into a 32-word slot, about 6 times slower.
// python3 mc/wcc/build_rom.py --native --disk mc/examples/mandelbrot.c -o ./bin/mandelbrot_c.img
// bin/wrm081632 --rom bin/firmware.rom --hdd bin/mandelbrot_c.img
//
// A product is a 64-bit one, that is MUL and MULH. Range +-128, steps of
// 6e-8: about what the 24 bits of a binary32 give near |c| = 1.
// Controls: arrows move the view, E zooms in, Q zooms out.

typedef unsigned char u8;
typedef unsigned int u32;
typedef int i32;
typedef long long i64;

// ---- devices ---------------------------------------------------------------

typedef struct VideoRegs {
	u32 status;
	u32 control;
	u32 mode;
	u32 width;
	u32 height;
	u32 bpp;
	u32 pitch;
	u32 vramSize;
	u32 start;
	u32 frame;
	u32 paletteIndex;
	u32 paletteData;
	u32 reserved[4];
	u32 command;
	u32 error;
	u32 dstBase;
	u32 dstPitch;
	u32 dstXY;
	u32 srcBase;
	u32 srcPitch;
	u32 srcXY;
	u32 size;
	u32 fg;
	u32 bg;
	u32 address;
	u32 count;
} VideoRegs;

typedef struct KbdRegs {
	u32 status;
	u32 data;
	u32 control;
} KbdRegs;

typedef struct TimerRegs {
	u32 countLo;
	u32 countHi;
	u32 frequency;
} TimerRegs;

#define VIDEO ((volatile VideoRegs *)0xFD007000)
#define KBD ((volatile KbdRegs *)0xFD001000)
#define TIMER ((volatile TimerRegs *)0xFD003000)
#define UART_DATA ((volatile u32 *)0xFD002000)

#define KBD_READY 1u

#define VIDEO_ENABLE 1u
#define VIDEO_320X240 0u
#define VIDEO_8BPP (2u << 4)
#define VIDEO_LOAD 4u
#define VIDEO_BUSY (1u << 0)
#define VIDEO_DONE (1u << 1)
#define VIDEO_VBLANK (1u << 3)

// USB HID usage IDs, keyboard page 0x07
#define KEY_RIGHT 0x4F
#define KEY_LEFT 0x50
#define KEY_DOWN 0x51
#define KEY_UP 0x52
#define KEY_E 0x08
#define KEY_Q 0x14

// ---- UART ------------------------------------------------------------------

static void uartPuts(const char *s) {
	while (*s) {
		*UART_DATA = (u32)*s++;
	}
}

static void uartDec(u32 v) {
	char digits[10];
	int n = 0;
	do {
		digits[n++] = (char)('0' + v % 10);
		v /= 10;
	} while (v != 0);
	while (n > 0) {
		*UART_DATA = (u32)digits[--n];
	}
}

// ---- video -----------------------------------------------------------------

static void videoWaitVblank(void) {
	VIDEO->status = VIDEO_VBLANK; // clear the old flag
	while ((VIDEO->status & VIDEO_VBLANK) == 0) {
	}
}

// Copies h lines of w bytes, from data to the frame at (x, y), by DMA. The
// address, x and w must be multiples of 4. 0 on a DMA error.
static int videoBlit8(const u8 *data, u32 w, u32 h, u32 x, u32 y) {
	while (VIDEO->status & VIDEO_BUSY) {
	}
	__asm__ volatile("fence" ::: "memory"); // finish RAM writes before the DMA reads them
	for (u32 row = 0; row < h; row++) {
		VIDEO->address = (u32)data + row * w;
		VIDEO->dstBase = VIDEO->start + (y + row) * VIDEO->pitch + x;
		VIDEO->count = w;
		VIDEO->command = VIDEO_LOAD;
		while ((VIDEO->status & VIDEO_DONE) == 0) {
		}
		if (VIDEO->error != 0) return 0;
	}
	return 1;
}

// ---- the set ---------------------------------------------------------------

typedef i32 fx; // Q8.24
#define FRAC 24
#define FX_ONE (1 << FRAC)

static inline fx fxmul(fx a, fx b) {
	return (fx)(((i64)a * b) >> FRAC);
}

// A pixel is the number of iterations before |z| > 2, and the palette turns
// it into a colour. Must not exceed 255 (8 bpp).
#define MAX_ITERS 255

// Iterations of z = z^2 + c for c = (cx, cy), up to MAX_ITERS.
static u32 mandelIters(fx cx, fx cy) {
	fx cy2 = fxmul(cy, cy);

	// the main cardioid
	fx xq = cx - FX_ONE / 4;
	fx q = fxmul(xq, xq) + cy2;
	if (fxmul(q, q + xq) < (cy2 >> 2)) return MAX_ITERS;

	// the period-2 bulb
	fx xp = cx + FX_ONE;
	if (fxmul(xp, xp) + cy2 < FX_ONE / 16) return MAX_ITERS;

	fx zx = 0, zy = 0, zx2 = 0, zy2 = 0;
	u32 n = 0;
	while (zx2 + zy2 <= 4 * FX_ONE && n < MAX_ITERS) {
		zy = (fx)(((i64)zx * zy) >> (FRAC - 1)) + cy; // 2 * zx * zy
		zx = zx2 - zy2 + cx;
		zx2 = fxmul(zx, zx);
		zy2 = fxmul(zy, zy);
		n++;
	}
	return n;
}

// One line of the widest video mode (1024 pixels)
static u8 line[1024] __attribute__((aligned(4)));

// Computes line y of the picture into 'line', centred on (px, py);
// 'step' is the size of a pixel on the plane.
static void renderLine(u32 y, u32 width, u32 height, fx px, fx py, fx step) {
	i32 hw = (i32)(width / 2);
	i32 hh = (i32)(height / 2);
	fx cy = ((i32)y - hh) * step + py;
	fx cx = -hw * step + px;
	for (u32 x = 0; x < width; x++) {
		line[x] = (u8)mandelIters(cx, cy);
		cx += step;
	}
}

static i32 clamp255(i32 v) {
	if (v < 0) return 0;
	if (v > 255) return 255;
	return v;
}

// Black, red, yellow, white by the number of iterations; the points inside
// the set (MAX_ITERS) are black.
static void resetPalette(void) {
	VIDEO->paletteIndex = 0;
	for (i32 n = 0; n < MAX_ITERS; n++) {
		i32 v = n * 765 / MAX_ITERS;
		VIDEO->paletteData = (u32)clamp255(v) << 16 | (u32)clamp255(v - 255) << 8 | (u32)clamp255(v - 510);
	}
	VIDEO->paletteData = 0;
}

// ---- the view and the keyboard ----------------------------------------------

#define PAN_PIXELS 32
#define MAX_STEP (FX_ONE / 16) // the zoom out limit
#define MAX_COORD (16 * FX_ONE) // the centre stays inside +-16: no overflow below

typedef struct View {
	fx x, y; // the centre of the picture
	fx step; // the size of a pixel on the plane
	int redraw;
} View;

static fx clampCoord(fx v) {
	if (v > MAX_COORD) return MAX_COORD;
	if (v < -MAX_COORD) return -MAX_COORD;
	return v;
}

// Reads all queued key events; the arrows move the view, E zooms in, Q out.
static void handleKeys(View *v) {
	while (KBD->status & KBD_READY) {
		u32 event = KBD->data;
		if (event & 0x80000000u) continue; // key release
		u32 key = event & 0xFFFF;
		fx pan = PAN_PIXELS * v->step;
		fx step;
		switch (key) {
		case KEY_RIGHT:
			v->x = clampCoord(v->x + pan);
			v->redraw = 1;
			break;
		case KEY_LEFT:
			v->x = clampCoord(v->x - pan);
			v->redraw = 1;
			break;
		case KEY_DOWN:
			v->y = clampCoord(v->y + pan);
			v->redraw = 1;
			break;
		case KEY_UP:
			v->y = clampCoord(v->y - pan);
			v->redraw = 1;
			break;
		case KEY_E:
			step = v->step * 2 / 3; // 1.5 times closer
			if (step >= 1) v->step = step;
			v->redraw = 1;
			break;
		case KEY_Q:
			step = v->step * 3 / 2; // 1.5 times farther
			if (step <= MAX_STEP) v->step = step;
			v->redraw = 1;
			break;
		}
	}
}

// n / d rounded to the nearest integer
static i32 roundDiv(i32 n, i32 d) {
	return (n >= 0 ? n + d / 2 : n - d / 2) / d;
}

// Draws the picture line by line. The set is symmetric about the real axis
// (cy = 0), so a line and its mirror image are one computation, drawn twice.
// The axis is moved to the nearest half-pixel to make the mirror line an
// exact line of the screen. Between lines the keyboard is read: when a key
// changes the view, v->redraw is set, the drawing stops and the caller starts
// it again with the new view. 0 if the video card reports an error.
static int drawFrame(u32 width, u32 height, View *v) {
	fx px = v->x;
	fx py = v->y;
	fx step = v->step;
	// 2 * py / step: where the axis is, in half pixels from the centre
	i32 shift = roundDiv(2 * py, step);
	fx axisY = shift * step / 2;
	// the mirror of line y is 'sum' - y
	i32 sum = 2 * (i32)(height / 2) - shift;
	for (i32 y = 0; y < (i32)height; y++) {
		i32 mirror = sum - y;
		int hasMirror = mirror >= 0 && mirror < (i32)height;
		// drawn already as the mirror of an earlier line
		if (hasMirror && mirror < y) continue;

		handleKeys(v);
		if (v->redraw) return 1; // the view has changed: this picture is stale
		renderLine((u32)y, width, height, px, axisY, step);
		if (!videoBlit8(line, width, 1, 0, (u32)y)) return 0;
		if (hasMirror && mirror != y) {
			if (!videoBlit8(line, width, 1, 0, (u32)mirror)) return 0;
		}
	}
	return 1;
}

int main(void) {
	uartPuts("Mandelbrot (C)!\n");

	// 320x240, 8 bpp: a quarter of the pixels of the console mode
	VIDEO->control = 0;
	VIDEO->mode = VIDEO_320X240 | VIDEO_8BPP;
	VIDEO->start = 0;
	VIDEO->control = VIDEO_ENABLE;

	u32 width = VIDEO->width;
	u32 height = VIDEO->height;
	// 8 bpp: a pixel is a palette index, a line is 'width' bytes
	if (VIDEO->bpp != 8 || width > 1024) {
		uartPuts("Unexpected video mode!\n");
		return 1;
	}

	resetPalette();

	View view = {0, 0, 4 * FX_ONE / (i32)width, 1}; // 4 units of the plane across the screen
	while (1) {
		handleKeys(&view);

		if (view.redraw) {
			view.redraw = 0; // set again by a key pressed while drawing
			u32 start = TIMER->countLo;
			if (!drawFrame(width, height, &view)) {
				uartPuts("Error!\n");
				return 1;
			}
			if (!view.redraw) {
				// a finished picture, not an interrupted one
				u32 ticks = TIMER->countLo - start;
				uartPuts("frame: ");
				uartDec(ticks);
				uartPuts(" ticks, ");
				uartDec(ticks / (TIMER->frequency / 1000));
				uartPuts(" ms\n");
			}
		}

		videoWaitVblank();
	}

	return 0;
}
