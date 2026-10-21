#include <cstdio>
#include <cstring>
#include <cstdlib>

void handle(char* input) {
    char buf[16];
    strcpy(buf, input);  // classic buffer overflow sink
    printf("%s\n", buf);
}

int main(int argc, char** argv) {
    if (argc > 1) {
        handle(argv[1]);  // argv → strcpy taint
    }
    return 0;
}
