# Whizlabs Video Extraction & Merging Guide

Step-by-step instructions to download and combine course videos from Whizlabs Business.

---

### 1. Install Dependencies
```bash
uv sync
```
Installs all project tools (Playwright, yt-dlp, and ffmpeg muxer) into your local environment.

```bash
uv run playwright install chromium
```
Downloads the Chromium browser binary that Playwright needs for the login window.

---

### 2. Log In and Save Session
```bash
uv run python videograb.py login https://business.whizlabs.com/ --storage-state wl.json --save-cookies cookies.txt
```
Pops open a browser window for you to sign in normally, then saves your authentication tokens and cookies when you hit Enter in the terminal.

---

### 3. Preview Course Videos (Optional)
```bash
uv run python videograb.py extract "https://business.whizlabs.com/learn/course/besa-generative-ai-labs-subscription/4418/oc" --storage-state wl.json
```
Queries Whizlabs with your saved login and prints a table of all discovered video lectures without downloading anything.

---

### 4. Download Course Videos
```bash
uv run python videograb.py download "https://business.whizlabs.com/learn/course/besa-generative-ai-labs-subscription/4418/oc" --storage-state wl.json -o ./besa_videos
```
Pulls down all course videos directly into the `./besa_videos` folder using your saved session.

---

### 5. Combine Video & Audio Tracks
```bash
uv run python merge_videos.py ./besa_videos -c
```
Losslessly glues the separate video and sound files into finished MP4s and wipes out the leftover unmerged tracks.
