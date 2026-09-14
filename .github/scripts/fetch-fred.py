#!/usr/bin/env python3
"""FRED 직접 수집 — Claude가 검색할 필요가 없는 13개 지표를 미리 받아둔다.

이 스크립트가 존재하는 이유는 비용이 아니라 가용성이다. 수집 전체가 Claude
세션 한도에 묶여 있으면 한도가 소진된 날은 19개 지표가 통째로 어제 값에
멈춘다(2026-09-08, 09-10 실제 사례). FRED가 원본인 지표는 API 키도 인증도
없이 CSV로 확정값을 받을 수 있으므로, 그만큼을 Claude 바깥으로 빼두면
한도가 비어 있든 아니든 최소 13개는 매일 갱신된다.

환율(usdkrw)도 여기서 받지만 경로가 다르다. FRED에 DEXKOUS가 있긴 해도 H.10
주간 발표라 최대 8일 밀려서 현물 시세로 쓸 수 없다. 그래서 별도 폴백 체인을
둔다 — 아래 fetch_usdkrw 주석에 그동안 틀렸던 이력까지 적어 뒀다.

달러인덱스·VIX·WTI도 같은 이유로 여기서 받는다. FRED에 없거나 지연되는데다,
검색으로 받던 시절에는 asOf를 '수집일'로 적어서 시장이 닫힌 주말에도 그날짜
값이 생겼다. 셋 다 환율과 같은 현물 시세 경로(_yahoo_quote)를 쓴다.

Claude에게 남는 건 판단이 필요한 2개뿐이다 — fedwatch(선물 시장의 금리 전망)와
nfp(예상치 대비 해석).

출력은 저장소 루트의 fred-latest.json이며 커밋되지 않는다(.gitignore).
prompt.txt가 이 파일을 Read해서 해당 지표의 검색을 건너뛴다.

값의 스케일은 전부 index.html이 지금 표시하고 있는 단위에 맞춰 변환한다 —
이 스크립트는 표시 형식을 바꾸지 않는다.
"""

import datetime
import json
import sys
import urllib.parse
import urllib.request

CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={}"
TIMEOUT = 25
OUT_PATH = "fred-latest.json"

# (data-log.json 키, FRED 시리즈, 배율, 소수자리, 표시단위, 설명)
# 배율은 FRED 원단위 -> 대시보드 표시단위 변환이다:
#   WALCL   백만$ -> 조$    (1e-6)
#   M2SL    십억$ -> 조$    (1e-3)
#   WDTGAL  백만$ -> 십억$  (1e-3)
#   ICSA    건    -> 천건   (1e-3)
#   BAMLH0A0HYM2 % -> bp   (100)
SERIES = [
    ("sofr",              "SOFR",         1.0,    2, "%",    "SOFR 금리"),
    ("tips10y_real",      "DFII10",       1.0,    2, "%",    "실질금리 (10Y TIPS)"),
    ("fed_balance_sheet", "WALCL",        1e-6,   2, "조$",   "Fed 대차대조표 총자산"),
    ("m2",                "M2SL",         1e-3,   2, "조$",   "M2 통화량"),
    ("rrp_balance",       "RRPONTSYD",    1.0,    2, "십억$", "RRP 잔액 (역레포)"),
    # TGA는 '잔고' 타일이므로 주간 평균(WTREGEN)이 아니라 수요일 시점 잔고인
    # WDTGAL을 쓴다. 둘은 같은 날짜에도 20~30십억달러쯤 차이가 난다.
    ("tga",               "WDTGAL",       1e-3,   1, "십억$", "TGA 잔고"),
    # 하이일드는 OAS(BAMLH0A0HYM2)가 맞다. BAMLH0A0HYM2EY는 유효수익률(7.22%)이라
    # bp로 환산하면 722bp가 나와 대시보드가 표시해 온 265bp와 전혀 다른 값이 된다.
    ("hy_spread",         "BAMLH0A0HYM2", 100.0,  0, "bp",   "하이일드 스프레드"),
    ("jobless_claims",    "ICSA",         1e-3,   0, "천건",  "신규 실업수당 청구건수"),
    ("sahm_rule",         "SAHMREALTIME", 1.0,    2, "%p",   "Sahm Rule"),
]

# 연준 기준금리만 하한·상한 두 시리즈를 묶어 "3.50–3.75" 문자열로 만든다.
FEDFUNDS = ("fedfunds", "DFEDTARL", "DFEDTARU", "%", "연준 기준금리")

# 서로 검산되는 지표는 같은 날짜로 맞춰야 한다. FRED 는 시리즈마다 발표 시점이
# 달라서 각자 최신값을 쓰면 화면에서 산수가 안 맞는다. 2026-09-14 실행이 그랬다 —
# DGS10·DGS2 는 09-10 까지인데 T10Y2Y 는 09-11 까지 나와 있어서 화면에
#   10Y 4.95(09-10)   2Y 4.56(09-10)   금리차 0.33(09-11)
# 이 걸렸다. 4.95 − 4.56 = 0.39 인데 금리차 칸은 0.33 이니 검산하는 사람은
# 대시보드가 고장났다고 본다(09-10 의 T10Y2Y 가 실제로 0.39다).
# 그래서 세 시리즈에 값이 모두 있는 가장 최근 날짜로 끊는다. 하루 늦더라도
# 세 숫자가 서로 맞는 편이 낫다.
COHERENT = {
    "us10y":         ("DGS10",  "美 10년물 국채금리", "%"),
    "us2y":          ("DGS2",   "美 2년물 국채금리",  "%"),
    "spread_10y_2y": ("T10Y2Y", "10Y-2Y 금리차",     "%p"),
}


def fetch_all(series_id):
    """{날짜: 값} 전체. 결측일('.')은 뺀다.

    실패는 예외로 올리고 호출부가 지표 단위로 건너뛴다 — 추측값은 절대 만들지 않는다.
    """
    with urllib.request.urlopen(CSV_URL.format(series_id), timeout=TIMEOUT) as resp:
        body = resp.read().decode("utf-8", "replace")

    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    # 없는 시리즈 ID를 주면 FRED는 200에 HTML 오류 페이지를 돌려준다. 정상 응답의
    # 첫 줄은 반드시 'observation_date,<시리즈ID>' 이므로 그걸로 걸러낸다.
    if not lines or not lines[0].lower().startswith("observation_date,"):
        raise ValueError(f"CSV 형식이 아님 (앞부분: {body[:60]!r})")

    out = {}
    for line in lines[1:]:
        date, _, raw = line.partition(",")
        raw = raw.strip()
        if raw and raw != ".":
            out[date.strip()] = float(raw)
    if not out:
        raise ValueError("숫자가 든 행이 없음")
    return out


def fetch_latest(series_id):
    """해당 시리즈의 '값이 있는' 가장 최근 (날짜, 값). 날짜가 ISO 라 문자열 max 로 충분하다."""
    data = fetch_all(series_id)
    day = max(data)
    return day, data[day]


def fetch_coherent():
    """검산 관계인 세 지표를 같은 날짜로 맞춰 받는다. (날짜, {키: 값})"""
    data = {key: fetch_all(sid) for key, (sid, _, _) in COHERENT.items()}
    common = set.intersection(*(set(v) for v in data.values()))
    if not common:
        raise ValueError("세 시리즈에 공통 날짜가 없음")
    day = max(common)
    return day, {key: data[key][day] for key in COHERENT}


# ── 원/달러 환율 ────────────────────────────────────────────────────────────
# 이 지표만 수집 경로가 다른 이유는 나머지 13개와 달리 '오늘 이 시각의 시세'가
# 필요하기 때문이다. 지금까지 두 번 틀렸고, 원인이 매번 달랐다.
#
# (1) 웹검색(~2026-09-12): 09-10~13 값이 1386.01 → 1386.01 → 1341.25 → 1386.01 로
#     튀었다. 검색 스니펫이 날짜 불분명한 사흘 전 수치를 섞어 준 탓이다.
#     그래서 09-13 에 스크립트 수집으로 옮겼다.
#
# (2) open.er-api.com 단독(2026-09-13~14): 이건 '실시간 API' 가 아니라 하루 한 번
#     (00:00 UTC 전후) 갱신되는 참조 환율이다. 워크플로는 00:09 UTC(09:09 KST)에
#     도는데, 09-14 실행에서 이 API 가 돌려준 기준일은 09-13 이었다. 그래서
#     09-14 기록이 09-13 값(1343.03) 그대로 남았다 — 09:10 KST 시점에 이미 33시간
#     묵은 값이고, 화면에는 "― 변동없음" 으로 찍혀서 '오늘 안 움직였다' 와
#     '오늘 값을 못 받았다' 가 구분되지 않았다.
#
# 결론: 1순위를 현물 시세로 바꾼다. Yahoo KRW=X 는 서울장(09:00~15:30 KST) 중에도
# 계속 갱신되고 epoch 타임스탬프를 함께 주므로, 며칠짜리 날짜 비교가 아니라
# '몇 시간 묵었는지' 로 신선도를 판정할 수 있다. 뒤의 둘은 성격이 다른 폴백이다:
# er-api 는 하루 한 번 참조 환율, FRED 는 주간 확정치(최대 8일 지연)다.
KST = datetime.timezone(datetime.timedelta(hours=9))
UA = "Mozilla/5.0 (compatible; macro-dashboard/1.0)"

# 검증 임계값. 이건 '시장이 그렇게 움직일 리 없다' 가 아니라 '파싱이 깨졌다' 를
# 잡기 위한 것이다. 원/달러가 하루 10% 움직이는 위기 상황은 실재하므로, 진짜
# 급등락을 걸러내고 대신 묵은 값을 보여주는 일이 없도록 밴드를 넓게 둔다.
# 좁게 잡으면 정작 봐야 할 날에 화면이 멈춘다.
FX_MIN, FX_MAX = 500.0, 3000.0    # 단위 사고(134.6 / 13460) 감지
FX_MAX_JUMP = 0.10                # 직전 기록 대비 10% 초과 = 쓰레기로 간주
FX_FRESH_HOURS = 30               # 이보다 묵었으면 stale 로 표시(주말 이월 허용)


def _yahoo_quote(symbol):
    """Yahoo 현물 시세. {price, epoch, gmtoffset, tzname}.

    asOf 를 응답의 epoch 에서 끌어내는 게 요점이다. 수집일을 그대로 기준일로
    적으면 시장이 닫힌 날에도 '오늘 값' 이 생긴다 — dxy·vix·wti 가 그랬다.
    날짜 변환은 호출부가 한다. 지표마다 어느 시간대로 끊어야 맞는지가 다르다.
    """
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/"
           f"{urllib.parse.quote(symbol)}?interval=1d&range=5d")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        payload = json.loads(resp.read().decode("utf-8", "replace"))
    meta = payload["chart"]["result"][0]["meta"]
    return {
        "price": float(meta["regularMarketPrice"]),
        "epoch": int(meta["regularMarketTime"]),
        "gmtoffset": int(meta.get("gmtoffset", 0)),
        "tzname": str(meta.get("exchangeTimezoneName", "?")),
    }


def _session_date(quote, roll_hours):
    """시세가 속한 '거래 세션 날짜'. 거래소 현지 시각 기준으로 끊는다.

    KST 로 끊으면 미국 정규장 종가가 하루 밀린다 — VIX 금요일 종가(16:15 ET)가
    KST 로는 토요일 05:15 이라 "기준일 2026-09-12(토)" 가 찍혔다. 토요일에
    VIX 값이 있을 리 없으니 오히려 숫자가 이상해 보인다.

    roll_hours 는 선물의 세션 롤오버다. CME 계열은 18:00 ET 에 다음날 세션이
    열리므로 6시간을 더해야 거래일이 맞는다(일요일 저녁 시세 = 월요일 세션).
    현물 지수(VIX)는 롤오버가 없어 0 이다.
    """
    tz = datetime.timezone(datetime.timedelta(seconds=quote["gmtoffset"]))
    local = datetime.datetime.fromtimestamp(quote["epoch"], tz)
    return (local + datetime.timedelta(hours=roll_hours)).strftime("%Y-%m-%d")


def _yahoo_krw():
    """원/달러 현물. (기준일, 값, 출처, epoch) — 1순위.

    환율만 KST 로 끊는다. 서울 시장을 보는 지표이고 09:10 KST 실행 시점이
    이미 당일 서울장 안이라 KST 날짜가 곧 거래일이다.
    """
    q = _yahoo_quote("KRW=X")
    as_of = datetime.datetime.fromtimestamp(q["epoch"], KST).strftime("%Y-%m-%d")
    return as_of, q["price"], "Yahoo:KRW=X", q["epoch"]


def _weekdays_since(day, today):
    """day(제외)부터 today(포함)까지의 평일 수. 주말 휴장을 감안한 신선도 판정용."""
    n, cur = 0, day
    while cur < today:
        cur += datetime.timedelta(days=1)
        if cur.weekday() < 5:
            n += 1
    return n


def _erapi_krw():
    """하루 한 번 갱신되는 참조 환율. (기준일, 값, 출처, epoch) — 2순위."""
    with urllib.request.urlopen(
            "https://open.er-api.com/v6/latest/USD", timeout=TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    rate = float(data["rates"]["KRW"])
    # time_last_update_unix 가 있으면 그걸 쓰고, 없을 때만 문자열을 파싱한다.
    epoch = data.get("time_last_update_unix")
    if isinstance(epoch, (int, float)) and epoch > 0:
        epoch = int(epoch)
        as_of = datetime.datetime.fromtimestamp(epoch, KST).strftime("%Y-%m-%d")
        return as_of, rate, "ER-API", epoch
    stamp = data.get("time_last_update_utc", "")   # "Sun, 13 Sep 2026 00:02:31 +0000"
    parts = stamp.split()
    months = {m: i for i, m in enumerate(
        "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(), 1)}
    if len(parts) < 4 or parts[2] not in months:
        raise ValueError(f"날짜 형식을 못 읽음: {stamp!r}")
    as_of = f"{parts[3]}-{months[parts[2]]:02d}-{int(parts[1]):02d}"
    return as_of, rate, "ER-API", None


def _fred_krw():
    """주간 확정치(H.10). 최대 8일 지연 — 최후 수단."""
    as_of, raw = fetch_latest("DEXKOUS")
    return as_of, raw, "FRED:DEXKOUS", None


def _prev_usdkrw():
    """data-log.json 에 남은 마지막 환율. 급변 판정 기준이 없으면 None."""
    try:
        with open("data-log.json", encoding="utf-8") as fh:
            hist = json.load(fh)["series"]["usdkrw"]["history"]
    except Exception:                                 # noqa: BLE001
        return None
    for entry in reversed(hist):
        if isinstance(entry.get("value"), (int, float)):
            return float(entry["value"])
    return None


def fetch_usdkrw():
    """(기준일, 값, 출처, stale여부) 반환.

    현물 -> 참조환율 -> 주간확정치 순으로 시도하고, 각 후보를 검증에 통과해야
    받아들인다. 검증에서 떨어진 후보는 다음 순위로 넘어간다 — 값을 고쳐 쓰거나
    추측하지 않는다. 전부 실패하면 예외를 올려 호출부가 지표를 건너뛴다.
    """
    prev = _prev_usdkrw()
    now = datetime.datetime.now(datetime.timezone.utc)
    errors = []

    for source in (_yahoo_krw, _erapi_krw, _fred_krw):
        try:
            as_of, rate, label, epoch = source()
        except Exception as exc:                      # noqa: BLE001
            errors.append(f"{source.__name__}: {exc}")
            continue

        # 검증 1 — 절대 범위. 단위가 어긋나거나 엉뚱한 필드를 읽었을 때 걸린다.
        if not (FX_MIN <= rate <= FX_MAX):
            errors.append(f"{label}: 범위 밖 값 {rate}")
            continue

        # 검증 2 — 직전 기록 대비 급변. 파싱 사고를 잡되 실제 급등락은 통과시킨다.
        if prev and abs(rate - prev) / prev > FX_MAX_JUMP:
            errors.append(f"{label}: 직전({prev}) 대비 {(rate/prev-1)*100:+.1f}% 급변")
            continue

        # 검증 3 — 신선도. 떨어뜨리지 않고 표시만 남긴다. 묵었더라도 아무것도
        # 없는 것보다는 낫고, 대신 화면이 '지연' 이라고 밝히게 한다.
        if epoch is not None:
            stale = (now.timestamp() - epoch) / 3600.0 > FX_FRESH_HOURS
        else:
            age_days = (now.astimezone(KST).date()
                        - datetime.date.fromisoformat(as_of)).days
            stale = age_days >= 2

        for err in errors:
            print(f"  (환율 {err})")
        return as_of, rate, label, stale

    raise ValueError("모든 환율 소스 실패 — " + " / ".join(errors))


# ── 현물 시세 지표 (dxy, vix, wti) ──────────────────────────────────────────
# 원래 이 셋은 Claude 가 WebSearch 로 받았는데, asOf 를 무조건 '수집일' 로
# 적고 있었다. 그래서 시장이 닫힌 주말에도 그날짜 값이 만들어졌다 — 실제
# 기록을 보면 vix 가 09-12(토) 15.84, 09-13(일) 17.51 이고 wti 는 09-12(토)
# 100.05 다. VIX 는 미국 정규장에서만 산출되고 토요일엔 값 자체가 없다.
# 즉 금요일 종가이거나 출처 불명인 수치에 '오늘 기준' 딱지가 붙어 있었다.
# 환율에서 겪은 것과 같은 병이라 같은 방식으로 고친다: 현물 시세 API 의
# epoch 로 asOf 를 정하고, 검증을 통과한 값만 받는다.
#
# stale 판정이 지표마다 다른 이유: dxy·wti 는 사실상 24/5 로 거래되므로
# '몇 시간 묵었나' 로 보면 되지만, VIX 는 정규장에서만 산출된다. VIX 를
# 시간으로 재면 월요일 아침 KST 마다 52시간 묵은 금요일 종가가 걸려서 매주
# "갱신 지연" 오탐이 난다. 그래서 VIX 만 영업일 기준으로 본다.
SPOT = [
    # (키, Yahoo심볼, 소수, 표시단위, 라벨, (하한,상한), stale시간|None=영업일, 세션롤오버h)
    # 롤오버: CME 계열 선물은 18:00 ET 에 다음 거래일 세션이 열리므로 6 을 더해야
    # 거래일이 맞는다(일요일 저녁 시세 = 월요일 세션). VIX 는 현물 지수라 0 이다.
    ("dxy", "DX-Y.NYB", 2, "pt",    "달러 인덱스",    (50.0, 200.0), 30,   6),
    ("vix", "^VIX",     2, "pt",    "VIX 변동성지수", (5.0, 150.0),  None, 0),
    ("wti", "CL=F",     2, "$/bbl", "WTI 유가",      (5.0, 300.0),  30,   6),
]


def fetch_spot(key, symbol, digits, unit, label, bounds, stale_hours, roll_hours):
    """현물 지표 하나. 검증을 통과하지 못하면 예외 — 값을 만들어 내지 않는다."""
    q = _yahoo_quote(symbol)
    price, epoch = q["price"], q["epoch"]
    as_of = _session_date(q, roll_hours)

    lo, hi = bounds
    if not (lo <= price <= hi):
        raise ValueError(f"범위 밖 값 {price} (허용 {lo}~{hi})")

    if stale_hours is not None:
        now = datetime.datetime.now(datetime.timezone.utc)
        stale = (now.timestamp() - epoch) / 3600.0 > stale_hours
    else:
        # 영업일 기준. 비교 대상도 거래소 현지 '오늘' 이어야 한다 — KST 로 재면
        # 시차 때문에 하루가 어긋난다.
        tz = datetime.timezone(datetime.timedelta(seconds=q["gmtoffset"]))
        stale = _weekdays_since(datetime.date.fromisoformat(as_of),
                                datetime.datetime.now(tz).date()) > 1

    return {
        "label": label,
        "asOf": as_of,
        "value": round(price, digits),
        "unit": unit,
        "source": f"Yahoo:{symbol}",
        "stale": stale,
    }


def main():
    out, failed = {}, []

    for key, series_id, scale, digits, unit, label in SERIES:
        try:
            as_of, raw = fetch_latest(series_id)
        except Exception as exc:                      # noqa: BLE001 — 지표 하나의 실패가 전체를 막지 않는다
            failed.append(f"{key}({series_id}): {exc}")
            continue

        value = round(raw * scale, digits)
        if digits == 0:
            value = int(value)
        out[key] = {
            "label": label,
            "asOf": as_of,
            "value": value,
            "unit": unit,
            "source": f"FRED:{series_id}",
        }

    # 10Y·2Y·금리차는 셋이 서로 검산되므로 같은 날짜로 끊어서 받는다. 하나라도
    # 실패하면 셋 다 건너뛴다 — 일부만 갱신하면 날짜가 다시 어긋나기 때문이다.
    try:
        day, values = fetch_coherent()
        for key, (series_id, label, unit) in COHERENT.items():
            out[key] = {
                "label": label,
                "asOf": day,
                "value": round(values[key], 2),
                "unit": unit,
                "source": f"FRED:{series_id}",
            }
    except Exception as exc:                          # noqa: BLE001
        failed.append(f"금리 3종(DGS10/DGS2/T10Y2Y): {exc}")

    # 기준금리는 하한/상한을 모두 받아야 의미가 있으므로 둘 중 하나라도 실패하면 통째로 건너뛴다.
    key, lo_id, hi_id, unit, label = FEDFUNDS
    try:
        lo_as_of, lo = fetch_latest(lo_id)
        hi_as_of, hi = fetch_latest(hi_id)
        out[key] = {
            "label": label,
            "asOf": max(lo_as_of, hi_as_of),
            "value": f"{lo:.2f}–{hi:.2f}",      # en dash — data-log.json 기존 표기와 동일
            "midpoint": round((lo + hi) / 2, 3),     # delta 판정(동결/인상/인하)에 쓴다
            "unit": unit,
            "source": f"FRED:{lo_id}+{hi_id}",
        }
    except Exception as exc:                          # noqa: BLE001
        failed.append(f"{key}({lo_id}/{hi_id}): {exc}")

    # 원/달러 환율 (FRED 계열이 아니라 현물 시세 우선)
    try:
        fx_as_of, fx_rate, fx_src, fx_stale = fetch_usdkrw()
        out["usdkrw"] = {
            "label": "원/달러 환율",
            "asOf": fx_as_of,
            "value": round(fx_rate, 2),
            "unit": "원",
            "source": fx_src,
            "stale": fx_stale,
        }
    except Exception as exc:                          # noqa: BLE001
        failed.append(f"usdkrw: {exc}")

    # 현물 시세 3종. 하나가 실패해도 나머지는 받는다 — 서로 검산 관계가 아니라서
    # 날짜를 맞출 필요가 없다. 실패분은 파일에 없으니 prompt.txt 규칙에 따라
    # 이전 값이 그대로 남는다.
    for spec in SPOT:
        try:
            out[spec[0]] = fetch_spot(*spec)
        except Exception as exc:                      # noqa: BLE001
            failed.append(f"{spec[0]}({spec[1]}): {exc}")

    with open(OUT_PATH, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2)
        fh.write("\n")

    print(f"FRED 수집 완료: {len(out)}개 성공, {len(failed)}개 실패 -> {OUT_PATH}")
    for k, v in out.items():
        print(f"  {k:<18} {v['value']} {v['unit']}  (asOf {v['asOf']}, {v['source']})")
    for f in failed:
        print(f"  [실패] {f}")

    # 부분 실패해도 0으로 끝낸다. 실패한 지표는 파일에 없고, prompt.txt가
    # '파일에 없으면 이전 값 유지'로 처리하므로 워크플로를 막을 이유가 없다.
    return 0


if __name__ == "__main__":
    sys.exit(main())
