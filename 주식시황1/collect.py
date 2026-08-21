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
"""
import argparse
import json
import subprocess
import sys
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
    """영상 상세 메타데이터 + 자막(가능하면)을 가져온다.

    기본 web 플레이어로는 클라우드 환경에서 "Sign in to confirm you're not
    a bot" (HTTP 429)로 거의 항상 차단된다. mweb 클라이언트 +
    --ignore-no-formats-error 조합을 쓰면 (다운로드용 포맷은 없지만) 제목/
    설명/업로드일 등 메타데이터는 대체로 가져올 수 있다. 다만 자막은 mweb도
    PO 토큰이 없으면 대부분 제공되지 않는다 (아래에서 별도로 재시도).
    """
    videos = run_yt_dlp_json(
        [
            "--skip-download",
            "--dump-json",
            "--extractor-args", "youtube:player_client=mweb",
            "--ignore-no-formats-error",
            video_url,
        ]
    )
    detail = videos[0] if videos else None
    if detail is None:
        return None

    if not detail.get("automatic_captions") and not detail.get("subtitles"):
        sub_videos = run_yt_dlp_json(
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
        if sub_videos and sub_videos[0].get("requested_subtitles"):
            detail["requested_subtitles"] = sub_videos[0]["requested_subtitles"]

    return detail


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
