# Soulseek retrieval plugin

A Melodex retrieval plugin (contract v1.0) that finds music in what [Soulseek](https://www.slsknet.org)'s users share
and downloads it, through your own [slskd](https://github.com/slskd/slskd) and its REST API.

It lives in the Melodex repository for now and will move to its own repository later.

## What it does

- **Search.** Asks slskd to search for the artist and title, waits for peers to answer, and reads what they offered.
  For an album, each peer's folder is a possible copy, offered only when it has a file for every requested track,
  matched by title and length. For a song, each file is scored on its title, the artist in its folders, and its
  length. Among copies that match as well, lossless ones come first, then peers with a free upload slot, a short
  queue and a fast connection. Locked files are left out.
- **Fetch.** Asks slskd to download the chosen files from that peer and follows them. A peer that refuses or fails,
  a download that waits in the peer's queue too long, or one that stalls, fails the fetch and is cancelled in slskd,
  so Melodex moves on to the next copy, usually another peer's. Finished files are moved from slskd's downloads
  folder into the staging directory the core gives it, and taken off slskd's list of downloads.
- **Cancel.** Cancels the downloads in slskd.

## Sharing

Soulseek is for sharing. slskd expects you to share a folder, and some peers won't send to those who share nothing;
the plugin's health says when you don't. Point slskd's `shares.directories` at what you'd like to share, read-only.

## Settings

| Setting | Default | Meaning |
| --- | --- | --- |
| `url` | `http://slskd:5030` | Where the plugin reaches slskd. |
| `apiKey` | | One of slskd's API keys (`web.authentication.api_keys`). |
| `downloadsFolder` | `/downloads` | Where slskd's downloads folder is mounted in this plugin, writable. |
| `format` | `best` | `best` prefers lossless copies and takes lossy ones where there's nothing better; `lossless` offers only FLAC, WAV, AIFF, APE and WavPack. |
| `searchSeconds` | `15` | How long a search waits for peers to answer. |
| `queueMinutes` | `20` | How long to wait in a peer's queue before trying another copy. |
| `stallMinutes` | `5` | How long a download may receive nothing before trying another copy. |
| `minScore` | `0.6` | Folders and files that match less well than this aren't offered. |

## Running it

```yaml
retriever-soulseek:
  build: plugins/retriever-soulseek
  volumes:
    - staging:/staging
    - /path/to/slskd/downloads:/downloads
```

Then add `http://retriever-soulseek:8080` under Settings → Plugins in Melodex (it's one of the templates under Add).

## Tests

```
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest
```

They talk to a fake slskd, and need ffmpeg to make the files a download test moves.
