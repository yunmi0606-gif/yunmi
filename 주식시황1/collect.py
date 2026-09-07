"""
channel.txt에 나열된 유튜브 채널들의 최신 영상 메타데이터/자막을 수집해
data/YYYY-MM-DD.json 으로 저장한다. (yt-dlp 필요: pip install yt-dlp)

사용법:
    python collect.py [--since-days 1]

수집 방식 (2026-09-07 확인):
    클라우드 IP에서는 기본 yt-dlp 자막 다운로드(--write-auto-sub)가 유튜브의
    "Sign in to confirm you're not a bot" / PO Token 요구로 거의 항상 막힌다.
    반면 --extractor-args youtube:player_client=web 과 --ignore-no-formats-error
    조합으로 영상 메타데이터를 받으면 HTTP 429 경고가 떠도 automatic_captions의
    서명된 timedtext URL까지는 정상적으로 딸려온다. 이 timedtext URL을 yt-dlp가
    아닌 일반 HTTP 요청으로 직접 받아 자막(json3)을 파싱하면 봇 차단을 우회해
    실제 한국어 자동자막 전문을 수집할 수 있다.
"""
import argparse
import json
import subprocess
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CHANNEL_FILE = BASE_DIR / "channel.txt"
DATA_DIR = BASE_DIR / "data"

KST = timezone(timedelta(hours=9))


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
        timeout=90,
    )
    if proc.returncode != 0 and not proc.stdout.strip():
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
    """영상 상세 메타데이터를 가져온다 (자막 URL 포함, 자막 본문은 별도 요청)."""
    videos = run_yt_dlp_json(
        [
            "--skip-download",
            "--dump-json",
            "--extractor-args", "youtube:player_client=web",
            "--ignore-no-formats-error",
            video_url,
        ]
    )
    return videos[0] if videos else None


def fetch_caption_text(caption_url, timeout=30):
    """automatic_captions의 timedtext URL(json3)을 직접 요청해 평문으로 변환한다."""
    req = urllib.request.Request(caption_url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    lines = []
    for event in data.get("events", []):
        for seg in event.get("segs") or []:
            text = seg.get("utf8")
            if text:
                lines.append(text)
    return "".join(lines)


def extract_ko_caption_url(detail, lang="ko"):
    tracks = (detail.get("automatic_captions") or {}).get(lang)
    if not tracks:
        return None
    for track in tracks:
        if track.get("ext") == "json3":
            return track.get("url")
    return None


def collect(since_days=1, max_videos_per_channel=15):
    channels = read_channels()
    cutoff = datetime.now(KST) - timedelta(days=since_days)
    results = []

    for channel_url in channels:
        print(f"[channel] {channel_url}")
        recent = list_recent_videos(channel_url, max_videos_per_channel)
        for entry in recent:
            video_url = entry.get("url") or f"https://www.youtube.com/watch?v={entry.get('id')}"
            detail = fetch_video_detail(video_url)
            if not detail:
                print(f"  [skip] 상세 정보 수집 실패: {video_url}")
                continue

            upload_date = detail.get("upload_date")  # YYYYMMDD
            if upload_date:
                uploaded_at = datetime.strptime(upload_date, "%Y%m%d").replace(tzinfo=KST)
                if uploaded_at < cutoff:
                    continue

            transcript = None
            caption_url = extract_ko_caption_url(detail)
            if caption_url:
                try:
                    transcript = fetch_caption_text(caption_url)
                except Exception as exc:
                    print(f"  [warn] 자막 다운로드 실패: {video_url}: {exc}", file=sys.stderr)

            results.append(
                {
                    "channel": channel_url,
                    "video_id": detail.get("id"),
                    "title": detail.get("title"),
                    "url": video_url,
                    "upload_date": upload_date,
                    "uploader": detail.get("uploader"),
                    "description": detail.get("description"),
                    "duration": detail.get("duration"),
                    "transcript": transcript,
                }
            )

    DATA_DIR.mkdir(exist_ok=True)
    out_path = DATA_DIR / f"{datetime.now(KST).strftime('%Y-%m-%d')}.json"
    out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    ok = sum(1 for r in results if r["transcript"])
    print(f"\n총 {len(results)}개 영상 수집 완료 (자막 {ok}건) -> {out_path}")
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--since-days", type=int, default=1)
    parser.add_argument("--max-videos-per-channel", type=int, default=15)
    args = parser.parse_args()
    collect(since_days=args.since_days, max_videos_per_channel=args.max_videos_per_channel)
