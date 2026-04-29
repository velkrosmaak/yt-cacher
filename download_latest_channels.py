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
  - skips channels whose latest video is already present
"""

from __future__ import annotations

import re
import shutil
import sys
import json
import subprocess
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

import yt_dlp


# Update this to wherever Plex scans your library from.
DEST_ROOT = Path("cache")

# Path to the text file containing one YouTube channel URL per line.
CHANNELS_FILE = Path("channels.txt")

# Path to Pushover credentials in key=value format.
PUSHOVER_FILE = Path("pushover.txt")

# Plex TV-style folder naming works best with "Season 1".
SEASON_NAME = "Season 1"


INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1F]')
EPISODE_PATTERN = re.compile(r"^s01e(\d{2,})\b", re.IGNORECASE)


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
) -> None:
    if not config:
        return

    payload = urlencode(
        {
            "token": config["app_token"],
            "user": config["user_key"],
            "title": "New YouTube episode downloaded",
            "message": f"{channel_name}\n{episode_title}",
        }
    ).encode("utf-8")
    request = Request(
        "https://api.pushover.net/1/messages.json",
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )

    with urlopen(request, timeout=15) as response:
        response.read()


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


def process_channel(channel_url: str, pushover_config: dict[str, str] | None) -> None:
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
        return

    episode_number = next_episode_number(season_dir)
    target_path, safe_channel = build_destination_paths(channel_name, video_title, episode_number)
    print(f"Downloading latest video for {safe_channel}")
    download_video(video_info, target_path, safe_channel, episode_number)
    write_episode_nfo(
        target_path.with_suffix(".nfo"),
        safe_channel,
        video_info,
        episode_number,
    )
    write_video_index(season_dir, video_id, target_path.name)
    try:
        send_pushover_notification(pushover_config, safe_channel, video_title)
    except Exception as exc:  # noqa: BLE001
        print(f"Notification failed for {safe_channel}: {exc}", file=sys.stderr)
    print(f"Saved to {target_path}")


def main() -> int:
    try:
        channels = load_channels(CHANNELS_FILE)
        pushover_config = load_pushover_config(PUSHOVER_FILE)
        if not channels:
            print(f"No channel URLs found in {CHANNELS_FILE}")
            return 1

        ensure_directory(DEST_ROOT)

        for channel_url in channels:
            try:
                process_channel(channel_url, pushover_config)
            except Exception as exc:  # noqa: BLE001
                print(f"Failed for {channel_url}: {exc}", file=sys.stderr)

        return 0
    except Exception as exc:  # noqa: BLE001
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
