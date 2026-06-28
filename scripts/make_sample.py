"""Synthesize a short, pleasant royalty-free sample track (no deps, stdlib only).

A 4-chord loop (C - G - Am - F) with an arpeggiated melody + soft pad, so the
app ships with a 'Try a sample track' option. Output: app/static/sample.wav
"""
import math
import os
import struct
import wave

SR = 44100
BPM = 100
BEAT = 60.0 / BPM

NOTES = {  # frequencies (Hz)
    "C3": 130.81, "E3": 164.81, "G3": 196.00, "A3": 220.00, "F3": 174.61, "D3": 146.83, "B3": 246.94,
    "C4": 261.63, "D4": 293.66, "E4": 329.63, "F4": 349.23, "G4": 392.00, "A4": 440.00, "B4": 493.88,
    "C5": 523.25, "E5": 659.25,
}

# (chord pad notes, arpeggio melody notes) per bar
PROGRESSION = [
    (["C3", "E3", "G3"], ["C4", "E4", "G4", "C5"]),
    (["G3", "B3", "D4"], ["G4", "B4", "D4", "G4"]),
    (["A3", "C4", "E4"], ["A4", "C5", "E5", "A4"]),
    (["F3", "A3", "C4"], ["F4", "A4", "C5", "F4"]),
]


def env(t, dur, a=0.01, r=0.2):
    if t < a:
        return t / a
    if t > dur - r:
        return max(0.0, (dur - t) / r)
    return 1.0


def tone(freq, t, harmonics=(1.0, 0.45, 0.2)):
    s = 0.0
    for i, amp in enumerate(harmonics, start=1):
        s += amp * math.sin(2 * math.pi * freq * i * t)
    return s / sum(harmonics)


def main():
    bars = 2  # repeat the 4-chord loop twice -> 8 bars total
    total = []
    for _ in range(bars):
        for pad_notes, mel_notes in PROGRESSION:
            bar_dur = 4 * BEAT
            n = int(bar_dur * SR)
            arp_dur = bar_dur / len(mel_notes)
            for i in range(n):
                t = i / SR
                # sustained pad chord
                pad = sum(tone(NOTES[p], t) for p in pad_notes) / len(pad_notes)
                pad *= 0.28 * env(t, bar_dur, a=0.05, r=0.3)
                # arpeggiated melody
                idx = min(len(mel_notes) - 1, int(t / arp_dur))
                lt = t - idx * arp_dur
                mel = tone(NOTES[mel_notes[idx]], lt) * 0.5 * env(lt, arp_dur, a=0.005, r=0.08)
                total.append(pad + mel)

    peak = max(abs(x) for x in total) or 1.0
    out = os.path.join(os.path.dirname(__file__), "..", "app", "static", "sample.wav")
    out = os.path.abspath(out)
    with wave.open(out, "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        frames = b"".join(struct.pack("<h", int(max(-1, min(1, x / peak * 0.9)) * 32767)) for x in total)
        w.writeframes(frames)
    print(f"wrote {out} ({len(total)/SR:.1f}s, {os.path.getsize(out)//1024} KB)")


if __name__ == "__main__":
    main()
