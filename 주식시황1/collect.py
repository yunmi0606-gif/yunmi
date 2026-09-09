"""
channel.txt에 나열된 유튜브 채널들의 최신 영상 메타데이터/자막을 수집해
data/YYYY-MM-DD.json 으로 저장한다. (yt-dlp 필요: pip install yt-dlp)

사용법:
    python collect.py [--since-days 1]

주의:
    유튜브의 봇 차단(HTTP 429, "Sign in to confirm you're not a bot")을
    피하려면 --extractor-args "youtube:player_client=android" 조합을 쓴다.
    web 클라이언트는 거의 항상 429/PO Token 요구로 막히지만, android
    플레이어 API는 클라우드 IP에서도 안정적으로 메타데이터와 자동자막
    (auto captions)을 내려받을 수 있었다(2026-09-09 확인). 그래도 유튜브
    쪽 정책 변경으로 다시 막힐 수 있으니, 실패 시 --cookies-from-browser로
    로그인 쿠키를 넘기거나 로컬(개인 PC) 환경에서 실행해야 한다.
"""
import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CHANNEL_FILE = BASE_DIR / "channel.txt"
DATA_DIR = BASE_DIR / "data"

KST = timezone(timedelta(hours=9))

PLAYER_CLIENT = "android"
EXTRACTOR_ARGS = ["--extractor-args", f"youtube:player_client={PLAYER_CLIENT}"]


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
    """영상 상세 메타데이터를 가져온다 (android 클라이언트 사용)."""
    videos = run_yt_dlp_json(
        [
            "--skip-download",
            "--dump-json",
            *EXTRACTOR_ARGS,
            video_url,
        ]
    )
    return videos[0] if videos else None


def vtt_to_text(vtt_path: Path) -> str:
    """자동자막 vtt 파일을 중복 제거된 평문 스크립트로 변환한다."""
    raw = vtt_path.read_text(encoding="utf-8", errors="ignore")
    lines = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("WEBVTT") or line.startswith("Kind:") or line.startswith("Language:"):
            continue
        if "-->" in line:
            continue
        if re.match(r"^\d+$", line):
            continue
        # 인라인 타임스탬프/태그 제거 (자동자막 특유의 <00:00:01.234><c> 형태)
        line = re.sub(r"<[^>]+>", "", line)
        line = line.strip()
        if not line:
            continue
        if not lines or lines[-1] != line:
            lines.append(line)
    return "\n".join(lines)


def fetch_transcript(video_id, video_url, tmp_dir: Path):
    """한국어 자동자막을 내려받아 평문으로 반환한다. 실패 시 None."""
    tmp_dir.mkdir(parents=True, exist_ok=True)
    out_tmpl = str(tmp_dir / "%(id)s.%(ext)s")
    subprocess.run(
        [
            sys.executable, "-m", "yt_dlp",
            "--write-auto-sub", "--sub-lang", "ko", "--sub-format", "vtt",
            "--skip-download",
            *EXTRACTOR_ARGS,
            "-o", out_tmpl,
            video_url,
        ],
        capture_output=True,
        text=True,
    )
    vtt_path = tmp_dir / f"{video_id}.ko.vtt"
    if not vtt_path.exists():
        return None
    text = vtt_to_text(vtt_path)
    return text or None


def collect(since_days=1, max_videos_per_channel=15, with_transcript=True):
    channels = read_channels()
    cutoff = datetime.now(KST) - timedelta(days=since_days)
    results = []
    tmp_dir = DATA_DIR / "subs_tmp"

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
            if with_transcript:
                transcript = fetch_transcript(detail.get("id"), video_url, tmp_dir)
                status = "성공" if transcript else "실패"
                print(f"  [sub] {detail.get('title')[:40]}... 자막 수집 {status}")

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
    print(f"\n총 {len(results)}개 영상 수집 완료 -> {out_path}")
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--since-days", type=int, default=1)
    parser.add_argument("--max-videos-per-channel", type=int, default=15)
    parser.add_argument("--no-transcript", action="store_true")
    args = parser.parse_args()
    collect(
        since_days=args.since_days,
        max_videos_per_channel=args.max_videos_per_channel,
        with_transcript=not args.no_transcript,
    )
