#!/bin/sh
set -eu

repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
template="$repo_dir/launchd/com.vidigami.downloader.plist.example"
destination="$HOME/Library/LaunchAgents/com.vidigami.downloader.plist"

if [ "$(uname -s)" != "Darwin" ]; then
  echo "launchd installation is only supported on macOS" >&2
  exit 1
fi

if [ ! -f "$template" ]; then
  echo "missing launchd template: $template" >&2
  exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents" "$repo_dir/logs"

# Substitute the local path while escaping XML text characters. Using an
# environment variable and a positional replacement avoids sed's replacement
# syntax interpreting path characters such as &, |, or backslash.
VIDIGAMI_REPO_DIR="$repo_dir" awk '
function xml_escape(value, result, char, position) {
  result = ""
  for (position = 1; position <= length(value); position++) {
    char = substr(value, position, 1)
    if (char == "&") char = "&amp;"
    else if (char == "<") char = "&lt;"
    else if (char == ">") char = "&gt;"
    result = result char
  }
  return result
}
{
  replacement = xml_escape(ENVIRON["VIDIGAMI_REPO_DIR"])
  line = $0
  while ((position = index(line, "__REPO_DIR__")) > 0) {
    line = substr(line, 1, position - 1) replacement \
      substr(line, position + length("__REPO_DIR__"))
  }
  print line
}
' "$template" > "$destination"

launchctl bootout "gui/$(id -u)" "$destination" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$destination"
echo "Installed and loaded $destination (daily at 18:15 local time and at login)."
