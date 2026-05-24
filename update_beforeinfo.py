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
        "stabilizer": "",
    }

    full_text = " ".join(lines)
    if "安定板使用" in full_text:
        weather["stabilizer"] = "安定板使用"

    start = 0
    for i, line in enumerate(lines):
        if "水面気象情報" in line:
            start = i
            break

    end = len(lines)
    for i in range(start + 1, len(lines)):
        if "スマートフォン版" in lines[i] or "PAGE TOP" in lines[i]:
            end = i
            break

    text = clean(" ".join(lines[start:end]))

    m = re.search(r"水面気象情報\s*([0-9R時点:]+)?", text)
    if m:
        weather["time"] = clean(m.group(1) or "")

    m = re.search(r"気温\s*([0-9.]+℃)", text)
    if m:
        weather["air_temperature"] = m.group(1)

    m = re.search(r"(晴|曇り|曇|雨|雪)", text)
    if m:
        weather["weather"] = m.group(1)

    m = re.search(r"風速\s*([0-9.]+m)", text)
    if m:
        weather["wind_speed"] = m.group(1)

    m = re.search(r"水温\s*([0-9.]+℃)", text)
    if m:
        weather["water_temperature"] = m.group(1)

    m = re.search(r"波高\s*([0-9.]+cm)", text)
    if m:
        weather["wave_height"] = m.group(1)

    for word in ["向かい風", "追い風", "右横風", "左横風", "無風", "北", "南", "東", "西"]:
        if word in text:
            weather["wind_direction"] = word
            break

    return weather



def parse_start_exhibition(lines: list[str]) -> tuple[list[dict[str, Any]], dict[int, dict[str, Any]]]:
    start = None
    for i, line in enumerate(lines):
        if "スタート展示" in line:
            start = i
            break

    if start is None:
        return [], {}

    end = len(lines)
    for i in range(start + 1, len(lines)):
        if "水面気象情報" in lines[i] or "スマートフォン版" in lines[i] or "PAGE TOP" in lines[i]:
            end = i
            break

    text = clean(" ".join(lines[start:end]))

    result: list[dict[str, Any]] = []
    by_boat: dict[int, dict[str, Any]] = {}

    pattern = re.compile(r"(?:^|\s)([1-6])\s+(?:Image\s+)?(F?\.\d{2})(?=\s|$)")
    course = 1

    for m in pattern.finditer(text):
        if course > 6:
            break
        boat = int(m.group(1))
        st = m.group(2)
        result.append({"course": course, "boat": boat, "st": st})
        by_boat[boat] = {"exhibition_course": str(course), "exhibition_st": st}
        course += 1

    return result, by_boat



def parse_beforeinfo_entries(lines: list[str]) -> dict[int, dict[str, Any]]:
    """
    展示タイム・チルト取得の修正版。
    公式の直前情報は、行によって
    「1 Image 選手名 52.0kg 6.81 -0.5」
    の横並びにも、
    「1 / Image / 選手名 / 52.0kg / 6.81 / -0.5」
    の縦並びにも見えるため、テキスト結合後に艇ごとのブロックとして読む。
    """
    out: dict[int, dict[str, Any]] = {}

    start = 0
    for i, line in enumerate(lines):
        if "ボートレーサー" in line and ("体重" in line or "展示" in line):
            start = i
            break
        if line == "枠":
            start = i

    end = len(lines)
    for i in range(start + 1, len(lines)):
        if "部品交換凡例" in lines[i] or "スタート展示" in lines[i] or "水面気象情報" in lines[i]:
            end = i
            break

    text = clean(" ".join(lines[start:end]))

    starts = list(re.finditer(r"(?:^|\s)([1-6])\s+(?:Image\s+)?", text))

    for idx, m in enumerate(starts):
        boat = int(m.group(1))
        if boat < 1 or boat > 6:
            continue

        block_start = m.end()
        block_end = starts[idx + 1].start() if idx + 1 < len(starts) else len(text)
        block = clean(text[block_start:block_end])

        row = re.search(
            r"([一-龥ぁ-んァ-ンー・\s]{2,30}?)\s+([0-9]{2,3}\.\dkg)\s+([0-9]\.\d{2})\s+([-+]?\d+\.\d)",
            block
        )
        if not row:
            continue

        name = clean(row.group(1))
        name = re.sub(r"^(写真|Image)\s*", "", name)
        weight = row.group(2)
        exhibition_time = row.group(3)
        tilt = row.group(4)

        after = block[row.end():]
        propeller = "新" if re.search(r"(?:^|\s)新(?:\s|$)", after) else ""

        parts_keywords = ["ピストン", "リング", "電気", "キャブ", "シリンダ", "シャフト", "ギヤ", "キャリボ"]
        found_parts = [p for p in parts_keywords if p in after]
        parts_exchange = " / ".join(found_parts)

        out[boat] = {
            "beforeinfo_name": name,
            "weight": weight,
            "exhibition_time": exhibition_time,
            "tilt": tilt,
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

    ex_time_count = sum(1 for v in entry_map.values() if v.get("exhibition_time"))
    ex_st_count = sum(1 for v in entry_map.values() if v.get("exhibition_st"))
    weather_ok = bool(weather.get("wind_speed") or weather.get("wave_height") or weather.get("air_temperature"))

    available = ex_time_count > 0 or ex_st_count > 0 or weather_ok

    return {
        "status": "取得済み" if available else "未掲載",
        "reason": "" if available else "直前情報の主要項目が未掲載または解析不可",
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

    return idx, race, f"[OK] {place} {rno}R beforeinfo={race.get('beforeinfo_status')} 展示T={ex_time_count}/6 展示ST={ex_st_count}/6 天気={'OK' if weather_ok else 'NG'}"

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
