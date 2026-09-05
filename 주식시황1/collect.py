"""
channel.txt에 나열된 유튜브 채널들의 최신 영상 메타데이터/자막을 수집해
data/YYYY-MM-DD.json 으로 저장한다. (yt-dlp 필요: pip install yt-dlp)

사용법:
    python collect.py [--since-days 1]

주의:
    클라우드 환경(특히 서버 IP)에서는 유튜브가 기본(web_safari/mweb 등) 클라이언트에
    HTTP 429 / "Sign in to confirm you're not a bot"으로 응답해 영상 상세 정보를
    가져오지 못하는 경우가 많다. 이를 우회하기 위해 `--extractor-args
    youtube:player_client=android`를 사용한다. android 클라이언트는 자동 생성
    자막(automatic_captions)의 서명된 timedtext URL을 정상적으로 돌려주므로,
    해당 URL을 직접 요청해 자막 전문(vtt)을 받아 텍스트로 정리한다.
    그래도 막힐 경우를 대비해 web(+--ignore-no-formats-error) 클라이언트로 재시도한다.
"""
import argparse
import html
import json
import re
import subprocess
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CHANNEL_FILE = BASE_DIR / "channel.txt"
DATA_DIR = BASE_DIR / "data"

KST = timezone(timedelta(hours=9))

PLAYER_CLIENTS = ["android", "web"]


def read_channels():
    channels = []
    for line in CHANNEL_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        channels.append(line)
    return channels


def run_yt_dlp_json(args, extra_args=()):
    proc = subprocess.run(
        [sys.executable, "-m", "yt_dlp", *extra_args, *args],
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


def list_recent_videos(channel_url, max_videos=15):
    url = channel_url.rstrip("/") + "/videos"
    return run_yt_dlp_json(
        ["--flat-playlist", "--dump-json", "--playlist-end", str(max_videos), url]
    )


def fetch_video_detail(video_url):
    """영상 상세 메타데이터를 여러 player_client로 순차 시도해 가져온다."""
    for client in PLAYER_CLIENTS:
        videos = run_yt_dlp_json(
            ["--skip-download", "--dump-json", "--ignore-no-formats-error", video_url],
            extra_args=["--extractor-args", f"youtube:player_client={client}"],
        )
        if videos:
            return videos[0]
    return None


def vtt_to_text(vtt_text):
    """자동 생성 vtt 자막을 사람이 읽을 수 있는 텍스트로 정리한다.

    유튜브 자동자막은 롤링(스크롤) 방식이라 인접 큐 사이에 줄이 중복된다.
    직전에 추가한 줄과 동일한 줄은 건너뛰어 중복을 제거한다.
    """
    lines = []
    last_line = None
    for raw_line in vtt_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("WEBVTT") or line.startswith("Kind:") or line.startswith("Language:"):
            continue
        if "-->" in line:
            continue
        line = re.sub(r"<[^>]+>", "", line)
        line = html.unescape(line).strip()
        if not line or line == last_line:
            continue
        lines.append(line)
        last_line = line
    return " ".join(lines)


def fetch_transcript(detail):
    captions = detail.get("automatic_captions") or {}
    for lang in ("ko", "ko-orig"):
        entries = captions.get(lang) or []
        vtt_url = next((e.get("url") for e in entries if e.get("ext") == "vtt"), None)
        if not vtt_url:
            continue
        try:
            with urllib.request.urlopen(vtt_url, timeout=20) as resp:
                vtt_text = resp.read().decode("utf-8", errors="ignore")
            text = vtt_to_text(vtt_text)
            if text:
                return text
        except Exception as exc:
            print(f"  [warn] 자막 다운로드 실패: {exc}", file=sys.stderr)
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

            transcript = fetch_transcript(detail)
            status = "ok" if transcript else "no_transcript"
            print(f"  [{status}] {detail.get('title')}")

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
    ok_count = sum(1 for r in results if r["transcript"])
    print(f"\n총 {len(results)}개 영상 수집 완료 (자막 확보 {ok_count}건) -> {out_path}")
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--since-days", type=int, default=1)
    parser.add_argument("--max-videos-per-channel", type=int, default=15)
    args = parser.parse_args()
    collect(since_days=args.since_days, max_videos_per_channel=args.max_videos_per_channel)
