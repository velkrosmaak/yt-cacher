# yt-cacher

This script downloads the latest video from a list of YouTube channels.

It creates an accompanying NFO file so that Kodi or Plex can scan it and add it to your library. It also creates a directory structure using channel names as subdirectotries.

This whole process has the unintended side effect of allowing you to watch your YouTube subscriptions without ads. 

It has no scheduling and is designed to bre triggered by `cron`.


## Setup
* Install Python and pip
* `git clone https://github.com/velkrosmaak/yt-cacher.git`
* `cd yt-cacher`
* `pip install -r requirements.txt`
* Create a text file called `channels.txt` and add YouTube channel URLs to it, one per line. This can be anywhere.
 
## Notification setup

Pushover notifications are supported and will trigger when a new video is downloaded, containing the channel name and the name of the video.

Get a Pushover account here: https://pushover.net/

Fill in `pushover.txt` in the project directory:

`app_token=YOUR_PUSHOVER_APP_TOKEN`

`user_key=YOUR_PUSHOVER_USER_KEY`

## Cron setup
`crontab -e`

Add this to the bottom of your crontab file to run this at 23:15 daily.

`15 23 * * * python /some/directory/yt-cacher/download_latest_channels.py --channels /some/directory/channels.txt --outdir /your/nas/videos/youtube`

# Usage
* `--channels` - the path and name of the file containing the list of channels
* `--outdir` - the directory that files get downloaded to (i.e. Your Plex/Kodi media directory)

`python download_latest_channels.py --channels /path/to/channels.txt --outdir /yournas/videos/youtube`
