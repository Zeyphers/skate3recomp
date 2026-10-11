#!/bin/zsh
# Converts the songs in "My Music" (next to your game folder) and puts them in Skate 3's playlist.
cd "${0:A:h}"
if ! command -v python3 >/dev/null; then
  echo "Python 3 is not installed. Install it with:  brew install python"
else
  python3 skate3_music.py install "$@"
fi
echo
read -r "?Press Enter to close this window..."
