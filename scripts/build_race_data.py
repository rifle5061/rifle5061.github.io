from __future__ import annotations

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

TIME_ZONE_BY_PLACE = {
    "三国": "モーニング", "徳山": "モーニング", "芦屋": "モーニング", "唐津": "モーニング",
    "桐生": "ナイター", "蒲郡": "ナイター", "住之江": "ナイター", "丸亀": "ナイター", "若松": "ナイター", "大村": "ナイター",
    "下関": "ミッドナイト",
}

BASE_RACELIST_URL = "https://www.boatrace.jp/owpc/pc/race/racelist"
BASE_INDEX_URL = "https://www.boatrace.jp/owpc/pc/race/index"

ZEN_NUM = {"１": 1, "２": 2, "３": 3, "４": 4, "５": 5, "６": 6}
BRANCHES = ["群馬","埼玉","東京","静岡","愛知","三重","福井","滋賀","大阪","兵庫","徳島","香川","岡山","広島","山口","福岡","佐賀","長崎"]

def log(msg: str) -> None:
    print(msg, flush=True)

def target_date() -> str:
    raw = (os.environ.get("TARGET_DATE") or "").strip()
    if raw:
        return raw.replace("-", "")
    return datetime.now(JST).strftime("%Y%m%d")

def date_hyphen(yyyymmdd: str) -> str:
    return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:8]}"

def clean(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()

def fetch_url(url: str, timeout: int = 25) -> str | None:
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; BoatAIRiskLab/0.8; +https://rifle5061.github.io/boat-ai-risk-lab/)"
    }

    last_error = None
    for attempt in range(1, 4):
        try:
            res = requests.get(url, headers=headers, timeout=timeout)
            res.raise_for_status()
            res.encoding = res.apparent_encoding or "utf-8"
            return res.text
        except Exception as e:
            last_error = e
            log(f"[FETCH_RETRY] attempt={attempt} url={url} error={e}")
            time.sleep(1.5 * attempt)

    log(f"[FETCH_NG] {url} {last_error}")
    return None

def target_jcds_from_official_index(date: str) -> list[str]:
    urls = [
        f"{BASE_INDEX_URL}?hd={date}",
        BASE_INDEX_URL,
    ]

    for url in urls:
        html = fetch_url(url, timeout=25)
        if not html:
            continue

        found = sorted(set(re.findall(r"[?&]jcd=(\d{2})", html)))
        found = [x for x in found if x in JCD_MAP]

        if found:
            places = [JCD_MAP[x] for x in found]
            log(f"[INFO] target venues from official index: {', '.join(places)}")
            return found

        text = BeautifulSoup(html, "html.parser").get_text("\n")
        places_found = []
        for jcd, place in JCD_MAP.items():
            if place in text:
                places_found.append(jcd)
        places_found = sorted(set(places_found))
        if places_found:
            places = [JCD_MAP[x] for x in places_found]
            log(f"[INFO] target venues from official index text: {', '.join(places)}")
            return places_found

    return []

def target_jcds_from_existing_racedata(date: str) -> list[str]:
    p = Path("race-data.json")
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        places = sorted({r.get("place") for r in data if r.get("date") == date_hyphen(date) and r.get("place")})
        jcds = [PLACE_TO_JCD[x] for x in places if x in PLACE_TO_JCD]
        if jcds:
            log(f"[INFO] target venues from existing race-data.json: {', '.join(places)}")
            return jcds
    except Exception as e:
        log(f"[WARN] cannot read existing race-data.json: {e}")
    return []

def target_jcds(date: str) -> list[str]:
    jcds = target_jcds_from_official_index(date)
    if jcds:
        return jcds

    jcds = target_jcds_from_existing_racedata(date)
    if jcds:
        return jcds

    log("[WARN] no target venues found. fallback to all 24 venues.")
    return list(JCD_MAP.keys())

def fetch_racelist_html(date: str, jcd: str, rno: int) -> str | None:
    url = f"{BASE_RACELIST_URL}?hd={date}&jcd={jcd}&rno={rno}"
    return fetch_url(url, timeout=25)

def soup_lines(soup: BeautifulSoup) -> list[str]:
    return [clean(x) for x in soup.get_text("\n").split("\n") if clean(x)]

def parse_deadline(lines: list[str], rno: int) -> str:
    for line in lines:
        if "締切予定時刻" in line:
            times = re.findall(r"\b([0-2]?[0-9]:[0-5][0-9])\b", line)
            if 1 <= rno <= len(times):
                return times[rno - 1]
            if times:
                return times[0]

    text = " ".join(lines)
    times = re.findall(r"\b([0-2]?[0-9]:[0-5][0-9])\b", text)
    if 1 <= rno <= len(times):
        return times[rno - 1]
    return times[0] if times else ""

def race_grade(text: str) -> str:
    upper = text.upper()
    if "SG" in upper: return "SG"
    if "G1" in upper or "Ｇ１" in upper: return "G1"
    if "G2" in upper or "Ｇ２" in upper: return "G2"
    if "G3" in upper or "Ｇ３" in upper: return "G3"
    return ""

def empty_entry(frame: int) -> dict[str, Any]:
    return {
        "frame": frame, "boat_no": frame, "racer_name": "", "class": "", "branch": "",
        "f_count": "", "l_count": "", "avg_st": "",
        "national_2rate": "", "national_3rate": "",
        "local_2rate": "", "local_3rate": "",
        "motor_2rate": "", "motor_3rate": "",
        "boat_2rate": "", "boat_3rate": "",
        "exhibition_st": "", "exhibition_time": "",
    }

def frame_line_to_int(line: str) -> int | None:
    s = clean(line)
    if s in ZEN_NUM:
        return ZEN_NUM[s]
    if re.fullmatch(r"[1-6]", s):
        return int(s)
    return None

def is_name_line(line: str) -> bool:
    if "/" in line:
        return False
    if line in BRANCHES:
        return False
    if line in ["全国","当地","モーター","ボート","勝率","2連率","3連率","写真","氏名"]:
        return False
    # 選手名は「今泉 徹」「松下 誉士」のような形が多い
    return bool(re.fullmatch(r"[一-龥ぁ-んァ-ンー・]+(?:\s+[一-龥ぁ-んァ-ンー・]+){0,2}", line)) and len(line.replace(" ", "")) >= 2

def decimals(line: str) -> list[str]:
    # 0.17 / 57.45 / .17 など。平均STや率の抽出用。
    return re.findall(r"(?<![0-9])(?:\d+\.\d+|\.\d{2})(?![0-9])", line)

def parse_block(frame: int, block: list[str]) -> dict[str, Any]:
    e = empty_entry(frame)

    # 登録番号/級別
    cls_idx = None
    for i, line in enumerate(block):
        m = re.search(r"\b(\d{4})\s*/\s*(A1|A2|B1|B2)\b", line)
        if m:
            e["class"] = m.group(2)
            cls_idx = i
            break

    search_from = cls_idx + 1 if cls_idx is not None else 0

    # 選手名
    for line in block[search_from:search_from + 8]:
        if is_name_line(line):
            e["racer_name"] = line
            break

    # 支部
    for line in block:
        if "/" in line:
            left = line.split("/", 1)[0]
            if left in BRANCHES:
                e["branch"] = left
                break

    # F/L
    for line in block:
        if re.fullmatch(r"F\d+", line):
            e["f_count"] = line.replace("F", "")
        if re.fullmatch(r"L\d+", line):
            e["l_count"] = line.replace("L", "")

    # 平均STと連率
    avg_idx = None
    nums: list[str] = []
    for i, line in enumerate(block):
        # 「0.17 3.93」のように平均ST＋全国勝率が出る行
        if re.search(r"\b0\.\d{2}\b", line):
            avg_idx = i
            break

    if avg_idx is not None:
        for line in block[avg_idx:]:
            for n in decimals(line):
                nums.append(n)
            # 必要分を取ったら、今節STなどが混ざる前に止める
            if len(nums) >= 11:
                break

    # nums: [avg_st, national_win, national_2, national_3, local_win, local_2, local_3, motor_2, motor_3, boat_2, boat_3]
    if len(nums) >= 1:
        e["avg_st"] = nums[0]
    if len(nums) >= 4:
        e["national_2rate"] = nums[2]
        e["national_3rate"] = nums[3]
    if len(nums) >= 7:
        e["local_2rate"] = nums[5]
        e["local_3rate"] = nums[6]
    if len(nums) >= 9:
        e["motor_2rate"] = nums[7]
        e["motor_3rate"] = nums[8]
    if len(nums) >= 11:
        e["boat_2rate"] = nums[9]
        e["boat_3rate"] = nums[10]

    return e

def parse_entries(soup: BeautifulSoup) -> list[dict[str, Any]]:
    lines = soup_lines(soup)

    # 出走表ヘッダー以降だけを見る
    start = 0
    for i, line in enumerate(lines):
        if line.startswith("枠 ボートレーサー"):
            start = i
            break

    frame_indexes: list[tuple[int, int]] = []
    for i in range(start, len(lines)):
        frame = frame_line_to_int(lines[i])
        if frame is not None:
            # 「レース 1R 2R...」などの上部リンクを避けるため、登録番号/級別が近くにあるものだけ採用
            nearby = " ".join(lines[i:i+12])
            if re.search(r"\b\d{4}\s*/\s*(A1|A2|B1|B2)\b", nearby):
                frame_indexes.append((frame, i))

    entries: list[dict[str, Any]] = []
    for idx, (frame, start_i) in enumerate(frame_indexes):
        end_i = frame_indexes[idx + 1][1] if idx + 1 < len(frame_indexes) else len(lines)
        block = lines[start_i:end_i]
        entries.append(parse_block(frame, block))

    # 1〜6が取れていれば返す
    by_frame = {e["frame"]: e for e in entries}
    final = [by_frame.get(i, empty_entry(i)) for i in range(1, 7)]
    return final

def to_float(v: Any, default: float = 0.0) -> float:
    try:
        s = str(v).replace("%", "")
        if s.startswith("."):
            s = "0" + s
        return float(s)
    except Exception:
        return default

def avg(vals: list[float]) -> float:
    vals = [v for v in vals if v > 0]
    return sum(vals) / len(vals) if vals else 0

def analyze(entries: list[dict[str, Any]]) -> dict[str, Any]:
    e1 = entries[0] if entries else {}
    st1 = to_float(e1.get("avg_st"), 0.16)
    n3 = to_float(e1.get("national_3rate"), 50)
    l3 = to_float(e1.get("local_3rate"), 50)
    m2 = to_float(e1.get("motor_2rate"), 35)
    m3 = to_float(e1.get("motor_3rate"), 45)

    trust = 55
    trust += (n3 - 50) * 0.25
    trust += (l3 - 50) * 0.15
    trust += (m2 - 35) * 0.18
    trust += (m3 - 45) * 0.12
    trust += (0.16 - st1) * 120
    trust = int(max(40, min(90, round(trust))))

    rough = int(max(20, min(88, 95 - trust)))
    if m2 < 25 or m3 < 35:
        rough = min(88, rough + 8)

    roles = {
        1: "イン信頼度",
        2: "差し脅威度＋壁性能",
        3: "攻撃脅威度＋3着残り",
        4: "カド攻撃度",
        5: "展開突入度",
        6: "穴絡み度",
    }

    risks = []
    for i, e in enumerate(entries, start=1):
        score = avg([
            to_float(e.get("national_3rate")),
            to_float(e.get("local_3rate")),
            to_float(e.get("motor_3rate")),
            to_float(e.get("boat_3rate")),
        ])
        if i == 1:
            score = trust
        rank = "A" if score >= 70 else "B+" if score >= 60 else "B" if score >= 50 else "C+"
        risks.append({
            "boat": i,
            "role": roles[i],
            "rank": rank,
            "risk_note": f"{roles[i]}として評価。平均ST・選手成績・当地・モーター/ボートを確認。"
        })

    score_map = {"S":100,"S-":96,"A+":94,"A":90,"A-":86,"B+":78,"B":72,"B-":66,"C+":58,"C":52,"C-":46,"D":35}
    bonus = {1:6,2:7,3:5,4:4,5:2,6:1}
    top = sorted(risks, key=lambda x: -(score_map.get(x["rank"], 0) + bonus.get(x["boat"], 0)))[:4]
    top4 = [{"boat": x["boat"], "label": x["role"], "rank": x["rank"], "reason": x["risk_note"]} for x in top]

    if trust >= 72:
        main = ["1-2-3","1-3-2","1-2-4","1-4-2","1-3-4"]
        osa = ["2-1-3","1-5-3","1-3-5"]
    elif trust >= 62:
        main = ["1-2-3","1-3-2","1-2-4","1-4-2","2-1-3"]
        osa = ["2-1-4","3-1-4","1-5-2"]
    else:
        main = ["1-2-3","2-1-3","1-3-2","3-1-2","2-1-4"]
        osa = ["4-1-5","5-1-2","2-5-1"]

    danger = [{"boat": 1, "reason": "1号艇信頼度がやや低く、イン人気の場合は過信注意"}] if trust < 65 else [{"boat": 2, "reason": "人気順だけでなくST・モーター・展示のズレを確認"}]

    return {
        "first_boat_reliability": trust,
        "roughness": rough,
        "risk_level": "B+（本命寄り）" if trust >= 72 else ("B（標準）" if trust >= 62 else "B-（1号艇過信注意）"),
        "danger_popular_boats": danger,
        "boat_risks": risks,
        "top4_boats": top4,
        "main_tickets": main,
        "hole_tickets": osa,
        "reserve_tickets": ["1-2-6","1-6-2","2-3-1","3-4-1","4-5-1"],
    }

def parse_race(date: str, jcd: str, rno: int, html: str) -> dict[str, Any] | None:
    soup = BeautifulSoup(html, "html.parser")
    lines = soup_lines(soup)
    text = " ".join(lines)

    if len(text) < 500 or ("出走表" not in text and "モーター" not in text and "平均ST" not in text):
        return None

    place = JCD_MAP[jcd]
    entries = parse_entries(soup)
    analysis = analyze(entries)
    grade = race_grade(text)
    tz = TIME_ZONE_BY_PLACE.get(place, "デイ")
    summary = f"{place}{rno}Rは、1号艇信頼度{analysis['first_boat_reliability']}、荒れ度{analysis['roughness']}。AI上位4艇と艇別リスクを確認。"

    return {
        "date": date_hyphen(date),
        "place": place,
        "race_no": f"{rno}R",
        "deadline": parse_deadline(lines, rno),
        "event_title": "",
        "grade": grade,
        "event_grade": grade,
        "time_zone": tz,
        "distance": "1800m",
        "status": "分析済み",
        "detail_level": "github_actions_line_parser_v1",
        "entries": entries,
        **analysis,
        "summary": summary,
        "analysis_text": f"GitHub Actionsで公式開催一覧から開催場を自動判定し、出走表主要データを取得。{summary}",
        "result": {"trifecta":"","full_order":[],"payout":None,"popularity":None,"hit_classification":"","review_note":""},
    }

def main() -> None:
    date = target_date()
    jcds = target_jcds(date)
    log(f"[INFO] target_date={date} target_venues={len(jcds)} target_pages={len(jcds) * 12}")

    races = []
    debug = []

    for jcd in jcds:
        place = JCD_MAP[jcd]
        for rno in range(1, 13):
            html = fetch_racelist_html(date, jcd, rno)
            if not html:
                continue

            item = parse_race(date, jcd, rno, html)
            if item:
                races.append(item)
                names = sum(1 for e in item["entries"] if e.get("racer_name"))
                line = f"[OK] {place} {rno}R names={names} deadline={item.get('deadline','')}"
            else:
                line = f"[SKIP] {place} {rno}R"

            log(line)
            debug.append(line)
            time.sleep(0.2)

    Path("race-data.json").write_text(json.dumps(races, ensure_ascii=False, indent=2), encoding="utf-8")
    Path("debug-race-data.txt").write_text("\n".join(debug), encoding="utf-8")
    log(f"created race-data.json races={len(races)}")

if __name__ == "__main__":
    main()
