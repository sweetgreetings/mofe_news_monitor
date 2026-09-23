# Design Ref: PLAN.md #17 — 전체 흐름(스크랩 -> 분류 -> 화면 표시 -> 복사/내보내기) 통합 테스트
#
# pytest 같은 테스트 프레임워크 없이, 지금까지 각 작업을 검증할 때 쓰던 것과 같은 방식
# (assert + print)으로 작성한다. 네이버 API 호출 경계(app.naver_api._search_one_keyword)만
# 목으로 바꾸고, 그 뒤(정렬·필터·분류·요약·저장·렌더링·설정)는 전부 실제 코드를 그대로 돌린다.
#
# 실제 데이터 폴더(data/articles, index.html, settings.json)는 절대 건드리지 않는다 —
# 임시 폴더로 각 모듈의 경로 상수를 바꿔치기해서, 테스트가 실사용자 데이터를 지우는 사고를
# 원천적으로 막는다.
#
# 실행: python3 tests/test_integration.py
import json
import sys
import tempfile
from contextlib import ExitStack
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import alerted_urls, config, credentials, api_usage, breaking_alert, breaking_alert_sender, breaking_alert_state, confirm_send, curation, custom_groups, draft_articles, draft_seen, history_renderer, landing_renderer, llm_classifier, naver_api, preview_cache, preview_order, preview_renderer, renderer, scheduler, scraper, settings, storage, telegram_bot, telegram_recipients, negative_guess, today_cuts, auto_classify_turn, photo_body, breaking_burst, send_log  # noqa: E402
import main  # noqa: E402

_FIXTURE_BY_KEYWORD = {
    "재정경제부": [
        {"originallink": "https://kbs.co.kr/1", "link": "https://kbs.co.kr/1",
         "title": "[포토] 재정경제부 브리핑 현장", "description": "사진 기사라 제외돼야 함"},
        {"originallink": "https://joongang.co.kr/2", "link": "https://joongang.co.kr/2",
         "title": "재정경제부 세제 개편 발표", "description": "재정경제부는 세제 개편안을 발표했다."},
        {"originallink": "https://imnews.imbc.com/3", "link": "https://imnews.imbc.com/3",
         "title": "재정경제부 세제 개편 발표", "description": "동일 제목, MBC(방송사)가 더 우선순위 높음"},
    ],
    "재경부": [
        {"originallink": "https://hankyung.com/4", "link": "https://hankyung.com/4",
         "title": "환율 급등 재경부 대응", "description": "환율이 급등하자 재경부가 대책을 내놨다."},
    ],
}


def _fake_search_one_keyword(keyword, after=None, before=None, include_unparsed_dates=False):
    # [수정: 2026-08-24] _search_one_keyword의 반환값이 (기사 목록, 조회 상한에 걸렸는지)
    # 튜플로 바뀌어(app.adhoc.collector의 §6.4b AND 상한 오판 수정), 이 목도 같은 모양을
    # 돌려줘야 한다 — 테스트 픽스처는 상한에 걸릴 일이 없으므로 항상 False.
    return (
        [
            {
                # [수정: 2026-08-14] 실제 수집 경로와 같은 판별 함수를 쓴다(네이버 oid 우선,
                # 없으면 도메인 폴백) — 테스트가 실제와 다른 경로를 타면 의미가 없다.
                "outlet": naver_api.resolve_outlet(item["link"], item["originallink"]),
                "title": item["title"],
                "url": item["link"],
                "summary": item["description"],
            }
            for item in _FIXTURE_BY_KEYWORD.get(keyword, [])
        ],
        False,
    )


def _article(key: str) -> dict:
    """테스트용 최소 기사 한 건."""
    return {"outlet": "테스트일보", "title": f"제목-{key}", "url": f"https://example.com/{key}", "summary": ""}


# [추가: 2026-09-04] **격리 밖에서 테스트를 실행하면 즉시 멈춘다.**
#
# 아래 main()이 테스트마다 임시 폴더를 patch.object로 갈아끼우지만, 그 하네스를 건너뛰고
# 테스트 함수를 직접 부르면(예: `python3 -c "import tests.test_integration as t;
# t.test_late_pickup_...()"` — 실제로 2026-09-04에 이렇게 진단하다 사고가 났다) patch가
# 하나도 안 걸린 채 **진짜 data/articles/에 회차 파일을 쓰고 덮어쓴다.** 그날
# 2026-09-03_14-00(147건)과 2026-09-03_11-00(38건)이 그렇게 날아갔다(14:00은 undo
# 스냅샷에서 복구, 11:00은 복구 불가).
#
# 테스트가 "실제 data/를 안 건드린다"는 약속은 하네스를 거쳤을 때만 참이었는데, 그 조건이
# 어디에도 강제돼 있지 않았다 — 이제 모든 테스트가 첫 줄에서 이걸 확인한다.
_REAL_ARTICLES_DIR = config.ARTICLES_DIR


def _require_isolation():
    """임시 폴더로 격리되지 않은 상태면 예외를 던진다(테스트마다 첫 줄에서 부른다)."""
    if Path(storage.ARTICLES_DIR).resolve() == Path(_REAL_ARTICLES_DIR).resolve():
        raise RuntimeError(
            "격리되지 않은 채로 테스트를 실행했습니다 — 실제 data/articles/를 덮어쓸 수 있습니다.\n"
            "테스트는 반드시 `python3 tests/test_integration.py`로 전체 실행하세요"
            "(개별 함수를 직접 호출하면 임시 폴더 patch가 걸리지 않습니다)."
        )


def test_full_pipeline_happy_path():
    """스크랩 -> 정렬 -> 필터 -> 분류 -> 화면 -> 복사/내보내기 텍스트까지 한 번에 검증한다.

    "재정경제부"·"재경부"는 "기관 정보" 그룹의 기본값(app.config.BASE_GROUP_KEYWORDS)에
    이미 포함돼 있어, 최초 설정 파일이 만들어질 때(app.settings._default_settings)
    자동으로 심어지므로 별도로 그룹을 등록하지 않아도 검색된다.
    """
    _require_isolation()

    with patch.object(naver_api, "_search_one_keyword", side_effect=_fake_search_one_keyword):
        result = scraper.collect_run_with_retry("09:00")

    # [수정: 2026-09-18] 사진·인사 기사 제외 설정은 없앴다 — 포토 기사도 늘 수집된다
    # (📷 배지·「사진 추정 모아 보기」로 화면에서만 가려 본다).
    titles = [a["title"] for a in result["articles"]]
    assert "[포토] 재정경제부 브리핑 현장" in titles, "포토 기사도 수집돼야 함"
    assert titles.count("재정경제부 세제 개편 발표") == 1, "완전 동일 제목 중복이 제거되지 않음"
    kept = next(a for a in result["articles"] if a["title"] == "재정경제부 세제 개편 발표")
    assert kept["outlet"] == "MBC", f"우선순위 높은 MBC가 아니라 {kept['outlet']}가 남음"
    print("1) 검색/정렬/필터: 통과 (포토 기사 포함, 중복 제거 시 우선순위 언론사 유지)")

    # [추가: 2026-08-10] 확정·전송 2단계 유예 도입으로 수집 직후엔 confirmed=False로
    # 저장된다(app.storage.save_run) — generate_screen은 확정된 회차만 그리므로,
    # 화면 렌더링 자체를 검증하는 이 테스트에서는 실제 (확정) 버튼 클릭을 흉내내 먼저
    # 확정 처리한다(app.confirm_send가 하는 일과 동일).
    storage.confirm_run(result)

    path = renderer.generate_screen()
    html_text = path.read_text(encoding="utf-8")
    assert "언론 모니터링 9시 기준" in html_text, "헤더 문구 누락"
    assert "<세제>" in html_text or "세제" in html_text, "소제목 분류 결과가 화면에 없음"
    assert "하나도 없어요" not in html_text
    print("2) 화면 렌더링: 통과 (헤더 표시, 소제목 분류 반영)")

    assert "PLAIN_TEXT" in html_text
    plain_start = html_text.index("const PLAIN_TEXT = ") + len("const PLAIN_TEXT = ")
    plain_end = html_text.index(";\n", plain_start)
    plain_text = json.loads(html_text[plain_start:plain_end])
    assert plain_text.startswith("언론 모니터링 9시 기준"), "복사/내보내기 텍스트 헤더 누락"
    assert "🤖" not in plain_text and "💬" not in plain_text, "복사/내보내기에 하단 요약 블록이 포함됨(제외 대상)"
    print("3) 복사/내보내기 텍스트: 통과 (헤더 포함, 하단 AI 블록 제외)")


def test_settings_actually_change_scraping():
    """설정 화면에서 저장한 키워드 그룹이 실제 스크랩 호출에 그대로 반영되는지 확인한다.

    [수정: 2026-07-25] "기관 정보" 그룹은 더는 코드가 고정 주입하지 않는다 — 최초 1회만
    기본값으로 심어질 뿐, 사용자가 save_keyword_groups로 저장하면 그 내용을 통째로
    대체한다(다른 그룹처럼 이름 변경·삭제가 자유로워야 하므로).

    [수정: 2026-07-25] "정기 스크랩에도 포함"(include_in_scrap)이 켜진 그룹만 예정된
    회차 검색에 쓰인다 — 실시간 기사 현황은 여전히 그룹 전체를 보되, 스크랩은 이걸로
    좁혀진다. 꺼진 그룹은 collect_run에 안 넘어가는 것까지 함께 확인한다.

    [수정: 2026-07-25] 키워드별 ON/OFF(disabled_keywords)도 정기 스크랩 검색에서
    실제로 빠지는지 함께 확인한다 — "외환"을 꺼둔 채 저장하면 검색에는 "환율"만 남아야 한다.
    """
    _require_isolation()
    settings.save_keyword_groups(
        [
            {
                "name": "환율그룹",
                "keywords": ["환율", "외환"],
                "mode": "AND",
                "include_in_scrap": True,
                "disabled_keywords": ["외환"],
            },
            {"name": "부동산그룹", "keywords": ["부동산"], "mode": "OR", "include_in_scrap": False},
        ]
    )
    captured = {}

    def spy(groups, after=None, before=None, track_keyword_matches=False):
        captured["groups"] = groups
        captured["track_keyword_matches"] = track_keyword_matches
        return [], []

    with patch.object(scraper, "search_articles_by_groups", spy):
        scraper.collect_run("10:30")  # groups 생략 -> 설정값을 읽어야 함

    assert len(captured["groups"]) == 1, "정기 스크랩에는 include_in_scrap이 꺼진 그룹(부동산그룹)이 섞이면 안 됨"
    assert captured["groups"][0]["name"] == "환율그룹"
    assert captured["groups"][0]["keywords"] == ["환율"], "꺼둔 키워드(외환)는 실제 검색에서 빠져야 함"
    # [추가: 2026-08-21] 확정본·초안이 "이 기사가 어떤 검색어로 걸렸는지"를 보여주려면
    # 수집 시점에 matched_keywords를 함께 받아 저장해야 한다(나중에 다시 계산할 방법이 없다).
    assert captured["track_keyword_matches"] is True, "정기 스크랩은 매칭 검색어를 함께 받아 저장해야 함"
    print("4) 설정 연동: 통과 (정기 스크랩은 include_in_scrap 켜진 그룹·ON 상태 키워드만 실제 스크랩에 반영됨)")


def test_empty_run_shows_placeholder():
    """이번 회차에 기사가 하나도 없으면 '💤'가 표시되는지 확인한다.

    별도 그룹을 저장하지 않아도 최초 설정 파일 생성 시 심어지는 "기관 정보" 기본
    그룹만으로 검색이 실행된다.
    """
    _require_isolation()
    with patch.object(naver_api, "_search_one_keyword", return_value=([], False)):
        result = scraper.collect_run_with_retry("13:30")
    storage.confirm_run(result)  # [추가: 2026-08-10] 위 테스트와 같은 이유
    html_text = renderer.generate_screen().read_text(encoding="utf-8")
    assert "💤" in html_text
    assert "언론 모니터링 13시 30분 기준" in html_text
    print("5) 빈 회차 처리: 통과 ('💤' 표시)")


def test_search_window_filters_by_time():
    """회차별 시간창 수집: after~before 구간 밖의 기사가 걸러지는지 확인한다 (PRD.md 기능1 규칙 2).

    naver_api._search_one_keyword 안쪽의 실제 필터링 로직을 검증해야 하므로, 이 테스트만
    한 단계 더 깊은 경계(requests.get)를 목으로 바꾼다.
    """
    _require_isolation()

    def _item(hour, minute, idx):
        pub = naver_api.kst_today_at(f"{hour:02d}:{minute:02d}")
        return {
            "originallink": f"https://chosun.com/{idx}",
            "link": f"https://chosun.com/{idx}",
            "title": f"기사{idx}",
            "description": "설명",
            "pubDate": pub.strftime("%a, %d %b %Y %H:%M:%S +0900"),
        }

    items = [_item(11, 0, 1), _item(10, 0, 2), _item(8, 0, 3), _item(6, 0, 4)]  # 최신순(API 응답과 동일)
    response = MagicMock()
    response.json.return_value = {"items": items}
    response.raise_for_status.return_value = None

    after = naver_api.kst_today_at("07:00")
    before = naver_api.kst_today_at("10:30")

    # [추가: 2026-08-20] 이 테스트만 requests.get을 직접 목으로 바꿔 실제 요청 루프를
    # 그대로 타므로, 그 루프 안의 app.api_usage.record_api_call()도 실제로 호출된다 —
    # 실제 data/api_usage.json을 건드리지 않도록 임시 경로로 바꿔치기한다.
    with tempfile.TemporaryDirectory() as usage_tmp, \
         patch.object(api_usage, "API_USAGE_FILE", Path(usage_tmp) / "api_usage.json"), \
         patch.object(naver_api.requests, "get", return_value=response):
        results, capped = naver_api._search_one_keyword("키워드", after=after, before=before)

    assert capped is False, "1000건 상한에 걸릴 만큼 많은 픽스처가 아닌데 capped=True로 나옴"
    titles = {r["title"] for r in results}
    assert titles == {"기사2", "기사3"}, f"07:00(제외)~10:30(포함) 구간엔 기사2·기사3만 남아야 하는데 {titles}"
    print("6) 회차별 시간창 필터: 통과 (창 밖 기사 제외, 경계는 하한 제외·상한 포함)")


def test_retention_cleanup_removes_old_runs():
    """7일 지난 회차가 자동 삭제 대상에 걸리는지 확인한다."""
    _require_isolation()
    now = datetime(2026, 7, 23, 12, 0)
    old_path = storage.save_run("09:00", [], run_at=(now - timedelta(days=8)).isoformat(timespec="seconds"))
    deleted = storage.delete_expired_runs(retention_days=7, now=now)
    assert old_path in deleted
    assert not old_path.exists()
    print("7) 보관 기간 정리: 통과 (8일 지난 회차 삭제됨)")


def test_run_filename_follows_run_at_not_save_time():
    """회차 파일명 날짜는 저장 시각이 아니라 run_at(실제 실행 시각)에서 나와야 한다.

    [추가: 2026-08-26] 예전엔 datetime.now()로 파일명을 지어서, 자정을 넘겨 저장된
    회차가 엉뚱한 날짜 파일에 꽂혔다(실측: run_at이 08-25 19:33인 회차가
    2026-08-26_19-30.json으로 저장됨). 그 결과 두 가지가 깨졌다 —
      1) run_exists가 파일명으로 판단해 "오늘 그 회차는 이미 돌았다"고 오판(회차 통째 누락)
      2) 같은 이름이 다시 필요해지면 앞 회차를 덮어써 기사가 조용히 사라짐
    이 테스트는 그 두 가지가 다시 생기지 않는지를 함께 본다.
    """
    _require_isolation()
    yesterday_evening = "2026-08-25T19:33:47"
    today_evening = "2026-08-26T19:31:02"

    # ① 어제 저녁에 실행된 회차 — 오늘 저장되더라도 파일명은 어제여야 한다.
    p_yesterday = storage.save_run("19:30", [_article("a")], run_at=yesterday_evening)
    assert p_yesterday.name == "2026-08-25_19-30.json", p_yesterday.name

    # ② 오늘 같은 슬롯이 정상 수집되면 별도 파일이어야 한다(덮어쓰기 금지).
    p_today = storage.save_run("19:30", [_article("b"), _article("c")], run_at=today_evening)
    assert p_today.name == "2026-08-26_19-30.json", p_today.name
    assert p_today != p_yesterday, "같은 파일로 저장되면 앞 회차 기사가 사라진다"
    assert len(json.loads(p_yesterday.read_text(encoding="utf-8"))["articles"]) == 1

    # ③ run_exists가 날짜별로 정확히 갈라야 한다.
    assert storage.run_exists("19:30", "2026-08-25")
    assert storage.run_exists("19:30", "2026-08-26")
    assert not storage.run_exists("19:30", "2026-08-24")

    # ④ run_at이 깨져 있어도 저장 자체는 실패하지 않는다(기사를 잃지 않는다).
    p_broken = storage.save_run("07:00", [_article("d")], run_at="이건날짜가아님")
    assert p_broken.exists()

    print("13) 회차 파일명 날짜: 통과 (run_at 기준으로 짓고, 같은 슬롯이라도 날짜가 다르면 안 덮어씀)")


def test_stale_run_shows_waiting_page():
    """자정이 지나 어제 회차만 남아있으면 generate_screen이 예외를 내고, 그 자리에
    안내 화면(🐰⏱️)을 대신 보여줄 수 있는지 확인한다 (실제 반영은 app.scheduler가 담당)."""
    _require_isolation()
    yesterday = (datetime.now() - timedelta(days=1)).isoformat(timespec="seconds")
    storage.save_run("16:30", [], run_at=yesterday)

    assert not storage.is_today(storage.load_latest_run())
    try:
        renderer.generate_screen()
        assert False, "어제 회차만 있으면 generate_screen은 RuntimeError를 내야 함"
    except RuntimeError:
        pass

    html_text = renderer.generate_waiting_page().read_text(encoding="utf-8")
    assert "🐰" in html_text and "⏱️" in html_text
    print("8) 자정 이후 안내 화면: 통과 (어제 회차는 스크린에 안 쓰이고, 안내 화면으로 대체됨)")


def test_scheduler_shows_waiting_before_first_slot():
    """오늘 아직 끝난 회차가 없을 때(current_active_slot이 None)만 run_scheduler가
    render_waiting을 호출하는지 확인한다 — 회차가 있는 시간대엔 호출하면 안 된다."""
    _require_isolation()
    schedule_times = [{"start": "00:00", "end": "09:00"}, {"start": "09:00", "end": "10:30"}]
    render_waiting_calls = []
    ticks = {"count": 0}

    def fake_should_continue():
        ticks["count"] += 1
        return ticks["count"] <= 2  # 두 번만 돌고 멈춘다 (03:00 -> 11:00)

    now_sequence = [datetime(2026, 7, 26, 3, 0), datetime(2026, 7, 26, 11, 0)]

    scheduler.run_scheduler(
        now_fn=lambda: now_sequence[ticks["count"] - 1],
        sleep=lambda _seconds: None,
        schedule_times=schedule_times,
        scrape=lambda run_slot, window_start: {},
        cleanup=lambda: [],
        # [추가: 2026-08-21] 아래 셋을 안 막으면 이 테스트가 **실제 data/를 건드린다** —
        # run_scheduler의 기본값이 진짜 함수라서, 주입하지 않으면 tick마다
        # data/group_overrides.json을 고치고(cleanup_overrides), 확정 회차를 텔레그램·
        # 이메일로 자동발송하고(check_confirm_send), 네이버 API를 호출해 [단독]·[속보]
        # 알림까지 보낸다(check_breaking_alert). 실제로 group_overrides.json이 이 테스트
        # 때문에 다시 쓰이는 걸 확인했다(CODING_CONVENTIONS.md §1 — 재봤더니 그랬다).
        cleanup_overrides=lambda: 0,
        check_confirm_send=lambda _now: None,
        check_breaking_alert=lambda _now: None,
        check_negative_guess=lambda _now: None,
        render_waiting=lambda: render_waiting_calls.append(True),
        should_continue=fake_should_continue,
    )

    assert render_waiting_calls == [True], "03:00(첫 회차 전)에만 호출되고, 11:00(회차 지남)엔 호출되면 안 됨"
    print("9) 스케줄러 안내 화면 트리거: 통과 (첫 회차 전에만 render_waiting 호출)")


def test_empty_round_keeps_previous_screen():
    """오늘 이미 앞선 회차가 있는데 다음 회차가 0건이면, 메인 화면은 갱신하지 않고
    이전 회차 화면을 그대로 둔다(사용자 논의 결과) — history.html에는 여전히 그 회차가
    💤로 기록된다.

    [수정: 2026-08-11] 확정 대기 단계를 없애면서(사용자 피드백 — 초안이 다음 회차
    미리보기로 안 넘어가 혼란) 수집만 하면 곧바로 확정본까지 반영된다. 예전엔 여기서
    회차마다 confirm_and_promote를 따로 호출해줘야 했지만 이제 불필요하다 — 그 "수집
    즉시 확정"이 실제로 지켜지는지도 함께 확인한다.
    """
    _require_isolation()
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        with patch.object(history_renderer, "HISTORY_HTML_PATH", tmp_dir / "history.html"), \
             patch.object(landing_renderer, "LANDING_HTML_PATH", tmp_dir / "home.html"):
            with patch.object(naver_api, "_search_one_keyword", side_effect=_fake_search_one_keyword):
                main._scrape_and_render("09:00", "00:00")
            assert storage.load_latest_run()["confirmed"], "수집 즉시 확정돼야 함(확정 대기 없음)"
            before = renderer.OUTPUT_HTML_PATH.read_text(encoding="utf-8")
            assert "언론 모니터링 9시 기준" in before

            with patch.object(naver_api, "_search_one_keyword", return_value=([], False)):
                main._scrape_and_render("10:30", "09:00")
            after = renderer.OUTPUT_HTML_PATH.read_text(encoding="utf-8")
            assert after == before, "0건 회차라도 오늘 이전 회차가 있으면 메인 화면을 덮어쓰면 안 됨"

            history_text = history_renderer.HISTORY_HTML_PATH.read_text(encoding="utf-8")
            assert "뉴스가 잠잠" in history_text, "history.html에는 0건 회차가 여전히 기록돼야 함"
    print("10) 0건 회차 처리: 통과 (메인 화면 유지, history엔 💤로 기록)")


def test_hidden_articles_newest_first():
    """숨긴 기사 관리 화면에서 가장 최근에 숨긴 기사가 맨 위에 오는지 확인한다.

    [수정: 2026-09-16] **숨김은 자정이 아니라 HIDDEN_VIEW_DAYS(7일) 뒤에 풀린다** —
    휴지통에 보이는 동안은 판정도 살아 있어야 "보이는데 못 되살리는 줄"이 안 생긴다
    (그 전엔 필터만 오늘 하루였다). 옛 형식(URL 문자열 목록)은 언제 숨겼는지 알 수 없어
    그냥 버려지는 것도 함께 확인한다(마이그레이션하지 않음).
    """
    _require_isolation()
    with tempfile.TemporaryDirectory() as tmp:
        hidden_file = Path(tmp) / "hidden_articles.json"
        with patch.object(curation, "HIDDEN_ARTICLES_FILE", hidden_file):
            curation.hide_article("https://a.com/1")
            curation.hide_article("https://b.com/2")
            curation.hide_article("https://c.com/3")
            order = [r["url"] for r in curation.load_hidden_records()]
            assert order == ["https://c.com/3", "https://b.com/2", "https://a.com/1"], \
                f"가장 최근에 숨긴 순서(c,b,a)여야 하는데 {order}"

            curation.unhide_article("https://b.com/2")
            assert [r["url"] for r in curation.load_hidden_records()] == ["https://c.com/3", "https://a.com/1"]
            assert curation.load_hidden_urls() == {"https://c.com/3", "https://a.com/1"}

            # 익일 0시 이후 — **숨김도 기록도 그대로 살아 있다**([수정: 2026-09-16]).
            tomorrow = datetime.now() + timedelta(days=1)
            assert curation.load_hidden_urls(now=tomorrow) == {"https://c.com/3", "https://a.com/1"}, \
                "자정이 지나도 숨김은 유지돼야 한다 — 휴지통에 보이는 동안은 되살릴 수 있어야 하므로"
            assert [r["url"] for r in curation.load_hidden_records(now=tomorrow)] == \
                ["https://c.com/3", "https://a.com/1"], "휴지통 열람은 7일치가 그대로 보여야 한다"
            assert curation.active_hidden_dates(now=tomorrow) >= {
                tomorrow.strftime("%Y-%m-%d"), datetime.now().strftime("%Y-%m-%d")
            }, "화면이 '되살릴 수 있는 줄'을 가르는 기준은 판정과 같은 창이어야 한다"
            # HIDDEN_VIEW_DAYS(7일)를 넘기면 판정·열람 양쪽에서 함께 빠진다(그 기사는 다시 보인다).
            next_week = datetime.now() + timedelta(days=curation.HIDDEN_VIEW_DAYS)
            assert curation.load_hidden_records(now=next_week) == []
            assert curation.load_hidden_urls(now=next_week) == set()

            # 이미 숨긴 기사를 다음 날 또 숨겨도 기록이 두 줄로 늘지 않는다 — 한 줄만
            # 복구해도 안 돌아오는 "유령 기록"이 생기면 안 된다.
            curation.hide_article("https://c.com/3", now=tomorrow)
            assert [r["url"] for r in curation.load_hidden_records(now=tomorrow)] == \
                ["https://c.com/3", "https://a.com/1"]
            # 복구는 날짜를 안 가리고 그 URL의 기록을 지운다(어제 숨긴 것도 오늘 되살아난다).
            curation.unhide_article("https://c.com/3", now=tomorrow)
            assert curation.load_hidden_urls(now=tomorrow) == {"https://a.com/1"}
            assert [r["url"] for r in curation.load_hidden_records(now=tomorrow)] == ["https://a.com/1"]

            # 옛 형식(문자열 목록)은 언제 숨겼는지 몰라 그냥 버려진다(마이그레이션 안 함).
            hidden_file.write_text(json.dumps(["https://old.com/1"]), encoding="utf-8")
            assert curation.load_hidden_records() == []

            # 소제목 통째·일괄 숨기기는 한 요청으로 온다 — 기사마다 url·outlet·title·pubDate·group이
            # 같은 순서로 되풀이되고, 빈 값도 자리를 지킨다. 되돌리기·화면 재생성은 한 번씩만.
            from app import settings_server as ss
            handler = object.__new__(ss._SettingsHandler)
            handler.send_response = handler.send_header = lambda *a: None
            handler.end_headers = lambda: None
            regenerated, pushed = [], []
            handler._regenerate_screens = lambda: regenerated.append(1)
            form = {
                "url": ["https://g.com/1", "https://g.com/2", "https://a.com/1", ""],
                "outlet": ["한겨레", "", "", ""],
                "title": ["제목1", "제목2", "", ""],
                "pubDate": ["", "", "", ""],
                "group": ["세제", "세제", "", ""],
                "batch": ["b1"],
            }
            with patch.object(ss, "undo_push", lambda label, **kw: pushed.append(kw["touched"])), \
                 patch.object(ss, "_hide_meta_fallbacks", lambda urls: {"https://g.com/2": {"outlet": "경향신문"}}):
                handler._handle_hide_article(form)
            assert regenerated == [1] and pushed == [["https://g.com/1", "https://g.com/2", "https://a.com/1"]], \
                f"되돌리기·재생성은 한 번씩 — {regenerated} {pushed}"
            records = {r["url"]: r for r in curation.load_hidden_records()}
            assert set(records) == {"https://g.com/1", "https://g.com/2", "https://a.com/1"}, "빈 url은 건너뛰고 이미 숨긴 건 한 줄"
            assert records["https://g.com/1"]["outlet"] == "한겨레" and records["https://g.com/2"]["title"] == "제목2"
            assert records["https://g.com/2"]["outlet"] == "경향신문", "빠진 값은 서버 폴백으로 채운다"
            assert records["https://g.com/1"]["hidden_at"] < records["https://g.com/2"]["hidden_at"], "보낸 순서대로 시각이 벌어져야 한다"
    print("11) 숨긴 기사 정렬 + 7일 유지: 통과 (최근 숨긴 순, 옛 형식은 폐기, 7일 동안 숨김·복구 유지, 8일째 해제, 여러 건 한 요청)")


def test_breaking_alert_scoop_detection():
    """[단독]·[속보] 알림: 말머리 붙은 기사만 걸러 [단독] 알림 수신자에게 보내고,
    한 번 보낸 기사는 다시 처리하지 않는지 확인한다(app.breaking_alert_sender.
    detect_and_alert) — 정기 회차 경로(app.scraper.collect_run)와 폴링·몰아보내기
    경로가 공유하는 핵심 함수라 여기서 한 번만 검증하면 셋 다 보장된다.
    """
    _require_isolation()
    articles = [
        # 첫 기사만 pub_date가 있다 — 게시 시각 줄이 붙는 기사와 안 붙는 기사를 한
        # 번에 검증하려는 것이다(값이 없으면 지어내지 않고 줄을 통째로 뺀다).
        {"outlet": "서울경제", "title": "[단독] 정부 개편안 검토", "url": "https://example.com/1",
         "summary": "", "pub_date": "2026-09-16T15:31:00+09:00"},
        {"outlet": "한국경제", "title": "평범한 기사 제목", "url": "https://example.com/2", "summary": ""},
        {"outlet": "매일경제", "title": "[단독] 시각 없는 기사", "url": "https://example.com/3", "summary": ""},
    ]
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        with patch.object(telegram_recipients, "TELEGRAM_RECIPIENTS_FILE", tmp_dir / "telegram_recipients.json"), \
             patch.object(alerted_urls, "ALERTED_URLS_FILE", tmp_dir / "alerted_urls.json"), \
             patch.object(settings, "SETTINGS_FILE", tmp_dir / "settings.json"), \
             patch.object(credentials, "telegram_bot_token", lambda: "fake-token"), \
             patch.object(telegram_bot.requests, "post") as mock_post:
            mock_post.return_value = MagicMock(ok=True)
            # "나"는 [단독]만 켜고 [속보]·정기 발송(enabled)은 껐다 — 세 축이 서로
            # 독립적으로 동작하는지까지 이 한 사람으로 검증한다.
            telegram_recipients.save_telegram_recipients(
                [{"name": "나", "chat_id": "111", "enabled": False, "alert_scoop": True, "alert_flash": False}]
            )

            sent = breaking_alert_sender.detect_and_alert(
                articles, now=datetime(2026, 9, 16, 15, 40)
            )
            assert sent == 2, f"[단독] 기사 2건만 알림 대상으로 처리돼야 하는데 {sent}"
            assert mock_post.call_count == 1, "[단독] 수신자에게 1회 전송돼야 함"
            sent_text = mock_post.call_args.kwargs["data"]["text"]
            assert "[단독] 정부 개편안 검토" in sent_text, "알림 본문에 [단독] 기사가 있어야 함"
            assert "평범한 기사 제목" not in sent_text, "말머리 없는 기사가 알림에 섞이면 안 됨"
            # [수정: 2026-09-16] 게시 시각은 URL 다음 줄(덩어리 맨 아래)에 온다.
            assert "https://example.com/1\n15:31 게시" in sent_text, (
                f"게시 시각이 URL 다음 줄에 있어야 함: {sent_text!r}"
            )
            assert "[단독] 시각 없는 기사\nhttps://example.com/3" in sent_text, (
                "pub_date가 없으면 게시 시각 줄 없이 예전 그대로 2줄이어야 함"
            )

            mock_post.reset_mock()
            # 첫 호출과 같은 now를 넘긴다 — 중복 방지 기록은 하루 단위라, 실제 시계를 쓰면
            # 테스트를 돌린 날짜가 첫 호출의 날짜와 다를 때 "새 날"로 보고 다시 보낸다.
            sent_again = breaking_alert_sender.detect_and_alert(articles, now=datetime(2026, 9, 16, 15, 40))
            assert sent_again == 0, "이미 보낸 기사는 다음 호출에서 다시 처리되면 안 됨"
            assert mock_post.call_count == 0, "같은 기사를 중복 발송하면 안 됨"
    # [추가: 2026-09-16] 앱 시작 몰아보내기 문구는 그 묶음이 실제로 밤(23시~6시)에
    # 나왔을 때만 "밤사이·새벽"이라고 부른다 — 낮에 앱을 다시 켜면 그 낮 기사까지
    # 딸려 오는 경로라, 늘 밤이라고 말하면 거짓말이 된다(사용자 제보: 16시 24분 기사).
    night = [{"pub_date": "2026-09-15T23:40:00+09:00"}, {"pub_date": "2026-09-16T05:59:00+09:00"}]
    assert "밤사이" in breaking_alert_sender._catch_up_header("단독", night)
    for label, batch in (
        ("낮 기사", [{"pub_date": "2026-09-16T16:24:00+09:00"}]),
        ("밤+낮 섞임", night + [{"pub_date": "2026-09-16T16:24:00+09:00"}]),
        ("6시 정각", [{"pub_date": "2026-09-16T06:00:00+09:00"}]),
        ("발행시각 모름", [{"pub_date": None}]),
    ):
        header = breaking_alert_sender._catch_up_header("단독", batch)
        assert "밤사이" not in header, f"{label}인데 '밤사이·새벽'이라고 하면 안 됨: {header}"
        assert "앱을 켜기 전에 게시된" in header, f"{label}: {header}"

    print("12) [단독]·[속보] 알림: 통과 (말머리 기사만 골라 대상 수신자에게 보내고, 게시 시각 표시, 밤사이 문구는 실제 밤일 때만, 중복 발송 방지)")



def test_breaking_alert_catches_up_at_window_start():
    """감시 시간대가 시작될 때 "감시 시간 밖에 나온 기사"를 몰아서 보내는지 확인한다.

    [추가: 2026-09-16] 설정 화면 체크박스는 "감시 시간 밖에 나온 기사는 **다음 감시
    시작 때** 몰아서 받기"라고 약속하는데, 예전 코드는 그 몰아보내기를 **앱이 켜질 때만**
    했다(catch_up_on_wake, main.py 1회). 그래서 컴퓨터를 밤새 켜두면 감시 시작 시각에
    아무 일도 안 일어나, 감시 시간 전(예: 06:00 시작인데 새벽 3시)에 나온 [단독]은
    다음 정기 회차까지 몇 시간을 기다렸다.

    이 테스트가 보는 것 넷:
      1) 그날 첫 폴링은 하한(after)을 감시 시작 시각이 아니라 **자정**으로 내린다.
      2) 그때 문구가 몰아보내기 문구("감시 시간 밖에 나온")로 바뀐다.
      3) 같은 날 두 번째 폴링부터는 평소대로 직전 폴링 시각부터만 본다(중복 조회 방지).
      4) 그래서 몰아보내기는 별도 검색이 아니라 **첫 폴링 한 번**으로 끝난다.
    """
    _require_isolation()
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        with patch.object(breaking_alert, "BREAKING_ALERT_FILE", tmp_dir / "breaking_alert.json"), \
             patch.object(breaking_alert_state, "BREAKING_ALERT_STATE_FILE", tmp_dir / "ba_state.json"), \
             patch.object(settings, "SETTINGS_FILE", tmp_dir / "settings.json"):
            settings.save_keyword_groups([
                {"name": "감시", "keywords": ["기재부"], "mode": "OR",
                 "include_in_live": True, "include_in_scrap": True},
            ])
            breaking_alert.save_breaking_alert_settings(
                enabled=True, group_names=["감시"], start="06:00", end="23:00",
                interval_min=3, catch_up_enabled=True,
            )
            # 앱이 밤새 켜져 있던 상태 — 어제 마지막 폴링 기록만 있고, 오늘 폴링은 아직.
            yesterday = (datetime.now() - timedelta(days=1)).replace(hour=22, minute=58)
            breaking_alert_state.save_state({"last_poll_at": yesterday.isoformat(timespec="seconds")})

            calls = []

            def fake_search(keywords, after=None, **kwargs):
                calls.append(after)
                return {}, [], []

            headers = []

            def fake_detect(articles, header_fn=None, now=None):
                headers.append(header_fn)
                return 0

            with patch.object(breaking_alert_sender, "search_keywords", side_effect=fake_search), \
                 patch.object(breaking_alert_sender, "detect_and_alert", side_effect=fake_detect):
                today_six = datetime.now().replace(hour=6, minute=0, second=1, microsecond=0)
                breaking_alert_sender.poll_and_alert_tick(now=today_six)

                assert len(calls) == 1, f"감시 시작 시각에 폴링이 한 번 돌아야 하는데 {len(calls)}회"
                assert calls[0] == naver_api.kst_today_at("00:00"), (
                    f"그날 첫 폴링은 자정까지 거슬러 봐야 하는데 {calls[0]}부터 봤다 — "
                    "감시 시작 전에 나온 기사를 못 줍는다"
                )
                assert headers[0] is breaking_alert_sender._window_catch_up_header, (
                    "몰아보내기 문구로 보내야 하는데 평소 문구를 썼다"
                )
                # [수정: 2026-09-16] header_fn 인자는 건수가 아니라 기사 목록이다.
                assert "감시 시간 밖에 게시된" in headers[0]("단독", [{}, {}]), "설정 화면 체크박스 문구와 같은 뜻이어야 함"

                # 같은 날 두 번째 폴링 — 이제는 직전 폴링 시각부터만 본다.
                breaking_alert_sender.poll_and_alert_tick(
                    now=today_six.replace(minute=4)
                )
                assert len(calls) == 2, "주기가 지났으면 두 번째 폴링이 돌아야 함"
                assert calls[1] == naver_api.kst_today_at("06:00"), (
                    f"두 번째 폴링은 직전 폴링(06:00)부터만 봐야 하는데 {calls[1]}"
                )
                assert headers[1] is None, "두 번째부터는 평소 문구여야 함"
    print("35) [단독]·[속보] 감시 시작 몰아보내기: 통과 (그날 첫 폴링이 자정까지 거슬러 보고, 이후는 평소대로)")


def test_late_pickup_recovers_previous_slot_articles():
    """회차 마감 순간 네이버가 아직 안 준 기사를 다음 회차가 주워 담는지 확인한다.

    [추가: 2026-09-03] 회차 창은 (시작, 마감]이고 다음 회차의 하한이 정확히 이 회차의
    마감이라, 마감 시각에 검색 결과에 없던 기사는 **어느 회차에도 못 들어가고 영영
    사라졌다**(실측: 2026-09-03 11:00 회차에서 6건). 이 테스트가 보는 것 넷:
      1) 수집 하한이 COLLECT_LOOKBACK_MIN만큼 앞으로 물려 검색된다
      2) 앞 회차에 **이미 실린** URL은 다시 안 담긴다(중복 게재 0)
      3) 그때 못 담겼던 기사만 late_pickup 표식을 달고 들어온다
      4) 이 회차 창 안의 기사에는 그 표식이 안 붙는다
    """
    _require_isolation()
    windows = [
        {"start": "09:30", "end": "11:00", "enabled": True},
        {"start": "11:00", "end": "14:00", "enabled": True},
    ]
    # [수정: 2026-09-04] 날짜를 하드코딩하지 않는다 — collect_run의 회차 창은
    # app.naver_api.kst_today_at가 만드는 **오늘** 시각이라, 어제 날짜를 박아두면
    # 자정이 지나는 순간 모든 기사가 "창보다 앞선 기사"가 되어 이 테스트가 통째로
    # 뒤집힌다(실제로 작성 다음 날 그렇게 깨졌다).
    today = datetime.now().date().isoformat()
    already = {**_article("이미-실림"), "pub_date": f"{today}T10:30:00+09:00"}
    missed = {**_article("놓쳤던-기사"), "pub_date": f"{today}T10:50:00+09:00"}
    normal = {**_article("이번-회차"), "pub_date": f"{today}T12:00:00+09:00"}

    # 앞 회차(11:00)가 이미 저장돼 있고 거기에 already가 실려 있다.
    storage.save_run("11:00", [already], run_at=f"{today}T11:00:08")

    captured = {}

    def fake_search(groups, after=None, before=None, track_keyword_matches=False):
        captured["after"] = after
        captured["before"] = before
        # 넓힌 창이라 앞 회차 기사까지 전부 다시 딸려온 상황을 흉내낸다.
        return [dict(already), dict(missed), dict(normal)], []

    with patch.object(scraper, "search_articles_by_groups", side_effect=fake_search), \
         patch.object(scraper, "active_schedule_times", return_value=windows):
        run = scraper.collect_run("14:00", window_start="11:00", run_at=f"{today}T14:00:05")

    # 1) 하한이 60분 물려졌나 (11:00 -> 10:00)
    assert captured["after"].strftime("%H:%M") == "10:00", (
        f"수집 하한이 11:00에서 60분 물려 10:00이어야 하는데 {captured['after']}"
    )
    assert captured["before"].strftime("%H:%M") == "14:00", "상한은 회차 마감 그대로여야 함"

    urls = {a["url"] for a in run["articles"]}
    # 2) 앞 회차에 이미 실린 기사는 안 들어온다
    assert already["url"] not in urls, "앞 회차 보고서에 이미 실린 기사가 또 담겼음(중복 게재)"
    # 3) 못 담겼던 기사만 표식을 달고 들어온다
    assert missed["url"] in urls, "앞 회차에서 놓친 기사가 회수되지 않았음"
    picked = next(a for a in run["articles"] if a["url"] == missed["url"])
    assert picked.get("late_pickup") is True, "회수된 기사에 late_pickup 표식이 없음"
    assert picked.get("late_pickup_slot") == "11:00", (
        f"회수된 기사의 소속 회차가 11:00이어야 하는데 {picked.get('late_pickup_slot')}"
    )
    # 4) 이 회차 창 안의 기사엔 표식이 안 붙는다
    same_window = next(a for a in run["articles"] if a["url"] == normal["url"])
    assert not same_window.get("late_pickup"), "이번 회차 창 안 기사에 회수 표식이 잘못 붙었음"

    # 화면: 배너와 칩이 뜨고, 복사/내보내기 텍스트에는 안 새어 나간다.
    storage.confirm_run(run)
    html_text = renderer.generate_screen().read_text(encoding="utf-8")
    assert "late-banner" in html_text, "회수 안내 배너가 안 그려짐"
    assert "11시 회차 기사" in html_text, "배너가 회수된 기사의 소속 회차를 안 알려줌"
    assert "⏱ 늦게 들어온 기사" in html_text, "회수된 기사에 '늦게 들어온 기사' 칩이 안 붙음"
    plain_start = html_text.index("const PLAIN_TEXT = ") + len("const PLAIN_TEXT = ")
    plain_text = json.loads(html_text[plain_start:html_text.index(";\n", plain_start)])
    assert "늦게 들어온 기사" not in plain_text, "화면 전용 표시가 복사/내보내기 텍스트로 샜음"

    print("14) 앞 회차 기사 회수: 통과 (하한 60분 물림, 이미 실린 건 제외, 회수분만 표식·배너)")

def test_catch_up_runs_all_missed_slots_today():
    """앱이 꺼져 있던 사이 놓친 회차를 **전부** 채우는지 확인한다 (PRD 기능1 규칙9).

    [수정: 2026-09-04] 예전 규칙은 "가장 최근에 놓친 1개만"이었다. 실측
    (2026-08-19~09-02)에서 예정 78회차 중 14회가 비었고, 여러 회차를 한꺼번에 놓친 날
    (8/28 4회·8/31 2회)은 앱을 켜도 마지막 하나만 채워졌다. 이 테스트가 보는 것 셋:
      1) 안 돌아간 회차를 전부 돌린다
      2) **오래된 것부터** 돌린다 (app.scraper.collect_run의 `_already_published_urls`가
         앞 회차 기사를 걸러내는 방어가 거꾸로 작동하면 안 되므로 순서가 곧 규칙이다)
      3) 한 회차가 실패해도 나머지는 계속 간다
    """
    _require_isolation()
    windows = [
        {"start": "00:00", "end": "06:00"},
        {"start": "06:00", "end": "09:30"},
        {"start": "09:30", "end": "11:00"},
        {"start": "11:00", "end": "14:00"},
        {"start": "14:00", "end": "17:00"},
        {"start": "17:00", "end": "19:30"},
    ]
    now = datetime.now().replace(hour=17, minute=10, second=0, microsecond=0)
    saved = {"06:00"}  # 06:00 회차만 이미 저장돼 있는 상태

    calls = []

    def fake_scrape(run_slot, window_start):
        calls.append((run_slot, window_start))
        return {"run_slot": run_slot, "articles": []}

    last = scheduler.run_due_slot(
        now=now, schedule_times=windows, scrape=fake_scrape,
        already_run=lambda slot: slot in saved,
    )

    ran = [slot for slot, _ in calls]
    assert ran == ["09:30", "11:00", "14:00", "17:00"], (
        f"놓친 회차를 오래된 것부터 전부 돌려야 하는데 실제로는 {ran}"
    )
    assert calls[0][1] == "06:00", "회차의 시작 시각은 설정된 값 그대로여야 함"
    assert last == "17:00", f"마지막으로 성공한 회차명을 돌려줘야 하는데 {last}"
    # 아직 오지 않은 회차(19:30)는 건드리지 않는다.
    assert "19:30" not in ran, "아직 안 온 회차를 미리 돌렸음"

    # 3) 한 회차가 실패해도 나머지는 계속 간다.
    calls2 = []

    def flaky_scrape(run_slot, window_start):
        calls2.append(run_slot)
        if run_slot == "11:00":
            raise RuntimeError("일시적 네트워크 오류")
        return {"run_slot": run_slot, "articles": []}

    # 일부러 만든 실패라 오류 로그를 가로챈다 — 터미널에 진짜 오류처럼 찍히지 않게 하고,
    # 「실패한 회차는 로그를 남긴다」도 같이 확인한다.
    with patch.object(scheduler.logger, "exception") as mock_exc:
        last2 = scheduler.run_due_slot(
            now=now, schedule_times=windows, scrape=flaky_scrape,
            already_run=lambda slot: slot in saved,
        )
    assert mock_exc.call_count == 1 and mock_exc.call_args.args[1] == "11:00", (
        "실패한 회차 하나만 오류 로그를 남겨야 함"
    )
    assert calls2 == ["09:30", "11:00", "14:00", "17:00"], (
        f"한 회차 실패가 뒤 회차를 막으면 안 되는데 실제로는 {calls2}"
    )
    assert last2 == "17:00", "실패한 회차 뒤에도 성공한 회차가 있으면 그 이름을 돌려줘야 함"

    # 4) 계속 실패하는 회차는 하루에 _MAX_SLOT_ATTEMPTS번까지만 붙든다.
    #    이 상한이 없으면 tick(10초)마다 collect_run_with_retry(3회 × 5분)를 하루 종일
    #    다시 돈다 — "가장 최근 1개만" 시절엔 다음 회차가 오면 후보에서 빠져 저절로
    #    멈췄던 것이, 보충을 넓히면서 사라진 제동이다.
    scheduler._slot_failures.clear()
    tries = []

    def always_fails(run_slot, window_start):
        tries.append(run_slot)
        raise RuntimeError("계속 실패")

    with patch.object(scheduler.logger, "exception") as mock_exc:
        for _ in range(6):  # tick을 6번 돌려본다
            scheduler.run_due_slot(
                now=now, schedule_times=windows, scrape=always_fails,
                already_run=lambda slot: slot in saved,
            )
    gave_up = [c for c in mock_exc.call_args_list if "더 시도하지 않습니다" in c.args[0]]
    assert len(gave_up) == 4, f"네 회차 모두 마지막 시도에서 「더 시도하지 않습니다」를 남겨야 하는데 {len(gave_up)}번"
    attempts_for_first = tries.count("09:30")
    assert attempts_for_first == scheduler._MAX_SLOT_ATTEMPTS, (
        f"한 회차를 하루에 {scheduler._MAX_SLOT_ATTEMPTS}번까지만 시도해야 하는데 "
        f"{attempts_for_first}번 시도했음"
    )
    scheduler._slot_failures.clear()

    print("15) 놓친 회차 전부 보충: 통과 (오래된 것부터, 하나 실패해도 나머지 진행, 재시도 상한)")


def test_draft_sees_the_same_window_as_confirmed():
    """초안과 확정본이 **같은 검색 창**을 보는지 확인한다.

    [추가: 2026-09-04] 2026-09-03에 collect_run의 하한만 COLLECT_LOOKBACK_MIN만큼
    앞으로 물리고 초안(_load_preview_search_base)은 회차 시작을 바닥으로 그대로 둬서,
    두 화면이 서로 다른 창을 봤다 — 앞 회차 마감 순간 네이버가 아직 색인하지 않은
    기사가 **확정본에는 회수돼 들어오는데 초안에는 끝까지 안 보였다**(실측 2026-09-04
    09:54: 초안 11건 / 11:00 확정본이 볼 17건, 차이 7건). 담당자 눈에는 그냥 사라진
    기사이고, 초안에서 다 정리해둔 뒤 본 적 없는 기사가 확정본에 튀어나온다.

    바로 위 test_late_pickup_...이 확정본 쪽만 지키고 있어서 이 갈라짐을 못 잡았다.
    이 테스트가 보는 것 다섯:
      1) 초안 하한도 COLLECT_LOOKBACK_MIN만큼 앞으로 물려진다
      2) **그 값이 collect_run의 하한과 정확히 같다** (이게 진짜 불변식이다 —
         1)만 보면 나중에 한쪽 상수만 바꿔도 통과해 버린다)
      3) 앞 회차에 이미 실린 URL은 초안에도 안 뜬다(중복 게재 0)
      4) 회수된 기사에만 late_pickup 표식이 붙는다
      5) 그 표식을 그리는 칩 CSS가 초안 화면에도 실제로 실린다
    """
    _require_isolation()
    windows = [
        {"start": "06:00", "end": "09:30", "enabled": True},
        {"start": "09:30", "end": "11:00", "enabled": True},
    ]
    slot = {"start": "09:30", "end": "11:00", "enabled": True}
    # 날짜를 하드코딩하지 않는다 — 회차 창은 kst_today_at가 만드는 **오늘** 시각이다
    # (test_late_pickup_...이 그렇게 한 번 깨진 적 있다).
    today = datetime.now().date().isoformat()
    already = {**_article("이미-실림"), "pub_date": f"{today}T09:00:00+09:00"}
    missed = {**_article("놓쳤던-기사"), "pub_date": f"{today}T09:27:00+09:00"}
    normal = {**_article("이번-회차"), "pub_date": f"{today}T09:40:00+09:00"}

    # 앞 회차(09:30)가 이미 저장돼 있고 거기에 already가 실려 있다. missed는 그때
    # 네이버가 아직 안 줘서 못 담겼다(09:27 발행인데 09:30:07 수집에서 누락).
    storage.save_run("09:30", [already], run_at=f"{today}T09:30:07")

    st = settings.load_settings()
    draft = {}

    def fake_draft_search(groups, after=None, before=None, track_keyword_matches=False):
        draft["after"] = after
        # 넓힌 창이라 앞 회차 구간이 통째로 다시 딸려온 상황을 흉내낸다.
        return [dict(already), dict(missed), dict(normal)], []

    with patch.object(preview_renderer, "search_articles_by_groups", side_effect=fake_draft_search), \
         patch.object(preview_renderer, "active_schedule_times", return_value=windows):
        articles = preview_renderer._compute_preview_articles(st, slot)

    # 1) 초안 하한이 09:30에서 60분 물려 08:30인가
    assert draft["after"].strftime("%H:%M") == "08:30", (
        f"초안 검색 하한이 09:30에서 60분 물려 08:30이어야 하는데 {draft['after']}"
    )

    # 2) **핵심** — 같은 회차에 대해 collect_run이 쓰는 하한과 글자 하나까지 같은가.
    confirmed = {}

    def fake_confirmed_search(groups, after=None, before=None, track_keyword_matches=False):
        confirmed["after"] = after
        return [], []

    with patch.object(scraper, "search_articles_by_groups", side_effect=fake_confirmed_search), \
         patch.object(scraper, "active_schedule_times", return_value=windows):
        scraper.collect_run("11:00", window_start="09:30", run_at=f"{today}T11:00:05")
    assert draft["after"] == confirmed["after"], (
        "초안과 확정본이 서로 다른 창을 봅니다 — 초안에는 안 보이는데 확정본에만 "
        f"들어오는 기사가 생깁니다 (초안 {draft['after']} / 확정본 {confirmed['after']})"
    )

    urls = {a["url"] for a in articles}
    # 3) 앞 회차 보고서에 이미 나간 기사는 초안에 다시 안 뜬다
    assert already["url"] not in urls, "앞 회차에 이미 실린 기사가 초안에 또 떴음(중복 게재)"
    # 4) 그때 못 담겼던 기사만 표식을 달고 들어온다
    assert missed["url"] in urls, "앞 회차에서 놓친 기사가 초안에 안 보임 — 이 버그 그대로"
    picked = next(a for a in articles if a["url"] == missed["url"])
    assert picked.get("late_pickup") is True, "회수된 기사에 late_pickup 표식이 없음"
    assert picked.get("late_pickup_slot") == "09:30", (
        f"회수된 기사의 소속 회차가 09:30이어야 하는데 {picked.get('late_pickup_slot')}"
    )
    same_window = next(a for a in articles if a["url"] == normal["url"])
    assert not same_window.get("late_pickup"), "이번 회차 창 안 기사에 회수 표식이 잘못 붙었음"

    # 5) 칩 CSS가 초안 화면에도 실린다 — 확정본과 **같은 함수**에서 나온 값이어야 한다
    #    (.photo-badge처럼 양쪽에 복붙해 두면 한쪽만 고쳤을 때 조용히 어긋난다).
    assert "{late_badge_style}" in preview_renderer._PAGE_TEMPLATE, (
        "초안 템플릿에 late_badge_style 자리가 없음 — 칩이 스타일 없이 그려짐"
    )
    assert preview_renderer._theme()["late_badge_style"] == renderer.late_badge_style(), (
        "초안이 쓰는 칩 CSS가 확정본과 다름 — 두 화면이 갈라질 수 있음"
    )
    assert ".late-badge" in preview_renderer._theme()["late_badge_style"]

    print("16) 초안·확정본 같은 창: 통과 (하한 일치, 이미 실린 건 제외, 회수분 표식·칩 CSS 공유)")


def test_regular_run_delete_and_restore():
    """정기 보관함 회차 삭제 — trash로 옮기고, 그 자리에 「삭제함」이 남고, 되살리면 돌아온다.

    [추가: 2026-09-11] 지키는 것 셋:
      1) 오늘 회차는 못 지운다 — 파일이 없어지면 스케줄러가 run_exists로 곧바로 다시 수집한다.
      2) 가장 최근 회차는 못 지운다 — 지우면 그 앞 회차가 자동발송 대상이 될 수 있다(새벽).
      3) 지운 자리는 「수집 기록 없음」이 아니라 「삭제함 · 되살리기」다(앱이 못 모은 것처럼 보이면 거짓말).
    """
    _require_isolation()
    now = datetime.now()
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    two_days_ago = (now - timedelta(days=2)).strftime("%Y-%m-%d")
    today = now.strftime("%Y-%m-%d")

    storage.save_run("09:00", [_article("a")], run_at=f"{two_days_ago}T09:01:00")
    storage.save_run("17:00", [], run_at=f"{two_days_ago}T17:01:00")
    storage.save_run("19:30", [_article("b")], run_at=f"{yesterday}T19:31:00")

    # ② 오늘 회차가 없는 새벽 — 어제 19:30이 "가장 최근"이라 잠긴다.
    try:
        storage.delete_run(yesterday, "19:30")
        raise AssertionError("가장 최근 회차가 지워졌음 — 그 앞 회차가 자동발송될 수 있다")
    except storage.RunLockedError as e:
        assert e.reason == "latest", e.reason

    # 지울 수 있는 회차 두 개를 한 묶음(같은 stamp)으로 지운다.
    assert storage.delete_run(two_days_ago, "17:00", stamp="20260101120000")
    assert storage.delete_run(two_days_ago, "09:00", stamp="20260101120000")
    assert not storage.run_exists("17:00", two_days_ago), "지운 회차 파일이 그대로 남아 있음"
    assert not storage.delete_run(two_days_ago, "17:00"), "이미 지운 회차를 또 지웠다고 함"
    deleted = storage.list_deleted_runs()
    assert {(e["date"], e["run_slot"]) for e in deleted} == {(two_days_ago, "17:00"), (two_days_ago, "09:00")}
    assert all(e["run_slot"] != "19:30" for e in deleted)
    # 조회 쪽(인덱스)에서도 빠져야 한다 — 보관함·엑셀·홈 추이가 전부 이걸 본다.
    assert all(m["run_at"][:10] != two_days_ago for m in storage.list_run_meta())

    # ③ 보관함 화면: 살아 있는 회차가 없어진 날짜도 줄이 남고, 지운 자리는 「삭제함 · 되살리기」.
    page = history_renderer.render_history_page(
        [], all_dates=storage.list_run_dates(), deleted_runs=deleted, latest_key=storage.latest_run_key()
    )
    assert f'data-slot-key="{two_days_ago}|17:00"' in page and "slot-gone" in page, "지운 자리가 안 그려짐"
    assert 'class="chip chip-gone"' in page, "지운 회차만 남은 날짜 표시가 없음"

    # ① 오늘 회차가 생기면 오늘은 "today"로 잠기고, 어제 19:30은 이제 지울 수 있다.
    storage.save_run("09:30", [_article("c")], run_at=f"{today}T09:31:00")
    try:
        storage.delete_run(today, "09:30")
        raise AssertionError("오늘 회차가 지워졌음 — 스케줄러가 곧바로 다시 수집한다")
    except storage.RunLockedError as e:
        assert e.reason == "today", e.reason
    assert storage.run_lock_reason(yesterday, "19:30") is None

    # [추가: 2026-09-15] 날짜 줄 🗑 — 지울 수 있는 회차가 있는 날만, 그 회차 키를 구워 넣는다.
    # 오늘은 없다. 지운 회차 둘만 남은 날짜는 「삭제함 · 모두 되살리기」로 그린다.
    page = history_renderer.render_history_page(
        [], all_dates=storage.list_run_dates(), deleted_runs=storage.list_deleted_runs(),
        latest_key=storage.latest_run_key(),
    )

    def _date_summary(d):
        return page.split(f'data-date="{d}"', 1)[1].split("</summary>", 1)[0]

    assert 'class="day-del"' in _date_summary(yesterday) and f"{yesterday}|19:30" in _date_summary(yesterday), (
        "날짜 줄 🗑가 없거나 지울 회차 키가 안 실림"
    )
    assert 'class="day-del"' not in _date_summary(today), "오늘 날짜 줄에 🗑가 그려짐"
    gone_part = page.split(f'data-date="{two_days_ago}"', 1)[0].rsplit("<details", 1)[1]
    assert "all-gone" in gone_part, "통째로 지운 날짜가 all-gone이 아님"
    assert "모두 되살리기" in _date_summary(two_days_ago), "지운 회차 둘인 날짜에 「모두 되살리기」가 없음"

    # 되살리기 — 원래 자리로 돌아오고, 같은 회차가 이미 있으면 덮어쓰지 않는다.
    trash_17 = next(e["trash_name"] for e in deleted if e["run_slot"] == "17:00")
    assert storage.restore_run(trash_17) == {"date": two_days_ago, "run_slot": "17:00"}
    assert storage.run_exists("17:00", two_days_ago)
    assert storage.restore_run(trash_17) is None, "이미 되살린 걸 또 되살렸다고 함"
    assert storage.restore_run("../../etc/passwd") is None, "trash 밖 경로를 받아들임"
    storage.delete_run(two_days_ago, "17:00", stamp="20260101130000")
    storage.save_run("17:00", [], run_at=f"{two_days_ago}T17:05:00")  # 같은 자리에 다른 회차가 생긴 경우
    newest_17 = next(e["trash_name"] for e in storage.list_deleted_runs() if e["run_slot"] == "17:00")
    assert storage.restore_run(newest_17) is None, "같은 날짜·회차가 있는데 덮어써 되살렸음"

    print("17) 정기 회차 삭제·되살리기: 통과 (오늘·가장 최근 잠금, 지운 자리 「삭제함」, 되살리기·충돌 방어)")


def test_adhoc_card_delete_and_restore():
    """수시 보관함 카드 삭제 — 보관 기한으로 옮긴 것은 「최근 삭제」에 안 뜨고, 제자리 줄·
    이름 없는 사안 묶음·되살리기가 동작하는지.

    [추가: 2026-09-11] 수시 카드 폴더는 하네스가 격리하지 않으므로 이 테스트가 직접 임시
    폴더로 바꿔 끼운다(실제 data/adhoc/를 절대 건드리지 않는다).
    """
    _require_isolation()
    from app.adhoc import archive_renderer, card

    with tempfile.TemporaryDirectory() as tmp:
        adhoc_dir = Path(tmp)
        (adhoc_dir / "cards").mkdir()
        (adhoc_dir / "trash").mkdir()
        with patch.object(card, "CARDS_DIR", adhoc_dir / "cards"), \
             patch.object(card, "TRASH_DIR", adhoc_dir / "trash"), \
             patch.object(card, "ISSUES_FILE", adhoc_dir / "issues.json"):
            issue = card.get_or_create_issue("재경위")
            base = datetime.now() - timedelta(days=3)
            # [수정: 2026-09-15] 보관함에 남는 건 확정본과 옛 원본(raw 없음)뿐이다 — 로데이터
            # 원본은 새 수집의 「지난 수집」에서 찾는다. 여기선 옛 원본으로 보관함 동작을 본다.
            kept = card.new_card(issue["id"], "재경위", "09:00", "10:00", ["재경위"], now=base, raw=False)
            gone_a = card.new_card(issue["id"], "재경위", "11:00", "12:00", ["재경위"], now=base + timedelta(minutes=1), raw=False)
            orphan = card.new_card("issue-없음", "시험", "09:00", "10:00", ["시험"], now=base + timedelta(minutes=2), raw=False)
            expired = card.new_card(issue["id"], "재경위", "13:00", "14:00", ["재경위"], now=base + timedelta(minutes=3), raw=False)

            # 사안 정보가 없는 카드는 「이름 없는 사안」 묶음으로 보여야 지울 수 있다.
            page = archive_renderer.render_archive_page(preset="all")
            assert "이름 없는 사안" in page and f'data-card-id="{orphan["id"]}"' in page, "사안 없는 카드가 안 보임"
            assert "선택 삭제" in page and "row-del" in page

            assert card.delete_card(gone_a["id"], stamp="20260101120000")
            assert card.delete_card(orphan["id"], stamp="20260101120000")
            assert card.delete_card(expired["id"], reason="expired")
            assert not card.delete_card("../issues"), "카드 폴더 밖 파일(사안 목록)을 trash로 옮김"
            assert (adhoc_dir / "issues.json").exists()
            listed = card.list_deleted_cards()
            assert {c["id"] for c in listed} == {gone_a["id"], orphan["id"]}, "보관 기한으로 옮긴 카드가 「최근 삭제」에 섞였음"

            # 방금 지운 카드는 제자리에 「삭제함 · 되살리기」 줄로, 나머지 지운 카드는 맨 아래 「최근 삭제」로.
            page = archive_renderer.render_archive_page(preset="all", deleted_ids=[gone_a["id"]])
            assert f'class="run-line gone" data-card-id="{gone_a["id"]}"' in page, "지운 자리에 「삭제함」 줄이 없음"
            assert "최근 삭제 · 회차 1개" in page, "제자리 줄과 「최근 삭제」에 같은 카드가 두 번 뜸"
            assert f'data-card-id="{kept["id"]}"' in page

            # 날짜 → 회차: 사안 층 없이 날짜 줄 🗑, 회차 줄에 사안명. 사안명 검색은 걸린 회차만.
            assert 'class="row-del day-del"' in page and 'data-unit="사안"' not in page, "날짜 줄 🗑가 없거나 사안 층이 남음"
            day_label = archive_renderer._format_date_kr_full(base.strftime("%Y-%m-%d"))
            date_part = page.split(f'data-day-label="{day_label}"', 1)[1].split("</details>", 1)[0]
            assert '<span class="iss">재경위</span>' in date_part and "11:00~12:00" not in date_part, "회차 줄에 사안명·기준 시각이 없음"
            other = card.get_or_create_issue("세제개편")
            card.new_card(other["id"], "세제개편", "09:00", "10:00", ["세제"], now=base + timedelta(minutes=5), raw=False)
            page = archive_renderer.render_archive_page(preset="all", q="재경")
            assert "세제개편" not in page and 'class="qmark">재경</span>' in page, "사안명 검색이 회차 줄을 못 거름"

            # 「보기」 스위치 — 같은 결과를 사안 → 회차로 묶는다. 사안 줄 정렬은 가나다순/최근순.
            card.new_card(issue["id"], "재경위", "09:00", "10:00", ["재경위"],
                          now=base + timedelta(days=1), raw=False)  # 재경위가 가장 최근 사안이 되게
            def _group_names(**kw):
                page = archive_renderer.render_archive_page(preset="all", **kw)
                return page, [b.split('"', 1)[0] for b in page.split('data-day-label="')[1:]]

            page, names = _group_names(view="issue")
            assert 'data-unit="사안"' in page and names == ["세제개편", "재경위"], f"가나다순이 아님: {names}"
            assert 'class="win wd">' in page and '<span class="iss">' not in page, "사안별 보기 회차 줄에 날짜가 없음"
            _, names = _group_names(view="issue", sort="recent")
            assert names == ["재경위", "세제개편"], f"최근순이 아님: {names}"
            _, names = _group_names()  # 기본은 언제나 날짜별(기억하지 않는다)
            assert all(n.endswith(")") for n in names), f"기본 보기가 날짜별이 아님: {names}"

            # 날짜가 통째로 「삭제함」이 되면 날짜 줄에 「모두 되살리기」(🗑는 없음)
            assert card.delete_card(kept["id"], stamp="20260101130000")
            page = archive_renderer.render_archive_page(preset="all", q="재경", deleted_ids=[gone_a["id"], kept["id"]])
            date_head = page.split(f'data-day-label="{day_label}"', 1)[1].split("</summary>", 1)[0]
            assert "모두 되살리기" in date_head and "day-del" not in date_head, "통째로 지운 날짜 줄이 이상함"
            assert card.restore_card(next(c["_trash_name"] for c in card.list_deleted_cards() if c["id"] == kept["id"]))

            # 되살리기 — 같은 id가 이미 있으면 덮어쓰지 않고, 형식 밖 이름은 받지 않는다.
            restored = card.restore_card(listed[0]["_trash_name"])
            assert restored in {gone_a["id"], orphan["id"]} and card.load_card(restored) is not None
            assert card.restore_card(listed[0]["_trash_name"]) is None
            assert card.restore_card("../issues.json") is None

    print("18) 수시 카드 삭제·되살리기: 통과 (이름 없는 사안 표시, 기한 만료 제외, 제자리 줄·최근 삭제·되살리기)")


def test_adhoc_reload_to_now():
    """수집 원본 헤더의 「지금까지 불러오기」(ADHOC_RELOAD_NOW_MOCKUP.html C안)를 확인한다.

    [추가: 2026-09-15]
      1) 서버가 시작은 그대로, 끝만 누른 순간의 시각으로 바꿔 다시 불러온다(to_now)
      2) 헤더에 버튼이 있고, 어제 만든(잠긴) 카드엔 없다
      3) 📥 칩: 수집 시각 = 끝 시각이면 (~HH:MM)을 빼고, 4회부터 첫·마지막만 + 툴팁
      4) 마지막 불러오기로 새로 들어온 기사만 노랑 — 첫 수집·검색어 편집으로 들어온 것은 아님
    """
    _require_isolation()
    from app.adhoc import card, renderer as adhoc_renderer, routes as adhoc_routes

    with tempfile.TemporaryDirectory() as tmp:
        adhoc_dir = Path(tmp)
        (adhoc_dir / "cards").mkdir()
        (adhoc_dir / "trash").mkdir()
        with patch.object(card, "CARDS_DIR", adhoc_dir / "cards"), \
             patch.object(card, "TRASH_DIR", adhoc_dir / "trash"), \
             patch.object(card, "ISSUES_FILE", adhoc_dir / "issues.json"):
            now = datetime.now().replace(second=0, microsecond=0)
            today = now.strftime("%Y-%m-%d")
            issue = card.get_or_create_issue("인사청문회")
            c = card.new_card(issue["id"], "인사청문회", "00:00", "00:01", ["청문회"], now=now)

            # 1) to_now — 폼에 무슨 시각이 실려 와도 무시하고 서버 시각을 쓴다.
            calls = []

            class _Handler:
                def _redirect(self, target):
                    calls.append(target)

            with patch.object(adhoc_routes, "run_collect", lambda cid: calls.append(("collect", cid))):
                adhoc_routes._handle_recollect(
                    _Handler(), {"id": [c["id"]], "to_now": ["1"], "window_end": ["00:02"]}
                )
            saved = card.load_card(c["id"])
            assert saved["window"]["start"] == "00:00", "「지금까지」가 시작 시각을 바꿈"
            # 테스트가 분 경계를 넘을 수 있어 앞뒤 1분까지 허용한다.
            allowed = {(now + timedelta(minutes=d)).strftime("%H:%M") for d in (0, 1)}
            assert saved["window"]["end"] in allowed, f"끝 시각이 누른 시각이 아님: {saved['window']['end']}"
            assert ("collect", c["id"]) in calls and "error=" not in calls[-1]

            # 2)~4) 화면 — 수집 이력·기사를 직접 심어 본다.
            def art(url, added_at, by="collect"):
                return {"outlet": "KBS", "title": url, "url": url, "summary": "청문회",
                        "pub_date": f"{today}T00:00:30+09:00", "matched_keywords": ["청문회"],
                        "group": None, "order": None, "hidden": False,
                        "added_at": added_at, "added_by": by}

            t1, t2 = f"{today}T10:06:21", f"{today}T10:15:37"
            saved["window"] = {"start": "00:00", "end": "10:15"}
            saved["collect_log"] = [
                {"at": t1, "window_end": "10:06", "found": 1, "added": 1},
                {"at": t2, "window_end": "10:15", "found": 3, "added": 1},
            ]
            saved["articles"] = [
                art("https://e.x/first", t1),
                art("https://e.x/new", t2),
                art("https://e.x/kwedit", f"{today}T10:10:00"),  # 검색어 편집으로 들어온 기사
            ]
            card.save_card(saved)
            page = adhoc_renderer.render_card_page(card.load_card(c["id"]))
            assert 'id="reload-now-form"' in page and 'name="to_now" value="1"' in page, "헤더 버튼 없음"
            assert "지금까지 다시 수집" in page and "</svg> 다시 불러오기</button>" not in page, "버튼 이름이 옛 이름"
            assert "10:06 1건" not in page, "없앤 📥 수집 이력 칩이 그려짐"
            new_rows = page.count('class="article is-new-arrival')
            assert new_rows == 1, f"노랑이 마지막 불러오기의 새 기사만이 아님: {new_rows}"

            # 첫 수집만 있으면 아무것도 안 칠한다.
            assert card.last_collect_new_urls({**saved, "collect_log": saved["collect_log"][:1]}) == set()
            assert card.last_collect_new_urls(saved) == {"https://e.x/new"}

            # 2) 어제 만든 카드엔 버튼이 없다.
            saved["collect_date"] = "2000-01-01"
            card.save_card(saved)
            page = adhoc_renderer.render_card_page(card.load_card(c["id"]))
            assert 'id="reload-now-form"' not in page, "잠긴 카드에 「지금까지 다시 수집」이 그려짐"

    print("22) 수시 「지금까지 다시 수집」: 통과 (끝만 지금으로, 잠긴 카드 제외, 📥 칩 없음, 새 기사 노랑)")



def test_draft_keeps_what_it_showed():
    """초안에 한 번 보인 기사가 그 회차에서 안 빠지는지(app.draft_seen) 확인한다.

    [추가: 2026-09-11] 제보: 초안에 늦게 들어온 기사가 떴는데, 손을 댄 뒤 다시 그려지자
    사라져 이번 회차에 넣을 수 없게 됐다. 약속은 "담당자가 숨기거나 조건을 바꾸지 않는
    한 초안에 한 번 보인 기사는 초안에서 안 빠지고 확정본에도 들어간다"이고, 이 테스트가
    보는 것:
      1) 다시 검색했을 때 그 기사가 안 잡혀도 초안에 남는다(규칙 ③)
      2) 같은 제목 기사가 새로 들어와도 먼저 보인 쪽이 대표를 지킨다(규칙 ①)
      3) 선택 언론사를 바꾸거나 숨기면 빠지고, 빠진 이유가 로그에 남는다(규칙 ② + 로그)
      4) 되돌리면 다시 돌아오고, 둘 다 보인 적 있으면 **먼저** 보인 쪽이 대표다
      5) 확정본(collect_run)도 마감 검색에 안 잡힌 그 기사를 담고, 대표도 초안과 같다
      6) 검색어가 바뀌면 붙잡기 목록은 새로 시작한다
    """
    _require_isolation()
    import logging as _logging
    from app import draft_seen

    windows = [
        {"start": "06:00", "end": "09:30", "enabled": True},
        {"start": "09:30", "end": "11:00", "enabled": True},
    ]
    slot = {"start": "09:30", "end": "11:00", "enabled": True}
    today = datetime.now().date().isoformat()
    late = {**_article("늦게-들어온"), "title": "같은 제목", "pub_date": f"{today}T09:20:00+09:00"}
    normal = {**_article("이번-회차"), "pub_date": f"{today}T09:40:00+09:00"}
    # late와 제목이 같고 언론사 순위가 더 높은(KBS) 기사 — 예전 규칙이면 대표를 뺏는다.
    twin = {"outlet": "KBS", "title": "같은 제목", "url": "https://kbs.example/twin",
            "summary": "", "pub_date": f"{today}T09:50:00+09:00"}
    st = settings.load_settings()
    results = {"now": []}

    def fake_search(groups, after=None, before=None, track_keyword_matches=False):
        return [dict(a) for a in results["now"]], []

    records = []

    class _Grab(_logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    grab = _Grab(level=_logging.INFO)
    preview_renderer.logger.addHandler(grab)
    old_level = preview_renderer.logger.level
    preview_renderer.logger.setLevel(_logging.INFO)

    def draft(st_override=None, fresh=True):
        # fresh=True: 초안 증분 캐시를 지워 "처음부터 다시 검색한 결과"를 흉내낸다 —
        # 캐시가 살아 있으면 예전 검색 결과가 그대로 이어져 붙잡기를 시험할 수 없다.
        if fresh and preview_cache.PREVIEW_CACHE_FILE.exists():
            preview_cache.PREVIEW_CACHE_FILE.unlink()
        with patch.object(preview_renderer, "search_articles_by_groups", side_effect=fake_search), \
             patch.object(preview_renderer, "active_schedule_times", return_value=windows):
            return {a["url"] for a in preview_renderer._compute_preview_articles(st_override or st, slot)}

    try:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(curation, "HIDDEN_ARTICLES_FILE", Path(tmp) / "hidden.json"):
                results["now"] = [late, normal]
                shown = draft()
                assert {late["url"], normal["url"]} <= shown, f"첫 초안에 두 기사가 다 떠야 하는데 {shown}"

                # 1)·2) 다시 검색했더니 late는 안 잡히고 같은 제목의 KBS 기사가 새로 들어왔다.
                results["now"] = [normal, twin]
                shown = draft()
                assert late["url"] in shown, "초안에 보였던 기사가 다시 검색했을 때 안 잡히자 사라졌음"
                assert twin["url"] not in shown, "같은 제목 기사가 새로 들어와 먼저 보인 기사의 대표 자리를 뺏었음"

                # 3) 선택 언론사를 KBS로 좁히면 late는 빠지고(설정이 붙잡기보다 앞선다) 이유가 남는다.
                records.clear()
                shown = draft({**st, "outlet_order": ["KBS"]})
                assert late["url"] not in shown and twin["url"] in shown, f"선택 언론사 변경이 안 먹음: {shown}"
                assert any("선택 언론사 밖" in m and "같은 제목" in m for m in records), f"빠진 이유 로그가 없음: {records}"

                # 4) 되돌리면 late가 돌아오고, twin도 한 번 보였지만 late가 **먼저** 보였으니 late가 대표다.
                shown = draft()
                assert late["url"] in shown and twin["url"] not in shown, f"되돌린 뒤 대표가 먼저 보인 기사여야 함: {shown}"

                # 3') 숨기면 빠지고(담당자 동작이 붙잡기보다 앞선다) 로그에 "숨김"이 남는다.
                records.clear()
                curation.hide_article(late["url"], outlet=late["outlet"], title=late["title"])
                assert late["url"] not in draft(), "숨긴 기사가 붙잡기 때문에 초안에 남았음"
                assert any("숨김" in m for m in records), f"숨김 이유 로그가 없음: {records}"
                curation.unhide_articles([late["url"]])
                assert late["url"] in draft(), "숨김을 풀었는데 초안에 안 돌아옴"

                # 5) 마감 검색에도 late가 안 잡혔다 — 그래도 확정본에 들어가고 대표도 초안과 같다.
                with patch.object(scraper, "search_articles_by_groups", side_effect=fake_search), \
                     patch.object(scraper, "active_schedule_times", return_value=windows):
                    run = scraper.collect_run("11:00", window_start="09:30", run_at=f"{today}T11:00:05")
                run_urls = {a["url"] for a in run["articles"]}
                assert late["url"] in run_urls, "초안에 보였던 기사가 확정본에 안 들어감"
                assert twin["url"] not in run_urls, "확정본의 같은 제목 대표가 초안과 다름"
                saved_late = next(a for a in run["articles"] if a["url"] == late["url"])
                assert saved_late.get("late_pickup") is True, "되돌려 넣은 앞 회차 기사에 늦게 들어온 표식이 없음"

        # 6) 검색어가 바뀌면 붙잡기 목록을 새로 시작한다(같은 회차여도).
        round_id = (today, "11:00")
        other = draft_seen.load_draft_seen(round_id, [{"keywords": ["전혀-다른-검색어"], "mode": "OR"}])
        assert other["articles"] == {} and other["reset_reason"] == "condition", "검색어가 바뀌었는데 옛 목록을 그대로 씀"
    finally:
        preview_renderer.logger.removeHandler(grab)
        preview_renderer.logger.setLevel(old_level)

    print("19) 초안에 보인 기사 붙잡기: 통과 (재검색에 안 잡혀도 유지, 먼저 보인 쪽이 대표, 설정·숨김은 우선, 확정본까지 반영, 빠진 이유 로그)")


def test_photo_caption_name_list_title():
    """요약이 잘려 날짜 도장·이메일 신호가 사라진 사진기사를 "~하는 + 이름-이름" 제목
    모양으로 잡는지, 그리고 가운뎃점·따옴표·서술형 제목은 안 잡는지 확인한다
    (app.filters.looks_like_photo_caption — 2026-09-11 뉴시스 대정부질문 사진 제보 실물).
    """
    from app.filters import looks_like_photo_caption

    truncated = "구윤철 경제부총리 겸 재정경제부 장관과 배경훈 부총리 겸 과학기술정보통신부 장관, 정성호 더불어민주당 의원이 11일 서울 여의도 국회에서 열린 제439회 국회(정기회) 제7차 본회의에 참석해 대화하고... "
    for title in ("대화 나누는 구윤철-배경훈-정성호",
                  "대화 나누는 한성숙-구윤철-배경훈-이언주",
                  "국회 예결위 출석한 구윤철-박홍근",
                  "G20 재무장관회의 중앙은행 총재회의 참석하는 구윤철- 신현송"):
        assert looks_like_photo_caption({"title": title, "summary": truncated}), f"이름 나열 캡션 제목을 놓침: {title}"

    body = "정부가 11일 발표한 개편안에 따르면 내년부터 새 제도가 시행된다."
    for title in ("현직 장관 19명 중 70년대생은 배경훈·원민경뿐",      # 가운뎃점은 인정하지 않는다(실측 오탐)
                  "구윤철-배경훈 \"부동산 세제 손본다\"",               # 따옴표 가드
                  "대화 나누는 구윤철-배경훈 회동 결과 발표했다",       # 이름으로 안 끝남
                  "[속보] 대화 나누는 구윤철-배경훈"):                  # [속보]는 항상 예외
        assert not looks_like_photo_caption({"title": title, "summary": body}), f"일반 기사를 사진으로 잘못 잡음: {title}"
    print("20) 이름 나열 캡션 제목: 통과 (요약이 잘려도 '~하는 + 이름-이름' 제목으로 잡고, 가운뎃점·따옴표·[속보]는 제외)")


def test_photo_caption_candidate_title_and_cut_stamp():
    """"…장관 후보"로 끝나는 캡션 제목과 연도에서 잘린 촬영 날짜 도장을 잡는지, 그리고 곡선
    따옴표 제목·연도에서 잘린 일반 기사 문장은 안 잡는지 확인한다
    (app.filters.looks_like_photo_caption — 2026-09-15 뉴시스 인사청문회 사진 제보 실물).
    """
    from app.filters import looks_like_photo_caption

    cut = "이형일 부총리 겸 재정경제부장관 후보가 15일 여의도 국회에서 열린 제439회 국회(정기회) 재정경제기획위원회 국무위원후보자(부총리 겸 재정경제부장관 이형일) 인사청문회에서 의원 질의에 답하고... "
    for title in ("답변하는 이형일 부총리 겸 재정경제부장관 후보",
                  "인사청문회 질의에 답하는 이형일 부총리 겸 재정경제부장관 후보",
                  "눈 감은 이형일 부총리 겸 재정경제부장관 후보"):
        assert looks_like_photo_caption({"title": title, "summary": cut}), f"'…장관 후보' 캡션 제목을 놓침: {title}"
    stamp_cut = "…인사청문회에서 인사말하고 있다. 2026.... "
    assert looks_like_photo_caption({"title": "이형일 후보 청문회", "summary": stamp_cut}), "연도에서 잘린 날짜 도장을 놓침"

    body = "정부가 11일 발표한 개편안에 따르면 내년부터 새 제도가 시행된다."
    assert not looks_like_photo_caption({"title": "李 엄포에 ‘물가’ 16번 언급한 경제부총리 후보자", "summary": body}), \
        "곡선 따옴표가 낀 일반 기사 제목을 사진으로 잘못 잡음"
    assert not looks_like_photo_caption({"title": "최상위 근로소득자 실효세율 38%",
                                         "summary": "…분석한 결과, 이같이 나타났다고 밝혔다. 2024... "}), \
        "연도에서 잘린 일반 기사 문장을 사진으로 잘못 잡음"

    # 받침 ㄴ 서술어(잠긴·만난·든) 캡션 제목 — 2026-09-15 노컷뉴스 제보 실물. 요약에 도장·이메일이 없다.
    plain = "이형일 경제부총리 겸 재정경제부 장관 후보자가 15일 서울 여의도 국회 재정경제기획위원회에서 열린 인사청문회에서 생각에 잠겨있다."
    for title in ("생각에 잠긴 이형일 경제부총리 후보자",
                  "미국 재무장관 만난 구윤철 부총리",
                  "스마트폰 손에 든 허장 재정경제부 2차관"):
        assert looks_like_photo_caption({"title": title, "summary": plain}), f"받침 ㄴ 서술어 캡션 제목을 놓침: {title}"
    for title in ("은행연합회 전무이사에 금융위 출신 우상현 전 BC카드 부사장",  # 사람을 꾸미는 명사
                  "진주시의회 예결특위위원장에 윤성관 의원"):                   # 이름이 ㄴ으로 끝남
        assert not looks_like_photo_caption({"title": title, "summary": body}), f"인사 기사를 사진으로 잘못 잡음: {title}"

    # 직책 없이 '이름 + 후보자'로 끝나는 캡션 제목 — 2026-09-15 노컷뉴스 제보 실물.
    talk = "이형일 경제부총리 겸 재정경제부 장관 후보자가 15일 서울 여의도 국회 재정경제기획위원회에서 열린 인사청문회에서 관계자와 대화를 하고 있다."
    assert looks_like_photo_caption({"title": "관계자와 대화하는 이형일 후보자", "summary": talk}), \
        "직책 없는 '이름 + 후보자' 캡션 제목을 놓침"
    assert not looks_like_photo_caption({"title": "빚투 말라는 정부, 10억아파트 5.3억 영끌한 국토 후보자", "summary": body}), \
        "이름 자리에 명사가 온 일반 기사 제목을 사진으로 잘못 잡음"
    # 도장이 통째로 잘리고 '…[이/가] N일 … 하고 있다.' 캡션 문장에서 끊긴 요약 — 2026-09-15 뉴스핌 제보 실물.
    oath = ("장동규, 정일구, 이건주 기자 = 김승원 법무부·이형일 부총리 겸 재정경제부·이소영 중소벤처기업부 장관 "
            "후보자가 15일 오전 서울 여의도 국회에서 열린 인사청문회에서 선서를 하고 있다.... ")
    assert looks_like_photo_caption({"title": "김승원 - 이형일 - 이소영 장관 후보자, 인사청문회 선서", "summary": oath}), \
        "캡션 문장 직후 잘린 사진기사를 놓침"
    for summary in ("…무디스 등 해외기관들은 이미 3.5%를 예상하고 있다.... ",                       # 주어+날짜 없음
                    "금융통화위원회는 지난 7월 16일과 8월 27일 두 차례 기준금리를 올려 연 3.00%를 유지하고 있다.... ",  # 주어 조사 '는'
                    "강신철 후보자의 경우 14~18일 중 하루를 정해 청문회를 여는 방안이 논의되고 있다.... "):
        assert not looks_like_photo_caption({"title": "정부, 재정 운용 방향 논의", "summary": summary}), \
            f"'…고 있다.'에서 잘린 일반 기사를 사진으로 잘못 잡음: {summary}"
    print("20b) '…장관 후보'·받침 ㄴ 서술어·'이름 + 후보자' 캡션 제목·잘린 날짜 도장·캡션 문장 직후 잘림: 통과 (곡선 따옴표 제목·인사 기사·'영끌한 국토 후보자'·'밝혔다. 2024...'·'…고 있다.'에서 잘린 칼럼 등 일반 기사는 제외)")


def test_outlet_photo_tag():
    """매체 고유 사진 표식 `[헤럴드pic]`을 [포토]와 같은 표식으로 잡고, 이름에 pic/픽이 들어간
    다른 말머리(일반 기사)는 안 잡는지, 툴팁이 실제 붙은 표식을 적는지 확인한다
    (app.filters — 2026-09-15 헤럴드경제 인사청문회 사진 제보 실물).
    """
    from app.filters import looks_like_photo_caption, photo_badge_tip

    cut = "김승원 법무부 장관 후보자, 이소영 중소벤처기업부 장관 후보자가 15일 오전 서울 여의도 국회에서 열린 인사청문회에 참석하고 있다. 여야는 이들을 둘러싼... "
    herald = {"title": "[헤럴드pic] 당당·초조·여유…누가 통과할까", "summary": cut}
    assert looks_like_photo_caption(herald), "[헤럴드pic] 표식 사진기사를 놓침"
    assert photo_badge_tip(herald["title"]) == "제목에 [헤럴드pic] 표식이 붙은 사진기사입니다"
    assert photo_badge_tip("[포토] 답변하는 후보자") == "제목에 [포토] 표식이 붙은 사진기사입니다"
    assert "표식은 없지만" in photo_badge_tip("모두발언하는 구윤철 부총리")

    body = "정부가 11일 발표한 개편안에 따르면 내년부터 새 제도가 시행된다."
    for title in ("1주택 종부세 부담 낮췄지만…‘양도세 공제 10억’은 그대로 [Pick코노미]",
                  "[AI픽] 중앙부처 9곳, 삼성SDS '브리티웍스' 도입 확정",
                  "장동혁, 이재명 정부 개각 관련 \"재판 취소용 개각\" [뉴시스Pic]",
                  "[뉴스1 PICK] 서울 시내버스 파업 D-1…통상임금 놓고 노사 '평행선'"):
        assert not looks_like_photo_caption({"title": title, "summary": body}), f"pic 말머리 일반 기사를 사진으로 잘못 잡음: {title}"
    print("20c) [헤럴드pic] 표식·툴팁: 통과 ([Pick코노미]·[AI픽]·[뉴시스Pic]·[뉴스1 PICK]은 제외)")


def test_photo_body_verdict():
    """원문 층(app.photo_body) — 네이버 미러 원문이 「사진 + 캡션 한 문장」이면 사진 기사로,
    짧은 일반 단신·[속보]는 아니라고 판정하고, 판정 결과가 제목·요약 추정을 이기는지 확인한다
    (2026-09-22 뉴스핌·연합뉴스 `'대미투자 사전보고' …` 사진 제보 실물을 줄인 것).
    """
    from app.filters import looks_like_photo_caption, photo_badge_tip

    def page(body, photos=1):
        imgs = '<span class="end_photo_org"><img src="x.jpg"><em class="img_desc">사진 설명</em></span>' * photos
        return f'<html><article id="dic_area" class="go_trans _article_content">{imgs}{body}</article></html>'

    caption = ("(서울=연합뉴스) 신현우 기자 = 22일 국회 재정경제기획위원회 전체회의에 이형일 부총리 겸 "
               "재정경제부 장관이 출석해 있다. 이날 회의에서 대미투자 사전 보고가 진행될 예정이다. "
               "2026.9.22 nowwego@yna.co.kr")
    brief = ("이재명 대통령은 22일 이형일 부총리 겸 재정경제부 장관과 강신철 국방부 장관, 홍지선 "
             "국토교통부 장관에 대한 임명안을 재가했다. 강유정 수석대변인은 이날 공지를 통해 밝혔다.")
    assert photo_body.judge_page(page(caption)) is True, "캡션 한 문장 원문을 사진으로 못 봄"
    assert photo_body.judge_page(page(brief)) is None, "짧은 일반 단신을 원문만으로 단정함(기존 추정에 맡겨야 함)"
    assert photo_body.judge_page(page(caption, photos=0)) is False, "사진 없는 원문을 사진으로 봄"
    assert photo_body.judge_page(page(caption + " 본문이 이어진다." * 30)) is False, "긴 본문을 사진으로 봄"
    assert photo_body.judge_page("<html>본문 없음</html>") is None, "본문을 못 찾았는데 판정함"
    no_day = "이형일 재정경제부 차관이 후보자로 발탁된 후 정부서울청사로 출근하고 있다. (사진=재정경제부)"
    assert photo_body.judge_page(page(no_day)) is None, "날짜 없는 짧은 캡션을 원문만으로 단정함"

    photo_url = "https://n.news.naver.com/mnews/article/001/0000000001"
    news_url = "https://n.news.naver.com/mnews/article/022/0000000002"
    photo_body._index[photo_url] = True
    photo_body._index[news_url] = False
    quoted = {"title": "'대미투자 사전보고' 재경위 출석한 이형일 부총리", "summary": "…진행될 예정이다... ", "url": photo_url}
    assert looks_like_photo_caption(quoted), "원문 판정이 사진인데 추정(따옴표 가드)에 막힘"
    assert photo_badge_tip(quoted["title"], photo_url).startswith("원문이"), "원문 판정 툴팁이 아님"
    guessed = {"title": "모두발언하는 구윤철 부총리", "summary": "", "url": news_url}
    assert not looks_like_photo_caption(guessed), "원문 판정이 일반 기사인데 제목 추정이 이김"
    guessed["url"] = "https://www.example.com/1"
    assert looks_like_photo_caption(guessed), "원문을 못 본 기사에서 기존 추정이 사라짐"
    undecided_url = "https://n.news.naver.com/mnews/article/030/0000000003"
    photo_body._index[undecided_url] = None
    guessed["url"] = undecided_url
    assert looks_like_photo_caption(guessed), "원문으로 못 가른 기사에서 기존 추정이 사라짐"
    tagged = {"title": "[포토] 답변하는 후보자", "summary": "", "url": news_url}
    assert looks_like_photo_caption(tagged), "[포토] 표식이 원문 판정에 밀림"
    print("20d) 원문 사진 판정: 캡션 원문·단신·[포토] 표식·추정 폴백 통과")


def test_editorial_filter():
    """실시간 현황 `[사설]` 체크박스 — 판정이 제목 끝의 `[사설]`까지 잡고(위치 무관),
    사설을 모아 소개하는 일반 기사는 안 잡으며, 화면에 체크박스와 data-editorial 표식이
    실제로 그려지는지 확인한다. [단독]·[속보] 알림·글자색을 물고 있는 headline_kind는
    이 말머리를 몰라야 한다(알림이 새어 나가면 안 된다 — app.filters).
    """
    from app.filters import headline_kind, is_editorial
    from app.live_renderer import render_live_page

    assert is_editorial("[사설] 재정 건전성을 다시 묻는다")
    # 제목 끝에 붙이는 매체가 있다(실측 50건 중 7건) — 맨 앞만 보면 그만큼을 놓친다.
    assert is_editorial("‘부동산·세제 그대로’ 새 경제팀, 민심과 市場 수긍할까[사설]")
    assert is_editorial("비거주 1주택을 투기로 보는 시각부터 바꿔야 [사설]")
    # 사설을 모아 소개하는 일반 기사는 사설이 아니다(괄호 안이 정확히 "사설"일 때만).
    assert not is_editorial("[전국 주요 신문 사설](14일 조간)")
    assert not is_editorial("여야, 사설 인용하며 공방")
    # 알림·글자색·정렬을 물고 있는 판정은 사설을 몰라야 한다.
    assert headline_kind("[사설] 재정 건전성을 다시 묻는다") is None

    articles = [
        {"outlet": "조선일보", "title": "[사설] 재정 건전성을 다시 묻는다", "url": "https://x.test/e1",
         "summary": "", "pub_date": "2026-09-16T08:00:00+09:00"},
        {"outlet": "한국경제", "title": "예산 심사 시작됐다 [사설]", "url": "https://x.test/e2",
         "summary": "", "pub_date": "2026-09-16T08:10:00+09:00"},
        {"outlet": "연합뉴스", "title": "[속보] 부총리 후보자 지명", "url": "https://x.test/f1",
         "summary": "", "pub_date": "2026-09-16T08:20:00+09:00"},
        {"outlet": "중앙일보", "title": "세제개편안 국회 제출", "url": "https://x.test/n1",
         "summary": "", "pub_date": "2026-09-16T08:30:00+09:00"},
    ]
    html = render_live_page(articles, True, "09:00", highlight_words=[], outlet_order=[], groups=[])
    assert 'id="headline-editorial-only"' in html, "[사설] 체크박스가 특수조건 줄에 없음"
    # [속보] 바로 뒤 자리 — 말머리끼리 붙어 있어야 한 묶음으로 읽힌다.
    assert html.index('id="headline-flash-only"') < html.index('id="headline-editorial-only"') < html.index('id="unclaimed-only"')
    assert html.count('data-editorial="1"') == 2, "사설 2건에만 표식이 붙어야 함"
    assert html.count('data-editorial=""') == 2
    # 사설에 [속보] 글자색·알림 표식이 딸려붙지 않는다.
    assert html.count('data-headline="속보"') == 1
    print("29) 실시간 [사설] 필터: 통과 (제목 끝 사설도 잡고, 사설 모음 기사·headline_kind는 그대로)")


def test_undo_returns_only_what_the_user_touched():
    """↩ 되돌리기가 돌려주는 touched(세이지로 칠할 기사)가 "담당자가 방금 손댄 것만"인지 확인한다
    (app.undo.push — 2026-09-15 사용자 결정). 3초 안에 따로따로 누른 동작은 되돌리기가 한 번에
    되더라도 마지막 기사만, 한 번의 동작(같은 batch)으로 여러 건을 처리했으면 그 전부.
    """
    from app import undo

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        # 실제 큐레이션 파일·LLM 캐시·회차 파일은 절대 안 건드린다 — undo()가 되돌리며 그
        # 경로들에 쓰기 때문에, 스냅샷 대상 전부를 임시 경로/빈 동작으로 바꿔치기한다.
        with patch.object(undo, "UNDO_FILE", tmp_dir / "undo_stack.json"), \
             patch.object(undo, "_SNAPSHOT_FILES", {"hidden": tmp_dir / "hidden.json"}), \
             patch.object(undo, "_snapshot_llm_cache", lambda: {}), \
             patch.object(undo, "_restore_llm_cache", lambda data: None), \
             patch.object(undo, "_snapshot_latest_run_file", lambda: None), \
             patch.object(undo, "_restore_latest_run_file", lambda data: None):
            # ① 서로 다른 기사를 3초 안에 따로 ↑ — 한 걸음으로 합쳐지되 칠하는 건 마지막 기사뿐
            undo.push("기사 순서 변경", touched=["A"])
            undo.push("기사 순서 변경", touched=["B"])
            assert undo.undo() == {"label": "기사 순서 변경", "touched": ["B"]}, "따로 누른 ↑인데 앞 기사까지 칠함"
            assert undo.undo() is None, "3초 안의 같은 동작이 한 걸음으로 안 합쳐짐"

            # ② 일괄 숨기기(같은 batch로 나뉘어 들어온 요청) — 체크한 기사 전부
            undo.push("기사 숨기기", touched=["A"], batch="b1")
            undo.push("기사 숨기기", touched=["B"], batch="b1")
            undo.push("기사 숨기기", touched=["C"], batch="b1")
            assert undo.undo()["touched"] == ["A", "B", "C"], "한 번에 숨긴 묶음이 전부 안 칠해짐"

            # ③ 일괄 숨기기 직후(3초 안) 단건 🗑️ — batch가 다르면 마지막 동작의 기사로 갈아끼운다
            undo.push("기사 숨기기", touched=["A"], batch="b2")
            undo.push("기사 숨기기", touched=["B"], batch="b2")
            undo.push("기사 숨기기", touched=["Z"])
            assert undo.undo()["touched"] == ["Z"], "앞 묶음까지 칠함"

            # ④ 기사를 짚지 않는 동작(AI 재분류 등)은 아무것도 안 칠한다
            undo.push("AI 소제목 분류")
            assert undo.undo()["touched"] == [], "AI 재분류 되돌리기가 기사를 칠함"

            # ⑤ 이 필드가 생기기 전에 쌓인 옛 기록 — 되돌리기는 되고 칠하지는 않는다
            undo.push("기사 숨기기", touched=["A"])
            data = json.loads((tmp_dir / "undo_stack.json").read_text(encoding="utf-8"))
            for entry in data["entries"]:
                entry.pop("touched", None)
                entry.pop("batch", None)
            (tmp_dir / "undo_stack.json").write_text(json.dumps(data), encoding="utf-8")
            assert undo.undo() == {"label": "기사 숨기기", "touched": []}, "옛 기록 되돌리기가 깨짐"
    print("21) 되돌리기 음영 대상: 통과 (따로 누른 건 마지막 기사만, 한 번에 처리한 묶음은 전부, AI 동작은 없음)")


def test_label_archive_copy_and_order():
    """라벨 보관함의 순서가 발행 최신순이고(발행시각 없는 기사는 맨 뒤), [복사][txt][xlsx]가
    그 순서 그대로 같은 텍스트를 내는지 확인한다(2026-09-15 — 머리줄은 소제목 형식을 입힌
    라벨명). 걸리는 기사가 0건인 조합엔 버튼을 안 그린다."""
    import html as html_mod
    import re
    from app import labels
    from app.settings_server import render_labels_page

    with tempfile.TemporaryDirectory() as tmp:
        with patch.object(labels, "LABELS_FILE", Path(tmp) / "labels.json"):
            reg = {"kind": "regular"}
            # 붙이는 순서(b → a → c)를 발행 순서(a 최신 → b → c 시각 없음)와 일부러 어긋나게.
            labels.attach_label({"url": "https://t.com/b", "outlet": "중앙일보", "title": "세제 B",
                                 "pub_date": "2026-09-10T09:00:00+09:00"}, "세제", reg)
            labels.attach_label({"url": "https://t.com/a", "outlet": "KBS", "title": "세제 A",
                                 "pub_date": "2026-09-12T15:30:00+09:00"}, "세제", reg)
            labels.attach_label({"url": "https://t.com/c", "outlet": "뉴시스", "title": "세제 C",
                                 "pub_date": ""}, "세제", reg)
            labels.attach_label({"url": "https://t.com/a", "outlet": "KBS", "title": "세제 A"}, "보고서", reg)

            order = [r["url"] for r in labels.articles_for_labels_and(["세제"])]
            assert order == ["https://t.com/a", "https://t.com/b", "https://t.com/c"], \
                f"발행 최신순(a, b, 시각 없는 c는 맨 뒤)이어야 하는데 {order}"

            page = render_labels_page(["세제"])
            copy_text = html_mod.unescape(re.search(r'data-copy-text="([^"]*)"', page).group(1))
            assert copy_text == (
                "<세제>\n"
                "ㅇ (KBS) 세제 A\nhttps://t.com/a\n\n"
                "ㅇ (중앙일보) 세제 B\nhttps://t.com/b\n\n"
                "ㅇ (뉴시스) 세제 C\nhttps://t.com/c\n"
            ), f"복사 텍스트가 어긋났다: {copy_text!r}"
            txt_text = html_mod.unescape(re.search(r'name="text" value="([^"]*)"', page).group(1))
            assert txt_text == copy_text, "txt와 복사가 같은 텍스트여야 한다"
            names = re.findall(r'name="filename" value="([^"]*)"', page)
            today = datetime.now().strftime("%Y-%m-%d")
            assert names == [f"{today}_라벨_세제.txt", f"{today}_라벨_세제.xlsx"], names
            rows = json.loads(html_mod.unescape(re.search(r'name="rows" value="([^"]*)"', page).group(1)))
            assert [r["URL"] for r in rows] == order, "xlsx도 화면과 같은 순서여야 한다"

            multi = render_labels_page(["세제", "보고서"])
            assert "&lt;세제 + 보고서&gt;" in multi, "여러 라벨이면 머리줄이 <세제 + 보고서>여야 한다"
            labels.attach_label({"url": "https://t.com/d", "outlet": "MBC", "title": "D"}, "기타라벨", reg)
            assert 'class="exp"' not in render_labels_page(["보고서", "기타라벨"]), \
                "걸리는 기사가 0건인 조합엔 복사·txt·xlsx를 그리지 않는다"
    print("23) 라벨 보관함 복사·txt: 통과 (발행 최신순, <라벨명> 머리줄, 세 버튼이 같은 순서·같은 텍스트)")


def test_photo_gather_and_adhoc_bulk_hide():
    """📷 사진 추정 모아 보기(PHOTO_GROUP_MOCKUP.html B안)와 수시 선택 바 「🗑 숨기기」를 확인한다.

    [추가: 2026-09-15]
      1) 정기 확정본: 사진 추정이 있으면 「📷 사진 추정 (N)」 알약, 없으면 버튼이 없다
         (옛 「사진 추정 전체 선택」은 사라졌다)
      2) 수시: 같은 이름의 버튼 + 선택 바의 숨기기 + 소제목 칸 이름(이름표용)
      3) bulk-hide는 화면에 보이는 것만 숨기고 되돌리기를 한 번만 쌓는다 — 숨길 게 없으면
         되돌리기를 안 쌓는다
    """
    _require_isolation()
    from app.adhoc import card, undo as adhoc_undo, renderer as adhoc_renderer, routes as adhoc_routes

    # 1) 정기 확정본
    def reg(url, title):
        return {"outlet": "연합뉴스", "title": title, "url": url, "summary": "요약",
                "pub_date": "2026-09-15T10:00:00+09:00", "group": "청문회"}
    args = (["청문회"], [], "ㅇ ({outlet}) {title}", "<{section}>")
    with_photo = renderer.render_page("11:00", [reg("https://r.x/1", "청문회 답변"),
                                               reg("https://r.x/2", "[포토] 답변하는 후보자")], *args)
    assert "사진 추정 (1)" in with_photo, "정기 모아 보기 버튼 없음"
    assert "selectAllPhotoSuspects" not in with_photo, "옛 전체 선택 함수가 남아 있음"
    assert "photoGather:" in with_photo and "togglePhotoGather" in with_photo
    without = renderer.render_page("11:00", [reg("https://r.x/1", "청문회 답변")], *args)
    assert 'onclick="togglePhotoGather()"' not in without, "사진 추정 0건인데 버튼이 그려짐"

    with tempfile.TemporaryDirectory() as tmp:
        adhoc_dir = Path(tmp)
        for sub in ("cards", "trash", "undo"):
            (adhoc_dir / sub).mkdir()
        with patch.object(card, "CARDS_DIR", adhoc_dir / "cards"), \
             patch.object(card, "TRASH_DIR", adhoc_dir / "trash"), \
             patch.object(card, "ISSUES_FILE", adhoc_dir / "issues.json"), \
             patch.object(adhoc_undo, "UNDO_DIR", adhoc_dir / "undo"):
            now = datetime.now().replace(second=0, microsecond=0)
            today = now.strftime("%Y-%m-%d")
            issue = card.get_or_create_issue("인사청문회")
            # [수정: 2026-09-15] 사진 추정 모아 보기·선택 바 숨기기는 이제 수집 확정본의 일이다
            # (원본은 로데이터라 숨기기가 없다 — 아래 끝에서 확인).
            c = card.new_bundle_card("인사청문회", now=now, issue_id=issue["id"])

            def art(url, title, group=None, hidden=False):
                return {"outlet": "뉴스1", "title": title, "url": url, "summary": "청문회",
                        "pub_date": f"{today}T00:00:30+09:00", "matched_keywords": ["청문회"],
                        "group": group, "order": None, "hidden": hidden,
                        "added_at": f"{today}T00:01:00", "added_by": "manual"}

            c["articles"] = [
                art("https://a.x/p1", "[포토] 답변하는 후보자", group="현장"),
                art("https://a.x/p2", "[포토] 출근하는 후보자", group="현장"),
                art("https://a.x/n1", "청문회 쟁점 정리", group="현장"),
                art("https://a.x/old", "[포토] 이미 숨긴 사진", hidden=True),
            ]
            card.save_card(c)

            # 2) 화면
            page = adhoc_renderer.render_card_page(card.load_card(c["id"]))
            assert "사진 추정 (2)" in page, "수시 모아 보기 버튼 없음(숨긴 기사는 안 센다)"
            assert "bulkHide(" in page and 'data-group-name="현장"' in page

            # 3) bulk-hide
            calls = []

            class _Handler:
                def _redirect(self, target):
                    calls.append(target)

            adhoc_routes._handle_bulk_hide(
                _Handler(), {"id": [c["id"]], "urls": ["https://a.x/p1", "https://a.x/p2", "https://a.x/old"]}
            )
            saved = {a["url"]: a["hidden"] for a in card.load_card(c["id"])["articles"]}
            assert saved == {"https://a.x/p1": True, "https://a.x/p2": True,
                             "https://a.x/n1": False, "https://a.x/old": True}, f"숨김 결과가 다름: {saved}"
            assert adhoc_undo.peek_label(c["id"]) == "기사 여러 건 숨기기", "되돌리기가 안 쌓임"
            stack_file = adhoc_dir / "undo" / f"{c['id']}.json"
            depth = len(json.loads(stack_file.read_text(encoding="utf-8"))["entries"])
            # 이미 숨긴 것만 다시 보내면 할 일이 없다 — 되돌리기를 안 쌓는다.
            adhoc_routes._handle_bulk_hide(_Handler(), {"id": [c["id"]], "urls": ["https://a.x/old"]})
            assert len(json.loads(stack_file.read_text(encoding="utf-8"))["entries"]) == depth, "빈 숨기기가 되돌리기를 쌓음"
            assert all("error=" not in t for t in calls)

            # 로데이터 원본엔 모아 보기·숨기기가 없다.
            raw = card.new_card(issue["id"], "인사청문회", "00:00", "00:01", ["청문회"], now=now)
            raw["articles"] = [dict(art("https://a.x/p1", "[포토] 답변하는 후보자"), added_by="collect")]
            card.save_card(raw)
            raw_page = adhoc_renderer.render_card_page(card.load_card(raw["id"]))
            assert ('onclick="togglePhotoGather()"' not in raw_page
                    and "onclick=\"bulkHide(" not in raw_page), "원본에 숨기기가 남아 있음"

    print("24) 사진 추정 모아 보기 · 수시 일괄 숨기기: 통과 (0건이면 버튼 없음, 보이는 것만 숨김, 되돌리기 한 걸음)")


def test_split_group():
    """「AI 기사 나누기」(2026-09-15) — 고른 기사만 새 소제목으로 나누고 다른 소제목은 안 건드리는지,
    그리고 결과가 「AI 기사 배정」과 같은 저장소(배정 기록·AI가 지은 이름 목록)와 소제목 순서에
    원래 자리 그대로 남는지 확인한다. AI 응답은 목으로 바꾼다."""
    from types import SimpleNamespace
    from app import assigned_groups, classifier, group_order, llm_classifier, settings_server, undo
    from app.group_split import order_with_new_names, plan_split

    _require_isolation()
    A = [_article(f"a{i}") for i in range(6)]
    urls = [a["url"] for a in A]

    def g(name, items):
        return {"name": name, "articles": list(items), "summary": ""}

    # ── 계산 규칙(app.group_split.plan_split) ──
    plan = plan_split("청문회", urls, [g("비거주 논란", A[:3]), g("정책 구상", A[3:])], True, {"재경관 회의"}, 5, True)
    assert plan["new_names"] == ["비거주 논란", "정책 구상"] and len(plan["moves"]) == 6, "기본 나누기가 틀림"
    assert plan_split("청문회", urls, [g("청문회 전체", A)], True, set(), 5, True) is None, \
        "통째로 새 이름 하나(= 이름만 바꾼 것)를 나눈 것으로 침"
    plan = plan_split("청문회", urls, [g("청문회", A[:2]), g("비거주 논란", A[2:])], True, set(), 5, True)
    assert set(plan["moves"]) == set(urls[2:]) and plan["stay"] == urls[:2], "원래 이름을 쓴 묶음이 제자리에 안 남음"
    plan = plan_split("청문회", urls, [g("청문회 쟁점", A[:2]), g("X", A[2:])], True, set(), 5, True,
                      original_display="청문회 쟁점")
    assert plan["stay"] == urls[:2], "담당자가 붙인 표시 이름을 쓴 묶음이 제자리에 안 남음"
    plan = plan_split("청문회", urls[:4], [g("비거주 논란", A[:4])], False, set(), 5, True)
    assert plan["moves"] == {u: "비거주 논란" for u in urls[:4]}, "일부만 골랐을 때 새 이름 하나로 떼어내지 못함"
    assert plan_split("청문회", urls, [g("재경관 회의", A[:3]), g("청문회", A[3:])], True, {"재경관 회의"}, 5, True) is None, \
        "다른 소제목 이름과 겹친 묶음을 그 소제목에 섞음"
    plan = plan_split("청문회", urls, [g("기타", A[:1]), g("비거주 논란", A[1:])], True, set(), 5, True)
    assert plan["moves"][urls[0]] == "기타" and plan["new_names"] == ["비거주 논란"], "이미 있는 기타로 안 합침"
    plan = plan_split("청문회", urls, [g("작은", A[:1]), g("큰", A[1:4]), g("중간", A[4:])], True, set(), 14, True)
    assert plan["new_names"] == ["큰"] and set(plan["stay"]) == {urls[0], urls[4], urls[5]}, "칸 수 상한을 넘김"
    assert order_with_new_names(["A", "청문회", "B"], "청문회", ["X", "Y", "기타"], {"기타"}) == ["A", "청문회", "X", "Y", "B"], \
        "새 소제목이 원래 자리 뒤에 안 들어감"
    assert order_with_new_names(["A", "B"], "기타", ["X"], {"기타"}) == ["A", "B", "X"], "기타를 나눈 결과가 기타 뒤로 감"

    # ── 서버 핸들러: 저장 경로 ──
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        B = [_article(f"b{i}") for i in range(2)]
        base = [g("청문회", A), g("재경관 회의", B)]
        slot = {"start": "11:00", "end": "14:00"}
        pushed, responses = [], []
        llm_result = [[g("비거주 논란", A[:3]), g("정책 구상", A[3:])]]
        fake = SimpleNamespace(
            _respond_json=lambda data, status=200: responses.append(data),
            _regenerate_screens=lambda: None,
            send_response=lambda code: responses.append(code),
            end_headers=lambda: None,
        )
        fake._split_group = lambda form, final: settings_server._SettingsHandler._split_group(fake, form, final)
        with patch.object(curation, "GROUP_OVERRIDES_FILE", tmp_dir / "overrides.json"), \
             patch.object(curation, "GROUP_LABELS_FILE", tmp_dir / "labels.json"), \
             patch.object(assigned_groups, "ASSIGNED_GROUPS_FILE", tmp_dir / "assigned.json"), \
             patch.object(group_order, "GROUP_ORDER_FILE", tmp_dir / "order.json"), \
             patch.object(settings_server, "next_pending_slot", lambda now: slot), \
             patch.object(settings_server, "_compute_preview_articles", lambda s, sl: A + B), \
             patch.object(settings_server, "classify_articles", lambda *a, **k: [dict(x, articles=list(x["articles"])) for x in base]), \
             patch.object(settings_server, "split_group_articles", lambda *a, **k: llm_result[0]), \
             patch.object(settings_server, "undo_push", lambda label, **k: pushed.append(label)):
            split = settings_server._SettingsHandler._handle_split_group

            split(fake, {"urls": urls, "group": ["재경관 회의"]})
            assert responses[-1] == {"moved": [], "reason": "stale"}, "화면이 본 소제목과 달라도 나눔"
            split(fake, {"urls": urls[:3] + [B[0]["url"]], "group": ["청문회"]})
            assert responses[-1]["reason"] == "mixed", "여러 소제목에 걸친 선택을 나눔"
            split(fake, {"urls": urls[:3], "group": ["청문회"]})
            assert responses[-1]["reason"] == "too_few", "3건을 나눔"
            llm_result[0] = [g("청문회 전체", A)]
            split(fake, {"urls": urls, "group": ["청문회"]})
            assert responses[-1]["reason"] == "no_split", "안 나뉜 결과를 적용함"
            assert not curation.load_group_overrides() and not pushed, "실패했는데 뭔가 저장됨"

            llm_result[0] = [g("비거주 논란", A[:3]), g("정책 구상", A[3:])]
            split(fake, {"urls": urls, "group": ["청문회"]})
            assert sorted(responses[-1]["moved"]) == sorted(urls), f"옮긴 기사 응답이 틀림: {responses[-1]}"
            assert pushed == ["AI 기사 나누기"], "되돌리기 한 걸음이 안 쌓임"
            overrides = curation.load_group_overrides()
            assert overrides == {**{u: "비거주 논란" for u in urls[:3]}, **{u: "정책 구상" for u in urls[3:]}}, overrides
            assert set(assigned_groups.load_assigned_groups()) == {"비거주 논란", "정책 구상"}, "새 이름이 목적지로 등록 안 됨"
            # 다음 렌더링(초안·마감 후 확정본이 거치는 것과 같은 두 단계)에서 원래 자리에 나오는지
            round_id = llm_classifier.round_id_for_slot(slot)
            rendered = classifier._apply_forced_groups(
                [dict(x, articles=list(x["articles"])) for x in base], overrides,
                {a["url"]: i for i, a in enumerate(A + B)}, classifier.forced_group_target_names(),
            )
            names = [x["name"] for x in group_order.apply_group_order(rendered, round_id)]
            assert names == ["비거주 논란", "정책 구상", "재경관 회의"], f"나뉜 소제목이 원래 자리에 안 옴: {names}"

        # ── 확정본: 회차 파일에 저장된 배정을 읽고, 옮긴 기사의 group만 고쳐 쓴다 ──
        with patch.object(curation, "GROUP_OVERRIDES_FILE", tmp_dir / "overrides2.json"), \
             patch.object(curation, "GROUP_LABELS_FILE", tmp_dir / "labels2.json"), \
             patch.object(assigned_groups, "ASSIGNED_GROUPS_FILE", tmp_dir / "assigned2.json"), \
             patch.object(group_order, "GROUP_ORDER_FILE", tmp_dir / "order2.json"), \
             patch.object(settings_server, "split_group_articles", lambda *a, **k: llm_result[0]), \
             patch.object(settings_server, "undo_push", lambda label, **k: pushed.append(label)):
            now = datetime.now().replace(microsecond=0)
            saved = [dict(a, group="청문회", group_summary="청문회 요약") for a in A] + \
                    [dict(b, group="재경관 회의", group_summary="회의 요약") for b in B]
            storage.save_run("14:00", saved, run_at=now.isoformat(), confirmed=True)
            split_final = settings_server._SettingsHandler._handle_split_group_final
            llm_result[0] = [g("비거주 논란", A[:4])]  # 일부만 골랐다 — 새 이름 하나로 떼어낸다
            split_final(fake, {"urls": urls[:4], "group": ["청문회"]})
            assert sorted(responses[-1]["moved"]) == sorted(urls[:4]), f"확정본 나누기 응답이 틀림: {responses[-1]}"
            run = storage.load_latest_run()
            by_url = {a["url"]: a for a in run["articles"]}
            assert [by_url[u]["group"] for u in urls] == ["비거주 논란"] * 4 + ["청문회"] * 2, "확정본 회차 파일 배정이 틀림"
            assert by_url[urls[4]]["group_summary"] == "청문회 요약" and by_url[B[0]["url"]]["group"] == "재경관 회의", \
                "옮기지 않은 기사를 건드림"
            order = group_order.load_group_order(llm_classifier.round_id_for_run(run))
            assert order == ["청문회", "비거주 논란", "재경관 회의"], f"확정본 순서가 틀림: {order}"

    # ── 되돌리기: 새로 스냅샷에 넣은 파일이 옛 기록 때문에 지워지지 않는지 ──
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        assigned_file = tmp_dir / "assigned.json"
        with patch.object(undo, "UNDO_FILE", tmp_dir / "undo_stack.json"), \
             patch.object(undo, "_SNAPSHOT_FILES", {"hidden": tmp_dir / "hidden.json", "assigned": assigned_file}), \
             patch.object(undo, "_snapshot_llm_cache", lambda: {}), \
             patch.object(undo, "_restore_llm_cache", lambda data: None), \
             patch.object(undo, "_snapshot_latest_run_file", lambda: None), \
             patch.object(undo, "_restore_latest_run_file", lambda data: None):
            undo.push("기사 숨기기")
            data = json.loads((tmp_dir / "undo_stack.json").read_text(encoding="utf-8"))
            data["entries"][-1]["files"].pop("assigned")  # 이 파일이 목록에 들어오기 전의 기록
            (tmp_dir / "undo_stack.json").write_text(json.dumps(data), encoding="utf-8")
            assigned_file.write_text('{"date": "x", "names": ["비거주 논란"]}', encoding="utf-8")
            undo.undo()
            assert assigned_file.exists(), "옛 되돌리기 기록이 새로 추가한 파일을 지움"
    print("25) AI 기사 나누기: 통과 (고른 기사만 새 소제목으로, 겹치는 이름·칸 수·안 나뉨 처리, 원래 자리에 삽입, 되돌리기 안전)")


def test_adhoc_move_to_other_issue_bundle():
    """확정본의 「옮기기 ▾」 → 다른 사안 확정본으로 옮기기.

      1) 메뉴엔 오늘 만든 **다른 사안** 확정본만 — 같은 사안의 다른 시각 확정본은 없다
      2) 옮기면 이 확정본에선 숨겨지고, 받는 쪽엔 미분류로 들어가며 sent_from(원본)은 그대로
      3) 같은 사안 확정본으로는 서버가 거부한다
      4) 받는 쪽에서 숨겨뒀던 사본은 되살린다
      5) 보낸 쪽 ↩ 한 번이면 두 확정본이 원래대로(새로 넣은 건 빠지고, 되살린 건 다시 숨김)
    """
    _require_isolation()
    from app.adhoc import card, undo as adhoc_undo, renderer as adhoc_renderer, routes as adhoc_routes

    with tempfile.TemporaryDirectory() as tmp:
        adhoc_dir = Path(tmp)
        for sub in ("cards", "trash", "undo"):
            (adhoc_dir / sub).mkdir()
        with patch.object(card, "CARDS_DIR", adhoc_dir / "cards"), \
             patch.object(card, "TRASH_DIR", adhoc_dir / "trash"), \
             patch.object(card, "ISSUES_FILE", adhoc_dir / "issues.json"), \
             patch.object(adhoc_undo, "UNDO_DIR", adhoc_dir / "undo"):
            now = datetime.now().replace(microsecond=0)
            today = now.strftime("%Y-%m-%d")
            hearing = card.get_or_create_issue("청문회")
            tax = card.get_or_create_issue("재산세")

            def art(url, hidden=False):
                a = {"outlet": "뉴스1", "title": f"기사 {url[-1]}", "url": url, "summary": "요약",
                     "pub_date": f"{today}T10:00:00+09:00", "matched_keywords": ["청문회"],
                     "group": "청문회 일정", "order": None, "hidden": hidden,
                     "added_at": f"{today}T10:05:00", "added_by": "manual",
                     "sent_from": {"card_id": "raw-1", "report_title": "청문회", "window_end": "11:10"}}
                if hidden:
                    a["hidden_at"] = f"{today}T10:06:00"
                return a

            src = card.new_bundle_card("청문회", now=now, issue_id=hearing["id"], basis_time="11:10")
            src["articles"] = [art("https://a.x/1"), art("https://a.x/2"), art("https://a.x/3")]
            card.save_card(src)
            dst = card.new_bundle_card("재산세", now=now + timedelta(seconds=1), issue_id=tax["id"], basis_time="15:20")
            dst["articles"] = [art("https://a.x/3", hidden=True)]
            card.save_card(dst)
            same = card.new_bundle_card("청문회", now=now + timedelta(seconds=2), issue_id=hearing["id"], basis_time="16:00")
            card.save_card(same)

            # 1) 메뉴
            page = adhoc_renderer.render_card_page(card.load_card(src["id"]))
            assert f'data-bundle="{dst["id"]}"' in page, "다른 사안 확정본이 메뉴에 없음"
            assert f'data-bundle="{same["id"]}"' not in page, "같은 사안 확정본이 메뉴에 올라옴"
            assert "moveArticleTo(this)" in page and "bulkMoveTo(this)" in page
            assert "bulk-group-select" not in page and "moveArticle(this)" not in page, "옛 드롭다운이 남아 있음"

            calls = []

            class _Handler:
                def _redirect(self, target):
                    calls.append(target)

            # 3) 같은 사안으로는 거부
            adhoc_routes._handle_move_to_bundle(_Handler(), {"id": [src["id"]], "bundle": [same["id"]], "urls": ["https://a.x/1"]})
            assert "error=" in calls[-1], calls[-1]
            assert not card.load_card(src["id"])["articles"][0]["hidden"]

            # 2)·4) 옮기기
            adhoc_routes._handle_move_to_bundle(
                _Handler(), {"id": [src["id"]], "bundle": [dst["id"]], "urls": ["https://a.x/1", "https://a.x/3"]}
            )
            assert "error=" not in calls[-1] and f"notice_card={dst['id']}" in calls[-1], calls[-1]
            s_after = {a["url"]: a for a in card.load_card(src["id"])["articles"]}
            d_after = {a["url"]: a for a in card.load_card(dst["id"])["articles"]}
            assert s_after["https://a.x/1"]["hidden"] and s_after["https://a.x/3"]["hidden"]
            assert not s_after["https://a.x/2"]["hidden"], "고르지 않은 기사까지 빠짐"
            assert d_after["https://a.x/1"]["group"] is None and not d_after["https://a.x/1"]["hidden"]
            assert d_after["https://a.x/1"]["sent_from"]["card_id"] == "raw-1", "원본 출처가 바뀜"
            assert not d_after["https://a.x/3"]["hidden"], "받는 쪽에서 숨겼던 사본이 안 되살아남"
            assert "https://a.x/1" in card.sent_index("raw-1", card.bundles_for_date(today)), "원본의 보냄 표시가 풀림"

            # 받는 쪽 ↩는 그 확정본 안에서만 — 이 동작이 스택 맨 위에 있어야 한다
            assert adhoc_undo.peek_label(dst["id"]) == "다른 사안에서 옮겨오기"

            # 5) 보낸 쪽 ↩
            adhoc_undo.undo(src["id"])
            s_back = {a["url"]: a for a in card.load_card(src["id"])["articles"]}
            d_back = {a["url"]: a for a in card.load_card(dst["id"])["articles"]}
            assert not s_back["https://a.x/1"]["hidden"] and not s_back["https://a.x/3"]["hidden"]
            assert "https://a.x/1" not in d_back, "되돌린 뒤에도 받는 쪽에 기사가 남음"
            assert d_back["https://a.x/3"]["hidden"], "되살렸던 사본이 다시 숨겨지지 않음"
    print("36) 수시 확정본 다른 사안으로 옮기기: 통과 (다른 사안만 메뉴에, 같은 사안 거부, 숨긴 사본 되살림, ↩ 한 번에 두 확정본 복구)")


def test_adhoc_four_step_flow():
    """수시 4단 흐름(새 수집 → 수집 원본 → 수집 확정본 → 수시 보관함, 2026-09-15)을 확인한다.

      1) 원본은 로데이터 — 소제목·숨기기·라벨·복사/txt가 없고 xlsx·보내기만, 한 목록 최신순
      2) 「확정본으로」(__auto__)는 같은 사안의 오늘 확정본으로 가고, 없으면 그 순간 만든다
         (다른 원본에서 ▾로 고른 확정본에도 보낼 수 있다 — 원본 시각별로 나뉘는 건 아래 28번)
      3) 확정본 머리줄 = 보낸 순간의 원본 시각 중 가장 늦은 것, 숨긴 기사 몫은 빠지고,
         시각이 안 적힌 옛 기사뿐이면 시각 없이
      4) 수시 보관함엔 원본이 안 쌓이고 확정본·옛 원본만
      5) 새 수집의 「지난 수집」 — 같은 날·같은 조건은 한 줄(×N), ?from= 으로 채워 열기
    """
    _require_isolation()
    from app.adhoc import archive_renderer, card, undo as adhoc_undo, renderer as adhoc_renderer, routes as adhoc_routes

    with tempfile.TemporaryDirectory() as tmp:
        adhoc_dir = Path(tmp)
        for sub in ("cards", "trash", "undo"):
            (adhoc_dir / sub).mkdir()
        with patch.object(card, "CARDS_DIR", adhoc_dir / "cards"), \
             patch.object(card, "TRASH_DIR", adhoc_dir / "trash"), \
             patch.object(card, "ISSUES_FILE", adhoc_dir / "issues.json"), \
             patch.object(adhoc_undo, "UNDO_DIR", adhoc_dir / "undo"):
            now = datetime.now().replace(microsecond=0)
            today = now.strftime("%Y-%m-%d")
            issue = card.get_or_create_issue("인사청문회")

            def art(url, hhmm, outlet="뉴스1"):
                return {"outlet": outlet, "title": f"청문회 {url[-1]}", "url": url, "summary": "청문회",
                        "pub_date": f"{today}T{hhmm}:00+09:00", "matched_keywords": ["청문회"],
                        "group": "옛 소제목", "order": None, "hidden": False,
                        "added_at": f"{today}T13:00:00", "added_by": "collect"}

            first = card.new_card(issue["id"], "인사청문회", "09:00", "13:05", ["청문회"], now=now)
            first["articles"] = [art("https://f.x/1", "10:00"), art("https://f.x/2", "12:00", "KBS")]
            card.save_card(first)
            assert card.is_raw(first), "새로 만든 원본에 raw가 없음"

            # 1) 원본 화면
            page = adhoc_renderer.render_card_page(card.load_card(first["id"]))
            for gone in ("소제목 배정", ">다른 소제목<", "hideArticle(this)", "class=\"icon-btn lab-btn", "txt로 저장"):
                assert gone not in page, f"원본에 「{gone}」이 남아 있음"
            assert "send-split" in page and "download-excel" in page
            assert 'class="pair-link' not in page, "확정본이 없는데 확정본 알약이 그려짐"
            assert page.index("https://f.x/2") < page.index("https://f.x/1"), "원본 목록이 최신순이 아님"
            assert "옛 소제목" not in page.split('id="bulk-bar"')[0], "원본에 소제목 칸이 그려짐"

            calls = []

            class _Handler:
                def _redirect(self, target):
                    calls.append(target)

            # 2) 처음 보내면 확정본이 생긴다 — 같은 사안·같은 이름
            adhoc_routes._handle_send_to_bundle(_Handler(), {"id": [first["id"]], "bundle": ["__auto__"], "urls": ["https://f.x/1"]})
            bundles = card.bundles_for_date(today)
            assert len(bundles) == 1 and bundles[0]["issue_id"] == issue["id"] and bundles[0]["report_title"] == "인사청문회", bundles
            bundle_id = bundles[0]["id"]
            assert bundles[0]["articles"][0]["sent_from"]["window_end"] == "13:05", "보낸 순간의 원본 시각이 안 적힘"

            second = card.new_card(issue["id"], "인사청문회", "11:00", "12:30", ["후보자"], now=now + timedelta(seconds=1))
            second["articles"] = [art("https://s.x/1", "12:10")]
            card.save_card(second)
            adhoc_routes._handle_send_to_bundle(_Handler(), {"id": [second["id"]], "bundle": [bundle_id], "urls": ["https://s.x/1"]})
            assert [b["id"] for b in card.bundles_for_date(today)] == [bundle_id], "▾로 고른 확정본이 아닌 곳으로 감"
            assert all("error=" not in t for t in calls), calls

            page = adhoc_renderer.render_card_page(card.load_card(first["id"]))
            assert "확정본 2건</a>" in page and "rawUnsend(this)" in page, "원본에 확정본 알약(두 원본에서 1건씩)·「보냄」이 없음"
            assert "오늘의 원본" in page and f'card?id={bundle_id}" ' not in page.split("tabwrap-h")[1].split("</div>")[1], \
                "원본 탭 줄에 확정본이 섞임"

            # 3) 확정본 머리줄
            bundle = card.load_card(bundle_id)
            assert adhoc_renderer.report_header_text(bundle) == "수시 모니터링 13시 05분 기준(인사청문회)"
            assert "13시 05분 기준" in adhoc_renderer.render_card_page(bundle), "화면 머리줄에 시각이 없음"
            for a in bundle["articles"]:
                if a["url"] == "https://f.x/1":
                    a["hidden"] = True
            card.save_card(bundle)
            assert adhoc_renderer.report_header_text(card.load_card(bundle_id)) == "수시 모니터링 12시 30분 기준(인사청문회)", \
                "숨긴 기사 몫의 시각이 안 빠짐"
            for a in bundle["articles"]:
                a["sent_from"].pop("window_end", None)
            bundle.pop("basis_time", None)  # 이 필드가 생기기 전의 확정본
            card.save_card(bundle)
            assert adhoc_renderer.report_header_text(card.load_card(bundle_id)) == "수시 모니터링 (인사청문회)", \
                "시각이 없는 옛 기사뿐인데 시각을 지어냄"

            # 4) 보관함
            legacy = card.new_card(issue["id"], "인사청문회", "08:00", "09:00", ["청문회"], now=now - timedelta(days=2), raw=False)
            archive = archive_renderer.render_archive_page(preset="all")
            assert f'data-card-id="{bundle_id}"' in archive and f'data-card-id="{legacy["id"]}"' in archive
            assert f'data-card-id="{first["id"]}"' not in archive and f'data-card-id="{second["id"]}"' not in archive, \
                "로데이터 원본이 보관함에 쌓임"

            # 5) 지난 수집
            card.new_card(issue["id"], "인사청문회", "09:00", "10:00", ["청문회"], now=now + timedelta(seconds=2))
            entries = adhoc_renderer._history_entries()
            same = [e for e in entries if e["date"] == today and e["keywords"] == ["청문회"]]
            assert len(same) == 1 and same[0]["count"] == 2, f"같은 날·같은 조건이 한 줄로 안 합쳐짐: {same}"
            new_page = adhoc_renderer.render_new_card_page(pick=first["id"])
            assert "지난 수집" in new_page and "×2" in new_page and "원본 열기" in new_page
            assert "빈 확정본 만들기" not in new_page, "새 수집에 확정본 만들기 링크가 남아 있음"
            assert json.dumps(first["id"]) in new_page, "?from= 으로 받은 카드가 화면에 안 실림"

    print("26) 수시 4단 흐름: 통과 (원본 로데이터·자동 확정본·머리줄 원본 시각·보관함엔 확정본만·지난 수집)")


def test_adhoc_raw_versions():
    """원본 하나 = 검색 조건 하나 — 같은 날·같은 사안의 원본은 「#2」로 가른다.

      1) 첫 원본은 번호 없음, 둘째부터 #2·#3 — 만들 때 적어 두어 가운데를 지워도 안 바뀐다
      2) 탭·머리줄·브라우저 탭 제목엔 #2, 보고서 첫 줄·사안명엔 없음
      3) 편집 창엔 검색어 편집이 없고 「검색어를 바꿔 새로 수집」 링크만
      4) #1에서 보낸 기사는 #2에서도 「✓ 보냄」, 보냄 취소는 두 판 모두 「처리 안 함」
      5) 기준 시각이 같아도 #2는 #1의 확정본을 이어 쓰지 않는다, 확정본 📥 칩에 #2
    """
    _require_isolation()
    from app.adhoc import card, undo as adhoc_undo, renderer as adhoc_renderer, routes as adhoc_routes

    with tempfile.TemporaryDirectory() as tmp:
        adhoc_dir = Path(tmp)
        for sub in ("cards", "trash", "undo"):
            (adhoc_dir / sub).mkdir()
        with patch.object(card, "CARDS_DIR", adhoc_dir / "cards"), \
             patch.object(card, "TRASH_DIR", adhoc_dir / "trash"), \
             patch.object(card, "ISSUES_FILE", adhoc_dir / "issues.json"), \
             patch.object(adhoc_undo, "UNDO_DIR", adhoc_dir / "undo"):
            now = datetime.now().replace(microsecond=0)
            today = now.strftime("%Y-%m-%d")
            issue = card.get_or_create_issue("인사청문회")

            def art(url):
                return {"outlet": "뉴스1", "title": f"청문회 {url[-1]}", "url": url, "summary": "청문회",
                        "pub_date": f"{today}T10:00:00+09:00", "matched_keywords": ["청문회"],
                        "group": None, "order": None, "hidden": False,
                        "added_at": f"{today}T13:00:00", "added_by": "collect"}

            first = card.new_card(issue["id"], "인사청문회", "09:00", "13:05", ["청문회"], now=now)
            first["articles"] = [art("https://v.x/1"), art("https://v.x/2")]
            card.save_card(first)
            second = card.new_card(issue["id"], "인사청문회", "09:00", "13:05", ["청문회", "후보자"],
                                   now=now + timedelta(seconds=1))
            second["articles"] = [art("https://v.x/1"), art("https://v.x/3")]
            card.save_card(second)
            third = card.new_card(issue["id"], "인사청문회", "09:00", "13:05", ["의혹"], now=now + timedelta(seconds=2))
            other = card.new_card(card.get_or_create_issue("세제")["id"], "세제", "09:00", "13:05", ["세제"],
                                  now=now + timedelta(seconds=3))

            # 1) 번호
            assert [card.version_suffix(c) for c in (first, second, third, other)] == ["", " #2", " #3", ""]
            card.delete_card(second["id"])
            assert card.version_suffix(card.load_card(third["id"])) == " #3", "가운데를 지웠더니 번호가 바뀜"
            fourth = card.new_card(issue["id"], "인사청문회", "09:00", "13:05", ["청문회"], now=now + timedelta(seconds=4))
            assert card.version_suffix(fourth) == " #4", "지운 번호를 다시 씀"

            # 1-1) 이름을 고치면 번호가 떨어진다 — 가를 것이 없는데 남으면 없는 판을 가리킨다.
            #      확정본 목록 이름도 같은 규칙을 따르고, 이름을 되돌리면 번호도 돌아온다.
            bundle = card.new_bundle_card("인사청문회", now=now + timedelta(seconds=5),
                                          issue_id=issue["id"], basis_time="13:05",
                                          source_card_id=third["id"], source_version=3)
            assert card.source_version_suffix(bundle) == " #3"
            renamed = card.load_card(third["id"])
            renamed["report_title"] = "의혹 따로"
            card.save_card(renamed)
            assert card.version_suffix(card.load_card(third["id"])) == "", "이름이 혼자인데 번호가 남음"
            assert card.source_version_suffix(bundle) == "", "원본에서 떨어진 번호가 확정본 이름에 남음"
            assert card.version_suffix(fourth) == " #4", "남의 이름을 고쳤는데 번호가 흔들림"
            assert card.version_suffix(card.load_card(first["id"])) == ""
            renamed["report_title"] = "인사청문회"
            card.save_card(renamed)
            assert card.version_suffix(card.load_card(third["id"])) == " #3", "이름을 되돌렸는데 번호가 안 돌아옴"
            card.delete_card(bundle["id"])
            trashed = [c["_trash_name"] for c in card.list_deleted_cards() if c["id"] == second["id"]]
            assert card.restore_card(trashed[0]) == second["id"]

            # 2) 화면에만
            page = adhoc_renderer.render_card_page(card.load_card(second["id"]))
            assert "<title>원본 · 인사청문회 #2" in page, "브라우저 탭 제목에 #2가 없음"
            assert 'class="ver-tag"' in page and "인사청문회 #3</a>" not in page
            assert "인사청문회 #3" in page, "탭 줄에 #3이 없음"
            assert adhoc_renderer.report_header_text(card.load_card(second["id"])).endswith("(인사청문회)"), \
                "보고서 첫 줄에 번호가 샘"

            # 3) 편집 창
            assert "/adhoc/card/add-keyword" not in page and "set-match-mode" not in page, "검색어 편집이 남아 있음"
            assert f'/adhoc/new?from={second["id"]}' in page and "검색어를 바꿔 새로 수집" in page

            calls = []

            class _Handler:
                def _redirect(self, target):
                    calls.append(target)

            # 4) #1에서 보낸 기사는 #2에서도 보냄
            adhoc_routes._handle_send_to_bundle(_Handler(), {"id": [first["id"]], "bundle": ["__auto__"], "urls": ["https://v.x/1"]})
            states = card.raw_row_states(card.load_card(second["id"]), card.bundles_for_date(today))
            assert states.get("https://v.x/1", ("",))[0] == "sent", "#1에서 보낸 기사가 #2에서 보냄으로 안 보임"
            assert "https://v.x/3" in card.open_raw_urls(card.load_card(second["id"]), card.bundles_for_date(today))
            assert "https://v.x/1" not in card.open_raw_urls(card.load_card(second["id"]), card.bundles_for_date(today))

            # 5) #2에서 보내면 #1의 확정본이 아니라 새 확정본
            b1 = card.bundles_for_date(today)[0]["id"]
            adhoc_routes._handle_send_to_bundle(_Handler(), {"id": [second["id"]], "bundle": ["__auto__"], "urls": ["https://v.x/3"]})
            bundles = card.bundles_for_date(today)
            assert len(bundles) == 2, "기준 시각이 같다고 #2가 #1의 확정본으로 섞여 들어감"
            b2 = [b for b in bundles if b["id"] != b1][0]
            assert "인사청문회 #2 1건" in adhoc_renderer.render_card_page(card.load_card(b2["id"])), "📥 칩에 #2가 없음"

            # 4b) #2에서 보냄 취소 — #1이 보낸 사본이 빠지고, 두 판 모두 처리 안 함
            adhoc_routes._handle_raw_unsend(_Handler(), {"id": [second["id"]], "url": ["https://v.x/1"]})
            day = card.bundles_for_date(today)
            for c in (first, second):
                assert "https://v.x/1" not in card.raw_row_states(card.load_card(c["id"]), day), \
                    "보냄 취소한 기사가 어느 판에선 숨김·보냄으로 남음"
            assert all("error=" not in t for t in calls), calls
    print("43) 수시 원본 판 번호: 통과 (#2·#3 고정, 화면에만, 편집 창은 시간만, 보냄 공유, 확정본 안 섞임)")


def test_adhoc_raw_trash_and_rounds():
    """수집 원본의 🗑 · 「처리 안 한 N건 전부 확정본으로」 · 불러올 때마다 새 확정본(2026-09-15).

      1) 원본 🗑는 목록에서 빼지 않고 「숨김」 표시만 — 필터는 「아직 처리 안 한 기사만」, 되돌리기 가능
      2) 전부 보내기는 보냄·숨김 표시가 없는 기사만 보내고, 확정본에 원본 시각(basis_time)이 적힌다
      3) 확정본에서 뺀 기사는 원본에서 「숨김」, 다시 누르면 「처리 안 함」 — 다시 보내면 되살아난다
      4) 「지금까지 불러오기」 뒤에 보내면 새 확정본, 같은 시각에서 또 보내면 그 확정본으로
      5) 탭·보관함은 「HH:MM 기준」으로 가른다
    """
    _require_isolation()
    from app.adhoc import archive_renderer, card, undo as adhoc_undo, renderer as adhoc_renderer, routes as adhoc_routes

    with tempfile.TemporaryDirectory() as tmp:
        adhoc_dir = Path(tmp)
        for sub in ("cards", "trash", "undo"):
            (adhoc_dir / sub).mkdir()
        with patch.object(card, "CARDS_DIR", adhoc_dir / "cards"), \
             patch.object(card, "TRASH_DIR", adhoc_dir / "trash"), \
             patch.object(card, "ISSUES_FILE", adhoc_dir / "issues.json"), \
             patch.object(adhoc_undo, "UNDO_DIR", adhoc_dir / "undo"):
            now = datetime.now().replace(microsecond=0)
            today = now.strftime("%Y-%m-%d")
            issue = card.get_or_create_issue("인사청문회")

            def art(n, hhmm):
                return {"outlet": "뉴스1", "title": f"청문회 기사 {n}", "url": f"https://r.x/{n}", "summary": "청문회",
                        "pub_date": f"{today}T{hhmm}:00+09:00", "matched_keywords": ["청문회"],
                        "group": None, "order": None, "hidden": False,
                        "added_at": f"{today}T15:05:00", "added_by": "collect"}

            raw = card.new_card(issue["id"], "인사청문회", "11:00", "15:05", ["청문회"], now=now)
            raw["articles"] = [art(1, "14:50"), art(2, "14:40"), art(3, "14:30"), art(4, "14:20")]
            card.save_card(raw)
            rid = raw["id"]
            calls = []

            class _Handler:
                def _redirect(self, target):
                    calls.append(target)

            def page():
                return adhoc_renderer.render_card_page(card.load_card(rid))

            p = page()
            assert "rawHide(this)" in p and "모든 기사 확정본으로" in p and 'data-count="4"' in p
            # f-string 안의 JS라 줄바꿈은 역슬래시를 두 번 적어야 한다 — 한 번이면 스크립트 전체가 죽는다
            assert "'') + '\\n\\n'" in p, "전부 보내기 확인창의 줄바꿈이 JS 문자열을 끊음"
            assert "남은 기사만 보기" not in p, "처리한 게 없는데 필터가 그려짐"
            assert 'id="raw-select-all"' in p, "원본 목록 머리에 전체 선택이 없음"
            assert 'onclick="copyWholeCard()"' in p and "/adhoc/card/download?id=" in p, "원본에 copy·txt가 없음"
            assert 'class="pair-link' not in p, "확정본이 없는데 확정본 알약이 그려짐"

            # 1) 원본 🗑 — 표시만, 되돌리기 가능
            adhoc_routes._handle_raw_mark(_Handler(), {"id": [rid], "url": ["https://r.x/1"]}, hide=True)
            p = page()
            assert "https://r.x/1" in p and "is-raw-hid" in p, "숨긴 기사가 목록에서 사라짐"
            assert "남은 기사만 보기" in p and "숨김 1" in p and 'data-count="3"' in p
            txt = adhoc_renderer.build_adhoc_plain_text(card.load_card(rid))
            assert "https://r.x/1" not in txt and "https://r.x/2" in txt, "원본 복사에 숨김 표시 기사가 섞임"
            assert adhoc_undo.peek_label(rid) == "기사 숨김 표시"
            adhoc_undo.undo(rid)
            assert not card.load_card(rid).get("raw_marks"), "되돌리기로 숨김 표시가 안 풀림"
            adhoc_routes._handle_raw_mark(_Handler(), {"id": [rid], "url": ["https://r.x/1"]}, hide=True)

            # 2) 전부 보내기
            adhoc_routes._handle_send_to_bundle(_Handler(), {"id": [rid], "bundle": ["__auto__"], "all_open": ["1"]})
            bundles = card.bundles_for_date(today)
            assert len(bundles) == 1 and bundles[0].get("basis_time") == "15:05", bundles
            b1 = bundles[0]["id"]
            assert sorted(a["url"] for a in bundles[0]["articles"]) == ["https://r.x/2", "https://r.x/3", "https://r.x/4"], \
                "숨김 표시한 기사까지 보냄"
            p = page()
            assert "보냄 3 · 숨김 1" in p and 'onclick="sendAllOpen(this)"' not in p, "처리할 게 없는데 전부 보내기가 남음"
            assert "rawUnsend(this)" in p and "↩ 보냄 취소" in p, "보낸 기사에 보냄 취소가 없음"

            # 2b) 「✓ 보냄」 다시 누르기 = 보냄 취소 → 처리 안 함(숨김 아님), ↩ 한 번이면 다시 보냄
            adhoc_routes._handle_raw_unsend(_Handler(), {"id": [rid], "url": ["https://r.x/3"]})
            assert [a["hidden"] for a in card.load_card(b1)["articles"] if a["url"] == "https://r.x/3"] == [True], \
                "보냄 취소했는데 확정본에 남음"
            assert "https://r.x/3" not in card.raw_row_states(card.load_card(rid), card.bundles_for_date(today)), \
                "보냄 취소한 기사가 숨김으로 보임"
            assert "보냄 2 · 숨김 1" in page() and 'data-count="1"' in page()
            assert adhoc_undo.peek_label(rid) == "보냄 취소" and adhoc_undo.peek_label(b1) == "원본에서 보냄 취소"
            adhoc_undo.undo(rid)
            assert [a["hidden"] for a in card.load_card(b1)["articles"] if a["url"] == "https://r.x/3"] == [False], \
                "원본 ↩가 확정본 사본을 되살리지 않음"
            assert card.raw_row_states(card.load_card(rid), card.bundles_for_date(today))["https://r.x/3"][0] == "sent"

            # 3) 확정본에서 빼면 원본에서 「숨김」 → 되돌리면 처리 안 함 → 다시 보내면 되살아남
            adhoc_routes._handle_set_hidden(_Handler(), {"id": [b1], "url": ["https://r.x/2"]}, hidden=True)
            p = page()
            assert "확정본에서 뺀 기사예요" in p and "숨김 2" in p, "확정본에서 뺀 기사가 원본에서 숨김으로 안 보임"
            adhoc_routes._handle_raw_mark(_Handler(), {"id": [rid], "url": ["https://r.x/2"]}, hide=False)
            p = page()
            assert "확정본에서 뺀 기사예요" not in p and 'data-count="1"' in p, "되돌렸는데 여전히 숨김"
            adhoc_routes._handle_send_to_bundle(_Handler(), {"id": [rid], "bundle": ["__auto__"], "urls": ["https://r.x/2"]})
            b = card.load_card(b1)
            assert [a["hidden"] for a in b["articles"] if a["url"] == "https://r.x/2"] == [False], "다시 보냈는데 확정본에 안 돌아옴"
            assert len(card.bundles_for_date(today)) == 1
            adhoc_routes._handle_set_hidden(_Handler(), {"id": [b1], "url": ["https://r.x/2"]}, hidden=True)
            assert card.raw_row_states(card.load_card(rid), card.bundles_for_date(today))["https://r.x/2"][0] == "hidden", \
                "되돌린 뒤 다시 뺐는데 원본에 숨김으로 안 보임"

            # 4) 지금까지 다시 수집 뒤 → 새 확정본, 같은 시각에서 또 → 그 확정본
            raw = card.load_card(rid)
            raw["window"]["end"] = "16:10"
            raw["articles"] += [art(5, "15:40"), art(6, "15:30")]
            card.save_card(raw)
            assert 'class="pair-link' not in page(), "다시 수집한 뒤인데 옛 시각 확정본 알약이 남음"
            adhoc_routes._handle_send_to_bundle(_Handler(), {"id": [rid], "bundle": ["__auto__"], "urls": ["https://r.x/5"]})
            adhoc_routes._handle_send_to_bundle(_Handler(), {"id": [rid], "bundle": ["__auto__"], "urls": ["https://r.x/6"]})
            bundles = sorted(card.bundles_for_date(today), key=lambda x: x["created_at"])
            assert len(bundles) == 2, f"불러온 뒤 보냈는데 확정본이 {len(bundles)}개"
            b2 = [x for x in bundles if x["id"] != b1][0]
            assert b2.get("basis_time") == "16:10" and len(b2["articles"]) == 2, "같은 원본 시각에서 보낸 게 한 확정본으로 안 모임"
            assert adhoc_renderer.report_header_text(b2) == "수시 모니터링 16시 10분 기준(인사청문회)"
            assert all("error=" not in t for t in calls), calls

            # 5) 탭·보관함
            bp = adhoc_renderer.render_card_page(card.load_card(b2["id"]))
            assert "인사청문회 15:05" in bp and "인사청문회 16:10" in bp, "확정본 탭에 시각이 없음"
            archive = archive_renderer.render_archive_page(preset="all")
            assert "15:05 기준" in archive and "16:10 기준" in archive, "보관함 줄에 시각이 없음"
    print("28) 수집 원본 🗑 · 전부 보내기 · 불러올 때마다 새 확정본: 통과")


def test_destination_switches():
    """검색어 그룹의 목적지 스위치 두 개(실시간 / 초안·확정본)가 서로 독립으로 도는지 확인한다.

    [추가: 2026-09-15] 옛 형식(enabled + include_in_scrap)을 옮기는 규칙 — 특히 「꺼짐 +
    정기 체크」가 초안·확정본으로 새어 들어가지 않는지 — 과, 새로 생긴 「초안·확정본만」
    그룹이 확정본엔 들어가고 실시간엔 안 들어가는지를 본다.
    """
    _require_isolation()
    legacy = {
        "base_group_seeded": True,
        "keyword_groups": [
            {"name": "둘다", "keywords": ["환율"], "mode": "OR", "include_in_scrap": True, "enabled": True},
            {"name": "실시간만", "keywords": ["부동산"], "mode": "OR", "include_in_scrap": False, "enabled": True},
            {"name": "꺼짐정기체크", "keywords": ["물가"], "mode": "OR", "include_in_scrap": True, "enabled": False},
        ],
    }
    settings.SETTINGS_FILE.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
    loaded = {g["name"]: g for g in settings.load_settings()["keyword_groups"]}
    assert (loaded["둘다"]["include_in_live"], loaded["둘다"]["include_in_scrap"]) == (True, True)
    assert (loaded["실시간만"]["include_in_live"], loaded["실시간만"]["include_in_scrap"]) == (True, False)
    assert (loaded["꺼짐정기체크"]["include_in_live"], loaded["꺼짐정기체크"]["include_in_scrap"]) == (False, False), \
        "꺼 둔 그룹의 정기 체크가 초안·확정본으로 새어 들어감"
    assert loaded["꺼짐정기체크"]["enabled"] is False and loaded["실시간만"]["enabled"] is True

    # 새로 생긴 상태 — 초안·확정본만
    settings.save_keyword_groups([
        {"name": "보고서만", "keywords": ["정부"], "mode": "OR", "include_in_live": False, "include_in_scrap": True},
        {"name": "실시간만", "keywords": ["부동산"], "mode": "OR", "include_in_live": True, "include_in_scrap": False},
        {"name": "꺼짐", "keywords": ["물가"], "mode": "OR", "include_in_live": False, "include_in_scrap": False},
    ])
    s = settings.load_settings()
    live_names = [g["name"] for g in settings.active_search_groups(s)]
    assert live_names == ["실시간만"], f"실시간 그룹이 스위치를 안 따름: {live_names}"
    assert settings.all_search_keywords(s) == ["정부"]
    in_use = [g["name"] for g in settings.active_search_groups(s, s["keyword_groups"])]
    assert in_use == ["보고서만", "실시간만"], f"둘 다 끈 그룹이 '쓰이는 그룹'에 섞임: {in_use}"

    captured = {}

    def spy(groups, after=None, before=None, track_keyword_matches=False):
        captured["groups"] = groups
        return [], []

    with patch.object(scraper, "search_articles_by_groups", spy):
        scraper.collect_run("10:30")
    assert [g["name"] for g in captured["groups"]] == ["보고서만"], "확정본 수집이 초안·확정본 스위치를 안 따름"

    # 초안·확정본이 하나도 없으면 막는다(화면 JS와 같은 문구)
    try:
        settings.save_keyword_groups([
            {"name": "실시간만", "keywords": ["부동산"], "mode": "OR", "include_in_live": True, "include_in_scrap": False},
        ])
    except settings.SettingsError as e:
        assert str(e) == '"초안·확정본"을 켠 그룹이 최소 1개는 있어야 합니다.'
    else:
        raise AssertionError("초안·확정본 그룹 0개인데 저장됨")
    print("27) 목적지 스위치: 통과 (옛 형식 옮기기·초안·확정본만·실시간만·둘 다 끔)")


def test_telegram_recipients_receive_and_notify():
    """텔레그램 받는 사람마다 받는 것(정기·수시 × 기사·요약, [단독]·[속보])과 알림 시간을
    정한다(2026-09-17, mockups/TELEGRAM_RECIPIENTS_MOCKUP.html).

      1) 옛 기록: enabled → 정기 기사, notify 없음 → 하루 종일(조용해지면 안 된다)
      2) 알림 시간: 업무 시간·자정 넘기는 평일 구간(금요일 밤은 토요일 새벽까지)
      3) 알림 시간 밖이면 disable_notification — 명단에 없는 chat id는 울린다
      4) 메시지: 기사만 / 요약만(보고서 머리 + 요약) / 둘 다 한 통 / 요약이 없거나 AI 분류
         실패면 기사 목록으로
      5) 정기 발송이 사람마다 다른 메시지를 보낸다
      6) 수시 확정본 발송 — 확인창에서 뺀 사람은 안 받고, 두 번째부터 (수정), 발송 기록
    """
    _require_isolation()
    from app import report_message
    from app.adhoc import card, send as adhoc_send, undo as adhoc_undo, renderer as adhoc_renderer

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        recipients_file = tmp_dir / "telegram_recipients.json"
        with patch.object(telegram_recipients, "TELEGRAM_RECIPIENTS_FILE", recipients_file), \
             patch.object(credentials, "telegram_bot_token", lambda: "fake-token"), \
             patch.object(telegram_bot.requests, "post") as mock_post:
            mock_post.return_value = MagicMock(ok=True)

            # 1) 옛 기록 읽기
            recipients_file.write_text(json.dumps([
                {"name": "옛사람", "chat_id": "100", "enabled": True, "alert_scoop": True, "alert_flash": False},
                {"name": "꺼둔사람", "chat_id": "101", "enabled": False},
            ], ensure_ascii=False), encoding="utf-8")
            old = telegram_recipients.load_telegram_recipients()
            assert old[0]["regular_articles"] is True and old[0]["regular_summary"] is False
            assert old[0]["alert_scoop"] is True and old[0]["adhoc_articles"] is False
            assert old[1]["regular_articles"] is False
            assert old[0]["notify"] == {"mode": "always"}, f"옛 기록은 하루 종일이어야 함: {old[0]['notify']}"

            # 2) 알림 시간
            rings = telegram_recipients.rings_now
            work = {"mode": "work"}
            assert rings(work, datetime(2026, 9, 17, 9, 0)) and not rings(work, datetime(2026, 9, 17, 18, 0))
            assert not rings(work, datetime(2026, 9, 19, 10, 0)), "토요일엔 업무 시간이 아님"
            night = {"mode": "custom", "days": "weekday", "start": "22:00", "end": "07:00"}
            assert rings(night, datetime(2026, 9, 18, 23, 0)), "금요일 23시"
            assert rings(night, datetime(2026, 9, 19, 3, 0)), "금요일 밤에 시작한 구간은 토요일 새벽까지"
            assert not rings(night, datetime(2026, 9, 21, 3, 0)), "일요일 밤에 시작한 구간은 평일이 아님"
            assert telegram_recipients.normalize_notify({"mode": "custom", "start": "10:00", "end": "10:00"}) == {"mode": "always"}
            assert telegram_recipients.notify_label({"mode": "custom", "days": "daily", "start": "08:30", "end": "19:00"}) == "매일 8:30–19"

            # 3) 조용히 보내기
            telegram_recipients.save_telegram_recipients([
                {"name": "업무", "chat_id": "1", "regular_articles": True, "notify": work},
                {"name": "종일", "chat_id": "2", "regular_summary": True, "notify": {"mode": "always"}},
            ])
            saved = json.loads(recipients_file.read_text(encoding="utf-8"))
            assert saved[0]["enabled"] is True and saved[1]["enabled"] is False, "enabled는 정기 기사를 따라 적는다"
            telegram_bot.send_text("안녕", ["1", "2", "999"], now=datetime(2026, 9, 17, 20, 0))
            sent = {c.kwargs["data"]["chat_id"]: c.kwargs["data"] for c in mock_post.call_args_list}
            assert sent["1"].get("disable_notification") == "true", "업무 시간 밖이면 알림 없이"
            assert "disable_notification" not in sent["2"], "하루 종일은 울림"
            assert "disable_notification" not in sent["999"], "명단에 없는 chat id는 울림"
            mock_post.reset_mock()

            # 4) 메시지 짓기
            text = "언론 모니터링 14시 기준\n- 메모\n\n<소제목>\nㅇ (KBS) 제목\nhttps://a.x\n"
            summary = "🤖 AI가 읽은 소제목별 주요 요약\n\n<소제목>\n요약 문장."
            people = [
                {"chat_id": "a", "regular_articles": True, "regular_summary": False},
                {"chat_id": "s", "regular_articles": False, "regular_summary": True},
                {"chat_id": "b", "regular_articles": True, "regular_summary": True},
            ]
            plan = {tuple(ids): msg for msg, ids in report_message.plan_messages(people, "regular", text, summary)}
            assert plan[("a",)] == text
            assert plan[("s",)] == "언론 모니터링 14시 기준\n- 메모\n\n" + summary + "\n", plan[("s",)]
            assert plan[("b",)].startswith(text.rstrip()) and plan[("b",)].rstrip().endswith("요약 문장."), "둘 다면 한 통"
            for label, kwargs in (("요약 없음", {"summary": ""}), ("AI 분류 실패", {"degraded": True})):
                args = {"summary": summary, "degraded": False, **kwargs}
                fallback = report_message.plan_messages(people[1:2], "regular", text, args["summary"], degraded=args["degraded"])
                assert fallback == [(text, ["s"])], f"{label}이면 요약만 받는 사람에게 기사 목록: {fallback}"
                assert report_message.email_message(text, args["summary"], args["degraded"]) == text, f"{label}이면 이메일은 기사 목록만"
            assert report_message.email_message(text, summary) == plan[("b",)], "이메일은 기사 목록 + 요약 한 통"
            from app.summarizer import summarize_group
            etc = {"name": "기타", "summary": "뒤섞인 AI 문단.", "articles": [{"title": f"제목{i}"} for i in range(7)]}
            assert summarize_group(etc) == "- 제목0\n- 제목1\n- 제목2\n- 제목3\n- 제목4\n외 2건", "「기타」 요약은 제목 목록"

            # 5) 정기 발송
            telegram_recipients.save_telegram_recipients([
                {"name": "기사", "chat_id": "10", "regular_articles": True},
                {"name": "요약", "chat_id": "11", "regular_summary": True},
                {"name": "안받음", "chat_id": "12", "adhoc_articles": True},
            ])
            run = {"run_slot": "14:00", "run_at": "2026-09-17T14:00:00", "articles": [], "send_count": 0,
                   "group": None}
            with patch.object(confirm_send, "latest_confirmed_report", return_value={"run": run, "text": text, "summary": summary}), \
                 patch.object(confirm_send, "run_looks_rule_based", return_value=False), \
                 patch.object(confirm_send, "active_recipient_emails", return_value=[]), \
                 patch.object(confirm_send, "record_send", side_effect=lambda r, auto=False: {**r, "send_count": 1}):
                _, ok = confirm_send.send_confirmed_run(run)
            assert ok, "정기 발송이 성공으로 기록돼야 함"
            by_id = {c.kwargs["data"]["chat_id"]: c.kwargs["data"]["text"] for c in mock_post.call_args_list}
            assert set(by_id) == {"10", "11"}, f"수시만 켠 사람은 정기를 안 받음: {set(by_id)}"
            assert "https://a.x" in by_id["10"] and "요약 문장" not in by_id["10"]
            assert "요약 문장" in by_id["11"] and "https://a.x" not in by_id["11"]
            mock_post.reset_mock()

            # 6) 수시 확정본 발송
            adhoc_dir = tmp_dir / "adhoc"
            for sub in ("cards", "trash", "undo"):
                (adhoc_dir / sub).mkdir(parents=True)
            with patch.object(card, "CARDS_DIR", adhoc_dir / "cards"), \
                 patch.object(card, "TRASH_DIR", adhoc_dir / "trash"), \
                 patch.object(card, "ISSUES_FILE", adhoc_dir / "issues.json"), \
                 patch.object(adhoc_undo, "UNDO_DIR", adhoc_dir / "undo"):
                telegram_recipients.save_telegram_recipients([
                    {"name": "수시기사", "chat_id": "20", "adhoc_articles": True},
                    {"name": "수시요약", "chat_id": "21", "adhoc_summary": True},
                    {"name": "뺄사람", "chat_id": "22", "adhoc_articles": True},
                ])
                bundle = card.new_bundle_card("인사청문회", basis_time="13:05")
                bundle["articles"] = [{
                    "outlet": "KBS", "title": "청문회 기사", "url": "https://b.x/1", "summary": "",
                    "pub_date": None, "group": "쟁점", "order": None, "hidden": False,
                    "added_at": "2026-09-17T13:00:00", "added_by": "manual",
                }]
                bundle["group_summaries"] = {"쟁점": "청문회 요약 문장."}
                card.save_card(bundle)

                page = adhoc_renderer.render_card_page(card.load_card(bundle["id"]))
                assert 'class="send-fab"' in page and "3명에게 발송" in page, "확정본엔 (발송)과 명단 확인창"

                # 발송 전에 되돌리기 기록을 하나 쌓아 둔다 — 나중에 되돌려도 발송 기록은 남아야 한다.
                adhoc_undo.push(bundle["id"], "기사 여러 건 숨기기", coalesce_sec=0)

                result = adhoc_send.send_bundle(bundle["id"], excluded_chat_ids={"22"})
                assert result == {"sent": 2, "failed": []}, result
                by_id = {c.kwargs["data"]["chat_id"]: c.kwargs["data"]["text"] for c in mock_post.call_args_list}
                assert set(by_id) == {"20", "21"}, f"확인창에서 뺀 사람은 안 받음: {set(by_id)}"
                assert "https://b.x/1" in by_id["20"] and "청문회 요약 문장" not in by_id["20"]
                assert "청문회 요약 문장" in by_id["21"] and "https://b.x/1" not in by_id["21"]
                assert by_id["21"].startswith("수시 모니터링"), "요약만 받아도 보고서 머리줄이 붙음"
                saved_card = card.load_card(bundle["id"])
                assert saved_card["send_count"] == 1 and saved_card["send_log"][-1]["to"] == ["수시기사", "수시요약"]
                assert "발송 완료" in adhoc_renderer.render_card_page(saved_card)

                adhoc_undo.undo(bundle["id"])
                assert card.load_card(bundle["id"])["send_count"] == 1, "되돌리기가 발송 기록을 지우면 안 됨"

                mock_post.reset_mock()
                adhoc_send.send_bundle(bundle["id"])
                texts = [c.kwargs["data"]["text"] for c in mock_post.call_args_list]
                assert len(texts) == 3 and all(t.startswith("(수정)수시 모니터링") for t in texts), texts[:1]

    print("30) 텔레그램 받는 것·알림 시간: 통과 (옛 기록 이관, 울리는 시간·자정 넘김, 조용히 보내기, 기사/요약/둘 다, 정기·수시 발송)")


def test_assigned_articles_keep_subheading_after_ai_members_hidden():
    """AI 첫 분류로 들어간 기사를 다 숨겨도, 그 소제목으로 배정된 기사가 보이면 소제목이 남는지 확인한다.

    [추가: 2026-09-17] 14:00 회차 「확대거시경제금융회의」 — 첫 분류 3건을 숨기자 소제목이
    사라져 「AI 기사 배정」으로 들어온 4건이 📂 미분류로 떨어지고 이름표도 없어졌다.
    """
    from app import classifier, llm_classifier

    def art(n):
        return {"url": f"https://example.com/{n}", "title": n, "summary": n}

    a1, a2, b1, b2, n1, n2 = (art(x) for x in ("a1", "a2", "b1", "b2", "n1", "n2"))
    round_id = ("2026-09-17", "14:00")
    everyone = [a1, a2, b1, b2, n1, n2]
    key = llm_classifier._cache_key(everyone, config.MAX_SUBHEADINGS, round_id)
    cache = {key: [
        ("금융시장 점검", [a1["url"], a2["url"]], "회의 요약"),
        ("청문회", [b1["url"], b2["url"], n1["url"], n2["url"]], "청문 요약"),
    ]}
    overrides = {n1["url"]: "금융시장 점검", n2["url"]: "금융시장 점검"}

    def names(visible):
        groups = classifier.classify_articles(
            visible, forced_groups=overrides, custom_group_names=[], round_id=round_id
        )
        return [(g["name"], [a["url"][-2:] for a in g["articles"]], g.get("summary")) for g in groups]

    with patch.object(llm_classifier, "_cache", cache), \
         patch.object(llm_classifier, "_ensure_cache_hydrated", lambda: None), \
         patch.object(llm_classifier, "is_configured", lambda: True), \
         patch.object(curation, "load_hidden_urls", lambda *a, **k: set()):
        got = names(everyone)
        assert got[0] == ("금융시장 점검", ["a1", "a2", "n1", "n2"], "회의 요약"), got
        # 첫 분류 기사(a1, a2)를 숨겨도 배정된 n1, n2가 원래 자리·요약 그대로 남는다
        got = names([b1, b2, n1, n2])
        assert got == [("금융시장 점검", ["n1", "n2"], "회의 요약"), ("청문회", ["b1", "b2"], "청문 요약")], got
        # 가리키는 기사까지 다 숨기면 그때 사라진다
        got = names([b1, b2])
        assert got == [("청문회", ["b1", "b2"], "청문 요약")], got


def test_cache_store_keeps_other_rounds():
    """캐시 파일에서 40개 넘게 불러온 뒤 새 분류를 넣어도, 앞 회차 분류가 메모리에 남는지 확인한다.

    [추가: 2026-09-17] 11:00 확정본 — 앱 재시작 뒤 14:00 초안 자동 분류가 캐시를 통째로
    비워, 초안에서 기사를 숨길 때 11:00 회차 소제목이 규칙 기반 이름(「후보자」·「부총리」)으로
    덮어써졌다.
    """
    from app import classifier, llm_classifier

    def art(n):
        return {"url": f"https://example.com/{n}", "title": n, "summary": n, "outlet": "연합뉴스"}

    old_round, new_round = ("2026-09-17", "11:00"), ("2026-09-17", "14:00")
    mine = [art(f"m{i}") for i in range(4)]
    entries = [
        {"round_id": ["2026-09-16", f"{h:02d}:00"], "urls": [f"https://example.com/old{h}-{i}" for i in range(2)],
         "max": config.MAX_SUBHEADINGS, "groups": [["옛 소제목", [f"https://example.com/old{h}-{i}" for i in range(2)], ""]]}
        for h in range(45)
    ]
    entries.append({"round_id": list(old_round), "urls": [a["url"] for a in mine], "max": config.MAX_SUBHEADINGS,
                    "groups": [["이형일 청문보고서 채택", [a["url"] for a in mine[:2]], ""],
                               ["미 금리인상 대응", [a["url"] for a in mine[2:]], ""]]})
    config.LLM_CLASSIFICATION_CACHE_FILE.write_text(json.dumps({"entries": entries}), encoding="utf-8")

    with patch.object(llm_classifier, "is_configured", lambda: True), \
         patch.object(curation, "load_hidden_urls", lambda *a, **k: set()):
        llm_classifier._ensure_cache_hydrated()
        # 다음 회차 초안의 새 분류가 캐시에 들어간다(API 대신 seed_cache로 같은 저장 경로를 탄다)
        fresh = [art("n1"), art("n2")]
        llm_classifier.seed_cache(fresh, [{"name": "새 쟁점", "articles": fresh}], round_id=new_round)
        assert len(llm_classifier._cache) <= llm_classifier._CACHE_MAX, len(llm_classifier._cache)
        # 앞 회차 확정본을 다시 저장해도 AI 이름이 그대로다(기사 하나 숨긴 상태)
        snap = classifier.snapshot_group_names(mine[1:], round_id=old_round)
        got = sorted({a["group"] for a in snap})
        assert got == ["미 금리인상 대응", "이형일 청문보고서 채택"], got
    on_disk = json.loads(config.LLM_CLASSIFICATION_CACHE_FILE.read_text(encoding="utf-8"))
    assert any(e["round_id"] == list(new_round) for e in on_disk["entries"])


def test_negative_guess_per_round():
    """홈 「부정 추정 기사」 — 회차마다 한 번, 「재경부」·「재정경제부」가 나온 정기 기사만 AI에 보낸다.

    AI 호출(_judge)만 가짜로 바꾸고, 회차 저장·대상 고르기·저장·홈 렌더링은 실제 코드를 돈다.
    """
    from app import negative_guess

    today = datetime.now().strftime("%Y-%m-%d")

    def art(n, title, summary="요약", group=None, hh="08:30"):
        return {"outlet": "연합뉴스", "title": title, "url": f"https://n.news.naver.com/{n}",
                "summary": summary, "pub_date": f"{today}T{hh}:00+09:00", "group": group}

    storage.save_run("09:30", [
        art(1, "재경부, 유류세 인하 두 달 연장", group="유류세 연장"),
        art(2, "주유소업계 반발", summary="재정경제부의 고속도로 할인에 업계가 반발했다", group="주유소 반발"),
        art(3, "[포토] 회의 주재하는 재경부 장관"),
        art(4, "반도체 수출 호조"),                       # 두 단어가 없다 → 안 보낸다
    ], run_at=f"{today}T09:30:05")

    sent = []
    fail_next = {"on": False}

    def fake_judge(articles):
        sent.append([a["title"] for a in articles])
        if fail_next["on"]:
            fail_next["on"] = False
            return None
        return [{"i": i + 1, "reason": "업계가 「불공정」 반발"} for i, a in enumerate(articles) if "반발" in a["title"]]

    with patch.object(negative_guess, "_judge", fake_judge), \
         patch.object(negative_guess, "llm_is_configured", lambda: True), \
         patch.object(negative_guess, "_attempts", {}):
        card = landing_renderer._board_card_html({}, storage.load_today_runs(today))
        assert "판정 중" in card, "판정 전 안내가 없다"

        negative_guess.negative_guess_tick(datetime.now(), background=False)
        assert sent == [["재경부, 유류세 인하 두 달 연장", "주유소업계 반발"]], f"대상 고르기가 틀렸다(사진·무관 기사는 빠져야): {sent}"
        state = negative_guess.load_state(today)
        assert list(state["runs"]) == ["09:30"] and list(state["items"]) == ["https://n.news.naver.com/2"], state

        negative_guess.negative_guess_tick(datetime.now(), background=False)
        assert len(sent) == 1, "판정한 회차를 또 보냈다"

        runs = storage.load_today_runs(today)
        card = landing_renderer._board_card_html({}, runs)
        assert "부정 추정 기사 <span class=\"cnt\">1건</span>" in card and "09:30 회차까지" in card, "카드 숫자·회차가 없다"
        assert 'class="nw"' not in card and 'class="dl"' not in card, "첫 회차엔 new·+N을 달지 않는다"
        assert 'class="grp"' not in card, "부정 추정 기사 목록엔 소제목을 달지 않는다"
        assert 'data-copy="ㅇ (연합뉴스) 주유소업계 반발\nhttps://n.news.naver.com/2"' in card, "복사 텍스트가 보고서 형식이 아니다"

        # 두 번째 회차 — 한 번 실패하면 기록하지 않고, 5분 뒤 다시 시도한다
        storage.save_run("11:00", [art(5, "재경부 세제 개편에 업계 반발", hh="10:40")], run_at=f"{today}T11:00:05")
        fail_next["on"] = True
        t0 = datetime.now()
        negative_guess.negative_guess_tick(t0, background=False)
        assert "11:00" not in negative_guess.load_state(today)["runs"], "실패한 회차를 판정했다고 적었다"
        negative_guess.negative_guess_tick(t0 + timedelta(minutes=2), background=False)
        assert len(sent) == 2, "5분 안에 다시 시도했다"
        negative_guess.negative_guess_tick(t0 + timedelta(minutes=6), background=False)
        assert sent[-1] == ["재경부 세제 개편에 업계 반발"], sent[-1]

        card = landing_renderer._board_card_html({}, storage.load_today_runs(today))
        assert "11:00 회차까지" in card and ">+1<" in card and card.count('class="nw">new<') == 1, "두 번째 회차의 +N·new가 없다"

    assert negative_guess.load_state("2000-01-01")["items"] == {}, "다른 날짜는 비어야 한다"
    print("37) 부정 추정 기사: 통과 (두 단어 나온 정기 기사만, 사진 제외, 회차마다 한 번, 실패 재시도, 첫 회차엔 new 없음)")


def test_breaking_alert_same_article_once():
    """같은 [속보]가 여러 키워드에 걸려 한 묶음에 여러 번 들어와도 한 번만 보내고 한 번만 기록한다.

    2026-09-18 실측: 같은 속보가 알림 기록에 3번 쌓여 홈 [속보] 목록·건수가 부풀었고, 텔레그램
    메시지에도 같은 기사가 여러 번 실렸다. 고치기 전 기록(중복 포함)도 홈은 한 번만 보여 줘야 한다.
    """
    art = {"outlet": "부산일보", "title": "[속보] 유류세 인하 두 달 연장", "url": "https://n.news.naver.com/082/1",
           "pub_date": "2026-09-18T08:05:00+09:00"}
    other = {"outlet": "매일경제", "title": "[속보] 유류세 인하 연장", "url": "https://n.news.naver.com/009/2",
             "pub_date": "2026-09-18T08:04:00+09:00"}
    sent = []
    with patch.object(breaking_alert_sender, "_send_batch", lambda kind, arts, *_a: sent.append([a["url"] for a in arts])):
        n = breaking_alert_sender.detect_and_alert([art, other, dict(art), dict(art), dict(other)])
    assert n == 2 and sent == [[art["url"], other["url"]]], f"같은 기사를 여러 번 보냈다: {sent}"
    items = alerted_urls.alerted_items()
    assert [it["url"] for it in items] == [other["url"], art["url"]], f"기록에 중복이 남았다: {items}"

    # 고치기 전에 쌓인 중복 기록도 홈 목록에선 한 번만
    data = json.loads(alerted_urls.ALERTED_URLS_FILE.read_text(encoding="utf-8"))
    data["items"] += [dict(data["items"][0]), dict(data["items"][0])]
    alerted_urls.ALERTED_URLS_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    assert len(alerted_urls.alerted_items()) == 2, "옛 중복 기록이 홈 목록에 그대로 나온다"
    card = landing_renderer._alert_line_html()
    assert card.count(f'href="{art["url"]}"') == 1 and '[속보] <span class="n">2</span>' in card, "홈 [속보] 칩 건수·목록이 중복을 센다"
    print("38) [단독]·[속보] 같은 기사 한 번만: 통과 (한 묶음 안 중복, 기록 중복, 옛 중복 기록 표시)")


def test_add_article_by_url():
    """"+ 수기로 기사 추가"(초안) — 주소 하나로 언론사·제목·요약·발행시각을 원문에서 읽는다.

    실측(2026-09-21, 저장된 회차의 pub_date를 정답으로 대조)이 고른 후보를 그대로 고정한다:
    네이버 미러는 data-date-time(30/30 정확), 언론사 자체 도메인은 article:published_time.
    이 테스트는 네트워크를 타지 않고 _fetch_page_html만 가짜로 바꾼다.

      1) 네이버 미러 — data-date-time을 KST ISO로 읽고, oid로 언론사를 확정한다
      2) 자체 도메인 — article:published_time(오프셋 포함)을 그대로 읽는다
      3) oid가 표에 없는 네이버 링크 — 도메인 추정이 "n.news.naver.com"을 내놓으므로
         og:article:author("{언론사} | 네이버")로 폴백한다
      4) 시각 후보가 없으면 pub_date 키 자체를 안 만든다(지어내지 않는다)
      5) 제목을 못 읽으면 None — 제목 없이는 보고서 한 줄을 세울 수 없다
    """
    naver_page = (
        '<meta property="og:title" content="유류세 인하 두 달 연장">'
        '<meta property="og:description" content="정부가 유류세 인하를 연장하기로 했다.">'
        '<meta property="og:article:author" content="이데일리 | 네이버">'
        '<span class="media_end_head_info_datestamp_time" data-date-time="2026-09-21 08:05:12">'
        "<title>유류세 인하 두 달 연장</title>"
    )
    # 018 = 이데일리(NAVER_OID_OUTLETS). oid가 표에 있으면 페이지를 안 보고 확정한다.
    with patch.object(naver_api, "_fetch_page_html", return_value=naver_page):
        got = naver_api.fetch_article_by_url("https://n.news.naver.com/mnews/article/018/0006373273")
    assert got["outlet"] == "이데일리", f"oid로 언론사를 확정하지 못했다: {got['outlet']}"
    assert got["pub_date"] == "2026-09-21T08:05:12+09:00", f"발행시각을 잘못 읽었다: {got['pub_date']}"
    assert got["title"] == "유류세 인하 두 달 연장" and got["summary"].startswith("정부가"), got

    own_page = (
        '<meta property="og:title" content="예산안 심사 시작">'
        '<meta property="article:published_time" content="2026-09-21T16:25:00+09:00">'
    )
    with patch.object(naver_api, "_fetch_page_html", return_value=own_page):
        got = naver_api.fetch_article_by_url("https://www.newspim.com/news/view/20260921000123")
    assert got["pub_date"] == "2026-09-21T16:25:00+09:00", f"자체 도메인 시각: {got['pub_date']}"
    assert got["outlet"] == "뉴스핌", f"도메인 추정이 안 됐다: {got['outlet']}"

    # oid 999는 표에 없다 — 도메인 추정은 "n.news.naver.com"이라 쓸모가 없으므로 페이지를 본다
    unknown_oid = (
        '<meta property="og:title" content="새 매체 기사">'
        '<meta property="og:article:author" content="처음보는신문 | 네이버">'
        '<span data-date-time="2026-09-21 09:00:00">'
    )
    with patch.object(naver_api, "_fetch_page_html", return_value=unknown_oid):
        got = naver_api.fetch_article_by_url("https://n.news.naver.com/mnews/article/999/0000000001")
    assert got["outlet"] == "처음보는신문", f"og:article:author 폴백이 안 됐다: {got['outlet']}"

    no_time = '<meta property="og:title" content="시각 없는 기사">'
    with patch.object(naver_api, "_fetch_page_html", return_value=no_time):
        got = naver_api.fetch_article_by_url("https://example.com/a")
    assert "pub_date" not in got, f"없는 발행시각을 지어냈다: {got}"

    with patch.object(naver_api, "_fetch_page_html", return_value="<p>제목이 없는 페이지</p>"):
        assert naver_api.fetch_article_by_url("https://example.com/b") is None, "제목 없이 기사를 만들었다"
    with patch.object(naver_api, "_fetch_page_html", return_value=None):
        assert naver_api.fetch_article_by_url("https://example.com/c") is None, "접속 실패를 기사로 만들었다"

    # 6) 블로그·카페·커뮤니티 주소는 호스트로 거른다(하위 도메인·모바일 포함). 언론사 자체
    #    도메인은 이름을 못 찾아도 통과해야 한다 — 이름으로 거르면 이투데이 등 실제 기사가 막혔다.
    for bad in ("https://blog.naver.com/a/1", "https://m.blog.naver.com/a/1",
                "https://cafe.naver.com/x/2", "https://abc.tistory.com/3", "https://gall.dcinside.com/b"):
        assert naver_api.is_non_news_url(bad), f"기사가 아닌 주소를 통과시켰다: {bad}"
    for good in ("https://n.news.naver.com/mnews/article/018/0006373273",
                 "https://www.etoday.co.kr/news/view/2627918", "https://www.x.com.kr/a"):
        assert not naver_api.is_non_news_url(good), f"언론사 주소를 막았다: {good}"

    # 7) 입구는 📌 칸 안 — 담아둔 기사가 0건이어도 칸이 한 줄로 남아 입구가 사라지지 않는다.
    from app.renderer import _render_manual_section
    assert _render_manual_section([], [], "{title}") == "", "입구 없는 칸이 0건에 그려졌다"
    empty = _render_manual_section([], [], "{title}", target="draft", add_button_html="<b>IN</b>")
    assert "manual-zone" in empty and "<b>IN</b>" in empty and "AI 기사 배정" not in empty, empty

    # 8) 수기로 넣은 기사는 칸 맨 위(at_top), 실시간 📌 담아두기는 맨 뒤.
    from app import manual_articles as ma
    with tempfile.TemporaryDirectory() as tmp, patch.object(ma, "MANUAL_ARTICLES_FILE", Path(tmp) / "m.json"):
        ma.add_manual_article({"url": "u1", "title": "a"})
        ma.add_manual_article({"url": "u2", "title": "b"})
        ma.add_manual_article({"url": "u3", "title": "c"}, at_top=True)
        assert [a["url"] for a in ma.load_manual_articles()] == ["u3", "u1", "u2"], ma.load_manual_articles()

    # 9) 붙여 넣은 네이버 주소는 수집이 저장하는 모양으로 맞춘다 — 안 맞추면 중복 대조를 빠져나간다.
    canon = "https://n.news.naver.com/mnews/article/021/0002819384"
    for pasted in (canon + "?sid=101", "https://n.news.naver.com/article/021/0002819384?cds=news_media_pc",
                   "https://m.news.naver.com/article/021/0002819384", "http://n.news.naver.com/mnews/article/021/0002819384#a",
                   "https://news.naver.com/main/read.naver?mode=LSD&sid1=101&oid=021&aid=0002819384"):
        assert naver_api.normalize_article_url(pasted) == canon, pasted
    own = "https://www.newspim.com/news/view/20260921000123?x=1"
    assert naver_api.normalize_article_url(own) == own, "언론사 자체 주소의 쿼리를 건드렸다"

    # 10) 중복 판정 — 초안은 「지금 초안」, 확정본은 저장된 회차와 대조하고, 오늘 앞 회차·숨긴 기사도 막는다.
    from app import settings_server as ss
    handler = object.__new__(ss._SettingsHandler)
    replies = []
    handler._respond_json = replies.append
    handler._regenerate_screens = lambda: None
    fetched = {"outlet": "경향신문", "title": "t", "url": canon, "summary": ""}

    def add(url, screen, *, run_urls=(), draft_urls=(), published=(), hidden=(), index=None, manual=(), got=None):
        replies.clear()
        with patch.object(ss, "_original_url_index", return_value=dict(index or {})), \
             patch.object(ss, "load_latest_run", return_value={"articles": [{"url": u} for u in run_urls]}), \
             patch.object(ss, "current_draft_urls", return_value=set(draft_urls)), \
             patch.object(ss, "_already_published_urls", return_value=set(published) | set(run_urls)), \
             patch.object(ss, "load_hidden_urls", return_value=set(hidden)), \
             patch.object(ss, "load_manual_articles", return_value=[{"url": u} for u in manual]), \
             patch.object(ss, "fetch_article_by_url", return_value=dict(got or fetched)), \
             patch.object(ss, "add_manual_article", return_value=True):
            handler._handle_add_article_by_url({"url": [url], "screen": [screen]})
        return replies[-1]

    pasted = canon + "?sid=101"
    assert add(pasted, "confirmed", run_urls=[canon])["reason"] == "이미 이 회차에 있는 기사입니다."
    # 초안: 지금 초안에 보이는 기사는 막고, 저장된 최신 회차(=앞 회차) 기사는 「앞 회차」로 말한다
    assert add(pasted, "preview", draft_urls=[canon])["reason"] == "이미 이 회차에 있는 기사입니다."
    assert add(pasted, "preview", run_urls=[canon])["reason"] == "이미 앞 회차에 실린 기사입니다."
    assert add(pasted, "confirmed", published=[canon])["reason"] == "이미 앞 회차에 실린 기사입니다."
    assert add(pasted, "preview", hidden=[canon])["reason"] == "숨긴 기사입니다."
    ok = add(pasted, "preview", run_urls=["https://other"])
    assert ok == {"ok": True, "url": canon}, f"겹치지 않는 기사를 막았거나 정리 안 된 주소를 돌려줬다: {ok}"

    # 11) 언론사 원문 주소로 넣어도 같은 기사를 알아본다 — 수집이 적어 둔 original_url(네이버 API
    #     originallink)과 대조해 네이버 주소로 바꿔 읽는다. 네이버 쪽엔 utm_* 꼬리가 붙는다.
    orig = "https://www.chosun.com/politics/assembly/2026/09/22/HEOXHFQTKBFJJEUGSSJRTDYHZQ/"
    idx = {k: canon for k in naver_api.original_url_keys(orig + "?utm_source=naver&utm_medium=referral")}
    assert add(orig, "confirmed", run_urls=[canon], index=idx)["reason"] == "이미 이 회차에 있는 기사입니다."
    assert add(orig, "preview", hidden=[canon], index=idx)["reason"] == "숨긴 기사입니다."
    # 기사 번호가 쿼리에 있는 주소는 쿼리가 다르면 다른 기사다(경로만으로 합치지 않는다)
    kbs = "https://news.kbs.co.kr/news/pc/view/view.do?ncd=8669419&ref=A"
    kbs_idx = {k: canon for k in naver_api.original_url_keys(kbs)}
    assert add(kbs, "confirmed", run_urls=[canon], index=kbs_idx)["reason"] == "이미 이 회차에 있는 기사입니다."
    other = add("https://news.kbs.co.kr/news/pc/view/view.do?ncd=8669420", "confirmed", run_urls=[canon], index=kbs_idx)
    assert other["ok"], f"기사 번호가 다른 KBS 기사를 같은 기사로 봤다: {other}"
    assert not (naver_api.original_url_keys("https://www.x.co.kr/news/articleView.html?idxno=1")
                & naver_api.original_url_keys("https://www.x.co.kr/news/articleView.html?idxno=2"))
    # 경로가 기사를 가리키면 꼬리(input=…)가 달라도 같은 기사
    assert (naver_api.original_url_keys("https://www.yna.co.kr/view/AKR20260922046300002?input=1195m")
            & naver_api.original_url_keys("https://m.yna.co.kr/view/AKR20260922046300002?section=economy"))
    # 반대 방향 — 네이버 주소로 넣었는데 같은 기사를 원문 주소로 이미 담아 둔 경우
    dup = add(canon, "preview", manual=[orig], got=dict(fetched, original_url=orig + "?utm_source=naver"))
    assert dup["reason"] == "이미 담아둔 기사입니다.", dup
    # 네이버 페이지의 「기사원문」 링크를 original_url로 읽는다
    with patch.object(naver_api, "_fetch_page_html", return_value=naver_page +
                      '<a href="https://www.edaily.co.kr/News/Read?newsId=01&amp;mediaCodeNo=257" target="_blank" class="media_end_head_origin_link">'):
        got = naver_api.fetch_article_by_url("https://n.news.naver.com/mnews/article/018/0006373273")
    assert got["original_url"] == "https://www.edaily.co.kr/News/Read?newsId=01&mediaCodeNo=257", got

    print("39) + 수기로 기사 추가: 통과 (네이버 data-date-time·자체 도메인 published_time, oid 폴백, 없는 시각은 안 지어냄, 주소 정리·중복 판정·원문 주소로 넣어도 같은 기사)")


def test_keyword_note_history_and_time_select():
    """키워드 메모 「지난 회차 메모」 참고 목록 + 수시 시간 드롭다운.

      1) 이 회차보다 앞 회차 메모만, 메모 있는 최근 3개 날짜, 같은 날 같은 문구는 한 줄로
      2) 단어마다 data-w(앞뒤 문장부호 뗀 값) — 화면이 치는 단어와 똑같은 것만 파랗게 칠한다
      3) 시간 드롭다운 — 숨은 입력이 폼 값, 목록에 없는 분은 제자리에 끼운다
    """
    from app import manual_keyword_note as mkn
    from app.renderer import keyword_note_history_html
    notes = {
        "2026-09-18|11:00": "옛 메모",
        "2026-09-19|11:00": "9/19 메모",
        "2026-09-21|14:00": "부총리 후보자 청문회, 세제개편 등",
        "2026-09-21|17:00": "부총리 후보자 청문회, 세제개편 등",
        "2026-09-22|11:00": "유류세 인하 연장",
        "2026-09-22|14:00": "지금 회차 메모",
        "2026-09-22|17:00": "뒤 회차 메모",
    }
    with tempfile.TemporaryDirectory() as tmp, patch.object(mkn, "MANUAL_KEYWORD_NOTE_FILE", Path(tmp) / "n.json"):
        (Path(tmp) / "n.json").write_text(json.dumps({"notes": notes}, ensure_ascii=False), encoding="utf-8")
        rows = mkn.past_notes(("2026-09-22", "14:00"))
        assert rows == [
            ("2026-09-22", ["11:00"], "유류세 인하 연장"),
            ("2026-09-21", ["14:00", "17:00"], "부총리 후보자 청문회, 세제개편 등"),
        ], rows
        # 달력상 어제(9/19·9/20)가 비어도 메모가 있는 가장 최근 하루를 가져온다
        rows_mon = mkn.past_notes(("2026-09-21", "11:00"))
        assert rows_mon == [("2026-09-19", ["11:00"], "9/19 메모")], rows_mon
        html_out = keyword_note_history_html(("2026-09-22", "14:00"))
    assert 'data-w="청문회"' in html_out and ">청문회,</span>" in html_out, "문장부호를 뗀 비교값이 없다"
    assert "9/19 메모" not in html_out, "직전 하루보다 더 거슬러 올라갔다"
    assert "지금 회차 메모" not in html_out and "뒤 회차 메모" not in html_out, "지금·뒤 회차 메모가 섞였다"
    assert "<button" not in html_out and "onclick" not in html_out, "참고 목록인데 누르는 줄이 생겼다"
    assert "9/21 14:00·17:00" in html_out, html_out

    from app.adhoc.renderer import _hm_select_html
    sel = _hm_select_html("win-end", "window_end", "14:56", "14:56")
    assert 'type="hidden" id="win-end" name="window_end" value="14:56"' in sel, sel
    assert '<option value="56" selected>56</option>' in sel and '<option value="14" selected>' in sel, sel
    assert '<option value="15"' not in sel, "지나지 않은 시각이 목록에 있다"
    assert sel.index('value="50"') < sel.index('value="56"'), "목록에 없는 분이 제자리에 안 들어갔다"
    print("41) 지난 회차 메모 · 수시 시간 드롭다운: 통과 (앞 회차만·오늘+직전 하루·같은 문구 합침·비교값, 숨은 입력·분 끼우기)")


def test_telegram_bot_name():
    """텔레그램 봇 이름(mockups/BOT_NAME_MOCKUP.html).

      1) 앱이 이름을 건 적 없는 봇이면 켤 때 기본 이름을 한 번 건다 — 그 뒤로는 안 건다
         (BotFather에서 바꾼 이름을 덮어쓰지 않는다)
      2) 이미 기본 이름이면 부르지 않고 기록만
      3) 비운 이름은 기본 이름, 64자 초과는 자른다
      4) 429면 기다릴 시간을 문구로 돌려주고 기록을 안 남긴다
    """
    from app import telegram_bot_name as tbn

    def resp(ok=True, status=200, body=None):
        r = MagicMock(ok=ok, status_code=status, text=json.dumps(body or {}))
        r.json.return_value = body or {}
        return r

    with tempfile.TemporaryDirectory() as tmp:
        record_file = Path(tmp) / "telegram_bot_name.json"
        with patch.object(tbn, "TELEGRAM_BOT_NAME_FILE", record_file), \
             patch.object(credentials, "telegram_bot_token", lambda: "12345:fake"), \
             patch.object(tbn.requests, "get") as mock_get, \
             patch.object(tbn.requests, "post") as mock_post:
            # 1)
            mock_get.return_value = resp(body={"ok": True, "result": {"name": "재정경제부 스크랩봇"}})
            mock_post.return_value = resp(body={"ok": True, "result": True})
            tbn.apply_default_bot_name_once()
            assert mock_post.call_count == 1
            assert mock_post.call_args.kwargs["data"] == {"name": config.DEFAULT_TELEGRAM_BOT_NAME}
            assert json.loads(record_file.read_text(encoding="utf-8"))["12345"]["name"] == config.DEFAULT_TELEGRAM_BOT_NAME
            mock_get.return_value = resp(body={"ok": True, "result": {"name": "담당자가 바꾼 이름"}})
            tbn.apply_default_bot_name_once()
            assert mock_post.call_count == 1, "한 번 건 봇은 다시 안 건다"

            # 2)
            record_file.unlink()
            mock_get.return_value = resp(body={"ok": True, "result": {"name": config.DEFAULT_TELEGRAM_BOT_NAME}})
            tbn.apply_default_bot_name_once()
            assert mock_post.call_count == 1 and record_file.exists()

            # 3)
            assert tbn.normalize_bot_name("   ") == config.DEFAULT_TELEGRAM_BOT_NAME
            assert len(tbn.normalize_bot_name("가" * 80)) == tbn.MAX_BOT_NAME_LEN

            # 4)
            record_file.unlink()
            mock_post.return_value = resp(ok=False, status=429, body={"ok": False, "parameters": {"retry_after": 1380}})
            # 일부러 만든 429라 경고 로그를 가로챈다 — 터미널에 실제 실패처럼 찍히지 않게 하고,
            # 「실패하면 경고를 남긴다」도 같이 확인한다.
            with patch.object(tbn.logger, "warning") as mock_warning:
                error = tbn.set_bot_name("새 이름")
            assert mock_warning.call_count == 1 and mock_warning.call_args.args[1] == 429
            assert error and "23분" in error, error
            assert not record_file.exists(), "실패하면 기록을 안 남긴다(다음에 켤 때 다시 시도)"


def test_draft_all_first_classified_hidden_shows_unclassified():
    """초안 첫 분류의 기사를 전부 숨기면, 규칙 기반 대신 남은 기사를 전부 📂 미분류로 보여주는지 확인한다.

    [추가: 2026-09-21] 17:00 회차 — 자동 분류가 6건일 때 쓰였고 그 6건을 다 숨기자 초안이
    단어 빈도 소제목으로 떨어졌다. 마감 때는 전부 미분류면 처음부터 분류해야 한다.
    """
    from app import classifier, llm_classifier

    def art(n):
        return {"url": f"https://example.com/{n}", "title": n, "summary": n}

    old = [art(f"o{i}") for i in range(3)]
    new = [art(f"n{i}") for i in range(4)]
    round_id = ("2026-09-21", "17:00")
    key = llm_classifier._cache_key(old, config.MAX_SUBHEADINGS, round_id)
    cache = {key: [("메가특구 사회적 대화", [a["url"] for a in old], "")]}
    hidden = {a["url"] for a in old}

    with patch.object(llm_classifier, "_cache", cache), \
         patch.object(llm_classifier, "_ensure_cache_hydrated", lambda: None), \
         patch.object(llm_classifier, "is_configured", lambda: True), \
         patch.object(curation, "load_hidden_urls", lambda *a, **k: hidden):
        groups = classifier.classify_articles(new, round_id=round_id)
        assert [g["name"] for g in groups] == [llm_classifier.UNCLASSIFIED_GROUP_NAME], groups
        assert len(groups[0]["articles"]) == 4
        assert not llm_classifier.last_classification_was_rule_based()
        # 다른 회차에선 이 규칙을 안 쓴다(전 회차 캐시로 미분류를 만들지 않는다)
        assert classifier.classify_articles(new, round_id=("2026-09-21", "14:00"))[0]["name"] != llm_classifier.UNCLASSIFIED_GROUP_NAME
        # 마감은 전부 미분류를 받으면 처음부터 분류한다(여기선 API 대신 호출 여부만 본다)
        calls = []
        real = classifier.classify_articles
        with patch.object(classifier, "classify_articles", lambda *a, **k: calls.append(k) or real(*a, **{**k, "allow_llm_call": False})):
            classifier.classify_for_finalize(new, round_id=round_id)
        assert calls and calls[0].get("allow_llm_call") is True, calls


def test_today_cut_round():
    """「✂ 오늘만 여기서 끊기」 — 시간표 끼우기·곧바로 확정본·예약·취소·기록 이어 쓰기.

    지키는 것:
      1) 끊은 시각이 그 날짜의 시간표에만 끼워진다(다른 날짜·설정 밖 시각은 무시)
      2) 지난 시각으로 끊으면 네이버를 다시 검색하지 않고 초안 캐시 기사로 곧바로 저장한다 —
         끊은 시각 뒤에 게시된 기사는 빠진다
      3) 끊는 순간 원래 회차 이름의 기록(이름표·메모·초안에 보인 기사)이 새 회차로 넘어간다
      4) 앞 시각이면 예약만 하고, 취소하면 그동안 다듬은 기록이 원래 회차로 돌아간다
      5) 이미 확정본이 된 끊기는 취소할 수 없다 · 회차 밖 시각은 거절한다
      6) 예약 회차가 수집되면 기록이 이어지는 회차로 넘어간다(after_round_collected)
    """
    from datetime import time as dt_time
    from app import cut_round, group_order, manual_keyword_note, today_cuts, undo

    _require_isolation()
    tmp = Path(tempfile.mkdtemp())
    today = datetime.now().date()
    today_s = today.isoformat()
    windows = [
        {"start": "11:00", "end": "14:00", "enabled": True},
        {"start": "14:00", "end": "17:00", "enabled": True},
    ]
    labels_file = tmp / "group_labels.json"
    with patch.object(curation, "GROUP_LABELS_FILE", labels_file), \
         patch.object(group_order, "GROUP_ORDER_FILE", tmp / "group_order.json"), \
         patch.object(manual_keyword_note, "MANUAL_KEYWORD_NOTE_FILE", tmp / "memo.json"), \
         patch.object(undo, "UNDO_FILE", tmp / "undo.json"):
        # 1) 시간표 끼우기
        today_cuts.add_cut(today_s, "15:57", "17:00")
        cut_windows = [(w["start"], w["end"]) for w in today_cuts.apply_cuts(windows, today)]
        assert cut_windows == [("11:00", "14:00"), ("14:00", "15:57"), ("15:57", "17:00")], cut_windows
        yesterday = today - timedelta(days=1)
        assert today_cuts.apply_cuts(windows, yesterday) == windows, "어제 시간표에 오늘 끊기가 끼워짐"
        today_cuts.add_cut(today_s, "18:00", "")
        assert len(today_cuts.apply_cuts(windows, today)) == 3, "어느 칸에도 안 들어가는 끊기가 끼워짐"
        today_cuts.remove_cut(today_s, "18:00")
        today_cuts.remove_cut(today_s, "15:57")
        assert today_cuts.apply_cuts(windows, today) == windows

        # 설정: 켜진 그룹 하나(요일 무관)
        st = settings.load_settings()
        st["schedule_groups"] = [{"name": "주중", "times": windows, "enabled": True,
                                  "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]}]
        settings.SETTINGS_FILE.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
        st = settings.load_settings()

        # 초안 캐시: 14:00~17:00 회차가 들고 있는 기사 셋
        slot = {"start": "14:00", "end": "17:00", "enabled": True}
        groups = settings.active_search_groups(
            st, [g for g in st.get("keyword_groups", []) if settings.group_in_scrap(g)]
        )
        pool = [
            {**_article("끊기-1"), "pub_date": f"{today_s}T14:10:00+09:00"},
            {**_article("끊기-2"), "pub_date": f"{today_s}T15:20:00+09:00"},
            {**_article("끊기-3"), "pub_date": f"{today_s}T15:50:00+09:00"},
        ]
        preview_cache.save_preview_cache(
            preview_renderer._preview_keywords_signature(groups, slot), pool[-1]["pub_date"], pool
        )
        # 원래 회차(17:00) 이름으로 다듬어 둔 것
        labels_file.write_text(json.dumps({f"{today_s}|17:00": {"AI 이름": "담당자 이름"}}, ensure_ascii=False))
        manual_keyword_note.save_manual_keyword_note("오늘 메모", (today_s, "17:00"))
        draft_seen.record_draft_seen((today_s, "17:00"), draft_seen.search_condition(groups), [pool[0]])

        def no_search(*a, **k):
            raise AssertionError("지난 시각 끊기인데 네이버를 다시 검색했음 — 초안 기사로 저장해야 한다")

        def fake_finalize(articles, *a, **k):
            return [{"name": "AI 이름", "articles": articles, "summary": ""}], {}, []

        # 화면을 다시 그리는 순간(confirm_and_promote)에 끊기가 이미 적혀 있어야 정기 보관함에 ✂가 붙는다.
        promoted = MagicMock(side_effect=lambda run: seen_at_render.append(today_cuts.cut_origin(today_s, "15:30")))
        seen_at_render = []
        now = datetime.combine(today, dt_time(15, 57))
        # 2) 지난 시각(15:30)으로 끊기 → 곧바로 확정본
        with patch.object(scraper, "search_articles_by_groups", side_effect=no_search), \
             patch.object(scraper, "classify_for_finalize", side_effect=fake_finalize), \
             patch.object(confirm_send, "confirm_and_promote", promoted):
            result = cut_round.cut_round("15:30", now=now)
        assert result == {"mode": "done", "end": "15:30", "from": "17:00"}, result
        assert promoted.called, "끊은 회차를 확정본으로 반영하지 않았음"
        assert seen_at_render == ["17:00"], f"화면을 그린 뒤에 끊기를 적었음 — 보관함 ✂ 표식이 빠진다: {seen_at_render}"
        run = storage.load_latest_run()
        assert run and run["run_slot"] == "15:30", f"15:30 회차가 저장되지 않음: {run and run.get('run_slot')}"
        saved = {a["url"] for a in run["articles"]}
        assert saved == {pool[0]["url"], pool[1]["url"]}, f"끊은 시각 뒤 기사가 섞였거나 빠졌음: {saved}"
        times = [(w["start"], w["end"]) for w in settings.active_schedule_times(st, now)]
        assert ("14:00", "15:30") in times and ("15:30", "17:00") in times, times
        assert settings.active_schedule_times(st, now, with_cuts=False) == windows, "설정 시간표가 바뀌었음"

        # 3) 기록이 새 회차 이름으로 넘어갔나
        labels = json.loads(labels_file.read_text())
        assert labels.get(f"{today_s}|15:30") == {"AI 이름": "담당자 이름"}, "이름표가 끊은 회차로 안 넘어감"
        assert labels.get(f"{today_s}|17:00"), "원래 회차 이름표가 지워짐(복사여야 한다)"
        assert manual_keyword_note.load_manual_keyword_note((today_s, "15:30")) == "오늘 메모"
        assert manual_keyword_note.load_manual_keyword_note((today_s, "17:00")) == "오늘 메모"
        seen = json.loads(draft_seen.DRAFT_SEEN_FILE.read_text())
        assert seen["round_id"] == [today_s, "15:30"], f"초안에 보인 기사 목록이 안 옮겨짐: {seen['round_id']}"
        assert today_cuts.cut_origin(today_s, "15:30") == "17:00"

        # 4) 앞 시각(16:30) 예약 → 취소
        now2 = datetime.combine(today, dt_time(15, 40))
        result = cut_round.cut_round("16:30", now=now2)
        assert result["mode"] == "pending", result
        assert cut_round.cut_state(now2)["pending"] == "16:30"
        assert json.loads(labels_file.read_text()).get(f"{today_s}|16:30"), "예약 회차로 이름표가 안 넘어감"
        # 예약해 둔 동안 다듬은 이름표는 취소하면 원래 회차로 돌아가야 한다
        data = json.loads(labels_file.read_text())
        data[f"{today_s}|16:30"] = {"AI 이름": "예약 중 고친 이름"}
        labels_file.write_text(json.dumps(data, ensure_ascii=False))
        cut_round.cancel_cut("16:30", now=now2)
        assert today_cuts.cut_origin(today_s, "16:30") is None, "취소했는데 끊기가 남음"
        assert json.loads(labels_file.read_text())[f"{today_s}|17:00"] == {"AI 이름": "예약 중 고친 이름"}, (
            "예약 중에 고친 이름표가 원래 회차로 안 돌아감"
        )

        # 5) 거절
        for bad, why in (("15:30", "이미 확정본이 된 끊기"), ("18:00", "회차 밖 시각")):
            try:
                if why.startswith("이미"):
                    cut_round.cancel_cut(bad, now=now2)
                else:
                    cut_round.cut_round(bad, now=now2)
            except cut_round.CutError:
                pass
            else:
                raise AssertionError(f"{why}({bad})를 거절하지 않았음")

        # 6) 예약 회차가 제시각에 수집되면 기록이 이어지는 회차로
        today_cuts.add_cut(today_s, "16:45", "17:00")
        data = json.loads(labels_file.read_text())
        data[f"{today_s}|16:45"] = {"AI 이름": "끊는 회차에서 고친 이름"}
        labels_file.write_text(json.dumps(data, ensure_ascii=False))
        today_cuts.after_round_collected({"run_at": f"{today_s}T16:45:05", "run_slot": "16:45"})
        assert json.loads(labels_file.read_text())[f"{today_s}|17:00"] == {"AI 이름": "끊는 회차에서 고친 이름"}

    print("오늘만 회차 끊기: 통과 (시간표 끼우기·초안 기사로 즉시 저장·예약·취소·기록 이어 쓰기)")



def test_download_filenames():
    """txt·xlsx 파일 이름 — `날짜_종류_구분`, 정기 회차 자리는 `14-00기준`, 초안은 끝에 `_초안`.

    정기(확정본·초안·보관함 회차 줄·날짜 줄)와 수시(원본·확정본·보관함 세 층)를 한 번에 본다.
    기간 이름은 같은 날짜가 여러 번 들어와도 하루로 친다(수시 보관함 — 같은 날 확정본 여러 장).
    """
    import re as _re
    from app.excel_export import date_range_label
    from app.adhoc import renderer as adhoc_renderer, archive_renderer

    _require_isolation()
    # 기간 이름
    assert date_range_label(["2026-09-15", "2026-09-15"]) == "2026-09-15", "같은 날 두 번이면 하루로"
    assert date_range_label(["2026-09-03", "2026-08-21", "2026-09-03"]) == "2026-08-21~09-03"
    assert date_range_label(["2026-09-22"]) == "2026-09-22"

    # 정기 확정본·보관함 — 실제 수집·렌더링을 거친 화면에서 읽는다
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        with patch.object(history_renderer, "HISTORY_HTML_PATH", tmp_dir / "history.html"), \
             patch.object(landing_renderer, "LANDING_HTML_PATH", tmp_dir / "home.html"), \
             patch.object(naver_api, "_search_one_keyword", side_effect=_fake_search_one_keyword):
            main._scrape_and_render("14:00", "00:00")
            today = storage.load_latest_run()["run_at"][:10]
            names = lambda text: set(_re.findall(r'name="filename" value="([^"]+)"', text))
            index_names = names(renderer.OUTPUT_HTML_PATH.read_text(encoding="utf-8"))
            assert index_names == {f"{today}_언론모니터링_14-00기준.txt", f"{today}_언론모니터링_14-00기준.xlsx"}, index_names
            history_text = history_renderer.HISTORY_HTML_PATH.read_text(encoding="utf-8")
            assert f"{today}_언론모니터링_14-00기준.txt" in names(history_text), "보관함 회차 줄"
            # 날짜 줄·맨 위 txt는 JS가 짓는다 — xlsx(excel_filename_for_dates)와 확장자만 달라야 한다
            assert "'_언론모니터링.txt'" in history_text and "_언론모니터링_전체" not in history_text
            assert history_renderer.excel_filename_for_dates([today, today]) == f"{today}_언론모니터링.xlsx"

    # 정기 초안 — txt·xlsx가 같은 이름
    actions = preview_renderer._actions_html("본문", "14:00", export_rows=[], export_date="2026-09-22")
    draft_names = set(_re.findall(r'name="filename" value="([^"]+)"', actions))
    assert draft_names == {"2026-09-22_언론모니터링_14-00기준_초안.txt", "2026-09-22_언론모니터링_14-00기준_초안.xlsx"}, draft_names

    # 수시 원본·확정본 — 사안명의 금지 문자는 밑줄로
    raw = {"kind": "collect", "raw": True, "collect_date": "2026-09-22", "report_title": "인사/청문회"}
    bundle = {"kind": "bundle", "collect_date": "2026-09-22", "report_title": "인사청문회"}
    assert adhoc_renderer.download_filename(raw) == "2026-09-22_수시모니터링_인사_청문회_원본.txt"
    assert adhoc_renderer.download_excel_filename(raw) == "2026-09-22_수시모니터링_인사_청문회_원본.xlsx"
    assert adhoc_renderer.download_filename(bundle) == "2026-09-22_수시모니터링_인사청문회.txt"

    # 수시 보관함 — 같은 날 확정본 두 장, 여러 날, 맨 위
    same_day = [{"collect_date": "2026-09-15"}, {"collect_date": "2026-09-15"}]
    assert archive_renderer._scope_filename(same_day, "인사청문회", "txt") == "2026-09-15_수시모니터링_인사청문회.txt"
    spread = [{"collect_date": "2026-09-03"}, {"collect_date": "2026-08-21"}]
    assert archive_renderer._scope_filename(spread, "재경위", "xlsx") == "2026-08-21~09-03_수시모니터링_재경위.xlsx"
    assert archive_renderer._scope_filename(spread, "", "txt") == "2026-08-21~09-03_수시모니터링_전체.txt"
    print("42) 내려받기 파일 이름: 통과 (정기 확정본·초안·보관함 `14-00기준`, 초안 `_초안`, 수시 원본·확정본·보관함 세 층, 같은 날 여러 장은 하루로)")


def test_send_log():
    """발송 기록(2026-09-23, mockups/SEND_LOG_MOCKUP.html).

      1) 텔레그램 발송 결과가 사람마다 남는다 — 받은 사람(알림 없이 갔는지·몇 통)과 못 받은
         사람(이유), 정기는 사람마다 받은 것(기사 목록/요약만)
      2) 403 본문으로 이유를 가른다(차단 / 대화 시작 안 함)
      3) 자동발송이 같은 결과로 되풀이되면 한 줄(repeat), 건너뜀은 한 번만
      4) [속보] 알림·호출 한도 경고도 남는다
      5) 화면: 회차 키로 오면 그 줄이 펼쳐지고, 실패 이유 밑에 고치는 방법
      6) 90일 지난 날짜 파일은 지워진다
    """
    _require_isolation()
    from app import report_message
    import app.settings_server as settings_server

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        with patch.object(telegram_recipients, "TELEGRAM_RECIPIENTS_FILE", tmp_dir / "telegram_recipients.json"), \
             patch.object(credentials, "telegram_bot_token", lambda: "fake-token"), \
             patch.object(telegram_bot.requests, "post") as mock_post:
            telegram_recipients.save_telegram_recipients([
                {"name": "담당자", "chat_id": "1", "regular_articles": True, "alert_flash": True, "notify": {"mode": "always"}},
                {"name": "박주무관", "chat_id": "2", "regular_summary": True, "alert_flash": True, "notify": {"mode": "always"}},
                {"name": "새사람", "chat_id": "3", "regular_articles": True, "notify": {"mode": "always"}},
            ])
            bodies = {
                "2": '{"ok":false,"description":"Forbidden: bot was blocked by the user"}',
                "3": "{\"ok\":false,\"description\":\"Forbidden: bot can't initiate conversation with a user\"}",
            }

            def fake_post(url, data, timeout):
                body = bodies.get(data["chat_id"])
                return MagicMock(ok=body is None, status_code=403, text=body or "")

            mock_post.side_effect = fake_post

            # 1)·2) 정기 발송 한 번
            recipients = telegram_recipients.report_recipients("regular")
            result = report_message.send_planned(
                report_message.plan_messages(recipients, "regular", "헤더\n\n본문", "요약 문단")
            )
            assert [d["target"] for d in result.delivered] == ["1"], result.delivered
            names = {r["chat_id"]: r["name"] for r in recipients}
            rows = send_log.deliveries(
                "telegram", result, names, report_message.content_labels(recipients, "regular", "요약 문단")
            )
            by_name = {r["name"]: r for r in rows}
            assert by_name["담당자"]["ok"] and by_name["담당자"]["content"] == "기사 목록"
            assert by_name["박주무관"]["content"] == "요약만"
            assert by_name["박주무관"]["error"] == "받는 사람이 봇을 차단했습니다(403)", by_name["박주무관"]
            assert by_name["새사람"]["error"] == "받는 사람이 아직 봇과 대화를 시작하지 않았습니다(403)"

            # 3) 자동발송 되풀이 — 같은 결과면 한 줄
            key = "2026-09-23|14:00"
            for _ in range(3):
                send_log.record("regular", "14:00 확정본", rows, auto=True, send_no=1, run_key=key,
                                coalesce=f"regular-auto:{key}")
            for _ in range(2):
                send_log.record("regular", "09:00 확정본", [], auto=True, skipped=True,
                                note="자동발송 건너뜀 — AI 분류 실패", coalesce="regular-auto:09")
            # 4) 알림
            breaking_alert_sender._send_batch(
                "속보", [{"outlet": "연합뉴스", "title": "[속보] 시험", "url": "https://example.com/a"}],
                "🚨 [속보] 1건", {}, datetime.now(),
            )
            days = send_log.load_days()
            entries = days[0][1]
            regular = [e for e in entries if e["kind"] == "regular" and e["status"] != "skipped"]
            assert len(regular) == 1 and regular[0]["repeat"] == 3, regular
            assert sum(1 for e in entries if e["status"] == "skipped") == 1, "건너뜀은 한 번만"
            flash = [e for e in entries if e["kind"] == "flash"]
            assert flash and flash[0]["articles"][0]["title"] == "[속보] 시험"
            assert send_log.tally_text(flash[0]) == "텔레그램 1/2", send_log.tally_text(flash[0])
            assert send_log.latest_for_run(key)["repeat"] == 3

            # 5) 화면
            page = settings_server.render_send_log_page({"run": [key]})
            assert "is-focus" in page and "3번 시도" in page, "회차 키로 오면 그 줄을 펼친다"
            assert "봇 차단을 풀면" in page and "[시작]을 누르면" in page, "실패마다 고치는 방법"
            assert "실패만" in page and "읽었는지는" in page

            # 6) 보관 기간
            old_file = send_log.SEND_LOG_DIR / "2020-01-01.json"
            old_file.write_text('{"entries": [{"at": "2020-01-01T09:00:00"}]}', encoding="utf-8")
            send_log.record("quota", "한도 경고", rows[:1])
            assert not old_file.exists(), "90일 지난 기록은 지운다"
    print("44) 발송 기록: 통과 (사람마다 도착·이유·받은 것, 403 이유 가르기, 자동발송 되풀이 한 줄, 알림 기록, 화면 펼침·고치는 방법, 90일 정리)")


def test_breaking_burst_alert():
    """[속보] 몰림 알림: 30분 안에 5개 언론사가 [속보]를 내면 개별 알림 뒤에 요약 한 통을
    더 보낸다. 언론사 수로 세고(같은 언론사 여러 건은 한 곳), 한 번 울리면 60분은 조용하고,
    창 밖(앱을 켤 때 몰아 받은 지난 기사)은 안 센다. [단독]은 대상이 아니다.
    """
    _require_isolation()

    def flash(i, outlet, hhmm, prefix="[속보]", title="국회 재경위, 청문보고서 채택"):
        return {"outlet": outlet, "title": f"{prefix} {title} {i}",
                "url": f"https://n.news.naver.com/mnews/article/{i:03d}/1",
                "pub_date": f"2026-09-17T{hhmm}:00+09:00"}

    def texts(mock_post):
        return [c.kwargs["data"]["text"] for c in mock_post.call_args_list]

    # 받는 사람 파일은 러너가 격리하지 않는다 — 여기서 임시 폴더로 돌리지 않으면 실제
    # data/telegram_recipients.json을 테스트 값으로 덮어쓴다(HISTORY.md 「글자 크기 단계」).
    with tempfile.TemporaryDirectory() as recipients_tmp, \
         patch.object(telegram_recipients, "TELEGRAM_RECIPIENTS_FILE", Path(recipients_tmp) / "telegram_recipients.json"), \
         patch.object(credentials, "telegram_bot_token", lambda: "fake-token"), \
         patch.object(telegram_bot.requests, "post") as mock_post:
        mock_post.return_value = MagicMock(ok=True)
        telegram_recipients.save_telegram_recipients(
            [{"name": "나", "chat_id": "111", "alert_scoop": True, "alert_flash": True}]
        )
        # 4곳(뉴시스 두 건은 한 곳) — 아직 안 울린다
        first = [flash(1, "뉴시스", "09:46"), flash(2, "YTN", "09:48"), flash(3, "뉴시스", "09:48"),
                 flash(4, "중앙일보", "09:49"), flash(5, "이데일리", "09:50")]
        breaking_alert_sender.detect_and_alert(first, now=datetime(2026, 9, 17, 9, 52))
        assert len(texts(mock_post)) == 1 and "🔥" not in texts(mock_post)[0], "4곳에서 울리면 안 된다"

        # 5번째 언론사 — 개별 알림 다음에 몰림 한 통. 몰림은 사건을 가리지 않고 언론사 수로
        # 세므로 다른 사건이어도 울린다. 같은 사건이면 개별 알림 쪽이 접히므로(app.alert_event,
        # test_breaking_alert_folds_same_event) 두 통의 순서는 사건이 다를 때 확인한다.
        mock_post.reset_mock()
        breaking_alert_sender.detect_and_alert(
            [flash(6, "헤럴드경제", "09:54", title="유류세 인하 두 달 연장")], now=datetime(2026, 9, 17, 9, 55))
        sent = texts(mock_post)
        assert len(sent) == 2 and sent[0].startswith("🚨"), f"개별 알림 뒤에 몰림 알림이 와야 한다: {sent}"
        assert sent[1] == (
            "🔥 [속보]가 몰리고 있어요\n09:46~09:54 사이 5개 언론사\n\n"
            "국회 재경위, 청문보고서 채택 1\n뉴시스 · YTN · 중앙일보 · 이데일리 · 헤럴드경제"
        ), sent[1]

        # 60분 안엔 다시 안 울린다
        mock_post.reset_mock()
        breaking_alert_sender.detect_and_alert(
            [flash(7, "서울경제", "09:58", title="추경 편성 논의 착수")], now=datetime(2026, 9, 17, 10, 0))
        assert len(texts(mock_post)) == 1, "한 번 울린 뒤 60분 안에 또 울리면 안 된다"

        # [단독]은 몰려도 안 센다
        mock_post.reset_mock()
        scoops = [flash(20 + i, f"매체{i}", "11:10", prefix="[단독]") for i in range(6)]
        breaking_alert_sender.detect_and_alert(scoops, now=datetime(2026, 9, 17, 11, 12))
        assert len(texts(mock_post)) == 1 and "🔥" not in texts(mock_post)[0], "[단독]은 몰림 대상이 아니다"

        # 앱을 켤 때 몰아 받은 지난 기사는 창 밖이라 안 센다(08:28~08:36 5곳, 지금 09:13)
        mock_post.reset_mock()
        old = [flash(40 + i, f"언론{i}", f"08:{28 + 2 * i}") for i in range(5)]
        breaking_alert_sender.detect_and_alert(old, now=datetime(2026, 9, 18, 9, 13))
        assert len(texts(mock_post)) == 1, "몰린 시간이 이미 지난 기사로 울리면 안 된다"

        # 꺼 두면 안 울린다
        breaking_alert.save_breaking_alert_settings(True, [], "06:00", "23:00", 3, True, False, 30, 5)
        mock_post.reset_mock()
        fresh = [flash(60 + i, f"신문{i}", "14:2" + str(i)) for i in range(6)]
        breaking_alert_sender.detect_and_alert(fresh, now=datetime(2026, 9, 18, 14, 27))
        assert len(texts(mock_post)) == 1, "몰림 알림을 끄면 개별 알림만 가야 한다"

    for bad in ((45, 5), (30, 7)):
        try:
            breaking_alert.save_breaking_alert_settings(True, [], "06:00", "23:00", 3, True, True, *bad)
        except breaking_alert.BreakingAlertSettingsError:
            continue
        raise AssertionError(f"고를 수 없는 몰림 기준이 저장됐다: {bad}")

    # 지난 기록 계산: 하루 중 언론사가 가장 많이 몰린 구간
    pairs = [(datetime(2026, 9, 1, h, m), {"outlet": o, "title": "[속보] t"})
             for h, m, o in ((7, 0, "A"), (7, 10, "E"), (8, 40, "B"), (8, 41, "C"), (8, 42, "C"), (8, 55, "D"))]
    best = breaking_burst._max_window(pairs, 30)
    assert breaking_burst._distinct_outlets(best) == ["B", "C", "D"], best
    print("40) [속보] 몰림 알림: 통과 (5곳째에 한 통 더, 언론사 수로 셈, 60분 조용, [단독]·지난 기사·끈 경우 제외)")

def test_breaking_alert_folds_same_event():
    """같은 사건을 여러 언론사가 [속보]로 내면 (1) 한 통 안에선 대표 1건 + 언론사 이름 한 줄로
    묶고, (2) 이미 나간 사건의 후속 기사는 새 통을 만들지 않는다.

    실측(2026-09-23, 저장 회차 34일 203건): 9/22가 19통 → 12통, 9/17이 15통 → 10통으로 줄고
    조용한 날(8/25·9/21)은 한 건도 안 접혔다. [단독]은 같은 사건 쌍이 34일 내내 0건이라
    대상이 아니다(효과 0, 다른 사건을 잘못 묶을 위험만 남는다).
    """
    _require_isolation()
    재가 = "李대통령, 이형일·강신철·홍지선 장관 임명안 재가"

    def art(i, outlet, hhmm, title=재가, tag="[속보]", day=22, pub=True):
        row = {"outlet": outlet, "title": f"{tag} {title}",
               "url": f"https://n.news.naver.com/mnews/article/{i:03d}/1"}
        if pub:
            row["pub_date"] = f"2026-09-{day}T{hhmm}:00+09:00"
        return row

    def texts(mock_post):
        return [c.kwargs["data"]["text"] for c in mock_post.call_args_list]

    with tempfile.TemporaryDirectory() as recipients_tmp, \
         patch.object(telegram_recipients, "TELEGRAM_RECIPIENTS_FILE", Path(recipients_tmp) / "telegram_recipients.json"), \
         patch.object(credentials, "telegram_bot_token", lambda: "fake-token"), \
         patch.object(telegram_bot.requests, "post") as mock_post:
        mock_post.return_value = MagicMock(ok=True)
        telegram_recipients.save_telegram_recipients(
            [{"name": "나", "chat_id": "111", "alert_scoop": True, "alert_flash": True}]
        )
        # 몰림 알림은 끈 채로 센다 — 여기서 세는 건 개별 알림 통수다(⑥에서 따로 켠다).
        breaking_alert.save_breaking_alert_settings(True, [], "06:00", "23:00", 3, True, False, 30, 5)

        # ① 한 통 안: 같은 사건 3곳 + 다른 사건 1건 → 블록 2개. 대표는 가장 먼저 게시한 곳(KBS)이고,
        #    입력 순서가 08:33부터여도 대표 자리를 넘겨받는다.
        batch = [art(1, "중앙일보", "08:33"), art(2, "KBS", "08:28"), art(3, "서울경제", "08:30"),
                 art(9, "한국경제", "08:35", title="대미투자 1호 텍사스 발전소 구매확약 못받아")]
        breaking_alert_sender.detect_and_alert(batch, now=datetime(2026, 9, 22, 8, 36))
        sent = texts(mock_post)
        assert len(sent) == 1, f"한 통으로 가야 한다: {sent}"
        assert sent[0].count("↳ 같은 사건: ") == 1, f"같은 사건 줄이 하나여야 한다:\n{sent[0]}"
        assert "↳ 같은 사건: 중앙일보 · 서울경제" in sent[0], f"묶인 언론사 이름이 입력 순서대로 와야 한다:\n{sent[0]}"
        assert sent[0].count("https://") == 2, f"묶은 기사 URL이 그대로 다 실렸다:\n{sent[0]}"
        assert sent[0].split("↳")[0].rstrip().endswith("08:28 게시"), f"대표는 가장 먼저 게시한 기사여야 한다:\n{sent[0]}"
        assert "(KBS)" in sent[0] and "(한국경제)" in sent[0], f"대표와 다른 사건이 다 있어야 한다:\n{sent[0]}"

        # ② 뒤따라 온 같은 사건 — 새 통을 아예 안 보낸다. 기록엔 folded 표시로 남고 홈 목록엔 보인다.
        mock_post.reset_mock()
        n = breaking_alert_sender.detect_and_alert([art(4, "뉴시스", "08:37")], now=datetime(2026, 9, 22, 8, 39))
        assert texts(mock_post) == [], f"같은 사건으로 또 울렸다: {texts(mock_post)}"
        assert n == 1, "접은 기사도 처리한 것으로 세야 한다(같은 후보를 폴링마다 다시 검사하지 않게)"
        items = {it["url"]: it for it in alerted_urls.alerted_items(datetime(2026, 9, 22, 8, 39))}
        assert len(items) == 5, f"접은 기사가 기록에서 사라졌다: {list(items)}"
        folded = [it for it in items.values() if it.get("folded")]
        assert [it["url"] for it in folded] == [art(4, "뉴시스", "08:37")["url"]], f"folded 표시가 틀렸다: {folded}"
        # 홈 [속보] 목록은 이 기록(items)만 보므로 접은 기사도 그대로 보인다(app.alerted_urls).

        # ③ [단독]은 묶지 않는다 — 제목이 거의 같아도 블록 둘, 후속도 제 통으로 온다.
        mock_post.reset_mock()
        breaking_alert_sender.detect_and_alert(
            [art(11, "조선일보", "09:10", tag="[단독]"), art(12, "동아일보", "09:12", tag="[단독]")],
            now=datetime(2026, 9, 22, 9, 13))
        sent = texts(mock_post)
        assert len(sent) == 1 and "↳" not in sent[0] and sent[0].count("https://") == 2, f"[단독]을 묶었다:\n{sent}"
        mock_post.reset_mock()
        breaking_alert_sender.detect_and_alert([art(13, "한겨레", "09:20", tag="[단독]")], now=datetime(2026, 9, 22, 9, 21))
        assert len(texts(mock_post)) == 1, "[단독] 후속을 접었다 — 대상이 아니다"

        # ④ 창(60분) 밖이면 새 사건으로 본다 — 같은 제목이 오후에 다시 나오면 알림이 온다.
        mock_post.reset_mock()
        breaking_alert_sender.detect_and_alert([art(5, "이데일리", "10:00")], now=datetime(2026, 9, 22, 10, 1))
        assert len(texts(mock_post)) == 1, "60분이 지난 같은 제목까지 접었다"

        # ⑤ 게시 시각을 못 읽는 기사는 접지 않는다 — 창을 확인할 수 없으면 보내는 쪽이 안전하다.
        mock_post.reset_mock()
        breaking_alert_sender.detect_and_alert([art(6, "뉴스핌", "10:05", pub=False)], now=datetime(2026, 9, 22, 10, 6))
        assert len(texts(mock_post)) == 1, "시각을 모르는 기사를 접었다"

        # ⑥ 접기만 한 경우에도 몰림 판정은 돈다 — 쏟아진 사실이 아무 데도 안 남으면 안 된다.
        breaking_alert.save_breaking_alert_settings(True, [], "06:00", "23:00", 3, True, True, 30, 3)
        breaking_alert_sender.detect_and_alert(
            [art(21, "KBS", "08:10", day=23), art(22, "MBC", "08:12", day=23)],
            now=datetime(2026, 9, 23, 8, 13))
        mock_post.reset_mock()
        breaking_alert_sender.detect_and_alert([art(23, "SBS", "08:15", day=23)], now=datetime(2026, 9, 23, 8, 16))
        sent = texts(mock_post)
        assert len(sent) == 1 and sent[0].startswith("🔥"), f"접기만 한 통에서 몰림 알림이 안 왔다: {sent}"
        assert "3개 언론사" in sent[0], f"접은 기사까지 세야 한다: {sent[0]}"

    print("41) [속보] 같은 사건 묶기: 통과 (한 통 안 묶음·후속 안 울림·[단독] 제외·창 밖·시각 없음·몰림은 그대로)")


def test_auto_send_does_not_resend_forever():
    """자동발송이 같은 회차를 무한히 다시 보내지 않는다(2026-09-23).

      1) 받는 사람 중 한 명이라도 실제로 받았으면 발송 완료로 기록한다 — 나머지 한 명이
         영구 실패(봇 차단·chat id 오타)해도 멀쩡한 사람에게 두 번 보내지 않는다
      2) 못 받은 사람은 send_failure에 그대로 남는다(정기 보관함 빨간 링)
      3) 아무에게도 못 갔을 때만 다시 시도하고, 그것도 _MAX_AUTO_SEND_ATTEMPTS번까지만
      4) 포기하면 발송 기록에 "직접 발송해주세요" 한 줄을 남긴다(조용히 멈추지 않는다)
      5) delivered가 없는 옛 결과(bool·ok만 있는 SendResult)는 예전 판단 그대로
    """
    _require_isolation()
    from app.send_result import SendResult

    def _stub_channels(st):
        """두 채널 조회·기록을 목으로 바꾼다(텔레그램만 씀)."""
        st.enter_context(patch.object(confirm_send, "report_recipients",
                                      lambda kind: [{"chat_id": "1"}, {"chat_id": "999"}]))
        st.enter_context(patch.object(confirm_send, "active_recipient_emails", lambda: []))
        st.enter_context(patch.object(confirm_send, "load_telegram_recipients",
                                      lambda: [{"chat_id": "1", "name": "담당자"},
                                               {"chat_id": "999", "name": "차단한 사람"}]))
        st.enter_context(patch.object(confirm_send, "load_email_recipients", lambda: []))
        st.enter_context(patch.object(confirm_send, "latest_confirmed_report",
                                      lambda: {"text": "언론 모니터링 11시 기준\n\n본문", "summary": ""}))
        st.enter_context(patch.object(confirm_send, "run_looks_rule_based", lambda r: False))
        st.enter_context(patch.object(confirm_send, "record_send", lambda r, auto=False: {
            **r, "send_count": r.get("send_count", 0) + 1, "sent_by": "auto" if auto else "manual"}))
        st.enter_context(patch.object(confirm_send, "record_send_failure", lambda r, f, auto=False: {
            **r, "send_failure": {"channels": f}}))

    # 1)·2) 둘 중 한 명만 영구 실패 — 한 번만 나가고 실패는 남는다
    run = {"run_slot": "11:00", "run_at": "2026-09-23T11:00:00",
           "confirmed": True, "send_count": 0, "articles": []}
    partial = SendResult(False,
                         [{"target": "999", "error": "받는 사람이 봇을 차단했습니다(403)"}],
                         [{"target": "1", "parts": 1}])
    sends = []

    def _partial(plan):
        sends.append(plan)
        return partial

    with ExitStack() as st:
        _stub_channels(st)
        st.enter_context(patch.object(confirm_send, "send_planned", _partial))
        st.enter_context(patch.object(confirm_send, "_record_send_log", lambda *a, **k: None))
        updated, sent = confirm_send.send_confirmed_run(run, auto=True)
        # 같은 회차를 한 번 더 보내려 해도 send_count가 이미 1이라 자동발송 분기를 안 탄다
        assert updated["send_count"] == 1, updated
        assert sent is True, "한 명이라도 받았으면 발송 완료여야 한다(안 그러면 tick마다 재발송)"
        assert len(sends) == 1, sends
    failed_names = [c["target"] for c in updated["send_failure"]["channels"]]
    assert failed_names == ["차단한 사람"], failed_names

    # 3)·4) 아무에게도 못 간 회차 — 10초 tick 240번(40분) 동안에도 상한까지만 시도하고 포기한다
    confirm_send._auto_send_attempts.clear()
    tried, notes = [], []
    dead_run = {"run_slot": "12:00", "run_at": "2026-09-23T12:00:00",
                "confirmed": True, "send_count": 0, "articles": []}

    def _never_delivered(r, text_override=None, auto=False):
        tried.append(auto)
        return r, False

    def _capture_log(run_, auto, rows, attempted=True, note=""):
        if note:
            notes.append(note)

    start = datetime(2026, 9, 23, 12, 10, 0)
    with ExitStack() as st:
        st.enter_context(patch.object(confirm_send, "send_confirmed_run", _never_delivered))
        st.enter_context(patch.object(confirm_send, "_record_send_log", _capture_log))
        st.enter_context(patch.object(confirm_send, "load_settings", lambda: {}))
        st.enter_context(patch.object(confirm_send, "is_auto_send_enabled", lambda s: True))
        st.enter_context(patch.object(confirm_send, "auto_send_grace_sec", lambda s: 300))
        st.enter_context(patch.object(confirm_send, "load_latest_run", lambda: dead_run))
        st.enter_context(patch.object(confirm_send, "run_looks_rule_based", lambda r: False))
        st.enter_context(patch.object(confirm_send, "generate_screen", lambda: None))
        for i in range(240):
            confirm_send.check_pending_confirm_and_send(start + timedelta(seconds=10 * i))
    assert len(tried) == confirm_send._MAX_AUTO_SEND_ATTEMPTS, f"시도 {len(tried)}회"
    assert notes and "직접 발송" in notes[-1], notes
    confirm_send._auto_send_attempts.clear()

    # 5) delivered가 없는 옛/가짜 결과는 예전 판단(ok)을 그대로 쓴다
    assert confirm_send._delivered_any(None) is False
    assert confirm_send._delivered_any(True) is True
    assert confirm_send._delivered_any(False) is False
    assert confirm_send._delivered_any(SendResult(True, [], [])) is True
    assert confirm_send._delivered_any(SendResult(False, [{"target": None, "error": "토큰 없음"}], [])) is False

    print("45) 자동발송 무한 재발송 방지: 통과 (한 명이라도 받으면 한 번만, 실패는 남고, 전원 실패는 5번까지·포기 안내)")


def test_undo_stack_dedupes_snapshots():
    """되돌리기 스택이 같은 값을 반복 저장하지 않는다(2026-09-23).

      1) 안 바뀐 조각은 blob 하나로 공유된다 — 스냅샷 내용·복원 결과는 그대로
      2) 복원은 「그땐 파일이 없었다」까지 정확히 재현한다
      3) 상한을 넘겨 밀려난 항목의 blob은 버려진다
      4) 옛 형식(조각을 항목 안에 그대로 담던 기록)도 그대로 읽히고 되돌아간다
    """
    _require_isolation()
    from app import undo

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        hidden = tmp_dir / "hidden.json"
        custom = tmp_dir / "custom.json"
        stack = tmp_dir / "undo_stack.json"
        with patch.object(undo, "UNDO_FILE", stack), \
             patch.object(undo, "_SNAPSHOT_FILES", {"hidden": hidden, "custom": custom}), \
             patch.object(undo, "_snapshot_llm_cache", lambda: {"entries": [], "last_names": []}), \
             patch.object(undo, "_restore_llm_cache", lambda data: None), \
             patch.object(undo, "_snapshot_latest_run_file", lambda: None), \
             patch.object(undo, "_restore_latest_run_file", lambda data: None):
            # 1) hidden만 바뀌는 흔한 동작 5번 — custom·llm_cache·run_file은 값이 하나뿐이어야 한다
            custom.write_text('{"names": []}', encoding="utf-8")
            for i in range(5):
                hidden.write_text(json.dumps({"2026-09-23": [{"url": f"u{i}"}]}), encoding="utf-8")
                undo.push(f"기사 숨기기 {i}", coalesce_sec=0)
            saved = json.loads(stack.read_text(encoding="utf-8"))
            assert saved["v"] == 2, saved.get("v")
            custom_refs = {e["files"]["custom"] for e in saved["entries"]}
            hidden_refs = {e["files"]["hidden"] for e in saved["entries"]}
            assert len(custom_refs) == 1, f"안 바뀐 파일이 {len(custom_refs)}번 저장됨"
            assert len(hidden_refs) == 5, hidden_refs
            assert len({e["llm_cache"] for e in saved["entries"]}) == 1
            # blob은 hidden 5 + custom 1 + llm_cache 1 + run_file(None) 1 = 8개뿐
            assert len(saved["blobs"]) == 8, len(saved["blobs"])

            # 2) 복원 — 파일이 없던 상태까지 그대로 돌아온다
            custom.unlink()
            undo.push("소제목 만들기", coalesce_sec=0)
            custom.write_text('{"names": ["새 소제목"]}', encoding="utf-8")
            hidden.write_text("{}", encoding="utf-8")
            undo.undo()
            assert not custom.exists(), "없던 파일이 되살아났다"
            assert json.loads(hidden.read_text(encoding="utf-8")) == {"2026-09-23": [{"url": "u4"}]}

            # 3) 상한을 넘기면 밀려난 항목의 blob도 같이 버려진다
            for i in range(undo._MAX_ENTRIES + 5):
                hidden.write_text(json.dumps({"2026-09-23": [{"url": f"x{i}"}]}), encoding="utf-8")
                undo.push(f"많이 쌓기 {i}", coalesce_sec=0)
            saved = json.loads(stack.read_text(encoding="utf-8"))
            assert len(saved["entries"]) == undo._MAX_ENTRIES
            used = {r for e in saved["entries"] for r in e["files"].values()}
            used |= {e["llm_cache"] for e in saved["entries"]} | {e["run_file"] for e in saved["entries"]}
            assert set(saved["blobs"]) == used, "가리키는 데 없는 blob이 남았다"

            # 4) 옛 형식 기록도 그대로 읽히고 되돌아간다
            hidden.write_text('{"옛날": []}', encoding="utf-8")
            stack.write_text(json.dumps({
                "date": datetime.now().date().isoformat(),
                "entries": [{
                    "label": "옛 형식 숨기기",
                    "at": datetime.now().isoformat(timespec="seconds"),
                    "files": {"hidden": '{"되돌린 뒤": []}', "custom": None},
                    "llm_cache": {"entries": [], "last_names": []},
                    "run_file": None,
                    "touched": ["u9"],
                    "batch": None,
                }],
            }, ensure_ascii=False), encoding="utf-8")
            custom.write_text('{"names": ["지워져야 함"]}', encoding="utf-8")
            assert undo.peek_label() == "옛 형식 숨기기"
            assert undo.undo() == {"label": "옛 형식 숨기기", "touched": ["u9"]}
            assert json.loads(hidden.read_text(encoding="utf-8")) == {"되돌린 뒤": []}
            assert not custom.exists(), "옛 형식의 None(파일 없음)이 재현되지 않았다"

    print("46) 되돌리기 스택 중복 제거: 통과 (안 바뀐 조각은 한 번만, 복원·파일 없음 재현, 밀려난 blob 정리, 옛 형식 이관)")


if __name__ == "__main__":
    tests = [
        test_draft_all_first_classified_hidden_shows_unclassified,
        test_assigned_articles_keep_subheading_after_ai_members_hidden,
        test_cache_store_keeps_other_rounds,
        test_full_pipeline_happy_path,
        test_settings_actually_change_scraping,
        test_empty_run_shows_placeholder,
        test_search_window_filters_by_time,
        test_retention_cleanup_removes_old_runs,
        test_run_filename_follows_run_at_not_save_time,
        test_stale_run_shows_waiting_page,
        test_scheduler_shows_waiting_before_first_slot,
        test_empty_round_keeps_previous_screen,
        test_hidden_articles_newest_first,
        test_breaking_alert_scoop_detection,
        test_breaking_alert_catches_up_at_window_start,
        test_late_pickup_recovers_previous_slot_articles,
        test_catch_up_runs_all_missed_slots_today,
        test_draft_sees_the_same_window_as_confirmed,
        test_draft_keeps_what_it_showed,
        test_regular_run_delete_and_restore,
        test_adhoc_card_delete_and_restore,
        test_photo_caption_name_list_title,
        test_photo_caption_candidate_title_and_cut_stamp,
        test_outlet_photo_tag,
        test_photo_body_verdict,
        test_undo_returns_only_what_the_user_touched,
        test_adhoc_reload_to_now,
        test_label_archive_copy_and_order,
        test_photo_gather_and_adhoc_bulk_hide,
        test_split_group,
        test_adhoc_four_step_flow,
        test_adhoc_raw_versions,
        test_adhoc_move_to_other_issue_bundle,
        test_destination_switches,
        test_adhoc_raw_trash_and_rounds,
        test_editorial_filter,
        test_telegram_recipients_receive_and_notify,
        test_telegram_bot_name,
        test_negative_guess_per_round,
        test_breaking_alert_same_article_once,
        test_add_article_by_url,
        test_keyword_note_history_and_time_select,
        test_today_cut_round,
        test_breaking_burst_alert,
        test_breaking_alert_folds_same_event,
        test_download_filenames,
        test_send_log,
        test_auto_send_does_not_resend_forever,
        test_undo_stack_dedupes_snapshots,
    ]

    failures = []
    # 실제 data/articles, index.html, settings.json은 절대 건드리지 않도록, 이 테스트가
    # 쓰고 지우는 모든 경로를 임시 폴더로 바꿔치기한 채로만 테스트를 돌린다.
    # 테스트마다 별도의 새 임시 폴더를 써서, 회차 저장 시각(run_at)이 같은 초에 겹쳐
    # load_latest_run이 다른 테스트의 회차를 잘못 고르는 일이 없도록 한다.
    for test in tests:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            tmp_articles_dir = tmp_dir / "articles"
            tmp_articles_dir.mkdir()

            with patch.object(storage, "ARTICLES_DIR", tmp_articles_dir), \
                 patch.object(renderer, "OUTPUT_HTML_PATH", tmp_dir / "index.html"), \
                 patch.object(settings, "SETTINGS_FILE", tmp_dir / "settings.json"), \
                 patch.object(draft_articles, "DRAFT_PENDING_ARTICLES_FILE", tmp_dir / "draft_pending_articles.json"), \
                 patch.object(preview_order, "PREVIEW_ORDER_FILE", tmp_dir / "preview_order.json"), \
                 patch.object(preview_cache, "PREVIEW_CACHE_FILE", tmp_dir / "preview_cache.json"), \
                 patch.object(custom_groups, "CUSTOM_GROUPS_FILE", tmp_dir / "custom_groups.json"), \
                 patch.object(alerted_urls, "ALERTED_URLS_FILE", tmp_dir / "alerted_urls.json"), \
                 patch.object(draft_seen, "DRAFT_SEEN_FILE", tmp_dir / "draft_seen.json"), \
                 patch.object(config, "LLM_CLASSIFICATION_CACHE_FILE", tmp_dir / "llm_classification_cache.json"), \
                 patch.object(llm_classifier, "_cache", {}), \
                 patch.object(llm_classifier, "_cache_hydrated", False), \
                 patch.object(llm_classifier, "_last_names", []), \
                 patch.object(llm_classifier, "_last_round_id", None), \
                 ExitStack() as more_patches:
                # Python 3.9는 한 함수 안 with 중첩이 20개까지라, 새로 격리할 저장소는 여기에 더한다.
                for target, name, value in (
                    (negative_guess, "NEGATIVE_GUESS_FILE", tmp_dir / "negative_guess.json"),
                    (today_cuts, "TODAY_CUTS_FILE", tmp_dir / "today_cuts.json"),
                    (auto_classify_turn, "AUTO_CLASSIFY_TURNS_FILE", tmp_dir / "auto_classify_turns.json"),
                    # [속보]를 보내는 모든 경로가 몰림 판정(app.breaking_burst)을 거쳐 설정을 읽고
                    # 상태 파일(last_burst_at)에 쓴다 — 실제 data/로 새지 않게.
                    (breaking_alert, "BREAKING_ALERT_FILE", tmp_dir / "breaking_alert.json"),
                    (breaking_alert_state, "BREAKING_ALERT_STATE_FILE", tmp_dir / "breaking_alert_state.json"),
                    # 원문 사진 판정 기록 — 실제 판정이 테스트로 새지 않게 비운 채로 시작한다.
                    (photo_body, "PHOTO_BODY_FILE", tmp_dir / "photo_body_verdicts.json"),
                    # 설정 화면에 저장한 키·봇 토큰 — 실제 토큰으로 테스트 메시지가 나가지 않게 비운다.
                    (credentials, "CREDENTIALS_FILE", tmp_dir / "credentials.json"),
                    # 발송 기록 — 테스트가 보내는 가짜 발송이 실제 data/send_log/에 쌓이지 않게.
                    (send_log, "SEND_LOG_DIR", tmp_dir / "send_log"),
                    (photo_body, "_verdicts", {}),
                    (photo_body, "_index", {}),
                ):
                    more_patches.enter_context(patch.object(target, name, value))
                # [추가: 2026-09-17] LLM 분류 캐시도 임시 폴더로 — 예전엔 회차를 저장하는
                # 테스트마다 실제 data/llm_classification_cache.json에 가짜 항목이 쌓였다.
                # 메모리 캐시도 테스트마다 비워, 앞 테스트의 분류가 다음 테스트로 새지 않게 한다.
                # [추가: 2026-08-20] collect_run이 회차 저장 직전에 [단독]·[속보] 알림
                # "바닥" 경로(app.breaking_alert_sender.detect_and_alert)를 항상 거치므로,
                # 그 경로가 쓰는 파일도 임시 폴더로 옮겨야 이 테스트가 실제 data/를 안 건드린다.
                try:
                    test()
                except AssertionError as e:
                    failures.append((test.__name__, str(e)))
                    print(f"실패: {test.__name__} — {e}")

    print()
    if failures:
        print(f"{len(failures)}개 테스트 실패:")
        for name, msg in failures:
            print(f"  - {name}: {msg}")
        sys.exit(1)
    else:
        print(f"모든 통합 테스트({len(tests)}개) 통과 (실제 data/ 폴더는 건드리지 않음)")
