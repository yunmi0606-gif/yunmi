"""
channel.txt에 나열된 유튜브 채널들의 최신 영상 메타데이터/자막을 수집해
data/YYYY-MM-DD.json 으로 저장한다. (yt-dlp 필요: pip install yt-dlp)

사용법:
    python collect.py [--since-days 1]

수집 전략 (클라우드 IP에서의 유튜브 봇 차단을 우회하기 위한 다단계 폴백):
    1. 채널 RSS 피드(videos.xml)로 제목/링크/게시시각을 수집한다.
       RSS는 봇 차단 없이 항상 동작하며, 원어(한국어) 제목이 그대로 나온다.
    2. 영상별 상세 정보(업로더/길이/자막)는 yt-dlp mweb 클라이언트로 시도한다.
       --ignore-no-formats-error 덕분에 실제 다운로드 포맷이 없어도 메타데이터는
       받아올 수 있다. 이마저 실패하면 해당 영상은 RSS 메타데이터만 기록한다.
    3. 자막(자동생성)은 여전히 PO Token 요구로 거의 항상 차단된다. 성공하면
       실제 자막을 기록하고, 실패하면 caption 필드를 비워 둔다 — format.md 규칙상
       자막 없이 내용을 추측/창작하지 않는다.

주의:
    실행 환경(특히 클라우드/서버 IP)에 따라 2/3단계가 실패할 수 있다. 이 경우
    --cookies-from-browser 로 로그인된 브라우저 쿠키를 넘기거나, 로컬(개인 PC)
    환경에서 실행해야 자막까지 안정적으로 수집된다.
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
ATOM_NS = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}


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


def resolve_channel_id(channel_url):
    """채널 핸들(@handle) URL로부터 RSS에 필요한 channel_id를 구한다."""
    entries = run_yt_dlp_json(
        [
            "--flat-playlist",
            "--dump-single-json",
            "--playlist-end",
            "1",
            channel_url.rstrip("/") + "/videos",
        ]
    )
    if not entries:
        return None
    return entries[0].get("channel_id")


def list_recent_videos_via_rss(channel_id, max_videos=15):
    """채널 RSS 피드에서 제목/링크/게시시각을 가져온다 (봇 차단 없음)."""
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            raw = resp.read()
    except Exception as exc:
        print(f"  [warn] RSS 수집 실패: {exc}", file=sys.stderr)
        return []

    root = ET.fromstring(raw)
    videos = []
    for entry in root.findall("a:entry", ATOM_NS)[:max_videos]:
        video_id = entry.findtext("yt:videoId", default=None, namespaces=ATOM_NS)
        title = entry.findtext("a:title", default=None, namespaces=ATOM_NS)
        published = entry.findtext("a:published", default=None, namespaces=ATOM_NS)
        if not video_id:
            continue
        published_kst = None
        if published:
            published_kst = datetime.fromisoformat(published).astimezone(KST)
        videos.append(
            {
                "id": video_id,
                "title": title,
                "url": f"https://www.youtube.com/watch?v={video_id}",
                "published_kst": published_kst,
            }
        )
    return videos


def fetch_video_detail(video_url):
    """mweb 클라이언트로 영상 상세 메타데이터 + (가능하면) 자동자막 URL을 가져온다."""
    videos = run_yt_dlp_json(
        [
            "--skip-download",
            "--dump-json",
            "--ignore-no-formats-error",
            "--extractor-args", "youtube:player_client=mweb",
            video_url,
        ]
    )
    return videos[0] if videos else None


def fetch_korean_caption_text(detail):
    """자동자막(한국어) VTT를 받아 순수 텍스트로 합친다. 실패하면 None."""
    auto_subs = (detail or {}).get("automatic_captions") or {}
    tracks = auto_subs.get("ko") or auto_subs.get("ko-KR")
    if not tracks:
        return None
    vtt_track = next((t for t in tracks if t.get("ext") == "vtt"), tracks[0])
    caption_url = vtt_track.get("url")
    if not caption_url:
        return None
    try:
        with urllib.request.urlopen(caption_url, timeout=20) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
    except Exception as exc:
        print(f"  [warn] 자막 다운로드 실패: {exc}", file=sys.stderr)
        return None

    lines = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith(("WEBVTT", "Kind:", "Language:")):
            continue
        if "-->" in line or line.isdigit():
            continue
        lines.append(line)
    text = " ".join(dict.fromkeys(lines))  # 자동자막 특유의 중복 라인 제거
    return text or None


def collect(since_days=1, max_videos_per_channel=15):
    channels = read_channels()
    cutoff = datetime.now(KST) - timedelta(days=since_days)
    results = []

    for channel_url in channels:
        print(f"[channel] {channel_url}")
        channel_id = resolve_channel_id(channel_url)
        if not channel_id:
            print(f"  [skip] channel_id 확인 실패: {channel_url}")
            continue

        recent = list_recent_videos_via_rss(channel_id, max_videos_per_channel)
        for entry in recent:
            published_kst = entry.get("published_kst")
            if published_kst and published_kst < cutoff:
                continue

            video_url = entry["url"]
            detail = fetch_video_detail(video_url)
            caption_text = fetch_korean_caption_text(detail) if detail else None

            results.append(
                {
                    "channel": channel_url,
                    "video_id": entry["id"],
                    "title": entry.get("title") or (detail or {}).get("title"),
                    "url": video_url,
                    "upload_date": published_kst.strftime("%Y%m%d") if published_kst else (detail or {}).get("upload_date"),
                    "uploader": (detail or {}).get("uploader"),
                    "duration": (detail or {}).get("duration"),
                    "caption_available": caption_text is not None,
                    "caption_text": caption_text,
                }
            )
            status = "자막 O" if caption_text else "자막 X(제목만)"
            print(f"  [ok] {entry.get('title')} ({status})")

    DATA_DIR.mkdir(exist_ok=True)
    out_path = DATA_DIR / f"{datetime.now(KST).strftime('%Y-%m-%d')}.json"
    out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n총 {len(results)}개 영상 수집 완료 (자막 확보 {sum(1 for r in results if r['caption_available'])}건) -> {out_path}")
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--since-days", type=int, default=1)
    parser.add_argument("--max-videos-per-channel", type=int, default=15)
    args = parser.parse_args()
    collect(since_days=args.since_days, max_videos_per_channel=args.max_videos_per_channel)
