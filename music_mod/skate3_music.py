#!/usr/bin/env python3
"""Skate 3 custom playlist music mod.

Put your songs in the "My Music" folder next to the game folder, then run:

    python skate3_music.py              build the mod from your songs and install it
    python skate3_music.py list         show the playlist that is installed now
    python skate3_music.py uninstall    put the game's original soundtrack back

Your songs only play in the modded build of the game from this repository; see MUSIC_MOD.md.

How it works: the game's playlist stream (data/audio/music/Ipod_Stream.mus + ipod.mpf) is
rebuilt with one slot per song, sized to the song's length, out of existing EA-XMA chunks
from the game's own music files. Their audio is never heard: every packet gets a unique tag
in bits the XMA frame parser ignores, and the patched runtime recognises the tagged packets
(custom_music/table.bin maps each one to a slot and sample position) and outputs the song's
PCM (custom_music/slotNN.pcm, 44.1 kHz stereo s16 big-endian) instead of decoding them. The
playlist titles, artists and albums in data/big/miscboot.big are renamed to match.

The originals of the three game files the mod replaces are kept in backup/ next to the
game folder, and every build starts from those originals.
"""
import argparse
import concurrent.futures
import hashlib
import json
import os
import random
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ebbig  # noqa: E402

HERE = Path(__file__).resolve().parent

SAMPLE_RATE = 44100
FRAME_SAMPLES = 512
START_SKIP = 384  # samples EA-XMA chunks drop from their first frame
PACKET_BYTES = 0x800
KEY_BYTES = 64
NUM_SLOTS = 46  # songs in the game's playlist
AUDIO_EXTENSIONS = (".m4a", ".mp3", ".flac", ".wav", ".ogg", ".aac", ".opus", ".wma", ".aiff", ".aif")

# Game files the mod replaces (relative to the game folder).
MODDED_FILES = ("data/audio/music/ipod.mpf", "data/audio/music/Ipod_Stream.mus", "data/big/miscboot.big")

LANG_TABLES = [0x1272BC66, 0x8339276A, 0xAE61901A, 0xB57E3E62, 0xD0436F8A, 0xEB8C4822, 0xEDCB897A]
KEY_TABLE = 0x6755CB5C

TARGET_LUFS = -11.0  # about where the game's own music sits
TRUE_PEAK = -1.0
PCM_CACHE_VERSION = 2  # bump when decode_pcm's output or the cache key changes
PAIR_GAP_SECONDS = 1  # silence between two songs sharing a playlist slot
LEFT_OUT_DIR = "Left out"  # songs moved here (inside your music folder) stay out of the playlist
SF_DATALESS = 0x40000000  # macOS st_flags: the file's contents are only in iCloud


class UserError(Exception):
    """A problem the user can fix; printed without a traceback."""


class Status:
    """A live status line (spinner, step, elapsed time) so long steps never look frozen.

    In a terminal the line redraws in place several times a second; when output goes
    somewhere else (a log, an IDE) a "still working" line is printed every 20 seconds.
    """
    SPINNER = "|/-\\"

    def __init__(self):
        self.text = ""
        self.lock = threading.Lock()
        self.tty = sys.stdout.isatty()
        self.t0 = time.time()
        self.drawn = False
        self.last_beat = 0.0
        self.stopping = threading.Event()
        self.thread = None

    def __enter__(self):
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stopping.set()
        self.thread.join()
        with self.lock:
            self._clear()

    def set(self, text):
        with self.lock:
            self.text = text
            self.last_beat = time.time()
            if not text:
                self._clear()

    def say(self, *args):
        """print() for permanent lines, kept above the status line."""
        with self.lock:
            self._clear()
            print(*args, flush=True)

    def _width(self):
        return max(20, shutil.get_terminal_size((80, 20)).columns - 1)

    def _clear(self):
        if self.drawn:
            sys.stdout.write("\r" + " " * self._width() + "\r")
            sys.stdout.flush()
            self.drawn = False

    def _run(self):
        tick = 0
        while not self.stopping.wait(0.25):
            tick += 1
            with self.lock:
                if not self.text:
                    continue
                elapsed = int(time.time() - self.t0)
                line = f"{self.SPINNER[tick % 4]} {self.text}  [{elapsed // 60}:{elapsed % 60:02d}]"
                if self.tty:
                    sys.stdout.write("\r" + line[:self._width()].ljust(self._width()))
                    sys.stdout.flush()
                    self.drawn = True
                elif time.time() - self.last_beat >= 20:
                    print(f"  ...still working: {line[2:]}", flush=True)
                    self.last_beat = time.time()


status = Status()
say = status.say


def disable_quick_edit():
    """On Windows, clicking inside a console window pauses the program until a key is
    pressed (QuickEdit mode), which looks exactly like a freeze. Turn it off while we run."""
    if sys.platform != "win32":
        return
    try:
        import atexit
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-10)  # STD_INPUT_HANDLE
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            ENABLE_QUICK_EDIT_MODE, ENABLE_EXTENDED_FLAGS = 0x0040, 0x0080
            kernel32.SetConsoleMode(handle, (mode.value & ~ENABLE_QUICK_EDIT_MODE) | ENABLE_EXTENDED_FLAGS)
            atexit.register(kernel32.SetConsoleMode, handle, mode.value)
    except (AttributeError, OSError):
        pass


def be32(b, o):
    return struct.unpack_from(">I", b, o)[0]


# ---------------------------------------------------------------- packet keys

def packet_key(packet):
    # FNV-1a over bytes 0..2 and 4..63; byte 3 (skip count) is left out in case the game's
    # streamer rewrites it. Must match PacketKey() in rexglue-sdk src/audio/custom_music.cpp.
    h = 0xCBF29CE484222325
    plen = len(packet)
    for i in (0, 1, 2):
        if i < plen:
            h = ((h ^ packet[i]) * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    end = min(KEY_BYTES, plen)
    for i in range(4, end):
        h = ((h ^ packet[i]) * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return h


class Bits:
    def __init__(self, data):
        self.d = data

    def read(self, pos, n=15):
        b = pos >> 3
        plen = len(self.d)
        if b + 2 < plen:
            v = (self.d[b] << 16) | (self.d[b + 1] << 8) | self.d[b + 2]
        else:
            p0 = self.d[b] if b < plen else 0
            p1 = self.d[b + 1] if b + 1 < plen else 0
            p2 = self.d[b + 2] if b + 2 < plen else 0
            v = (p0 << 16) | (p1 << 8) | p2
        return (v >> (24 - n - (pos & 7))) & ((1 << n) - 1)


def structural_bits(packet, carry_header_bits):
    """Bits the XMA parser reads in this packet (within the key window).

    carry_header_bits: how many bits of a frame-length header split off the previous
    packet continue at the start of this one (0 if none).
    Returns (set of bit indices, header bits carried into the next packet).
    """
    window = KEY_BYTES * 8
    s = set(range(32 + carry_header_bits))
    bits = Bits(packet)
    first = (((packet[0] & 3) << 13) | (packet[1] << 5) | (packet[2] >> 3)) + 32
    if 32 < first <= window:
        s.add(first - 1)  # end flag of the frame continued from the previous packet
    pos = first
    carry = 0
    total = PACKET_BYTES * 8
    while pos < total:
        if pos + 15 > total:
            carry = pos + 15 - total
            if pos < window:
                s.update(range(pos, window))
            break
        if pos < window:
            s.update(range(pos, min(window, pos + 15)))
        length = bits.read(pos, 15)
        if length == 0x7FFF or length < 16:
            break
        end_bit = pos + length - 1
        if end_bit < window:
            s.add(end_bit)
        pos += length
    return s, carry


def get_packet_frames_old(packet):
    """Count frames and split status the way ReXGlue's old XMA frame decoder does."""
    fo = (((packet[0] & 3) << 13) | (packet[1] << 5) | (packet[2] >> 3)) + 32
    if fo > 16384 - 15:
        return 0, False
    pos = fo
    count = 0
    bits = Bits(packet)
    while True:
        if 16384 - pos < 15:
            return count, False
        count += 1
        size = bits.read(pos, 15)
        pos += 15
        if size == 0x7FFF or size < 16 or (size - 15) > (16384 - pos):
            return count, True
        pos += (size - 16)
        trailer = bits.read(pos, 1)
        pos += 1
        if trailer == 0 or pos >= 16384:
            return count, False


# ---------------------------------------------------------------- EA MUS/MPF

class Mus:
    def __init__(self, path):
        self.path = path
        self.f = open(path, "rb")
        head = self.f.read(0x28)
        self.checksum = head[0:4]
        self.count = struct.unpack_from("<I", head, 4)[0]
        table = self.f.read(self.count * 0x1C)
        self.entries = []
        for i in range(self.count):
            e = table[i * 0x1C:(i + 1) * 0x1C]
            ehash = e[0:4]
            snr, sns, snr_size, sns_size = struct.unpack_from(">IIII", e, 8)
            self.entries.append((ehash, snr * 0x10, sns * 0x80, snr_size, sns_size))

    def chunk(self, i):
        ehash, snr, sns, snr_size, sns_size = self.entries[i]
        self.f.seek(snr)
        snr_bytes = self.f.read(snr_size)
        self.f.seek(sns)
        sns_bytes = self.f.read(sns_size)
        return ehash, snr_bytes, sns_bytes

    def close(self):
        self.f.close()


def snr_info(snr):
    w0, w1 = struct.unpack(">II", snr[:8])
    return (w0 >> 24) & 0xF, ((w0 >> 18) & 0x3F) + 1, w0 & 0x3FFFF, w1 & 0x1FFFFFFF


def sns_packets(sns):
    """Yield (offset of packet data inside sns, data length) for each block."""
    p = 0
    while p < len(sns):
        hd = be32(sns, p)
        flag, size = hd >> 24, hd & 0xFFFFFF
        data_len = be32(sns, p + 8) // 4 - 4
        for j in range(0, data_len, PACKET_BYTES):
            yield p + 12 + j, min(PACKET_BYTES, data_len - j)
        p += size
        if flag & 0x80:
            break


# ---------------------------------------------------------------- songs

def run_tool(args):
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")


def list_songs(music_dir):
    songs = []
    files = [f for f in sorted(music_dir.iterdir())
             if not f.name.startswith(".") and f.suffix.lower() in AUDIO_EXTENSIONS]
    for i, f in enumerate(files):
        status.set(f"Reading your songs ({i + 1} of {len(files)})" +
                   (" - downloading it from iCloud" if is_offloaded(f) else ""))
        probe = run_tool(["ffprobe", "-v", "error", "-show_entries", "format=duration:format_tags",
                          "-of", "json", str(f)])
        try:
            info = json.loads(probe.stdout)["format"]
            duration = float(info["duration"])
        except (ValueError, KeyError):
            say(f"  skipping {f.name}: ffprobe could not read it")
            continue
        t = {k.lower(): v for k, v in info.get("tags", {}).items()}
        artist, _, title = f.stem.partition(" - ")
        if not title:
            artist, title = "", f.stem
        songs.append({
            "file": f,
            "artist": (t.get("artist") or artist or "Unknown").strip(),
            "title": (t.get("title") or title).strip(),
            "album": (t.get("album") or "").strip(),
            "duration": duration,
        })
    songs.sort(key=lambda s: (s["artist"].lower(), s["title"].lower()))
    return songs


# "(Official Video)", "[Lyrics]", "(Remastered 2009)"... left over from video downloads.
TITLE_JUNK = re.compile(r"\s*[(\[](?:official[^)\]]*|lyrics?|lyric video|audio|hd|hq|"
                        r"remaster(?:ed)?(?: \d{4})?|\d{4} remaster(?:ed)?)[)\]]", re.IGNORECASE)


def clean_text(s, limit=40):
    s = TITLE_JUNK.sub("", s)
    s = s.replace("\u2019", "'").replace("\u201c", '"').replace("\u201d", '"')
    for junk in (" (Remastered 2003)", " (Remastered)", " - Remastered"):
        s = s.replace(junk, "")
    s = s.encode("latin-1", "replace").decode("latin-1")
    return s[:limit] or "Unknown"


def pcm_cache_path(cache_dir, song):
    st = song["file"].stat()
    key = f"{PCM_CACHE_VERSION}|{TARGET_LUFS}|{song['file'].name}|{st.st_size}"
    return cache_dir / (hashlib.sha1(key.encode("utf-8")).hexdigest()[:20] + ".pcm")


def decode_pcm(song, out_path):
    """Decode to 44.1 kHz stereo s16 big-endian, loudness-normalised (two-pass EBU R128)."""
    probe = run_tool(["ffmpeg", "-hide_banner", "-nostats", "-i", str(song["file"]), "-vn",
                      "-af", f"loudnorm=I={TARGET_LUFS}:TP={TRUE_PEAK}:LRA=11:print_format=json",
                      "-f", "null", "-"]).stderr
    try:
        m = json.loads(probe[probe.rindex("{"):probe.rindex("}") + 1])
    except ValueError:
        raise UserError(f"ffmpeg could not decode {song['file'].name}:\n{probe[-800:]}")
    af = (f"loudnorm=I={TARGET_LUFS}:TP={TRUE_PEAK}:LRA=11:measured_I={m['input_i']}"
          f":measured_TP={m['input_tp']}:measured_LRA={m['input_lra']}"
          f":measured_thresh={m['input_thresh']}:offset={m['target_offset']}:linear=true")
    tmp = out_path.with_suffix(".part")
    result = run_tool(["ffmpeg", "-v", "error", "-y", "-i", str(song["file"]), "-vn", "-af", af,
                       "-ac", "2", "-ar", str(SAMPLE_RATE), "-f", "s16be", str(tmp)])
    if result.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise UserError(f"ffmpeg could not decode {song['file'].name}:\n{result.stderr[-800:]}")
    os.replace(tmp, out_path)


def decode_all(songs, cache_dir, jobs):
    cache_dir.mkdir(parents=True, exist_ok=True)
    todo = [s for s in songs if not s["pcm"].exists()]
    if not todo:
        say(f"All {len(songs)} songs already converted.")
        return
    say(f"Converting {len(todo)} song(s) ({len(songs) - len(todo)} already done), "
        f"{min(jobs, len(todo))} at a time. This is the slow part...")
    status.set(f"Converting songs: 0 of {len(todo)} done (the first ones take about 30 seconds)")
    done = 0
    t0 = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = {pool.submit(decode_pcm, s, s["pcm"]): s for s in todo}
        for fut in concurrent.futures.as_completed(futures):
            s = futures[fut]
            fut.result()
            done += 1
            say(f"  [{done}/{len(todo)}] {s['artist']} - {s['title']}")
            left = (time.time() - t0) / done * (len(todo) - done)
            eta = f"about {max(1, round(left / 60))} min left" if left >= 60 else "less than a minute left"
            status.set(f"Converting songs: {done} of {len(todo)} done, {eta}")


# ---------------------------------------------------------------- fitting songs into slots

def ask(prompt):
    try:
        return input(prompt).strip()
    except EOFError:
        return ""


def fit_songs(songs, music_dir, fit):
    """Fit the songs into the playlist's slots. Returns slot groups of one or two songs.

    fit: "ask" (let the user choose, when there's a terminal), "pair" or "trim".
    """
    extra = len(songs) - NUM_SLOTS
    if extra <= 0:
        return [[s] for s in songs]
    can_pair = len(songs) <= 2 * NUM_SLOTS
    if fit == "ask":
        if sys.stdin.isatty():
            fit = ask_how_to_fit(songs, extra, can_pair)
        else:
            say(f"You have {len(songs)} songs but the playlist has {NUM_SLOTS} slots. Run this in a "
                "terminal to choose which to leave out, or pass --fit pair to put two songs in a slot.")
            fit = "trim"
    if fit == "pick":
        return [[s] for s in pick_left_out(songs, music_dir, extra)]
    if fit == "pair":
        if not can_pair:
            say(f"Even with two songs per slot only {2 * NUM_SLOTS} fit, so the last "
                f"{len(songs) - 2 * NUM_SLOTS} are left out.")
            songs = songs[:2 * NUM_SLOTS]
        return pair_up(songs, len(songs) - NUM_SLOTS)
    say(f"Skate 3's playlist has {NUM_SLOTS} slots, so {extra} song(s) were left out:")
    for s in songs[NUM_SLOTS:]:
        say(f"  - {s['artist']} - {s['title']}")
    return [[s] for s in songs[:NUM_SLOTS]]


def ask_how_to_fit(songs, extra, can_pair):
    status.set("")
    say(f"\nYou have {len(songs)} songs, but Skate 3's playlist only has {NUM_SLOTS} slots.")
    say(f"  1) Pick which {extra} song(s) to leave out")
    if can_pair:
        say(f"  2) Keep all {len(songs)}: {extra} slot(s) get two songs that play back to back")
    say(f"  3) Leave out the last {extra} (alphabetically)")
    choices = {"1": "pick", "2": "pair", "3": "trim"} if can_pair else {"1": "pick", "3": "trim"}
    while True:
        answer = ask(f"Choose {'1, 2 or 3' if can_pair else '1 or 3'} and press Enter [1]: ") or "1"
        if answer in choices:
            return choices[answer]


def pick_left_out(songs, music_dir, extra):
    say("")
    for i, s in enumerate(songs, 1):
        say(f"  {i:3}. {s['artist']} - {s['title']}")
    while True:
        answer = ask(f"Type the numbers of the songs to leave out (at least {extra}), separated "
                     "by spaces, then press Enter: ")
        try:
            picks = sorted({int(x) for x in answer.replace(",", " ").split()})
        except ValueError:
            say("  Please type just numbers, e.g.  4 17 32")
            continue
        if any(i < 1 or i > len(songs) for i in picks):
            say(f"  The numbers go from 1 to {len(songs)}.")
            continue
        if len(picks) < extra:
            say(f"  Pick at least {extra} (you picked {len(picks)}).")
            continue
        break
    out_dir = music_dir / LEFT_OUT_DIR
    out_dir.mkdir(exist_ok=True)
    for i in picks:
        f = songs[i - 1]["file"]
        dest = out_dir / f.name
        n = 2
        while dest.exists():
            dest = out_dir / f"{f.stem} ({n}){f.suffix}"
            n += 1
        shutil.move(str(f), str(dest))
        say(f"  left out: {songs[i - 1]['artist']} - {songs[i - 1]['title']}")
    say(f"Moved them to {out_dir}\n(move a song back into your music folder to put it back in the playlist).")
    return [s for i, s in enumerate(songs, 1) if i not in set(picks)]


def pair_up(songs, pairs_needed):
    """Group songs into slots with `pairs_needed` slots holding two songs, pairing songs by the
    same artist where possible."""
    by_artist = {}
    for s in songs:
        by_artist.setdefault(s["artist"].lower(), []).append(s)
    pairs = []
    taken = set()
    for group in sorted(by_artist.values(), key=len, reverse=True):
        for i in range(0, len(group) - 1, 2):
            if len(pairs) == pairs_needed:
                break
            pairs.append([group[i], group[i + 1]])
            taken.update((id(group[i]), id(group[i + 1])))
    rest = [s for s in songs if id(s) not in taken]
    while len(pairs) < pairs_needed:
        b, a = rest.pop(), rest.pop()
        pairs.append([a, b])
    groups = [[s] for s in rest] + pairs
    groups.sort(key=lambda g: (g[0]["artist"].lower(), g[0]["title"].lower()))
    for a, b in pairs:
        say(f"  sharing a slot: {a['artist']} - {a['title']}  +  {b['artist']} - {b['title']}")
    return groups


def group_pcm(group, cache_dir):
    """A slot's audio: the song's converted file, or both songs joined by a short gap."""
    if len(group) == 1:
        return group[0]["pcm"]
    a, b = group
    out = cache_dir / f"pair_{a['pcm'].stem}_{b['pcm'].stem}.pcm"
    if not out.exists():
        tmp = out.with_suffix(".part")
        with open(tmp, "wb") as f:
            with open(a["pcm"], "rb") as src:
                shutil.copyfileobj(src, f, 1 << 20)
            f.write(bytes(SAMPLE_RATE * 4 * PAIR_GAP_SECONDS))
            with open(b["pcm"], "rb") as src:
                shutil.copyfileobj(src, f, 1 << 20)
        os.replace(tmp, out)
    return out


def slot_strings(group):
    """(title, artist, album) shown in the game for a slot."""
    if len(group) == 1:
        s = group[0]
        return clean_text(s["title"]), clean_text(s["artist"]), clean_text(s["album"] or s["title"])
    a, b = group
    title = f"{a['title']} / {b['title']}"
    if a["artist"].lower() == b["artist"].lower():
        artist = a["artist"]
    else:
        artist = f"{a['artist']} / {b['artist']}"
    album = a["album"] if a["album"] and a["album"] == b["album"] else title
    return clean_text(title), clean_text(artist), clean_text(album)


# ---------------------------------------------------------------- MPF rebuild

def rebuild_mpf(orig, chains, stream_ms, sns_offsets, snr_region_end):
    """chains[k] = list of new stream indices for slot k (k < NUM_SLOTS)."""
    d = bytearray(orig)
    nodes_end = be32(d, 0x1C)

    nodes = []
    for variant, section in ((0, 0x20), (1, 0x40)):
        for k in range(NUM_SLOTS):
            part = (variant * NUM_SLOTS + k) * 2
            s_idx = len(nodes)
            nodes.append([section, 0x8101, part << 16, 0, None])
            nodes.append([0xFFFF0000 | section, 0x0101, (part + 1) << 16, 0])
            chain = chains[k]
            for stream in chain:
                nodes.append([((stream + 1) << 16) | section, 0x8401, (s_idx << 16) | 0x40, 0, None])
            first_seg = s_idx + 2
            nodes[s_idx][4] = 0x007F0000 | first_seg
            for j in range(len(chain)):
                nxt = first_seg + j + 1 if j + 1 < len(chain) else s_idx + 1
                nodes[first_seg + j][4] = 0x007F0000 | nxt

    tail = d[nodes_end:]
    # node offsets are stored /4 relative to the file; data region starts right after table
    count = len(nodes)
    new_tab = 0x48
    new_data = new_tab + ((count * 2 + 3) // 4) * 4
    body = bytearray()
    offs = []
    for n in nodes:
        offs.append((new_data + len(body)) // 4)
        for w in n:
            body += struct.pack(">I", w)
    if offs[-1] > 0xFFFF:
        raise UserError("the playlist is too long for the game's music index (too many songs "
                        "or songs that are too long)")
    new_nodes_end = new_data + len(body)
    shift = new_nodes_end - nodes_end

    out = bytearray(d[:0x48])
    for o in offs:
        out += struct.pack(">H", o)
    while len(out) < new_data:
        out += b"\0"
    out += body
    # tail holds section records, names, track tables, samples table; offsets inside it
    # are absolute (either bytes or /4 words) so they must move by `shift` (a multiple of 4).
    assert shift % 4 == 0
    tail = bytearray(tail)
    # word-pointers at the start of the tail (3 x u16 section record pointers)
    for i in range(3):
        v = struct.unpack_from(">H", tail, 2 * i)[0]
        struct.pack_into(">H", tail, 2 * i, v + shift // 4)
    out += tail
    struct.pack_into(">H", out, 0x12, count)
    struct.pack_into(">I", out, 0x18, new_data)
    for field in range(0x1C, 0x3C, 4):
        struct.pack_into(">I", out, field, be32(d, field) + shift)
    # track tables hold /4 pointers to track entries
    for field in (0x28, 0x2C):
        base = be32(out, field)
        end = be32(out, field + 4)
        for o in range(base, end, 4):
            v = be32(out, o)
            if v:
                struct.pack_into(">I", out, o, v + shift // 4)
    track0 = be32(out, be32(out, 0x2C)) * 4
    struct.pack_into(">I", out, track0 + 0x0C, snr_region_end)
    # samples table
    samples = be32(out, 0x34)
    del out[samples:]
    for i, ms in enumerate(stream_ms):
        out += struct.pack(">II", sns_offsets[i] // 0x80, ms)
    struct.pack_into(">I", out, 0x38, len(out))
    return bytes(out)


# ---------------------------------------------------------------- strings

def patch_strings(big_path, slot_text):
    """slot_text[k] = (title, artist, album); returns the patched archive."""
    data = bytearray(big_path.read_bytes())
    count = be32(data, 4)
    shift = (be32(data, 8) >> 8) & 0xFF
    recs = {}
    for i in range(count):
        off, csize, size, rid = struct.unpack_from(">IIII", data, 0x30 + 16 * i)
        recs[rid] = (i, off << shift, csize, size)

    def load(rid):
        _, off, csize, size = recs[rid]
        raw = bytes(data[off:off + (csize or size)])
        return ebbig.decompress(raw, size) if csize else raw

    # key names -> hash, from the debug key table
    keys = load(KEY_TABLE)
    kcount = struct.unpack_from("<I", keys, 8)[0]
    kbase = 0x24 + 8 * kcount
    hash_of = {}
    for k in range(kcount):
        h, o = struct.unpack_from("<II", keys, 0x24 + 8 * k)
        e = keys.index(b"\0", kbase + o)
        hash_of[keys[kbase + o:e].decode("latin-1")] = h

    exe_names = json.loads((HERE / "ipod_slots.json").read_text())
    replacements = {}
    for k, name in enumerate(exe_names):
        key = "IPOD_" + name.upper()
        title, artist, album = slot_text[k]
        for suffix, text in (("_TITLE", title), ("_ARTIST", artist), ("_ALBUM", album)):
            h = hash_of.get(key + suffix)
            if h is None:
                raise UserError(f"miscboot.big has no string key {key + suffix} - is this the "
                                "Xbox 360 version of Skate 3?")
            replacements[h] = text.encode("latin-1")

    for rid in LANG_TABLES:
        t = bytearray(load(rid))
        cnt = struct.unpack_from("<I", t, 8)[0]
        base = 0x24 + 8 * cnt
        for k in range(cnt):
            h, o = struct.unpack_from("<II", t, 0x24 + 8 * k)
            if h in replacements:
                struct.pack_into("<I", t, 0x24 + 8 * k + 4, len(t) - base)
                t += replacements[h] + b"\0"
        struct.pack_into("<I", t, 4, len(t) - 8)
        comp = ebbig.refpack_compress(t)
        # append record RefPack-compressed, aligned like the rest of the archive
        while len(data) % (1 << shift):
            data.append(0)
        idx = recs[rid][0]
        struct.pack_into(">IIII", data, 0x30 + 16 * idx, len(data) >> shift, len(comp), len(t), rid)
        data += comp
    while len(data) % (1 << shift):
        data.append(0)
    struct.pack_into(">I", data, 0x1C, len(data))
    return bytes(data)


# ---------------------------------------------------------------- folders

class Layout:
    """Where everything lives. `root` is the folder holding the game folder."""

    def __init__(self, game, music=None, work=None):
        self.game = game
        self.root = game.parent
        self.music = music or self.root / "My Music"
        self.work = work or default_work_dir()
        self.backup = self.root / "backup" / "game"
        self.stage = self.work  # mirrors the game folder
        self.cache = self.work / "pcm_cache"

    def original(self, rel):
        """The game's original copy of a file the mod replaces."""
        b = self.backup / rel
        return b if b.exists() else self.game / rel


def default_work_dir():
    """Converted songs and build output go to the local cache folder, never a synced one."""
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / "Skate3MusicMod"


def is_offloaded(path):
    """True if the file's contents are only in the cloud (reading it waits for a download)."""
    try:
        st = os.stat(path)
    except OSError:
        return False
    if getattr(st, "st_flags", 0) & SF_DATALESS:  # iCloud Drive
        return True
    # OneDrive Files On-Demand: OFFLINE | RECALL_ON_OPEN | RECALL_ON_DATA_ACCESS
    return bool(getattr(st, "st_file_attributes", 0) & (0x1000 | 0x40000 | 0x400000))


def cloud_folder(path):
    """The cloud service syncing `path` ("iCloud Drive", "OneDrive", ...), or None."""
    p = str(path.resolve())
    home = Path.home()
    if sys.platform == "darwin":
        if p.startswith(str(home / "Library" / "Mobile Documents")):
            return "iCloud Drive"
        if p.startswith(str(home / "Library" / "CloudStorage")):
            return "a cloud-synced folder"
        icloud = home / "Library" / "Mobile Documents" / "com~apple~CloudDocs"
        for name in ("Documents", "Desktop"):
            if (icloud / name).exists() and (p + "/").startswith(str(home / name) + "/"):
                return "iCloud Drive"
    elif sys.platform == "win32":
        for var in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
            root = os.environ.get(var)
            if root and p.lower().startswith(root.lower().rstrip("\\") + "\\"):
                return "OneDrive"
    return None


def warn_cloud(lay):
    service = cloud_folder(lay.game)
    if not service:
        return
    if sys.platform == "win32":
        fix = "or right-click it and choose 'Always keep on this device'."
    else:
        fix = "or right-click it in Finder and choose 'Keep Downloaded'."
    say(f"\nHeads up: your Skate 3 folder is inside {service}. When your disk gets full, files there\n"
        "are moved to the cloud, and this tool - and the game - then have to wait for them to\n"
        "download again, which can take a long time and looks like a freeze. To avoid it, move the\n"
        f"whole '{lay.root.name}' folder somewhere that doesn't sync (e.g. a 'Games' folder in your\n"
        f"home folder), {fix}\n")


def is_game_folder(p):
    return (p / "data/audio/music/ipod.mpf").is_file() and (p / "default.xex").is_file()


def find_game(arg):
    if arg:
        p = Path(arg).expanduser().resolve()
        if is_game_folder(p):
            return p
        if is_game_folder(p / "game"):
            return p / "game"
        raise UserError(f"{p} is not a Skate 3 game folder (it should contain default.xex and "
                        "data/audio/music/ipod.mpf).")
    env = os.environ.get("SKATE3_GAME")
    candidates = [Path(env)] if env else []
    candidates += [HERE.parent / "game", HERE.parent.parent / "game", Path.cwd() / "game", Path.cwd()]
    for c in candidates:
        if is_game_folder(c):
            return c.resolve()
    raise UserError(
        "Could not find the Skate 3 game folder. It is the 'game' folder next to the game "
        "(skate3.exe / skate3recomp.app), created when you install the game from your ISO.\n"
        "Pass it with --game, e.g.  python skate3_music.py --game \"C:\\Games\\Skate3\\game\"")


def check_ffmpeg():
    missing = [t for t in ("ffmpeg", "ffprobe") if shutil.which(t) is None]
    if not missing:
        return
    if sys.platform == "win32":
        hint = ("Install it with:  winget install Gyan.FFmpeg\n"
                "then close and reopen this window (or double-click the launcher again).")
    elif sys.platform == "darwin":
        hint = "Install it with:  brew install ffmpeg"
    else:
        hint = "Install it with your package manager, e.g.  sudo apt install ffmpeg"
    raise UserError(f"{' and '.join(missing)} not found. FFmpeg is needed to convert your songs.\n{hint}")


def game_running():
    try:
        if sys.platform == "win32":
            out = run_tool(["tasklist", "/FI", "IMAGENAME eq skate3.exe", "/NH"]).stdout
            return "skate3.exe" in out.lower()
        return run_tool(["pgrep", "-x", "skate3"]).returncode == 0
    except OSError:
        return False


def check_not_running():
    if game_running():
        raise UserError("Skate 3 is running - close it first, then try again.")


def check_backups(lay):
    """Refuse to work from files the mod already replaced if their originals are gone."""
    if (lay.game / "custom_music").exists():
        missing = [rel for rel in MODDED_FILES if not (lay.backup / rel).exists()]
        if missing:
            raise UserError(
                "The music mod is installed but the original game files are missing from "
                f"{lay.backup}:\n  " + "\n  ".join(missing) +
                "\nReinstall the game files from your ISO, then run this again.")


def link_or_copy(src, dst, allow_link=True):
    if dst.exists() or dst.is_symlink():
        dst.unlink()
    if allow_link:
        try:
            os.link(src, dst)
            return
        except OSError:
            pass
    shutil.copyfile(src, dst)


# ---------------------------------------------------------------- build

def build(lay, jobs, fit="ask"):
    t0 = time.time()
    check_ffmpeg()
    check_backups(lay)
    lay.music.mkdir(parents=True, exist_ok=True)
    say(f"Game folder:  {lay.game}")
    say(f"Your songs:   {lay.music}")
    warn_cloud(lay)
    if (lay.root / "mod_build").is_dir():
        say(f"(The old {lay.root / 'mod_build'} folder isn't used anymore - you can delete it.)")
    songs = list_songs(lay.music)
    if not songs:
        raise UserError(f"No songs found. Put your songs (mp3, m4a, flac, wav, ogg, opus, ...) "
                        f"in\n  {lay.music}\nand run this again.")
    groups = fit_songs(songs, lay.music, fit)
    songs = [s for g in groups for s in g]
    paired = sum(len(g) == 2 for g in groups)
    say(f"{len(songs)} songs" +
        (f" in {len(groups)} playlist slots ({paired} with two songs)" if paired else "") +
        (f" (they repeat to fill all {NUM_SLOTS} playlist slots)" if len(groups) < NUM_SLOTS else ""))
    for s in songs:
        s["pcm"] = pcm_cache_path(lay.cache, s)
    decode_all(songs, lay.cache, jobs)
    n = len(groups)
    slots = [groups[k % n] for k in range(NUM_SLOTS)]
    slot_pcm = [group_pcm(g, lay.cache) for g in slots]
    slot_samples = [p.stat().st_size // 4 for p in slot_pcm]

    say("Building the playlist...")
    status.set("Building the playlist: reading the game's music files")
    rng = random.Random(1234)
    music_dir = lay.game / "data/audio/music"
    # pool of donor chunks (stereo 44.1k EA-XMA), largest first
    sources = []
    for path in (lay.original("data/audio/music/Ipod_Stream.mus"),
                 music_dir / "World_Stream.mus", music_dir / "Game_Stream.mus"):
        if is_offloaded(path):
            status.set(f"Building the playlist: downloading {path.name} from iCloud")
        sources.append(Mus(path))
    pool = []
    for si, m in enumerate(sources):
        for i in range(m.count):
            m.f.seek(m.entries[i][1])
            codec, ch, sr, ns = snr_info(m.f.read(8))
            if codec == 3 and ch == 2 and sr == SAMPLE_RATE:
                pool.append((ns, si, i))
    pool.sort(reverse=True)

    # keys of every original packet, so tagged keys never collide with real music
    taken = set()
    for m in sources:
        for i in range(m.count):
            _, _, sns = m.chunk(i)
            for off, _ in sns_packets(sns):
                taken.add(packet_key(sns[off:off + KEY_BYTES]))

    chains = []
    new_chunks = []  # (ehash, snr, sns, ms)
    table = []
    used = set()
    pool_head = 0
    for k in range(NUM_SLOTS):
        if k >= n:
            chains.append(chains[k % n])
            continue
        status.set(f"Building the playlist: song {k + 1} of {n}")
        need = slot_samples[k]
        chain = []
        chunk_base = 0
        split_pending = False
        while need > 0:
            while pool_head < len(pool) and (pool[pool_head][1], pool[pool_head][2]) in used:
                pool_head += 1
            if pool_head >= len(pool):
                raise UserError("Your songs are too long in total: the game's music files don't "
                                "have enough audio chunks to hold them. Remove a few songs.")
            best = None
            for i in range(pool_head, len(pool)):
                p = pool[i]
                if p[0] < need:
                    break
                if (p[1], p[2]) not in used:
                    best = p
            pick = best if best is not None else pool[pool_head]
            used.add((pick[1], pick[2]))
            ehash, snr, sns = sources[pick[1]].chunk(pick[2])
            sns = bytearray(sns)
            carry = 0
            frame_cursor = 0
            for off, dlen in list(sns_packets(sns)):
                pkt = sns[off:off + PACKET_BYTES]
                if len(pkt) < PACKET_BYTES:
                    pkt += b"\xff" * (PACKET_BYTES - len(pkt))
                pkt = bytearray(pkt)
                struct_set, next_carry = structural_bits(pkt, carry)
                free = [b for b in range(32, min(KEY_BYTES, dlen) * 8) if b not in struct_set]
                sample_k = min(24, len(free))
                for _attempt in range(16):
                    trial = bytearray(pkt)
                    for b in rng.sample(free, sample_k):
                        trial[b >> 3] ^= 0x80 >> (b & 7)
                    key = packet_key(trial)
                    if key not in taken:
                        break
                else:
                    raise UserError("could not tag a packet uniquely (please report this)")
                taken.add(key)
                nb = min(KEY_BYTES, dlen)
                sns[off:off + nb] = trial[:nb]
                carry = next_carry

                fc, split = get_packet_frames_old(trial)
                if split_pending:
                    frame_0 = frame_cursor + 1
                    frame_cursor += 1
                else:
                    frame_0 = frame_cursor
                table.append((key, k, chunk_base + frame_0 * FRAME_SAMPLES - START_SKIP))
                if split:
                    frame_cursor += fc - 1
                    split_pending = True
                else:
                    frame_cursor += fc
                    split_pending = False

            chain.append(len(new_chunks))
            chunk_samples = frame_cursor * FRAME_SAMPLES
            new_chunks.append((ehash, snr, bytes(sns), max(1, round(chunk_samples * 1000 / SAMPLE_RATE))))
            need -= chunk_samples
            chunk_base += chunk_samples
        chains.append(chain)

    # write the new MUS
    status.set("Building the playlist: writing the game files")
    count = len(new_chunks)
    snr_start = (0x28 + count * 0x1C + 0x7FF) & ~0x7FF
    snr_end = snr_start + count * 0x10
    sns_pos = (snr_end + 0x7F) & ~0x7F
    sns_offsets = []
    mus = bytearray(sources[0].checksum + struct.pack("<I", count) + b"\xcc" * 0x20)
    for i, (ehash, snr, sns, ms) in enumerate(new_chunks):
        sns_offsets.append(sns_pos)
        mus += ehash + struct.pack(">HH", i & 0xFFFF, 0)
        mus += struct.pack(">IIIII", (snr_start + i * 0x10) // 0x10, sns_pos // 0x80, len(snr), len(sns), 0)
        sns_pos = (sns_pos + len(sns) + 0x7F) & ~0x7F
    mus += b"\0" * (snr_start - len(mus))
    for _, snr, _, _ in new_chunks:
        mus += snr + b"\0" * (0x10 - len(snr))
    for m in sources:
        m.close()

    stage_music = lay.stage / "data/audio/music"
    stage_music.mkdir(parents=True, exist_ok=True)
    (lay.stage / "data/big").mkdir(parents=True, exist_ok=True)
    stage_cm = lay.stage / "custom_music"
    if stage_cm.exists():
        shutil.rmtree(stage_cm)  # older builds kept full song copies here
    stage_cm.mkdir(parents=True)
    with open(stage_music / "Ipod_Stream.mus", "wb") as f:
        f.write(mus)
        for i, (_, _, sns, _) in enumerate(new_chunks):
            f.seek(sns_offsets[i])
            f.write(sns)
        f.truncate((sns_offsets[-1] + len(new_chunks[-1][2]) + 0x7F) & ~0x7F)

    new_mpf = rebuild_mpf(lay.original("data/audio/music/ipod.mpf").read_bytes(), chains,
                          [c[3] for c in new_chunks], sns_offsets, snr_end)
    (stage_music / "ipod.mpf").write_bytes(new_mpf)

    # lookup table for the runtime
    table.sort()
    with open(stage_cm / "table.bin", "wb") as f:
        f.write(b"S3CM" + struct.pack("<II", 1, len(table)))
        for k in range(NUM_SLOTS):
            f.write(struct.pack("<I", slot_samples[k]))
        for key, slot, pos in table:
            f.write(struct.pack("<QIi", key, slot, pos))

    slot_text = [slot_strings(g) for g in slots]
    (lay.stage / "data/big/miscboot.big").write_bytes(
        patch_strings(lay.original("data/big/miscboot.big"), slot_text))

    with open(stage_cm / "slots.txt", "w", encoding="utf-8", newline="\n") as f:
        for k, g in enumerate(slots):
            for s in g:  # two lines with the same slot number for a shared slot
                f.write(f"{k:2}  {s['artist']} - {s['title']}\n")
    (stage_cm / "sources.txt").write_text(
        "\n".join(f"{k:2}  {pcm.name}" for k, pcm in enumerate(slot_pcm)) + "\n", encoding="utf-8")
    say(f"Built in {time.time() - t0:.0f}s ({count} audio chunks, {len(table)} tagged packets).")
    return slots


# ---------------------------------------------------------------- install

def install(lay):
    check_not_running()
    check_backups(lay)
    stage_cm = lay.stage / "custom_music"
    if not (stage_cm / "table.bin").is_file():
        raise UserError("Nothing built yet - run  python skate3_music.py build  first.")
    status.set("Installing into the game folder")
    for rel in MODDED_FILES:
        backup = lay.backup / rel
        if not backup.exists():
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(lay.game / rel, backup)
            say(f"Backed up the original {rel}")
    for rel in MODDED_FILES:
        shutil.copyfile(lay.stage / rel, lay.game / rel)
    dest = lay.game / "custom_music"
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir()
    for name in ("table.bin", "slots.txt"):
        shutil.copyfile(stage_cm / name, dest / name)
    # Hard links save space, but a cloud-synced folder gets real copies.
    allow_link = cloud_folder(lay.game) is None
    for line in (stage_cm / "sources.txt").read_text(encoding="utf-8").splitlines():
        k, pcm = line.split()
        link_or_copy(lay.cache / pcm, dest / f"slot{int(k):02d}.pcm", allow_link)
    say(f"Installed into {lay.game}")


def uninstall(lay):
    check_not_running()
    status.set("Restoring the original soundtrack")
    restored = 0
    for rel in MODDED_FILES:
        backup = lay.backup / rel
        if backup.exists():
            shutil.copy2(backup, lay.game / rel)
            restored += 1
    cm = lay.game / "custom_music"
    if cm.exists():
        shutil.rmtree(cm)
    if restored:
        say("Custom music removed - the original soundtrack is back.")
    else:
        say("The music mod wasn't installed (no backups found), nothing to do.")


def show_playlist(lay):
    slots = lay.game / "custom_music" / "slots.txt"
    if not slots.exists():
        say("The original Skate 3 soundtrack is installed (no custom music).")
        return
    by_slot = {}
    for line in slots.read_text(encoding="utf-8").splitlines():
        num, name = line.strip().split(None, 1)
        by_slot.setdefault(int(num), []).append(name)
    entries = []
    for names in by_slot.values():
        if names not in entries:  # short playlists repeat songs to fill the slots
            entries.append(names)
    count = sum(len(e) for e in entries)
    shared = sum(len(e) > 1 for e in entries)
    say(f"Your Skate 3 playlist ({count} songs" +
        (f", {shared} slots with two songs" if shared else "") + "):")
    for i, names in enumerate(entries, 1):
        say(f"  {i:2}. " + "  +  ".join(names))


# ---------------------------------------------------------------- main

def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    ap = argparse.ArgumentParser(
        description="Put your own songs in Skate 3's playlist.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="commands:\n"
               "  install    build the mod from your songs and install it (default)\n"
               "  build      only build, don't install\n"
               "  uninstall  restore the game's original soundtrack\n"
               "  list       show the installed playlist")
    ap.add_argument("command", nargs="?", default="install",
                    choices=("install", "build", "uninstall", "list"), metavar="command")
    ap.add_argument("--game", help="the Skate 3 game folder (found automatically if not given)")
    ap.add_argument("--music", help='folder with your songs (default: "My Music" next to the game folder)')
    ap.add_argument("--work", help="folder for converted songs and build output "
                                   f"(default: {default_work_dir()})")
    ap.add_argument("--fit", choices=("ask", "pair", "trim"), default="ask",
                    help=f"with more than {NUM_SLOTS} songs: ask (default), pair (two songs in "
                         "some slots) or trim (leave out the last ones)")
    ap.add_argument("--jobs", type=int, default=max(1, min(8, os.cpu_count() or 1)),
                    help="songs to convert at the same time")
    args = ap.parse_args()
    disable_quick_edit()
    if sys.version_info < (3, 8):
        raise UserError("Python 3.8 or newer is needed.")
    lay = Layout(find_game(args.game),
                 Path(args.music).expanduser().resolve() if args.music else None,
                 Path(args.work).expanduser().resolve() if args.work else None)
    if args.command == "list":
        show_playlist(lay)
        return
    with status:
        if args.command == "uninstall":
            uninstall(lay)
            return
        if args.command == "install":
            check_not_running()
        say("Working... (a spinning line at the bottom means it's still going)")
        build(lay, args.jobs, args.fit)
        if args.command == "install":
            install(lay)
    if args.command == "install":
        print()
        show_playlist(lay)
        print("\nDone! Start the game - your songs are now its playlist. (Make sure you run the\n"
              "build from this mod, the official one plays the wrong audio.)")


if __name__ == "__main__":
    try:
        main()
    except UserError as e:
        print(f"\nError: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
