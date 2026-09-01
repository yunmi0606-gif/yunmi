"""
channel.txt에 나열된 유튜브 채널들의 최근 영상 메타데이터(+가능하면 자막)를 수집해
data/YYYY-MM-DD.json 으로 저장한다. (yt-dlp 필요: pip install yt-dlp)

사용법:
    python collect.py [--since-days 1]

동작 방식:
    1) 채널 handle -> channel_id 를 yt-dlp(flat-playlist)로 확인한다.
    2) channel_id 로 유튜브 RSS(videos.xml)를 직접 요청해 최근 영상의
       제목/링크/게시시각(published, UTC)을 수집한다. RSS는 유튜브의
       봇 차단(HTTP 429 "Sign in to confirm you're not a bot")과 무관하게
       항상 동작하므로 클라우드 환경에서도 안정적이다.
    3) 각 영상에 대해 yt-dlp로 상세 메타데이터/자동자막 수집을 "시도"한다.
       클라우드 IP에서는 대부분 HTTP 429 또는 PO Token 요구로 실패하며,
       이 경우 실패로 기록하고 자막 없이 넘어간다(추측/창작 절대 금지).

주의:
    실행 환경(특히 클라우드/서버 IP)에 따라 유튜브의 봇 차단(HTTP 429,
    "Sign in to confirm you're not a bot")으로 개별 영상 상세 정보/자막
    수집이 실패할 수 있다. 이 경우 --cookies-from-browser 로 로그인된
    브라우저 쿠키를 넘기거나, 로컬(개인 PC) 환경에서 실행해야 한다.
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

ATOM_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}


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
    videos = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            videos.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if proc.returncode != 0 and not videos:
        print(f"  [warn] yt-dlp 실패: {' '.join(args)}\n{proc.stderr.strip()[-500:]}", file=sys.stderr)
    return videos


def resolve_channel_id(channel_url):
    entries = run_yt_dlp_json(
        ["--flat-playlist", "--dump-json", "--playlist-end", "1", channel_url.rstrip("/") + "/videos"]
    )
    if not entries:
        return None
    return entries[0].get("channel_id") or entries[0].get("playlist_channel_id")


def fetch_rss_entries(channel_id):
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = resp.read()
    root = ET.fromstring(raw)
    entries = []
    for entry in root.findall("atom:entry", ATOM_NS):
        video_id = entry.findtext("yt:videoId", default="", namespaces=ATOM_NS)
        title = entry.findtext("atom:title", default="", namespaces=ATOM_NS)
        published = entry.findtext("atom:published", default="", namespaces=ATOM_NS)
        entries.append({"video_id": video_id, "title": title, "published": published})
    return entries


def fetch_video_detail(video_url):
    """영상 상세 메타데이터 + 자막(가능하면)을 가져온다. 실패 시 None."""
    videos = run_yt_dlp_json(
        [
            "--skip-download",
            "--dump-json",
            "--write-auto-sub",
            "--sub-lang", "ko",
            "--sub-format", "vtt",
            "--no-write-sub",
            "--extractor-args", "youtube:player_client=mweb",
            video_url,
        ]
    )
    return videos[0] if videos else None


def collect(since_days=1, max_videos_per_channel=15):
    channels = read_channels()
    cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)
    results = []

    for channel_url in channels:
        print(f"[channel] {channel_url}")
        channel_id = resolve_channel_id(channel_url)
        if not channel_id:
            print(f"  [skip] channel_id 확인 실패: {channel_url}")
            continue

        try:
            rss_entries = fetch_rss_entries(channel_id)[:max_videos_per_channel]
        except Exception as exc:  # noqa: BLE001
            print(f"  [skip] RSS 수집 실패: {exc}")
            continue

        for entry in rss_entries:
            video_id = entry["video_id"]
            if not video_id:
                continue
            published = entry["published"]
            if published:
                uploaded_at = datetime.fromisoformat(published.replace("Z", "+00:00"))
                if uploaded_at < cutoff:
                    continue
            else:
                uploaded_at = None

            video_url = f"https://www.youtube.com/watch?v={video_id}"
            detail = fetch_video_detail(video_url)

            results.append(
                {
                    "channel": channel_url,
                    "video_id": video_id,
                    "title": entry["title"],
                    "url": video_url,
                    "published_utc": published,
                    "uploaded_kst": uploaded_at.astimezone(KST).isoformat() if uploaded_at else None,
                    "detail_collected": detail is not None,
                    "uploader": detail.get("uploader") if detail else None,
                    "description": detail.get("description") if detail else None,
                    "duration": detail.get("duration") if detail else None,
                    "has_auto_captions": bool(detail.get("automatic_captions")) if detail else False,
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
