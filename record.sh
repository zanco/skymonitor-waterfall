#!/bin/bash
# record.sh - run rtl_power continuously and save one CSV per day
# (rtl_power_YYYY-MM-DD.csv in CSV_DIR) for skymon_data.py / skymon_tiles.py.
#
# Example: 399-407 MHz in 4 kHz bins, one sweep every 30 s.
# Adjust frequency range, gain and ppm for your own receiver.

HERE="$(cd "$(dirname "$0")" && pwd)"
. "$HERE/config.sh" || { echo "config.sh missing - copy config.example.sh to config.sh"; exit 1; }

DEVICE="${DEVICE:-0}"      # rtl_power -d : which RTL-SDR (see rtl_test)
GAIN="${GAIN:-42}"
PPM="${PPM:-0}"
RANGE="${RANGE:-399M:407M:4k}"

mkdir -p "$CSV_DIR"
rtl_power -d "$DEVICE" -f "$RANGE" -g "$GAIN" -p "$PPM" -i 30 -c 50% \
    | awk -v dir="$CSV_DIR" -F', ' '{f=dir"/rtl_power_"$1".csv"; print >> f; fflush(f)}'

# Tip: to also feed another program (for example a classic PNG waterfall
# script), add  print; fflush()  inside the awk block and pipe it onward.
