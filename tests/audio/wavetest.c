/* What a game does: open the default wave output and play 1 s of silence. */
#include <windows.h>
#include <mmsystem.h>
#include <stdio.h>
int main(void) {
    WAVEFORMATEX f = {WAVE_FORMAT_PCM, 2, 48000, 48000 * 4, 4, 16, 0};
    HWAVEOUT h; static char data[48000 * 4]; WAVEHDR hdr = {data, sizeof data};
    printf("devices: %u\n", waveOutGetNumDevs());
    MMRESULT r = waveOutOpen(&h, WAVE_MAPPER, &f, 0, 0, CALLBACK_NULL);
    if (r != MMSYSERR_NOERROR) { printf("waveOutOpen failed: %u\n", r); return 1; }
    DWORD t0 = GetTickCount();
    waveOutPrepareHeader(h, &hdr, sizeof hdr);
    waveOutWrite(h, &hdr, sizeof hdr);
    while (!(hdr.dwFlags & WHDR_DONE) && GetTickCount() - t0 < 5000) Sleep(20);
    printf("played 1 s of silence in %lu ms (done=%d)\n", GetTickCount() - t0, !!(hdr.dwFlags & WHDR_DONE));
    waveOutUnprepareHeader(h, &hdr, sizeof hdr);
    waveOutClose(h);
    fflush(stdout);
    return 0;
}
