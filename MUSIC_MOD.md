# Skate 3 Recomp: Custom Music Mod

This fork of [skate3recomp](https://github.com/mchughalex/skate3recomp) (v2.0.2) adds:

- **Your own songs in the in-game playlist.** Drop music files in a folder, run one script,
  and they replace the soundtrack in the game's playlist with the right titles and artists.
- **Music Volume and Game Volume sliders** in the settings menu (Esc → Audio), so you can
  turn the skateboard and sound effects down without touching the music, or the other way round.
- **One-click scripts** for Windows and macOS to install, list and remove your songs.
- **Build scripts** that turn this repository into a Windows `skate3.exe` or a macOS app.

The songs only play in a build made from this fork. The music data the script installs
needs this fork's patched runtime, so the official release plays the wrong audio with it.

You need your own legally obtained Xbox 360 copy of Skate 3, same as the original project.
No game files or music are included here.

---

## 1. Get the game running

1. Install and run the official release once
   ([releases page](https://github.com/mchughalex/skate3recomp/releases)): select your ISO,
   let it extract the game files, and let it install the title update when it asks.
   This leaves a `game` folder next to `skate3.exe` (or next to `skate3recomp.app` on a Mac).
2. Build this fork with your game folder (see [Building](#4-building) below) and put the new
   `skate3.exe` + DLLs (Windows) or `skate3recomp.app` (Mac) in place of the official ones.

Your folder should end up looking like this:

```
Skate 3/
├── skate3.exe            (Windows)   or   skate3recomp.app   (Mac)
├── game/                 the game files from your ISO
├── My Music/             your songs go here
└── music_mod/            the music scripts (from this repository)
```

## 2. Add your songs

1. Put your songs in the **`My Music`** folder next to the `game` folder.
   mp3, m4a, flac, wav, ogg, opus, wma and aiff all work.
   Name files `Artist - Title.mp3` if they have no tags; tags win when present.
2. Close the game.
3. Run the installer:
   - **Windows:** double-click `music_mod\Install Music Mod.bat`
   - **Mac:** double-click `music_mod/Install Music Mod.command`
     (the first time, right-click → Open if macOS blocks it)
   - or from a terminal: `python3 music_mod/skate3_music.py`
4. Start the game. Your songs are now the game's playlist, with their own titles and artists
   in the game's playlist editor.

While it works, a spinning status line at the bottom shows the current step, a timer and
(while converting) about how long is left. If the line stops spinning, something outside the
script is holding it up; see [Troubleshooting](#troubleshooting).

To change songs, add or remove files in `My Music` and run the installer again. Songs that
were converted before are reused, so only new songs take time. A first run with 43 songs
took about 2 minutes on an M-series Mac.

### More than 46 songs

Skate 3's playlist has **46 slots**. That number is built into the game's code (the list of
46 soundtrack entries is compiled into the executable, and the playlist menus and saved
playlists are built around it), so the mod can't add more entries. When you have more songs,
the installer asks what to do:

1. **Pick which songs to leave out.** It lists your songs by number; type the numbers. The
   songs you pick are moved to `My Music/Left out`, so the choice sticks. Move a file back
   into `My Music` to put it back in the playlist.
2. **Two songs in a slot**, so up to 92 songs fit. Only as many slots as needed get a second
   song, preferably two by the same artist. Both play back to back and the playlist shows
   them as "Song A / Song B". They're switched on/off and skipped together.
3. **Leave out the last ones** alphabetically.

From a terminal you can skip the question with `--fit pair` or `--fit trim`.

**Other scripts** in `music_mod`:

| Script | What it does |
|---|---|
| `Show Playlist` | Lists the songs that are installed right now |
| `Uninstall Music Mod` | Puts the original Skate 3 soundtrack back |

**Good to know**

- With fewer than 46 songs, they repeat to fill the slots.
- Every song is loudness-matched to the game's own music, so nothing is suddenly loud or quiet.
- The first install backs up the three game files it changes to `backup/game/`. Uninstall
  restores them from there.
- Converted songs are cached (about 10 MB per minute of music) so reinstalling is fast, in a
  local folder that never syncs to the cloud: `~/Library/Caches/Skate3MusicMod` on a Mac,
  `%LOCALAPPDATA%\Skate3MusicMod` on Windows, `~/.cache/Skate3MusicMod` on Linux. You can
  delete it any time; it's rebuilt when needed. (Older versions used a `mod_build` folder next
  to the game; that one can be deleted.)
- **Keep the Skate 3 folder out of iCloud Drive / OneDrive** (for example, not in Documents
  if Documents syncs). When the disk is low, the system moves synced files to the cloud and
  the script and the game have to wait for downloads. The script warns you when this applies.

### Requirements for the music script

- **Python 3.8+**: Windows `winget install -e --id Python.Python.3.12`, Mac `brew install python`
- **FFmpeg**: Windows `winget install Gyan.FFmpeg`, Mac `brew install ffmpeg`

## 3. Volume sliders

Press **Esc** (keyboard) or **RB + Start** (controller) and open **Audio**:

| Setting | Controls |
|---|---|
| **Music Volume** | The soundtrack and your playlist songs |
| **Game Volume** | Everything else: skateboard and wheel sounds, tricks, bails, speech, ambience |

Both apply instantly and are saved with your other settings. The Music Volume row also shows
how many custom songs are installed.

The game's own Audio Settings (Music, Sound FX, Dialog) still work too, and stack with
these sliders.

## 4. Building

Building recompiles the game's code from your own game files, so you need the `game` folder
from step 1 (after the title update has been installed).

### Windows (`skate3.exe`)

One-time setup in PowerShell:

```powershell
winget install -e --id Microsoft.VisualStudio.2022.BuildTools --override "--quiet --wait --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended"
winget install -e --id LLVM.LLVM
winget install -e --id Kitware.CMake
winget install -e --id Ninja-build.Ninja
winget install -e --id Python.Python.3.12
winget install -e --id Git.Git
```

Close and reopen PowerShell, then:

```powershell
git clone --recursive https://github.com/Zeyphers/skate3recomp.git
cd skate3recomp
powershell -ExecutionPolicy Bypass -File packaging\build-windows.ps1 -GameDir "C:\path\to\Skate 3\game"
```

The first build takes a while because it recompiles the whole game. The output is
`dist\Skate3Recomp-MusicMod-Windows.zip` with `skate3.exe`, its DLLs, the `music_mod`
scripts and an empty `My Music` folder. Extract it and move your `game` folder next to
`skate3.exe`. Or copy `skate3.exe` and the DLLs over your existing install.

If you don't have the title update in your game folder, pass the update package instead:
`-TitleUpdate "C:\path\to\TU_12K2276_000000C000000.00000000000O3"`.

### macOS (Apple Silicon)

```sh
brew install llvm cmake ninja molten-vk ffmpeg
git clone --recursive https://github.com/Zeyphers/skate3recomp.git
cd skate3recomp
packaging/build-macos.sh "/path/to/Skate 3/game" --install
```

`--install` puts the new `skate3recomp.app` next to your game folder, keeping the old one
in `backup/skate3recomp.app.orig`. Without it, the app is left in `dist/`.

### Linux

Follow the Linux section of [README.md](README.md). The mod is part of the runtime, so a
normal build includes it; then use `python3 music_mod/skate3_music.py`.

## Troubleshooting

| Problem | Fix |
|---|---|
| `ffmpeg not found` | Install FFmpeg (see requirements), then open a new terminal / double-click again |
| `Could not find the Skate 3 game folder` | Keep `music_mod` next to the `game` folder, or run `python skate3_music.py --game "path\to\game"` |
| `Skate 3 is running` | Close the game first; the files can't be replaced while it runs |
| Playlist shows your song names but plays other music | You're running the official build. Run the one built from this fork |
| Songs missing from the playlist | Only 46 slots; check `My Music/Left out`, or reinstall and choose "two songs in a slot" |
| Script looks frozen / status line stops moving | Usually the Skate 3 folder is in iCloud Drive or OneDrive and files are being downloaded back from the cloud. Move the folder somewhere that doesn't sync, or right-click it → Keep Downloaded (Mac) / Always keep on this device (Windows). On Windows, also check you didn't click inside the window: that pauses it until you press a key (the script turns this off while it runs) |
| Want the original soundtrack back | Run `Uninstall Music Mod` |

## How it works

Skate 3 streams its playlist from `data/audio/music/Ipod_Stream.mus` (EA-XMA audio) indexed
by `ipod.mpf`. `skate3_music.py` rebuilds both with one slot per song, sized to each song's
length, using audio chunks taken from the game's own music files. Every 2 KB XMA packet gets
a unique tag in bits the decoder ignores. The runtime (`rexglue-sdk`
`src/audio/custom_music.cpp`) loads `game/custom_music/table.bin`, recognises the tagged
packets as the game streams them, and outputs your song's audio
(`custom_music/slotNN.pcm`, 44.1 kHz stereo) instead of decoding the packet. The game
still thinks it's playing its own stream, so its playlist editor and song switching keep
working. Titles, artists and albums are patched into the string tables in
`data/big/miscboot.big`.

The volume sliders scale each decoded XMA frame in `src/audio/xma_context.cpp`. Music
streams are the stereo 44.1 kHz ones (and your songs); everything else counts as game sound.

The runtime changes live in the [rexglue-skate3 fork](https://github.com/Zeyphers/rexglue-skate3)
on the `skate3-music-mod` branch, which is this repository's `third_party/rexglue-sdk`
submodule. That fork also pins Dear ImGui to the public v1.92.5 tag. Upstream points at an
ImGui commit that can't be downloaded, so a fresh clone of the original project doesn't build.

## Updating from upstream

```sh
git remote add upstream https://github.com/mchughalex/skate3recomp.git
git fetch upstream
git merge upstream/main
```
