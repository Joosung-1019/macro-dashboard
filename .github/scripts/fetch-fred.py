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

남는 5개(dxy, vix, wti, fedwatch, nfp)는 FRED에 없거나(dxy·fedwatch), 실시간
시세가 필요하거나(vix·wti), 예상치 대비 판단이 필요해서(nfp) Claude가 검색한다.

출력은 저장소 루트의 fred-latest.json이며 커밋되지 않는다(.gitignore).
prompt.txt가 이 파일을 Read해서 해당 지표의 검색을 건너뛴다.

값의 스케일은 전부 index.html이 지금 표시하고 있는 단위에 맞춰 변환한다 —
이 스크립트는 표시 형식을 바꾸지 않는다.
"""

import datetime
import json
import sys
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
    ("us10y",             "DGS10",        1.0,    2, "%",    "美 10년물 국채금리"),
    ("us2y",              "DGS2",         1.0,    2, "%",    "美 2년물 국채금리"),
    ("spread_10y_2y",     "T10Y2Y",       1.0,    2, "%p",   "10Y-2Y 금리차"),
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


def fetch_latest(series_id):
    """해당 시리즈의 '값이 있는' 가장 최근 (날짜, 값)을 돌려준다.

    FRED CSV는 결측일을 '.'로 채우므로 뒤에서부터 숫자가 든 첫 행을 찾는다.
    실패는 예외로 올리고 호출부가 지표 단위로 건너뛴다 — 추측값은 절대 만들지 않는다.
    """
    with urllib.request.urlopen(CSV_URL.format(series_id), timeout=TIMEOUT) as resp:
        body = resp.read().decode("utf-8", "replace")

    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    # 없는 시리즈 ID를 주면 FRED는 200에 HTML 오류 페이지를 돌려준다. 정상 응답의
    # 첫 줄은 반드시 'observation_date,<시리즈ID>' 이므로 그걸로 걸러낸다.
    if not lines or not lines[0].lower().startswith("observation_date,"):
        raise ValueError(f"CSV 형식이 아님 (앞부분: {body[:60]!r})")

    for line in reversed(lines[1:]):
        date, _, raw = line.partition(",")
        raw = raw.strip()
        if raw and raw != ".":
            return date.strip(), float(raw)

    raise ValueError("숫자가 든 행이 없음")


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


def _yahoo_krw():
    """현물 시세. (기준일KST, 값, 출처, epoch) — 1순위."""
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/KRW=X"
           "?interval=1d&range=5d")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        meta = json.loads(resp.read().decode("utf-8", "replace"))
    meta = meta["chart"]["result"][0]["meta"]
    rate = float(meta["regularMarketPrice"])
    epoch = int(meta["regularMarketTime"])
    as_of = datetime.datetime.fromtimestamp(epoch, KST).strftime("%Y-%m-%d")
    return as_of, rate, "Yahoo:KRW=X", epoch


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
