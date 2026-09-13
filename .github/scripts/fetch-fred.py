#!/usr/bin/env python3
"""FRED 직접 수집 — Claude가 검색할 필요가 없는 13개 지표를 미리 받아둔다.

이 스크립트가 존재하는 이유는 비용이 아니라 가용성이다. 수집 전체가 Claude
세션 한도에 묶여 있으면 한도가 소진된 날은 19개 지표가 통째로 어제 값에
멈춘다(2026-09-08, 09-10 실제 사례). FRED가 원본인 지표는 API 키도 인증도
없이 CSV로 확정값을 받을 수 있으므로, 그만큼을 Claude 바깥으로 빼두면
한도가 비어 있든 아니든 최소 13개는 매일 갱신된다.

남는 6개(usdkrw, dxy, vix, wti, fedwatch, nfp)는 FRED에 없거나(dxy·fedwatch),
FRED 값이 1~8일 지연돼 실시간 시세로서 의미가 떨어지거나(usdkrw·wti·vix),
예상치 대비 판단이 필요해서(nfp) 그대로 Claude가 검색한다.

출력은 저장소 루트의 fred-latest.json이며 커밋되지 않는다(.gitignore).
prompt.txt가 이 파일을 Read해서 해당 지표의 검색을 건너뛴다.

값의 스케일은 전부 index.html이 지금 표시하고 있는 단위에 맞춰 변환한다 —
이 스크립트는 표시 형식을 바꾸지 않는다.
"""

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


# 원/달러는 FRED(DEXKOUS)가 H.10 주간 발표라 최신값이 일주일까지 밀린다. 대시보드는
# 현물 환율을 보여주는 자리이므로 실시간 API를 1순위로 쓰고, 그게 막히면 FRED로
# 내려간다. 둘 다 실패하면 값을 만들지 않고 건너뛴다(이전 값 유지).
#
# 이 지표를 스크립트로 옮긴 이유: 웹검색으로 받던 2026-09-10~13 동안 값이
# 1386.01 → 1386.01 → 1341.25 → 1386.01 로 튀었다. 마지막 1386.01은 실제 시세
# (약 1343)보다 43원 높은 사흘 전 값이 되돌아온 것이었다. 검색 스니펫은 날짜가
# 불분명한 수치를 섞어 주므로 환율처럼 매일 변하는 값에는 쓰지 않는다.
FX_URL = "https://open.er-api.com/v6/latest/USD"


def fetch_usdkrw():
    """(기준일, 원/달러) 반환. 실시간 API -> FRED 순으로 시도한다."""
    try:
        with urllib.request.urlopen(FX_URL, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        rate = data["rates"]["KRW"]
        # time_last_update_utc 예: "Sun, 13 Sep 2026 00:02:31 +0000"
        stamp = data.get("time_last_update_utc", "")
        as_of = ""
        parts = stamp.split()
        if len(parts) >= 4:
            months = {m: i for i, m in enumerate(
                "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(), 1)}
            if parts[2] in months:
                as_of = f"{parts[3]}-{months[parts[2]]:02d}-{int(parts[1]):02d}"
        if not as_of:
            raise ValueError(f"날짜 형식을 못 읽음: {stamp!r}")
        return as_of, float(rate), "ER-API"
    except Exception as fx_exc:                       # noqa: BLE001
        as_of, raw = fetch_latest("DEXKOUS")          # 실패하면 예외가 그대로 올라간다
        print(f"  (환율 실시간 API 실패 -> FRED 대체: {fx_exc})")
        return as_of, raw, "FRED:DEXKOUS"


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

    # 원/달러 환율 (FRED 계열이 아니라 실시간 API 우선)
    try:
        fx_as_of, fx_rate, fx_src = fetch_usdkrw()
        out["usdkrw"] = {
            "label": "원/달러 환율",
            "asOf": fx_as_of,
            "value": round(fx_rate, 2),
            "unit": "원",
            "source": fx_src,
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
