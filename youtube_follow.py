#!/usr/bin/env python3
"""
YouTube Follow - Latest Videos Tracker

Reads follow.json, fetches recent videos from YouTube channels,
generates an interactive HTML page with embedded videos, transcript download,
and auto-generates HTML summaries via Claude CLI.

Usage:
    python3 youtube_follow.py                    # Fetch videos, generate HTML, start server
    python3 youtube_follow.py --serve            # Just start server (skip fetching)
    python3 youtube_follow.py --generate-only    # Fetch + generate HTML, then exit (no server)
    python3 youtube_follow.py --generate-summaries  # Generate summaries for all transcripts missing one
"""

import subprocess
import json
import os
import sys
import re
import html
import urllib.request
import xml.etree.ElementTree as ET
import threading
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
import unicodedata
import time

# ============================================================
# CONFIGURATION (defaults, overridden by follow.json)
# ============================================================
FOLLOW_FILE = "follow.json"
OUTPUT_HTML = "latest_videos.html"
HISTORY_FILE = os.path.join("transcripts", "history.json")

# These get overridden from follow.json in main()
DAYS_BACK = 10
TRANSCRIPTS_DIR = "transcripts"
SERVER_PORT = 8081
DEFAULT_LANG = "en"
API_BASE = ""

# Global config dict, loaded from follow.json
CONFIG = {}
# ============================================================

SUMMARY_PROMPT = """Read this VTT transcript and create a comprehensive HTML summary.

Title: {title}
Channel: {channel}
Date: {date}

Requirements:
- Standalone HTML file with dark theme (background #0f0f0f, text #f1f1f1)
- Include: title, channel, date at the top
- Table of contents with anchor links
- Break content into logical sections with clear headings
- Use highlight boxes for key points
- Include a glossary if technical terms are used
- Responsive design
- Do NOT include any <script> tags

CRITICAL: Your response must start with <!DOCTYPE html> and contain ONLY the HTML.
Do NOT wrap it in markdown fences, tool calls, artifacts, or any other wrapper.
Do NOT use any tools. Just output the raw HTML directly as plain text.
"""


def resolve_root(file_mapping):
    """Detect which platform we're on and return the correct root path.

    Checks paths in order: docker (/data), dxp8800 (/volume3/cloud), mac (/Volumes/cloud).
    """
    check_order = [
        ("docker", file_mapping.get("docker", "/data")),
        ("dxp8800", file_mapping.get("dxp8800", "/volume3/cloud")),
        ("msi_edgexpert", file_mapping.get("msi_edgexpert", "/Volumes/cloud")),
        ("mac", file_mapping.get("mac", "/Volumes/cloud")),
    ]
    for platform, path in check_order:
        if os.path.isdir(path):
            print(f"  Platform detected: {platform} (root: {path})")
            return path
    # Fallback to current directory
    print("  Warning: No known root found, using current directory")
    return os.getcwd()


def load_config(filepath):
    """Load follow.json and return the config dict."""
    with open(filepath, "r", encoding="utf-8") as f:
        return json.load(f)


def read_follow_json(filepath):
    """Read follow.json and return channels list, videos list, and config dict.

    Each channel/video entry includes url, language, html_summary_path.
    """
    config = load_config(filepath)

    channels = []
    for ch in config.get("channels", []):
        channels.append({
            "url": ch["url"],
            "language": ch.get("language", config.get("default_language", "en")),
            "html_summary_path": ch.get("html_summary_path", ""),
        })

    videos = []
    for v in config.get("videos", []):
        videos.append({
            "url": v["url"],
            "language": v.get("language", config.get("default_language", "en")),
            "html_summary_path": v.get("html_summary_path", ""),
            "title": v.get("title", ""),
        })

    return channels, videos, config


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
    """Download transcripts for individual video URLs from follow.json.

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
                    "html_summary_path": entry.get("html_summary_path", ""),
                })
                save_history(history)

                # Trigger summary generation in background
                if entry.get("html_summary_path"):
                    transcript_path = os.path.join(TRANSCRIPTS_DIR, filename)
                    threading.Thread(
                        target=generate_summary,
                        args=(transcript_path, channel, title, upload_date, CONFIG),
                        kwargs={"html_summary_path": entry["html_summary_path"]},
                        daemon=True,
                    ).start()
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


def find_channel_summary_path(channel_name, config):
    """Find the html_summary_path for a channel by matching its name."""
    for ch in config.get("channels", []):
        if get_channel_name(ch["url"]) == channel_name:
            return ch.get("html_summary_path", "")
    for v in config.get("videos", []):
        # For individual videos, we can't match by channel name easily
        pass
    return ""


def generate_summary(transcript_path, channel_name, title, upload_date, config,
                     html_summary_path=None):
    """Generate an HTML summary from a transcript using Claude CLI.

    Runs claude -p with the transcript content piped in.
    Writes the resulting HTML to the appropriate summary directory.
    Updates history.json with the summary path.
    """
    if not html_summary_path:
        html_summary_path = find_channel_summary_path(channel_name, config)
    if not html_summary_path:
        print(f"  [Summary] No html_summary_path for {channel_name}, skipping")
        return

    # Resolve to absolute path
    root = resolve_root(config.get("file_mapping", {}))
    summary_dir = os.path.join(root, html_summary_path)
    os.makedirs(summary_dir, exist_ok=True)

    # Build summary filename from transcript filename
    transcript_basename = os.path.basename(transcript_path)
    # Remove language and .vtt extension: foo.en.vtt -> foo.html
    summary_name = re.sub(r"\.\w+\.vtt$", ".html", transcript_basename)
    summary_path = os.path.join(summary_dir, summary_name)

    if os.path.exists(summary_path):
        print(f"  [Summary] Already exists: {summary_path}")
        return

    # Read transcript content
    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            transcript_content = f.read()
    except Exception as e:
        print(f"  [Summary] Error reading transcript: {e}")
        return

    # Truncate very large transcripts to avoid exceeding context window
    MAX_TRANSCRIPT_CHARS = 300000  # ~300KB
    if len(transcript_content) > MAX_TRANSCRIPT_CHARS:
        print(f"  [Summary] Transcript too large ({len(transcript_content)} chars), truncating to {MAX_TRANSCRIPT_CHARS}")
        transcript_content = transcript_content[:MAX_TRANSCRIPT_CHARS]

    date_display = format_date_display(upload_date)
    prompt = SUMMARY_PROMPT.format(
        title=title,
        channel=channel_name,
        date=date_display,
    )

    print(f"  [Summary] Generating summary for: {title}")
    print(f"  [Summary] Output: {summary_path}")

    try:
        result = subprocess.run(
            ["claude", "-p", prompt, "--output-format", "text",
             "--tools", ""],
            input=transcript_content,
            capture_output=True,
            text=True,
            timeout=600,
        )
        if result.returncode == 0 and result.stdout.strip():
            output = result.stdout.strip()
            # Extract HTML if wrapped in tool calls or artifacts
            if not (output.startswith("<!") or output.startswith("<html")):
                # Try to extract <!DOCTYPE html>...</html> from wrapped output
                match = re.search(r'(<!DOCTYPE html>.*?</html>)', output,
                                  re.DOTALL | re.IGNORECASE)
                if match:
                    output = match.group(1)
                    print(f"  [Summary] Extracted HTML from wrapped output")
                else:
                    print(f"  [Summary] Claude CLI returned non-HTML output, skipping")
                    print(f"  [Summary] First 200 chars: {output[:200]}")
                    return
            with open(summary_path, "w", encoding="utf-8") as f:
                f.write(output)
            print(f"  [Summary] Generated: {summary_name}")

            # Update history.json with summary info
            _update_history_summary(transcript_path, summary_path, config)

            # Update index pages
            update_indexes(config)
        else:
            stderr = result.stderr.strip() if result.stderr else "No output"
            print(f"  [Summary] Claude CLI failed: {stderr}")
    except FileNotFoundError:
        print("  [Summary] Claude CLI not found. Install with: npm install -g @anthropic-ai/claude-code")
    except subprocess.TimeoutExpired:
        print("  [Summary] Claude CLI timed out (600s)")
    except Exception as e:
        print(f"  [Summary] Error: {e}")


def _update_history_summary(transcript_path, summary_path, config):
    """Update history.json entry with summary_path info."""
    history = load_history()
    transcript_basename = os.path.basename(transcript_path)

    for entry in history:
        if entry.get("filename") == transcript_basename:
            # Store relative summary path (relative to root)
            root = resolve_root(config.get("file_mapping", {}))
            if summary_path.startswith(root):
                entry["summary_path"] = summary_path[len(root):].lstrip("/")
            else:
                entry["summary_path"] = summary_path
            entry["summary_generated_at"] = datetime.now().isoformat()
            break

    save_history(history)


def update_indexes(config):
    """Generate global and per-channel index pages listing all summaries."""
    root = resolve_root(config.get("file_mapping", {}))
    nginx_base = config.get("file_mapping", {}).get("nginx_base", "")
    history = load_history()

    # Filter to entries with summaries
    summarized = [e for e in history if e.get("summary_path")]
    if not summarized:
        return

    # Global index
    script_dir = os.path.dirname(os.path.abspath(__file__))
    global_index_path = os.path.join(script_dir, "summaries_index.html")
    _write_index_html(
        global_index_path,
        "YouTube Summaries - All Channels",
        summarized,
        root,
        nginx_base,
    )
    print(f"  [Index] Updated global index: {global_index_path}")

    # Per-channel indexes
    channels_paths = {}
    for ch in config.get("channels", []):
        name = get_channel_name(ch["url"])
        channels_paths[name] = ch.get("html_summary_path", "")

    for channel_name, rel_path in channels_paths.items():
        if not rel_path:
            continue
        channel_dir = os.path.join(root, rel_path)
        if not os.path.isdir(channel_dir):
            continue
        channel_entries = [e for e in summarized if e.get("channel") == channel_name]
        if not channel_entries:
            continue
        index_path = os.path.join(channel_dir, "index.html")
        _write_index_html(
            index_path,
            f"YouTube Summaries - {channel_name}",
            channel_entries,
            root,
            nginx_base,
        )
        print(f"  [Index] Updated {channel_name} index: {index_path}")


def _write_index_html(path, title, entries, root, nginx_base):
    """Write a dark-themed index HTML page listing summary links."""
    entries_sorted = sorted(entries, key=lambda e: e.get("upload_date", ""), reverse=True)

    rows = ""
    for e in entries_sorted:
        summary_rel = e.get("summary_path", "")
        if nginx_base and summary_rel:
            summary_url = f"{nginx_base}/{summary_rel}"
        else:
            summary_url = summary_rel
        channel_text = html.escape(e.get("channel", ""))
        title_text = html.escape(e.get("title", ""))
        date_text = format_date_display(e.get("upload_date", ""))
        video_url = html.escape(e.get("url", ""))
        rows += f"""
        <tr>
          <td class="channel-cell">{channel_text}</td>
          <td class="title-cell"><a href="{html.escape(summary_url)}">{title_text}</a></td>
          <td class="date-cell">{date_text}</td>
          <td class="link-cell"><a href="{video_url}" target="_blank">YouTube</a></td>
        </tr>"""

    index_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{html.escape(title)}</title>
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
  .table-container {{
    overflow-x: auto;
    border: 1px solid #333;
    border-radius: 8px;
  }}
  table {{
    width: 100%;
    border-collapse: collapse;
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
  }}
  tbody tr {{ border-bottom: 1px solid #222; }}
  tbody tr:hover {{ background: #1a1a1a; }}
  td {{ padding: 10px; vertical-align: middle; font-size: 14px; }}
  .channel-cell {{ font-weight: 600; color: #ff8888; white-space: nowrap; }}
  .title-cell a {{ color: #f1f1f1; text-decoration: none; }}
  .title-cell a:hover {{ color: #ff4444; text-decoration: underline; }}
  .date-cell {{ white-space: nowrap; color: #aaa; }}
  .link-cell a {{ color: #6688ff; text-decoration: none; }}
  .link-cell a:hover {{ text-decoration: underline; }}
</style>
</head>
<body>
<h1>{html.escape(title)}</h1>
<p class="subtitle">{len(entries_sorted)} summaries &bull; Updated {datetime.now().strftime('%Y-%m-%d %H:%M')}</p>
<div class="table-container">
<table>
<thead>
  <tr>
    <th>Channel</th>
    <th>Title (Summary Link)</th>
    <th>Date</th>
    <th>Video</th>
  </tr>
</thead>
<tbody>
{rows}
</tbody>
</table>
</div>
</body>
</html>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(index_html)


def generate_all_summaries(config):
    """Generate summaries for all history entries that don't have one yet.

    Processes sequentially to avoid overwhelming Claude CLI.
    """
    history = load_history()
    pending = [e for e in history if not e.get("summary_path")]
    if not pending:
        print("  All transcripts already have summaries.")
        return

    print(f"  Found {len(pending)} transcripts without summaries.\n")

    for i, entry in enumerate(pending, 1):
        filename = entry.get("filename", "")
        transcript_path = os.path.join(TRANSCRIPTS_DIR, filename)

        if not os.path.exists(transcript_path):
            print(f"  [{i}/{len(pending)}] Skipping (file missing): {filename}")
            continue

        channel = entry.get("channel", "")
        title = entry.get("title", "")
        upload_date = entry.get("upload_date", "")

        # Get html_summary_path from entry or look it up from config
        html_summary_path = entry.get("html_summary_path", "")
        if not html_summary_path:
            html_summary_path = find_channel_summary_path(channel, config)

        if not html_summary_path:
            print(f"  [{i}/{len(pending)}] Skipping (no summary path): {channel} - {title}")
            continue

        print(f"  [{i}/{len(pending)}] {channel} - {title}")
        generate_summary(
            transcript_path, channel, title, upload_date, config,
            html_summary_path=html_summary_path,
        )

    # Update indexes after all summaries are done
    update_indexes(config)
    print(f"\n  Done generating summaries.")


def generate_html(all_videos, config):
    """Generate the HTML page with video table."""
    all_videos.sort(key=lambda v: v.get("upload_date", ""), reverse=True)

    # Filter out already-downloaded videos
    history = load_history()
    downloaded_ids = {entry["video_id"] for entry in history if entry.get("video_id")}
    all_videos = [v for v in all_videos if v["video_id"] not in downloaded_ids]

    api_base = config.get("api_base", "")
    days_back = config.get("days_back", DAYS_BACK)

    # Collect all unique html_summary_path values for the download-video tab
    all_paths = []
    for ch in config.get("channels", []):
        p = ch.get("html_summary_path", "")
        if p and p not in all_paths:
            all_paths.append(p)
    for v in config.get("videos", []):
        p = v.get("html_summary_path", "")
        if p and p not in all_paths:
            all_paths.append(p)
    path_options = "".join(
        f'<option value="{html.escape(p)}">{html.escape(p)}</option>'
        for p in sorted(all_paths)
    )

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

    nginx_base = config.get("file_mapping", {}).get("nginx_base", "")

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>YouTube Follow - Latest Videos (Last {days_back} days)</title>
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
  .summary-link {{
    display: inline-block;
    margin-top: 4px;
    font-size: 12px;
    color: #6688ff;
    text-decoration: none;
  }}
  .summary-link:hover {{ text-decoration: underline; }}
  .summary-badge {{
    display: inline-block;
    background: #2a4a2a;
    color: #44ff44;
    font-size: 11px;
    padding: 2px 6px;
    border-radius: 3px;
    margin-top: 4px;
  }}
  /* Download Video form */
  .download-form {{
    max-width: 600px;
    margin: 20px auto;
    padding: 24px;
    background: #1a1a1a;
    border: 1px solid #333;
    border-radius: 8px;
  }}
  .download-form .form-group {{
    margin-bottom: 16px;
  }}
  .download-form label {{
    display: block;
    color: #aaa;
    font-size: 14px;
    margin-bottom: 6px;
  }}
  .download-form select,
  .download-form input[type="text"] {{
    width: 100%;
    background: #0f0f0f;
    color: #f1f1f1;
    border: 1px solid #333;
    padding: 10px 12px;
    border-radius: 4px;
    font-size: 14px;
  }}
  .download-form select:focus,
  .download-form input[type="text"]:focus {{
    outline: none;
    border-color: #ff4444;
  }}
  .download-form .submit-btn {{
    background: #cc0000;
    color: white;
    border: none;
    padding: 10px 24px;
    border-radius: 4px;
    cursor: pointer;
    font-size: 14px;
    font-weight: 500;
    transition: background 0.15s;
  }}
  .download-form .submit-btn:hover {{ background: #ff2222; }}
  .download-form .submit-btn:disabled {{
    background: #555;
    cursor: not-allowed;
  }}
  #custom-path-group {{
    display: none;
    margin-top: 8px;
  }}
</style>
</head>
<body>

<h1>YouTube Follow - Latest Videos</h1>
<p class="subtitle">Videos published in the last {days_back} days &bull; Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}</p>
<p style="text-align:center;margin-bottom:16px">
  <a href="{nginx_base}/GitHub/YOUTUBE/summaries_index.html"
     style="color:#6688ff;text-decoration:none;font-size:14px">
     View All Summaries Index
  </a>
</p>

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
  <button class="tab-btn" onclick="switchTab('download-video')">Download Video</button>
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
    <th>Summary</th>
  </tr>
</thead>
<tbody id="downloaded-body">
</tbody>
</table>
</div>
</div>

<!-- Download Video Tab -->
<div id="tab-download-video" class="tab-content">
<div class="download-form">
  <h2 style="color:#ff4444;margin-bottom:16px;font-size:18px;text-align:center">Download a Specific Video</h2>
  <div class="form-group">
    <label for="video-url-input">YouTube Video URL</label>
    <input type="text" id="video-url-input" placeholder="https://www.youtube.com/watch?v=...">
  </div>
  <div class="form-group">
    <label for="path-select">Summary Path</label>
    <select id="path-select" onchange="toggleCustomPath()">
      {path_options}
      <option value="__other__">Other (custom path)</option>
    </select>
    <div id="custom-path-group">
      <input type="text" id="custom-path-input" placeholder="e.g. GitHub/MYPROJECT/youtube/ChannelName">
    </div>
  </div>
  <div class="form-group">
    <label for="lang-select">Language</label>
    <select id="lang-select">
      <option value="en">English (en)</option>
      <option value="it">Italian (it)</option>
      <option value="es">Spanish (es)</option>
      <option value="fr">French (fr)</option>
      <option value="de">German (de)</option>
    </select>
  </div>
  <div style="text-align:center">
    <button class="submit-btn" id="download-video-btn" onclick="downloadVideo()">Download Video Transcript</button>
  </div>
  <div id="download-video-status" style="text-align:center;margin-top:12px"></div>
</div>
</div>

<script>
const API_BASE = '{api_base}';
const NGINX_BASE = '{nginx_base}';
let currentTab = 'latest';

function switchTab(tab) {{
  currentTab = tab;
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
  document.getElementById('tab-' + tab).classList.add('active');
  document.querySelector('[onclick*="' + tab + '"]').classList.add('active');
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
    const resp = await fetch(API_BASE + '/history');
    const data = await resp.json();
    stats.textContent = data.length + ' transcripts downloaded';
    if (data.length === 0) {{
      tbody.innerHTML = '<tr><td colspan="7" class="empty-msg">No transcripts downloaded yet.</td></tr>';
      return;
    }}
    tbody.innerHTML = data.map(v => {{
      let summaryCell = '<td class="action-cell">-</td>';
      if (v.summary_path) {{
        const summaryUrl = NGINX_BASE + '/' + v.summary_path;
        summaryCell = `<td class="action-cell"><a class="summary-link" href="${{summaryUrl}}" target="_blank" style="color:#44ff44;font-size:13px;font-weight:500">View Summary</a></td>`;
      }} else {{
        summaryCell = '<td class="action-cell"><span style="color:#666;font-size:12px">No summary</span></td>';
      }}
      return `
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
        ${{summaryCell}}
      </tr>
    `}}).join('');
    filterChannel();
  }} catch (e) {{
    stats.textContent = 'Could not load history';
    tbody.innerHTML = '<tr><td colspan="7" class="empty-msg">Server not running at ' + API_BASE + '</td></tr>';
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
    const resp = await fetch(API_BASE + '/download-transcript', {{
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
      if (data.summary_triggered) {{
        status.textContent += ' (summary generating... ~2-5 min)';
      }}
      setTimeout(() => {{ btn.closest('tr').style.display = 'none'; }}, 1500);
    }} else {{
      status.className = 'status error';
      status.textContent = 'Error: ' + data.error;
      btn.disabled = false;
    }}
  }} catch (e) {{
    status.className = 'status error';
    status.textContent = 'Server not running at ' + API_BASE;
    btn.disabled = false;
  }}
}}

function toggleCustomPath() {{
  const sel = document.getElementById('path-select');
  const customGroup = document.getElementById('custom-path-group');
  customGroup.style.display = sel.value === '__other__' ? 'block' : 'none';
}}

async function downloadVideo() {{
  const urlInput = document.getElementById('video-url-input');
  const pathSelect = document.getElementById('path-select');
  const customPathInput = document.getElementById('custom-path-input');
  const langSelect = document.getElementById('lang-select');
  const btn = document.getElementById('download-video-btn');
  const status = document.getElementById('download-video-status');

  const videoUrl = urlInput.value.trim();
  if (!videoUrl) {{
    status.innerHTML = '<span style="color:#ff4444">Please enter a YouTube URL</span>';
    return;
  }}

  let summaryPath = pathSelect.value;
  if (summaryPath === '__other__') {{
    summaryPath = customPathInput.value.trim();
    if (!summaryPath) {{
      status.innerHTML = '<span style="color:#ff4444">Please enter a custom path</span>';
      return;
    }}
  }}

  btn.disabled = true;
  status.innerHTML = '<span style="color:#ffcc00">Downloading transcript...</span>';

  try {{
    const resp = await fetch(API_BASE + '/download-video', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{
        url: videoUrl,
        html_summary_path: summaryPath,
        language: langSelect.value
      }})
    }});
    const data = await resp.json();
    if (data.success) {{
      let msg = 'Downloaded: ' + data.filename;
      if (data.summary_triggered) msg += ' (summary generating... ~2-5 min)';
      status.innerHTML = '<span style="color:#44ff44">' + escapeHtml(msg) + '</span>';
      urlInput.value = '';
    }} else {{
      status.innerHTML = '<span style="color:#ff4444">Error: ' + escapeHtml(data.error) + '</span>';
    }}
  }} catch (e) {{
    status.innerHTML = '<span style="color:#ff4444">Server not running at ' + API_BASE + '</span>';
  }}
  btn.disabled = false;
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
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(history, ensure_ascii=False).encode("utf-8"))
        elif self.path == "/summaries":
            history = load_history()
            summaries = [e for e in history if e.get("summary_path")]
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(summaries, ensure_ascii=False).encode("utf-8"))
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
            channel = data.get("channel", "")
            title = data.get("title", "")
            upload_date = data.get("upload_date", "")

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

            # Record successful download in history and trigger summary
            summary_triggered = False
            if response.get("success"):
                # Find html_summary_path for this channel
                html_summary_path = find_channel_summary_path(channel, CONFIG)

                history = load_history()
                history.append({
                    "channel": channel,
                    "title": title,
                    "url": video_url,
                    "video_id": data.get("video_id", ""),
                    "upload_date": upload_date,
                    "language": sub_lang,
                    "view_count": data.get("view_count", 0),
                    "filename": response["filename"],
                    "downloaded_at": datetime.now().isoformat(),
                    "html_summary_path": html_summary_path,
                })
                save_history(history)

                # Trigger summary generation in background
                if html_summary_path:
                    transcript_path = os.path.join(TRANSCRIPTS_DIR, response["filename"])
                    threading.Thread(
                        target=generate_summary,
                        args=(transcript_path, channel, title, upload_date, CONFIG),
                        kwargs={"html_summary_path": html_summary_path},
                        daemon=True,
                    ).start()
                    summary_triggered = True

                response["summary_triggered"] = summary_triggered

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(response).encode())

        elif self.path == "/download-video":
            content_len = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_len)
            data = json.loads(body)

            video_url = data.get("url", "")
            html_summary_path = data.get("html_summary_path", "")
            sub_lang = data.get("language", DEFAULT_LANG)

            # Fetch video metadata using yt-dlp
            try:
                meta_cmd = [
                    "yt-dlp", "--dump-json", "--no-download", video_url
                ]
                meta_result = subprocess.run(meta_cmd, capture_output=True, text=True, timeout=30)
                meta = json.loads(meta_result.stdout)
                title = meta.get("title", "UnknownTitle")
                channel = meta.get("channel", meta.get("uploader", "Unknown"))
                # Remove spaces from channel name
                channel = re.sub(r'[^a-zA-Z0-9]', '', channel)
                upload_date = meta.get("upload_date", datetime.now().strftime("%Y%m%d"))
                video_id = meta.get("id", "")
                view_count = meta.get("view_count", 0)
            except Exception as e:
                response = {"success": False, "error": f"Could not fetch video info: {e}"}
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps(response).encode())
                return

            transcript_name = build_transcript_filename(channel, upload_date, title)
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

            print(f"  [Download Video] Downloading transcript: {transcript_name}")

            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)

                expected_file = f"{output_path}.{sub_lang}.vtt"
                filename = None
                if os.path.exists(expected_file):
                    filename = os.path.basename(expected_file)
                else:
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

            # Record in history and trigger summary
            summary_triggered = False
            if response.get("success"):
                history = load_history()
                # Remove existing entry with same video_id to avoid duplicates
                history = [e for e in history if e.get("video_id") != video_id]
                history.append({
                    "channel": channel,
                    "title": title,
                    "url": video_url,
                    "video_id": video_id,
                    "upload_date": upload_date,
                    "language": sub_lang,
                    "view_count": view_count,
                    "filename": response["filename"],
                    "downloaded_at": datetime.now().isoformat(),
                    "html_summary_path": html_summary_path,
                })
                save_history(history)

                if html_summary_path:
                    transcript_path = os.path.join(TRANSCRIPTS_DIR, response["filename"])
                    threading.Thread(
                        target=generate_summary,
                        args=(transcript_path, channel, title, upload_date, CONFIG),
                        kwargs={"html_summary_path": html_summary_path},
                        daemon=True,
                    ).start()
                    summary_triggered = True

                response["summary_triggered"] = summary_triggered

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
    global DAYS_BACK, TRANSCRIPTS_DIR, SERVER_PORT, DEFAULT_LANG, API_BASE, CONFIG, HISTORY_FILE

    if not os.path.exists(FOLLOW_FILE):
        print(f"Error: {FOLLOW_FILE} not found!")
        sys.exit(1)

    # Load config from JSON
    channels, individual_videos, config = read_follow_json(FOLLOW_FILE)
    CONFIG = config

    # Override defaults from config
    DAYS_BACK = config.get("days_back", DAYS_BACK)
    TRANSCRIPTS_DIR = config.get("transcripts_dir", TRANSCRIPTS_DIR)
    SERVER_PORT = config.get("server_port", SERVER_PORT)
    DEFAULT_LANG = config.get("default_language", DEFAULT_LANG)
    API_BASE = config.get("api_base", "")
    HISTORY_FILE = os.path.join(TRANSCRIPTS_DIR, "history.json")

    print(f"\nYouTube Follow - Fetching videos from the last {DAYS_BACK} days\n")
    print(f"  Config: {FOLLOW_FILE}")
    print(f"  Server port: {SERVER_PORT}")
    print(f"  API base: {API_BASE}")
    resolve_root(config.get("file_mapping", {}))
    print()

    print(f"Found {len(channels)} channels and {len(individual_videos)} individual videos:\n")
    for ch in channels:
        print(f"  - {get_channel_name(ch['url'])} (lang: {ch['language']})")
    for v in individual_videos:
        print(f"  - [video] {v['url']} (lang: {v['language']})")
    print()

    # --generate-summaries: generate summaries for all existing transcripts
    if "--generate-summaries" in sys.argv:
        print("Generating summaries for existing transcripts...\n")
        generate_all_summaries(config)
        return

    # --serve: skip fetching, just serve existing HTML
    if "--serve" in sys.argv:
        if not os.path.exists(OUTPUT_HTML):
            print(f"Error: {OUTPUT_HTML} not found! Run without --serve first.")
            sys.exit(1)
        print(f"Starting server at http://0.0.0.0:{SERVER_PORT}")
        server = HTTPServer(("0.0.0.0", SERVER_PORT), RequestHandler)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nServer stopped.")
        return

    # --generate-only: fetch + generate HTML, then exit
    generate_only = "--generate-only" in sys.argv

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
    html_content = generate_html(all_videos, config)
    with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
        f.write(html_content)
    print(f"Generated {OUTPUT_HTML}")

    # Create transcripts directory
    os.makedirs(TRANSCRIPTS_DIR, exist_ok=True)

    # Update index pages if any summaries exist
    update_indexes(config)

    if generate_only:
        print("\n--generate-only: done.")
        return

    # Start server
    print(f"\nStarting server at http://0.0.0.0:{SERVER_PORT}")
    print("Press Ctrl+C to stop.\n")
    server = HTTPServer(("0.0.0.0", SERVER_PORT), RequestHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")


if __name__ == "__main__":
    main()
