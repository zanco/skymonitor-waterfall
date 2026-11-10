#!/bin/bash
# upload_live.sh - build the live waterfall data (last N hours) from the
# rtl_power CSVs and upload it to your web server (FTPS via curl + ~/.netrc).
#
# Cron, every 5 minutes:
#   */5 * * * * $HOME/skymonitor-waterfall/upload_live.sh
# Test without uploading:
#   NO_UPLOAD=1 ~/skymonitor-waterfall/upload_live.sh
#
# Settings live in config.sh (copy config.example.sh to config.sh first).

HERE="$(cd "$(dirname "$0")" && pwd)"
. "$HERE/config.sh" || { echo "config.sh missing - copy config.example.sh to config.sh"; exit 1; }
LOG="$OUT_DIR/upload_live.log"

mkdir -p "$OUT_DIR"
exec 9>"$OUT_DIR/.lock_live"
flock -n 9 || exit 0                       # previous run still busy: skip this one

{
echo "[$(date '+%F %T')] start"
# yesterday's and today's CSV (enough for a few hours around midnight)
FILES=()
for d in "$(date -d yesterday +%F)" "$(date +%F)"; do
    f="$CSV_DIR/rtl_power_$d.csv"
    [ -s "$f" ] && FILES+=("$f")
done
if [ ${#FILES[@]} -eq 0 ]; then
    echo "no CSV found in $CSV_DIR"; exit 0
fi

if ! nice -n 10 python3 "$HERE/skymon_data.py" "${FILES[@]}" --hours "$LIVE_HOURS" \
        --out-dir "$OUT_DIR" --max-cols 8192 --station "$STATION"; then
    echo "!!! converter failed"; exit 1
fi

if [ -n "$NO_UPLOAD" ]; then
    echo "NO_UPLOAD: files made in $OUT_DIR, not uploaded"; exit 0
fi

# first the PNG, then the JSON (which refers to the PNG)
for name in rtl_power_data.png rtl_power_data.json; do
    if curl -sS --ssl-reqd ${FTP_CA:+--cacert "$FTP_CA"} --netrc-file "$HOME/.netrc" --ftp-create-dirs \
            --connect-timeout 30 --max-time 120 \
            -T "$OUT_DIR/$name" "ftp://$FTP_SERVER$FTP_DIR/$name"; then
        echo "upload ok: $name"
    else
        echo "!!! upload failed: $name"
    fi
done
} >> "$LOG" 2>&1

tail -n 400 "$LOG" > "$LOG.tmp" 2>/dev/null && mv "$LOG.tmp" "$LOG"
