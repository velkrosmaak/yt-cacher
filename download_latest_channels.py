#!/usr/bin/env python3
"""Download the latest released video for each YouTube channel in channels.txt.

Requires:
  pip install yt-dlp
  ffmpeg available on PATH

The script:
  - reads channel URLs from channels.txt
  - finds the latest released video on each channel
  - downloads the video as MP4 when possible
  - stores it as:
      DEST_ROOT/<channel>/Season 1/s01eNN <channel> - <video title>.mp4
  - embeds basic MP4 metadata to help media managers
  - creates Plex-friendly NFO sidecar metadata files
  - optionally sends Pushover notifications for new downloads
  - optionally trims sponsor segments using SponsorBlock
  - skips channels whose latest video is already present
"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import os
import re
import shutil
import sys
import json
import subprocess
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

try:
    import yt_dlp
except ModuleNotFoundError:
    yt_dlp = None


# Default output directory when --outdir is not provided.
DEST_ROOT = Path.cwd()

# Default channel list path when --channels is not provided.
CHANNELS_FILE = Path.cwd() / "channels.txt"

# Path to Pushover credentials in key=value format.
PUSHOVER_FILE = Path("pushover.txt")

# Log file appended on each run.
LOG_FILE = Path(__file__).resolve().parent / "yt-cache.log"
README_FILE = Path(__file__).resolve().parent / "README.md"

# Plex TV-style folder naming works best with "Season 1".
SEASON_NAME = "Season 1"


INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1F]')
EPISODE_PATTERN = re.compile(r"^s01e(\d{2,})\b", re.IGNORECASE)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download the latest released video from each YouTube channel in a list."
    )
    parser.add_argument(
        "--outdir",
        type=Path,
        default=DEST_ROOT,
        help="Directory where the show folders and episodes will be written.",
    )
    parser.add_argument(
        "--channels",
        type=Path,
        default=CHANNELS_FILE,
        help="Text file containing one YouTube channel URL per line.",
    )
    parser.add_argument(
        "--trim-sponsors",
        action="store_true",
        help="Trim SponsorBlock sponsor segments from downloaded videos.",
    )
    return parser.parse_args()


def fail_startup(message: str) -> int:
    print(f"Startup validation failed: {message}", file=sys.stderr)
    print(f"See {README_FILE} for setup and usage details.", file=sys.stderr)
    return 1


def sanitize_name(value: str, fallback: str) -> str:
    cleaned = INVALID_FILENAME_CHARS.sub("", value).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = cleaned.rstrip(".")
    return cleaned or fallback


def load_channels(path: Path) -> list[str]:
    if not path.exists():
        raise FileNotFoundError(
            f"Missing {path}. Create it with one YouTube channel URL per line."
        )

    channels: list[str] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        channels.append(line)
    return channels


def validate_startup(
    outdir: Path,
    channels_file: Path,
    pushover_file: Path,
    log_file: Path,
) -> str | None:
    if yt_dlp is None:
        return "Missing Python dependency 'yt-dlp'. Install it with: pip install -r requirements.txt"

    if shutil.which("ffmpeg") is None:
        return "Missing system dependency 'ffmpeg'. Install ffmpeg and ensure it is on PATH."

    if not channels_file.exists():
        return f"Channel list file does not exist: {channels_file}"
    if not channels_file.is_file():
        return f"Channel list path is not a file: {channels_file}"

    try:
        channels_file.read_text(encoding="utf-8")
    except OSError as exc:
        return f"Channel list file is not readable: {channels_file} ({exc})"

    try:
        outdir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return f"Could not create output directory {outdir}: {exc}"
    if not outdir.is_dir():
        return f"Output path is not a directory: {outdir}"
    if not os.access(outdir, os.W_OK):
        return f"Output directory is not writable: {outdir}"

    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with log_file.open("a", encoding="utf-8"):
            pass
    except OSError as exc:
        return f"Log file is not writable: {log_file} ({exc})"

    if pushover_file.exists() and pushover_file.is_dir():
        return f"Pushover config path is a directory, not a file: {pushover_file}"

    return None


def load_key_value_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}

    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip().lower()] = value.strip()
    return values


def load_pushover_config(path: Path) -> dict[str, str] | None:
    config = load_key_value_file(path)
    if not config:
        return None

    user = config.get("user_key")
    token = config.get("app_token")
    if not user or not token:
        print(
            f"Pushover config at {path} is missing user_key or app_token; notifications disabled.",
            file=sys.stderr,
        )
        return None
    return {"user_key": user, "app_token": token}


def send_pushover_notification(
    config: dict[str, str] | None,
    channel_name: str,
    episode_title: str,
    sponsorblock_trimmed: bool,
) -> None:
    if not config:
        return

    print("Sending Pushover notification...")
    message = f"{channel_name}\n{episode_title}"
    if sponsorblock_trimmed:
        message += "\nSponsorBlock removed sponsor segments."

    payload = urlencode(
        {
            "token": config["app_token"],
            "user": config["user_key"],
            "title": "New YouTube episode downloaded",
            "message": message,
        }
    ).encode("utf-8")
    request = Request(
        "https://api.pushover.net/1/messages.json",
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )

    try:
        with urlopen(request, timeout=15) as response:
            resp_data = response.read().decode("utf-8")
            print(f"Pushover response: {resp_data}")
    except Exception as exc:
        if hasattr(exc, "read"):
            print(f"Pushover error response: {exc.read().decode('utf-8')}", file=sys.stderr)
        raise


def append_run_log(log_path: Path, downloads: list[tuple[str, str]]) -> None:
    timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    lines = [f"[{timestamp}]"]
    if downloads:
        lines.extend(f"- {channel_name}: {episode_title}" for channel_name, episode_title in downloads)
    else:
        lines.append("- No new videos downloaded.")

    with log_path.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines))
        handle.write("\n\n")


def fetch_sponsor_segments(video_id: str) -> list[list[float]]:
    payload = urlencode({
        "videoID": video_id,
        "category": "sponsor",
        "actionType": "skip",
    })
    request = Request(
        f"https://sponsor.ajay.app/api/skipSegments?{payload}",
        headers={"User-Agent": "yt-cacher/1.0"},
        method="GET",
    )
    with urlopen(request, timeout=15) as response:
        data = json.loads(response.read().decode("utf-8"))

    segments: list[list[float]] = []
    for item in data:
        segment = item.get("segment")
        if (
            isinstance(segment, list)
            and len(segment) == 2
            and all(isinstance(value, (int, float)) for value in segment)
        ):
            start, end = float(segment[0]), float(segment[1])
            if end > start:
                segments.append([start, end])
    return sorted(segments, key=lambda item: item[0])


def build_keep_segments(duration: float, skip_segments: list[list[float]]) -> list[list[float]]:
    if duration <= 0:
        return []

    merged: list[list[float]] = []
    for start, end in skip_segments:
        bounded_start = max(0.0, min(duration, start))
        bounded_end = max(0.0, min(duration, end))
        if bounded_end <= bounded_start:
            continue
        if merged and bounded_start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], bounded_end)
        else:
            merged.append([bounded_start, bounded_end])

    keep_segments: list[list[float]] = []
    cursor = 0.0
    for start, end in merged:
        if start > cursor:
            keep_segments.append([cursor, start])
        cursor = max(cursor, end)
    if cursor < duration:
        keep_segments.append([cursor, duration])
    return [segment for segment in keep_segments if segment[1] - segment[0] > 0.25]


def trim_sponsor_segments(video_path: Path, video_info: dict) -> bool:
    video_id = video_info.get("id")
    duration = float(video_info.get("duration") or 0)
    if not video_id or duration <= 0:
        return False

    skip_segments = fetch_sponsor_segments(video_id)
    if not skip_segments:
        return False

    keep_segments = build_keep_segments(duration, skip_segments)
    if not keep_segments:
        return False
    if len(keep_segments) == 1 and abs(keep_segments[0][1] - duration) < 0.25:
        return False

    temp_dir = video_path.parent / ".sponsorblock"
    temp_dir.mkdir(parents=True, exist_ok=True)
    list_file = temp_dir / f"{hashlib.sha1(video_path.name.encode('utf-8')).hexdigest()}.txt"
    part_files: list[Path] = []

    try:
        for index, (start, end) in enumerate(keep_segments, start=1):
            part_path = temp_dir / f"part_{index:03d}.mp4"
            cmd = [
                "ffmpeg",
                "-y",
                "-ss",
                f"{start:.3f}",
                "-to",
                f"{end:.3f}",
                "-i",
                str(video_path),
                "-c",
                "copy",
                "-avoid_negative_ts",
                "1",
                str(part_path),
            ]
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            part_files.append(part_path)

        list_file.write_text(
            "".join(f"file '{part_path.name}'\n" for part_path in part_files),
            encoding="utf-8",
        )
        trimmed_path = video_path.with_name(f"{video_path.stem}.trimmed{video_path.suffix}")
        cmd = [
            "ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_file),
            "-c",
            "copy",
            str(trimmed_path),
        ]
        subprocess.run(cmd, check=True, cwd=temp_dir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        shutil.move(str(trimmed_path), str(video_path))
        return True
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def latest_video_info(channel_url: str) -> dict:
    opts = {
        "extract_flat": True,
        "playlistend": 1,
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": False,
    }

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"{channel_url.rstrip('/')}/videos", download=False)

    entries = info.get("entries") or []
    if not entries:
        raise RuntimeError(f"No videos found for channel: {channel_url}")

    latest = entries[0]
    video_url = latest.get("url")
    if not video_url:
        raise RuntimeError(f"Could not determine latest video URL for: {channel_url}")

    if not str(video_url).startswith("http"):
        video_url = f"https://www.youtube.com/watch?v={video_url}"

    opts = {
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": False,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(video_url, download=False)


def ensure_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def existing_video_for_id(season_dir: Path, video_id: str) -> Path | None:
    index_path = season_dir / ".youtube_ids.json"
    if not index_path.exists():
        return None

    try:
        index_data = json.loads(index_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None

    filename = index_data.get(video_id)
    if not filename:
        return None

    candidate = season_dir / filename
    return candidate if candidate.exists() else None


def next_episode_number(season_dir: Path) -> int:
    highest = 0
    for file_path in season_dir.glob("*.mp4"):
        match = EPISODE_PATTERN.match(file_path.name)
        if match:
            highest = max(highest, int(match.group(1)))
    return highest + 1


def build_destination_paths(channel_name: str, video_title: str, episode_number: int) -> tuple[Path, str]:
    safe_channel = sanitize_name(channel_name, "Unknown Channel")
    safe_title = sanitize_name(video_title, "Untitled Video")
    season_dir = DEST_ROOT / safe_channel / SEASON_NAME
    episode_code = f"s01e{episode_number:02d}"
    filename = f"{episode_code} {safe_channel} - {safe_title}.mp4"
    return season_dir / filename, safe_channel


def write_video_index(season_dir: Path, video_id: str, filename: str) -> None:
    index_path = season_dir / ".youtube_ids.json"
    data: dict[str, str] = {}
    if index_path.exists():
        try:
            data = json.loads(index_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}

    data[video_id] = filename
    index_path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def format_upload_date(raw_value: str | None) -> str:
    if not raw_value or len(raw_value) != 8 or not raw_value.isdigit():
        return ""
    return f"{raw_value[0:4]}-{raw_value[4:6]}-{raw_value[6:8]}"


def prettify_xml(root: ET.Element) -> bytes:
    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def write_tvshow_nfo(
    show_dir: Path,
    channel_name: str,
    channel_url: str,
    video_info: dict,
) -> None:
    root = ET.Element("tvshow")
    ET.SubElement(root, "title").text = channel_name
    ET.SubElement(root, "showtitle").text = channel_name
    ET.SubElement(root, "sorttitle").text = channel_name
    ET.SubElement(root, "plot").text = (
        video_info.get("channel_description")
        or video_info.get("uploader")
        or f"YouTube channel archive for {channel_name}"
    )
    ET.SubElement(root, "studio").text = channel_name
    ET.SubElement(root, "premiered").text = format_upload_date(
        video_info.get("upload_date")
    )
    ET.SubElement(root, "url").text = channel_url

    unique_id = ET.SubElement(root, "uniqueid", type="youtube", default="true")
    unique_id.text = (
        video_info.get("channel_id")
        or video_info.get("uploader_id")
        or channel_url
    )

    (show_dir / "tvshow.nfo").write_bytes(prettify_xml(root))


def write_episode_nfo(
    nfo_path: Path,
    channel_name: str,
    video_info: dict,
    episode_number: int,
) -> None:
    root = ET.Element("episodedetails")
    ET.SubElement(root, "title").text = video_info.get("title") or "Untitled Video"
    ET.SubElement(root, "showtitle").text = channel_name
    ET.SubElement(root, "season").text = "1"
    ET.SubElement(root, "episode").text = str(episode_number)
    ET.SubElement(root, "plot").text = video_info.get("description") or ""
    ET.SubElement(root, "aired").text = format_upload_date(video_info.get("upload_date"))
    ET.SubElement(root, "studio").text = channel_name
    ET.SubElement(root, "runtime").text = str(
        max(1, int((video_info.get("duration") or 0) / 60))
    )
    ET.SubElement(root, "url").text = video_info.get("webpage_url") or ""

    unique_id = ET.SubElement(root, "uniqueid", type="youtube", default="true")
    unique_id.text = video_info.get("id") or ""

    nfo_path.write_bytes(prettify_xml(root))


def add_mp4_metadata(source_path: Path, target_path: Path, video_info: dict, channel_name: str, episode_number: int) -> None:
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(source_path),
        "-map",
        "0",
        "-c",
        "copy",
        "-metadata",
        f"title={video_info['title']}",
        "-metadata",
        f"show={channel_name}",
        "-metadata",
        f"artist={channel_name}",
        "-metadata",
        f"album={channel_name} - Season 1",
        "-metadata",
        f"episode_id=s01e{episode_number:02d}",
        "-metadata",
        "season_number=1",
        "-metadata",
        f"comment={video_info.get('webpage_url', '')}",
        str(target_path),
    ]
    subprocess.run(cmd, check=True)


def download_video(video_info: dict, target_path: Path, channel_name: str, episode_number: int) -> None:
    ensure_directory(target_path.parent)
    temp_dir = target_path.parent / ".tmp_downloads"
    ensure_directory(temp_dir)

    temp_template = "%(uploader)s - %(title)s [%(id)s].%(ext)s"

    opts = {
        "format": "bv*+ba/b",
        "merge_output_format": "mp4",
        "outtmpl": temp_template,
        "paths": {"home": str(temp_dir)},
        "quiet": False,
        "noplaylist": True,
        "writethumbnail": False,
        "updatetime": False,
    }

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([video_info["webpage_url"]])

        downloaded_files = sorted(
            temp_dir.rglob("*.mp4"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if not downloaded_files:
            raise RuntimeError(
                f"Download finished, but no MP4 file was found for {video_info['title']}"
            )

        latest_file = downloaded_files[0]
        add_mp4_metadata(latest_file, target_path, video_info, channel_name, episode_number)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def process_channel(
    channel_url: str,
    pushover_config: dict[str, str] | None,
    trim_sponsors: bool,
) -> tuple[str, str] | None:
    video_info = latest_video_info(channel_url)

    channel_name = sanitize_name(
        video_info.get("channel")
        or video_info.get("uploader")
        or video_info.get("creator")
        or "Unknown Channel",
        "Unknown Channel",
    )
    video_title = video_info.get("title") or "Untitled Video"
    video_id = video_info.get("id")
    if not video_id:
        raise RuntimeError(f"Missing video id for latest upload from {channel_url}")

    show_dir = DEST_ROOT / channel_name
    season_dir = DEST_ROOT / channel_name / SEASON_NAME
    ensure_directory(show_dir)
    ensure_directory(season_dir)
    write_tvshow_nfo(show_dir, channel_name, channel_url, video_info)

    already_downloaded = existing_video_for_id(season_dir, video_id)
    if already_downloaded:
        print(f"Skipping {channel_name}: latest video already exists at {already_downloaded}")
        try:
            send_pushover_notification(
                pushover_config,
                channel_name,
                f"No new videos found. Latest video already exists: {video_title}",
                False,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"Notification failed for {channel_name}: {exc}", file=sys.stderr)
        return None

    episode_number = next_episode_number(season_dir)
    target_path, safe_channel = build_destination_paths(channel_name, video_title, episode_number)
    print(f"Downloading latest video for {safe_channel}")
    download_video(video_info, target_path, safe_channel, episode_number)
    sponsorblock_trimmed = False
    if trim_sponsors:
        try:
            sponsorblock_trimmed = trim_sponsor_segments(target_path, video_info)
            if sponsorblock_trimmed:
                print(f"Trimmed sponsor segments for {safe_channel}: {video_title}")
        except Exception as exc:  # noqa: BLE001
            print(f"SponsorBlock trim failed for {safe_channel}: {exc}", file=sys.stderr)
    write_episode_nfo(
        target_path.with_suffix(".nfo"),
        safe_channel,
        video_info,
        episode_number,
    )
    write_video_index(season_dir, video_id, target_path.name)
    try:
        send_pushover_notification(
            pushover_config,
            safe_channel,
            video_title,
            sponsorblock_trimmed,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"Notification failed for {safe_channel}: {exc}", file=sys.stderr)
    print(f"Saved to {target_path}")
    return safe_channel, video_title


def main() -> int:
    try:
        args = parse_args()
        global DEST_ROOT, CHANNELS_FILE
        DEST_ROOT = args.outdir.expanduser().resolve()
        CHANNELS_FILE = args.channels.expanduser().resolve()

        startup_error = validate_startup(
            DEST_ROOT,
            CHANNELS_FILE,
            PUSHOVER_FILE.resolve(),
            LOG_FILE,
        )
        if startup_error:
            return fail_startup(startup_error)

        channels = load_channels(CHANNELS_FILE)
        pushover_config = load_pushover_config(PUSHOVER_FILE)
        downloaded_items: list[tuple[str, str]] = []
        if not channels:
            return fail_startup(
                f"No channel URLs found in {CHANNELS_FILE}. Add one YouTube channel URL per line."
            )

        ensure_directory(DEST_ROOT)

        for channel_url in channels:
            try:
                result = process_channel(channel_url, pushover_config, args.trim_sponsors)
                if result:
                    downloaded_items.append(result)
            except Exception as exc:  # noqa: BLE001
                print(f"Failed for {channel_url}: {exc}", file=sys.stderr)

        append_run_log(LOG_FILE, downloaded_items)
        return 0
    except Exception as exc:  # noqa: BLE001
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
