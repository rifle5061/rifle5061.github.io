from __future__ import annotations
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

DATA_PATH = Path("race-data.json")
DEBUG_PATH = Path("debug-enrich-race-data.txt")

def f(v: Any, d: float = 0.0) -> float:
    try:
        s = str(v).strip()
        if s in ("", "-", "None", "null"):
            return d
        return float(s)
    except Exception:
        return d

def c(v: float, lo: int = 0, hi: int = 100) -> int:
    return int(max(lo, min(hi, round(v))))

def st(e: dict[str, Any], d: float = 0.18) -> float:
    return f(e.get("avg_st"), d)

def rank(score: float) -> str:
    if score >= 88: return "S"
    if score >= 82: return "A+"
    if score >= 75: return "A"
    if score >= 68: return "A-"
    if score >= 61: return "B+"
    if score >= 54: return "B"
    if score >= 47: return "B-"
    if score >= 40: return "C+"
    return "C"

def role(b: int) -> str:
    return {
        1: "イン信頼度",
        2: "差し脅威度＋壁性能",
        3: "攻撃脅威度＋3着残り",
        4: "カド攻撃度",
        5: "展開突入度",
        6: "穴絡み度",
    }.get(b, "艇別評価")

def score(e: dict[str, Any], b: int) -> float:
    n3, l3 = f(e.get("national_3rate"), 45), f(e.get("local_3rate"), 45)
    m2, m3 = f(e.get("motor_2rate"), 30), f(e.get("motor_3rate"), 40)
    b3 = f(e.get("boat_3rate"), 40)
    fb = {1:16, 2:11, 3:7, 4:4, 5:0, 6:-4}.get(b, 0)
    sb = (0.18 - st(e)) * 120
    return max(20, min(100, n3*.24 + l3*.16 + m2*.18 + m3*.24 + b3*.10 + fb + sb))

def valid_ticket(t: str) -> bool:
    p = t.split("-")
    return len(p) == 3 and len(set(p)) == 3 and all(x in {"1","2","3","4","5","6"} for x in p)

def uniq(tickets: list[str], limit: int) -> list[str]:
    out = []
    for t in tickets + ["1-2-3","1-3-2","1-2-4","1-4-2","2-1-3","3-1-2","1-5-3","2-3-1","4-1-5","5-1-2"]:
        if valid_ticket(t) and t not in out:
            out.append(t)
        if len(out) >= limit:
            break
    return out

def tickets(top: list[int], trust: int, skip: int) -> tuple[list[str], list[str]]:
    a = top[0] if len(top) > 0 else 1
    b = top[1] if len(top) > 1 else 2
    cc = top[2] if len(top) > 2 else 3
    d = top[3] if len(top) > 3 else 4
    if trust >= 72:
        main = [f"1-{b}-{cc}", f"1-{cc}-{b}", f"1-{b}-{d}", f"1-{d}-{b}", f"1-{cc}-{d}"]
        osa = [f"{b}-1-{cc}", f"1-5-{b}", f"{cc}-1-{b}"]
    elif trust >= 62:
        main = [f"1-{b}-{cc}", f"1-{cc}-{b}", f"{b}-1-{cc}", f"1-{b}-{d}", f"{cc}-1-{b}"]
        osa = [f"{b}-{cc}-1", f"{cc}-{b}-1", f"1-5-{cc}"]
    else:
        atk = a if a != 1 else b
        sec = b if b != atk else cc
        main = [f"{atk}-1-{sec}", f"1-{atk}-{sec}", f"{atk}-{sec}-1", f"1-{sec}-{atk}", f"{sec}-1-{atk}"]
        osa = ["4-1-5", f"5-1-{atk}", f"{atk}-5-1"]
    return uniq(main, 5), uniq(osa, 3)

def enrich_race(race: dict[str, Any]) -> dict[str, Any]:
    r = deepcopy(race)
    es = r.get("entries", [])
    if not isinstance(es, list) or len(es) < 6:
        return r

    scores = {i: score(e, i) for i, e in enumerate(es, start=1)}
    e1 = es[0]
    trust = 53
    trust += (f(e1.get("national_3rate"), 50)-50)*.22
    trust += (f(e1.get("local_3rate"), 50)-50)*.15
    trust += (f(e1.get("motor_2rate"), 35)-35)*.16
    trust += (f(e1.get("motor_3rate"), 45)-45)*.13
    trust += (f(e1.get("boat_3rate"), 45)-45)*.06
    trust += (0.16-st(e1, .17))*125

    outside = max(scores.get(2,50), scores.get(3,50), scores.get(4,50))
    if outside >= 75: trust -= 6
    elif outside >= 68: trust -= 3
    if f(e1.get("motor_2rate"), 35) < 25 or f(e1.get("motor_3rate"), 45) < 35: trust -= 8
    elif f(e1.get("motor_2rate"), 35) < 30: trust -= 4
    trust = c(trust, 35, 92)

    rough = 100 - trust + max(0, outside-65)*.35
    if f(e1.get("motor_2rate"), 35) < 28 or f(e1.get("motor_3rate"), 45) < 38: rough += 8
    rough = c(rough, 15, 90)

    motor_over = 0
    for b, e in enumerate(es, start=1):
        if (f(e.get("motor_2rate")) >= 45 or f(e.get("motor_3rate")) >= 55) and (b >= 4 or f(e.get("national_3rate")) < 45 or st(e) >= .18):
            motor_over += 12
    motor_over = c(motor_over)

    st_trap = 0
    sts = {i: st(e) for i, e in enumerate(es, start=1)}
    for b in range(2, 7):
        if sts[b] <= .15 and (sts[b-1] - sts[b]) < .03: st_trap += 8
        if b >= 5 and sts[b] <= .15: st_trap += 5
    st_trap = c(st_trap)

    collapse = 25
    if trust < 65: collapse += 18
    if scores.get(2,50) < 55: collapse += 10
    if scores.get(3,50) >= 68: collapse += 10
    if scores.get(4,50) >= 68: collapse += 9
    if max(scores.get(5,45), scores.get(6,40)) >= 62: collapse += 6
    collapse = c(collapse, 10, 95)

    skip = c(rough*.45 + collapse*.35 + motor_over*.20)
    risks = []
    for b, e in enumerate(es, start=1):
        sc = c(scores[b])
        n3, l3 = f(e.get("national_3rate")), f(e.get("local_3rate"))
        m2, m3, b3 = f(e.get("motor_2rate")), f(e.get("motor_3rate")), f(e.get("boat_3rate"))
        av = e.get("avg_st", "-")
        if b == 1:
            note = f"1号艇信頼度{trust}。平均ST{av}、全国3連率{n3:.2f}、当地3連率{l3:.2f}、M2/M3={m2:.2f}/{m3:.2f}。2〜4号艇の攻撃圧も加味。"
        elif b == 2:
            note = f"差し脅威度と壁性能を評価。平均ST{av}、M2/M3={m2:.2f}/{m3:.2f}。1号艇を守れるか、3号艇の攻めを止められるかが焦点。"
        elif b == 3:
            note = f"攻撃脅威度＋3着残り評価。平均ST{av}、全国/当地3連率={n3:.2f}/{l3:.2f}、M3={m3:.2f}。"
        elif b == 4:
            note = f"カド攻撃度評価。平均ST{av}、M2/M3={m2:.2f}/{m3:.2f}。自力攻め/展開待ちを確認。"
        elif b == 5:
            note = f"展開突入度評価。平均ST{av}、M3={m3:.2f}、B3={b3:.2f}。2・3着の展開突入候補。"
        else:
            note = f"穴絡み度評価。平均ST{av}、M3={m3:.2f}、B3={b3:.2f}。隊形崩壊時の3着穴を確認。"
        risks.append({"boat": b, "role": role(b), "rank": rank(sc), "score": sc, "risk_note": note})

    top = sorted(risks, key=lambda x: (-x["score"], x["boat"]))
    top4 = [{"boat": x["boat"], "label": x["role"], "rank": x["rank"], "score": x["score"], "reason": x["risk_note"]} for x in top[:4]]

    danger = []
    if trust < 65:
        danger.append({"boat": 1, "reason": f"1号艇信頼度{trust}で過信注意。イン人気でもモーター・平均ST・2〜4号艇の攻撃圧を確認。"})
    for x in top:
        b = x["boat"]
        if b == 1: continue
        e = es[b-1]
        if x["score"] >= 68 and b >= 5:
            danger.append({"boat": b, "reason": f"{b}号艇はデータ評価高めだが外枠不利あり。人気するなら過信注意。"})
        elif x["score"] >= 70 and st(e) >= .18:
            danger.append({"boat": b, "reason": f"{b}号艇は総合評価は高いが平均STが遅め。人気するなら隊形確認。"})
        if len(danger) >= 3: break
    if not danger:
        danger.append({"boat": top[0]["boat"] if top else 1, "reason": "オッズ未取得のため人気ズレは未反映。人気順だけでなくST・モーター・枠のズレを確認。"})

    top_boats = [x["boat"] for x in top]
    main, osa = tickets(top_boats, trust, skip)

    risk_level = "B+（本命寄り）" if trust >= 72 else ("B（標準）" if trust >= 62 else ("C（見送り寄り）" if skip >= 70 else "B-（1号艇過信注意）"))
    hit = c(82 - rough*.45 - skip*.25 + (10 if trust >= 72 else 0), 25, 82)
    hit_label = "高" if hit >= 70 else ("中" if hit >= 45 else "低")

    r.update({
        "detail_level": "ai_detailed_risk_auto_v1",
        "first_boat_reliability": trust,
        "roughness": rough,
        "risk_level": risk_level,
        "motor_overvalue_risk": motor_over,
        "average_st_trap_index": st_trap,
        "development_collapse_risk": collapse,
        "skip_index": skip,
        "hit_rate_index": hit,
        "hit_rate_label": hit_label,
        "popularity_gap_index": None,
        "popularity_gap_note": "オッズ未取得のため未計算",
        "danger_popular_boats": danger,
        "boat_risks": risks,
        "top4_boats": top4,
        "main_tickets": main,
        "hole_tickets": osa,
        "summary": f"{r.get('place','')}{r.get('race_no','')}は、1号艇信頼度{trust}、荒れ度{rough}、見送り指数{skip}。展開崩壊リスク{collapse}、モーター過大評価リスク{motor_over}。オッズ・展示後補正は未反映のため、直前情報と人気順で最終確認。",
        "analysis_text": f"出走表データをAIルールで再評価したサイト用詳細リスク。予想的中率指数{hit}%（{hit_label}）、平均STトラップ指数{st_trap}、展開崩壊リスク{collapse}、見送り指数{skip}。危険人気はオッズ未取得のため『人気した場合の注意候補』として表示。",
    })
    return r

def main() -> None:
    if not DATA_PATH.exists():
        raise FileNotFoundError("race-data.json が見つかりません")
    raw = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    is_dict = isinstance(raw, dict) and isinstance(raw.get("races"), list)
    races = raw["races"] if is_dict else raw
    if not isinstance(races, list):
        raise ValueError("race-data.json の形式が想定外です")

    enhanced = [enrich_race(r) for r in races]
    if is_dict:
        raw["races"] = enhanced
        output = raw
    else:
        output = enhanced
    DATA_PATH.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")

    dates = sorted({str(r.get("date","")) for r in enhanced if r.get("date")})
    places = sorted({str(r.get("place","")) for r in enhanced if r.get("place")})
    detailed = sum(1 for r in enhanced if r.get("detail_level") == "ai_detailed_risk_auto_v1")
    warnings = []
    for r in enhanced:
        es = r.get("entries", [])
        if len(es) != 6:
            warnings.append(f"{r.get('place')} {r.get('race_no')} entries={len(es)}")
        miss = [str(e.get("frame","?")) for e in es if not e.get("racer_name")]
        if miss:
            warnings.append(f"{r.get('place')} {r.get('race_no')} 選手名なし={','.join(miss)}")

    lines = [
        "===== ENRICH RACE DATA CHECK =====",
        f"総レース数: {len(enhanced)}",
        f"日付: {', '.join(dates)}",
        f"場数: {len(places)}",
        f"詳細リスク化: {detailed}",
        f"警告: {len(warnings)}",
    ]
    if warnings:
        lines.append("----- WARNINGS -----")
        lines.extend(warnings[:100])
    DEBUG_PATH.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))

if __name__ == "__main__":
    main()
