#!/bin/sh
set -eu

destination="$HOME/Library/LaunchAgents/com.vidigami.downloader.plist"

if [ "$(uname -s)" != "Darwin" ]; then
  echo "launchd uninstallation is only supported on macOS" >&2
  exit 1
fi

launchctl bootout "gui/$(id -u)" "$destination" 2>/dev/null || true
if [ -e "$destination" ]; then
  rm "$destination"
fi
echo "Removed $destination. Existing archive and logs were left untouched."
