/* Open ALSA "default" and play 1 s of silence; then try hardware names, which
   must be refused inside the sandbox. Prints one line per attempt. */
#include <alsa/asoundlib.h>
#include <stdio.h>
#include <string.h>
#include <time.h>
static double now(void) { struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t); return t.tv_sec + t.tv_nsec / 1e9; }
int main(void) {
    snd_pcm_t *pcm; int err;
    if ((err = snd_pcm_open(&pcm, "default", SND_PCM_STREAM_PLAYBACK, 0)) < 0) {
        printf("default: open failed: %s\n", snd_strerror(err));
    } else {
        snd_pcm_set_params(pcm, SND_PCM_FORMAT_S16_LE, SND_PCM_ACCESS_RW_INTERLEAVED, 2, 48000, 1, 200000);
        static short buf[4800 * 2]; memset(buf, 0, sizeof buf);
        double t0 = now();
        for (int i = 0; i < 10; i++) snd_pcm_writei(pcm, buf, 4800);
        snd_pcm_drain(pcm);
        printf("default: played 1 s of silence in %.0f ms\n", (now() - t0) * 1000);
        snd_pcm_close(pcm);
    }
    const char *hw[] = {"hw:0,0", "hw:1,0", "plughw:1,0", "front:1"};
    for (int i = 0; i < 4; i++) {
        err = snd_pcm_open(&pcm, hw[i], SND_PCM_STREAM_PLAYBACK, SND_PCM_NONBLOCK);
        if (err < 0) printf("%s: refused (%s)\n", hw[i], snd_strerror(err));
        else { printf("%s: OPENED (would hold the card)\n", hw[i]); snd_pcm_close(pcm); }
    }
    return 0;
}
