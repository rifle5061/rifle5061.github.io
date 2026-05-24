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

    start = None
    for i, line in enumerate(lines):
        if "水面気象情報" in line:
            start = i
            weather["time"] = clean(line.replace("水面気象情報", ""))
            break

    if start is None:
        return weather

    end = min(len(lines), start + 30)
    for i in range(start + 1, len(lines)):
        if "スマートフォン版" in lines[i] or "PAGE TOP" in lines[i] or "■ボートレース" in lines[i]:
            end = i
            break

    chunk = lines[start:end]

    for i, line in enumerate(chunk):
        if line == "気温" and i + 1 < len(chunk):
            weather["air_temperature"] = chunk[i + 1]
            if i + 2 < len(chunk) and any(w in chunk[i + 2] for w in ["晴", "曇", "曇り", "雨", "雪"]):
                weather["weather"] = chunk[i + 2]
        elif line == "風速" and i + 1 < len(chunk):
            weather["wind_speed"] = chunk[i + 1]
        elif line == "水温" and i + 1 < len(chunk):
            weather["water_temperature"] = chunk[i + 1]
        elif line == "波高" and i + 1 < len(chunk):
            weather["wave_height"] = chunk[i + 1]
        elif any(w in line for w in ["向かい風", "追い風", "右横風", "左横風", "無風"]):
            weather["wind_direction"] = line

    return weather



def parse_start_exhibition(lines: list[str]) -> tuple[list[dict[str, Any]], dict[int, dict[str, Any]]]:
    """
    スタート展示の実値がテキストに出ている場合だけ拾う。
    今回のsourceでは見出しのみで、実値が出ていないレースが多い。
    """
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

    chunk = lines[start:end]
    result: list[dict[str, Any]] = []
    by_boat: dict[int, dict[str, Any]] = {}

    # 1 .10 のような連続だけを拾う
    course = 1
    for i, token in enumerate(chunk[:-1]):
        if re.fullmatch(r"[1-6]", token) and re.fullmatch(r"F?\.\d{2}", chunk[i + 1]):
            boat = int(token)
            st = chunk[i + 1]
            result.append({"course": course, "boat": boat, "st": st})
            by_boat[boat] = {"exhibition_course": str(course), "exhibition_st": st}
            course += 1
            if course > 6:
                break

    return result, by_boat



def parse_beforeinfo_entries(lines: list[str]) -> dict[int, dict[str, Any]]:
    """
    公式beforeinfoのテキストは、展示タイムが本文テキストに出ないケースがある。
    そのため、ここでは確実に取れている「体重・チルト・部品交換」を優先して拾う。
    展示タイムは本文に 6.80 のような値が出た場合だけ保存する。
    """
    out: dict[int, dict[str, Any]] = {}

    start = 0
    for i, line in enumerate(lines):
        if line == "枠":
            start = i
            break

    end = len(lines)
    for i in range(start + 1, len(lines)):
        if "部品交換凡例" in lines[i] or "スタート展示" in lines[i] or "水面気象情報" in lines[i]:
            end = i
            break

    chunk = lines[start:end]

    # 艇番行の位置を拾う。次行が選手名、次々行が体重なら艇データとして採用。
    starts = []
    for i, line in enumerate(chunk):
        if re.fullmatch(r"[1-6]", line):
            if i + 2 < len(chunk) and re.search(r"kg$", chunk[i + 2]):
                starts.append(i)

    for pos, i in enumerate(starts):
        boat = int(chunk[i])
        block_end = starts[pos + 1] if pos + 1 < len(starts) else len(chunk)
        block = chunk[i:block_end]

        e = {
            "beforeinfo_name": clean(block[1]) if len(block) > 1 else "",
            "weight": clean(block[2]) if len(block) > 2 else "",
            "exhibition_time": "",
            "tilt": "",
            "propeller": "",
            "parts_exchange": "",
        }

        # 体重の直後からR/進入/ST/着順が出る前までを直前情報部分として見る
        head_vals = []
        for token in block[3:]:
            if token in {"R", "進入", "ST", "着順"}:
                break
            head_vals.append(token)

        # head_vals内の 6.xx は展示タイム、-0.5/0.0/0.5/1.0 などはチルト候補
        for token in head_vals:
            if re.fullmatch(r"[0-9]\.\d{2}", token):
                e["exhibition_time"] = token
            elif re.fullmatch(r"[-+]?\d+\.\d", token):
                e["tilt"] = token
            elif token == "新":
                e["propeller"] = "新"
            elif any(p in token for p in ["ピストン", "リング", "電気", "キャブ", "シリンダ", "シャフト", "ギヤ", "キャリボ"]):
                e["parts_exchange"] = token

        # 部品交換が体重直後に来るケースも拾う
        parts = []
        for token in block[3:]:
            if token in {"R", "進入", "ST", "着順"}:
                break
            if any(p in token for p in ["ピストン", "リング", "電気", "キャブ", "シリンダ", "シャフト", "ギヤ", "キャリボ"]):
                parts.append(token)
        if parts:
            e["parts_exchange"] = " / ".join(parts)

        out[boat] = e

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

    for boat, v in start_by_boat.items():
        entry_map.setdefault(boat, {}).update(v)

    ex_time_count = sum(1 for v in entry_map.values() if v.get("exhibition_time"))
    ex_st_count = sum(1 for v in entry_map.values() if v.get("exhibition_st"))
    weather_ok = bool(weather.get("wind_speed") or weather.get("wave_height") or weather.get("air_temperature"))

    available = bool(entry_map) or weather_ok or ex_time_count > 0 or ex_st_count > 0

    return {
        "status": "取得済み" if available else "未掲載",
        "reason": "" if available else "直前情報の主要項目が未掲載または解析不可",
        "entries": entry_map,
        "weather": weather,
        "start_exhibition": start_exhibition,
    }



def extract_beforeinfo_source_snippet(html: str, place: str, rno: int) -> str:
    soup = BeautifulSoup(html, "html.parser")
    lines = soup_lines(soup)

    start = 0
    for i, line in enumerate(lines):
        if "直前情報" in line or line == "枠" or "ボートレーサー" in line:
            start = max(0, i - 20)
            break

    end = min(len(lines), start + 280)
    for i in range(start + 1, len(lines)):
        if "スマートフォン版" in lines[i] or "PAGE TOP" in lines[i]:
            end = min(len(lines), i + 20)
            break

    body = "\n".join(f"{idx:03d}: {line}" for idx, line in enumerate(lines[start:end], start=start))
    return (
        "--------------------------------------------------\n"
        f"{place} {rno}R beforeinfo source\n"
        "--------------------------------------------------\n"
        + body
    )

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

def fetch_and_parse(task: tuple[int, dict[str, Any], str]) -> tuple[int, dict[str, Any], str, str]:
    idx, race, target = task
    place = race.get("place", "")
    jcd = PLACE_TO_JCD.get(place)
    rno = race_no_int(race.get("race_no", ""))

    url = f"{BASE_BEFOREINFO_URL}?hd={target}&jcd={jcd}&rno={rno}"
    html = fetch_url(url, timeout=12)

    if not html:
        return idx, race, f"[NG] {place} {rno}R fetch failed", ""

    source_snippet = extract_beforeinfo_source_snippet(html, place, rno)
    parsed = parse_beforeinfo(html)
    fetched_at = now_jst().strftime("%Y-%m-%d %H:%M:%S")
    race = merge_beforeinfo_into_race(race, parsed, fetched_at)

    entries = race.get("entries", [])
    ex_time_count = sum(1 for e in entries if e.get("exhibition_time"))
    ex_st_count = sum(1 for e in entries if e.get("exhibition_st"))
    weather_ok = bool(race.get("weather", {}).get("wind_speed") or race.get("weather", {}).get("wave_height") or race.get("weather", {}).get("air_temperature"))

    status_text = "OK" if weather_ok else "NG"
    line = f"[OK] {place} {rno}R beforeinfo={race.get('beforeinfo_status')} 展示T={ex_time_count}/6 展示ST={ex_st_count}/6 天気={status_text}"
    return idx, race, line, source_snippet

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
    source_debug: list[str] = []
    updated = 0

    tasks = [(idx, race, target) for idx, race in rows]

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_map = {executor.submit(fetch_and_parse, task): task for task in tasks}
        for future in concurrent.futures.as_completed(future_map):
            try:
                idx, race, line, source_snippet = future.result()
                data[idx] = race
                if race.get("beforeinfo_status") == "取得済み":
                    updated += 1
                if source_snippet and ("展示T=0/6" in line or "展示ST=0/6" in line or "天気=NG" in line):
                    source_debug.append(source_snippet)
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
        if race.get("weather", {}).get("wind_speed") or race.get("weather", {}).get("wave_height") or race.get("weather", {}).get("air_temperature"):
            weather_races += 1

    debug.append(f"展示タイム6艇取得：{ex_time_races}")
    debug.append(f"展示ST6艇取得：{ex_st_races}")
    debug.append(f"水面情報取得：{weather_races}")

    data_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    Path("debug-beforeinfo.txt").write_text("\n".join(debug), encoding="utf-8")
    Path("debug-beforeinfo-source.txt").write_text("\n\n".join(source_debug[:30]), encoding="utf-8")
    log(f"updated beforeinfo races={updated}/{len(rows)}")

if __name__ == "__main__":
    main()
