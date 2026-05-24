from __future__ import annotations

import concurrent.futures
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup

JST = timezone(timedelta(hours=9))

JCD_MAP = {
    "01": "桐生", "02": "戸田", "03": "江戸川", "04": "平和島", "05": "多摩川", "06": "浜名湖",
    "07": "蒲郡", "08": "常滑", "09": "津", "10": "三国", "11": "びわこ", "12": "住之江",
    "13": "尼崎", "14": "鳴門", "15": "丸亀", "16": "児島", "17": "宮島", "18": "徳山",
    "19": "下関", "20": "若松", "21": "芦屋", "22": "福岡", "23": "唐津", "24": "大村",
}
PLACE_TO_JCD = {v: k for k, v in JCD_MAP.items()}

BASE_BEFOREINFO_URL = "https://www.boatrace.jp/owpc/pc/race/beforeinfo"
MAX_WORKERS = 6

def log(msg: str) -> None:
    print(msg, flush=True)

def clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("\u3000", " ")).strip()

def now_jst() -> datetime:
    return datetime.now(JST)

def target_date() -> str:
    raw = (os.environ.get("TARGET_DATE") or "").strip()
    if raw:
        return raw.replace("-", "")
    return now_jst().strftime("%Y%m%d")

def date_hyphen(yyyymmdd: str) -> str:
    return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:8]}"

def date_compact(date_str: str) -> str:
    return str(date_str).replace("-", "")

def race_no_int(race_no: Any) -> int:
    m = re.search(r"\d+", str(race_no or ""))
    return int(m.group(0)) if m else 0

def fetch_url(url: str, timeout: int = 12) -> str | None:
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; BoatAIRiskLab-beforeinfo/1.0; +https://rifle5061.github.io/boat-ai-risk-lab/)"
    }
    last_error = None
    for attempt in range(1, 3):
        try:
            res = requests.get(url, headers=headers, timeout=timeout)
            res.raise_for_status()
            res.encoding = res.apparent_encoding or "utf-8"
            return res.text
        except Exception as e:
            last_error = e
            log(f"[FETCH_RETRY] attempt={attempt} url={url} error={e}")
            time.sleep(0.8 * attempt)
    log(f"[FETCH_NG] {url} {last_error}")
    return None

def soup_lines(soup: BeautifulSoup) -> list[str]:
    return [clean(x) for x in soup.get_text("\n").split("\n") if clean(x)]

def parse_weather(lines: list[str]) -> dict[str, Any]:
    weather = {
        "time": "",
        "air_temperature": "",
        "weather": "",
        "wind_speed": "",
        "wind_direction": "",
        "water_temperature": "",
        "wave_height": "",
    }

    start = None
    for i, line in enumerate(lines):
        if "水面気象情報" in line:
            start = i
            m = re.search(r"(\d{1,2}:\d{2})", line)
            if m:
                weather["time"] = m.group(1)
            break

    if start is None:
        return weather

    chunk = lines[start:start + 24]
    for i, line in enumerate(chunk):
        m = re.search(r"気温\s*([0-9.]+℃)", line)
        if m:
            weather["air_temperature"] = m.group(1)

        m = re.search(r"風速\s*([0-9.]+m)", line)
        if m:
            weather["wind_speed"] = m.group(1)

        m = re.search(r"水温\s*([0-9.]+℃)", line)
        if m:
            weather["water_temperature"] = m.group(1)

        m = re.search(r"波高\s*([0-9.]+cm)", line)
        if m:
            weather["wave_height"] = m.group(1)

        if any(w in line for w in ["晴", "曇", "曇り", "雨", "雪"]):
            if not any(key in line for key in ["気温", "水温", "風速", "波高"]):
                weather["weather"] = line

        if any(w in line for w in ["向かい風", "追い風", "右横風", "左横風", "無風", "北", "南", "東", "西"]):
            if "Image" not in line and not any(key in line for key in ["気温", "水温", "風速", "波高"]):
                weather["wind_direction"] = line

    return weather

def parse_start_exhibition(lines: list[str]) -> tuple[list[dict[str, Any]], dict[int, dict[str, Any]]]:
    """
    スタート展示の並びとSTを取得。
    公式テキストは「スタート展示」「コース 並び ST」の後に
    1 Image F.10
    3 Image F.02
    のように出る。先頭数字を艇番、行順を展示コースとして扱う。
    """
    start = None
    for i, line in enumerate(lines):
        if line == "スタート展示" or "スタート展示" in line:
            start = i
            break

    if start is None:
        return [], {}

    result: list[dict[str, Any]] = []
    by_boat: dict[int, dict[str, Any]] = {}

    course = 1
    for line in lines[start:start + 40]:
        m = re.search(r"^([1-6])\s+(?:Image\s+)?(F?\.\d{2})", line)
        if not m:
            continue

        boat = int(m.group(1))
        st = m.group(2)

        row = {
            "course": course,
            "boat": boat,
            "st": st,
        }
        result.append(row)
        by_boat[boat] = {
            "exhibition_course": str(course),
            "exhibition_st": st,
        }
        course += 1

        if course > 6:
            break

    return result, by_boat

def parse_beforeinfo_entries(lines: list[str]) -> dict[int, dict[str, Any]]:
    """
    直前情報テーブルから艇ごとの展示タイム・チルトなどを拾う。
    Webテキストでは以下のような行が出る：
    1 Image 今泉 徹 52.0kg 6.81 0.0
    6 Image 木村 浩士 55.7kg 6.87 0.0
    """
    out: dict[int, dict[str, Any]] = {}

    # 直前情報テーブル周辺だけを見る
    start = 0
    for i, line in enumerate(lines):
        if "枠" in line and "写真" in line and "ボートレーサー" in line:
            start = i
            break

    end = len(lines)
    for i in range(start + 1, len(lines)):
        if "部品交換凡例" in lines[i] or "スタート展示" in lines[i]:
            end = i
            break

    chunk = lines[start:end]

    row_pattern = re.compile(
        r"^([1-6])\s+(?:Image\s+)?(.+?)\s+([0-9.]+kg)\s+([0-9]\.\d{2})\s+([-+]?\d+\.\d+)(.*)$"
    )

    for line in chunk:
        m = row_pattern.search(line)
        if not m:
            continue

        boat = int(m.group(1))
        name = clean(m.group(2))
        rest = clean(m.group(6))

        # 部品交換・プロペラ新などが取れたら入れる。無ければ空欄。
        propeller = "新" if "新" in rest else ""
        parts_exchange = ""
        parts_keywords = ["ピストン", "リング", "電気", "キャブ", "シリンダ", "シャフト", "ギヤ", "キャリボ"]
        found_parts = [p for p in parts_keywords if p in rest]
        if found_parts:
            parts_exchange = " / ".join(found_parts)

        out[boat] = {
            "beforeinfo_name": name,
            "weight": m.group(3),
            "exhibition_time": m.group(4),
            "tilt": m.group(5),
            "propeller": propeller,
            "parts_exchange": parts_exchange,
        }

    return out

def parse_beforeinfo(html: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    lines = soup_lines(soup)
    text = " ".join(lines)

    if "直前情報" not in text:
        return {
            "status": "未取得",
            "reason": "直前情報ページではありません",
            "entries": {},
            "weather": {},
            "start_exhibition": [],
        }

    entry_map = parse_beforeinfo_entries(lines)
    start_exhibition, start_by_boat = parse_start_exhibition(lines)
    weather = parse_weather(lines)

    # 展示ST・展示コースを艇データに合流
    for boat, v in start_by_boat.items():
        entry_map.setdefault(boat, {}).update(v)

    available = bool(entry_map or weather or start_exhibition)

    return {
        "status": "取得済み" if available else "未取得",
        "reason": "" if available else "直前情報の主要項目が未掲載",
        "entries": entry_map,
        "weather": weather,
        "start_exhibition": start_exhibition,
    }

def merge_beforeinfo_into_race(race: dict[str, Any], parsed: dict[str, Any], fetched_at: str) -> dict[str, Any]:
    if parsed.get("status") != "取得済み":
        # 既存データを壊さない
        race["beforeinfo_status"] = parsed.get("status", "未取得")
        race["beforeinfo_reason"] = parsed.get("reason", "")
        return race

    race["beforeinfo_status"] = "取得済み"
    race["beforeinfo_updated_at"] = fetched_at
    race["weather"] = parsed.get("weather", {})
    race["start_exhibition"] = parsed.get("start_exhibition", [])

    entry_map = parsed.get("entries", {})
    entries = race.get("entries", [])
    if not isinstance(entries, list):
        entries = []

    # entriesが無ければ6艇分だけ作る
    if len(entries) < 6:
        existing = {int(e.get("frame") or e.get("boat_no") or i + 1): e for i, e in enumerate(entries)}
        entries = []
        for boat in range(1, 7):
            e = existing.get(boat, {"frame": boat, "boat_no": boat})
            entries.append(e)

    for i, e in enumerate(entries):
        boat = int(e.get("frame") or e.get("boat_no") or i + 1)
        add = entry_map.get(boat, {})
        for k, v in add.items():
            if v not in ("", None):
                e[k] = v
        e["frame"] = boat
        e["boat_no"] = boat

    race["entries"] = entries
    return race

def target_races(data: list[dict[str, Any]], target: str) -> list[tuple[int, dict[str, Any]]]:
    target_h = date_hyphen(target)
    rows = []
    for idx, race in enumerate(data):
        if str(race.get("date", "")) != target_h:
            continue
        place = race.get("place", "")
        rno = race_no_int(race.get("race_no", ""))
        if not place or place not in PLACE_TO_JCD or not rno:
            continue
        rows.append((idx, race))
    return rows

def fetch_and_parse(task: tuple[int, dict[str, Any], str]) -> tuple[int, dict[str, Any], str]:
    idx, race, target = task
    place = race.get("place", "")
    jcd = PLACE_TO_JCD.get(place)
    rno = race_no_int(race.get("race_no", ""))

    url = f"{BASE_BEFOREINFO_URL}?hd={target}&jcd={jcd}&rno={rno}"
    html = fetch_url(url, timeout=12)

    if not html:
        return idx, race, f"[NG] {place} {rno}R fetch failed"

    parsed = parse_beforeinfo(html)
    fetched_at = now_jst().strftime("%Y-%m-%d %H:%M:%S")
    race = merge_beforeinfo_into_race(race, parsed, fetched_at)

    entries = race.get("entries", [])
    ex_time_count = sum(1 for e in entries if e.get("exhibition_time"))
    ex_st_count = sum(1 for e in entries if e.get("exhibition_st"))
    weather_ok = bool(race.get("weather", {}).get("wind_speed") or race.get("weather", {}).get("wave_height"))

    return idx, race, f"[OK] {place} {rno}R beforeinfo={race.get('beforeinfo_status')} 展示T={ex_time_count}/6 展示ST={ex_st_count}/6 weather={'OK' if weather_ok else 'NG'}"

def main() -> None:
    target = target_date()
    data_path = Path("race-data.json")
    if not data_path.exists():
        raise FileNotFoundError("race-data.json が見つかりません")

    data_raw = json.loads(data_path.read_text(encoding="utf-8"))
    data = data_raw if isinstance(data_raw, list) else data_raw.get("races", [])

    rows = target_races(data, target)
    log(f"[INFO] target_date={target} target_races={len(rows)} workers={MAX_WORKERS}")

    debug: list[str] = []
    updated = 0

    tasks = [(idx, race, target) for idx, race in rows]

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_map = {executor.submit(fetch_and_parse, task): task for task in tasks}
        for future in concurrent.futures.as_completed(future_map):
            try:
                idx, race, line = future.result()
                data[idx] = race
                if race.get("beforeinfo_status") == "取得済み":
                    updated += 1
            except Exception as e:
                _, race, _ = future_map[future]
                line = f"[ERROR] {race.get('place')} {race.get('race_no')} {e}"

            log(line)
            debug.append(line)

    debug.append("===== BEFOREINFO QUALITY CHECK =====")
    debug.append(f"対象レース数：{len(rows)}")
    debug.append(f"直前情報取得済み：{updated}")

    # 全体の表示用チェック
    ex_time_races = 0
    ex_st_races = 0
    weather_races = 0
    for _, race in rows:
        entries = race.get("entries", [])
        if sum(1 for e in entries if e.get("exhibition_time")) >= 6:
            ex_time_races += 1
        if sum(1 for e in entries if e.get("exhibition_st")) >= 6:
            ex_st_races += 1
        if race.get("weather", {}).get("wind_speed") or race.get("weather", {}).get("wave_height"):
            weather_races += 1

    debug.append(f"展示タイム6艇取得：{ex_time_races}")
    debug.append(f"展示ST6艇取得：{ex_st_races}")
    debug.append(f"水面情報取得：{weather_races}")

    data_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    Path("debug-beforeinfo.txt").write_text("\n".join(debug), encoding="utf-8")
    log(f"updated beforeinfo races={updated}/{len(rows)}")

if __name__ == "__main__":
    main()
