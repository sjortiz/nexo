#!/bin/sh
# Install or remove the macOS host metrics add-on: a pinned otelcol-contrib under
# ~/.local/share/nexo/mac-metrics, run by a user LaunchAgent at login.
#   deploy/local/mac-metrics/install.sh [--uninstall] [HOST_NAME] [OTLP_PORT]
set -eu

VERSION=0.137.0
SHA256_ARM64=702a6280120733313cd02ceacb7c1ca7bd258296e33cc4277d4c5b02449a5035
SHA256_AMD64=757de6c820dbd0c14a9a1549cadc3fe836f086f91d2fb1da80d972cabbc27c50
LABEL=com.nexo.mac-metrics
HERE=$(cd "$(dirname "$0")" && pwd)
DEST="${NEXO_DATA_DIR:-$HOME/.local/share/nexo}/mac-metrics"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

if [ "$(uname -s)" != Darwin ]; then
  echo "mac-metrics: macOS only; on Linux the stack's collector already sees the host" >&2
  exit 1
fi

if [ "${1:-}" = --uninstall ]; then
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  rm -rf "$DEST"
  echo "mac-metrics: removed"
  exit 0
fi

HOST_NAME=${1:-$(hostname -s)}  # the stack names its host the same way
OTLP_PORT=${2:-4318}
case "$(uname -m)" in
  arm64) ARCH=arm64 SHA=$SHA256_ARM64 ;;
  *) ARCH=amd64 SHA=$SHA256_AMD64 ;;
esac

mkdir -p "$DEST" "$HOME/Library/LaunchAgents"
if [ ! -x "$DEST/otelcol-contrib-$VERSION" ]; then
  ARCHIVE="$DEST/otelcol-contrib.tar.gz"
  curl -fsSL -o "$ARCHIVE" \
    "https://github.com/open-telemetry/opentelemetry-collector-releases/releases/download/v$VERSION/otelcol-contrib_${VERSION}_darwin_$ARCH.tar.gz"
  echo "$SHA  $ARCHIVE" | shasum -a 256 -c - >/dev/null
  tar -xzf "$ARCHIVE" -C "$DEST" otelcol-contrib
  mv "$DEST/otelcol-contrib" "$DEST/otelcol-contrib-$VERSION"
  rm -f "$ARCHIVE"
fi
cp "$HERE/otelcol.yaml" "$DEST/otelcol.yaml"

cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>$DEST/otelcol-contrib-$VERSION</string><string>--config</string><string>$DEST/otelcol.yaml</string>
  </array>
  <key>EnvironmentVariables</key><dict>
    <key>NEXO_HOST</key><string>$HOST_NAME</string>
    <key>NEXO_OTLP_PORT</key><string>$OTLP_PORT</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardErrorPath</key><string>$DEST/otelcol.log</string>
</dict></plist>
PLIST

launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$PLIST"
echo "mac-metrics: otelcol-contrib $VERSION running as $LABEL, host.name=$HOST_NAME, OTLP 127.0.0.1:$OTLP_PORT"
