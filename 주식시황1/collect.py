"""
channel.txt에 나열된 유튜브 채널들의 최근 영상 메타데이터(+가능하면 자막)를 수집해
data/YYYY-MM-DD.json 으로 저장한다. (yt-dlp 필요: pip install yt-dlp)

사용법:
    python collect.py [--since-days 1] [--max-videos-per-channel 15]

동작 방식 (여러 세션에서 검증된 방식을 통합):
    1) `yt-dlp --dump-json <채널>/videos` 로 채널의 최근 영상들을 완전한 메타데이터
       (제목/업로드일/automatic_captions 서명 URL 포함)와 함께 가져온다.
    2) 위 방식이 봇 차단(HTTP 429) 등으로 실패하면, channel_id를 확인해 유튜브 RSS
       (videos.xml)를 직접 요청하는 방식으로 폴백한다. RSS는 봇 차단과 무관하게
       항상 동작하므로 최소한 제목/링크/업로드 시각은 수집할 수 있다.
    3) dump-json으로 automatic_captions 서명 URL을 확보한 영상은, 그 URL을
       curl로 직접 요청해 한국어(ko) 자동자막(vtt)을 가져온다. 실패 시 지수
       백오프로 재시도한다(yt-dlp 서브커맨드를 다시 호출하면 봇 차단에 걸리기 쉬움).

주의:
    실행 환경(특히 클라우드/서버 IP)에 따라 위 방식들도 실패할 수 있다. 이 경우
    --cookies-from-browser 로 로그인된 브라우저 쿠키를 넘기거나, 로컬(개인 PC)
    환경에서 실행해야 한다. 자막을 구하지 못한 영상은 title/업로드일 등 메타데이터만
    남기고 transcript는 빈 문자열로 저장한다 (요약 시 "자막 없음"으로 처리, 추측·창작 금지).
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
ATOM_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
}


def read_channels():
    channels = []
    for line in CHANNEL_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        channels.append(line)
    return channels


def run_yt_dlp_json(args, timeout=600):
    proc = subprocess.run(
        [sys.executable, "-m", "yt_dlp", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
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


def list_channel_videos(channel_url, max_videos=15):
    """채널의 최근 영상들을 완전한 메타데이터(automatic_captions 포함)와 함께 가져온다."""
    url = channel_url.rstrip("/") + "/videos"
    return run_yt_dlp_json(
        [
            "--skip-download", "--dump-json",
            "--playlist-end", str(max_videos),
            "--ignore-errors",
            "--ignore-no-formats-error",
            "--extractor-args", "youtube:player_client=web",
            url,
        ]
    )


def resolve_channel_id(channel_url):
    entries = run_yt_dlp_json(
        ["--flat-playlist", "--dump-json", "--playlist-end", "1", channel_url.rstrip("/") + "/videos"]
    )
    if not entries:
        return None
    return entries[0].get("channel_id") or entries[0].get("playlist_channel_id")


def fetch_rss_entries(channel_id):
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
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


def clean_vtt(text):
    """자동 생성 vtt 자막에서 타임스탬프/중복 롤링 라인을 제거해 평문으로 만든다."""
    out = []
    seen = set()
    for line in text.splitlines():
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


def fetch_transcript(sub_url, retries=3, backoff=10):
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


def collect_via_dump_json(channel_url, cutoff, max_videos):
    videos = list_channel_videos(channel_url, max_videos)
    results = []
    for video in videos:
        upload_date = video.get("upload_date")  # YYYYMMDD
        if upload_date:
            uploaded_at = datetime.strptime(upload_date, "%Y%m%d").replace(tzinfo=KST)
            if uploaded_at < cutoff:
                continue

        video_id = video.get("id")
        video_url = video.get("webpage_url") or f"https://www.youtube.com/watch?v={video_id}"
        sub_url = get_ko_sub_url(video)
        transcript = fetch_transcript(sub_url)
        print(f"  [{'sub-ok' if transcript else 'no-sub'}] {video.get('title')}")

        results.append(
            {
                "channel": channel_url,
                "video_id": video_id,
                "title": video.get("title"),
                "url": video_url,
                "upload_date": upload_date,
                "uploader": video.get("uploader"),
                "description": video.get("description"),
                "duration": video.get("duration"),
                "transcript": transcript,
            }
        )
        time.sleep(8)
    return results


def collect_via_rss(channel_url, cutoff_utc, max_videos):
    channel_id = resolve_channel_id(channel_url)
    if not channel_id:
        print(f"  [skip] channel_id 확인 실패: {channel_url}")
        return []
    try:
        entries = fetch_rss_entries(channel_id)[:max_videos]
    except Exception as exc:  # noqa: BLE001
        print(f"  [skip] RSS 수집 실패: {exc}")
        return []

    results = []
    for entry in entries:
        video_id = entry["video_id"]
        if not video_id:
            continue
        published = entry["published"]
        if published:
            uploaded_at = datetime.fromisoformat(published.replace("Z", "+00:00"))
            if uploaded_at < cutoff_utc:
                continue
        results.append(
            {
                "channel": channel_url,
                "video_id": video_id,
                "title": entry["title"],
                "url": f"https://www.youtube.com/watch?v={video_id}",
                "upload_date": None,
                "published_utc": published,
                "uploader": None,
                "description": None,
                "duration": None,
                "transcript": "",
            }
        )
    return results


def collect(since_days=1, max_videos_per_channel=15):
    channels = read_channels()
    cutoff = datetime.now(KST) - timedelta(days=since_days)
    cutoff_utc = datetime.now(timezone.utc) - timedelta(days=since_days)
    results = []

    for channel_url in channels:
        print(f"[channel] {channel_url}")
        entries = collect_via_dump_json(channel_url, cutoff, max_videos_per_channel)
        if not entries:
            print(f"  [fallback] dump-json 실패 -> RSS 폴백 시도")
            entries = collect_via_rss(channel_url, cutoff_utc, max_videos_per_channel)
        results.extend(entries)

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
