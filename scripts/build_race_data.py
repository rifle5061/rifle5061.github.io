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

TIME_ZONE_BY_PLACE = {
    "三国": "モーニング", "徳山": "モーニング", "芦屋": "モーニング", "唐津": "モーニング",
    "桐生": "ナイター", "蒲郡": "ナイター", "住之江": "ナイター", "丸亀": "ナイター", "若松": "ナイター", "大村": "ナイター",
    "下関": "ミッドナイト",
}

BASE_RACELIST_URL = "https://www.boatrace.jp/owpc/pc/race/racelist"
BASE_INDEX_URL = "https://www.boatrace.jp/owpc/pc/race/index"

BRANCHES = ["群馬","埼玉","東京","静岡","愛知","三重","福井","滋賀","大阪","兵庫","徳島","香川","岡山","広島","山口","福岡","佐賀","長崎"]
ZEN_NUM = {"１": 1, "２": 2, "３": 3, "４": 4, "５": 5, "６": 6}
MAX_WORKERS = 6

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
    return re.sub(r"\s+", " ", (s or "").replace("\u3000", " ")).strip()

def fetch_url(url: str, timeout: int = 12) -> str | None:
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; BoatAIRiskLab/1.0; +https://rifle5061.github.io/boat-ai-risk-lab/)"
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

def target_jcds_from_official_index(date: str) -> list[str]:
    urls = [
        f"{BASE_INDEX_URL}?hd={date}",
        BASE_INDEX_URL,
    ]

    for url in urls:
        html = fetch_url(url, timeout=15)
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
    return fetch_url(url, timeout=12)

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
        "motor_no": "", "motor_2rate": "", "motor_3rate": "",
        "boat_no_data": "", "boat_2rate": "", "boat_3rate": "",
        "exhibition_st": "", "exhibition_time": "",
    }

def decimals(line: str) -> list[str]:
    return re.findall(r"(?<![0-9])(?:\d+\.\d+|\.\d{2})(?![0-9])", line)

def is_name_candidate(line: str) -> bool:
    if not line:
        return False
    if line in BRANCHES:
        return False
    if "/" in line:
        return False
    if re.search(r"\d", line):
        return False
    if line in ["写真", "登録番号/級別", "氏名", "支部/出身地", "年齢/体重", "F数", "L数",
                "平均ST", "勝率", "2連率", "3連率", "全国", "当地", "モーター", "ボート", "初日", "最終日"]:
        return False
    # 例: 今泉 徹 / 松下 誉士 / 菅沼 佳昭
    return bool(re.fullmatch(r"[一-龥ぁ-んァ-ンー・]{1,6}(?:\s+[一-龥ぁ-んァ-ンー・]{1,6}){0,2}", line))

def parse_entry_by_class_line(frame: int, block: list[str]) -> dict[str, Any]:
    e = empty_entry(frame)

    if not block:
        return e

    m = re.search(r"\b(\d{4})\s*/\s*(A1|A2|B1|B2)\b", block[0])
    if m:
        e["class"] = m.group(2)

    # 選手名：登録番号/級別の次以降で最初の日本語名候補
    for line in block[1:12]:
        if is_name_candidate(line):
            e["racer_name"] = line
            break

    # 支部
    for line in block[1:18]:
        if "/" in line:
            left = clean(line.split("/", 1)[0])
            if left in BRANCHES:
                e["branch"] = left
                break

    # F/L
    for line in block[1:24]:
        if re.fullmatch(r"F\d+", line):
            e["f_count"] = line.replace("F", "")
        if re.fullmatch(r"L\d+", line):
            e["l_count"] = line.replace("L", "")

    # 平均ST行を起点に数字を取得
    avg_idx = None
    nums: list[str] = []
    for i, line in enumerate(block):
        if re.search(r"(?<![0-9])0\.\d{2}(?![0-9])", line):
            avg_idx = i
            break

    if avg_idx is not None:
        for line in block[avg_idx:]:
            # 早見や成績の直前までで十分
            if "今節成績" in line:
                break
            for n in decimals(line):
                nums.append(n)
            if len(nums) >= 11:
                break

    # 想定: [avg_st, national_win, national_2, national_3, local_win, local_2, local_3, motor_2, motor_3, boat_2, boat_3]
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

    # モーターNo/ボートNoは「当地3連率」の後に整数が入るが、初期版では無理に使わない
    return e




def is_frame_token(s: str) -> bool:
    s = clean(s)
    return s in {"1","2","3","4","5","6","１","２","３","４","５","６"}

def frame_token_to_int(s: str) -> int:
    s = clean(s)
    if s in ZEN_NUM:
        return ZEN_NUM[s]
    return int(s)

def is_reg_no(s: str) -> bool:
    return bool(re.fullmatch(r"\d{4}", clean(s)))

def is_class_token(s: str) -> bool:
    return bool(re.fullmatch(r"A1|A2|B1|B2", clean(s)))

def is_branch_line(s: str) -> bool:
    s = clean(s)
    if "/" not in s:
        return False
    left = s.split("/", 1)[0]
    return left in BRANCHES

def is_age_weight_line(s: str) -> bool:
    return bool(re.search(r"\d+歳\s*/\s*\d+(?:\.\d+)?kg", clean(s)))

def is_fl_line(s: str, prefix: str) -> bool:
    return bool(re.fullmatch(prefix + r"\d+", clean(s)))

def next_nonempty(lines: list[str], idx: int) -> str:
    return lines[idx] if 0 <= idx < len(lines) else ""

def set_number_fields_from_sequence(e: dict[str, Any], vals: list[str]) -> dict[str, Any]:
    # vals想定：
    # avgST, 全国勝率, 全国2連率, 全国3連率,
    # 当地勝率, 当地2連率, 当地3連率,
    # モーターNo, モーター2連率, モーター3連率,
    # ボートNo, ボート2連率, ボート3連率
    if len(vals) >= 1:
        e["avg_st"] = vals[0]
    if len(vals) >= 4:
        e["national_2rate"] = vals[2]
        e["national_3rate"] = vals[3]
    if len(vals) >= 7:
        e["local_2rate"] = vals[5]
        e["local_3rate"] = vals[6]
    if len(vals) >= 10:
        e["motor_no"] = vals[7]
        e["motor_2rate"] = vals[8]
        e["motor_3rate"] = vals[9]
    if len(vals) >= 13:
        e["boat_no_data"] = vals[10]
        e["boat_2rate"] = vals[11]
        e["boat_3rate"] = vals[12]
    return e

def parse_entries_by_vertical_lines(soup: BeautifulSoup) -> list[dict[str, Any]]:
    """
    BOATRACE公式のスマホ/テキスト化状態では、
    1艇ごとに
    枠 → 登録番号 → / → 級別 → 氏名 → 支部/出身地 → 年齢/体重 → F → L → 平均ST → 勝率...
    と縦に並ぶ。
    これをそのまま読む方式。names=5や平均ST '-' の欠損対策。
    """
    lines = soup_lines(soup)

    # 出走表ヘッダー以降だけを見る
    start = 0
    for i, line in enumerate(lines):
        if line == "枠" and i + 1 < len(lines):
            start = i
            break

    entries: list[dict[str, Any]] = []
    used_indexes = set()

    for i in range(start, len(lines) - 12):
        if i in used_indexes:
            continue

        if not is_frame_token(lines[i]):
            continue

        frame = frame_token_to_int(lines[i])
        if frame < 1 or frame > 6:
            continue

        # 直後に 登録番号 / 級別 が並ぶ場所だけ採用
        if not is_reg_no(next_nonempty(lines, i + 1)):
            continue
        if clean(next_nonempty(lines, i + 2)) != "/":
            continue
        if not is_class_token(next_nonempty(lines, i + 3)):
            continue

        name = clean(next_nonempty(lines, i + 4))
        branch_line = clean(next_nonempty(lines, i + 5))
        age_line = clean(next_nonempty(lines, i + 6))
        f_line = clean(next_nonempty(lines, i + 7))
        l_line = clean(next_nonempty(lines, i + 8))

        if not name or not is_branch_line(branch_line):
            continue
        if not is_age_weight_line(age_line):
            continue
        if not is_fl_line(f_line, "F") or not is_fl_line(l_line, "L"):
            continue

        e = empty_entry(frame)
        e["class"] = clean(lines[i + 3])
        e["racer_name"] = name
        e["branch"] = branch_line.split("/", 1)[0]
        e["f_count"] = f_line.replace("F", "")
        e["l_count"] = l_line.replace("L", "")

        vals: list[str] = []
        # L数の次から、平均STや各率を13個分拾う。平均STが「-」の場合も保存する。
        j = i + 9
        while j < len(lines) and len(vals) < 13:
            s = clean(lines[j])

            # 次の艇に入ったら止める
            if len(vals) > 0 and is_frame_token(s):
                if j + 3 < len(lines) and is_reg_no(lines[j + 1]) and clean(lines[j + 2]) == "/" and is_class_token(lines[j + 3]):
                    break

            # 数値または "-" を採用。モーターNo/ボートNoは整数も採用。
            if s == "-" or re.fullmatch(r"\d+(?:\.\d+)?", s) or re.fullmatch(r"\.\d{2}", s):
                vals.append(s)

            j += 1

        e = set_number_fields_from_sequence(e, vals)
        entries.append(e)
        used_indexes.update(range(i, j))

        if len(entries) >= 6:
            break

    # 6艇取れたら順番で1〜6へ正規化。途中抜けがあれば枠番号優先。
    if len(entries) == 6:
        entries.sort(key=lambda x: x.get("frame", 99))
        for idx, e in enumerate(entries, start=1):
            e["frame"] = idx
            e["boat_no"] = idx

    by_frame = {e.get("frame"): e for e in entries}
    return [by_frame.get(i, empty_entry(i)) for i in range(1, 7)]

def parse_entries_by_regex_backup(soup: BeautifulSoup) -> list[dict[str, Any]]:
    """
    縦読みが失敗した時の保険。本文全体から登録番号/級別/氏名/支部を拾う。
    """
    lines = soup_lines(soup)
    text = " ".join(lines)
    frame_chars = "1-6１-６"
    branch_pat = "|".join(BRANCHES)
    pattern = re.compile(
        rf"([{frame_chars}])\s+"
        rf"(\d{{4}})\s*/\s*(A1|A2|B1|B2)\s+"
        rf"([一-龥ぁ-んァ-ンー・\s]{{2,28}}?)\s+"
        rf"(({branch_pat})/[^ ]+)"
    )
    matches = list(pattern.finditer(text))[:6]
    entries = []
    for pos, m in enumerate(matches, start=1):
        e = empty_entry(pos)
        e["class"] = m.group(3)
        e["racer_name"] = clean(m.group(4))
        e["branch"] = m.group(6)
        block_start = m.end()
        block_end = matches[pos].start() if pos < len(matches) else len(text)
        block = text[block_start:block_end]
        f = re.search(r"\bF(\d+)\b", block)
        l = re.search(r"\bL(\d+)\b", block)
        if f: e["f_count"] = f.group(1)
        if l: e["l_count"] = l.group(1)
        # 数字の順番補完
        nums = re.findall(r"(?<![0-9])(?:\d+\.\d+|\.\d{2})(?![0-9])", block)
        avg_idx = None
        for k, n in enumerate(nums):
            try:
                v = float("0" + n if n.startswith(".") else n)
                if 0.01 <= v <= 0.40:
                    avg_idx = k
                    break
            except Exception:
                pass
        if avg_idx is not None:
            data_nums = nums[avg_idx:]
            vals = data_nums[:13]
            e = set_number_fields_from_sequence(e, vals)
        entries.append(e)
    by = {e["frame"]: e for e in entries}
    return [by.get(i, empty_entry(i)) for i in range(1, 7)]

def merge_entries(primary: list[dict[str, Any]], backup: list[dict[str, Any]]) -> list[dict[str, Any]]:
    keys = [
        "racer_name","class","branch","f_count","l_count","avg_st",
        "national_2rate","national_3rate","local_2rate","local_3rate",
        "motor_no","motor_2rate","motor_3rate","boat_no_data","boat_2rate","boat_3rate",
        "exhibition_st","exhibition_time"
    ]
    out = []
    for boat in range(1, 7):
        base = primary[boat-1] if len(primary) >= boat else empty_entry(boat)
        b = backup[boat-1] if len(backup) >= boat else empty_entry(boat)
        for k in keys:
            if not base.get(k) and b.get(k):
                base[k] = b[k]
        base["frame"] = boat
        base["boat_no"] = boat
        out.append(base)
    return out

def parse_entries(soup: BeautifulSoup) -> list[dict[str, Any]]:
    primary = parse_entries_by_vertical_lines(soup)
    backup = parse_entries_by_regex_backup(soup)
    return merge_entries(primary, backup)


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


def extract_source_snippet(soup: BeautifulSoup) -> str:
    """
    欠損原因確認用。
    出走表の周辺テキストを最大260行だけ保存する。
    race-data.json本体には残さず、debug-source-snippets.txt用に使う。
    """
    lines = soup_lines(soup)

    start = 0
    for i, line in enumerate(lines):
        if "枠" in line and ("ボートレーサー" in line or "登録番号" in line):
            start = i
            break

    end = min(len(lines), start + 260)
    for i in range(start + 1, len(lines)):
        if "今節成績" in lines[i] or "早見" in lines[i]:
            end = min(len(lines), i + 40)
            break

    return "\n".join(f"{idx:03d}: {line}" for idx, line in enumerate(lines[start:end], start=start))

def parse_race(date: str, jcd: str, rno: int, html: str) -> dict[str, Any] | None:
    soup = BeautifulSoup(html, "html.parser")
    lines = soup_lines(soup)
    text = " ".join(lines)

    if len(text) < 500 or ("出走表" not in text and "モーター" not in text and "平均ST" not in text):
        return None

    place = JCD_MAP[jcd]
    entries = parse_entries(soup)
    analysis = analyze(entries)
    grade = ""  # 誤SG/G1表示防止。正確な開催グレード取得までは空欄固定。
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
        "detail_level": "github_actions_vertical_line_parser_v1",
        "entries": entries,
        "_source_snippet": extract_source_snippet(soup),
        **analysis,
        "summary": summary,
        "analysis_text": f"GitHub Actionsで公式開催一覧から開催場を自動判定し、出走表主要データを取得。{summary}",
        "result": {"trifecta":"","full_order":[],"payout":None,"popularity":None,"hit_classification":"","review_note":""},
    }

def fetch_and_parse(task: tuple[str, str, int]) -> tuple[dict[str, Any] | None, str]:
    date, jcd, rno = task
    place = JCD_MAP[jcd]
    html = fetch_racelist_html(date, jcd, rno)
    if not html:
        return None, f"[NG] {place} {rno}R fetch failed"

    item = parse_race(date, jcd, rno, html)
    if not item:
        return None, f"[SKIP] {place} {rno}R"

    names = sum(1 for e in item["entries"] if e.get("racer_name"))
    line = f"[OK] {place} {rno}R names={names} deadline={item.get('deadline','')}"
    return item, line


def build_quality_report(races: list[dict[str, Any]]) -> list[str]:
    report: list[str] = []
    warn_lines: list[str] = []

    total = len(races)
    ok = 0

    for race in sorted(races, key=lambda r: (PLACE_TO_JCD.get(r.get("place",""), "99"), int(str(r.get("race_no","0R")).replace("R","") or 0))):
        entries = race.get("entries", [])
        missing_name = []
        missing_class = []
        missing_st = []

        for e in entries:
            boat = e.get("frame") or e.get("boat_no")
            if not e.get("racer_name"):
                missing_name.append(f"{boat}号艇")
            if not e.get("class"):
                missing_class.append(f"{boat}号艇")
            if not e.get("avg_st"):
                missing_st.append(f"{boat}号艇")

        if not missing_name and not missing_class and not missing_st and len(entries) == 6:
            ok += 1
        else:
            parts = []
            if missing_name:
                parts.append("選手名なし=" + ",".join(missing_name))
            if missing_class:
                parts.append("級別なし=" + ",".join(missing_class))
            if missing_st:
                parts.append("平均STなし=" + ",".join(missing_st))
            warn_lines.append(f"[CHECK_WARN] {race.get('place')} {race.get('race_no')} " + " / ".join(parts))

    report.append("===== DATA QUALITY CHECK =====")
    report.append(f"総レース数：{total}")
    report.append(f"正常：{ok}")
    report.append(f"要確認：{len(warn_lines)}")
    report.extend(warn_lines[:80])
    if len(warn_lines) > 80:
        report.append(f"... warn omitted: {len(warn_lines) - 80}")
    return report


def build_source_snippet_report(races: list[dict[str, Any]]) -> list[str]:
    """
    CHECK_WARNが出るレースだけ、生テキスト断片を別ファイルに出す。
    """
    out: list[str] = []
    out.append("===== SOURCE SNIPPETS FOR CHECK_WARN RACES =====")

    for race in sorted(races, key=lambda r: (PLACE_TO_JCD.get(r.get("place",""), "99"), int(str(r.get("race_no","0R")).replace("R","") or 0))):
        entries = race.get("entries", [])
        issues = []

        for e in entries:
            boat = e.get("frame") or e.get("boat_no")
            missing = []
            if not e.get("racer_name"):
                missing.append("選手名")
            if not e.get("class"):
                missing.append("級別")
            if not e.get("avg_st"):
                missing.append("平均ST")
            if missing:
                issues.append(f"{boat}号艇：" + "・".join(missing) + "なし")

        if issues:
            out.append("")
            out.append("--------------------------------------------------")
            out.append(f"{race.get('place')} {race.get('race_no')} / " + " / ".join(issues))
            out.append("--------------------------------------------------")
            out.append(str(race.get("_source_snippet", ""))[:12000])

    return out

def main() -> None:
    date = target_date()
    jcds = target_jcds(date)
    tasks = [(date, jcd, rno) for jcd in jcds for rno in range(1, 13)]

    log(f"[INFO] target_date={date} target_venues={len(jcds)} target_pages={len(tasks)} workers={MAX_WORKERS}")

    races: list[dict[str, Any]] = []
    debug: list[str] = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_map = {executor.submit(fetch_and_parse, task): task for task in tasks}
        for future in concurrent.futures.as_completed(future_map):
            task = future_map[future]
            try:
                item, line = future.result()
            except Exception as e:
                _, jcd, rno = task
                line = f"[ERROR] {JCD_MAP.get(jcd, jcd)} {rno}R {e}"
                item = None

            log(line)
            debug.append(line)
            if item:
                races.append(item)

    races.sort(key=lambda r: (r.get("date",""), PLACE_TO_JCD.get(r.get("place",""), "99"), int(str(r.get("race_no","0R")).replace("R","") or 0)))

    if len(races) < max(12, len(tasks) // 2):
        Path("debug-race-data.txt").write_text("\n".join(debug) + f"\n[ABORT] too few races: {len(races)}/{len(tasks)}", encoding="utf-8")
        log(f"[ABORT] too few races: {len(races)}/{len(tasks)}. race-data.json not overwritten.")
        return

    Path("debug-source-snippets.txt").write_text("\n".join(build_source_snippet_report(races)), encoding="utf-8")

    # race-data.jsonには内部確認用の生テキストを入れない
    for race in races:
        race.pop("_source_snippet", None)

    Path("race-data.json").write_text(json.dumps(races, ensure_ascii=False, indent=2), encoding="utf-8")
    debug.extend(build_quality_report(races))
    Path("debug-race-data.txt").write_text("\n".join(debug), encoding="utf-8")
    log(f"created race-data.json races={len(races)}")

if __name__ == "__main__":
    main()
