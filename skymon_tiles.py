#!/usr/bin/env python3
"""
skymon_tiles.py - cut the daily rtl_power CSVs into one data image per hour,
for waterfall_history.html (several days of scrollable history).

For every hour HH of day D (local time of this machine, same as rtl_power):
  <out-dir>/tiles/sky_YYYYMMDD_HH.png   8-bit grey, one row per sweep, NEWEST ROW ON TOP
                                        level in dB = offset + v * step (same for ALL tiles)
  <out-dir>/tiles/sky_YYYYMMDD_HH.json  sidecar (only needed locally): row times, size, stats
and an index of the last --days days:
  <out-dir>/skymon_index.json

A finished hour is made once and marked "complete"; only the current hour is
made again on every run. Lines of complete hours are skipped without parsing
the numbers, so a run normally only parses the current hour.
Each PNG that was (re)written is printed as "CHANGED <name>" for the upload script.

Usage:
  python3 skymon_tiles.py --csv-dir ~/skymon_csv --out-dir ~/skymon_web --days 3
"""
import argparse
import csv
import glob
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import numpy as np

try:
    from PIL import Image
except ImportError:
    sys.exit("Pillow is needed: pip3 install pillow")

SETTLE = timedelta(minutes=2)        # an hour counts as finished 2 min after it ended


def parse_ts(d, t):
    s = d.strip() + " " + t.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    raise ValueError("unrecognised timestamp: %r" % s)


def tile_name(hs):
    return hs.strftime("sky_%Y%m%d_%H")


def load_json(path):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(obj, fh, separators=(",", ":"))
    os.replace(tmp, path)


def read_hours(path, skip):
    """Return {hour_start: [(ts, freqs, dB), ...]} for one CSV.
    Lines whose 'YYYY-MM-DD HH' is in `skip` are not parsed."""
    hours = {}
    state = {"key": None, "rows": []}

    def flush():
        rows = state["rows"]
        if not rows:
            return
        try:
            ts = parse_ts(*state["key"])
        except ValueError:
            return
        rows.sort(key=lambda r: r[0])
        f = np.concatenate([r[1] for r in rows])
        p = np.concatenate([r[2] for r in rows])
        hours.setdefault(ts.replace(minute=0, second=0, microsecond=0), []).append((ts, f, p))

    with open(path, newline="") as fh:
        for row in csv.reader(fh):
            if len(row) < 7:
                continue
            if (row[0].strip() + " " + row[1].strip()[:2]) in skip:
                continue
            try:
                lo, step = float(row[2]), float(row[4])
                vals = np.array([float(x) for x in row[6:] if x.strip() not in ("", "nan", "-nan")],
                                dtype=np.float32)
            except ValueError:
                continue
            if vals.size == 0:
                continue
            key = (row[0], row[1])
            if key != state["key"]:
                flush()
                state["key"], state["rows"] = key, []
            state["rows"].append((lo, lo + step * (np.arange(vals.size) + 0.5), vals))
    flush()
    return hours


def make_tile(hs, sweeps, args, complete, tiles_dir):
    sweeps.sort(key=lambda s: s[0])
    lens = [s[1].size for s in sweeps]
    ref_len = max(set(lens), key=lens.count)
    ref_f = next(s[1] for s in sweeps if s[1].size == ref_len)
    df = float(ref_f[1] - ref_f[0])
    rows, times = [], []
    for ts, f, p in sweeps:
        if f.size != ref_len or not np.allclose(f, ref_f):
            # incomplete sweep (still being written, or rtl_power restarted): drop it
            if f[0] > ref_f[0] + df or f[-1] < ref_f[-1] - df:
                continue
            p = np.interp(ref_f, f, p).astype(np.float32)
        rows.append(p)
        times.append(ts)
    if not rows:
        return None
    data = np.vstack(rows)                          # oldest first
    data[~np.isfinite(data)] = args.offset
    rng = 255 * args.step
    pmax = float(data.max())
    if pmax > args.offset + rng:
        print("note: %s: strongest peaks clip %.1f dB above %.1f dB"
              % (tile_name(hs), pmax - (args.offset + rng), args.offset + rng), file=sys.stderr)
    v = np.clip(np.round((data - args.offset) / args.step), 0, 255).astype(np.uint8)
    v = v[::-1]                                     # newest on top
    times = times[::-1]

    name = tile_name(hs)
    png = os.path.join(tiles_dir, name + ".png")
    tmp = png + ".tmp"
    Image.fromarray(v).save(tmp, format="PNG", optimize=True)
    os.replace(tmp, png)
    side = {
        "png": name + ".png",
        "t0": hs.strftime("%Y-%m-%dT%H:%M:%S"),
        "dt": [int(round((t - hs).total_seconds())) for t in times],   # seconds after t0, newest first
        "rows": int(v.shape[0]),
        "cols": int(v.shape[1]),
        "freq_lo_hz": float(ref_f[0] - df / 2),
        "freq_hi_hz": float(ref_f[-1] + df / 2),
        "db_offset": float(args.offset),
        "db_step": float(args.step),
        "complete": bool(complete),
        "stats": {"noise_floor_db": round(float(np.median(data)), 2),
                  "p999_db": round(float(np.percentile(data, 99.9)), 2),
                  "max_db": round(pmax, 2)},
        "gen": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    write_json(os.path.join(tiles_dir, name + ".json"), side)
    return side


def build_index(args, tiles_dir, now):
    cur_hour = now.replace(minute=0, second=0, microsecond=0)
    cutoff = cur_hour - timedelta(hours=max(1, int(round(args.days * 24))) - 1)
    sides = []
    for path in glob.glob(os.path.join(tiles_dir, "sky_????????_??.json")):
        s = load_json(path)
        if not s:
            continue
        try:
            hs = datetime.strptime(s["t0"], "%Y-%m-%dT%H:%M:%S")
        except (KeyError, ValueError):
            continue
        if hs >= cutoff:
            sides.append((hs, s))
    sides.sort(key=lambda x: x[0], reverse=True)    # newest first
    if not sides:
        return None
    ref = sides[0][1]
    keep = []
    for hs, s in sides:
        if s["cols"] != ref["cols"] or abs(s["freq_lo_hz"] - ref["freq_lo_hz"]) > 1 \
                or abs(s["freq_hi_hz"] - ref["freq_hi_hz"]) > 1 or s["db_offset"] != ref["db_offset"] \
                or s["db_step"] != ref["db_step"]:
            print("note: %s left out of the index (other frequency range / scale)" % s["png"], file=sys.stderr)
            continue
        keep.append(s)
    recent = keep[:24]
    floor = float(np.median([s["stats"]["noise_floor_db"] for s in recent]))
    p999 = float(np.median([s["stats"]["p999_db"] for s in recent]))
    index = {
        "version": 2,
        "station": args.station,
        "cols": ref["cols"],
        "freq_lo_hz": ref["freq_lo_hz"],
        "freq_hi_hz": ref["freq_hi_hz"],
        "db_offset": ref["db_offset"],
        "db_step": ref["db_step"],
        "row_order": "newest_first",
        "time_zone": "local time of the recording machine",
        "default_cmin": args.cmin if args.cmin is not None else round(floor - 4, 1),
        "default_cmax": args.cmax if args.cmax is not None else round(p999 + 2, 1),
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tiles": [{"png": s["png"], "t0": s["t0"], "dt": s["dt"], "rows": s["rows"], "gen": s["gen"]}
                  for s in keep],
    }
    write_json(os.path.join(args.out_dir, "skymon_index.json"), index)
    return index


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv-dir", default=os.path.expanduser("~/skymon_csv"), help="folder with rtl_power_YYYY-MM-DD.csv")
    ap.add_argument("--out-dir", default=os.path.expanduser("~/skymon_web"), help="tiles/ and skymon_index.json go here")
    ap.add_argument("--days", type=float, default=3.0, help="days of history in the index (default 3)")
    ap.add_argument("--offset", type=float, default=-50.0, help="dB of grey level 0, same for all tiles (default -50)")
    ap.add_argument("--step", type=float, default=0.25, help="dB per grey level (default 0.25 -> -50 .. +13.75 dB)")
    ap.add_argument("--station", default="STATION", help="label shown in the viewer")
    ap.add_argument("--cmin", type=float, default=None, help="default CMIN shown in viewer")
    ap.add_argument("--cmax", type=float, default=None, help="default CMAX shown in viewer")
    args = ap.parse_args()

    now = datetime.now()
    tiles_dir = os.path.join(args.out_dir, "tiles")
    os.makedirs(tiles_dir, exist_ok=True)
    first_day = (now - timedelta(days=args.days)).date()
    changed = []

    for path in sorted(glob.glob(os.path.join(args.csv_dir, "rtl_power_????-??-??.csv"))):
        dstr = os.path.basename(path)[len("rtl_power_"):-len(".csv")]
        try:
            day = datetime.strptime(dstr, "%Y-%m-%d")
        except ValueError:
            continue
        if day.date() < first_day:
            continue
        done_mark = os.path.join(tiles_dir, ".done_" + dstr)
        if os.path.exists(done_mark):
            continue
        day_over = now >= day + timedelta(days=1) + SETTLE
        skip = set()
        for h in range(24):
            s = load_json(os.path.join(tiles_dir, tile_name(day + timedelta(hours=h)) + ".json"))
            if s and s.get("complete"):
                skip.add("%s %02d" % (dstr, h))
        if len(skip) < 24:
            hours = read_hours(path, skip)
            for hs in sorted(hours):
                side = make_tile(hs, hours[hs], args, now >= hs + timedelta(hours=1) + SETTLE, tiles_dir)
                if side:
                    changed.append(side["png"])
        if day_over:
            open(done_mark, "w").close()            # whole day handled, never read this CSV again

    index = build_index(args, tiles_dir, now)
    for name in changed:
        print("CHANGED " + name)
    if index:
        n = sum(t["rows"] for t in index["tiles"])
        print("index: %d tiles, %d rows, %s .. %s, %d tile(s) (re)made"
              % (len(index["tiles"]), n, index["tiles"][-1]["t0"], index["tiles"][0]["t0"], len(changed)))
    else:
        print("no tiles found for the last %.1f days" % args.days)


if __name__ == "__main__":
    main()
