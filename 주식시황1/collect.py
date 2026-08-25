"""
channel.txt에 나열된 유튜브 채널들의 최신 영상 메타데이터/자막을 수집해
data/YYYY-MM-DD.json 으로 저장한다. (yt-dlp 필요: pip install yt-dlp)

사용법:
    python collect.py [--since-days 1]

방식 (2026-08-25 개선):
    1) 채널 RSS(videos.xml)로 오늘자 영상 목록(제목/링크/업로드일)을 우선 확보한다.
       yt-dlp 상세 API가 막혀도 이 단계는 거의 항상 성공한다.
    2) 각 영상에 대해 yt-dlp --dump-json 을
       --extractor-args "youtube:player_client=android,web_safari" 로 시도해
       설명(description)과 automatic_captions의 서명된 timedtext URL을 얻는다.
       유튜브 봇 차단(HTTP 429)으로 실패하면 지수 백오프로 재시도한다.
    3) 2)에서 얻은 서명된 자막 URL은 requests로 직접 요청하면(추가 yt-dlp 호출 없이)
       봇 차단을 우회해 실제 자막(json3)을 받아올 수 있는 경우가 많다. 성공하면
       세그먼트 텍스트를 이어붙여 순수 텍스트 자막으로 저장한다.

주의:
    그래도 일부 영상은 끝내 봇 차단으로 설명/자막을 얻지 못할 수 있다. 이 경우
    RSS로 얻은 제목/링크/업로드일만 저장하고 자막은 빈 문자열로 남긴다.
    format.md 규칙(실제 자막/스크립트 근거, 추측·창작 금지)에 따라, 자막이
    없는 영상은 제목 기준 추정 이상의 상세 요약을 만들면 안 된다.
"""
import argparse
import json
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
ATOM_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}
EXTRACTOR_ARGS = "youtube:player_client=android,web_safari"


def read_channels():
    channels = []
    for line in CHANNEL_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        channels.append(line)
    return channels


def http_get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def resolve_channel_id(channel_url):
    """채널 handle URL에서 UC... channel_id를 얻는다 (flat-playlist 메타데이터 이용)."""
    url = channel_url.rstrip("/") + "/videos"
    proc = subprocess.run(
        [sys.executable, "-m", "yt_dlp", "--flat-playlist", "--dump-json",
         "--playlist-end", "1", url],
        capture_output=True, text=True,
    )
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        cid = entry.get("playlist_channel_id") or entry.get("channel_id")
        if cid:
            return cid
    return None


def list_recent_from_rss(channel_id, cutoff):
    """채널 RSS로 cutoff 이후 업로드된 영상의 제목/링크/업로드일을 가져온다."""
    feed_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    raw = http_get(feed_url)
    root = ET.fromstring(raw)
    videos = []
    for entry in root.findall("atom:entry", ATOM_NS):
        video_id = entry.find("yt:videoId", ATOM_NS).text
        title = entry.find("atom:title", ATOM_NS).text
        link = entry.find("atom:link", ATOM_NS).attrib["href"]
        published = entry.find("atom:published", ATOM_NS).text
        uploaded_at = datetime.fromisoformat(published).astimezone(KST)
        if uploaded_at < cutoff:
            continue
        author_el = entry.find("atom:author/atom:name", ATOM_NS)
        videos.append(
            {
                "video_id": video_id,
                "title": title,
                "url": link,
                "uploader": author_el.text if author_el is not None else None,
                "upload_date": uploaded_at.strftime("%Y%m%d"),
            }
        )
    return videos


def fetch_video_detail_json(video_url, retries=4, backoff=15):
    """description + automatic_captions(서명 URL 포함) 등 상세 메타데이터를 시도한다."""
    for attempt in range(retries):
        proc = subprocess.run(
            [sys.executable, "-m", "yt_dlp", "--skip-download", "--dump-json",
             "--extractor-args", EXTRACTOR_ARGS, video_url],
            capture_output=True, text=True,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            try:
                return json.loads(proc.stdout.strip().splitlines()[-1])
            except json.JSONDecodeError:
                pass
        if attempt < retries - 1:
            time.sleep(backoff * (attempt + 1))
    return None


def fetch_caption_text(detail, lang="ko"):
    """automatic_captions의 서명된 timedtext URL을 직접 요청해 자막 텍스트를 얻는다."""
    if not detail:
        return ""
    caps = (detail.get("automatic_captions") or {}).get(lang)
    if not caps:
        return ""
    json3_url = next((c["url"] for c in caps if c.get("ext") == "json3"), None)
    if not json3_url:
        return ""
    try:
        raw = http_get(json3_url, timeout=20)
        data = json.loads(raw)
    except Exception:
        return ""
    parts = []
    for event in data.get("events", []):
        for seg in event.get("segs", []) or []:
            text = seg.get("utf8")
            if text:
                parts.append(text)
    return "".join(parts).strip()


def collect(since_days=1, max_videos_per_channel=15):
    channels = read_channels()
    cutoff = datetime.now(KST) - timedelta(days=since_days)
    results = []

    for channel_url in channels:
        print(f"[channel] {channel_url}")
        channel_id = resolve_channel_id(channel_url)
        if not channel_id:
            print(f"  [skip] channel_id 확인 실패: {channel_url}", file=sys.stderr)
            continue

        recent = list_recent_from_rss(channel_id, cutoff)[:max_videos_per_channel]
        for i, entry in enumerate(recent):
            if i > 0:
                time.sleep(12)
            print(f"  [video] {entry['title']}")
            detail = fetch_video_detail_json(entry["url"])
            description = detail.get("description") if detail else None
            caption_text = fetch_caption_text(detail)
            if not caption_text:
                print(f"    [warn] 자막 수집 실패: {entry['url']}")

            results.append(
                {
                    "channel": channel_url,
                    "video_id": entry["video_id"],
                    "title": entry["title"],
                    "url": entry["url"],
                    "upload_date": entry["upload_date"],
                    "uploader": entry["uploader"],
                    "description": description,
                    "caption_text": caption_text,
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
