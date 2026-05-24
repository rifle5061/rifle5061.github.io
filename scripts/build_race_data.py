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
        "User-Agent": "Mozilla/5.0 (compatible; BoatAIRiskLab/0.7; +https://rifle5061.github.io/boat-ai-risk-lab/)"
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
            time.sleep(1.5 * attempt)

    log(f"[FETCH_NG] {url} {last_error}")
    return None

def target_jcds_from_official_index(date: str) -> list[str]:
    """
    BOATRACE公式の本日/指定日のレース一覧ページから開催場コードを抽出する。
    ここで取れた場だけ1R〜12Rを取得するので、無駄な24場巡回を避ける。
    """
    urls = [
        f"{BASE_INDEX_URL}?hd={date}",
        BASE_INDEX_URL,
    ]

    for url in urls:
        html = fetch_url(url, timeout=12)
        if not html:
            continue

        # 公式ページ内のリンクから jcd=XX を拾う
        found = sorted(set(re.findall(r"[?&]jcd=(\d{2})", html)))
        found = [x for x in found if x in JCD_MAP]

        if found:
            places = [JCD_MAP[x] for x in found]
            log(f"[INFO] target venues from official index: {', '.join(places)}")
            return found

        # 念のため、場名が本文に出ている場合も拾う
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
    # 1. 公式の開催中/開催日一覧から判定
    jcds = target_jcds_from_official_index(date)
    if jcds:
        return jcds

    # 2. 公式一覧が取れない場合は、既存race-data.jsonの場だけ
    jcds = target_jcds_from_existing_racedata(date)
    if jcds:
        return jcds

    # 3. 最後の保険だけ全場
    log("[WARN] no target venues found. fallback to all 24 venues.")
    return list(JCD_MAP.keys())

def fetch_racelist_html(date: str, jcd: str, rno: int) -> str | None:
    url = f"{BASE_RACELIST_URL}?hd={date}&jcd={jcd}&rno={rno}"
    return fetch_url(url, timeout=25)

def parse_deadline(text: str) -> str:
    for pat in [
        r"締切予定時刻\s*([0-9]{1,2}:[0-9]{2})",
        r"締切予定\s*([0-9]{1,2}:[0-9]{2})",
        r"締切\s*([0-9]{1,2}:[0-9]{2})",
    ]:
        m = re.search(pat, text)
        if m:
            return m.group(1)
    times = re.findall(r"\b([0-2]?[0-9]:[0-5][0-9])\b", text)
    return times[0] if times else ""

def race_grade(text: str) -> str:
    upper = text.upper()
    if "SG" in upper:
        return "SG"
    if "G1" in upper or "Ｇ１" in upper:
        return "G1"
    if "G2" in upper or "Ｇ２" in upper:
        return "G2"
    if "G3" in upper or "Ｇ３" in upper:
        return "G3"
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

def cell_lines(cell) -> list[str]:
    return [clean(x) for x in cell.get_text("\n").split("\n") if clean(x)]

def parse_row(cells: list[list[str]], frame: int) -> dict[str, Any]:
    e = empty_entry(frame)
    flat = [x for c in cells for x in c]
    joined = " ".join(flat)

    m = re.search(r"\b(A1|A2|B1|B2)\b", joined)
    if m:
        e["class"] = m.group(1)

    branches = ["群馬","埼玉","東京","静岡","愛知","三重","福井","滋賀","大阪","兵庫","徳島","香川","岡山","広島","山口","福岡","佐賀","長崎"]
    for b in branches:
        if b in flat:
            e["branch"] = b
            break

    for token in flat:
        if re.fullmatch(r"[一-龥ぁ-んァ-ンー・]{2,10}", token):
            if token not in branches and token not in ["全国","当地","モーター","ボート","勝率","2連率","3連率"]:
                e["racer_name"] = token
                break

    f = re.search(r"F\s*([0-9])", joined)
    l = re.search(r"L\s*([0-9])", joined)
    if f:
        e["f_count"] = f.group(1)
    if l:
        e["l_count"] = l.group(1)

    st = re.findall(r"(?<![0-9])\.([0-9]{2})(?![0-9])", joined)
    if st:
        e["avg_st"] = "." + st[0]

    rates = re.findall(r"(?<![0-9])([0-9]{1,3}\.[0-9]{2})(?![0-9])", joined)
    if len(rates) >= 12:
        e["national_2rate"] = rates[1]
        e["national_3rate"] = rates[2]
        e["local_2rate"] = rates[4]
        e["local_3rate"] = rates[5]
        e["motor_2rate"] = rates[7]
        e["motor_3rate"] = rates[8]
        e["boat_2rate"] = rates[10]
        e["boat_3rate"] = rates[11]
    elif len(rates) >= 8:
        e["national_2rate"] = rates[0]
        e["national_3rate"] = rates[1]
        e["local_2rate"] = rates[2]
        e["local_3rate"] = rates[3]
        e["motor_2rate"] = rates[4]
        e["motor_3rate"] = rates[5]
        e["boat_2rate"] = rates[6]
        e["boat_3rate"] = rates[7]

    return e

def parse_entries(soup: BeautifulSoup) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []

    for table in soup.find_all("table"):
        for tr in table.find_all("tr"):
            raw_cells = tr.find_all(["td", "th"])
            if len(raw_cells) < 4:
                continue

            cells = [cell_lines(c) for c in raw_cells]
            flat = [x for c in cells for x in c]
            if not flat:
                continue

            first_text = " ".join(cells[0]) if cells else ""
            m = re.search(r"^[\s　]*([1-6])[\s　]*$", first_text)
            if not m:
                m = re.search(r"^[\s　]*([1-6])[\s　]", " ".join(flat))
            if not m:
                continue

            frame = int(m.group(1))
            joined = " ".join(flat)

            if not (re.search(r"\b(A1|A2|B1|B2)\b", joined) or re.search(r"\.[0-9]{2}", joined) or "モーター" in joined):
                continue

            if frame not in [x["frame"] for x in candidates]:
                candidates.append(parse_row(cells, frame))

    candidates.sort(key=lambda x: x["frame"])
    if len(candidates) >= 6:
        return candidates[:6]

    return [empty_entry(i) for i in range(1, 7)]

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
            "risk_note": f"{roles[i]}として評価。ST・選手成績・当地・モーター/ボートを確認。"
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
    text = clean(soup.get_text("\n"))

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
        "deadline": parse_deadline(text),
        "event_title": "",
        "grade": grade,
        "event_grade": grade,
        "time_zone": tz,
        "distance": "1800m",
        "status": "分析済み",
        "detail_level": "github_actions_auto_venues_v1",
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
