#!/bin/zsh
# Puts the original Skate 3 soundtrack back.
cd "${0:A:h}"
if ! command -v python3 >/dev/null; then
  echo "Python 3 is not installed. Install it with:  brew install python"
else
  python3 skate3_music.py uninstall "$@"
fi
echo
read -r "?Press Enter to close this window..."
