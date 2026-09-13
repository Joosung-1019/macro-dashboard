#!/usr/bin/env python3
"""FRED 13개 지표를 index.html과 data-log.json에 직접 반영한다.

fetch-fred.py가 값을 받아오기만 하던 것을 여기서 화면까지 밀어 넣는다. 그
차이가 중요하다 — 수집만 해두고 반영을 Claude에 맡기면, Claude가 한도에 걸려
죽는 날에는 13개를 받아놓고도 대시보드는 19개 전부 어제 값에 멈춘다(2026-09-08,
09-10이 그랬다). 이 스크립트를 Claude보다 먼저 돌리고 결과를 커밋하면, 그날
Claude가 한 줄도 못 써도 13개는 화면에 최신값으로 남는다.

Claude는 이후 남은 5개(dxy, vix, wti, fedwatch, nfp)만 처리한다.

안전 규칙: 패치 지점이 정확히 한 번 매칭되지 않으면 아무것도 쓰지 않고
0이 아닌 코드로 끝난다. 절반만 고쳐진 HTML을 남기느니 통째로 손을 떼고
기존 경로(Claude가 전부 처리)로 가는 편이 낫다. 워크플로에서 이 단계는
continue-on-error 이므로 실패해도 수집 자체는 계속된다.
"""

import datetime
import json
import re
import statistics
import sys

INDEX = "index.html"
LOG = "data-log.json"
FRED = "fred-latest.json"
WINDOW = 30


def kst_today():
    return (datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(hours=9)).strftime("%Y-%m-%d")


# ── 지표 정의 ────────────────────────────────────────────────────────────────
# tile      : 01 섹션 타일의 tile-label (없으면 타일이 없는 지표)
# hero      : hero-grid 의 label
# check     : 02/03 섹션의 check-label — 표기가 타일과 다른 것이 있다
# dec       : 소수 자리. 값과 delta 표기에 함께 쓴다
# unit      : 타일 value 안의 unit span 텍스트
# cunit     : check-value 는 단위를 문자열에 붙여 쓴다 ("4.80%", "206천건")
# sign      : 양수에 + 를 붙여 표기하는 지표 (금리차)
# trend     : 30일 평균 trend-line 대상인지
# pill      : 값으로 상태가 바뀌는 지표만 지정. None 이면 기존 pill 을 건드리지 않는다
#             (항상 info 인 지표를 굳이 다시 쓰지 않는다)
def pill_spread(v):   return ("good", "안정") if v > 0 else ("critical", "경고")
def pill_rrp(v):      return ("warn", "주의") if v < 50 else ("info", "정보")
def pill_hy(v):       return ("good", "안정") if v < 400 else (("warn", "주의") if v <= 700 else ("critical", "경고"))
def pill_jobless(v):  return ("good", "안정") if v <= 250 else (("warn", "주의") if v <= 300 else ("critical", "경고"))
def pill_sahm(v):     return ("good", "안정") if v < 0.3 else (("warn", "주의") if v < 0.5 else ("critical", "경고"))

SPEC = {
    # 환율은 값이 커서 천단위 쉼표를 쓴다(1,343.03). 웹검색이 사흘 전 값을
    # 되돌려 놓는 일이 있어 2026-09-13 에 스크립트 수집으로 옮겼다.
    "usdkrw":            dict(tile="원/달러 환율", hero="원/달러 환율",
                              dec=2, unit="KRW", trend=True, comma=True),
    "us10y":             dict(tile="美 10년물 국채금리", check="10년물 국채금리",
                              dec=2, unit="%", cunit="%", trend=True),
    "us2y":              dict(tile="美 2년물 국채금리", dec=2, unit="%", trend=True),
    "spread_10y_2y":     dict(tile="10Y–2Y 금리차", hero="美 10Y–2Y 금리차",
                              check="美10Y–美2Y 금리차", dec=2, unit="%p", cunit="%p",
                              sign=True, trend=True, pill=pill_spread),
    "fedfunds":          dict(tile="연준 기준금리", hero="연준 기준금리",
                              check="연준 기준금리", unit="%", cunit="%", range=True),
    "sofr":              dict(tile="SOFR 금리", dec=2, unit="%", trend=True),
    "tips10y_real":      dict(tile="실질금리 (10Y TIPS)", check="실질금리 (10Y TIPS)",
                              dec=2, unit="%", cunit="%", trend=True),
    # asof_prefix / monthly 는 data-log.json 의 asOf 표기를 기존 이력과 맞추기 위한 것.
    # Fed 총자산은 주간 H.4.1 발표분이고, M2·Sahm 은 월간 계열이라 FRED 가 주는
    # 월초 날짜(2026-07-01)가 아니라 월(2026-07)로 적어 온 이력을 따른다.
    "fed_balance_sheet": dict(tile="Fed 대차대조표 총자산", dec=2, unit="조$",
                              asof_prefix="H.4.1 "),
    "m2":                dict(tile="M2 통화량", dec=2, unit="조$", monthly=True),
    "rrp_balance":       dict(tile="RRP 잔액 (역레포)", dec=2, unit="십억$",
                              trend=True, pill=pill_rrp),
    "tga":               dict(tile="TGA 잔고", dec=1, unit="십억$", trend=True),
    "hy_spread":         dict(tile="하이일드 스프레드", dec=0, unit="bp",
                              trend=True, pill=pill_hy),
    "jobless_claims":    dict(check="신규 실업수당 청구건수", dec=0, cunit="천건",
                              pill=pill_jobless),
    "sahm_rule":         dict(check="Sahm Rule", dec=2, cunit="%p", pill=pill_sahm,
                              monthly=True),
}


class Abort(Exception):
    """패치 지점을 확실히 못 찾았을 때. 아무것도 쓰지 않고 끝낸다."""


def num(v, dec, sign=False, comma=False):
    c = "," if comma else ""
    return f"{v:+{c}.{dec}f}" if sign else f"{v:{c}.{dec}f}"


def delta_chip(cur, prev, dec, is_range=False):
    """전일 대비 칩 HTML. prev 가 없으면 최초 기록으로 표기한다."""
    if prev is None:
        return '<span class="delta-chip first">최초 기록</span>'
    if is_range:                       # 기준금리는 미드포인트로 판정한다
        if cur == prev:
            return '<span class="delta-chip flat">동결</span>'
        cls, txt = ("up", "인상") if cur > prev else ("down", "인하")
        return f'<span class="delta-chip {cls}">{txt}</span>'
    d = round(cur - prev, dec)
    if abs(d) < 10 ** -dec / 2:
        return '<span class="delta-chip flat">― 변동없음</span>'
    cls, arrow = ("up", "▲") if d > 0 else ("down", "▼")
    return f'<span class="delta-chip {cls}">{arrow} {d:+.{dec}f}</span>'


def trend_line(hist, dec):
    """30일 평균 대비 trend-line HTML. 표본이 5개 미만이면 축적 중으로 표시한다."""
    vals = [e["value"] for e in hist if isinstance(e["value"], (int, float))]
    if len(vals) < 5:
        return f'<span class="trend-line">데이터 축적 중 · {len(vals)}일째 / 30일</span>'
    w = vals[-WINDOW:]
    mean = statistics.fmean(w)
    cur = w[-1]
    if mean == 0:
        return f'<span class="trend-line">30일 평균 {mean:,.2f} 대비 —</span>'
    pct = (cur - mean) / abs(mean) * 100
    sd = statistics.stdev(w) if len(w) > 1 else 0.0
    flag = ""
    outlier = (abs(pct) >= 5) if sd < 1e-9 else (abs((cur - mean) / sd) >= 1.5)
    if outlier:
        cls, txt = ("up", "평균 상회") if cur > mean else ("down", "평균 하회")
        flag = f'<span class="trend-flag {cls}">{txt}</span>'
    return f'<span class="trend-line">30일 평균 {mean:,.2f} 대비 {pct:+.2f}%{flag}</span>'


def sub1(pattern, repl, text, what, literal=True):
    """정확히 한 번만 치환한다. 0번이나 2번 이상이면 중단한다.

    literal=True 면 repl 을 글자 그대로 넣는다(HTML 조각). literal=False 면
    \\g<1> 같은 역참조를 살린다. 이 구분이 없으면 역참조가 문자열로 박혀
    태그가 깨지는데, 치환 횟수는 1이라 중단 로직에도 걸리지 않는다.
    """
    if literal:
        repl = repl.replace("\\", "\\\\")
    new, n = re.subn(pattern, repl, text, flags=re.S)
    if n != 1:
        raise Abort(f"{what}: {n}곳 매칭 (1곳이어야 함)")
    return new


def patch_tile(html, label, value_html, trend_html, as_of, pill):
    """01 섹션 타일 하나를 갱신한다. tile-label 로 블록을 찾아 그 안만 바꾼다."""
    m = re.search(rf'(<div class="tile-top"><span class="tile-label">{re.escape(label)}'
                  rf'(?:<span class="code">[^<]*</span>)?</span>)(.*?)(?=<div class="tile">|</div>\s*</div>\s*<p|\Z)',
                  html, re.S)
    if not m:
        raise Abort(f'타일 "{label}" 을 찾지 못함')
    block = m.group(0)
    new = block

    if pill:
        new = sub1(r'<span class="pill [a-z]+">[^<]*</span>',
                   f'<span class="pill {pill[0]}">{pill[1]}</span>', new, f"{label} pill")
    new = sub1(r'<span class="value">.*?</span></span>', value_html, new, f"{label} value")
    if trend_html is not None:
        if '<span class="trend-line">' in new:
            # trend-flag 가 안에 중첩될 수 있으므로 '텍스트 또는 flag span' 의
            # 반복으로 본문을 소비한 뒤 바깥 </span> 을 닫는다.
            new = sub1(r'<span class="trend-line">'
                       r'(?:[^<]|<span class="trend-flag[^"]*">[^<]*</span>)*'
                       r'</span>',
                       trend_html, new, f"{label} trend")
        else:                       # trend-line 이 없던 타일에는 value 뒤에 새로 넣는다
            new = new.replace(value_html, value_html + "\n        " + trend_html, 1)
    # 접두어는 지표마다 의미가 다르다(H.4.1 = Fed 주간 발표, 기준월 = 월간 계열,
    # 산출 = 직접 계산). 날짜만 갈아끼우고 접두어는 그대로 둔다.
    mm = re.search(r'<span class="meta"><span>(기준일|산출|H\.4\.1|기준월) ([^<]*)</span>', new)
    if not mm:
        raise Abort(f"{label} meta: 기준일 표기를 찾지 못함")
    shown_date = as_of[:7] if mm.group(1) == "기준월" else as_of
    new = sub1(r'(<span class="meta"><span>(?:기준일|산출|H\.4\.1|기준월) )[^<]*(</span>)',
               rf'\g<1>{shown_date}\g<2>', new, f"{label} meta", literal=False)
    return html[:m.start()] + new + html[m.end():]


def patch_hero(html, label, value_html):
    m = re.search(rf'(<span class="label">{re.escape(label)}</span>\s*)'
                  rf'(<span class="value">.*?</span></span>)', html, re.S)
    if not m:
        raise Abort(f'hero 타일 "{label}" 을 찾지 못함')
    return html[:m.start(2)] + value_html + html[m.end(2):]


def patch_check(html, label, value_text, chip_html, pill):
    """02/03 섹션 한 줄. check-row 의 상태 클래스와 pill 도 함께 맞춘다."""
    # 행 하나씩 끊어서 라벨이 든 것만 고른다. 한 번의 정규식으로 잡으려 하면
    # 시작 지점이 앞 행으로 밀려 두 행이 함께 걸린다.
    rows = [m for m in re.finditer(
        r'<div class="check-row[^"]*">.*?</div>\s*</div>', html, re.S)]
    hit = [m for m in rows
           if f'<span class="check-label">{label}</span>' in m.group(0)]
    if len(hit) != 1:
        raise Abort(f'check 행 "{label}": {len(hit)}곳 매칭 (1곳이어야 함)')
    m = hit[0]
    block = m.group(0)
    new = sub1(r'<span class="check-value">[^<]*</span><span class="delta-chip [a-z]+">[^<]*</span>',
               f'<span class="check-value">{value_text}</span>{chip_html}',
               block, f"{label} check-value")
    if pill:
        new = sub1(r'<span class="pill [a-z]+">([^<]*)</span>',
                   f'<span class="pill {pill[0]}">{pill[1]}</span>', new, f"{label} check pill")
        new = re.sub(r'^<div class="check-row[^"]*">',
                     f'<div class="check-row {pill[0]}">', new)
    return html[:m.start()] + new + html[m.end():]


def main():
    today = kst_today()
    fred = json.load(open(FRED, encoding="utf-8"))
    doc = json.load(open(LOG, encoding="utf-8"))
    html = open(INDEX, encoding="utf-8").read()
    series = doc["series"]

    touched = []
    for key, spec in SPEC.items():
        if key not in fred:
            continue                                  # 수집 실패분은 건드리지 않는다
        item = fred[key]
        ent = series.get(key)
        if ent is None:
            raise Abort(f"data-log.json 에 {key} 가 없음")
        hist = ent["history"]

        is_range = spec.get("range", False)
        raw = item["value"]
        cur_num = item["midpoint"] if is_range else raw

        # 이전 값 — 오늘 기록이 이미 있으면(재실행) 그 앞의 것과 비교한다
        prior = [e for e in hist if e["date"] != today]
        prev = None
        if prior:
            p = prior[-1]["value"]
            prev = _mid(p) if is_range else (p if isinstance(p, (int, float)) else None)

        # data-log 갱신 (멱등: 오늘 기록이 있으면 교체)
        as_of_log = item["asOf"][:7] if spec.get("monthly") else item["asOf"]
        as_of_log = spec.get("asof_prefix", "") + as_of_log
        hist[:] = prior + [{"date": today, "asOf": as_of_log, "value": raw}]
        del hist[:-WINDOW]

        dec = spec.get("dec", 2)
        chip = delta_chip(cur_num, prev, dec, is_range)
        pill = spec["pill"](cur_num) if spec.get("pill") else None

        if spec.get("tile"):
            shown = raw if is_range else num(raw, dec, spec.get("sign", False), spec.get("comma", False))
            value_html = (f'<span class="value">{shown}'
                          f'<span class="unit">{spec["unit"]}</span>{chip}</span>')
            trend = trend_line(hist, dec) if spec.get("trend") else None
            html = patch_tile(html, spec["tile"], value_html, trend, item["asOf"], pill)
            if spec.get("hero"):
                html = patch_hero(html, spec["hero"], value_html)

        if spec.get("check"):
            shown = raw if is_range else num(raw, dec, spec.get("sign", False), spec.get("comma", False))
            html = patch_check(html, spec["check"], f'{shown}{spec["cunit"]}', chip, pill)

        touched.append(f'{key}={raw}{spec.get("unit") or spec.get("cunit","")}')

    # 01 섹션 summary-row 를 실제 pill 개수로 다시 센다
    sec = html[html.index('<section class="group" id="ind">'):html.index('<!-- 02')]
    cnt = {c: len(re.findall(rf'<span class="pill {c}">', sec)) for c in
           ("good", "warn", "critical", "todo")}
    for cls, label in (("good", "안정"), ("warn", "주의"),
                       ("critical", "경고"), ("todo", "수치 확인 필요")):
        html = sub1(rf'(<span class="status-count"><span class="dot {cls}"></span>'
                    rf'{label} <b>)\d+(</b></span>)',
                    rf'\g<1>{cnt[cls]}\g<2>', html, f"summary {label}", literal=False)

    doc["meta"]["lastRun"] = (datetime.datetime.now(datetime.timezone.utc)
                              + datetime.timedelta(hours=9)).strftime("%Y-%m-%d %H:%M")

    open(INDEX, "w", encoding="utf-8").write(html)
    with open(LOG, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=2)
        fh.write("\n")

    print(f"대시보드 반영 완료: {len(touched)}개 지표")
    for t in touched:
        print(f"  {t}")
    print(f"  summary-row: 안정 {cnt['good']} 주의 {cnt['warn']} "
          f"경고 {cnt['critical']} 확인필요 {cnt['todo']}")
    return 0


def _mid(v):
    """'3.50–3.75' 형태의 기준금리 문자열에서 미드포인트를 얻는다."""
    if isinstance(v, (int, float)):
        return float(v)
    parts = re.findall(r'-?\d+(?:\.\d+)?', str(v))
    return (float(parts[0]) + float(parts[1])) / 2 if len(parts) >= 2 else None


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Abort as e:
        print(f"::error::대시보드 반영 중단 — {e}")
        print("index.html / data-log.json 은 건드리지 않았다. Claude 단계가 기존대로 처리한다.")
        sys.exit(1)
