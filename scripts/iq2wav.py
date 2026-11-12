#!/usr/bin/env python3
"""Stream 8-bit IQ from stdin into a WAV file that IridiScope (and SDR# / SDR++ style tools) can open.

  rtl_sdr ... - | iq2wav.py --rate 2500000 out.wav             (RTL-SDR: unsigned 8-bit, written as is)
  hackrf_transfer -r - ... | iq2wav.py --signed --rate 10000000 out.wav   (HackRF: signed 8-bit, +128)

WAV 8-bit PCM is unsigned by definition, so signed (HackRF ci8) samples are shifted by 128.
The header is written first with size fields of 0 (readers then use the data up to the end of the file),
and patched with the real sizes when the input ends. Sizes over 4 GB wrap modulo 2^32, which IridiScope
detects. Ctrl+C is ignored here: the SDR tool stops, input ends, and the header is finalised.
"""
import argparse, os, signal, struct, sys

FLIP = bytes((b ^ 0x80) for b in range(256))   # signed <-> unsigned 8-bit


def header(rate, data_bytes):
    data_bytes &= 0xFFFFFFFF
    riff = (36 + data_bytes) & 0xFFFFFFFF
    return (b"RIFF" + struct.pack("<I", riff) + b"WAVE"
            + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 2, rate, rate * 2, 2, 8)
            + b"data" + struct.pack("<I", data_bytes))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out")
    ap.add_argument("--rate", type=int, required=True, help="sample rate in Hz")
    ap.add_argument("--signed", action="store_true", help="input is signed 8-bit (HackRF ci8)")
    a = ap.parse_args()
    signal.signal(signal.SIGINT, signal.SIG_IGN)

    total = 0
    src = sys.stdin.buffer
    with open(a.out, "wb") as f:
        f.write(header(a.rate, 0))
        while True:
            buf = src.read(1 << 20)
            if not buf:
                break
            if len(buf) & 1:                  # keep I/Q pairs whole
                more = src.read(1)
                buf += more
                if not more:
                    buf = buf[:-1]
            f.write(buf.translate(FLIP) if a.signed else buf)
            total += len(buf)
        f.seek(0)
        f.write(header(a.rate, total))
    secs = total / 2 / a.rate
    if secs < 0.5:
        # nothing real arrived: e.g. hackrf_transfer prints its usage text to stdout when it finds no radio
        os.remove(a.out)
        print("no samples received from the radio", file=sys.stderr)
        sys.exit(2)
    print(f"wrote {a.out}: {total / 1e9:.2f} GB, {secs:.0f} s of IQ", file=sys.stderr)


if __name__ == "__main__":
    main()
