#!/usr/bin/env python3
"""
music_pill.py — Waybar custom module (continuous/exec mode)

Emits one JSON line per tick to stdout, in the format Waybar expects for a
custom module:

    {"text": "...", "tooltip": "...", "class": "playing", "alt": "spotify"}

Behavior:
  1. If the active MPRIS player exposes lyrics in its metadata (checked
     against known lyric keys, exact match first then fuzzy fallback),
     scroll the lyrics line.
  2. Otherwise, if online lookup is enabled, fetch synced/plain lyrics from
     lrclib.net and scroll those.
  3. Otherwise scroll "Title | Album | Artist".
  4. Emits "class": "none" and clears text when nothing is playing, and a
     distinct "class": "error" when playerctl itself is unavailable or
     failing, so those two situations are never confused in your CSS.

Run this as a Waybar "custom" module with "exec": "python3 music_pill.py"
and no "interval" — this script loops forever and prints continuously.

Requires: playerctl (CLI), python3. No extra pip packages.

Environment variables:
  WAYBAR_MUSIC_DEBUG            1 to log diagnostics to stderr (default 0)
  WAYBAR_MUSIC_LYRICS_ONLINE    0 to disable lrclib.net lookups (default 1)
  WAYBAR_MUSIC_PLAYER_PRIORITY  comma-separated substrings, e.g.
                                 "spotify,firefox,vlc" — matched
                                 case-insensitively against player names to
                                 pick a preferred player when several are
                                 active at the same playback state.

Privacy note: when online lyrics are enabled, the current track's artist,
title, album, and duration are sent to lrclib.net (a free, third-party,
no-API-key lyrics service) to look up lyrics. No other data leaves your
machine. Set WAYBAR_MUSIC_LYRICS_ONLINE=0 to keep everything fully local
(native MPRIS lyrics metadata only, when a player provides it).
"""

# Defers evaluation of type hints (e.g. `list[str]`, `dict[str, str]`) so
# this file still runs on Python 3.8, not just 3.9+. Must be the first
# statement after the module docstring.
from __future__ import annotations

import collections
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

# ---- Config ---------------------------------------------------------------

SCROLL_TICK = 0.4          # seconds between scroll frames
SCROLL_WINDOW = 40         # visible characters in the scrolling text
SCROLL_GAP = "   •   "     # separator when the text wraps around
IDLE_TEXT = ""             # shown when nothing is playing
POLL_INTERVAL = 1.0        # seconds between metadata/status polls (real time,
                            # not tick-counted, so it doesn't drift)
COMMAND_TIMEOUT = 5        # seconds before a playerctl call is given up on

# Non-standard metadata keys some players/clients use to expose lyrics.
# Checked exactly (case-insensitive) first; substring hints are the fallback,
# used only if no exact key matched, to reduce false-positive matches.
LYRIC_EXACT_KEYS = {
    "lyrics", "xesam:lyrics", "syncedlyrics", "unsyncedlyrics",
    "xesam:astext", "mpris:lyrics",
}
LYRIC_KEY_HINTS = (
    "lyric", "astext", "lrc", "syncedlyric", "currentlyric",
)

# Set WAYBAR_MUSIC_DEBUG=1 in the environment to dump diagnostics (including
# every metadata key your active player exposes, on track change, and any
# playerctl/network failures) to stderr.
DEBUG = os.environ.get("WAYBAR_MUSIC_DEBUG") == "1"

# Online lyrics lookup/display is enabled by default. Set
# WAYBAR_MUSIC_LYRICS_ONLINE=0 to disable lrclib.net lookups and only ever use
# MPRIS-native lyrics (rare), with a Title | Album | Artist fallback.
LYRICS_ONLINE_ENABLED = os.environ.get("WAYBAR_MUSIC_LYRICS_ONLINE", "1") != "0"
LRCLIB_URL = "https://lrclib.net/api/get"
LRCLIB_TIMEOUT = 8
# LRCLIB asks integrations to identify themselves with a meaningful
# application/project reference and a way to reach the maintainer, so it can
# throttle or contact the project instead of just blackholing the requests.
LRCLIB_USER_AGENT = (
    "waybar-music-pill/2.0 "
    "(+https://github.com/itsfoss/desktop-customization)"
)

# Lyric fetch failures are retried after a cooldown instead of being cached
# as permanent misses, so a transient network blip doesn't kill lyrics for a
# track for the rest of the process's life.
LYRICS_RETRY_COOLDOWN = 600  # seconds (10 min)
LYRICS_CACHE_MAX_ENTRIES = 200
# LRCLIB recommends sequential requests.
LYRICS_FETCH_MAX_WORKERS = 1
LRCLIB_MIN_REQUEST_INTERVAL = 0.5  # seconds between outbound requests

# Preferred player order: comma-separated substrings, matched
# case-insensitively against player names (e.g. "spotify,firefox").
PLAYER_PRIORITY = [
    p.strip().lower()
    for p in os.environ.get("WAYBAR_MUSIC_PLAYER_PRIORITY", "").split(",")
    if p.strip()
]

_PLAYERCTL_PATH = shutil.which("playerctl")

# ---- Helpers ----------------------------------------------------------------


def debug_log(message: str) -> None:
    if DEBUG:
        print(f"[music_pill debug] {message}", file=sys.stderr, flush=True)


def run(
    cmd: list,
    timeout: float = COMMAND_TIMEOUT,
    treat_nonzero_as_empty: bool = False,
) -> Optional[str]:
    """Run a command and return its stripped stdout, or None on failure.

    Distinguishes (via debug_log) a missing binary, a timeout, a non-zero
    exit, and other subprocess errors, so failures are diagnosable instead
    of silently looking like "nothing playing".

    treat_nonzero_as_empty: set this for a specific call site where a
    non-zero exit is a known, benign "there's no data" signal for *that*
    command (e.g. `playerctl -l` when no players are open) rather than
    trying to detect this generically by matching stderr text — playerctl's
    exact wording varies across versions and locales, so string-matching
    stderr is fragile and was dropped in favor of this explicit per-call
    opt-in.
    """
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
    except FileNotFoundError:
        debug_log(f"command not found: {cmd[0]}")
        return None
    except subprocess.TimeoutExpired:
        debug_log(f"command timed out after {timeout}s: {' '.join(cmd)}")
        return None
    except subprocess.SubprocessError as exc:
        debug_log(f"subprocess error running {' '.join(cmd)}: {exc}")
        return None

    if result.returncode != 0:
        if treat_nonzero_as_empty:
            debug_log(
                f"command exited non-zero (rc={result.returncode}), treated "
                f"as 'no data' for this call site: {' '.join(cmd)} "
                f"stderr={result.stderr.strip()!r}"
            )
            return ""

        debug_log(
            f"command failed (rc={result.returncode}): {' '.join(cmd)} "
            f"stderr={result.stderr.strip()!r}"
        )
        return None

    return result.stdout.strip()


def get_player_list() -> Optional[list[str]]:
    """Return all currently available MPRIS player names.

    `playerctl -l` exits non-zero when no players are open — that's the
    normal idle state, not a module error, so this call site opts in to
    treat_nonzero_as_empty rather than relying on stderr wording (which
    varies by playerctl version and system locale).
    """
    raw = run(["playerctl", "-l"], treat_nonzero_as_empty=True)
    if raw is None:
        return None
    return [line.strip() for line in raw.splitlines() if line.strip()]


def get_all_statuses() -> Optional[dict[str, str]]:
    """Return {player_name: status} for all available players."""
    players = get_player_list()
    if players is None:
        return None

    statuses: dict[str, str] = {}
    for player in players:
        raw = run(["playerctl", "-p", player, "status"])
        if raw is None:
            # A player can disappear between `-l` and the status query.
            # Treat that as a transient race rather than failing the module.
            debug_log(f"player disappeared while reading status: {player}")
            continue
        status = raw.strip().lower()
        if status:
            statuses[player] = status

    return statuses


def get_all_metadata_multi() -> Optional[dict[str, dict[str, str]]]:
    """Return metadata for all players.

    Track fields are requested explicitly with playerctl's format mechanism
    instead of depending on the human-readable layout of `playerctl metadata`.
    The raw metadata query is still used as a secondary source so non-standard
    native lyric fields can be preserved.
    """
    players = get_player_list()
    if players is None:
        return None

    data: dict[str, dict[str, str]] = {}

    # Use an unlikely ASCII separator so titles/artists containing spaces are
    # preserved exactly and do not depend on column formatting.
    separator = "\x1f"
    track_format = (
        "{{xesam:title}}" + separator
        + "{{xesam:album}}" + separator
        + "{{xesam:artist}}" + separator
        + "{{mpris:length}}"
    )

    for player in players:
        meta: dict[str, str] = {}

        formatted = run([
            "playerctl", "-p", player, "metadata",
            "--format", track_format,
        ])

        if formatted is not None:
            fields = formatted.split(separator)
            while len(fields) < 4:
                fields.append("")

            title, album, artist, length = (
                field.strip() for field in fields[:4]
            )
            if title:
                meta["xesam:title"] = title
            if album:
                meta["xesam:album"] = album
            if artist:
                meta["xesam:artist"] = artist
            if length:
                meta["mpris:length"] = length

        # Preserve any additional/non-standard metadata, especially native
        # lyrics exposed by some MPRIS clients. This parsing is now secondary,
        # so variations in its formatting cannot break basic track detection.
        raw = run(["playerctl", "-p", player, "metadata"])
        if raw is not None:
            for line in raw.splitlines():
                parts = line.split(None, 1)
                if len(parts) != 2:
                    continue
                key, value = parts
                meta.setdefault(key.strip(), value.strip())

        data[player] = meta

    return data


def _player_priority_rank(player: str) -> int:
    lowered = player.lower()
    for i, pref in enumerate(PLAYER_PRIORITY):
        if pref in lowered:
            return i
    return len(PLAYER_PRIORITY)


def pick_active_player(statuses: dict, metadata_map: dict) -> Optional[str]:
    """Prefer a playing player, else paused, else whatever has metadata.

    Within each status tier, honor WAYBAR_MUSIC_PLAYER_PRIORITY if set, then
    fall back to sorted player name for a deterministic (not
    dict/CLI-order-dependent) result.
    """
    candidates = [p for p in metadata_map if p in statuses]
    if not candidates:
        return None
    candidates.sort(key=lambda p: (_player_priority_rank(p), p))

    playing = [p for p in candidates if statuses.get(p) == "playing"]
    if playing:
        return playing[0]
    paused = [p for p in candidates if statuses.get(p) == "paused"]
    if paused:
        return paused[0]
    return candidates[0]


def find_lyrics(meta: dict) -> Optional[str]:
    """Two-stage lookup: exact known keys first, then fuzzy substring hints,
    to reduce the chance of matching an unrelated metadata field."""
    lowered = {k.lower(): k for k in meta}

    for exact in LYRIC_EXACT_KEYS:
        real_key = lowered.get(exact)
        if real_key is not None:
            value = meta[real_key].strip()
            if value:
                return " / ".join(v.strip() for v in value.splitlines() if v.strip())

    for key, value in meta.items():
        if any(hint in key.lower() for hint in LYRIC_KEY_HINTS):
            value = value.strip()
            if value:
                return " / ".join(v.strip() for v in value.splitlines() if v.strip())

    debug_log("no lyric-like key found. Available keys: " + ", ".join(sorted(meta.keys())))
    return None


def get_position_seconds(player: str) -> float:
    raw = run(["playerctl", "-p", player, "position"])
    if raw is None:
        return 0.0
    try:
        return float(raw)
    except ValueError:
        return 0.0


def get_length_seconds(meta: dict) -> float:
    length_us = meta.get("mpris:length", "")
    try:
        return float(length_us) / 1_000_000
    except ValueError:
        return 0.0


def get_track_line(meta: dict) -> Optional[str]:
    title = meta.get("xesam:title", "").strip()
    album = meta.get("xesam:album", "").strip()
    artist = meta.get("xesam:artist", "").strip()
    parts = [p for p in (title, album, artist) if p]
    if not parts:
        return None
    return " | ".join(parts)


def build_tooltip(player: str, status: str, meta: dict) -> str:
    """A tooltip that actually tells you what's playing, instead of a static
    'Click for player controls' string that's misleading if no click action
    is configured in Waybar."""
    title = meta.get("xesam:title", "").strip() or "Unknown title"
    artist = meta.get("xesam:artist", "").strip()
    album = meta.get("xesam:album", "").strip()

    lines = [player]
    lines.append(f"{artist} — {title}" if artist else title)
    if album:
        lines.append(f"Album: {album}")
    lines.append(status.capitalize() if status else "Unknown state")
    return "\n".join(lines)


# ---- Online lyrics (lrclib.net) ---------------------------------------------

_LRC_TIMESTAMP_RE = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]")

# Cache keyed by (artist, title, album) so we only hit the network once per
# track (plus retries on failure), not once per SCROLL_TICK. Lookups run on
# a small bounded thread pool so a slow/unreachable network never stalls the
# scrolling text and rapid track-skipping can't spawn unbounded threads.
_lyrics_cache = collections.OrderedDict()
_lyrics_cache_lock = threading.Lock()
_lrclib_request_lock = threading.Lock()
_lrclib_rate_limit_lock = threading.Lock()
_lrclib_last_request_at = 0.0
_lrclib_rate_limit_until = 0.0
_lyrics_executor = ThreadPoolExecutor(
    max_workers=LYRICS_FETCH_MAX_WORKERS, thread_name_prefix="lrclib-fetch"
)


def _track_key(meta: dict):
    return (
        meta.get("xesam:artist", "").strip().lower(),
        meta.get("xesam:title", "").strip().lower(),
        meta.get("xesam:album", "").strip().lower(),
    )


def _parse_lrc(lrc_text: str) -> list:
    """Parse LRC text into a sorted list of (seconds, text).

    Handles multiple timestamps stacked on one line, e.g.
    "[00:10.00][00:20.00]Hello" (common for repeated chorus lines), by
    emitting one entry per timestamp rather than only the first.
    """
    lines = []
    for raw_line in lrc_text.splitlines():
        timestamps = _LRC_TIMESTAMP_RE.findall(raw_line)
        if not timestamps:
            continue
        text = _LRC_TIMESTAMP_RE.sub("", raw_line).strip()
        if not text:
            continue
        for minutes, seconds in timestamps:
            total_seconds = int(minutes) * 60 + float(seconds)
            lines.append((total_seconds, text))
    lines.sort(key=lambda item: item[0])
    return lines


def _fetch_lrclib(meta: dict) -> dict:
    """Blocking network call — always run this inside the thread pool."""
    title = meta.get("xesam:title", "").strip()
    artist = meta.get("xesam:artist", "").strip()
    album = meta.get("xesam:album", "").strip()
    duration = get_length_seconds(meta)

    if not title or not artist:
        return {"ok": False, "synced": None, "plain": None}

    params = {"track_name": title, "artist_name": artist}
    if album:
        params["album_name"] = album
    if duration:
        params["duration"] = str(int(round(duration)))

    url = LRCLIB_URL + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": LRCLIB_USER_AGENT})

    global _lrclib_last_request_at, _lrclib_rate_limit_until

    # Keep requests sequential and respect server-imposed rate limits.
    with _lrclib_request_lock:
        with _lrclib_rate_limit_lock:
            rate_limit_until = _lrclib_rate_limit_until

        now = time.monotonic()
        if now < rate_limit_until:
            return {
                "ok": False,
                "synced": None,
                "plain": None,
                "retry_after": rate_limit_until - now,
            }

        wait = LRCLIB_MIN_REQUEST_INTERVAL - (now - _lrclib_last_request_at)
        if wait > 0:
            time.sleep(wait)

        try:
            with urllib.request.urlopen(req, timeout=LRCLIB_TIMEOUT) as resp:
                _lrclib_last_request_at = time.monotonic()
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as exc:
            _lrclib_last_request_at = time.monotonic()
            if exc.code == 429:
                retry_after = exc.headers.get("Retry-After", "")
                try:
                    cooldown = max(1.0, float(retry_after))
                except (TypeError, ValueError):
                    cooldown = LYRICS_RETRY_COOLDOWN

                with _lrclib_rate_limit_lock:
                    _lrclib_rate_limit_until = time.monotonic() + cooldown

                debug_log(
                    f"lrclib rate limited '{title}' by '{artist}'; "
                    f"honoring Retry-After for {cooldown:.0f}s"
                )
                return {
                    "ok": False,
                    "synced": None,
                    "plain": None,
                    "retry_after": cooldown,
                }

            debug_log(f"lrclib HTTP error {exc.code} for '{title}' by '{artist}'")
            return {"ok": False, "synced": None, "plain": None}
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            _lrclib_last_request_at = time.monotonic()
            debug_log(f"lrclib lookup failed for '{title}' by '{artist}': {exc}")
            return {"ok": False, "synced": None, "plain": None}
        except (json.JSONDecodeError, UnicodeError) as exc:
            debug_log(f"lrclib returned invalid data for '{title}' by '{artist}': {exc}")
            return {"ok": False, "synced": None, "plain": None}

    synced_raw = data.get("syncedLyrics") or ""
    plain_raw = data.get("plainLyrics") or ""
    synced = _parse_lrc(synced_raw) if synced_raw else None
    plain = plain_raw.strip() or None

    found = "synced" if synced else ("plain" if plain else "none")
    debug_log(f"lrclib result for '{title}' by '{artist}': {found}")

    return {"ok": True, "synced": synced, "plain": plain}


def _evict_oldest_if_over_cap() -> None:
    """Call while holding _lyrics_cache_lock."""
    while len(_lyrics_cache) > LYRICS_CACHE_MAX_ENTRIES:
        _lyrics_cache.popitem(last=False)


def get_online_lyrics(meta: dict) -> Optional[dict]:
    """Non-blocking: returns cached lyrics if available, else kicks off a
    background fetch (bounded by the thread pool) and returns None for this
    tick. Failed lookups are retried after LYRICS_RETRY_COOLDOWN instead of
    being cached as a permanent miss."""
    if not LYRICS_ONLINE_ENABLED:
        return None

    key = _track_key(meta)
    if not key[0] or not key[1]:
        return None

    now = time.monotonic()

    with _lyrics_cache_lock:
        entry = _lyrics_cache.get(key)

        should_submit = False
        if entry is None:
            entry = {"status": "pending", "synced": None, "plain": None, "next_retry": None}
            _lyrics_cache[key] = entry
            _evict_oldest_if_over_cap()
            should_submit = True
        elif entry["status"] == "failed" and entry["next_retry"] is not None and now >= entry["next_retry"]:
            entry["status"] = "pending"
            should_submit = True
        else:
            _lyrics_cache.move_to_end(key)

        if should_submit:
            def worker():
                result = _fetch_lrclib(meta)
                with _lyrics_cache_lock:
                    if result["ok"]:
                        _lyrics_cache[key] = {
                            "status": "done",
                            "synced": result["synced"],
                            "plain": result["plain"],
                            "next_retry": None,
                        }
                    else:
                        retry_delay = result.get("retry_after", LYRICS_RETRY_COOLDOWN)
                        _lyrics_cache[key] = {
                            "status": "failed",
                            "synced": None,
                            "plain": None,
                            "next_retry": time.monotonic() + max(1.0, retry_delay),
                        }
                    _lyrics_cache.move_to_end(key)

            _lyrics_executor.submit(worker)
            return None

        return entry if entry["status"] == "done" else None


def current_synced_line(synced_lines: list, position_seconds: float) -> Optional[str]:
    """Return the lyric line active at the given playback position."""
    active = None
    for line_time, text in synced_lines:
        if line_time <= position_seconds:
            active = text
        else:
            break
    return active


def scroll(text: str, offset: int) -> str:
    """Return a fixed-width window of `text`, scrolling with wraparound."""
    if len(text) <= SCROLL_WINDOW:
        return text
    looped = text + SCROLL_GAP
    doubled = looped + looped
    start = offset % len(looped)
    return doubled[start:start + SCROLL_WINDOW]


def emit(text: str, tooltip: str, css_class: str, alt: str = "") -> None:
    payload = {
        "text": text,
        "tooltip": tooltip,
        "class": css_class,
        "alt": alt,
    }
    print(json.dumps(payload), flush=True)


# ---- Main loop --------------------------------------------------------------


def main() -> None:
    if _PLAYERCTL_PATH is None:
        # Surface this loudly and distinctly instead of looking identical to
        # "nothing is playing" forever.
        emit("playerctl not found", "Install playerctl and ensure it's on PATH", "error")
        debug_log("playerctl not found on PATH; exiting.")
        sys.exit(1)

    offset = 0
    last_source_text = ""
    next_poll_at = 0.0  # time.monotonic() timestamp; poll immediately on first loop
    consecutive_poll_failures = 0

    cache = {"player": None, "status": "", "source_text": "", "css_class": "none", "tooltip": "Nothing playing"}

    while True:
        now = time.monotonic()
        if now >= next_poll_at:
            next_poll_at = now + POLL_INTERVAL

            statuses = get_all_statuses()
            metadata_map = get_all_metadata_multi()

            if statuses is None or metadata_map is None:
                # playerctl itself failed (not "no player open") — say so.
                consecutive_poll_failures += 1
                debug_log(f"poll failed ({consecutive_poll_failures} in a row)")
                cache = {
                    "player": None,
                    "status": "",
                    "source_text": "playerctl error",
                    "css_class": "error",
                    "tooltip": "playerctl command failed — see WAYBAR_MUSIC_DEBUG=1 logs",
                }
            else:
                consecutive_poll_failures = 0
                player = pick_active_player(statuses, metadata_map)

                if not player:
                    cache = {"player": None, "status": "", "source_text": "", "css_class": "none", "tooltip": "Nothing playing"}
                else:
                    meta = metadata_map.get(player, {})
                    status = statuses.get(player, "")
                    tooltip = build_tooltip(player, status, meta)

                    lyrics = find_lyrics(meta)
                    if lyrics:
                        source_text = lyrics
                        css_class = "lyrics playing" if status == "playing" else "lyrics paused"
                    else:
                        online = get_online_lyrics(meta)
                        if online and online["synced"]:
                            position = get_position_seconds(player)
                            line = current_synced_line(online["synced"], position)
                            if line:
                                source_text = line
                                css_class = "lyrics synced playing" if status == "playing" else "lyrics synced paused"
                            else:
                                track_line = get_track_line(meta)
                                source_text = track_line or "Unknown track"
                                css_class = "playing" if status == "playing" else "paused"
                        elif online and online["plain"]:
                            source_text = online["plain"].replace("\n", "  /  ")
                            css_class = "lyrics playing" if status == "playing" else "lyrics paused"
                        else:
                            track_line = get_track_line(meta)
                            source_text = track_line or "Unknown track"
                            css_class = "playing" if status == "playing" else "paused"

                    cache = {
                        "player": player,
                        "status": status,
                        "source_text": source_text,
                        "css_class": css_class,
                        "tooltip": tooltip,
                    }

        if not cache["player"]:
            emit(cache["source_text"] or IDLE_TEXT, cache["tooltip"], cache["css_class"])
            offset = 0
            last_source_text = ""
            time.sleep(SCROLL_TICK)
            continue

        source_text = cache["source_text"]
        if source_text != last_source_text:
            offset = 0
            last_source_text = source_text

        visible = scroll(source_text, offset)
        emit(visible, cache["tooltip"], cache["css_class"], alt=cache["player"])

        offset += 1
        time.sleep(SCROLL_TICK)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
