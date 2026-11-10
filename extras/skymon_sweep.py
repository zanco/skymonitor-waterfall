#!/usr/bin/env python3
"""
skymon_sweep.py - wide rtl_power-style sweep with a LimeSDR (via SoapySDR),
e.g. 2180-2290 MHz in ~20 MHz hops at 24 MS/s.

The device stays open; per hop it retunes, throws away a short settle period,
averages power spectra for --dwell seconds and keeps the clean middle part of
the band. Output is rtl_power-compatible CSV (one line per hop, all hops of one
sweep share the same date/time), written to <csv-dir>/rtl_power_YYYY-MM-DD.csv,
so skymon_tiles.py / waterfall_history.html work on it unchanged.
Levels are dB relative to full scale per FFT bin (not the same numbers as rtl_power).

Test one sweep (prints statistics per hop):
  python3 skymon_sweep.py --once --out ~/sweep_test.csv
Run continuously:
  python3 skymon_sweep.py --csv-dir ~/skymon_sband/csv

Sharing the Lime with lime_grab.sh: while --busy-file exists the sweeper closes
the device and waits; it reopens it a few seconds after the file is removed.
"""
import argparse
import os
import signal
import sys
import time
from datetime import datetime

import numpy as np
import SoapySDR
from SoapySDR import SOAPY_SDR_RX, SOAPY_SDR_CF32

OVERFLOW = getattr(SoapySDR, "SOAPY_SDR_OVERFLOW", -4)
try:
    import scipy.fft as _sfft

    def fft_rows(x):
        return _sfft.fft(x, axis=1, workers=-1)
except ImportError:
    def fft_rows(x):
        return np.fft.fft(x, axis=1)

stop = False


def on_signal(signum, frame):
    global stop
    stop = True


def log(*a):
    print(datetime.now().strftime("[%F %T]"), *a, file=sys.stderr, flush=True)


def parse_gains(s):
    g = {}
    for part in s.split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            g[k.strip()] = float(v)
    return g


class Lime:
    def __init__(self, a):
        self.a = a
        self.dev = SoapySDR.Device(a.device)
        d = self.dev
        d.setSampleRate(SOAPY_SDR_RX, 0, a.rate)
        if a.bw > 0:
            d.setBandwidth(SOAPY_SDR_RX, 0, a.bw)
        if a.antenna:
            d.setAntenna(SOAPY_SDR_RX, 0, a.antenna)
        for k, v in parse_gains(a.gain).items():
            d.setGain(SOAPY_SDR_RX, 0, k, v)
        try:
            d.setDCOffsetMode(SOAPY_SDR_RX, 0, True)
        except Exception:
            pass
        self.st = d.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CF32, [0])
        d.activateStream(self.st)
        self.buf = np.empty(a.nfft * 128, dtype=np.complex64)

    def close(self):
        try:
            self.dev.deactivateStream(self.st)
            self.dev.closeStream(self.st)
        except Exception:
            pass
        self.dev = None

    def read(self):
        sr = self.dev.readStream(self.st, [self.buf], len(self.buf), timeoutUs=1000000)
        return sr.ret

    def measure(self, f_center, win):
        """Average power spectrum (fftshifted, linear) at f_center; returns (spectrum, n_spectra, overflows, used_fraction)."""
        a, nfft = self.a, self.a.nfft
        self.dev.setFrequency(SOAPY_SDR_RX, 0, f_center)
        t_end = time.monotonic() + a.settle
        while time.monotonic() < t_end:          # throw away samples from before/while retuning
            self.read()
        acc = np.zeros(nfft, dtype=np.float64)
        n = over = errs = 0
        t0 = time.monotonic()
        t_end = t0 + a.dwell
        while time.monotonic() < t_end:
            ret = self.read()
            if ret == OVERFLOW:
                over += 1
                continue
            if ret <= 0:
                errs += 1
                if errs > 20:
                    raise RuntimeError("readStream keeps failing (%d) at %.3f MHz" % (ret, f_center / 1e6))
                continue
            m = ret // nfft
            if m == 0:
                continue
            X = fft_rows(self.buf[:m * nfft].reshape(m, nfft) * win)
            acc += (X.real ** 2 + X.imag ** 2).sum(axis=0)
            n += m
        used = n * nfft / (a.rate * (time.monotonic() - t0))
        if n == 0:
            return None, 0, over, 0.0
        return np.fft.fftshift(acc / n), n, over, used


def plan(a):
    """Hop list: (centre, low edge of kept part, number of kept bins)."""
    binw = a.rate / a.nfft
    keep = int(a.use * a.nfft) // 2 * 2
    step = keep * binw
    hops, lo = [], a.f_lo
    while lo < a.f_hi - binw / 2:
        nb = min(keep, int(round((a.f_hi - lo) / binw)))
        centre = lo + (keep // 2) * binw + binw / 2     # FFT bin nfft/2 (DC) sits at the centre
        hops.append((centre, lo, nb))
        lo += step
    return hops, binw, keep


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--device", default="driver=lime")
    ap.add_argument("--f-lo", type=float, default=2180e6, help="start frequency in Hz (default 2180e6)")
    ap.add_argument("--f-hi", type=float, default=2290e6, help="end frequency in Hz (default 2290e6)")
    ap.add_argument("--rate", type=float, default=24e6, help="sample rate (default 24e6)")
    ap.add_argument("--bw", type=float, default=22e6, help="analog bandwidth, 0 = driver default (default 22e6)")
    ap.add_argument("--use", type=float, default=0.84, help="fraction of each hop kept, middle part (default 0.84 = ~20 MHz)")
    ap.add_argument("--nfft", type=int, default=2048, help="FFT size: 2048 at 24 MS/s = 11.7 kHz bins")
    ap.add_argument("--gain", default="TIA=2,PGA=10,LNA=28", help="gain elements as in lime_grab.sh")
    ap.add_argument("--antenna", default="LNAH")
    ap.add_argument("--dwell", type=float, default=3.5, help="seconds of averaging per hop (default 3.5)")
    ap.add_argument("--settle", type=float, default=0.15, help="seconds thrown away after each retune")
    ap.add_argument("--interval", type=float, default=30, help="start a sweep every N seconds (default 30)")
    ap.add_argument("--csv-dir", default=os.path.expanduser("~/skymon_sband/csv"))
    ap.add_argument("--out", default=None, help="write to this file instead of daily CSVs ('-' = stdout)")
    ap.add_argument("--busy-file", default=os.path.expanduser("~/.lime_busy"),
                    help="while this file exists the Lime is released (for lime_grab.sh)")
    ap.add_argument("--once", action="store_true", help="one sweep with statistics, then stop")
    a = ap.parse_args()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    hops, binw, keep = plan(a)
    log("%d hops of %.3f MHz, %d bins of %.2f kHz, %.3f-%.3f MHz, dwell %.1f s"
        % (len(hops), keep * binw / 1e6, sum(h[2] for h in hops), binw / 1e3, a.f_lo / 1e6, a.f_hi / 1e6, a.dwell))
    win = np.hanning(a.nfft).astype(np.float32)
    norm = float(win.sum()) ** 2                      # full-scale sine -> 0 dB
    if a.out is None:
        os.makedirs(a.csv_dir, exist_ok=True)

    lime = None
    while not stop:
        if os.path.exists(a.busy_file):
            if lime:
                lime.close(); lime = None
                log("busy file present: Lime released")
            time.sleep(2)
            continue
        if lime is None:
            try:
                time.sleep(3)
                lime = Lime(a)
                log("Lime opened")
            except Exception as e:
                log("cannot open Lime (%s), retry in 10 s" % e)
                lime = None
                time.sleep(10)
                continue

        t_start = time.time()
        now = datetime.now()
        lines, ok = [], True
        for centre, lo, nb in hops:
            if stop or os.path.exists(a.busy_file):
                ok = False
                break
            try:
                spec, n, over, used = lime.measure(centre, win)
            except Exception as e:
                log("error: %s - reopening Lime" % e)
                lime.close(); lime = None
                ok = False
                break
            if spec is None:
                ok = False
                break
            db = 10 * np.log10(spec / norm + 1e-30)
            c = a.nfft // 2
            db[c - 1:c + 2] = (db[c - 4:c - 1].mean() + db[c + 2:c + 5].mean()) / 2   # hide the DC spike
            part = db[c - keep // 2: c - keep // 2 + nb]
            lines.append("%s, %s, %.0f, %.0f, %.2f, %d, %s" % (
                now.strftime("%Y-%m-%d"), now.strftime("%H:%M:%S"), lo, lo + nb * binw, binw, n,
                ", ".join("%.1f" % v for v in part)))
            if a.once:
                log("hop %.3f MHz: %d spectra, %.0f%% of samples used, %d overflows, dB min %.1f median %.1f max %.1f"
                    % (centre / 1e6, n, used * 100, over, part.min(), np.median(part), part.max()))
        if ok and lines:
            if a.out == "-":
                fh = sys.stdout
            else:
                path = a.out or os.path.join(a.csv_dir, "rtl_power_%s.csv" % now.strftime("%Y-%m-%d"))
                fh = open(path, "a")
            fh.write("\n".join(lines) + "\n")
            fh.flush()
            if fh is not sys.stdout:
                fh.close()
            if a.once:
                log("sweep took %.1f s" % (time.time() - t_start))
        if a.once:
            break
        while not stop and time.time() < t_start + a.interval:
            time.sleep(0.2)

    if lime:
        lime.close()
    log("stopped")


if __name__ == "__main__":
    main()
