#!/bin/sh
# mirrorwatch bootstrap: scaffold a ready-to-edit deployment directory.
#
#   curl -fsSL https://raw.githubusercontent.com/W0rkingChr1s/mirrorwatch/main/install.sh | sh
#
# Optionally pass a target directory (default: ./mirrorwatch):
#   ... | sh -s -- /opt/mirrorwatch
#
# It downloads the compose file, an example config, and an .env template, then
# prints the next steps. It never starts anything or needs root.

set -eu

DIR="${1:-mirrorwatch}"
RAW="https://raw.githubusercontent.com/W0rkingChr1s/mirrorwatch/main"

# Pick a downloader.
if command -v curl >/dev/null 2>&1; then
    fetch() { curl -fsSL "$1" -o "$2"; }
elif command -v wget >/dev/null 2>&1; then
    fetch() { wget -qO "$2" "$1"; }
else
    echo "mirrorwatch: need curl or wget to download files" >&2
    exit 1
fi

echo "mirrorwatch: setting up in $DIR/"
mkdir -p "$DIR/config"
cd "$DIR"

fetch "$RAW/docker-compose.yml" docker-compose.yml

if [ ! -f config/config.json ]; then
    fetch "$RAW/examples/html-index.json" config/config.json
else
    echo "  keeping existing config/config.json"
fi

if [ ! -f .env ]; then
    fetch "$RAW/.env.example" .env
else
    echo "  keeping existing .env"
fi

cat <<EOF

Done. Next steps:

  cd $DIR
  \$EDITOR config/config.json     # what to watch
  \$EDITOR .env                   # TELEGRAM_TOKEN, TELEGRAM_CHAT_ID

  docker compose up -d
  docker compose logs -f          # look for "baseline established"

Docs: https://github.com/W0rkingChr1s/mirrorwatch
EOF
