#!/usr/bin/env python3
"""
skymon_data.py - turn rtl_power CSV output into a compact "data image" for the
interactive skymonitor viewer (waterfall.html).

Output (written to --out-dir):
  rtl_power_data.png   8-bit greyscale PNG, one row per sweep, NEWEST ROW ON TOP.
                       pixel value v -> level in dB = offset + v * step
  rtl_power_data.json  metadata: frequencies, row times, offset/step, defaults

rtl_power CSV lines look like:
  date, time, Hz low, Hz high, Hz step, samples, dB, dB, dB, ...
All hops of one sweep share the same date+time, so lines are grouped on that.

Usage examples:
  python3 skymon_data.py /path/to/rtl_power.csv --hours 8 --out-dir ~/skymon_web
  python3 skymon_data.py day1.csv day2.csv --hours 24 --out-dir ~/skymon_web
"""
import argparse
import csv
import json
import os
import sys
from collections import deque
from datetime import datetime, timezone

import numpy as np

try:
    from PIL import Image
except ImportError:
    sys.exit("Pillow is needed: pip3 install pillow")


def parse_ts(d, t):
    d, t = d.strip(), t.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(d + " " + t, fmt)
        except ValueError:
            pass
    raise ValueError("unrecognised timestamp: %r %r" % (d, t))


def read_sweeps(paths, hours):
    """Yield (timestamp, freqs[Hz], dB[]) per complete sweep, keeping only the
    last `hours` hours (relative to the newest sweep seen)."""
    keep = deque()
    cur_key, cur_rows = None, []

    def flush():
        if not cur_rows:
            return None
        cur_rows.sort(key=lambda r: r[0])
        f = np.concatenate([r[1] for r in cur_rows])
        p = np.concatenate([r[2] for r in cur_rows])
        return f, p

    for path in paths:
        with open(path, newline="") as fh:
            for row in csv.reader(fh):
                if len(row) < 7:
                    continue
                try:
                    key = (row[0], row[1])
                    lo, hi, step = float(row[2]), float(row[3]), float(row[4])
                    vals = np.array([float(x) for x in row[6:] if x.strip() not in ("", "nan", "-nan")],
                                    dtype=np.float32)
                except ValueError:
                    continue
                if vals.size == 0:
                    continue
                freqs = lo + step * (np.arange(vals.size) + 0.5)
                if key != cur_key:
                    res = flush()
                    if res is not None:
                        ts = parse_ts(*cur_key)
                        keep.append((ts, res[0], res[1]))
                        # drop sweeps older than `hours` before the newest one
                        while keep and (ts - keep[0][0]).total_seconds() > hours * 3600:
                            keep.popleft()
                    cur_key, cur_rows = key, []
                cur_rows.append((lo, freqs, vals))
    # The very last group may be an incomplete sweep still being written; only
    # keep it if it has as many bins as the previous complete sweep.
    res = flush()
    if res is not None and keep and res[0].size == keep[-1][1].size:
        keep.append((parse_ts(*cur_key), res[0], res[1]))
    return list(keep)


def maxpool(a, axis, n):
    """Max-pool array `a` along `axis` down to at most n samples (keeps narrow peaks)."""
    size = a.shape[axis]
    if size <= n:
        return a
    f = int(np.ceil(size / n))
    pad = (-size) % f
    if pad:
        widths = [(0, 0)] * a.ndim
        widths[axis] = (0, pad)
        a = np.pad(a, widths, mode="edge")
    new_shape = list(a.shape)
    new_shape[axis] = a.shape[axis] // f
    new_shape.insert(axis + 1, f)
    return a.reshape(new_shape).max(axis=axis + 1)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", nargs="+", help="rtl_power CSV file(s), oldest first")
    ap.add_argument("--hours", type=float, default=8.0, help="how many hours back to keep (default 8)")
    ap.add_argument("--out-dir", default=".", help="where to write rtl_power_data.png/.json")
    ap.add_argument("--name", default="rtl_power_data", help="base name of the output files")
    ap.add_argument("--max-cols", type=int, default=2048, help="max frequency bins (peak-preserving)")
    ap.add_argument("--max-rows", type=int, default=2400, help="max sweeps (peak-preserving)")
    ap.add_argument("--step", type=float, default=0.25, help="dB per grey level (default 0.25 -> 64 dB range)")
    ap.add_argument("--station", default="STATION", help="label shown in the viewer")
    ap.add_argument("--cmin", type=float, default=None, help="default CMIN shown in viewer")
    ap.add_argument("--cmax", type=float, default=None, help="default CMAX shown in viewer")
    args = ap.parse_args()

    sweeps = read_sweeps(args.csv, args.hours)
    if len(sweeps) < 2:
        sys.exit("Not enough complete sweeps found in %s" % ", ".join(args.csv))

    # Reference frequency grid = the most common sweep length.
    lens = [s[1].size for s in sweeps]
    ref_len = max(set(lens), key=lens.count)
    ref_f = next(s[1] for s in sweeps if s[1].size == ref_len)
    rows, times = [], []
    for ts, f, p in sweeps:
        if f.size != ref_len or not np.allclose(f, ref_f):
            p = np.interp(ref_f, f, p).astype(np.float32)
        rows.append(p)
        times.append(ts)
    data = np.vstack(rows)                 # oldest first
    data = np.where(np.isfinite(data), data, np.nanmin(data))

    data = maxpool(data, 1, args.max_cols)
    f_lo = float(ref_f[0] - (ref_f[1] - ref_f[0]) / 2)
    f_hi = float(ref_f[-1] + (ref_f[1] - ref_f[0]) / 2)
    # time rows: pool, keeping the time of the first sweep in each group
    if data.shape[0] > args.max_rows:
        fr = int(np.ceil(data.shape[0] / args.max_rows))
        times = times[::fr]
        data = maxpool(data, 0, args.max_rows)
        times = times[:data.shape[0]]

    # Levels for encoding and default colour scale.
    floor = float(np.median(data))
    p999 = float(np.percentile(data, 99.9))
    pmax = float(data.max())
    offset = np.floor((np.percentile(data, 0.1) - 3.0) * 4) / 4   # 3 dB below the 0.1% level
    rng = 255 * args.step
    if pmax > offset + rng:
        print("note: %.1f dB of the strongest peaks clip at the top of the %.0f dB range"
              % (pmax - (offset + rng), rng), file=sys.stderr)
    v = np.clip(np.round((data - offset) / args.step), 0, 255).astype(np.uint8)

    v = v[::-1]                                   # newest on top
    times_newest_first = times[::-1]

    os.makedirs(args.out_dir, exist_ok=True)
    png = os.path.join(args.out_dir, args.name + ".png")
    js = os.path.join(args.out_dir, args.name + ".json")
    Image.fromarray(v).save(png, optimize=True)

    def iso(t):
        # rtl_power writes local time of the machine; keep it as given.
        return t.strftime("%Y-%m-%dT%H:%M:%S")

    meta = {
        "version": 1,
        "station": args.station,
        "image": os.path.basename(png),
        "rows": int(v.shape[0]),
        "cols": int(v.shape[1]),
        "row_order": "newest_first",
        "freq_lo_hz": f_lo,
        "freq_hi_hz": f_hi,
        "db_offset": float(offset),
        "db_step": float(args.step),
        "times": [iso(t) for t in times_newest_first],
        "time_zone": "local time of the recording machine",
        "stats": {"noise_floor_db": round(floor, 2), "p999_db": round(p999, 2), "max_db": round(pmax, 2)},
        "default_cmin": args.cmin if args.cmin is not None else round(floor - 4, 1),
        "default_cmax": args.cmax if args.cmax is not None else round(p999 + 2, 1),
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    tmp = js + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(meta, fh, separators=(",", ":"))
    os.replace(tmp, js)

    print("wrote %s (%dx%d, %.0f kB) and %s" % (png, v.shape[1], v.shape[0], os.path.getsize(png) / 1024, js))
    print("sweeps %d, %s .. %s, %.3f-%.3f MHz" % (len(times), iso(times[0]), iso(times[-1]), f_lo / 1e6, f_hi / 1e6))
    print("levels: noise floor %.1f dB, 99.9%% %.1f dB, max %.1f dB; encoded %.1f .. %.1f dB in %.2f dB steps"
          % (floor, p999, pmax, offset, offset + rng, args.step))


if __name__ == "__main__":
    main()
