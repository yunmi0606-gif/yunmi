"""
channel.txt에 나열된 유튜브 채널들의 최신 영상 메타데이터/자막을 수집해
data/YYYY-MM-DD.json 으로 저장한다. (yt-dlp 필요: pip install yt-dlp)

사용법:
    python collect.py [--since-days 1] [--max-videos-per-channel 20]

동작 방식 (2026-08-26 기준):
    1. `yt-dlp --dump-json <채널>/videos` 로 채널의 최근 영상들을 "완전한" 메타데이터
       (제목/업로드일/automatic_captions 서명 URL 등)와 함께 가져온다. 이 방식은
       --flat-playlist 보다 느리지만, 개별 영상 페이지를 다시 요청하지 않아도 되므로
       유튜브의 봇 차단(HTTP 429 / "Sign in to confirm you're not a bot")을 상대적으로
       덜 유발한다.
    2. 각 영상의 automatic_captions(자동 생성 자막)에서 한국어(ko) vtt 트랙의 서명된
       timedtext URL을 뽑아, requests/curl로 "직접" 요청해 자막 텍스트를 가져온다.
       (yt-dlp의 자막 다운로드 서브커맨드를 다시 호출하면 봇 차단에 걸리기 쉬움)
    3. 자막 요청은 실패 시 지수 백오프로 재시도하고, 성공 여부와 무관하게 요청 사이에
       텀을 둬서 레이트리밋을 피한다.

주의:
    실행 환경(특히 클라우드/서버 IP)에 따라 위 방식도 실패할 수 있다. 이 경우
    --cookies-from-browser 로 로그인된 브라우저 쿠키를 넘기거나, 로컬(개인 PC)
    환경에서 실행해야 한다. 자막을 구하지 못한 영상은 title/업로드일 등 메타데이터만
    남기고 transcript는 빈 문자열로 저장한다 (요약 시 "자막 없음"으로 처리).
"""
import argparse
import json
import re
import subprocess
import sys
import time
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


def read_channels():
    channels = []
    for line in CHANNEL_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        channels.append(line)
    return channels


def list_channel_videos(channel_url, max_videos=20):
    """채널의 최근 영상들을 완전한 메타데이터(automatic_captions 포함)와 함께 가져온다."""
    url = channel_url.rstrip("/") + "/videos"
    proc = subprocess.run(
        [
            sys.executable, "-m", "yt_dlp",
            "--skip-download", "--dump-json",
            "--playlist-end", str(max_videos),
            "--ignore-errors",
            url,
        ],
        capture_output=True,
        text=True,
        timeout=600,
    )
    if proc.stderr:
        print(f"  [warn] yt-dlp stderr (일부): {proc.stderr.strip()[-500:]}", file=sys.stderr)
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


def get_ko_sub_url(video):
    for key in ("ko", "ko-orig"):
        for fmt in video.get("automatic_captions", {}).get(key, []):
            if fmt.get("ext") == "vtt":
                return fmt.get("url")
    return None


def collect(since_days=1, max_videos_per_channel=20):
    channels = read_channels()
    cutoff = datetime.now(KST) - timedelta(days=since_days)
    results = []

    for channel_url in channels:
        print(f"[channel] {channel_url}")
        recent = list_channel_videos(channel_url, max_videos_per_channel)
        for video in recent:
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

    DATA_DIR.mkdir(exist_ok=True)
    out_path = DATA_DIR / f"{datetime.now(KST).strftime('%Y-%m-%d')}.json"
    out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n총 {len(results)}개 영상 수집 완료 -> {out_path}")
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--since-days", type=int, default=1)
    parser.add_argument("--max-videos-per-channel", type=int, default=20)
    args = parser.parse_args()
    collect(since_days=args.since_days, max_videos_per_channel=args.max_videos_per_channel)
