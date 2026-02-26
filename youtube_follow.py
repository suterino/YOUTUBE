#!/usr/bin/env python3
"""
YouTube Follow - Latest Videos Tracker

Reads follow.txt, fetches recent videos from YouTube channels,
generates an interactive HTML page with embedded videos and transcript download.

Usage:
    python3 youtube_follow.py           # Fetch videos, generate HTML, start server
    python3 youtube_follow.py --serve   # Just start server (skip fetching)
"""

import subprocess
import json
import os
import sys
import re
import html
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
import unicodedata
import time

# ============================================================
# CONFIGURATION
# ============================================================
DAYS_BACK = 10                     # How many days back to look for videos
FOLLOW_FILE = "follow.txt"         # File with YouTube channel URLs
OUTPUT_HTML = "latest_videos.html"  # Output HTML file
TRANSCRIPTS_DIR = "transcripts"    # Directory for downloaded transcripts
HISTORY_FILE = os.path.join("transcripts", "history.json")
SERVER_PORT = 8080                 # Local server port
DEFAULT_LANG = "en"                # Default subtitle language if not specified
# ============================================================


def load_history():
    """Load download history from JSON file. Creates empty list if missing."""
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def save_history(history):
    """Save download history to JSON file."""
    os.makedirs(TRANSCRIPTS_DIR, exist_ok=True)
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2, ensure_ascii=False)


def read_follow_file(filepath):
    """Read follow.txt and return channels and individual video URLs.

    Lines starting with # are comments and are ignored.
    Channel URLs contain /@, individual video URLs contain /watch?v=.
    Format: URL [language:XX]
    """
    channels = []
    videos = []
    with open(filepath, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if not line.startswith("http"):
                continue
            lang_match = re.search(r"language:(\w+)", line)
            lang = lang_match.group(1) if lang_match else DEFAULT_LANG
            url = line.split()[0]
            if "watch?v=" in url or "youtu.be/" in url:
                videos.append({"url": url, "language": lang})
            else:
                channels.append({"url": url, "language": lang})
    return channels, videos


def get_channel_name(url):
    """Extract channel name from URL like https://www.youtube.com/@MarcoCasario."""
    match = re.search(r"@([\w]+)", url)
    return match.group(1) if match else url.rstrip("/").split("/")[-1]


def get_channel_id(channel_url):
    """Fetch the channel page to extract the channel ID."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        req = urllib.request.Request(channel_url, headers=headers)
        resp = urllib.request.urlopen(req, timeout=15)
        page_html = resp.read().decode("utf-8", errors="ignore")
        for pattern in [
            r'externalId":"(UC[^"]+)',
            r'channelId":"(UC[^"]+)',
            r'channel_id=(UC[^"&]+)',
        ]:
            match = re.search(pattern, page_html)
            if match:
                return match.group(1)
    except Exception as e:
        print(f"    Error resolving channel ID: {e}")
    return None


def _fetch_via_ytdlp(channel_url, channel_name, days_back, language):
    """Fallback: fetch recent videos using yt-dlp when RSS fails."""
    date_after = (datetime.now() - timedelta(days=days_back)).strftime("%Y%m%d")
    url = channel_url.rstrip("/")
    if not url.endswith("/videos"):
        url += "/videos"
    cmd = [
        "yt-dlp",
        "--dateafter", date_after,
        "--playlist-end", "15",
        "-j",
        "--no-download",
        url,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        videos = []
        for line in result.stdout.strip().split("\n"):
            if line.strip():
                try:
                    data = json.loads(line)
                    videos.append({
                        "channel": channel_name,
                        "title": data.get("title", "Unknown"),
                        "url": data.get("webpage_url", data.get("url", "")),
                        "video_id": data.get("id", ""),
                        "upload_date": data.get("upload_date", ""),
                        "duration": data.get("duration", 0),
                        "view_count": data.get("view_count", 0),
                        "language": language,
                    })
                except json.JSONDecodeError:
                    continue
        print(f"    Found {len(videos)} recent videos from {channel_name} (via yt-dlp)")
        return videos
    except subprocess.TimeoutExpired:
        print(f"    yt-dlp fallback also timed out for {channel_name}")
        return []
    except Exception as e:
        print(f"    yt-dlp fallback error for {channel_name}: {e}")
        return []


def fetch_recent_videos(channel_url, days_back, language=DEFAULT_LANG):
    """Fetch recent videos using YouTube's RSS feed (fast, no yt-dlp needed)."""
    channel_name = get_channel_name(channel_url)
    print(f"  Fetching videos from {channel_name} (lang: {language})...")

    # Step 1: resolve channel handle to channel ID
    channel_id = get_channel_id(channel_url)
    if not channel_id:
        print(f"    Could not resolve channel ID for {channel_name}")
        return []

    # Step 2: fetch RSS feed with retries (YouTube is flaky)
    feed_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    rss_headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/xml",
    }
    xml_data = None
    for attempt in range(1, 6):
        try:
            req = urllib.request.Request(feed_url, headers=rss_headers)
            resp = urllib.request.urlopen(req, timeout=10)
            xml_data = resp.read()
            break
        except Exception as e:
            if attempt < 5:
                print(f"    RSS attempt {attempt} failed ({e}), retrying...")
                time.sleep(2)
            else:
                print(f"    RSS feed failed after 5 attempts, falling back to yt-dlp...")

    if xml_data is None:
        return _fetch_via_ytdlp(channel_url, channel_name, days_back, language)

    # Step 3: parse XML and filter by date
    ns = {
        "atom": "http://www.w3.org/2005/Atom",
        "yt": "http://www.youtube.com/xml/schemas/2015",
        "media": "http://search.yahoo.com/mrss/",
    }
    cutoff = datetime.now() - timedelta(days=days_back)
    videos = []

    try:
        root = ET.fromstring(xml_data)
        for entry in root.findall("atom:entry", ns):
            published_el = entry.find("atom:published", ns)
            if published_el is None:
                continue
            published = published_el.text
            # Parse ISO date like "2026-02-16T15:00:00+00:00"
            pub_date = datetime.fromisoformat(published.replace("Z", "+00:00"))

            if pub_date.replace(tzinfo=None) >= cutoff:
                video_id_el = entry.find("yt:videoId", ns)
                title_el = entry.find("atom:title", ns)
                stats_el = entry.find(".//media:statistics", ns)

                video_id = video_id_el.text if video_id_el is not None else ""
                title = title_el.text if title_el is not None else "Unknown"
                views = int(stats_el.get("views", 0)) if stats_el is not None else 0

                videos.append({
                    "channel": channel_name,
                    "title": title,
                    "url": f"https://www.youtube.com/watch?v={video_id}",
                    "video_id": video_id,
                    "upload_date": pub_date.strftime("%Y%m%d"),
                    "duration": 0,
                    "view_count": views,
                    "language": language,
                })
    except ET.ParseError as e:
        print(f"    Error parsing RSS feed: {e}")
        return []

    print(f"    Found {len(videos)} recent videos from {channel_name}")
    return videos


def download_individual_videos(video_entries):
    """Download transcripts for individual video URLs from follow.txt.

    Skips videos already in history. Updates history.json on success.
    """
    if not video_entries:
        return

    history = load_history()
    downloaded_urls = {entry["url"] for entry in history if entry.get("url")}

    os.makedirs(TRANSCRIPTS_DIR, exist_ok=True)

    for entry in video_entries:
        url = entry["url"]
        lang = entry["language"]

        # Normalize URL for comparison (strip trailing params like &t=)
        base_url = url.split("&")[0] if "&" in url else url
        if any(h_url.split("&")[0] == base_url for h_url in downloaded_urls):
            print(f"  Skipping (already downloaded): {url}")
            continue

        print(f"  Fetching metadata for: {url}")

        # Get video metadata via yt-dlp
        cmd = ["yt-dlp", "-j", "--no-download", url]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if not result.stdout.strip():
                print(f"    Could not fetch metadata")
                continue
            data = json.loads(result.stdout.strip().split("\n")[0])
        except Exception as e:
            print(f"    Error fetching metadata: {e}")
            continue

        channel = data.get("channel", data.get("uploader", "Unknown"))
        # Clean channel name (remove spaces)
        channel = re.sub(r"[^a-zA-Z0-9]", "", channel)
        title = data.get("title", "Unknown")
        video_id = data.get("id", "")
        upload_date = data.get("upload_date", datetime.now().strftime("%Y%m%d"))
        view_count = data.get("view_count", 0)

        transcript_name = build_transcript_filename(channel, upload_date, title)
        output_path = os.path.join(TRANSCRIPTS_DIR, transcript_name)

        print(f"  Downloading transcript: {transcript_name}")

        dl_cmd = [
            "yt-dlp",
            "--write-auto-sub",
            "--sub-lang", lang,
            "--skip-download",
            "-o", output_path,
            url,
        ]

        try:
            result = subprocess.run(dl_cmd, capture_output=True, text=True, timeout=60)

            expected_file = f"{output_path}.{lang}.vtt"
            filename = None
            if os.path.exists(expected_file):
                filename = os.path.basename(expected_file)
            else:
                for fname in os.listdir(TRANSCRIPTS_DIR):
                    if fname.startswith(transcript_name):
                        filename = fname
                        break

            if filename:
                print(f"    Success: {filename}")
                history.append({
                    "channel": channel,
                    "title": title,
                    "url": url,
                    "video_id": video_id,
                    "upload_date": upload_date,
                    "language": lang,
                    "view_count": view_count,
                    "filename": filename,
                    "downloaded_at": datetime.now().isoformat(),
                })
                save_history(history)
            else:
                stderr_lines = result.stderr.strip().split("\n") if result.stderr else []
                error_msg = stderr_lines[-1] if stderr_lines else "No subtitle file created"
                print(f"    Failed: {error_msg}")

        except subprocess.TimeoutExpired:
            print(f"    Timeout downloading transcript")
        except Exception as e:
            print(f"    Error: {e}")


def sanitize_title(title):
    """Convert title to CamelCase with no spaces or special characters."""
    title = unicodedata.normalize("NFKD", title)
    title = re.sub(r"[''`]", "", title)
    words = re.findall(r"[a-zA-Z0-9]+", title)
    result = "".join(w.capitalize() for w in words)
    return result[:60] if result else "Untitled"


def format_date_for_filename(upload_date_str):
    """Convert YYYYMMDD to MMDDYYYY."""
    if len(upload_date_str) == 8:
        return upload_date_str[4:6] + upload_date_str[6:8] + upload_date_str[:4]
    return upload_date_str


def format_date_display(upload_date_str):
    """Convert YYYYMMDD to MM/DD/YYYY for display."""
    if len(upload_date_str) == 8:
        return f"{upload_date_str[4:6]}/{upload_date_str[6:8]}/{upload_date_str[:4]}"
    return upload_date_str


def build_transcript_filename(channel, upload_date, title):
    """Build the transcript filename: ChannelName-MMDDYYYY-TitleCamelCase."""
    date_fmt = format_date_for_filename(upload_date)
    title_fmt = sanitize_title(title)
    return f"{channel}-{date_fmt}-{title_fmt}"


def generate_html(all_videos):
    """Generate the HTML page with video table."""
    all_videos.sort(key=lambda v: v.get("upload_date", ""), reverse=True)

    # Filter out already-downloaded videos
    history = load_history()
    downloaded_ids = {entry["video_id"] for entry in history if entry.get("video_id")}
    all_videos = [v for v in all_videos if v["video_id"] not in downloaded_ids]

    # Build unique channel list for filter dropdown
    channels_seen = []
    for v in all_videos:
        if v["channel"] not in channels_seen:
            channels_seen.append(v["channel"])
    channel_options = "".join(
        f'<option value="{html.escape(ch)}">{html.escape(ch)}</option>'
        for ch in sorted(channels_seen)
    )

    rows = ""
    for i, v in enumerate(all_videos):
        video_id = html.escape(v["video_id"])
        embed_url = f"https://www.youtube.com/embed/{video_id}"
        video_url = html.escape(v["url"])
        title_text = html.escape(v["title"])
        channel_text = html.escape(v["channel"])
        display_date = format_date_display(v["upload_date"])
        views = f"{v.get('view_count', 0):,}" if v.get("view_count") else "N/A"
        lang = html.escape(v.get("language", DEFAULT_LANG))
        transcript_name = html.escape(
            build_transcript_filename(v["channel"], v["upload_date"], v["title"])
        )

        rows += f"""
        <tr id="row-{i}" data-channel="{channel_text}">
          <td class="video-cell">
            <iframe width="280" height="158" src="{embed_url}"
                    frameborder="0" allowfullscreen loading="lazy"></iframe>
          </td>
          <td class="channel-cell">{channel_text}</td>
          <td class="title-cell">
            <a href="{video_url}" target="_blank">{title_text}</a>
          </td>
          <td class="date-cell">{display_date}</td>
          <td class="lang-cell">{lang}</td>
          <td class="views-cell">{views}</td>
          <td class="action-cell">
            <button class="dl-btn" id="btn-{i}"
                    data-url="{video_url}"
                    data-name="{transcript_name}"
                    data-lang="{lang}"
                    data-channel="{channel_text}"
                    data-title="{title_text}"
                    data-date="{html.escape(v['upload_date'])}"
                    data-views="{v.get('view_count', 0)}"
                    data-videoid="{video_id}"
                    onclick="downloadTranscript(this)">
              Download Transcript
            </button>
            <span class="status" id="status-{i}"></span>
          </td>
        </tr>"""

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>YouTube Follow - Latest Videos (Last {DAYS_BACK} days)</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: #0f0f0f;
    color: #f1f1f1;
    padding: 20px;
  }}
  h1 {{
    text-align: center;
    margin-bottom: 8px;
    font-size: 24px;
    color: #ff4444;
  }}
  .subtitle {{
    text-align: center;
    color: #aaa;
    margin-bottom: 20px;
    font-size: 14px;
  }}
  /* Tabs */
  .tabs {{
    display: flex;
    justify-content: center;
    gap: 4px;
    margin-bottom: 16px;
  }}
  .tab-btn {{
    background: #1a1a1a;
    color: #aaa;
    border: 1px solid #333;
    padding: 10px 24px;
    border-radius: 6px 6px 0 0;
    cursor: pointer;
    font-size: 14px;
    font-weight: 500;
    transition: background 0.15s, color 0.15s;
  }}
  .tab-btn:hover {{ color: #f1f1f1; }}
  .tab-btn.active {{
    background: #0f0f0f;
    color: #ff4444;
    border-bottom-color: #0f0f0f;
    font-weight: 600;
  }}
  .tab-content {{ display: none; }}
  .tab-content.active {{ display: block; }}
  .table-container {{
    overflow-x: auto;
    overflow-y: auto;
    max-height: 80vh;
    border: 1px solid #333;
    border-radius: 8px;
  }}
  table {{
    width: 100%;
    border-collapse: collapse;
    min-width: 1100px;
  }}
  thead th {{
    position: sticky;
    top: 0;
    background: #1a1a1a;
    padding: 12px 10px;
    text-align: left;
    font-weight: 600;
    font-size: 13px;
    text-transform: uppercase;
    color: #aaa;
    border-bottom: 2px solid #333;
    z-index: 10;
  }}
  tbody tr {{
    border-bottom: 1px solid #222;
    transition: background 0.15s;
  }}
  tbody tr:hover {{
    background: #1a1a1a;
  }}
  td {{
    padding: 10px;
    vertical-align: middle;
    font-size: 14px;
  }}
  .video-cell {{ width: 290px; min-width: 290px; }}
  .video-cell iframe {{ border-radius: 6px; }}
  .channel-cell {{ font-weight: 600; color: #ff8888; white-space: nowrap; }}
  .title-cell {{ max-width: 300px; }}
  .title-cell a {{
    color: #f1f1f1;
    text-decoration: none;
    line-height: 1.4;
  }}
  .title-cell a:hover {{ color: #ff4444; text-decoration: underline; }}
  .date-cell {{ white-space: nowrap; color: #aaa; }}
  .lang-cell {{ white-space: nowrap; color: #aaa; text-transform: uppercase; }}
  .views-cell {{ white-space: nowrap; color: #aaa; }}
  .action-cell {{ white-space: nowrap; min-width: 180px; }}
  .dl-btn {{
    background: #cc0000;
    color: white;
    border: none;
    padding: 8px 14px;
    border-radius: 4px;
    cursor: pointer;
    font-size: 13px;
    font-weight: 500;
    transition: background 0.15s;
  }}
  .dl-btn:hover {{ background: #ff2222; }}
  .dl-btn:disabled {{
    background: #555;
    cursor: not-allowed;
  }}
  .status {{
    display: block;
    margin-top: 4px;
    font-size: 12px;
  }}
  .status.loading {{ color: #ffcc00; }}
  .status.success {{ color: #44ff44; }}
  .status.error {{ color: #ff4444; }}
  .stats {{
    text-align: center;
    margin-bottom: 15px;
    color: #888;
    font-size: 13px;
  }}
  .empty-msg {{
    text-align: center;
    color: #666;
    padding: 40px;
    font-size: 15px;
  }}
  /* Channel filter */
  .filter-bar {{
    display: flex;
    justify-content: center;
    align-items: center;
    gap: 10px;
    margin-bottom: 16px;
  }}
  .filter-bar label {{
    color: #aaa;
    font-size: 14px;
  }}
  .filter-bar select {{
    background: #1a1a1a;
    color: #f1f1f1;
    border: 1px solid #333;
    padding: 8px 12px;
    border-radius: 4px;
    font-size: 14px;
    cursor: pointer;
    min-width: 200px;
  }}
  .filter-bar select:hover {{ border-color: #555; }}
  .filter-bar select:focus {{ outline: none; border-color: #ff4444; }}
</style>
</head>
<body>

<h1>YouTube Follow - Latest Videos</h1>
<p class="subtitle">Videos published in the last {DAYS_BACK} days &bull; Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}</p>

<div class="filter-bar">
  <label for="channel-filter">Channel:</label>
  <select id="channel-filter" onchange="filterChannel()">
    <option value="">All Channels</option>
    {channel_options}
  </select>
</div>

<div class="tabs">
  <button class="tab-btn active" onclick="switchTab('latest')">Latest Videos</button>
  <button class="tab-btn" onclick="switchTab('downloaded')">Downloaded</button>
</div>

<!-- Latest Videos Tab -->
<div id="tab-latest" class="tab-content active">
<p class="stats">{len(all_videos)} videos found</p>
<div class="table-container">
<table>
<thead>
  <tr>
    <th>Video</th>
    <th>Channel</th>
    <th>Title</th>
    <th>Date</th>
    <th>Language</th>
    <th>Views</th>
    <th>Transcript</th>
  </tr>
</thead>
<tbody>
{rows}
</tbody>
</table>
</div>
</div>

<!-- Downloaded Tab -->
<div id="tab-downloaded" class="tab-content">
<p class="stats" id="downloaded-stats">Loading...</p>
<div class="table-container">
<table>
<thead>
  <tr>
    <th>Video</th>
    <th>Channel</th>
    <th>Title</th>
    <th>Date</th>
    <th>Language</th>
    <th>Views</th>
  </tr>
</thead>
<tbody id="downloaded-body">
</tbody>
</table>
</div>
</div>

<script>
let currentTab = 'latest';

function switchTab(tab) {{
  currentTab = tab;
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
  document.getElementById('tab-' + tab).classList.add('active');
  document.querySelector('[onclick*=\"' + tab + '\"]').classList.add('active');
  if (tab === 'downloaded') loadDownloaded();
  else filterChannel();
}}

function filterChannel() {{
  const sel = document.getElementById('channel-filter').value;
  // Filter latest videos tab
  document.querySelectorAll('#tab-latest tbody tr').forEach(row => {{
    if (!sel || row.getAttribute('data-channel') === sel) {{
      row.style.display = '';
    }} else {{
      row.style.display = 'none';
    }}
  }});
  // Filter downloaded tab (if loaded)
  document.querySelectorAll('#downloaded-body tr').forEach(row => {{
    const ch = row.getAttribute('data-channel');
    if (!ch || !sel || ch === sel) {{
      row.style.display = '';
    }} else {{
      row.style.display = 'none';
    }}
  }});
}}

function formatDate(d) {{
  if (d && d.length === 8) return d.slice(4,6) + '/' + d.slice(6,8) + '/' + d.slice(0,4);
  return d || '';
}}

function escapeHtml(t) {{
  const d = document.createElement('div');
  d.textContent = t;
  return d.innerHTML;
}}

async function loadDownloaded() {{
  const tbody = document.getElementById('downloaded-body');
  const stats = document.getElementById('downloaded-stats');
  try {{
    const resp = await fetch('/history');
    const data = await resp.json();
    stats.textContent = data.length + ' transcripts downloaded';
    if (data.length === 0) {{
      tbody.innerHTML = '<tr><td colspan="6" class="empty-msg">No transcripts downloaded yet.</td></tr>';
      return;
    }}
    tbody.innerHTML = data.map(v => `
      <tr data-channel="${{escapeHtml(v.channel || '')}}">
        <td class="video-cell">
          <iframe width="280" height="158" src="https://www.youtube.com/embed/${{escapeHtml(v.video_id)}}"
                  frameborder="0" allowfullscreen loading="lazy"></iframe>
        </td>
        <td class="channel-cell">${{escapeHtml(v.channel || '')}}</td>
        <td class="title-cell">
          <a href="${{escapeHtml(v.url || '')}}" target="_blank">${{escapeHtml(v.title || '')}}</a>
        </td>
        <td class="date-cell">${{formatDate(v.upload_date)}}</td>
        <td class="lang-cell">${{escapeHtml(v.language || '')}}</td>
        <td class="views-cell">${{v.view_count ? Number(v.view_count).toLocaleString() : 'N/A'}}</td>
      </tr>
    `).join('');
    filterChannel();
  }} catch (e) {{
    stats.textContent = 'Could not load history';
    tbody.innerHTML = '<tr><td colspan="6" class="empty-msg">Server not running.</td></tr>';
  }}
}}

async function downloadTranscript(btn) {{
  const videoUrl = btn.getAttribute('data-url');
  const transcriptName = btn.getAttribute('data-name');
  const lang = btn.getAttribute('data-lang');
  const idx = btn.id.replace('btn-', '');
  const status = document.getElementById('status-' + idx);

  btn.disabled = true;
  status.className = 'status loading';
  status.textContent = 'Downloading...';

  try {{
    const resp = await fetch('/download-transcript', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{
        url: videoUrl,
        name: transcriptName,
        lang: lang,
        channel: btn.getAttribute('data-channel'),
        title: btn.getAttribute('data-title'),
        upload_date: btn.getAttribute('data-date'),
        view_count: parseInt(btn.getAttribute('data-views')) || 0,
        video_id: btn.getAttribute('data-videoid')
      }})
    }});
    const data = await resp.json();
    if (data.success) {{
      status.className = 'status success';
      status.textContent = 'Downloaded: ' + data.filename;
      setTimeout(() => {{ btn.closest('tr').style.display = 'none'; }}, 1500);
    }} else {{
      status.className = 'status error';
      status.textContent = 'Error: ' + data.error;
      btn.disabled = false;
    }}
  }} catch (e) {{
    status.className = 'status error';
    status.textContent = 'Server not running. Start with: python3 youtube_follow.py --serve';
    btn.disabled = false;
  }}
}}
</script>

</body>
</html>"""
    return html_content


class RequestHandler(BaseHTTPRequestHandler):
    """HTTP request handler for serving HTML and handling transcript downloads."""

    def log_message(self, format, *args):
        print(f"  [{self.log_date_time_string()}] {format % args}")

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            with open(OUTPUT_HTML, "rb") as f:
                self.wfile.write(f.read())
        elif self.path == "/history":
            history = load_history()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps(history, ensure_ascii=False).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/download-transcript":
            content_len = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_len)
            data = json.loads(body)

            video_url = data.get("url", "")
            transcript_name = data.get("name", "transcript")
            sub_lang = data.get("lang", DEFAULT_LANG)

            os.makedirs(TRANSCRIPTS_DIR, exist_ok=True)
            output_path = os.path.join(TRANSCRIPTS_DIR, transcript_name)

            cmd = [
                "yt-dlp",
                "--write-auto-sub",
                "--sub-lang", sub_lang,
                "--skip-download",
                "-o", output_path,
                video_url,
            ]

            print(f"  Downloading transcript: {transcript_name}")

            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)

                expected_file = f"{output_path}.{sub_lang}.vtt"
                if os.path.exists(expected_file):
                    filename = os.path.basename(expected_file)
                    response = {"success": True, "filename": filename}
                    print(f"    Success: {filename}")
                else:
                    # Check if any subtitle file was created with this prefix
                    filename = None
                    for fname in os.listdir(TRANSCRIPTS_DIR):
                        if fname.startswith(transcript_name):
                            filename = fname
                            break
                    if filename:
                        response = {"success": True, "filename": filename}
                        print(f"    Success: {filename}")
                    else:
                        stderr_lines = result.stderr.strip().split("\n") if result.stderr else []
                        error_msg = stderr_lines[-1] if stderr_lines else "No subtitle file created"
                        response = {"success": False, "error": error_msg}
                        print(f"    Failed: {error_msg}")

            except subprocess.TimeoutExpired:
                response = {"success": False, "error": "Download timed out"}
            except Exception as e:
                response = {"success": False, "error": str(e)}

            # Record successful download in history
            if response.get("success"):
                history = load_history()
                history.append({
                    "channel": data.get("channel", ""),
                    "title": data.get("title", ""),
                    "url": video_url,
                    "video_id": data.get("video_id", ""),
                    "upload_date": data.get("upload_date", ""),
                    "language": sub_lang,
                    "view_count": data.get("view_count", 0),
                    "filename": response["filename"],
                    "downloaded_at": datetime.now().isoformat(),
                })
                save_history(history)

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(response).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


def main():
    print(f"\nYouTube Follow - Fetching videos from the last {DAYS_BACK} days\n")

    if not os.path.exists(FOLLOW_FILE):
        print(f"Error: {FOLLOW_FILE} not found!")
        sys.exit(1)

    channels, individual_videos = read_follow_file(FOLLOW_FILE)
    print(f"Found {len(channels)} channels and {len(individual_videos)} individual videos in {FOLLOW_FILE}:\n")
    for ch in channels:
        print(f"  - {get_channel_name(ch['url'])} (lang: {ch['language']})")
    for v in individual_videos:
        print(f"  - [video] {v['url']} (lang: {v['language']})")
    print()

    # --serve: skip fetching, just serve existing HTML
    if "--serve" in sys.argv:
        if not os.path.exists(OUTPUT_HTML):
            print(f"Error: {OUTPUT_HTML} not found! Run without --serve first.")
            sys.exit(1)
        print(f"Starting server at http://localhost:{SERVER_PORT}")
        server = HTTPServer(("localhost", SERVER_PORT), RequestHandler)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nServer stopped.")
        return

    # Download transcripts for individual videos
    if individual_videos:
        print("Processing individual videos...\n")
        download_individual_videos(individual_videos)
        print()

    # Fetch videos from all channels
    all_videos = []
    for ch in channels:
        videos = fetch_recent_videos(ch["url"], DAYS_BACK, ch["language"])
        all_videos.extend(videos)

    print(f"\nTotal: {len(all_videos)} videos found\n")

    # Generate HTML even if no videos found (shows empty state)
    html_content = generate_html(all_videos)
    with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
        f.write(html_content)
    print(f"Generated {OUTPUT_HTML}")

    # Create transcripts directory
    os.makedirs(TRANSCRIPTS_DIR, exist_ok=True)

    # Start server
    print(f"\nStarting server at http://localhost:{SERVER_PORT}")
    print("Press Ctrl+C to stop.\n")
    server = HTTPServer(("localhost", SERVER_PORT), RequestHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")


if __name__ == "__main__":
    main()
