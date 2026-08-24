"""
channel.txt에 나열된 유튜브 채널들의 최신 영상 메타데이터/자막을 수집해
data/YYYY-MM-DD.json 으로 저장한다. (yt-dlp 필요: pip install yt-dlp)

사용법:
    python collect.py [--since-days 1]

주의:
    실행 환경(특히 클라우드/서버 IP)에 따라 유튜브의 봇 차단(HTTP 429,
    "Sign in to confirm you're not a bot")으로 개별 영상 상세 정보/자막
    수집이 실패할 수 있다. 이 경우 --cookies-from-browser 로 로그인된
    브라우저 쿠키를 넘기거나, 로컬(개인 PC) 환경에서 실행해야 한다.

    상세 정보(yt-dlp) 수집이 막히더라도 채널의 RSS 피드
    (https://www.youtube.com/feeds/videos.xml?channel_id=...)는 별도
    엔드포인트라 대체로 차단되지 않는다. 이 스크립트는 상세 수집이
    실패하면 RSS로 제목/게시일/설명을 대신 채우되, 자막은 RSS에 없으므로
    caption_collected=False 로 표시한다. 자막이 없으면 format.md 규칙상
    핵심요약은 채우지 않아야 한다.
"""
import argparse
import json
import subprocess
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CHANNEL_FILE = BASE_DIR / "channel.txt"
DATA_DIR = BASE_DIR / "data"

KST = timezone(timedelta(hours=9))
ATOM_NS = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}


def read_channels():
    channels = []
    for line in CHANNEL_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        channels.append(line)
    return channels


def run_yt_dlp_json(args):
    proc = subprocess.run(
        [sys.executable, "-m", "yt_dlp", *args],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        print(f"  [warn] yt-dlp 실패: {' '.join(args)}\n{proc.stderr.strip()[-500:]}", file=sys.stderr)
        return []
    videos = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            videos.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return videos


def list_recent_videos(channel_url, max_videos=15):
    url = channel_url.rstrip("/") + "/videos"
    return run_yt_dlp_json(
        ["--flat-playlist", "--dump-json", "--playlist-end", str(max_videos), url]
    )


def fetch_video_detail(video_url):
    """영상 상세 메타데이터 + 자막(가능하면)을 가져온다."""
    videos = run_yt_dlp_json(
        [
            "--skip-download",
            "--dump-json",
            "--write-auto-sub",
            "--sub-lang", "ko",
            "--sub-format", "vtt",
            "--no-write-sub",
            video_url,
        ]
    )
    return videos[0] if videos else None


def fetch_channel_rss(channel_id):
    """채널 RSS 피드에서 최신 영상의 제목/게시일/설명을 가져온다.

    yt-dlp 상세 수집이 봇 차단으로 실패해도 이 엔드포인트는 대체로 열려
    있어, 최소한 제목/링크/게시일은 채울 수 있다.
    """
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    try:
        req = urllib.request.Request(url, headers={"Accept-Language": "ko-KR,ko;q=0.9"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            root = ET.fromstring(resp.read())
    except Exception as exc:
        print(f"  [warn] RSS 수집 실패({channel_id}): {exc}", file=sys.stderr)
        return {}

    by_id = {}
    for entry in root.findall("a:entry", ATOM_NS):
        vid = entry.findtext("yt:videoId", default="", namespaces=ATOM_NS)
        if not vid:
            continue
        title = entry.findtext("a:title", default="", namespaces=ATOM_NS)
        published = entry.findtext("a:published", default="", namespaces=ATOM_NS)
        group = entry.find("media:group", {"media": "http://search.yahoo.com/mrss/"})
        description = ""
        if group is not None:
            description = group.findtext(
                "media:description", default="", namespaces={"media": "http://search.yahoo.com/mrss/"}
            )
        by_id[vid] = {"title": title, "published": published, "description": description}
    return by_id


def collect(since_days=1, max_videos_per_channel=15):
    channels = read_channels()
    cutoff = datetime.now(KST) - timedelta(days=since_days)
    results = []

    for channel_url in channels:
        print(f"[channel] {channel_url}")
        recent = list_recent_videos(channel_url, max_videos_per_channel)
        channel_id = next((e.get("playlist_channel_id") for e in recent if e.get("playlist_channel_id")), None)
        rss_by_id = fetch_channel_rss(channel_id) if channel_id else {}

        for entry in recent:
            video_id = entry.get("id")
            video_url = entry.get("url") or f"https://www.youtube.com/watch?v={video_id}"
            detail = fetch_video_detail(video_url)
            rss_entry = rss_by_id.get(video_id)
            caption_collected = bool(detail)

            if not detail and not rss_entry:
                print(f"  [skip] 상세/RSS 정보 모두 수집 실패: {video_url}")
                continue
            if not detail:
                print(f"  [fallback] RSS로 대체 수집: {video_url}")

            if detail:
                upload_date = detail.get("upload_date")  # YYYYMMDD
                title = detail.get("title")
                uploader = detail.get("uploader")
                description = detail.get("description")
                duration = detail.get("duration")
            else:
                published = rss_entry["published"]  # ISO-8601, e.g. 2026-08-24T08:05:40+00:00
                uploaded_at = datetime.fromisoformat(published).astimezone(KST)
                upload_date = uploaded_at.strftime("%Y%m%d")
                title = rss_entry["title"]
                uploader = None
                description = rss_entry["description"]
                duration = None

            if upload_date:
                uploaded_at = datetime.strptime(upload_date, "%Y%m%d").replace(tzinfo=KST)
                if uploaded_at < cutoff:
                    continue

            results.append(
                {
                    "channel": channel_url,
                    "video_id": video_id,
                    "title": title,
                    "url": video_url,
                    "upload_date": upload_date,
                    "uploader": uploader,
                    "description": description,
                    "duration": duration,
                    "caption_collected": caption_collected,
                }
            )

    DATA_DIR.mkdir(exist_ok=True)
    out_path = DATA_DIR / f"{datetime.now(KST).strftime('%Y-%m-%d')}.json"
    out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n총 {len(results)}개 영상 수집 완료 -> {out_path}")
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--since-days", type=int, default=1)
    parser.add_argument("--max-videos-per-channel", type=int, default=15)
    args = parser.parse_args()
    collect(since_days=args.since_days, max_videos_per_channel=args.max_videos_per_channel)
