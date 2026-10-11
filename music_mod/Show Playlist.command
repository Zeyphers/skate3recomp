#!/bin/zsh
# Shows the songs in your installed Skate 3 playlist.
cd "${0:A:h}"
if ! command -v python3 >/dev/null; then
  echo "Python 3 is not installed. Install it with:  brew install python"
else
  python3 skate3_music.py list "$@"
fi
echo
read -r "?Press Enter to close this window..."
