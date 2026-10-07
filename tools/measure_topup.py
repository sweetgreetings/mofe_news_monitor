# Design Ref: EXPERT_QUESTIONS.md "4. 늦게 색인된 기사를 제 회차에 담을 것인가"
"""회차 마감 뒤 몇 분에 다시 훑으면 기사가 몇 건 더 들어오는지 **재기만 하는** 스크립트.

**왜 있나.** 회차 창은 (시작, 마감]이고, 마감 순간 네이버가 아직 색인하지 않은 기사는
다음 회차가 하한을 COLLECT_LOOKBACK_MIN(60분)만큼 물려 주워 담는다. 그래서 기사를
잃지는 않지만 **다음 회차 보고서에 실린다**(실측: 전체의 2.8%). 이걸 제 회차에 담으려면
마감 뒤 한 번 더 훑어야 하는데(app.scraper.top_up_last_run이 하루 마지막 회차에만 하는
일), **마감 +몇 분이 적당한지는 지금 데이터로 알 수 없다** — 안 들어온 기사는 셀 수가
없기 때문이다. 이 스크립트가 그 빈자리를 며칠 재서 채운다.

**재기만 한다 — 앱 데이터는 한 글자도 안 쓴다.** 회차 파일·설정은 읽기만 하고, 결과는
data/measure_topup/{날짜}.json에만 쌓는다. 앱을 켜 둔 채로 같이 돌려도 안전하다
(설정 서버 포트를 열지 않으므로 중복 실행 자물쇠에도 안 걸린다).

쓰는 법:
    python3 tools/measure_topup.py            # 상주 — 오늘 회차 마감마다 저절로 잰다
    python3 tools/measure_topup.py --once 14:00  # 그 회차를 지금 한 번만 재본다
    python3 tools/measure_topup.py --report      # 쌓인 결과를 표로 본다

네이버 호출: 한 번 잴 때 「초안·확정본」을 켠 그룹의 키워드 수만큼(지금 8개, 페이지를
넘기면 그 배수). 회차 5개 × 시점 4번이면 하루 수백 회로, 한도 25,000의 2% 안쪽이다.
그래도 [단독]·[속보] 감시가 80%에서 멈추는 몫을 잠식하지 않도록, 오늘 사용량이
_USAGE_CEILING을 넘으면 그 회차 측정을 건너뛰고 건너뛴 사실을 기록한다.
"""
import argparse
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.api_usage import today_call_count, usage_ratio
from app.config import COLLECT_LOOKBACK_MIN
from app.last_slot_topup import schedule_times_now
from app.credentials import naver_is_configured
from app.naver_api import kst_today_at, search_articles_by_groups
from app.scraper import _already_published_urls
from app.settings import active_search_groups, group_in_scrap, load_settings
from app.filters import deduplicate_by_title
from app.filters import filter_by_outlet_whitelist
from app.storage import list_runs_for_date

# 마감 뒤 몇 분에 재볼 것인가. 자동발송 유예(기본 5분·최대 30분)와 맞물리는 값들이라
# 이 넷을 고른다 — 3·5는 "유예 안에 끝나는 보충", 10·30은 "발송 뒤에 덧붙는 보충".
DELAYS_MIN = [3, 5, 10, 30]

# 오늘 네이버 호출이 이 비율을 넘으면 측정을 건너뛴다. 본업(회차 수집)과 [단독]·[속보]
# 감시(80%에서 스스로 멈춘다)의 몫을 재는 일이 잠식하면 안 된다.
_USAGE_CEILING = 0.60

_POLL_SEC = 30
_OUT_DIR = Path(__file__).resolve().parent.parent / "data" / "measure_topup"


def _out_path(date_str: str) -> Path:
    return _OUT_DIR / f"{date_str}.json"


def _load(date_str: str) -> dict:
    path = _out_path(date_str)
    if not path.exists():
        return {"date": date_str, "runs": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"date": date_str, "runs": {}}


def _save(data: dict) -> None:
    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = _out_path(data["date"])
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _find_run(date_str: str, run_slot: str):
    for run in list_runs_for_date(date_str):
        if run.get("run_slot") == run_slot:
            return run
    return None


def measure(date_str: str, window: dict, delay_min: int) -> dict:
    """그 회차 창을 지금 다시 훑어 **회차에 아직 없는 기사**가 몇 건인지 센다(저장 안 함).

    app.scraper.top_up_last_run과 **같은 창·같은 제외 규칙**을 쓴다 — 여기서 센 건수가
    곧 "그때 보충을 돌렸으면 덧붙었을 건수"여야 하기 때문이다.
    """
    run_slot = window["end"]
    stamp = datetime.now().strftime("%H:%M:%S")
    run = _find_run(date_str, run_slot)
    if run is None:
        return {"delay_min": delay_min, "at": stamp, "skipped": "회차 파일 없음"}
    if usage_ratio() > _USAGE_CEILING:
        return {"delay_min": delay_min, "at": stamp, "skipped": f"API 사용량 {usage_ratio():.0%}"}

    settings = load_settings()
    groups = active_search_groups(
        settings, [g for g in settings.get("keyword_groups", []) if group_in_scrap(g)]
    )
    window_start = kst_today_at(window["start"]) if window.get("start") else None
    after_dt = window_start
    if window_start is not None:
        after_dt = max(
            window_start - timedelta(minutes=COLLECT_LOOKBACK_MIN),
            window_start.replace(hour=0, minute=0, second=0, microsecond=0),
        )
    before_dt = kst_today_at(run_slot)

    calls_before = today_call_count()
    articles, failed = search_articles_by_groups(groups, after=after_dt, before=before_dt)
    calls = today_call_count() - calls_before
    if failed:
        return {"delay_min": delay_min, "at": stamp, "skipped": f"검색 실패 {failed}", "api_calls": calls}

    published = _already_published_urls(date_str)
    fresh = [a for a in articles if a.get("url") not in published]
    if fresh:
        # 선택 언론사 화이트리스트 — collect_run·top_up_last_run이 거는 것과 같은 필터다.
        # 이게 빠지면 목록 밖 매체(지방지·전문지)가 통째로 "새 기사"로 잡혀 건수가 부푼다.
        outlet_order = settings.get("outlet_order", [])
        if outlet_order:
            fresh = filter_by_outlet_whitelist(fresh, outlet_order)
        # 제목 중복 제거도 top_up과 같게 — 저장된 기사를 앞에 둬 대표 자리를 지키게 한다.
        survived = {a["url"] for a in deduplicate_by_title(run.get("articles", []) + fresh)}
        fresh = [a for a in fresh if a["url"] in survived]

    return {
        "delay_min": delay_min,
        "at": stamp,
        "new": len(fresh),
        "api_calls": calls,
        "run_articles": len(run.get("articles", [])),
        "topup_at": run.get("topup_at"),
        "articles": [
            {
                "pub_date": a.get("pub_date"),
                "outlet": a.get("outlet"),
                "title": (a.get("title") or "")[:60],
                "url": a.get("url"),
            }
            for a in fresh
        ],
    }


# 예정 시점에서 이만큼 넘게 지났으면 재지 않는다. 늦게 재면 그 사이 다음 회차가
# 기사를 주워 가(_already_published_urls에 들어가) 0건으로 보이므로, "그때 훑었으면
# 몇 건이었나"라는 물음에 거짓으로 답하게 된다. 스크립트를 오후에 처음 켜도 오늘
# 지난 회차를 소급해 재지 않는 것도 같은 이유다.
_POINT_GRACE_MIN = 5


def _due_points(window: dict, now: datetime) -> list:
    """지금 재야 할 측정 시점들 — 지났지만 아직 유예 안인 것만.

    자정을 넘기는 시점은 아예 빼고 돌려준다(측정 대상이 아니다): 네이버는 당일 기사만
    주므로 00:00 뒤에 어제 회차 창을 훑으면 빈 결과가 오고, 그건 "안 늘었다"가 아니라
    "물어볼 수 없다"이다. app.last_slot_topup이 23:50에서 보충을 자르는 것과 같은 이유다.
    """
    hour, minute = map(int, window["end"].split(":"))
    end_dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    due = []
    for delay in DELAYS_MIN:
        at = end_dt + timedelta(minutes=delay)
        if at.date() != end_dt.date():
            continue
        if at <= now <= at + timedelta(minutes=_POINT_GRACE_MIN):
            due.append(delay)
    return due


def tick(now=None) -> None:
    now = now or datetime.now()
    date_str = now.strftime("%Y-%m-%d")
    data = _load(date_str)
    changed = False
    for window in schedule_times_now(now):
        run_slot = window["end"]
        entry = data["runs"].setdefault(run_slot, {"start": window.get("start"), "points": []})
        done = {p["delay_min"] for p in entry["points"]}
        for delay in _due_points(window, now):
            if delay in done:
                continue
            point = measure(date_str, window, delay)
            entry["points"].append(point)
            entry["points"].sort(key=lambda p: p["delay_min"])
            changed = True
            if "skipped" in point:
                print(f"[{point['at']}] {run_slot} 회차 +{delay}분 — 건너뜀: {point['skipped']}")
            else:
                print(f"[{point['at']}] {run_slot} 회차 +{delay}분 — 새 기사 {point['new']}건 "
                      f"(네이버 호출 {point['api_calls']}회)")
    if changed:
        _save(data)


def report() -> None:
    paths = sorted(_OUT_DIR.glob("*.json"))
    if not paths:
        print("아직 잰 기록이 없습니다. python3 tools/measure_topup.py 를 켜 두세요.")
        return
    # 합계는 **네 시점을 모두 정상적으로 잰 회차**만 넣는다 — 시점마다 분모가 다르면
    # "+5분 평균 4.0 > +10분 평균 3.7" 같은 거꾸로 된 표가 나와 읽는 사람을 속인다.
    totals = {d: 0 for d in DELAYS_MIN}
    counted = 0
    for path in paths:
        data = json.loads(path.read_text(encoding="utf-8"))
        print(f"\n== {data['date']} ==")
        for run_slot in sorted(data["runs"]):
            entry = data["runs"][run_slot]
            cells = []
            full = True
            for delay in DELAYS_MIN:
                point = next((p for p in entry["points"] if p["delay_min"] == delay), None)
                if point is None:
                    cells.append(f"+{delay:>2}분   ·  ")
                    full = False
                elif "skipped" in point:
                    cells.append(f"+{delay:>2}분   ✕  ")
                    full = False
                else:
                    cells.append(f"+{delay:>2}분 {point['new']:>3}건")
            print(f"  {run_slot} 회차 │ " + " │ ".join(cells))
            if full:
                counted += 1
                for delay in DELAYS_MIN:
                    point = next(p for p in entry["points"] if p["delay_min"] == delay)
                    totals[delay] += point["new"]
    print(f"\n== 합계 (네 시점을 모두 잰 회차 {counted}개) ==")
    if not counted:
        print("  아직 네 시점을 다 잰 회차가 없습니다 — 하루 더 돌려 보세요.")
        print("  (· = 그 시각에 스크립트가 안 돌고 있었음, ✕ = 건너뜀)")
        return
    for delay in DELAYS_MIN:
        avg = totals[delay] / counted
        print(f"  마감 +{delay:>2}분에 훑었다면: 총 {totals[delay]:>4}건 · 회차당 평균 {avg:.1f}건")
    print("\n  * 회차당 평균이 앞 시점과 거의 같으면 더 기다릴 값이 없다는 뜻입니다.")
    print("  (· = 그 시각에 스크립트가 안 돌고 있었음, ✕ = API 사용량 등으로 건너뜀)")


def main() -> None:
    parser = argparse.ArgumentParser(description="회차 마감 뒤 보충 훑기 값어치 측정")
    parser.add_argument("--once", metavar="HH:MM", help="그 회차를 지금 한 번만 재본다")
    parser.add_argument("--report", action="store_true", help="쌓인 결과를 표로 본다")
    args = parser.parse_args()

    if args.report:
        report()
        return
    if not naver_is_configured():
        print("네이버 API 키가 없습니다 — 설정 화면(/naver)에서 먼저 등록하세요.")
        return

    now = datetime.now()
    if args.once:
        window = next((w for w in schedule_times_now(now) if w["end"] == args.once), None)
        if window is None:
            print(f"{args.once} 회차가 오늘 시간표에 없습니다.")
            return
        point = measure(now.strftime("%Y-%m-%d"), window, 0)
        print(json.dumps(point, ensure_ascii=False, indent=2)[:2000])
        return

    windows = schedule_times_now(now)
    print(f"측정 시작 — 오늘 회차 {len(windows)}개, 마감 뒤 {DELAYS_MIN}분에 각각 잽니다.")
    print(f"결과는 {_OUT_DIR}/ 에 쌓입니다. 멈추려면 Ctrl+C.\n")
    while True:
        try:
            tick()
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # 재는 일이 앱을 따라 죽으면 안 된다 — 다음 tick에 다시 시도
            print(f"[{datetime.now():%H:%M:%S}] 측정 중 오류(건너뜁니다): {exc}")
        time.sleep(_POLL_SEC)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n멈췄습니다. python3 tools/measure_topup.py --report 로 결과를 볼 수 있습니다.")
