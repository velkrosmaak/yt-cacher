#!/usr/bin/env python3

from __future__ import annotations
import argparse
import os
import sys
import subprocess
import json
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import List, Dict, Optional
import logging

import requests
import glob

# Setup logging
logging.basicConfig(
    filename="/tmp/ytc.log",
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    filemode="a"
)
logger = logging.getLogger(__name__)


def read_channels(path: str) -> List[str]:
    with open(path, "r", encoding="utf-8") as fh:
        lines = [l.strip() for l in fh if l.strip() and not l.strip().startswith("#")]
    return lines


def get_latest_video_url_for_channel(channel: str) -> Optional[Dict]:
    """Fetch the latest video info for a channel URL using yt-dlp.
    Returns dict with keys: id, url, title, upload_date, description, thumbnails
    """
    import subprocess
    logger.debug(f"Fetching latest video for channel: {channel}")
    
    # Convert @handle to videos tab URL properly
    # if "/@" in channel:
    #     channel = channel + "/videos"
    
    cmd = ["yt-dlp", "--flat-playlist", "--print-json", "--skip-download",
           "-S", "epoch~", "--playlist-items", "1-10", channel]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=30)
    except subprocess.CalledProcessError as e:
        logger.error(f"yt-dlp failed for {channel}: {e.stderr}")
        print(f"yt-dlp failed for {channel}: {e.stderr}", file=sys.stderr)
        return None
    # yt-dlp prints one JSON per line for each entry
    videos = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            j = json.loads(line)
            videos.append(j)
        except Exception as e:
            logger.debug(f"Failed to parse JSON line: {e}")
            continue
    if not videos:
        logger.warning(f"No videos found for channel: {channel}")
        return None
    # Find the latest by smallest playlist_index (1 is newest)
    latest = min(videos, key=lambda v: v.get('playlist_index', float('inf')))
    logger.info(f"Found latest video: {latest.get('id')} - {latest.get('title')}")
    return latest


def download_video(video_url: str, outdir: str, video_id: str, filename_template: str = "%(id)s.mp4") -> Optional[str]:
    import subprocess
    os.makedirs(outdir, exist_ok=True)
    logger.debug(f"Downloading {video_url} to {outdir} with template {filename_template}")
    # Request best video+audio and merge/remux output to MP4
    cmd = ["yt-dlp", "-f", "bestvideo+bestaudio/best", "--merge-output-format", "mp4", "--no-playlist", "-o", os.path.join(outdir, filename_template), video_url]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            logger.error(f"yt-dlp exited with {proc.returncode} for {video_url}: {proc.stderr}")
            print(f"yt-dlp exited with {proc.returncode}")
            return None
    except FileNotFoundError:
        logger.error("yt-dlp not found on PATH")
        print("yt-dlp not found. Install yt-dlp and ensure it's on PATH.")
        return None
    # After download, find the file by looking for video_id.* in outdir
    pattern = os.path.join(outdir, f"{video_id}.*")
    matches = glob.glob(pattern)
    if matches:
        # Return the first match (should be the downloaded file)
        path = os.path.abspath(matches[0])
        logger.info(f"Download complete: {path}")
        return path
    logger.warning(f"No file found after download; pattern was {pattern}")
    return None


def sanitize_filename(name: str, maxlen: int = 100) -> str:
    # Remove or replace characters illegal in filenames
    illegal = '<>:\\\"/|?*'  # Windows forbids these (backslash and quote escaped)
    clean = ''.join('_' if c in illegal or ord(c) < 32 else c for c in name)
    clean = clean.strip()
    if len(clean) > maxlen:
        clean = clean[:maxlen].rstrip()
    return clean


def write_nfo_for_video(outdir: str, video_id: str, episode_num: int, metadata: Dict) -> None:
    """Write a Plex-compatible NFO file for the video (as a TV episode)."""
    nfo_path = os.path.join(outdir, f"{video_id}.nfo")
    root = ET.Element("episodedetails")
    
    # Episode info
    season = ET.SubElement(root, "season")
    season.text = "1"
    episode = ET.SubElement(root, "episode")
    episode.text = str(episode_num)
    
    # Title
    title = ET.SubElement(root, "title")
    title.text = metadata.get("title", "")
    
    # Plot / description - Plex prefers <overview>
    overview = ET.SubElement(root, "overview")
    overview.text = metadata.get("description", "")
    # also include <plot> for compatibility
    plot = ET.SubElement(root, "plot")
    plot.text = metadata.get("description", "")
    
    # Date uploaded
    aired = ET.SubElement(root, "aired")
    dt = metadata.get("upload_date")
    if dt:
        try:
            # yt-dlp format: YYYYMMDD
            year = str(dt)[:4]
            month = str(dt)[4:6]
            day = str(dt)[6:8]
            aired.text = f"{year}-{month}-{day}"
        except Exception:
            aired.text = ""
    
    # Thumb/poster
    thumbs = metadata.get("thumbnails") or []
    if thumbs:
        thumb = ET.SubElement(root, "thumb")
        thumb.text = thumbs[0].get("url") if isinstance(thumbs[0], dict) else str(thumbs[0])
    
    # Duration
    duration = metadata.get("duration")
    if duration:
        runtime = ET.SubElement(root, "runtime")
        runtime.text = str(int(duration // 60))  # minutes
    
    tree = ET.ElementTree(root)
    tree.write(nfo_path, encoding="utf-8", xml_declaration=True)
    print(f"Wrote NFO: {nfo_path}")


def send_pushover(token: str, user: str, message: str, title: Optional[str] = None) -> bool:
    """Send a Pushover notification. Returns True on success."""
    url = "https://api.pushover.net/1/messages.json"
    data = {
        "token": token,
        "user": user,
        "message": message,
    }
    if title:
        data["title"] = title
    try:
        logger.debug(f"Sending Pushover notification: {title} - {message}")
        r = requests.post(url, data=data, timeout=10)
        r.raise_for_status()
        logger.info("Pushover notification sent successfully")
        return True
    except Exception as e:
        logger.error(f"Pushover send failed: {e}")
        print(f"Pushover send failed: {e}")
        return False


def main():
    logger.info("=" * 60)
    logger.info("Starting YouTube Cacher")
    parser = argparse.ArgumentParser(description="Download latest YouTube videos from channels and tag for Kodi")
    parser.add_argument("--channels", required=True, help="Text file with one channel URL or id per line")
    parser.add_argument("--outdir", default="youtube_cache", help="Directory to save videos and NFOs")
    parser.add_argument("--dry-run", action="store_true", help="Only show what would be downloaded")
    parser.add_argument("--pushover-token", help="Pushover application token (or set PUSHOVER_TOKEN env)")
    parser.add_argument("--pushover-user", help="Pushover user/key (or set PUSHOVER_USER env)")
    args = parser.parse_args()
    logger.info(f"Options: channels={args.channels}, outdir={args.outdir}, dry_run={args.dry_run}")

    channels = read_channels(args.channels)

    # Pushover config (CLI args override environment)
    pushover_token = args.pushover_token or os.environ.get("PUSHOVER_TOKEN")
    pushover_user = args.pushover_user or os.environ.get("PUSHOVER_USER")

    if not pushover_token or not pushover_user:
        print("Notifications not configured, skipping Pushover notifications.")

    # Track episode numbers per channel for Plex TV naming
    episode_counter: Dict[str, int] = {}

    for ch in channels:
        logger.info(f"Processing channel: {ch}")
        print(f"Processing channel: {ch}")
        info = get_latest_video_url_for_channel(ch)
        if not info:
            logger.warning(f"No latest video found for {ch}")
            print(f"No latest video found for {ch}")
            continue
        
        vid = info.get("id")
        vurl = f"https://www.youtube.com/watch?v={vid}"
        full_meta = info  # Use the metadata from RSS
        
        logger.debug(f"Using video ID: {vid}, title: {info.get('title')}")
        
        if args.dry_run:
            print(json.dumps({"channel": ch, "video": info}, indent=2))
            continue
        
        # Extract channel name from URL (remove @ prefix if present)
        chan_name = ch
        if "/@" in ch:
            # Extract just the handle name after /@
            chan_name = ch.split("/@")[-1].split("/")[0]
        elif "/c/" in ch:
            chan_name = ch.split("/c/")[-1].split("/")[0]
        elif "/user/" in ch:
            chan_name = ch.split("/user/")[-1].split("/")[0]
        
        chan_name = sanitize_filename(chan_name)
        
        # Initialize episode counter for this channel if needed
        if chan_name not in episode_counter:
            episode_counter[chan_name] = 1
        else:
            episode_counter[chan_name] += 1
        
        ep_num = episode_counter[chan_name]
        
        # Directory: outdir/ChannelName/Season 01/
        video_outdir = os.path.abspath(os.path.join(args.outdir, chan_name, "Season 01"))
        
        # Get title for filename
        title = full_meta.get("title", vid) if full_meta else (info.get("title") or vid)
        title = sanitize_filename(title)
        
        # Filename: ChannelName - s01e## - VideoTitle.mp4
        plex_filename = f"{chan_name} - s01e{ep_num:02d} - {title}.mp4"
        
        # Check if episode already exists
        exists = False
        if os.path.isdir(video_outdir):
            for fn in os.listdir(video_outdir):
                if f"s01e{ep_num:02d}" in fn and fn.endswith('.mp4'):
                    print(f"Episode already present as {fn}, skipping download")
                    logger.info(f"Episode already exists: {fn}")
                    exists = True
                    break
        
        if not exists:
            print(f"Downloading {vurl} -> {video_outdir}/{plex_filename}")
            logger.info(f"Downloading: {vurl}")
            downloaded_path = download_video(vurl, video_outdir, vid, filename_template=plex_filename)
        else:
            downloaded_path = None
        
        # Write NFO file
        write_nfo_for_video(video_outdir, vid, ep_num, full_meta or {})
        
        # Send notification if downloaded
        if downloaded_path:
            print(f"Downloaded to {downloaded_path}")
            if pushover_token and pushover_user:
                msg = f"{chan_name} - s01e{ep_num:02d} - {title}"
                send_pushover(pushover_token, pushover_user, msg, title="YouTube Cacher: Download complete")


if __name__ == "__main__":
    main()

