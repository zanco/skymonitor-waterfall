#!/bin/bash
# upload_history.sh - make hourly tiles (several days of history) for
# waterfall_history.html and upload new/changed tiles plus skymon_index.json.
#
# Cron, every 5 minutes:
#   */5 * * * * $HOME/skymonitor-waterfall/upload_history.sh
# Test without uploading:
#   NO_UPLOAD=1 ~/skymonitor-waterfall/upload_history.sh
# Tiles that failed to upload stay listed in tiles/.pending and are retried.
#
# Settings live in config.sh (copy config.example.sh to config.sh first).

HERE="$(cd "$(dirname "$0")" && pwd)"
. "$HERE/config.sh" || { echo "config.sh missing - copy config.example.sh to config.sh"; exit 1; }
LOG="$OUT_DIR/upload_history.log"
PENDING="$OUT_DIR/tiles/.pending"

mkdir -p "$OUT_DIR/tiles"
exec 9>"$OUT_DIR/.lock_history"
flock -n 9 || exit 0                       # previous run still busy: skip this one

upload() {   # upload <local file> <path below FTP_DIR>
    curl -sS --ssl-reqd ${FTP_CA:+--cacert "$FTP_CA"} --netrc-file "$HOME/.netrc" --ftp-create-dirs \
         --connect-timeout 30 --max-time 120 \
         -T "$1" "ftp://$FTP_SERVER$FTP_DIR/$2"
}

{
echo "[$(date '+%F %T')] start"
if ! OUT=$(nice -n 10 python3 "$HERE/skymon_tiles.py" --csv-dir "$CSV_DIR" --out-dir "$OUT_DIR" \
            --days "$HISTORY_DAYS" --offset "$DB_OFFSET" --station "$STATION"); then
    echo "!!! tile maker failed"; exit 1
fi
echo "$OUT" | grep -v '^CHANGED '
echo "$OUT" | sed -n 's/^CHANGED //p' >> "$PENDING"

if [ -n "$NO_UPLOAD" ]; then
    echo "NO_UPLOAD: tiles made in $OUT_DIR/tiles, not uploaded ($(sort -u "$PENDING" | grep -c .) waiting)"; exit 0
fi

# tiles first, then the index (which refers to the tiles)
LEFT=()
for name in $(sort -u "$PENDING"); do
    [ -f "$OUT_DIR/tiles/$name" ] || continue
    if upload "$OUT_DIR/tiles/$name" "tiles/$name"; then
        echo "upload ok: tiles/$name"
    else
        echo "!!! upload failed: tiles/$name (retry next run)"
        LEFT+=("$name")
    fi
done
printf '%s\n' "${LEFT[@]}" | grep . > "$PENDING"

if upload "$OUT_DIR/skymon_index.json" "skymon_index.json"; then
    echo "upload ok: skymon_index.json"
else
    echo "!!! upload failed: skymon_index.json"
fi
} >> "$LOG" 2>&1

tail -n 400 "$LOG" > "$LOG.tmp" 2>/dev/null && mv "$LOG.tmp" "$LOG"
