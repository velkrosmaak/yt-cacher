# yt-cacher

This script downloads the latest video from a list of YouTube channels defined in `channels.txt`. 

It creates an accompanying NFO file so that Kodi or Plex can scan it and add it to your library. It also creates a directory structure using channel names as subdirectotries.

This whole process has the unintended side effect of allowing you to watch your YouTube subscriptions without ads. 

It has no scheduling and is designed to bre triggered by `cron`.


## Setup
* Install Python and pip
* `pip install -r requirements.txt`
* Create a text file called `channels.txt` and add YouTube channel URLs to it. One per line.
 
## Notification setup

Pushover notifications are supported and will trigger when a new video is downloaded, containing the channel name and the name of the video.

Get a Pushover account here: https://pushover.net/

### How to supply your Pushover credentials

Environment (recommended):
export PUSHOVER_TOKEN=<token> 
export PUSHOVER_USER=<userkey>

Or pass on CLI:
--pushover-token <token> --pushover-user <userkey>

# Usage
* `--channels` - the path and name of the file containing the list of channels
* `--outdir` - the directory that files get downloaded to (i.e. Your Plex/Kodi media directory)
* (Optional) `--pushover-token <token>`
* (Optional) `--pushover-user <userkey>`

`python ytc.py --channels channels.txt --outdir /yournas/videos/youtube`