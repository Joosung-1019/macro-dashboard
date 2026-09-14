#!/usr/bin/env python3
"""수집 결과를 점검해 문제가 있으면 실행을 실패로 표시한다.

존재 이유는 알림이다. 갱신 자체는 성공해도 값이 묵었을 수 있는데, 그 경우에도
워크플로가 success 로 끝나면 아침 알림이 "매크로 지표 갱신 완료" 라고 간다.
화면에는 "갱신 지연" 칩이 떠 있어도 알림만 보고는 알 수 없으니, 결국 사람이
매일 사이트를 열어 실제 시세와 대조해야 한다. 2026-09-14 에 환율이 틀린 걸
그렇게 발견했다. 여기서 걸러 실행을 실패로 끝내면 알림이 "부분갱신" 으로
바뀌고, 사이트를 열지 않아도 이상을 알 수 있다.

점검 대상을 좁힌 건 의도적이다. FRED 계열의 발표 지연(기준일이 1~8일 전인 것)은
검사하지 않는다. 시리즈마다 주기가 제각각이라 그걸 다 잡으려 들면 경고가 매일
뜨고, 그러면 진짜 문제가 생긴 날에도 그냥 넘기게 된다. 양치기 소년이 되면
안 붙이느니만 못하다.

그래서 판단이 필요 없는, 명백한 것만 본다:
  1) 수집 실패 — 못 받아온 지표가 있다
  2) 현물 4종의 stale — 실시간 시세가 묵은 건 정상일 수 없다
  3) 환율이 폴백 경로로 받아졌다 — 현물이 아니라 하루 단위 참조 환율이다
  4) data-log 에 오늘 기록이 없다 — 반영 단계가 중단됐다는 뜻
"""

import datetime
import json
import sys

FRED = "fred-latest.json"
LOG = "data-log.json"
SPOT = ("usdkrw", "dxy", "vix", "wti")


def kst_today():
    return (datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(hours=9)).strftime("%Y-%m-%d")


def main():
    try:
        fred = json.load(open(FRED, encoding="utf-8"))
    except Exception as exc:                          # noqa: BLE001
        print(f"::error::수집 결과 파일을 읽지 못했다 ({exc}). "
              "수집 단계가 통째로 실패한 것이다 — 대시보드는 어제 값 그대로다.")
        return 1

    series = {k: v for k, v in fred.items() if not k.startswith("_")}
    problems = []

    # 1) 수집 실패
    failed = fred.get("_meta", {}).get("failed", [])
    if failed:
        problems.append(f"수집 실패 {len(failed)}건 — " + " / ".join(failed))

    # 2) 현물 지표가 묵었다
    stale = [k for k in SPOT if isinstance(series.get(k), dict) and series[k].get("stale")]
    if stale:
        detail = ", ".join(f"{k}(기준일 {series[k]['asOf']})" for k in stale)
        problems.append(f"현물 시세 갱신 지연 — {detail}")

    # 3) 환율이 폴백으로 받아졌다. 값 자체는 쓸 수 있지만 현물이 아니라
    #    하루 단위 참조 환율(er-api)이거나 주간 확정치(FRED)라 실시간 시세와
    #    다르다. 오늘 화면의 환율을 그대로 믿으면 안 된다는 신호다.
    fx = series.get("usdkrw")
    if isinstance(fx, dict) and not str(fx.get("source", "")).startswith("Yahoo"):
        problems.append(f"환율이 폴백 경로로 수집됨 — {fx.get('source')} "
                        f"(기준일 {fx.get('asOf')}). 현물 시세가 아니다.")

    # 4) 반영 단계가 중단됐는지. asOf(관측일)가 아니라 date(수집일)를 보므로
    #    FRED 발표 지연과는 무관하다 — 여기서는 오탐이 나지 않는다.
    today = kst_today()
    try:
        log = json.load(open(LOG, encoding="utf-8"))["series"]
    except Exception as exc:                          # noqa: BLE001
        problems.append(f"data-log.json 을 읽지 못했다 ({exc})")
        log = None

    if log is not None:
        missing = []
        for key in series:
            hist = log.get(key, {}).get("history") or []
            if not hist or hist[-1].get("date") != today:
                missing.append(key)
        if missing:
            problems.append(
                f"오늘({today}) 기록이 없는 지표 {len(missing)}개 — "
                + ", ".join(missing[:6]) + (" 외" if len(missing) > 6 else "")
                + ". 대시보드 반영 단계가 중단됐을 수 있다.")

    if not problems:
        print(f"데이터 점검 통과 — 지표 {len(series)}개 수집, "
              f"현물 4종 모두 최신, data-log 오늘({today}) 기록 정상")
        return 0

    for p in problems:
        print(f"::warning::{p}")
    print("::error::데이터 점검 실패. 대시보드에는 해당 지표가 이전 값으로 "
          "남아 있고 화면에 '갱신 지연' 으로 표시된다.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
