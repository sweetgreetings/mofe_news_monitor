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
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import curation, custom_groups, draft_articles, history_renderer, landing_renderer, naver_api, preview_cache, preview_order, renderer, scheduler, scraper, settings, storage  # noqa: E402
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


def _fake_search_one_keyword(keyword, after=None, before=None):
    return [
        {
            "outlet": naver_api._extract_outlet(item["originallink"]),
            "title": item["title"],
            "url": item["link"],
            "summary": item["description"],
        }
        for item in _FIXTURE_BY_KEYWORD.get(keyword, [])
    ]


def test_full_pipeline_happy_path():
    """스크랩 -> 정렬 -> 필터 -> 분류 -> 화면 -> 복사/내보내기 텍스트까지 한 번에 검증한다.

    "재정경제부"·"재경부"는 "기관 정보" 그룹의 기본값(app.config.BASE_GROUP_KEYWORDS)에
    이미 포함돼 있어, 최초 설정 파일이 만들어질 때(app.settings._default_settings)
    자동으로 심어지므로 별도로 그룹을 등록하지 않아도 검색된다.
    """

    with patch.object(naver_api, "_search_one_keyword", side_effect=_fake_search_one_keyword):
        result = scraper.collect_run_with_retry("09:00")

    # [포토] 제외 + 완전 동일 제목 중복 제거(우선순위 높은 MBC가 남음) -> 2건만 남아야 함
    titles = [a["title"] for a in result["articles"]]
    assert "[포토] 재정경제부 브리핑 현장" not in titles, "포토 기사가 제외되지 않음"
    assert titles.count("재정경제부 세제 개편 발표") == 1, "완전 동일 제목 중복이 제거되지 않음"
    kept = next(a for a in result["articles"] if a["title"] == "재정경제부 세제 개편 발표")
    assert kept["outlet"] == "MBC", f"우선순위 높은 MBC가 아니라 {kept['outlet']}가 남음"
    print("1) 검색/정렬/필터: 통과 (포토 제외, 중복 제거 시 우선순위 언론사 유지)")

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
    assert "[포토]" not in plain_text
    print("3) 복사/내보내기 텍스트: 통과 (헤더 포함, 하단 AI 블록 제외, 포토 기사 제외)")


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

    def spy(groups, after=None, before=None):
        captured["groups"] = groups
        return []

    with patch.object(scraper, "search_articles_by_groups", spy):
        scraper.collect_run("10:30")  # groups 생략 -> 설정값을 읽어야 함

    assert len(captured["groups"]) == 1, "정기 스크랩에는 include_in_scrap이 꺼진 그룹(부동산그룹)이 섞이면 안 됨"
    assert captured["groups"][0]["name"] == "환율그룹"
    assert captured["groups"][0]["keywords"] == ["환율"], "꺼둔 키워드(외환)는 실제 검색에서 빠져야 함"
    print("4) 설정 연동: 통과 (정기 스크랩은 include_in_scrap 켜진 그룹·ON 상태 키워드만 실제 스크랩에 반영됨)")


def test_empty_run_shows_placeholder():
    """이번 회차에 기사가 하나도 없으면 '💤'가 표시되는지 확인한다.

    별도 그룹을 저장하지 않아도 최초 설정 파일 생성 시 심어지는 "기관 정보" 기본
    그룹만으로 검색이 실행된다.
    """
    with patch.object(naver_api, "_search_one_keyword", return_value=[]):
        scraper.collect_run_with_retry("13:30")
    html_text = renderer.generate_screen().read_text(encoding="utf-8")
    assert "💤" in html_text
    assert "언론 모니터링 13시 30분 기준" in html_text
    print("5) 빈 회차 처리: 통과 ('💤' 표시)")


def test_search_window_filters_by_time():
    """회차별 시간창 수집: after~before 구간 밖의 기사가 걸러지는지 확인한다 (PRD.md 기능1 규칙 2).

    naver_api._search_one_keyword 안쪽의 실제 필터링 로직을 검증해야 하므로, 이 테스트만
    한 단계 더 깊은 경계(requests.get)를 목으로 바꾼다.
    """

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

    with patch.object(naver_api.requests, "get", return_value=response):
        results = naver_api._search_one_keyword("키워드", after=after, before=before)

    titles = {r["title"] for r in results}
    assert titles == {"기사2", "기사3"}, f"07:00(제외)~10:30(포함) 구간엔 기사2·기사3만 남아야 하는데 {titles}"
    print("6) 회차별 시간창 필터: 통과 (창 밖 기사 제외, 경계는 하한 제외·상한 포함)")


def test_retention_cleanup_removes_old_runs():
    """7일 지난 회차가 자동 삭제 대상에 걸리는지 확인한다."""
    now = datetime(2026, 7, 23, 12, 0)
    old_path = storage.save_run("09:00", [], run_at=(now - timedelta(days=8)).isoformat(timespec="seconds"))
    deleted = storage.delete_expired_runs(retention_days=7, now=now)
    assert old_path in deleted
    assert not old_path.exists()
    print("7) 보관 기간 정리: 통과 (8일 지난 회차 삭제됨)")


def test_stale_run_shows_waiting_page():
    """자정이 지나 어제 회차만 남아있으면 generate_screen이 예외를 내고, 그 자리에
    안내 화면(🐰⏱️)을 대신 보여줄 수 있는지 확인한다 (실제 반영은 app.scheduler가 담당)."""
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
        render_waiting=lambda: render_waiting_calls.append(True),
        should_continue=fake_should_continue,
    )

    assert render_waiting_calls == [True], "03:00(첫 회차 전)에만 호출되고, 11:00(회차 지남)엔 호출되면 안 됨"
    print("9) 스케줄러 안내 화면 트리거: 통과 (첫 회차 전에만 render_waiting 호출)")


def test_empty_round_keeps_previous_screen():
    """오늘 이미 앞선 회차가 있는데 다음 회차가 0건이면, 메인 화면은 갱신하지 않고
    이전 회차 화면을 그대로 둔다(사용자 논의 결과) — history.html에는 여전히 그 회차가
    💤로 기록된다."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        with patch.object(history_renderer, "HISTORY_HTML_PATH", tmp_dir / "history.html"), \
             patch.object(landing_renderer, "LANDING_HTML_PATH", tmp_dir / "home.html"):
            with patch.object(naver_api, "_search_one_keyword", side_effect=_fake_search_one_keyword):
                main._scrape_and_render("09:00", "00:00")
            before = renderer.OUTPUT_HTML_PATH.read_text(encoding="utf-8")
            assert "언론 모니터링 9시 기준" in before

            with patch.object(naver_api, "_search_one_keyword", return_value=[]):
                main._scrape_and_render("10:30", "09:00")
            after = renderer.OUTPUT_HTML_PATH.read_text(encoding="utf-8")
            assert after == before, "0건 회차라도 오늘 이전 회차가 있으면 메인 화면을 덮어쓰면 안 됨"

            history_text = history_renderer.HISTORY_HTML_PATH.read_text(encoding="utf-8")
            assert "뉴스가 잠잠" in history_text, "history.html에는 0건 회차가 여전히 기록돼야 함"
    print("10) 0건 회차 처리: 통과 (메인 화면 유지, history엔 💤로 기록)")


def test_hidden_articles_newest_first():
    """숨긴 기사 관리 화면에서 가장 최근에 숨긴 기사가 맨 위에 오는지 확인한다.

    [수정: 2026-07-26] 익일 0시가 지나면 전부 비워지는 정책도 함께 확인한다 — 어제
    숨긴 기록은 오늘 읽으면 안 보여야 하고, 옛 형식(URL 문자열 목록)은 언제 숨겼는지
    알 수 없어 이 정책상 그냥 버려져야 한다(마이그레이션하지 않음).
    """
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

            # 익일 0시 이후 조회 — 어제 숨긴 기록은 전부 안 보여야 한다.
            tomorrow = datetime.now() + timedelta(days=1)
            assert curation.load_hidden_records(now=tomorrow) == []
            assert curation.load_hidden_urls(now=tomorrow) == set()

            # 옛 형식(문자열 목록)은 언제 숨겼는지 몰라 그냥 버려진다(마이그레이션 안 함).
            hidden_file.write_text(json.dumps(["https://old.com/1"]), encoding="utf-8")
            assert curation.load_hidden_records() == []
    print("11) 숨긴 기사 정렬 + 익일 초기화: 통과 (최근 숨긴 순, 옛 형식은 폐기, 다음날엔 전부 비워짐)")


if __name__ == "__main__":
    tests = [
        test_full_pipeline_happy_path,
        test_settings_actually_change_scraping,
        test_empty_run_shows_placeholder,
        test_search_window_filters_by_time,
        test_retention_cleanup_removes_old_runs,
        test_stale_run_shows_waiting_page,
        test_scheduler_shows_waiting_before_first_slot,
        test_empty_round_keeps_previous_screen,
        test_hidden_articles_newest_first,
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
                 patch.object(custom_groups, "CUSTOM_GROUPS_FILE", tmp_dir / "custom_groups.json"):
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
