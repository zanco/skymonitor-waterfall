# config.example.sh - copy to config.sh and fill in your own values.
# config.sh is in .gitignore, so your server details never end up on GitHub.

STATION="MYCALL"                       # label shown in the viewers

CSV_DIR="$HOME/skymon_csv"             # where record.sh writes rtl_power_YYYY-MM-DD.csv
OUT_DIR="$HOME/skymon_web"             # local output (PNG/JSON/tiles), also usable as a local web folder

LIVE_HOURS=8                           # hours shown by waterfall.html
HISTORY_DAYS=3                         # days listed for waterfall_history.html
DB_OFFSET=-50                          # dB of grey level 0 in the history tiles (0.25 dB/step -> -50..+13.75)

# FTPS upload. Login and password go in ~/.netrc, not here:
#   machine ftp.example.com login myuser password mypassword
# (chmod 600 ~/.netrc)
FTP_SERVER="ftp.example.com"
FTP_DIR="/public_html/skymonitor"      # folder on the server where the viewer pages live
FTP_CA=""                              # optional: CA certificate file if your host needs one
