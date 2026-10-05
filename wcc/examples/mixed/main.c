extern int mAdd(int left, int right);
extern void puts(const char *text);

int cAdjust(int value) {
    return value + 1;
}

int main(void) {
    if (mAdd(20, 21) != 42) return 1;
    puts("M + C: 42\n");
    return 0;
}
