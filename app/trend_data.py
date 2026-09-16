# Design Ref: HISTORY.md "홈 화면 재구성" — 정책 단어 추이의 날짜별 집계 로직.
"""정책 단어 추이(홈 카드 · 전용 화면 /trend 둘 다)가 공유하는 순수 데이터 계산.

"기사 목록을 날짜별로 모으고, 그 안에서 단어가 몇 번 나오는지 센다"는 두 화면이
완전히 똑같이 하는 일이라 여기 한 곳에 모았다 — 렌더링(SVG·표·HTML)은 각 화면
(app.landing_renderer/app.trend_renderer)이 따로 맡는다.
"""
from datetime import datetime, timedelta

from app.storage import list_run_dates, list_runs_for_date


def word_hits(word: str, articles: list) -> int:
    """제목+요약 원문에서 문자열 그대로 찾는다 — 소제목 분류 상태와 무관하게 항상
    셀 수 있는 게 이 카드의 존재 이유다(CLAUDE.md "정책 단어 추이" 참고)."""
    return sum(1 for a in articles if word in (a.get("title", "") + " " + (a.get("summary") or "")))


def articles_by_date_range(start_str: str, end_str: str) -> dict:
    """start_str~end_str(둘 다 포함, YYYY-MM-DD) 구간의 날짜별 기사 목록.

    **숨긴 기사를 빼지 않는다** — 이 카드가 재는 건 "그 단어가 얼마나 언급됐나"라는
    주제 축의 추이지 "보고서에 몇 건을 실었나"가 아니다. 숨김은 "이 회차 보고서에서
    뺀다"는 뜻이지 "그런 기사가 없었다"가 아니고(라벨 보관함이 숨긴 기사도 남기는 것과
    같은 원칙), 절대값이 아니라 추이를 보는 화면이라 담당자의 큐레이션에 값이 흔들리면
    안 된다(사용자 결정, 2026-09-01).

    [수정: 2026-09-01] 그 전엔 hidden_urls를 받아 app.curation.is_hidden으로 걸렀는데,
    숨김 기록(data/hidden_articles.json)이 자정에 초기화되는 오늘 전용 저장소라 **필터가
    오늘 날짜에만 걸렸다** — 과거는 원본 건수, 오늘만 큐레이션 후 건수로 그려져 비교 자체가
    성립하지 않았다. 실측(2026-09-01): "종부세"가 원본 43건인데 그중 42건(같은 속보의
    중복 기사)을 숨겨 그래프엔 1건으로 찍혔고, 같은 창의 8/26(11건)·8/27(8건)은 그날
    숨긴 게 있었어도 기록이 남지 않아 전부 원본 그대로였다. 배경은 HISTORY.md
    "정책 단어 추이가 숨김에 흔들리던 문제" 참고.

    {date_str: [기사, ...] 또는 None} — None은 "그 날짜에 저장된 회차가 아예 없다"는
    뜻이다(0건과 다르다: 0건은 회차는 돌았는데 기사가 없었다는 뜻, None은 회차 자체가
    없어 애초에 모른다는 뜻). 이 구분이 있어야 추이 그래프가 "기록 없음" 구간을 0으로
    그려 뉴스가 없었다고 거짓말하지 않는다.

    list_run_dates()는 인덱스만 보는 캐시라 회차 파일을 한 개도 안 연다(app.run_index) —
    그걸로 먼저 "회차가 있는 날짜"만 걸러낸 뒤, 그 날짜만 list_runs_for_date로 연다.
    """
    stored = set(list_run_dates())
    start = datetime.strptime(start_str, "%Y-%m-%d")
    end = datetime.strptime(end_str, "%Y-%m-%d")
    by_date: dict = {}
    d = start
    while d <= end:
        ds = d.strftime("%Y-%m-%d")
        if ds not in stored:
            by_date[ds] = None
        else:
            seen, arts = set(), []
            for run in list_runs_for_date(ds):
                for a in run.get("articles", []):
                    if a["url"] in seen:
                        continue
                    seen.add(a["url"])
                    arts.append(a)
            by_date[ds] = arts
        d += timedelta(days=1)
    return by_date


def all_registered_keywords(settings: dict) -> list:
    """정기 스크랩(include_in_scrap) 여부와 무관하게, **꺼지지 않은(enabled) 그룹**의
    키워드를 전부 모은다 — app.settings.all_search_keywords와 다른 목적이다.

    실측(2026-08-25): "세제개편"·"추가 검색어" 그룹은 include_in_scrap=False라
    all_search_keywords에는 안 잡히지만, 그 키워드(종부세 등)는 재정경제부·예결위
    검색으로 들어온 기사 본문에 이미 51건이나 등장하고 있었다 — 수집 축(누구·어디를
    검색하나)과 주제 축(무슨 얘기인가)이 다르기 때문이다. 정책 단어 추이는
    주제 축이라 include_in_scrap을 안 본다. enabled=False(그룹 자체를 껐음)만 뺀다.
    """
    flat, seen = [], set()
    for group in settings.get("keyword_groups", []):
        if not group.get("enabled", True):
            continue
        for kw in group.get("keywords", []):
            word = kw["word"] if isinstance(kw, dict) else kw
            if word not in seen:
                seen.add(word)
                flat.append(word)
    return flat


def last_complete_day(today_str: str) -> str:
    """추이가 보여줄 수 있는 마지막 날짜 = 어제.

    [추가: 2026-09-11] 예전엔 오늘까지 그렸는데, 오늘 칸은 그 시각까지 저장된 회차만
    담아 **하루치의 일부**다(실측 2026-09-11 09:48: 회차 2개·23건 — 전날 하루 234건의
    10%). 점선으로 "집계 중"을 표시해도 눈은 점선보다 급락을 먼저 읽어, 모든 단어가
    오늘 무너지는 것처럼 보였다. 이 화면이 답하는 건 "흐름"이고 "오늘 무엇이 떴나"는
    홈의 오늘의 쟁점 카드가 맡으므로, 추이는 하루가 다 찬 날까지만 그린다(사용자 결정).
    배경은 HISTORY.md 같은 항목 참고.
    """
    return (datetime.strptime(today_str, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")


def _bucket_period_end(bucket: dict, unit: str) -> str:
    """그 버킷이 원래 끝나는 날(구간 끝에서 잘렸는지와 무관하게) — 일별은 그날,
    주별은 그 주 일요일, 월별은 그달 말일."""
    first = datetime.strptime(bucket["dates"][0], "%Y-%m-%d")
    if unit == "day":
        return bucket["dates"][0]
    if unit == "week":
        return (first + timedelta(days=6 - first.weekday())).strftime("%Y-%m-%d")
    nxt = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    return (nxt - timedelta(days=1)).strftime("%Y-%m-%d")


def fill_bucket_status(buckets: list, unit: str, by_date: dict, today_str: str) -> None:
    """각 버킷에 record(그 구간에 저장된 회차가 있나)·partial(아직 집계 중인가)을 채운다.

    partial은 "그 버킷의 기간이 아직 안 끝났다" 하나로 판정한다 — 구간 끝이 어제로
    잘리므로 일별 칸은 절대 partial이 아니지만, 주별·월별의 마지막 칸(이번 주·이번 달)은
    어제에서 잘려도 여전히 덜 찬 칸이라 점선으로 남아야 한다. 예전 판정("오늘 날짜가
    그 칸에 들어 있나")을 그대로 두면 끝이 어제가 되는 순간 그 점선이 조용히 사라진다.
    과거 구간을 직접 지정해 주 중간에서 잘린 칸은 기간이 이미 끝났으므로 partial이 아니다.

    yesterday는 "일별 칸이고 그 날이 어제인가" — [추가: 2026-09-14] 그래프가 그 칸 날짜
    밑에 「어제」를 붙이는 데 쓴다. 주별·월별 칸은 하루가 아니라서 늘 False다.
    """
    yday = last_complete_day(today_str)
    for b in buckets:
        b["record"] = any(by_date.get(d) is not None for d in b["dates"])
        b["partial"] = _bucket_period_end(b, unit) >= today_str
        b["yesterday"] = unit == "day" and b["dates"] == [yday]


_KRDAY = ("월", "화", "수", "목", "금", "토", "일")  # datetime.weekday() 0=월 순서


def build_buckets(start_str: str, end_str: str) -> tuple:
    """start_str~end_str 구간을 일/주/월 중 하나로 나눠 담을 빈 버킷 목록을 만든다.

    단위는 구간 길이로 자동 결정한다 — ~31일 일별, ~100일 주별, 그 이상 월별
    (CLAUDE.md "정책 단어 추이" 참고). 각 버킷은 {label, tip_label, dates, weekend}
    만 담고 있다 — "그 구간에 저장된 회차가 있는가(record)"·"단어별 건수(values)"는
    호출부가 app.trend_data.articles_by_date_range 결과와 맞춰 채운다(이 함수는 그
    데이터를 몰라도 되는 순수 날짜 계산이라서다).

    주별 버킷의 key는 그 주의 월요일 날짜, 월별 버킷의 key는 "YYYY-MM"이다 — 이
    key로 나중에 실제 날짜(dates)와 매칭한다.
    """
    start = datetime.strptime(start_str, "%Y-%m-%d")
    end = datetime.strptime(end_str, "%Y-%m-%d")
    days = (end - start).days + 1
    unit = "day" if days <= 31 else ("week" if days <= 100 else "month")

    all_dates = []
    d = start
    while d <= end:
        all_dates.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)

    buckets: list = []
    if unit == "day":
        for ds in all_dates:
            dt = datetime.strptime(ds, "%Y-%m-%d")
            wd = dt.weekday()
            buckets.append({
                "label": f"{dt.month}/{dt.day}",
                "wd_label": f"{dt.month}/{dt.day}({_KRDAY[wd]})",
                # [추가: 2026-09-14] 그래프 x축 전용(요일까지 다 찍히는 10칸 이하일 때만) —
                # 표·복사·엑셀은 1개월 일별에서 달이 바뀌므로 월이 있는 wd_label을 그대로 쓴다.
                "short_label": f"{dt.day}일({_KRDAY[wd]})",
                "tip_label": f"{ds} ({_KRDAY[wd]})",
                "dates": [ds],
                "weekend": wd >= 5,
                "is_monday": wd == 0,
            })
    elif unit == "week":
        cur = None
        for ds in all_dates:
            dt = datetime.strptime(ds, "%Y-%m-%d")
            mon = dt - timedelta(days=dt.weekday())
            key = mon.strftime("%Y-%m-%d")
            if cur is None or cur["key"] != key:
                cur = {
                    "key": key, "label": f"{mon.month}/{mon.day}",
                    "tip_label": f"{mon.strftime('%Y-%m-%d')} 주", "dates": [],
                    "weekend": False, "is_monday": False,
                }
                buckets.append(cur)
            cur["dates"].append(ds)
    else:  # month
        cur = None
        for ds in all_dates:
            key = ds[:7]
            if cur is None or cur["key"] != key:
                y, m = key.split("-")
                label = f"{y[2:]}.{int(m)}월" if (not buckets or m == "01") else f"{int(m)}월"
                cur = {
                    "key": key, "label": label, "tip_label": f"{y}년 {int(m)}월",
                    "dates": [], "weekend": False, "is_monday": False,
                }
                buckets.append(cur)
            cur["dates"].append(ds)

    return buckets, unit
