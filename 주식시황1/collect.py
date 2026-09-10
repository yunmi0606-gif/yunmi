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
    (auto captions)을 내려받을 수 있었다(2026-09-09 확인).

    다만 2026-09-10에 확인된 바로는, 같은 IP로 짧은 시간에 요청을 몰아서
    보내면(예: 같은 날 전체 수집을 두 번 연달아 실행) android 클라이언트도
    광범위하게 차단당할 수 있다. 이를 줄이기 위해 요청 사이에 PACE_DELAY
    만큼 쉬어가고(사람이 훑어보는 것처럼), 하루 범위(since_days)를 벗어난
    영상을 만나면 해당 채널은 더 볼 것 없다고 보고 바로 다음 채널로
    넘어간다(업로드 목록이 최신순이라는 전제). 그래도 막히면
    --cookies-from-browser로 로그인 쿠키를 넘기거나 로컬(개인 PC)
    환경에서 실행해야 한다. 같은 날 이 스크립트를 반복 실행하지 않는 것도
    차단을 피하는 데 도움이 된다.
"""
import argparse
import json
import random
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

PLAYER_CLIENT = "android"
EXTRACTOR_ARGS = ["--extractor-args", f"youtube:player_client={PLAYER_CLIENT}"]

# 요청 사이 기본 간격(초). 매 yt-dlp 호출 전에 이만큼(+지터) 쉬어서 봇 차단
# 트리거가 되는 짧은 시간 내 요청 폭주를 피한다.
PACE_DELAY = (2.0, 4.0)


def pace():
    time.sleep(random.uniform(*PACE_DELAY))


def read_channels():
    channels = []
    for line in CHANNEL_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        channels.append(line)
    return channels


BOT_CHECK_RETRY_DELAYS = (10, 30, 60)  # 초 단위, "Sign in to confirm you're not a bot" 대응


def run_yt_dlp_json(args):
    pace()
    for attempt, delay in enumerate((0, *BOT_CHECK_RETRY_DELAYS)):
        if delay:
            time.sleep(delay)
        proc = subprocess.run(
            [sys.executable, "-m", "yt_dlp", *args],
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0:
            break
        is_bot_check = "not a bot" in proc.stderr
        if not is_bot_check or attempt == len(BOT_CHECK_RETRY_DELAYS):
            print(f"  [warn] yt-dlp 실패: {' '.join(args)}\n{proc.stderr.strip()[-500:]}", file=sys.stderr)
            break
        print(f"  [retry] 봇 차단 감지, {BOT_CHECK_RETRY_DELAYS[attempt]}초 후 재시도: {args[-1]}", file=sys.stderr)
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


TAB_SUFFIXES = ("/videos", "/streams", "/shorts")


def list_recent_videos(channel_url, max_videos=15):
    url = channel_url.rstrip("/")
    if not url.endswith(TAB_SUFFIXES):
        url += "/videos"
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


# 한 채널에서 연속으로 이만큼 상세 정보 수집에 완전히 실패하면(재시도까지
# 다 써도 실패) 그 채널은 오늘 차단된 것으로 보고 남은 영상은 건너뛴다.
# 끝까지 밀어붙여봐야 시간만 잡아먹고 성공 확률은 낮기 때문.
CHANNEL_FAILURE_LIMIT = 2


def collect(since_days=1, max_videos_per_channel=15, with_transcript=True):
    channels = read_channels()
    cutoff = datetime.now(KST) - timedelta(days=since_days)
    results = []
    failed_channels = []
    tmp_dir = DATA_DIR / "subs_tmp"

    for channel_url in channels:
        print(f"[channel] {channel_url}")
        recent = list_recent_videos(channel_url, max_videos_per_channel)
        consecutive_failures = 0
        for entry in recent:
            video_url = entry.get("url") or f"https://www.youtube.com/watch?v={entry.get('id')}"
            detail = fetch_video_detail(video_url)
            if not detail:
                print(f"  [skip] 상세 정보 수집 실패: {video_url}")
                consecutive_failures += 1
                if consecutive_failures >= CHANNEL_FAILURE_LIMIT:
                    print(f"  [skip-channel] 연속 {consecutive_failures}건 실패, 이 채널은 오늘 차단된 것으로 보고 건너뜁니다: {channel_url}")
                    failed_channels.append(channel_url)
                    break
                continue
            consecutive_failures = 0

            upload_date = detail.get("upload_date")  # YYYYMMDD
            if upload_date:
                uploaded_at = datetime.strptime(upload_date, "%Y%m%d").replace(tzinfo=KST)
                if uploaded_at < cutoff:
                    # 업로드 목록은 최신순이므로, 하루 범위를 벗어난 영상을
                    # 만나면 이후 영상도 전부 더 오래된 것 -> 채널 순회 종료.
                    break

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
    if failed_channels:
        print(f"[경고] 아래 채널은 오늘 봇 차단으로 수집하지 못했습니다 (내일 재시도 필요):")
        for ch in failed_channels:
            print(f"  - {ch}")
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
