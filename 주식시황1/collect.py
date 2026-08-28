"""
channel.txt에 나열된 유튜브 채널들의 최신 영상 메타데이터/자막을 수집해
data/YYYY-MM-DD.json 으로 저장한다. (yt-dlp 필요: pip install yt-dlp)

사용법:
    python collect.py [--since-days 1] [--max-videos-per-channel 15]

동작 방식 (2026-08-28 기준, 그동안의 시도를 통합):
    1. `--flat-playlist --dump-json <채널>/videos` 로 채널의 최근 영상 id 목록을
       가볍게 가져온다 (봇 차단을 거의 유발하지 않음).
    2. 채널 RSS 피드(videos.xml)에서 같은 영상들의 정확한 한국어 제목/게시일/설명을
       가져온다. RSS는 별도 엔드포인트라 대체로 차단되지 않는다.
    3. 영상별로 `--skip-download --dump-json <영상 URL>` 상세 조회를 시도해
       automatic_captions(자동 생성 자막)의 한국어(ko) vtt 서명 URL을 얻는다.
       이 요청은 유튜브 봇 차단(HTTP 429 / "Sign in to confirm you're not a bot")에
       걸리기 쉬우므로 실패해도 무시하고 RSS 메타데이터만으로 계속 진행한다.
    4. 자막 URL을 얻으면 curl로 timedtext를 직접 요청해 평문 자막(transcript)으로
       변환한다. 실패하면 transcript는 빈 문자열, caption_collected=False로 남긴다.

주의:
    자막을 구하지 못한 영상은 title/업로드일 등 메타데이터만 남기고 transcript는
    빈 문자열로 저장한다. format.md 규칙상 자막이 없는 영상은 핵심요약을 채우지
    않고 제목 기준 정보만 기록해야 한다 (추측/창작 금지).
"""
import argparse
import json
import re
import subprocess
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CHANNEL_FILE = BASE_DIR / "channel.txt"
DATA_DIR = BASE_DIR / "data"

KST = timezone(timedelta(hours=9))
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}
YT_NS = "http://www.youtube.com/xml/schemas/2015"
MEDIA_NS = "http://search.yahoo.com/mrss/"


def read_channels():
    channels = []
    for line in CHANNEL_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        channels.append(line)
    return channels


def run_yt_dlp_json(args, timeout=120):
    proc = subprocess.run(
        [sys.executable, "-m", "yt_dlp", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        print(f"  [warn] yt-dlp 실패: {' '.join(args)}\n{proc.stderr.strip()[-300:]}", file=sys.stderr)
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


def fetch_channel_rss(channel_id):
    """채널 RSS 피드에서 최신 영상의 한국어 제목/게시일/설명을 가져온다."""
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
        vid = entry.findtext(f"{{{YT_NS}}}videoId", default="")
        if not vid:
            continue
        title = entry.findtext("a:title", default="", namespaces=ATOM_NS)
        published = entry.findtext("a:published", default="", namespaces=ATOM_NS)
        group = entry.find(f"{{{MEDIA_NS}}}group")
        description = ""
        if group is not None:
            description = group.findtext(f"{{{MEDIA_NS}}}description", default="")
        by_id[vid] = {"title": title, "published": published, "description": description}
    return by_id


def fetch_video_detail(video_url, timeout=60):
    """영상 상세 메타데이터(automatic_captions 포함)를 시도한다. 실패하면 None."""
    try:
        videos = run_yt_dlp_json(
            ["--skip-download", "--dump-json", video_url], timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return None
    return videos[0] if videos else None


def clean_vtt(text):
    """자동 생성 vtt 자막에서 타임스탬프/중복 롤링 라인을 제거해 평문으로 만든다."""
    lines = text.splitlines()
    out = []
    seen = set()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith("WEBVTT") or line.startswith("Kind:") or line.startswith("Language:"):
            continue
        if "-->" in line:
            continue
        line = re.sub(r"<[^>]+>", "", line).strip()
        if not line or line in seen:
            continue
        seen.add(line)
        out.append(line)
    return "\n".join(out)


def get_ko_sub_url(video):
    for key in ("ko", "ko-orig"):
        for fmt in video.get("automatic_captions", {}).get(key, []):
            if fmt.get("ext") == "vtt":
                return fmt.get("url")
    return None


def fetch_transcript(sub_url, retries=2, backoff=8):
    """자막 timedtext URL을 직접 요청해 평문 자막을 반환한다. 실패 시 빈 문자열."""
    if not sub_url:
        return ""
    for attempt in range(retries):
        try:
            proc = subprocess.run(
                ["curl", "-sS", "-m", "30", "-A", USER_AGENT, sub_url],
                capture_output=True, text=True, timeout=40,
            )
            if proc.returncode == 0 and proc.stdout.strip().startswith("WEBVTT"):
                return clean_vtt(proc.stdout)
        except subprocess.TimeoutExpired:
            pass
        time.sleep(backoff * (attempt + 1))
    return ""


def collect(since_days=1, max_videos_per_channel=15):
    channels = read_channels()
    cutoff = datetime.now(KST) - timedelta(days=since_days)
    results = []

    for channel_url in channels:
        print(f"[channel] {channel_url}")
        recent = list_recent_videos(channel_url, max_videos_per_channel)
        channel_id = next((e.get("channel_id") or e.get("playlist_channel_id") for e in recent), None)
        rss_by_id = fetch_channel_rss(channel_id) if channel_id else {}

        for entry in recent:
            video_id = entry.get("id")
            video_url = entry.get("url") or f"https://www.youtube.com/watch?v={video_id}"
            rss_entry = rss_by_id.get(video_id)

            detail = None
            try:
                detail = fetch_video_detail(video_url)
            except Exception as exc:
                print(f"  [warn] 상세 수집 예외: {exc}", file=sys.stderr)

            if not detail and not rss_entry:
                print(f"  [skip] 상세/RSS 정보 모두 수집 실패: {video_url}")
                continue

            if detail:
                upload_date = detail.get("upload_date")
                title = detail.get("title")
                uploader = detail.get("uploader")
                description = detail.get("description")
                duration = detail.get("duration")
                sub_url = get_ko_sub_url(detail)
                transcript = fetch_transcript(sub_url)
            else:
                published = rss_entry["published"]
                uploaded_at = datetime.fromisoformat(published).astimezone(KST)
                upload_date = uploaded_at.strftime("%Y%m%d")
                title = rss_entry["title"]
                uploader = None
                description = rss_entry["description"]
                duration = None
                transcript = ""

            if rss_entry and rss_entry.get("title"):
                title = rss_entry["title"]

            if upload_date:
                uploaded_at = datetime.strptime(upload_date, "%Y%m%d").replace(tzinfo=KST)
                if uploaded_at < cutoff:
                    continue

            caption_collected = bool(transcript)
            print(f"  [{'sub-ok' if caption_collected else ('meta-only' if detail else 'rss-fallback')}] {title}")

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
                    "transcript": transcript,
                    "caption_collected": caption_collected,
                }
            )
            time.sleep(3)

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
