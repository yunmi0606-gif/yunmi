"""
channel.txt에 나열된 유튜브 채널들의 최신 영상 메타데이터/자막을 수집해
data/YYYY-MM-DD.json 으로 저장한다. (yt-dlp 필요: pip install yt-dlp)

사용법:
    python collect.py [--since-days 1]

동작 방식:
    1) 채널 핸들 -> channel_id 해석 (yt-dlp) 후 채널 RSS(videos.xml)를 직접
       파싱해 최신 영상의 제목/링크/게시시각을 가져온다. 이 방식은 유튜브의
       봇 차단(HTTP 429 등)과 무관하게 항상 동작한다.
    2) 각 영상에 대해 yt-dlp로 상세 메타데이터(설명 등)와 자동자막을
       추가로 시도한다. 클라우드 IP에서는 "Sign in to confirm you're not
       a bot" / PO Token 요구로 실패할 수 있으며, 이 경우 1)에서 얻은
       제목/링크/게시시각만 결과에 남는다.

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

ATOM_NS = {"atom": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}


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


def resolve_channel_id(channel_url):
    proc = subprocess.run(
        [sys.executable, "-m", "yt_dlp", "--dump-single-json", "--playlist-items", "0", channel_url],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        print(f"  [warn] channel_id 해석 실패: {channel_url}\n{proc.stderr.strip()[-500:]}", file=sys.stderr)
        return None
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    return data.get("channel_id")


def list_recent_videos_via_rss(channel_id):
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        xml_bytes = resp.read()
    root = ET.fromstring(xml_bytes)
    entries = []
    for entry in root.findall("atom:entry", ATOM_NS):
        video_id = entry.findtext("yt:videoId", default="", namespaces=ATOM_NS)
        title = entry.findtext("atom:title", default="", namespaces=ATOM_NS)
        published = entry.findtext("atom:published", default="", namespaces=ATOM_NS)
        entries.append(
            {
                "id": video_id,
                "title": title,
                "url": f"https://www.youtube.com/watch?v={video_id}",
                "published": published,
            }
        )
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
            "--ignore-no-formats-error",
            video_url,
        ]
    )
    return videos[0] if videos else None


def collect(since_days=1, max_videos_per_channel=15):
    channels = read_channels()
    cutoff = datetime.now(KST) - timedelta(days=since_days)
    results = []

    for channel_url in channels:
        print(f"[channel] {channel_url}")
        channel_id = resolve_channel_id(channel_url)
        if not channel_id:
            print(f"  [skip] channel_id 해석 실패: {channel_url}")
            continue

        try:
            entries = list_recent_videos_via_rss(channel_id)
        except Exception as exc:  # noqa: BLE001
            print(f"  [skip] RSS 수집 실패: {channel_url} ({exc})")
            continue

        for entry in entries[:max_videos_per_channel]:
            published = entry.get("published")
            uploaded_at = None
            if published:
                uploaded_at = datetime.fromisoformat(published.replace("Z", "+00:00")).astimezone(KST)
                if uploaded_at < cutoff:
                    continue

            video_url = entry["url"]
            detail = fetch_video_detail(video_url)

            results.append(
                {
                    "channel": channel_url,
                    "video_id": entry.get("id"),
                    "title": (detail or {}).get("title") or entry.get("title"),
                    "url": video_url,
                    "upload_date": uploaded_at.strftime("%Y-%m-%d") if uploaded_at else None,
                    "upload_time_kst": uploaded_at.strftime("%Y-%m-%d %H:%M") if uploaded_at else None,
                    "uploader": (detail or {}).get("uploader"),
                    "description": (detail or {}).get("description"),
                    "duration": (detail or {}).get("duration"),
                    "detail_collected": detail is not None,
                }
            )

    DATA_DIR.mkdir(exist_ok=True)
    out_path = DATA_DIR / f"{datetime.now(KST).strftime('%Y-%m-%d')}.json"
    out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    success = sum(1 for r in results if r["detail_collected"])
    print(f"\n총 {len(results)}개 영상 수집 완료 (상세/자막 성공 {success}건) -> {out_path}")
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--since-days", type=int, default=1)
    parser.add_argument("--max-videos-per-channel", type=int, default=15)
    args = parser.parse_args()
    collect(since_days=args.since_days, max_videos_per_channel=args.max_videos_per_channel)
