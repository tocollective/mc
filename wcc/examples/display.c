extern void wrm_halt(void) __attribute__((noreturn));

typedef unsigned int uint32_t;

typedef struct VideoRegs {
	uint32_t status;
	uint32_t control;
	uint32_t mode;
	uint32_t width;
	uint32_t height;
	uint32_t bpp;
	uint32_t pitch;
	uint32_t vramSize;
	uint32_t start;
	uint32_t frame;
	uint32_t paletteIndex;
	uint32_t paletteData;
	uint32_t reserved[4];
	uint32_t command;
	uint32_t error;
	uint32_t dstBase;
	uint32_t dstPitch;
	uint32_t dstXY;
	uint32_t srcBase;
	uint32_t srcPitch;
	uint32_t srcXY;
	uint32_t size;
	uint32_t fg;
	uint32_t bg;
	uint32_t address;
	uint32_t count;
} VideoRegs_t;

volatile VideoRegs_t* video = (volatile VideoRegs_t*)0xFD007000;
#define VIDEO_BUSY (1u << 0)
#define VIDEO_DONE (1u << 1)
#define VIDEO_ERROR (1u << 2)
#define VIDEO_VBLANK (1u << 3)

// static void videoWait(void) {
// 	while (video->status & VIDEO_BUSY) {
// 	}
// }

static void videoWaitVblank(void) {
	video->status = VIDEO_VBLANK; // clear the old flag
	while (!(video->status & VIDEO_VBLANK)) {
	}
}

#define VIDEO_FILL 1
#define CON_BG 0
#define CON_PITCH 640
#define CON_ROWS 30

void videoClear(uint32_t color) {
	video->dstBase = 0;
	video->dstPitch = CON_PITCH;
	video->dstXY = 0;
	video->size = CON_ROWS * 16 << 16 | CON_PITCH;
	video->fg = color;
	video->command = VIDEO_FILL;
}

int main(void) {
	videoClear(CON_BG);

	int i = 0;
	while (1) {
		videoClear(i);

		videoWaitVblank();
		i++;
		// wrm_halt();
	}

	return 0;
}