// The same program as mandelbrot.m, in C with 'float', to compare their speed.
// python3 mc/wcc/build_rom.py --native --disk mc/examples/mandelbrot_float.c -o ./bin/mandelbrot_float.img
// bin/wrm081632 --rom bin/firmware.rom --hdd bin/mandelbrot_float.img
//
// Only the native path (--native) knows floating point: wcc replaces the
// calls of the soft-float helpers of GCC (__mulsf3 and so on) with the WRM
// instructions FMUL, FADD..., so a float operation is one instruction.
// WRM has binary32 only: write 1.0f, not 1.0, which would be a double.
// Controls: arrows move the view, E zooms in, Q zooms out.

typedef unsigned char u8;
typedef unsigned int u32;
typedef int i32;

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

// A pixel is the number of iterations before |z| > 2, and the palette turns
// it into a colour. Must not exceed 255 (8 bpp).
#define MAX_ITERS 255

// Iterations of z = z^2 + c for c = (cx, cy), up to MAX_ITERS.
static u32 mandelIters(float cx, float cy) {
	float cy2 = cy * cy;

	// the main cardioid
	float xq = cx - 0.25f;
	float q = xq * xq + cy2;
	if (q * (q + xq) < 0.25f * cy2) return MAX_ITERS;

	// the period-2 bulb
	float xp = cx + 1.0f;
	if (xp * xp + cy2 < 0.0625f) return MAX_ITERS;

	float zx = 0.0f, zy = 0.0f, zx2 = 0.0f, zy2 = 0.0f;
	u32 n = 0;
	while (zx2 + zy2 <= 4.0f && n < MAX_ITERS) {
		zy = 2.0f * zx * zy + cy;
		zx = zx2 - zy2 + cx;
		zx2 = zx * zx;
		zy2 = zy * zy;
		n++;
	}
	return n;
}

// One line of the widest video mode (1024 pixels)
static u8 line[1024] __attribute__((aligned(4)));

// Computes line y of the picture into 'line', centred on (px, py);
// 'step' is the size of a pixel on the plane.
static void renderLine(u32 y, u32 width, u32 height, float px, float py, float step) {
	float hw = (float)(width / 2);
	float hh = (float)(height / 2);
	float cy = ((float)y - hh) * step + py;
	float cx = (0.0f - hw) * step + px;
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

#define PAN_PIXELS 32.0f // an arrow moves the view by this many pixels
#define ZOOM_FACTOR 1.5f // Q and E change the pixel size this many times

typedef struct View {
	float x, y; // the centre of the picture
	float step; // the size of a pixel on the plane
	int redraw;
} View;

// Reads all queued key events; the arrows move the view, E zooms in, Q out.
static void handleKeys(View *v) {
	while (KBD->status & KBD_READY) {
		u32 event = KBD->data;
		if (event & 0x80000000u) continue; // key release
		u32 key = event & 0xFFFF;
		float pan = PAN_PIXELS * v->step;
		switch (key) {
		case KEY_RIGHT:
			v->x = v->x + pan;
			v->redraw = 1;
			break;
		case KEY_LEFT:
			v->x = v->x - pan;
			v->redraw = 1;
			break;
		case KEY_DOWN:
			v->y = v->y + pan;
			v->redraw = 1;
			break;
		case KEY_UP:
			v->y = v->y - pan;
			v->redraw = 1;
			break;
		case KEY_E:
			v->step = v->step / ZOOM_FACTOR;
			v->redraw = 1;
			break;
		case KEY_Q:
			v->step = v->step * ZOOM_FACTOR;
			v->redraw = 1;
			break;
		}
	}
}

// v rounded to the nearest integer
static i32 roundToInt(float v) {
	if (v < 0.0f) return (i32)(v - 0.5f);
	return (i32)(v + 0.5f);
}

// Draws the picture line by line. The set is symmetric about the real axis
// (cy = 0), so a line and its mirror image are one computation, drawn twice.
// The axis is moved to the nearest half-pixel to make the mirror line an
// exact line of the screen. Between lines the keyboard is read: when a key
// changes the view, v->redraw is set, the drawing stops and the caller starts
// it again with the new view. 0 if the video card reports an error.
static int drawFrame(u32 width, u32 height, View *v) {
	float px = v->x;
	float py = v->y;
	float step = v->step;
	// 2 * py / step: where the axis is, in half pixels from the centre
	i32 shift = roundToInt(2.0f * py / step);
	float axisY = (float)shift * step * 0.5f;
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

	View view = {0.0f, 0.0f, 4.0f / (float)width, 1}; // 4 units of the plane across the screen
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
