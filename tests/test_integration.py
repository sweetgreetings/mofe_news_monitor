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
import ast
import json
import os
import re
import sys
import tempfile
from contextlib import ExitStack
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import agency_archive, alert_groups, alerted_urls, config, credentials, api_usage, breaking_alert, breaking_alert_sender, breaking_alert_state, confirm_send, curation, custom_groups, draft_articles, draft_seen, history_renderer, landing_renderer, llm_classifier, naver_api, preview_cache, preview_order, preview_renderer, renderer, scheduler, scraper, settings, storage, telegram_bot, telegram_recipients, negative_guess, today_cuts, auto_classify_turn, photo_body, breaking_burst, send_log, photo_overrides, manual_keyword_note  # noqa: E402
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


def _fake_search_one_keyword(keyword, after=None, before=None, include_unparsed_dates=False, day=None):
    # day: 어느 날짜를 모으나(지난 날짜 수집). 픽스처는 늘 "오늘" 기사를 돌려주므로
    # 값을 쓰지 않지만, 실제 함수와 시그니처가 어긋나면 호출 자체가 터진다.
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



def test_alert_scope_follows_keyword_switch():
    """[단독]·[속보] 알림 범위를 정하는 자리가 **검색어 그룹 스위치 하나**인지 확인한다.

    고치기 전 사고: 「바닥」 경로(app.scraper.collect_run이 회차 저장 직전에 하는 말머리
    검사)가 설정을 통째로 무시하고 **회차 기사 전부**를 알렸다. "감시를 켜지 않았어도
    회차 안에서는 놓치지 않게" 하려던 안전장치인데, 그 때문에 담당자가 알림을 끌 방법이
    아예 없었다 — 설정 화면에서 그룹 체크를 풀어도 그 그룹이 초안·확정본에 들어가는 한
    알림은 그대로 나갔다(국정감사 기간 「재경위」 키워드 하나가 [단독] 알림의 절반을
    만들었다). 이제 폴링과 바닥이 **같은 스위치**를 본다.

    보는 것 다섯:
      1) 바닥 경로(articles_in_alert_scope)가 알림 끈 그룹의 기사를 걸러낸다.
      2) 폴링(_watched_keywords)도 같은 스위치를 본다 — 두 경로가 같은 범위여야 한다.
      3) 꺼진 그룹(목적지 둘 다 끔)은 알림도 안 간다.
      4) 스위치가 저장·복원된다(include_in_alert).
      5) 옛 설정(/breaking-alert의 group_names)이 스위치로 옮겨지고 그 키는 지워진다.
    """
    _require_isolation()
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        with patch.object(settings, "SETTINGS_FILE", tmp_dir / "settings.json"), \
             patch.object(breaking_alert, "BREAKING_ALERT_FILE", tmp_dir / "breaking_alert.json"), \
             patch.object(alert_groups, "BREAKING_ALERT_FILE", tmp_dir / "breaking_alert.json"):
            settings.save_keyword_groups([
                {"name": "기관", "keywords": ["재경부"], "mode": "OR",
                 "include_in_live": True, "include_in_scrap": True, "include_in_alert": True},
                {"name": "재경위", "keywords": ["재정경제기획위원회"], "mode": "OR",
                 "include_in_live": True, "include_in_scrap": True, "include_in_alert": False},
                {"name": "꺼진그룹", "keywords": ["설탕세"], "mode": "OR",
                 "include_in_live": False, "include_in_scrap": False, "include_in_alert": True},
            ])

            # 4) 스위치가 저장·복원된다
            saved = {g["name"]: g for g in settings.load_settings()["keyword_groups"]}
            assert saved["기관"]["include_in_alert"] is True
            assert saved["재경위"]["include_in_alert"] is False, "끈 스위치가 저장되지 않았다"

            # 3) 꺼진 그룹은 스위치를 켜 뒀어도 알림 대상이 아니다 — 다만 **설정은 남는다**
            assert settings.group_in_alert(saved["꺼진그룹"]) is False, (
                "어느 화면에도 안 보이는 그룹이 텔레그램만 울리는 조합은 뜻이 없다"
            )
            assert saved["꺼진그룹"]["include_in_alert"] is True, (
                "꺼진 그룹의 알림 설정이 False로 굳으면 그룹을 다시 켤 때 안 돌아온다 "
                "— 꺼둔 키워드(disabled_keywords)를 보존하는 것과 같은 원칙"
            )
            assert settings.group_in_alert(
                dict(saved["꺼진그룹"], include_in_live=True)
            ) is True, "다시 켠 그룹의 알림 설정이 살아나지 않는다"

            # 2) 폴링이 보는 키워드 — 알림을 켠 그룹의 것만
            watched = breaking_alert_sender._watched_keywords(settings.load_settings())
            assert watched == ["재경부"], f"폴링 범위가 스위치와 어긋난다: {watched}"

            # 1) 바닥 경로 — 회차 기사 중 알림 켠 그룹에 걸린 것만 넘어간다
            articles = [
                {"url": "u1", "title": "[단독] 기관 기사", "matched_keywords": ["재경부"]},
                {"url": "u2", "title": "[단독] 국감 공방", "matched_keywords": ["재정경제기획위원회"]},
                {"url": "u3", "title": "[속보] 둘 다 걸림",
                 "matched_keywords": ["재정경제기획위원회", "재경부"]},
                {"url": "u4", "title": "[단독] 수기로 담은 기사"},  # matched_keywords 없음
            ]
            scoped = {a["url"] for a in breaking_alert_sender.articles_in_alert_scope(articles)}
            assert scoped == {"u1", "u3"}, (
                f"바닥 경로가 스위치를 안 따른다: {scoped} — 끈 그룹의 기사가 새면 "
                "담당자가 알림을 끌 수가 없다"
            )

            # 알림을 전부 끄면 바닥도 조용하다(그게 끄는 것의 뜻이다)
            groups = settings.load_settings()["keyword_groups"]
            for g in groups:
                g["include_in_alert"] = False
            settings.save_keyword_groups(groups)
            assert breaking_alert_sender.articles_in_alert_scope(articles) == []

            # 5) 옛 설정 이관 — group_names -> include_in_alert, 그 키는 지운다
            settings.save_keyword_groups([
                {"name": "기관", "keywords": ["재경부"], "mode": "OR",
                 "include_in_live": True, "include_in_scrap": True},
                {"name": "재경위", "keywords": ["재정경제기획위원회"], "mode": "OR",
                 "include_in_live": True, "include_in_scrap": True},
            ])
            (tmp_dir / "breaking_alert.json").write_text(
                json.dumps({"enabled": True, "group_names": ["기관"], "start": "06:00",
                            "end": "23:00", "interval_min": 3, "catch_up_enabled": True},
                           ensure_ascii=False),
                encoding="utf-8",
            )
            assert alert_groups.transplant_alert_groups() is True
            moved = {g["name"]: g["include_in_alert"]
                     for g in settings.load_settings()["keyword_groups"]}
            assert moved == {"기관": True, "재경위": False}, f"이관 결과가 다르다: {moved}"
            raw = json.loads((tmp_dir / "breaking_alert.json").read_text(encoding="utf-8"))
            assert "group_names" not in raw, (
                "옛 키가 남으면 같은 설정이 두 자리에 있게 된다"
            )
            # 두 번째 호출은 아무것도 하지 않는다(이미 옮겼다)
            assert alert_groups.transplant_alert_groups() is False

    print("54) [단독]·[속보] 알림 범위: 통과 (검색어 그룹 스위치 하나 — 폴링·바닥 같은 범위, "
          "꺼진 그룹 제외, 옛 group_names 이관·키 삭제)")


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
            # 감시 대상은 그룹의 「[단독]·[속보] 알림」 스위치가 정한다(include_in_alert)
            settings.save_keyword_groups([
                {"name": "감시", "keywords": ["기재부"], "mode": "OR",
                 "include_in_live": True, "include_in_scrap": True, "include_in_alert": True},
            ])
            breaking_alert.save_breaking_alert_settings(
                enabled=True, start="06:00", end="23:00",
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
    """정기 보관함 회차 삭제 — trash로 옮기고, 보관함에선 흔적 없이 사라지고, 되살리면 돌아온다.

    [추가: 2026-09-11] 지키는 것 셋:
      1) 오늘 회차는 못 지운다 — 파일이 없어지면 스케줄러가 run_exists로 곧바로 다시 수집한다.
      2) 가장 최근 회차는 못 지운다 — 지우면 그 앞 회차가 자동발송 대상이 될 수 있다(새벽).
      3) [수정: 2026-10-12] 지운 자리는 **아무 줄도 아니다** — 「삭제함」 줄도, 「수집 기록
         없음」도 안 그린다(뒤는 앱이 못 모은 날처럼 보여 거짓말이고, 앞은 같은 삭제가
         보관함 안 두 자리에 나타나 헷갈렸다). 지운 회차만 남은 날짜는 줄째 사라진다.
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

    # ③-1 살아 있는 회차가 남은 날짜 — 지운 자리에 「삭제함」도 「수집 기록 없음」도 없다
    # (뒤는 담당자가 지운 걸 앱이 못 모은 것처럼 보이게 하므로 거짓말이다).
    page = history_renderer.render_history_page(
        [], all_dates=storage.list_run_dates(), deleted_runs=storage.list_deleted_runs(),
        latest_key=storage.latest_run_key(),
    )
    day_part = page.split(f'data-date="{two_days_ago}"', 1)[1].split("</details>\n<details", 1)[0]
    assert f"{two_days_ago}|09:00" in day_part, "살아 있는 회차까지 사라졌음"
    assert "17:00" not in day_part, "지운 회차 자리에 아직 줄이 있음"
    assert "삭제함" not in page and "slot-gone" not in page, "「삭제함」 줄이 아직 남아 있음"

    assert storage.delete_run(two_days_ago, "09:00", stamp="20260101120000")
    assert not storage.run_exists("17:00", two_days_ago), "지운 회차 파일이 그대로 남아 있음"
    assert not storage.delete_run(two_days_ago, "17:00"), "이미 지운 회차를 또 지웠다고 함"
    deleted = storage.list_deleted_runs()
    assert {(e["date"], e["run_slot"]) for e in deleted} == {(two_days_ago, "17:00"), (two_days_ago, "09:00")}
    assert all(e["run_slot"] != "19:30" for e in deleted)
    # 조회 쪽(인덱스)에서도 빠져야 한다 — 보관함·엑셀·홈 추이가 전부 이걸 본다.
    assert all(m["run_at"][:10] != two_days_ago for m in storage.list_run_meta())

    # ③-2 그 날짜의 회차를 다 지우면 날짜 줄째 사라진다.
    page = history_renderer.render_history_page(
        [], all_dates=storage.list_run_dates(), deleted_runs=deleted, latest_key=storage.latest_run_key()
    )
    assert f'data-date="{two_days_ago}"' not in page, "지운 회차만 남은 날짜 줄이 안 사라짐"

    # ① 오늘 회차가 생기면 오늘은 "today"로 잠기고, 어제 19:30은 이제 지울 수 있다.
    storage.save_run("09:30", [_article("c")], run_at=f"{today}T09:31:00")
    try:
        storage.delete_run(today, "09:30")
        raise AssertionError("오늘 회차가 지워졌음 — 스케줄러가 곧바로 다시 수집한다")
    except storage.RunLockedError as e:
        assert e.reason == "today", e.reason
    assert storage.run_lock_reason(yesterday, "19:30") is None

    # [추가: 2026-09-15] 날짜 줄 🗑 — 지울 수 있는 회차가 있는 날만, 그 회차 키를 구워 넣는다.
    # 오늘은 없다. 되살리는 길은 보관함이 아니라 「삭제 기록」이라 그 링크가 한 번 선다.
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
    assert "되살리기" not in page.split("<script", 1)[0], "보관함 본문에 되살리기가 남아 있음"
    assert 'class="deleted-link' in page, "「삭제 기록」으로 가는 링크가 없음"

    # [추가: 2026-10-12] 되살릴 기간이 지나면 껍데기로 — 줄은 「삭제 기록」에 남고 되살리기만 빠진다.
    assert storage.purge_expired_deleted_runs(restore_days=36500, now=now) == 0, "기간 안인데 껍데기가 됐음"
    assert storage.purge_expired_deleted_runs(restore_days=0, now=now + timedelta(days=1)) == 2
    purged = storage.list_deleted_runs()
    assert len(purged) == 2 and not any(e["restorable"] for e in purged), "껍데기가 목록에서 빠지거나 되살릴 수 있다고 함"
    assert storage.deleted_run_article_count(purged[0]["trash_name"]) is not None, "껍데기에 건수가 없음"
    assert storage.restore_run(purged[0]["trash_name"]) is None, "껍데기를 되살렸음"
    # 껍데기여도 그 자리에 「수집 기록 없음」이 돌아오면 안 된다(지운 사실이 거짓이 된다).
    page = history_renderer.render_history_page(
        [], all_dates=storage.list_run_dates(), deleted_runs=storage.list_deleted_runs(),
        latest_key=storage.latest_run_key(),
    )
    assert f'data-date="{two_days_ago}"' not in page, "껍데기 날짜가 보관함에 되살아남"

    # 그 상태를 되돌려 아래 되살리기 검사를 이어 간다.
    assert storage.empty_deleted_runs() == 2, "비우기가 껍데기를 안 지움"
    assert storage.list_deleted_runs() == []
    storage.save_run("09:00", [_article("a")], run_at=f"{two_days_ago}T09:01:00")
    storage.save_run("17:00", [], run_at=f"{two_days_ago}T17:01:00")
    assert storage.delete_run(two_days_ago, "17:00", stamp="20260101120000")
    assert storage.delete_run(two_days_ago, "09:00", stamp="20260101120000")
    deleted = storage.list_deleted_runs()

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

    print("17) 정기 회차 삭제·되살리기: 통과 (오늘·가장 최근 잠금, 보관함엔 흔적 없음, 기한 뒤 껍데기·비우기, 되살리기·충돌 방어)")


def test_adhoc_card_delete_and_restore():
    """수시 보관함 카드 삭제 — 보관 기한으로 옮긴 것은 삭제 기록에 안 뜨고, 지운 회차는
    보관함에서 **흔적 없이** 사라지고(초록 안내 한 줄만), 되살리기가 동작하는지.

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

            # [수정: 2026-10-12] 지운 카드는 목록에서 흔적 없이 사라지고, 그 자리를 초록
            # 안내 한 줄이 받는다 — 「삭제함」 줄도 맨 아래 「최근 삭제」도 없다.
            page = archive_renderer.render_archive_page(preset="all", gone="「재경위」 12:00 기준 회차를")
            assert f'data-card-id="{gone_a["id"]}"' not in page, "지운 카드가 아직 목록에 있음"
            assert "삭제함" not in page and "recent-del" not in page, "「삭제함」·「최근 삭제」가 남아 있음"
            assert 'class="ok-note"' in page and "「재경위」 12:00 기준 회차를 지웠어요" in page, "초록 안내가 없음"
            assert 'href="/deleted"' in page, "「삭제 기록」으로 가는 길이 없음"
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

            # 그 날짜의 회차를 다 지우면 날짜 줄째 사라진다(「삭제함」 묶음을 안 남긴다).
            assert card.delete_card(kept["id"], stamp="20260101130000")
            page = archive_renderer.render_archive_page(preset="all", q="재경")
            assert f'data-day-label="{day_label}"' not in page, "지운 회차만 남은 날짜 줄이 안 사라짐"
            assert card.restore_card(next(c["_trash_name"] for c in card.list_deleted_cards() if c["id"] == kept["id"]))

            # [추가: 2026-10-12] 기한이 지나면 껍데기로 — 삭제 기록에 줄은 남고 되살리기만 빠진다.
            assert card.purge_expired_deleted_cards(restore_days=36500) == 0, "기간 안인데 껍데기가 됐음"
            assert card.purge_expired_deleted_cards(restore_days=0, now=datetime.now()) >= 1
            shells = [c for c in card.list_deleted_cards() if not c["_restorable"]]
            assert shells and all(isinstance(c.get("article_count"), int) for c in shells), "껍데기에 건수가 없음"
            assert card.restore_card(shells[0]["_trash_name"]) is None, "껍데기를 되살렸음"
            # 보관 기한으로 앱이 옮긴 것(.expired-)은 담당자 눈에 안 보이니 EXPIRED_TRASH_KEEP_DAYS
            # 뒤에 같이 지운다 — 치우려고 옮긴 건데 디스크에 영원히 남으면 옮긴 의미가 없다.
            assert list((adhoc_dir / "trash").glob("*.expired-*.json")), "기한 만료 카드가 벌써 사라졌음"
            card.purge_expired_deleted_cards(restore_days=36500, now=datetime.now() + timedelta(days=40))
            assert not list((adhoc_dir / "trash").glob("*.expired-*.json")), "기한 만료 카드가 안 치워짐"
            assert card.empty_deleted_cards() >= 1 and card.list_deleted_cards() == [], "비우기가 안 됨"

            # 아래 되살리기 검사를 위해 다시 만들어 지운다.
            again = card.new_card(issue["id"], "재경위", "11:00", "12:00", ["재경위"], now=base, raw=False)
            assert card.delete_card(again["id"], stamp="20260101140000")
            listed = card.list_deleted_cards()

            # 되살리기 — 같은 id가 이미 있으면 덮어쓰지 않고, 형식 밖 이름은 받지 않는다.
            restored = card.restore_card(listed[0]["_trash_name"])
            assert restored == again["id"] and card.load_card(restored) is not None
            assert card.restore_card(listed[0]["_trash_name"]) is None
            assert card.restore_card("../issues.json") is None

    print("18) 수시 카드 삭제·되살리기: 통과 (이름 없는 사안 표시, 보관함엔 흔적 없이 초록 안내, 기한 뒤 껍데기·비우기, 되살리기)")


def test_adhoc_past_date_collect():
    """수시 수집을 지난 날짜로 — 어제는 없던 검색어로 어제 기사를 모을 수 있나.

    [추가: 2026-10-21] 네이버 검색 API에는 기간 칸이 없고 최신순으로만 주므로, "그 날짜가
    아니면 멈춘다"는 판정 한 곳(app.naver_api._compare_collect_day)이 곧 "어느 날을
    모으나"를 정한다. 검색어를 언제 등록했는지는 네이버가 모르기 때문에, 오늘 처음 만든
    검색어로도 지난 날 기사가 그대로 나온다.

      1) **날짜 판정은 세 갈래여야 한다** — 참/거짓으로 묶으면 목록 맨 앞의 "오늘" 기사에서
         그대로 멈춰 0건이 나오고, 그게 "그날 기사가 없었다"와 똑같이 보인다(실측 재현).
      2) new_card(collect_date=)는 날짜만 과거로 두고 id·created_at은 now 그대로 — 카드
         id가 파일명 사전순 = 만든 순서라 과거로 끌려가면 목록 정렬이 뒤집힌다.
      3) 지난 날짜 카드도 수집된다(옛 자정 잠금은 "검색이 늘 오늘이었다"는 사라진 이유였다).
         미래·한도 밖 날짜는 거부한다.
      4) 1000건 상한에 걸려 못 닿은 구간은 카드에 적히고(capped_keywords) 화면에 **빨간**
         경고로 선다 — 한 건도 못 받은 경우(reached=None)와 일부만 받은 경우를 가른다.
      5) 지난 날짜 원본 배너는 더 이상 "다시 수집하면 오늘 날짜로 검색됩니다"라고 하지
         않는다(그 문장이 거짓이 됐다) — 재수집 칸도 잠기지 않는다.
    """
    _require_isolation()
    from app import naver_api
    from app.adhoc import card, collector as adhoc_collector, renderer as adhoc_renderer

    kst = naver_api._KST
    now = datetime.now().replace(second=0, microsecond=0)
    today = now.strftime("%Y-%m-%d")
    yday = (now - timedelta(days=1)).strftime("%Y-%m-%d")

    # 1) 세 갈래 판정 — 오늘 기사는 "새것"(건너뜀)이고 그제 기사는 "오래됨"(중단)이다.
    assert naver_api._compare_collect_day(naver_api.kst_at(today, "09:00"), yday) > 0, \
        "지난 날짜를 모으는데 오늘 기사를 '오래된 기사'로 봐서 첫 건에서 멈춘다"
    assert naver_api._compare_collect_day(naver_api.kst_at(yday, "09:00"), yday) == 0
    assert naver_api._compare_collect_day(
        naver_api.kst_at((now - timedelta(days=2)).strftime("%Y-%m-%d"), "23:00"), yday) < 0
    # day를 안 주면 예전 그대로 "오늘"이다.
    assert naver_api._compare_collect_day(datetime.now(kst)) == 0

    with tempfile.TemporaryDirectory() as tmp:
        adhoc_dir = Path(tmp)
        (adhoc_dir / "cards").mkdir()
        (adhoc_dir / "trash").mkdir()
        with patch.object(card, "CARDS_DIR", adhoc_dir / "cards"), \
             patch.object(card, "TRASH_DIR", adhoc_dir / "trash"), \
             patch.object(card, "ISSUES_FILE", adhoc_dir / "issues.json"):
            # 2) 날짜만 과거, id·created_at은 지금
            c = card.new_card("", "", "00:00", "23:59", ["세법개정"], now=now, collect_date=yday)
            assert c["collect_date"] == yday
            assert c["id"].startswith(now.strftime("%Y%m%d")), "카드 id가 과거로 끌려갔다"
            assert card.new_card("", "", "00:00", "23:59", ["x"], now=now)["collect_date"] == today
            for bad in (f"{now.year + 1}-01-01", "20261020"):
                try:
                    card.new_card("", "", "00:00", "23:59", ["x"], now=now, collect_date=bad)
                except card.AdhocCardError:
                    pass
                else:
                    raise AssertionError(f"잘못된 날짜를 받아들임: {bad}")

            # 3) 지난 날짜 카드도 수집된다 — 그 날짜로 검색이 걸리는지 day 인자로 확인한다.
            seen = {}

            def _fake(keywords, after=None, before=None, include_unparsed_dates=False, day=None):
                seen["day"] = day
                seen["window"] = (after, before)
                return ({keywords[0]: [{
                    "outlet": "한국일보", "title": "세법개정안 국회 제출", "url": "https://n.news.naver.com/a/1",
                    "summary": "요약", "pub_date": naver_api.kst_at(yday, "15:30").isoformat(),
                }]}, [], [])

            with patch.object(adhoc_collector, "search_keywords", _fake):
                adhoc_collector.run_collect(c["id"], now=now, log=False)
            assert seen["day"] == yday, f"수집이 그 날짜로 검색하지 않음: {seen['day']}"
            assert seen["window"][0] == naver_api.kst_at(yday, "00:00"), "창이 오늘 날짜 위에 섰다"
            saved = card.load_card(c["id"])
            assert len(saved["articles"]) == 1 and not saved.get("capped_keywords")

            future = dict(saved, collect_date=f"{now.year + 1}-01-01")
            old = dict(saved, collect_date=(now - timedelta(days=9999)).strftime("%Y-%m-%d"))
            for bad_card, why in ((future, "미래"), (old, "한도 밖")):
                try:
                    adhoc_collector._validate_collectable(bad_card, now)
                except adhoc_collector.AdhocCollectError:
                    pass
                else:
                    raise AssertionError(f"{why} 날짜 수집을 막지 않음")

            # 4) 못 닿은 구간 — 한 건도 못 받은 경우와 일부만 받은 경우를 가른다.
            got = {"넓은말": [], "세법개정": [{"pub_date": naver_api.kst_at(yday, "13:20").isoformat()}]}
            shortfall = adhoc_collector._shortfall(
                got, {"넓은말", "세법개정"}, {"keywords": ["넓은말", "세법개정"]})
            assert {i["keyword"]: i["reached"] for i in shortfall} == {
                "넓은말": None, "세법개정": naver_api.kst_at(yday, "13:20").isoformat()
            }, f"못 닿은 구간을 잘못 적음: {shortfall}"
            assert adhoc_collector._shortfall(got, set(), {"keywords": []}) == []

            warn = adhoc_renderer._capped_warn_html(dict(saved, capped_keywords=shortfall))
            assert 'class="warn capped"' in warn, "못 닿음 경고가 빨간 .warn이 아니다"
            assert "stale" not in warn, "못 닿음은 사실 알림(앰버)이 아니라 불완전한 결과다"
            assert "한 건도" in warn and "13:20" in warn
            assert adhoc_renderer._capped_warn_html(saved) == "", "닿았는데 경고가 떴다"

            # 5) 새 수집 폼의 날짜 칸 + 지난 날짜 배너 문구
            form = adhoc_renderer.render_new_card_page()
            assert 'name="collect_date"' in form and f'max="{today}"' in form
            assert "setCollectDate(1)" in form, "「어제」 칩이 없다"
            # 칩이 "지금 고른 날짜"를 말한다(시안 A안) — 누르는 쪽과 불 켜는 쪽이 **같은
            # 날짜 셈**(dateMinusDays)을 봐야 "눌렀는데 안 켜지는" 어긋남이 안 생긴다.
            assert form.count('data-days="0"') == 1 and form.count('data-days="1"') == 1
            assert ".qchip.on" in form, "켜진 칩 CSS가 없다 — 눌러도 모양이 안 변한다"
            assert "syncDateChips();" in form, "화면을 열 때 「오늘」이 안 켜진다"
            # 시간 줄의 빠른 칩(0시부터·최근 N시간)은 켜짐이 없는 동작 버튼이다.
            assert 'data-days' not in form.split("0시부터")[0].rsplit("<span class=\"n-quick\">", 1)[-1]
            page = adhoc_renderer.render_card_page(saved)
            assert "오늘 날짜로 검색" not in page, "거짓이 된 옛 문구가 남아 있다"
            assert f"다시 수집해도 <b>{yday}</b> 날짜로 검색돼요" in page
            assert 'id="recol-start" name="window_start" value="00:00" disabled' not in page

            # 6) **확정본으로 보낼 수 있어야 쓸모가 있다** — 모으기만 되고 보고서를 못
            #    만들면 반쪽이다. 보내는 곳은 **원본과 같은 날짜의 확정본**이라 날짜가
            #    섞이지 않는다(옛 자정 잠금이 막으려던 바로 그것).
            from app.adhoc import routes as adhoc_routes, undo as adhoc_undo

            with patch.object(adhoc_undo, "UNDO_DIR", adhoc_dir / "undo"):
                (adhoc_dir / "undo").mkdir(exist_ok=True)
                redirects = []

                class _H:
                    def _redirect(self, target):
                        redirects.append(target)

                adhoc_routes._handle_send_to_bundle(_H(), {
                    "id": [c["id"]], "urls": ["https://n.news.naver.com/a/1"],
                    "bundle": ["__new__"], "bundle_name": ["세법개정 점검"],
                })
                assert redirects and "error=" not in redirects[-1], f"보내기가 막힘: {redirects}"
                day_bundles = card.bundles_for_date(yday)
                assert len(day_bundles) == 1, "지난 날짜 원본에서 확정본으로 못 보냄"
                assert day_bundles[0]["collect_date"] == yday, \
                    "확정본이 오늘 날짜로 섰다 — 어제 기사가 오늘 보고서에 담긴다"
                assert len(day_bundles[0]["articles"]) == 1
                assert card.bundles_for_date(today) == [], "오늘 확정본이 생겼다"
                assert card.sent_index(c["id"], day_bundles), "원본에 ✓ 보냄이 안 붙었다"

            page = adhoc_renderer.render_card_page(card.load_card(day_bundles[0]["id"]))
            assert "기사를 더 보낼 수 없어요" not in page, "거짓이 된 옛 확정본 배너가 남아 있다"

    print("55) 수시 지난 날짜 수집: 통과 (세 갈래 날짜 판정, 날짜만 과거, 못 닿음 빨간 경고, 폼 날짜 칸, 같은 날짜 확정본으로 보내기)")


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


def test_adhoc_nav_today_only():
    """수시 상단바 「원본」·「확정본」은 **오늘 만든 카드**만 가리킨다.

    예전엔 날짜를 안 보고 가장 최근 카드로 가서, 오늘 확정본이 없으면 **이틀 전 확정본**이
    열리고 그 화면은 열리자마자 "여기엔 기사를 더 보낼 수 없다"는 배너부터 띄웠다 — 갈 수
    없는 곳으로 데려다 놓고 왜 못 하는지 설명하는 모양이다. 홈 흐름도는 처음부터 오늘
    카드만 봤으므로(landing_renderer._todays_adhoc_state) 같은 칸이 두 자리에서 다르게
    동작하고 있었다.

    지난 카드는 **지우지도 숨기지도 않는다** — 바뀌는 건 상단바가 어디로 가느냐 하나이고,
    그래서 빈 화면이 지난 카드로 가는 길을 눈앞에 들고 있어야 한다.
    """
    _require_isolation()

    from app.adhoc import card, renderer, routes

    class _H:
        def __init__(self):
            self.body = ""
            self.code = None

        def _respond(self, body):
            self.body = body

        def send_response(self, code):
            self.code = code

        def end_headers(self):
            pass

    with tempfile.TemporaryDirectory() as tmp:
        adhoc_dir = Path(tmp)
        (adhoc_dir / "cards").mkdir()
        (adhoc_dir / "trash").mkdir()
        with patch.object(card, "CARDS_DIR", adhoc_dir / "cards"), \
             patch.object(card, "TRASH_DIR", adhoc_dir / "trash"), \
             patch.object(card, "ISSUES_FILE", adhoc_dir / "issues.json"):
            now = datetime.now()
            two_days_ago = now - timedelta(days=2)
            issue = card.get_or_create_issue("증안펀드", now=two_days_ago)
            old_raw = card.new_card(
                issue["id"], "증안펀드", "00:00", "17:41", ["증권시장안정펀드"], now=two_days_ago,
            )
            old_bundle = card.new_bundle_card(
                "증안펀드", now=two_days_ago, issue_id=issue["id"],
                basis_time="17:41", source_card_id=old_raw["id"],
            )

            # 1) 오늘 카드가 하나도 없다 — 상단바는 지난 카드가 아니라 빈 화면을 가리킨다.
            assert renderer._latest_href("bundle") == "/adhoc/card?kind=bundle", \
                "오늘 확정본이 없는데 상단바가 지난 확정본으로 간다"
            assert renderer._latest_href("collect") == "/adhoc/card?kind=collect", \
                "오늘 원본이 없는데 상단바가 지난 원본으로 간다"
            nav = renderer.page_nav("bundle")
            assert old_bundle["id"] not in nav and old_raw["id"] not in nav, \
                "상단바에 지난 카드 id가 남아 있다"
            # 흐린 자리(is-empty)로 두지 않는다 — 갈 곳이 언제나 있다.
            assert "is-empty" not in nav, "수시 상단바에 흐린 칸이 남아 있다"

            # 2) 빈 화면이 지난 카드로 가는 길을 들고 있다 — 화면에 찍히던 이름 그대로.
            page = renderer.render_empty_kind_page("bundle")
            assert "오늘 만든 확정본이 없어요" in page
            assert f'/adhoc/card?id={old_bundle["id"]}' in page, \
                "빈 화면에 지난 확정본으로 가는 길이 없다"
            assert "수시 보관함" in page, "지난 확정본이 어디 있는지 안 적혀 있다"
            raw_page = renderer.render_empty_kind_page("collect")
            assert "오늘 모은 원본이 없어요" in raw_page
            assert f'/adhoc/card?id={old_raw["id"]}' in raw_page, \
                "빈 화면에 지난 원본으로 가는 길이 없다"
            assert "지난 수집" in raw_page, "지난 원본이 어디 있는지 안 적혀 있다"

            # 3) 라우트 — id 없이 kind만 오면 빈 화면이고 404가 아니다.
            h = _H()
            assert routes.handle_get(h, "/adhoc/card?kind=bundle") is True
            assert h.code is None and "오늘 만든 확정본이 없어요" in h.body
            # 모르는 kind는 예전처럼 404 — 빈 화면이 아무 주소나 받아주면 안 된다.
            h = _H()
            assert routes.handle_get(h, "/adhoc/card?kind=zzz") is True
            assert h.code == 404

            # 4) 오늘 원본이 생기면 상단바가 그리로 가고, 확정본 빈 화면의 첫 단추도 그 원본이다
            #    (확정본은 원본에서 처음 보낼 때 저절로 생긴다 — 그게 다음 할 일이다).
            today_raw = card.new_card(
                issue["id"], "증안펀드", "00:00", "16:05", ["증안펀드"], now=now,
            )
            assert renderer._latest_href("collect") == f'/adhoc/card?id={today_raw["id"]}'
            assert renderer._latest_href("bundle") == "/adhoc/card?kind=bundle", \
                "오늘 원본이 생겼다고 확정본 칸이 따라가면 안 된다"
            page = renderer.render_empty_kind_page("bundle")
            assert f'오늘 원본 열기' in page and today_raw["id"] in page, \
                "확정본 빈 화면이 오늘 원본을 안 가리킨다"

            # 5) 오늘 확정본이 생기면 그리로 간다.
            today_bundle = card.new_bundle_card(
                "증안펀드", now=now, issue_id=issue["id"],
                basis_time="16:05", source_card_id=today_raw["id"],
            )
            assert renderer._latest_href("bundle") == f'/adhoc/card?id={today_bundle["id"]}'

    print("55) 수시 상단바 오늘 카드만: 통과 (지난 카드로 안 감, 빈 화면이 지난 카드 길을 들고 있음, "
          "kind 라우트, 확정본 빈 화면은 오늘 원본을 가리킴)")


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
    # 서술어 뒤가 사람 직함이 아니라 **정당 이름**인 캡션 제목 — 2026-10-08 뉴스핌 제보 실물.
    # 요약이 "…퇴장하고... "처럼 "고 있다."에 닿기 전에 잘려 날짜 도장·기자 이메일이 통째로 없다.
    walkout = ("8일 오전 서울 여의도 국회 재정경제기획위원회에서 열린 기획예산처에 대한 국정감사에서 국민의힘 "
               "의원들이 노트북 앞에 피켓을 붙인 가운데 감사개시가 지연되자 더불어민주당 의원들이 퇴장하고... ")
    for title in ("재경위 국감 퇴장하는 더불어민주당", "재경위 퇴장하는 국민의힘"):
        assert looks_like_photo_caption({"title": title, "summary": walkout}), f"정당 이름으로 끝나는 캡션 제목을 놓침: {title}"
    assert not looks_like_photo_caption({"title": "종부세 제외 기준 두고 ‘부자감세’ 지적받는 민주당", "summary": body}), \
        "약칭 정당명으로 끝나는 일반 기사 제목을 사진으로 잘못 잡음"
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
            # 사안은 이제 확정본 이름을 지을 때 태어난다 — 이름이 다르면 다른 보고서이고,
            # 같은 이름의 다른 시각 확정본으로 옮기는 것도 담당자가 할 수 있는 일이다.
            assert f'data-bundle="{dst["id"]}"' in page, "오늘 다른 확정본이 메뉴에 없음"
            assert f'data-bundle="{same["id"]}"' in page, "같은 이름 확정본이 메뉴에서 빠짐"
            assert "moveArticleTo(this)" in page and "bulkMoveTo(this)" in page
            assert "bulk-group-select" not in page and "moveArticle(this)" not in page, "옛 드롭다운이 남아 있음"

            calls = []

            class _Handler:
                def _redirect(self, target):
                    calls.append(target)

            # 3) 자기 자신으로는 거부
            adhoc_routes._handle_move_to_bundle(_Handler(), {"id": [src["id"]], "bundle": [src["id"]], "urls": ["https://a.x/1"]})
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
            assert adhoc_undo.peek_label(dst["id"]) == "다른 확정본에서 옮겨오기"

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

            # 2) 처음 보낼 때 이름을 지으면 그 이름으로 확정본이 생긴다(사안도 여기서 태어난다)
            adhoc_routes._handle_send_to_bundle(
                _Handler(), {"id": [first["id"]], "bundle": ["__new__"],
                             "bundle_name": ["인사청문회"], "urls": ["https://f.x/1"]})
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


def test_adhoc_screen_name():
    """원본의 이름은 「그 원본이 한 검색」이다 — 사안명도 #N도 아니다.

      1) screen_name = 검색어. 검색어까지 같은 원본이 오늘 둘 이상이면 끝 시각이 붙는다
      2) 머리줄은 개수로 줄인다(ADHOC_HEAD_KEYWORDS_MAX, 「외 N개」) — 탭은 폭으로 줄인다
      3) 화면·브라우저 탭 제목·보고서 첫 줄이 같은 값, ✏️·#N은 없다
      4) 「✓ 보냄」은 이 원본이 보낸 기사만 — 옆 판이 보낸 기사는 「처리 안 함」
      5) 자동 확정본은 「내가 보낸 것」으로 간다. 보낸 적이 없으면 이름부터 묻는다
      6) 확정본 이름·출처 칩에 #N이 없고, 다시 수집하면 직전 이름을 채워 준다
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

            # 검색어가 같고 시간창만 다른 두 판 + 검색어가 다른 판
            first = card.new_card(issue["id"], "인사청문회", "09:00", "13:05", ["청문회"], now=now)
            first["articles"] = [art("https://v.x/1"), art("https://v.x/2")]
            card.save_card(first)
            second = card.new_card(issue["id"], "인사청문회", "10:00", "14:05", ["청문회"],
                                   now=now + timedelta(seconds=1))
            second["articles"] = [art("https://v.x/1"), art("https://v.x/3")]
            card.save_card(second)
            third = card.new_card(issue["id"], "인사청문회", "09:00", "13:05", ["의혹", "후보자"],
                                  now=now + timedelta(seconds=2))

            # 1) 검색어가 이름이고, 겹치는 판에만 끝 시각이 붙는다
            assert card.screen_name(first) == "청문회 13:05"
            assert card.screen_name(second) == "청문회 14:05"
            assert card.screen_name(third) == "의혹 · 후보자", "검색어가 혼자인데 시각이 붙음"
            # 사안명을 고쳐도 이름이 안 흔들린다 — 더는 이름으로 가르지 않는다
            renamed = card.load_card(third["id"])
            renamed["report_title"] = "딴 이름"
            card.save_card(renamed)
            assert card.screen_name(card.load_card(third["id"])) == "의혹 · 후보자"

            # 2) 머리줄은 개수로 줄인다
            many = card.new_card(issue["id"], "인사청문회", "09:00", "13:05",
                                 ["가", "나", "다", "라"], now=now + timedelta(seconds=3))
            assert card.keywords_label(many) == "가 · 나 · 다 외 1개"
            assert card.keywords_text(many) == "가 · 나 · 다 · 라", "툴팁엔 전부 나와야 한다"
            assert card.keywords_label(first) == "청문회", "안 넘치는데 줄임"

            # 3) 화면·탭 제목·보고서 첫 줄이 같은 값
            page = adhoc_renderer.render_card_page(card.load_card(second["id"]))
            assert "<title>원본 · 청문회 14:05" in page, "브라우저 탭 제목이 검색어가 아님"
            assert "기준(청문회)" in page, "머리줄 괄호 안이 검색어가 아님"
            assert 'class="ver-tag"' not in page and "청문회 #" not in page, "판 번호가 남아 있음"
            assert 'id="report-title-input"' not in page, "원본에 이름 고치는 칸이 남아 있음"
            assert adhoc_renderer.report_header_text(card.load_card(second["id"])).endswith("(청문회)"), \
                "보고서 첫 줄이 사안명으로 나감"
            assert adhoc_renderer.download_filename(card.load_card(many["id"])).startswith(
                f"{today}_수시모니터링_가 외3_원본"), "파일명이 첫 검색어 + 외N이 아님"

            # 3b) 이름 창과 그 뒤를 덮는 막은 **닫혀 있을 때 진짜로 사라져야 한다.**
            # 이 앱엔 공용 `.hidden`이 없어 규칙을 빠뜨리면 막(inset:0)이 늘 떠 있고,
            # 화면의 모든 클릭이 거기 먹혀 **카드 화면이 통째로 먹통**이 된다.
            assert "#bundle-namer.hidden" in page and "#namer-back.hidden" in page, \
                "이름 창·막을 숨기는 CSS가 없음 — 막이 화면을 덮어 모든 클릭을 먹는다"

            # 3c) 기본 「확정본으로」 단추는 메뉴와 같은 곳을 가리킨다 — 보낸 적이 없으면
            # __auto__는 서버가 거부하므로 단추만 그걸 들고 있으면 먹통으로 보인다.
            assert 'class="send-go" data-bundle="__new__"' in page, \
                "보낸 적 없는 원본인데 단추가 __auto__를 가리킴"

            calls = []

            class _Handler:
                def _redirect(self, target):
                    calls.append(target)

            # 5) 보낸 적이 없으면 __auto__는 거부한다 — 이름을 먼저 짓는 자리다
            adhoc_routes._handle_send_to_bundle(
                _Handler(), {"id": [first["id"]], "bundle": ["__auto__"], "urls": ["https://v.x/1"]})
            assert card.bundles_for_date(today) == [], "이름 없이 확정본이 생김"
            assert "error=" in calls[-1]

            # 이름을 지어 보내면 그 이름으로 생기고 사안도 거기서 태어난다
            adhoc_routes._handle_send_to_bundle(
                _Handler(), {"id": [first["id"]], "bundle": ["__new__"],
                             "bundle_name": ["청문회 종합"], "urls": ["https://v.x/1"]})
            bundles = card.bundles_for_date(today)
            assert len(bundles) == 1 and bundles[0]["report_title"] == "청문회 종합"
            assert bundles[0].get("issue_id"), "확정본이 사안을 못 물려받음"
            assert card.find_issue_by_name("청문회 종합") is not None, "사안이 안 생김"
            assert "#" not in card.bundle_label(bundles[0]), "확정본 이름에 판 번호가 남음"

            # 5b) 두 번째부터는 「내가 보낸 확정본」으로 간다
            assert card.default_bundle_for(card.load_card(first["id"]), bundles)["id"] == bundles[0]["id"]
            assert card.default_bundle_for(card.load_card(second["id"]), bundles) is None, \
                "남이 만든 확정본을 자동으로 이어 씀"
            assert card.last_bundle_name_for(card.load_card(first["id"]), bundles) == "청문회 종합"

            # 4) 「✓ 보냄」은 이 원본이 보낸 기사만이고, 옆 원본이 보낸 건 「이미 보냄」으로
            #    따로 선다 — 같은 기사가 여러 원본에 걸려도 「처리 안 함」과 안 섞인다.
            day = card.bundles_for_date(today)
            assert card.raw_row_states(card.load_card(first["id"]), day).get(
                "https://v.x/1", ("",))[0] == "sent"
            _second = card.load_card(second["id"])
            _st = card.raw_row_states(_second, day)["https://v.x/1"]
            assert _st[0] == "sent_other", "옆 원본이 보낸 기사가 「처리 안 함」으로 섞임"
            assert _st[1] == "청문회 종합", _st  # bundle_label은 시각을 안 붙인다
            assert _st[2] == first["id"], "셋째 값은 보낸 원본 카드 id여야 한다"
            # 내가 보낸 게 아니므로 「✓ 보냄」도, 그 줄의 취소도 없다 — 취소·숨김 되돌리기가
            # 전부 「내가 보낸 것」 기준이라 달아 두면 눌러도 아무 일이 안 일어난다.
            _second_page = adhoc_renderer.render_card_page(_second)
            # CSS 주석에도 걸리지 않게 마크업으로 센다
            assert 'class="send-split sent-split"' in _second_page \
                and 'class="se-label"' in _second_page, "옆 원본이 보낸 기사에 회색 표시가 안 붙음"
            assert "「청문회 13:05」 원본이 「청문회 종합」 확정본으로 이미 보낸" in _second_page, \
                "말풍선이 보낸 원본의 검색어 이름을 안 적음"
            assert "rawUnsend(this)" not in _second_page, "내가 한 적 없는 보냄에 취소 버튼이 달림"
            # 그래도 **일부러** 다른 확정본으로 보내는 길은 ▾로 열려 있어야 한다
            assert "그래도 다른 확정본으로 보내기" in _second_page, "보낼 길이 통째로 막힘"
            # 「모든 기사 확정본으로」가 보낼 목록에서도 빠진다 — 확인창 건수와 실제가 같아야 한다
            assert "https://v.x/1" not in card.open_raw_urls(_second, day)
            assert "https://v.x/3" in card.open_raw_urls(_second, day)

            # 4b) 그래도 같은 확정본으로 보내면 **이유를 적어 돌려보낸다** — 조용히 0건으로
            #     끝나면 화면이 그대로 돌아와 버튼이 고장 난 것처럼 보인다.
            adhoc_routes._handle_send_to_bundle(
                _Handler(), {"id": [second["id"]], "bundle": [bundles[0]["id"]],
                             "urls": ["https://v.x/1"]})
            assert "error=" in calls[-1] and "%EC%9D%B4%EB%AF%B8" in calls[-1], calls[-1]
            assert len(card.load_card(bundles[0]["id"])["articles"]) == 1, "중복이 들어감"

            # 4c) 일부러 **다른** 확정본으로 보내는 길은 열려 있다(조건이 다른 두 보고서)
            adhoc_routes._handle_send_to_bundle(
                _Handler(), {"id": [second["id"]], "bundle": ["__new__"],
                             "bundle_name": ["청문회 속보"], "urls": ["https://v.x/1"]})
            assert len(card.bundles_for_date(today)) == 2, "다른 확정본으로 보내는 길이 막힘"
            assert card.raw_row_states(card.load_card(second["id"]),
                                       card.bundles_for_date(today))["https://v.x/1"][0] == "sent", \
                "내가 보낸 뒤에도 「이미 보냄」에 머묾"

            # 6) 확정본 출처 칩은 그 원본의 검색어다
            bundle_page = adhoc_renderer.render_card_page(card.load_card(bundles[0]["id"]))
            assert "청문회 13:05 1건" in bundle_page, "출처 칩이 검색어가 아님"
            # 오류로 돌려보낸 건 **일부러 확인한 둘뿐**이다 — 이름 없이 __auto__(5),
            # 같은 확정본에 이미 있는 기사(4b). 나머지는 조용히 성공해야 한다.
            assert sum("error=" in t for t in calls) == 2, calls
    print("43) 수시 원본 이름: 통과 (검색어가 이름, 옆 원본이 보낸 건 「이미 보냄」, 이름은 확정본에서)")


def test_adhoc_raw_filter_row():
    """수시 원본의 「거르기」 줄 — 체크 하나 + 종류 칩 셋(2026-10-22).

      1) 왼쪽은 체크(어디까지 볼까), 세로선 오른쪽은 칩(무엇이 섞여 있나)
      2) 칩 건수와 행 표식은 **같은 판정**(_raw_kinds_of)에서 나오고, 0건인 칩은 안 그린다
      3) 「등록 언론사만」은 설정의 체크 그대로고, 0곳이면 잠근 채 한 건도 안 거른다
      4) 보기 필터일 뿐 — 복사·txt는 한 건도 안 줄어든다
      5) 가려 둔 기사는 「모든 기사 확정본으로」로도 안 나간다
    """
    _require_isolation()
    from app.adhoc import card, renderer as adhoc_renderer

    with tempfile.TemporaryDirectory() as tmp:
        adhoc_dir = Path(tmp)
        for sub in ("cards", "trash", "undo"):
            (adhoc_dir / sub).mkdir()
        with patch.object(card, "CARDS_DIR", adhoc_dir / "cards"), \
             patch.object(card, "TRASH_DIR", adhoc_dir / "trash"), \
             patch.object(card, "ISSUES_FILE", adhoc_dir / "issues.json"):
            now = datetime.now().replace(microsecond=0)
            today = now.strftime("%Y-%m-%d")
            issue = card.get_or_create_issue("예산")

            def art(n, outlet, title):
                return {"outlet": outlet, "title": title, "url": f"https://r.x/{n}", "summary": "예산 기사 본문",
                        "pub_date": f"{today}T14:0{n}:00+09:00", "matched_keywords": ["예산"],
                        "group": None, "order": None, "hidden": False,
                        "added_at": f"{today}T15:05:00", "added_by": "collect"}

            raw = card.new_card(issue["id"], "예산", "11:00", "15:05", ["예산"], now=now)
            raw["articles"] = [
                art(1, "KBS", "예산안 심사 본격화"),
                art(2, "뉴스1", "[오늘의 일정] 10월 22일 국회"),
                art(3, "동네신문", "[인사] 재정경제부"),
                art(4, "동네신문", "지역 예산 토론회"),
            ]
            card.save_card(raw)
            saved = card.load_card(raw["id"])

            def page(outlets):
                # 렌더러가 **이름으로** 가져다 쓰는 load_settings를 갈아끼운다 —
                # app.settings 쪽을 갈면 이 모듈의 참조는 그대로라 아무 일도 안 일어난다.
                with patch.object(adhoc_renderer, "load_settings",
                                  lambda: {"outlet_order": list(outlets), "keyword_groups": []}):
                    return adhoc_renderer.render_card_page(saved)

            p = page(["KBS", "뉴스1"])

            # 1) 줄이 있고, 체크와 칩이 세로선으로 갈린다
            assert '<div class="frow">' in p and 'id="major-only"' in p, "원본에 거르기 줄이 없음"
            assert '<span class="fsep">' in p, "체크와 칩을 가르는 세로선이 없음"

            # 2) 칩 건수 = 행 표식 수. 판정이 두 군데로 갈리면 「칩엔 3건인데 모아 보니 2건」이 된다.
            for key, cls, _ic, name, _tip in adhoc_renderer._RAW_KINDS:
                n = sum(1 for a in saved["articles"] if key in adhoc_renderer._raw_kinds_of(a))
                rows = len(re.findall(r'<div class="article[^"]*\b' + cls + r'\b', p))
                assert rows == n, f"{name} 칩 건수({n})와 행 표식({rows})이 어긋남"
                # 칩이 있는지는 마크업으로 본다 — 화면 글자는 CSS 주석에도 적혀 나간다.
                chip = f'data-kind="{key}"'
                if n:
                    assert chip in p and f"{name} ({n})" in p, f"{name} 칩이 안 그려짐"
                else:
                    assert chip not in p, f"{name}이 0건인데 칩이 그려짐"
            assert "일정 추정 (1)" in p and "인사 (1)" in p, "말머리 기사를 못 잡음"

            # 3) 등록 언론사만 — 설정의 체크 그대로(동네신문 2건이 비등록)
            assert '등록 언론사만 <span class="n">2</span>' in p, "등록 언론사 건수가 설정과 다름"
            assert len(re.findall(r'<div class="article[^"]*\bis-nonmajor\b', p)) == 2, \
                "비등록 언론사 행 표식이 어긋남"
            locked = page([])
            assert "disabled> 등록 언론사만" in locked, "체크한 언론사가 0곳인데 칸이 안 잠김"
            assert not re.search(r'<div class="article[^"]*\bis-nonmajor\b', locked), \
                "체크한 언론사가 0곳인데 기사를 거르고 있음"

            # 4) 보기 필터일 뿐 — 가져가기 텍스트는 그대로다
            text = adhoc_renderer.build_adhoc_plain_text(saved)
            assert text.count("https://r.x/") == 4, "거르기 줄이 복사 텍스트를 줄임"

            # 5) 가려 둔 기사는 「모든 기사 확정본으로」로도 안 나간다
            assert "major-only" in p and "postWithUrls('/adhoc/card/send-to-bundle', fields, urls)" in p, \
                "가린 기사를 빼고 보내는 길이 없음"

            # 모아 보기는 종류를 갈아타는 한 벌 — 확정본의 알약도 같은 함수를 쓴다
            assert "function toggleKindGather(" in p and "ADHOC_KINDS" in p, "종류별 모아 보기 JS가 없음"
            assert "function togglePhotoGather() { toggleKindGather('photo'); }" in p, \
                "확정본 알약이 쓰던 입구가 사라짐"


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
            adhoc_routes._handle_send_to_bundle(
                _Handler(), {"id": [rid], "bundle": ["__new__"], "bundle_name": ["인사청문회"], "all_open": ["1"]})
            bundles = card.bundles_for_date(today)
            assert len(bundles) == 1 and bundles[0].get("basis_time") == "15:05", bundles
            b1 = bundles[0]["id"]
            assert sorted(a["url"] for a in bundles[0]["articles"]) == ["https://r.x/2", "https://r.x/3", "https://r.x/4"], \
                "숨김 표시한 기사까지 보냄"
            p = page()
            assert "보냄 3 · 숨김 1" in p and 'onclick="sendAllOpen(this)"' not in p, "처리할 게 없는데 전부 보내기가 남음"
            # ↩는 글자가 아니라 오늘 전체 기사와 같은 `icon("undo")` SVG다.
            assert "rawUnsend(this)" in p and "보냄 취소</span>" in p, "보낸 기사에 보냄 취소가 없음"

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
            # 다시 수집하면 기준 시각이 달라 자동 확정본이 없다 — 이름부터 다시 묻고(직전 이름이
            # 채워져 있다) 그 뒤 같은 시각에서 보낸 건 그 확정본으로 간다.
            adhoc_routes._handle_send_to_bundle(
                _Handler(), {"id": [rid], "bundle": ["__new__"], "bundle_name": ["인사청문회"], "urls": ["https://r.x/5"]})
            adhoc_routes._handle_send_to_bundle(_Handler(), {"id": [rid], "bundle": ["__auto__"], "urls": ["https://r.x/6"]})
            bundles = sorted(card.bundles_for_date(today), key=lambda x: x["created_at"])
            assert len(bundles) == 2, f"불러온 뒤 보냈는데 확정본이 {len(bundles)}개"
            b2 = [x for x in bundles if x["id"] != b1][0]
            assert b2.get("basis_time") == "16:10" and len(b2["articles"]) == 2, "같은 원본 시각에서 보낸 게 한 확정본으로 안 모임"
            assert adhoc_renderer.report_header_text(b2) == "수시 모니터링 16시 10분 기준(인사청문회)"
            assert all("error=" not in t for t in calls), calls

            # 5) 탭·보관함 — 같은 이름 확정본은 **번호**로 갈린다(시각이 아니다).
            # 시각은 머리줄이 이미 말하고(「16시 10분 기준」), 탭에서 되풀이하면 담당자가
            # 같은 이름 둘을 숫자로 대조해 가려야 한다. 보고서 첫 줄은 번호 없이 그대로다.
            # 테스트는 두 확정본을 같은 초에 만든다 — 실제로는 몇 분씩 벌어지는 일이라,
            # 만든 순서가 또렷하도록 여기서만 created_at을 떼어 놓는다.
            for _bid, _when in ((b1, "15:05:00"), (b2["id"], "16:10:00")):
                _c = card.load_card(_bid)
                _c["created_at"] = f"{today}T{_when}"
                card.save_card(_c)
            bp = adhoc_renderer.render_card_page(card.load_card(b2["id"]))
            assert "인사청문회 (2)" in bp, "같은 이름 확정본에 번호가 안 붙음"
            assert "인사청문회 15:05" not in bp and "인사청문회 16:10" not in bp, "화면 이름에 시각이 남음"
            assert card.bundle_label(card.load_card(b1)) == "인사청문회", "첫 확정본에 번호가 붙음"
            assert adhoc_renderer.report_header_text(card.load_card(b2["id"])) == (
                "수시 모니터링 16시 10분 기준(인사청문회)"), "보고서 첫 줄에 번호가 샘"
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

    # 홈 카드를 그리는 쪽(landing_renderer)도 따로 키를 확인한다 — 여기를 같이 막지
    # 않으면 예전처럼 실제 .env의 ANTHROPIC_API_KEY에 기대게 된다.
    with patch.object(negative_guess, "_judge", fake_judge), \
         patch.object(negative_guess, "llm_is_configured", lambda: True), \
         patch.object(landing_renderer, "llm_is_configured", lambda: True), \
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

      1) **오늘** 이 회차보다 앞 회차 메모만(어제는 안 가져온다), 이어진 같은 문구는 한 줄로
      1-1) 오늘 앞 회차가 없으면 빈 목록(첫 회차엔 칸이 통째로 안 그려진다)
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
        # 오늘(9/22) 앞 회차만 — 어제(9/21)는 안 가져온다
        rows = mkn.past_notes(("2026-09-22", "14:00"))
        assert rows == [("2026-09-22", ["11:00"], "유류세 인하 연장")], rows
        # 오늘 앞 회차가 없으면 빈 목록 — 어제 메모로 칸을 채우지 않는다
        assert mkn.past_notes(("2026-09-22", "09:30")) == []
        assert mkn.past_notes(("2026-09-21", "11:00")) == []
        html_out = keyword_note_history_html(("2026-09-22", "14:00"))
        assert keyword_note_history_html(("2026-09-22", "09:30")) == "", "앞 회차가 없는데 칸을 그렸다"
        # 이어진 같은 문구는 한 줄로 합치고, 줄 수 상한(5)까지만
        many = {f"2026-09-22|{s}": f"오늘 {s}" for s in ("07:00", "08:00", "09:30", "11:00", "14:00", "15:00")}
        many["2026-09-22|08:00"] = many["2026-09-22|07:00"]
        (Path(tmp) / "n.json").write_text(json.dumps({"notes": many}, ensure_ascii=False), encoding="utf-8")
        later = mkn.past_notes(("2026-09-22", "17:00"))
        assert len(later) == 5 and later[-1] == ("2026-09-22", ["07:00", "08:00"], "오늘 07:00"), later
    assert 'data-w="청문회"' not in html_out, "어제 메모(9/21)가 섞였다"
    assert 'data-w="유류세"' in html_out and ">연장</span>" in html_out, "문장부호를 뗀 비교값이 없다"
    assert "9/19 메모" not in html_out and "9/21" not in html_out, "오늘이 아닌 날짜가 보인다"
    assert "지금 회차 메모" not in html_out and "뒤 회차 메모" not in html_out, "지금·뒤 회차 메모가 섞였다"
    assert "<button" not in html_out and "onclick" not in html_out, "참고 목록인데 누르는 줄이 생겼다"
    # 앞칸은 회차 시각만 — 모든 줄에 붙는 「오늘」은 적지 않는다
    assert '<span class="kn-h-when">11:00</span>' in html_out, html_out

    from app.adhoc.renderer import _hm_select_html
    sel = _hm_select_html("win-end", "window_end", "14:56", "14:56")
    assert 'type="hidden" id="win-end" name="window_end" value="14:56"' in sel, sel
    assert '<option value="56" selected>56</option>' in sel and '<option value="14" selected>' in sel, sel
    assert '<option value="15"' not in sel, "지나지 않은 시각이 목록에 있다"
    assert sel.index('value="50"') < sel.index('value="56"'), "목록에 없는 분이 제자리에 안 들어갔다"
    print("41) 지난 회차 메모 · 수시 시간 드롭다운: 통과 (오늘 앞 회차만·어제 안 가져옴·최신 5줄·같은 문구 합침·시각만 적음, 숨은 입력·분 끼우기)")


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


def test_partial_cache_survives_non_hidden_drops():
    """숨김 외의 이유로 기사가 빠져도 같은 회차 캐시를 이어 쓰고, 마감은 얇으면 처음부터 분류한다.

    [추가: 2026-10-06] 09:30 회차 — 맥북이 잠들어 06:00 회차가 09:11에야 저장되자, 09:09에
    분류해 둔 캐시 33건 중 15건이 「앞 회차에 이미 실림」으로 초안에서 빠졌다. 거기에 기사를
    13건 숨기니 옛 과반 조건(분모는 숨김만 빼 주고 분자는 모든 이유로 깎인다)이 깨져
    소제목이 「특히」·「lng」·「임명」으로 뭉개졌다.

    지키는 것:
      1) 같은 회차 캐시면 겹침이 과반에 못 미쳐도 재사용한다(규칙 기반으로 안 떨어진다)
      2) 캐시에 없는 기사는 📂 미분류로 모인다
      3) 마감은 이어 쓴 분류가 기사 과반을 미분류로 남기면 처음부터 분류한다
         — 그래야 assign_to_existing이 기사를 소제목 한두 칸에 몰아넣지 않는다
    """
    from app import classifier, llm_classifier

    def art(n):
        return {"url": f"https://example.com/{n}", "title": n, "summary": n}

    round_id = ("2026-10-06", "09:30")
    cached = [art(f"c{i}") for i in range(12)]
    cache = {
        llm_classifier._cache_key(cached, config.MAX_SUBHEADINGS, round_id): [
            ("국정감사 개막 및 일정", [a["url"] for a in cached[:4]], "국감 요약"),
            ("부동산 세제 개편", [a["url"] for a in cached[4:8]], "세제 요약"),
            ("외환보유액 감소", [a["url"] for a in cached[8:]], "외환 요약"),
        ]
    }
    # 숨김은 2건뿐이고, 6건은 「앞 회차에 이미 실림」으로 빠졌다(캐시에서 못 빼는 쪽)
    hidden = {cached[0]["url"], cached[1]["url"]}
    visible = cached[2:4] + cached[8:10] + [art("new1")]

    with patch.object(llm_classifier, "_cache", cache), \
         patch.object(llm_classifier, "_ensure_cache_hydrated", lambda: None), \
         patch.object(llm_classifier, "is_configured", lambda: True), \
         patch.object(curation, "load_hidden_urls", lambda *a, **k: hidden):
        # 옛 과반 조건이라면 탈락했을 상황인지 먼저 확인한다(겹침 4 × 2 = 8 < 살아있는 캐시 10)
        overlap, live = 4, len(cached) - len(hidden)
        assert overlap * 2 < live, f"과반이 깨지는 상황이 아니다: {overlap}*2 vs {live}"

        groups = classifier.classify_articles(visible, round_id=round_id)
        names = [g["name"] for g in groups]
        assert "국정감사 개막 및 일정" in names and "외환보유액 감소" in names, names
        assert llm_classifier.UNCLASSIFIED_GROUP_NAME in names, names
        assert not llm_classifier.last_classification_was_rule_based(), "규칙 기반으로 떨어졌다"
        # 기사가 빠진 소제목은 빠지고, 새 기사는 미분류로 모인다
        unc = next(g for g in groups if g["name"] == llm_classifier.UNCLASSIFIED_GROUP_NAME)
        assert [a["url"] for a in unc["articles"]] == ["https://example.com/new1"], unc

        # 마감 — 미분류가 과반이 아니면 초안 분류를 그대로 이어 쓴다(AI 전체 재분류 없음)
        calls = []
        real = classifier.classify_articles
        def spy(*a, **k):
            calls.append(bool(k.get("allow_llm_call")))
            return real(*a, **{**k, "allow_llm_call": False})
        with patch.object(classifier, "classify_articles", spy), \
             patch.object(classifier, "assign_to_existing", lambda *a, **k: None), \
             patch.object(llm_classifier, "assign_to_existing", lambda *a, **k: None):
            classifier.classify_for_finalize(visible, round_id=round_id)
        assert True not in calls, "미분류가 과반이 아닌데 전체 재분류를 불렀다"

        # 마감 — 겹침이 1건뿐이면(미분류 과반) 처음부터 분류한다
        thin = [cached[2]] + [art(f"n{i}") for i in range(6)]
        calls.clear()
        with patch.object(classifier, "classify_articles", spy), \
             patch.object(classifier, "assign_to_existing", lambda *a, **k: None), \
             patch.object(llm_classifier, "assign_to_existing", lambda *a, **k: None):
            classifier.classify_for_finalize(thin, round_id=round_id)
        assert True in calls, "미분류가 과반인데 전체 재분류를 안 불렀다"


def test_today_cut_round():
    """「✂ 오늘만 여기서 끊기」 — 시간표 끼우기·곧바로 확정본·예약·취소·기록 이어 쓰기.

    지키는 것:
      1) 끊은 시각이 그 날짜의 시간표에만 끼워진다(다른 날짜·설정 밖 시각은 무시)
      2) 지난 시각으로 끊으면 네이버를 다시 검색하지 않고 초안 캐시 기사로 곧바로 저장한다 —
         끊은 시각 뒤에 게시된 기사는 빠진다
      3) 끊는 순간 원래 회차 이름의 기록(이름표·메모·초안에 보인 기사)이 새 회차로 넘어간다
      4) 앞 시각이면 예약만 하고, 취소하면 그동안 다듬은 기록이 원래 회차로 돌아간다
      5) 확정본이 된 끊기도 보내기 전·뒤 회차 전이면 되돌아간다(회차 파일까지) · 회차 밖 시각은 거절
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
        # 확정본까지 실제로 저장되는지 보려고 화면 HTML 만들기만 빼고 confirm_and_promote가 하는
        # 일(storage.confirm_run)을 그대로 돌린다 — 확정 표시를 흉내 내면 "끊었는데 확정본이
        # 아니다"를 못 잡는다.
        seen_at_render = []

        def _promote(run):
            seen_at_render.append(today_cuts.cut_origin(today_s, "15:30"))
            storage.confirm_run(run)

        promoted = MagicMock(side_effect=_promote)
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
        # 2-1) 확정본으로 저장됐나 — 확정본 화면이 읽는 값과 회차 파일이 모두 끊은 회차여야 한다
        confirmed = storage.load_latest_confirmed_run()
        assert confirmed and confirmed["run_slot"] == "15:30", (
            f"끊은 회차가 확정본이 아님: {confirmed and confirmed.get('run_slot')}"
        )
        assert confirmed.get("confirmed") is True, "확정 표시가 안 붙었음"
        on_disk = json.loads((storage.ARTICLES_DIR / f"{today_s}_15-30.json").read_text(encoding="utf-8"))
        assert on_disk["confirmed"] is True and on_disk["run_slot"] == "15:30", on_disk.get("run_slot")
        assert {a["url"] for a in on_disk["articles"]} == {pool[0]["url"], pool[1]["url"]}, (
            "확정본 파일에 담긴 기사가 화면과 다름"
        )
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

        # 5) 거절 — 회차 밖 시각
        try:
            cut_round.cut_round("18:00", now=now2)
        except cut_round.CutError:
            pass
        else:
            raise AssertionError("회차 밖 시각(18:00)을 거절하지 않았음")

        # 5-1) 되돌리기가 막히는 두 경우 — 이미 발송했거나, 뒤 회차가 이미 돌았거나
        run_file = storage.ARTICLES_DIR / f"{today_s}_15-30.json"
        saved_run = json.loads(run_file.read_text(encoding="utf-8"))
        run_file.write_text(json.dumps({**saved_run, "send_count": 1}, ensure_ascii=False), encoding="utf-8")
        assert cut_round.cut_state(now2)["blocked"] == "sent", "발송한 확정본인데 되돌리기가 열렸음"
        try:
            cut_round.cancel_cut("15:30", now=now2)
        except cut_round.CutError:
            pass
        else:
            raise AssertionError("이미 발송한 확정본을 되돌렸음")
        run_file.write_text(json.dumps(saved_run, ensure_ascii=False), encoding="utf-8")

        storage.save_run("17:00", [], run_at=f"{today_s}T17:00:05")
        assert cut_round.cut_state(now2)["blocked"] == "next", "뒤 회차가 돌았는데 되돌리기가 열렸음"
        try:
            cut_round.cancel_cut("15:30", now=now2)
        except cut_round.CutError:
            pass
        else:
            raise AssertionError("뒤 회차가 이미 수집됐는데 되돌렸음")
        (storage.ARTICLES_DIR / f"{today_s}_17-00.json").unlink()

        # 5-2) 되돌리기 — 끊기 한 줄과 회차 파일이 **함께** 사라지고 시간표가 돌아간다
        state = cut_round.cut_state(now2)
        assert state["revert"] == {"end": "15:30", "count": 2,
                                   "restored": {"start": "14:00", "end": "17:00"}}, state["revert"]
        with patch.object(confirm_send, "refresh_confirmed_screens", MagicMock()):
            reverted = cut_round.cancel_cut("15:30", now=now2)
        assert reverted == {"mode": "reverted", "end": "15:30", "restored": "17:00"}, reverted
        assert not run_file.exists(), "되돌렸는데 회차 파일이 남아 있음 — 시간표 밖 고아 확정본이 된다"
        assert today_cuts.cut_origin(today_s, "15:30") is None, "되돌렸는데 끊기가 남음"
        assert settings.active_schedule_times(st, now2) == windows, "시간표가 끊기 전으로 안 돌아감"
        assert any(d["run_slot"] == "15:30" for d in storage.list_deleted_runs()), (
            "되돌린 회차가 휴지통에 없음 — 잘못 눌렀을 때 되살릴 길이 사라진다"
        )
        seen_back = json.loads(draft_seen.DRAFT_SEEN_FILE.read_text())
        assert seen_back["round_id"] == [today_s, "17:00"], (
            f"초안에 보인 기사 목록이 원래 회차로 안 돌아감: {seen_back['round_id']}"
        )

        # 6) 예약 회차가 제시각에 수집되면 기록이 이어지는 회차로
        today_cuts.add_cut(today_s, "16:45", "17:00")
        data = json.loads(labels_file.read_text())
        data[f"{today_s}|16:45"] = {"AI 이름": "끊는 회차에서 고친 이름"}
        labels_file.write_text(json.dumps(data, ensure_ascii=False))
        today_cuts.after_round_collected({"run_at": f"{today_s}T16:45:05", "run_slot": "16:45"})
        assert json.loads(labels_file.read_text())[f"{today_s}|17:00"] == {"AI 이름": "끊는 회차에서 고친 이름"}

    print("오늘만 회차 끊기: 통과 (시간표 끼우기·초안 기사로 즉시 확정본 저장·예약·취소·되돌리기·기록 이어 쓰기)")



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


def test_no_ai_key_is_not_a_failure():
    """AI 키를 아예 안 쓰는 상태에서는 규칙 기반 소제목이 「실패」로 취급되지 않는다.

    초안 배너는 앰버 안내 한 줄로 바뀌고(재분류 칩을 안 가리킨다), 확정본 경고·정기
    보관함 칩·자동발송 차단은 걸리지 않는다 — 안 그러면 모든 회차가 「AI 분류 실패」가
    되어 자동발송이 매번 멈추고, 진짜 실패(크레딧 부족)와 구분이 안 된다.
    저장된 classification_degraded가 있는 회차는 키가 없어져도 실패로 남는다.
    """
    from app import classifier

    rule_based = {"articles": [{"group": "부총리"}, {"group": "기금"}, {"group": "환율"}]}
    stored_fail = dict(rule_based, classification_degraded=True)

    with patch.object(classifier, "llm_is_configured", return_value=False):
        assert classifier.run_looks_rule_based(rule_based), "사실 판정은 그대로 규칙 기반이다"
        assert not classifier.run_classification_failed(rule_based), "키가 없으면 실패가 아니다"
        assert classifier.run_classification_failed(stored_fail), "그때 실제로 실패한 회차는 남는다"
    with patch.object(classifier, "llm_is_configured", return_value=True):
        assert classifier.run_classification_failed(rule_based), "키가 있는데 규칙 기반이면 실패다"

    with patch.object(preview_renderer, "llm_is_configured", return_value=False):
        banner = preview_renderer._classify_degraded_html(True, 12, ("2026-09-28", "09:30"))
    assert "no-key" in banner and "크레딧을 채워" in banner, banner
    assert "⚠️" not in banner and "btn-ref" not in banner, "키 없음은 경고가 아니라 안내다"

    # 초안 한 판을 실제로 그려서, 배너는 뜨되 복사·텍스트·엑셀 확인창 플래그
    # (CLASSIFY_DEGRADED)는 꺼지는지 본다 — 가져갈 때마다 묻는 확인창이 되면 안 된다.
    slot = {"start": "08:00", "end": "09:30"}
    articles = [
        {"url": f"https://n.news.naver.com/a/{i}", "title": f"기재부 세수 대책 {i}",
         "summary": "정부가 세수 대책을 내놨다.", "outlet": "연합뉴스",
         "pub_date": f"2026-09-28T08:{10 + i:02d}:00"}
        for i in range(6)
    ]
    with patch.object(preview_renderer, "llm_is_configured", return_value=False), \
         patch.object(preview_renderer, "llm_last_classification_was_rule_based", return_value=True):
        content = preview_renderer._compute_preview_content(
            slot, articles, ["기재부"], [], "ㅇ ({outlet}) {title}", "09:18"
        )
    assert "no-key" in content["body"], "초안 배너는 앰버 안내로 뜬다"
    assert content["classify_degraded"] is False, "키가 없으면 가져가기 확인창은 안 묻는다"
    assert content["classify_degraded_names"] == [], content["classify_degraded_names"]

    print("47) AI 키 없음은 실패가 아니다: 통과 (사실 판정과 실패 판정 분리, 저장된 실패는 유지, 배너는 앰버 안내)")


def test_unclassified_sits_below_summary():
    """📂 소제목 미분류는 💬 요약 블록 **아래**, 📌 담아둔 기사 칸 바로 위에 선다(확정본·초안 같다).

    보고서로 나갈 소제목·기사와 그 요약이 먼저 끝나고, 아직 갈 곳이 안 정해진 기사가 그 뒤에
    남는다 — 왼쪽 띠를 가진 두 칸(미분류 앰버·담아둔 기사 캐러멜)이 화면 맨 아래 나란히 선다.
    같이 「+ 한 줄 메모」의 +를 뗐다(칸이 하나뿐이라 더하는 게 아니라 쓰는 자리다 — 「+ 새
    소제목」의 +는 그대로). 두 화면이 어긋나면 안 되므로 한 테스트에서 둘 다 본다.
    """
    _require_isolation()
    uncl = llm_classifier.UNCLASSIFIED_GROUP_NAME

    def art(url, title, group):
        return {"outlet": "연합뉴스", "title": title, "url": url,
                "summary": "정부가 세수 대책을 내놨다.", "pub_date": "2026-09-15T10:00:00+09:00",
                "group": group}

    articles = [art("https://u.x/1", "세제 개편 발표", "세제"),
                art("https://u.x/2", "아직 못 고른 기사", uncl)]

    def places(page):
        # 미분류 칸을 가리키는 표식은 두 화면이 다르다(초안만 앰버 띠 class를 단다) —
        # 둘 다 가진 data-toc-name으로 자리를 잰다.
        return (page.index('<div class="bottom">'),
                page.index('data-toc-name="%s"' % uncl),
                page.index('<div class="manual-zone"'))

    # 1) 확정본
    page = renderer.render_page("11:00", articles, ["세제"], [], "ㅇ ({outlet}) {title}", "<{section}>")
    bottom, unclassified, manual = places(page)
    assert bottom < unclassified < manual, "확정본: 미분류가 요약 아래·담아둔 기사 위에 와야 한다"
    assert '<section class="subheading" id="subheading-0"' in page, "다른 소제목은 제자리에 남아야 한다"

    # 2) 초안 — 분류 결과만 심어 같은 자리를 확인한다(AI 호출 없음)
    groups = [{"name": "세제", "articles": [articles[0]]}, {"name": uncl, "articles": [articles[1]]}]
    with patch.object(preview_renderer, "classify_articles", return_value=groups):
        content = preview_renderer._compute_preview_content(
            {"start": "09:00", "end": "09:30"}, articles, ["세제"], [],
            "ㅇ ({outlet}) {title}", "09:18",
        )
    bottom, unclassified, manual = places(content["body"])
    assert bottom < unclassified < manual, "초안: 미분류가 요약 아래·담아둔 기사 위에 와야 한다"
    assert '<section class="subheading subheading-unclassified"' in content["body"], \
        "초안 미분류 칸의 앰버 띠(class)는 그대로 둔다"

    # 3) 「한 줄 메모」엔 +가 없고, 「새 소제목」엔 그대로 있다
    draft_actions = preview_renderer._actions_html("본문", "09:30", export_rows=[], export_date="2026-09-15")
    for name, html_text in (("확정본", page), ("초안", draft_actions)):
        assert 'toggleKeywordNote(this)">한 줄 메모</button>' in html_text, f"{name}: 메모 버튼에 +가 남아 있음"
        assert '<span class="plus-glyph">+</span>새 소제목' in html_text, f"{name}: 새 소제목의 +는 그대로 둔다"

    print("48) 📂 소제목 미분류 자리: 통과 (요약 아래·담아둔 기사 위, 확정본·초안 같음, 메모 버튼 + 뗌)")


def test_empty_custom_group_appears_once_in_group_list():
    """빈 커스텀 소제목이 「이동할 소제목」 드롭다운에 **한 번만** 선다(확정본·초안 같다).

    이 화면들이 소제목 목록을 세는 자리는 `.subheading h2 .rename-btn[data-current]`
    하나다(getAllGroups·moveGroupOrder). 기사가 0건인 커스텀 소제목의 🗑은 ✏️와 같은
    `rename-btn` 클래스를 쓰는데 거기에 `data-current`까지 달려 있어, 그 칸만 목록에
    **두 번** 걸렸다(2026-10-16 제보 — 소제목을 하나 만들었는데 드롭다운에 같은 이름이
    둘). 🗑이 읽는 이름은 `data-del-current`로 옮겼다.
    """
    _require_isolation()
    empty_name = "10월 최근 경제동향"

    def art(url, title, group):
        return {"outlet": "연합뉴스", "title": title, "url": url,
                "summary": "정부가 세수 대책을 내놨다.", "pub_date": "2026-10-16T10:00:00+09:00",
                "group": group}

    articles = [art("https://e.x/1", "세제 개편 발표", "세제")]

    def check(label, page):
        # 목록에 걸리는 표식은 **소제목 칸 하나당 많아야 하나**다(📂 미분류 칸엔 아예 없다).
        blocks = page.split('<section class="subheading')[1:]
        assert blocks, f"{label}: 소제목이 하나도 안 그려졌다"
        for block in blocks:
            name = re.search(r'data-toc-name="([^"]*)"', block)
            marks = block.count('data-current="')
            assert marks <= 1, \
                f"{label}: 「{name and name.group(1)}」 칸에 목록 표식이 {marks}개 — 드롭다운에 두 번 선다"
        assert page.count(f'data-current="{empty_name}"') == 1, \
            f"{label}: 빈 소제목이 목록에 한 번만 서야 한다"
        # 🗑은 목록에서 빠지되 확인창에 보일 이름은 그대로 들고 있어야 한다.
        assert f'data-del-current="{empty_name}"' in page, f"{label}: 🗑이 화면 이름을 잃었다"

    # 1) 확정본
    with patch.object(renderer, "load_custom_groups", return_value=[empty_name]):
        page = renderer.render_page("11:00", articles, ["세제"], [], "ㅇ ({outlet}) {title}", "<{section}>")
    check("확정본", page)

    # 2) 초안 — 분류 결과만 심어 같은 자리를 확인한다(AI 호출 없음)
    groups = [{"name": "세제", "articles": [articles[0]]}]
    with patch.object(preview_renderer, "classify_articles", return_value=groups), \
         patch.object(preview_renderer, "load_custom_groups", return_value=[empty_name]):
        content = preview_renderer._compute_preview_content(
            {"start": "09:00", "end": "11:00"}, articles, ["세제"], [],
            "ㅇ ({outlet}) {title}", "10:18",
        )
    check("초안", content["body"])

    print("83) 빈 커스텀 소제목: 통과 (드롭다운·순서 목록에 한 번만, 🗑은 data-del-current)")


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
            # 회차 키의 날짜는 「오늘」이어야 한다 — send_log.record는 오늘 날짜 파일에
            # 쌓고 latest_for_run은 키의 날짜 파일만 본다(날짜를 박아두면 그날만 통과한다).
            key = f"{date.today().isoformat()}|14:00"
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
        breaking_alert.save_breaking_alert_settings(True, "06:00", "23:00", 3, True, False, 30, 5)
        mock_post.reset_mock()
        fresh = [flash(60 + i, f"신문{i}", "14:2" + str(i)) for i in range(6)]
        breaking_alert_sender.detect_and_alert(fresh, now=datetime(2026, 9, 18, 14, 27))
        assert len(texts(mock_post)) == 1, "몰림 알림을 끄면 개별 알림만 가야 한다"

    for bad in ((45, 5), (30, 7)):
        try:
            breaking_alert.save_breaking_alert_settings(True, "06:00", "23:00", 3, True, True, *bad)
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
        breaking_alert.save_breaking_alert_settings(True, "06:00", "23:00", 3, True, False, 30, 5)

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
        breaking_alert.save_breaking_alert_settings(True, "06:00", "23:00", 3, True, True, 30, 3)
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
        st.enter_context(patch.object(confirm_send, "run_classification_failed", lambda r: False))
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
        st.enter_context(patch.object(confirm_send, "run_classification_failed", lambda r: False))
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


def test_last_slot_topup_adds_late_articles():
    """하루 마지막 회차 — 정시에 확정본을 내고, 마감 뒤 늦게 색인된 기사를 덧붙인다.

    예전엔 마지막 회차만 수집을 30분 늦췄다(옛 LAST_SLOT_COLLECT_DELAY_MIN). 기사는 많이
    담겼지만 하루의 마지막 보고서만 30~45분 뒤에 보여, 실사용에서 "왜 17시 확정본이 안
    나오나"로 되풀이됐다(2026-09-29). 이 테스트가 보는 것:

      1) 보충 시각 규칙 — 마감 + 30분, 23:50에서 잘림, 23:50 이후 마감이면 보충 없음
      2) 마지막 회차도 **정시에** 수집 대상이 된다(지연이 사라졌다)
      3) 보충은 **새 기사만 덧붙인다** — 기존 기사끼리의 순서·숨김·소제목은 그대로이고,
         새 기사는 제 소제목 안 **언론사 순위 자리**에 끼워 들어간다(맨 뒤가 아니다)
      4) 새 기사엔 late_pickup + topup_pickup이 붙고 late_pickup_slot은 **안 붙는다**
         (앞 회차 기사가 아니라 이 회차 시간대 기사라 회차 이름을 적으면 안 된다)
      5) 이미 있는 제목과 같은 기사는 안 붙는다(담당자가 보던 기사가 밀려나지 않는다)
      6) 한 번 보충하면 그날 다시 안 돈다(topup_at) — 실패했을 때만 다시 시도된다
      7) 확정본 배너·칩이 "마감 뒤에 들어온 기사"로 말한다(앞 회차라고 하지 않는다)
    """
    from app import last_slot_topup, scheduler, undo

    _require_isolation()
    today = datetime.now().date().isoformat()
    windows = [
        {"start": "09:00", "end": "14:00", "enabled": True},
        {"start": "14:00", "end": "17:00", "enabled": True},
    ]

    # 1) 보충 시각 규칙
    assert last_slot_topup.topup_due_at(windows) == "17:30", "마감 + 30분이어야 함"
    assert last_slot_topup.topup_due_at(
        [{"start": "20:00", "end": "23:40", "enabled": True}]
    ) == "23:50", "자정을 넘기는 대신 23:50으로 당겨야 함"
    for end in ("23:50", "23:59"):
        assert last_slot_topup.topup_due_at(
            [{"start": "20:00", "end": end, "enabled": True}]
        ) is None, f"{end} 마감엔 벌 시간이 없어 보충을 건너뛰어야 함"
    assert last_slot_topup.topup_due_at([]) is None

    # 2) 마지막 회차도 정시에 돈다 — 17:00 마감 직후 tick에서 바로 후보가 된다
    due = scheduler.find_slots_to_run(
        datetime.strptime(f"{today} 17:00", "%Y-%m-%d %H:%M"),
        schedule_times=windows,
        already_run=lambda slot: False,
    )
    assert [w["end"] for w in due] == ["14:00", "17:00"], (
        f"마지막 회차가 정시에 수집 대상이 아니다: {[w['end'] for w in due]}"
    )

    # --- 17:00 회차가 정시에 저장된 상태를 만든다 ---
    kept = {**_article("담당자가-보던-기사"), "pub_date": f"{today}T16:10:00+09:00",
            "group": "세제개편", "group_summary": "세제 관련 요약"}
    hidden_one = {**_article("숨긴-기사"), "pub_date": f"{today}T16:20:00+09:00",
                  "group": "세제개편", "group_summary": "세제 관련 요약"}
    storage.save_run("17:00", [kept, hidden_one], run_at=f"{today}T17:00:06")
    curation.hide_article(hidden_one["url"], outlet=hidden_one["outlet"], title=hidden_one["title"])

    # 마감 뒤 네이버가 뒤늦게 준 기사들
    # 언론사를 갈라 둬야 "맨 뒤"가 아니라 "언론사 순위 자리"에 들어가는지 볼 수 있다 —
    # 조선일보는 PRIORITY_OUTLETS 안이고 테스트일보는 목록 밖(맨 뒤 순위)이다.
    late_new = {**_article("마감뒤-들어온-기사"), "outlet": "조선일보",
                "pub_date": f"{today}T16:55:00+09:00"}
    same_title = {**_article("다른-주소"), "title": kept["title"],
                  "pub_date": f"{today}T16:58:00+09:00"}
    captured = {}

    def fake_search(groups, after=None, before=None, track_keyword_matches=False):
        captured["after"] = after
        captured["before"] = before
        # 보충 훑기도 같은 창을 그대로 다시 훑으므로 기존 기사까지 딸려 온다.
        return [dict(kept), dict(hidden_one), dict(late_new), dict(same_title)], []

    now = datetime.strptime(f"{today} 17:30", "%Y-%m-%d %H:%M")
    # 보충 전에 쌓인 되돌리기 기록은 보충 뒤에 남아 있으면 안 된다 — ↩ 한 번이 최신 회차
    # 파일을 통째로 되돌려 방금 덧붙인 기사를 말없이 지운다(끊기와 같은 이유).
    undo.push("숨기기")
    assert undo.peek_label() == "숨기기"
    with patch.object(scraper, "search_articles_by_groups", side_effect=fake_search), \
         patch.object(scraper, "naver_is_configured", return_value=True), \
         patch.object(scraper, "detect_and_alert", return_value=None), \
         patch.object(scraper, "assign_to_existing", return_value={late_new["url"]: "세제개편"}):
        added = scraper.top_up_last_run(now=now, schedule_times=windows)

    assert added == 1, f"덧붙인 건수가 1이어야 하는데 {added}"
    assert undo.peek_label() is None, (
        "보충 뒤에도 되돌리기 기록이 남았다 — ↩ 한 번이 덧붙인 기사를 지운다"
    )
    # 창은 그 회차 그대로 — 하한만 60분 물리고 상한은 마감이다(마감 뒤 게시분은 안 담는다)
    assert captured["after"].strftime("%H:%M") == "13:00", f"하한이 13:00이어야 하는데 {captured['after']}"
    assert captured["before"].strftime("%H:%M") == "17:00", "상한은 회차 마감 그대로여야 함"

    run = last_slot_topup.find_run(today, "17:00")
    urls = [a["url"] for a in run["articles"]]
    # 3) 기존 기사끼리의 순서는 그대로, 새 기사는 제 소제목 안 언론사 순위 자리
    assert urls.index(kept["url"]) < urls.index(hidden_one["url"]), (
        f"기존 기사끼리의 상대 순서가 바뀌었다: {urls}"
    )
    assert urls[0] == late_new["url"], (
        f"보충분이 언론사 순위 자리에 안 들어갔다(조선일보 > 테스트일보): {urls}"
    )
    assert run["run_slot"] == "17:00" and run["run_at"] == f"{today}T17:00:06", (
        "보충이 회차 이름·시각을 바꿨다 — 보고서는 여전히 그 회차의 보고서여야 한다"
    )
    # 5) 같은 제목은 안 붙는다
    assert same_title["url"] not in urls, "이미 있는 제목과 같은 기사가 덧붙었다"

    fresh = next(a for a in run["articles"] if a["url"] == late_new["url"])
    # 4) 표식
    assert fresh.get("late_pickup") and fresh.get("topup_pickup"), "보충 표식이 안 붙었다"
    assert not fresh.get("late_pickup_slot"), (
        "보충분에 회차 이름이 붙었다 — 배너가 자기 회차를 남처럼 가리키게 된다"
    )
    assert fresh["group"] == "세제개편", "기존 소제목에 배정되지 않았다"
    assert fresh["group_summary"] == "세제 관련 요약", "그 소제목의 요약을 물려받지 않았다"
    # 6) 두 번째 호출은 아무것도 안 한다
    assert run.get("topup_at"), "보충 기록(topup_at)이 안 남았다"
    with patch.object(scraper, "search_articles_by_groups", side_effect=fake_search), \
         patch.object(scraper, "naver_is_configured", return_value=True):
        assert scraper.top_up_last_run(now=now, schedule_times=windows) == 0, (
            "이미 보충한 회차를 또 훑었다"
        )

    # 7) 확정본 화면 — 앞 회차라고 말하지 않는다
    storage.confirm_run(run)
    html_text = renderer.generate_screen().read_text(encoding="utf-8")
    assert "late-banner" in html_text, "보충 안내 배너가 안 그려짐"
    assert "마감 뒤에 들어온 기사" in html_text, "배너가 보충분을 마감 뒤 기사로 말하지 않음"
    banner = html_text[html_text.index('<div class="late-banner">'):]
    banner = banner[: banner.index("</div>")]
    assert "앞 회차" not in banner, f"보충분을 앞 회차 기사라고 말했음: {banner}"
    assert "보충 수집으로 함께 담았어요" in banner, "보충 문장이 아니라 회수 문장을 썼음"
    assert "⏱ 늦게 들어온 기사" in html_text, "보충분에 칩이 안 붙음"
    plain_start = html_text.index("const PLAIN_TEXT = ") + len("const PLAIN_TEXT = ")
    plain_text = json.loads(html_text[plain_start:html_text.index(";\n", plain_start)])
    assert "늦게 들어온 기사" not in plain_text, "화면 전용 표시가 복사/내보내기 텍스트로 샜음"

    # 시각 전에는 돌지 않고, 23:59 마감이면 아예 돌지 않는다(run_topup_if_due 쪽 판단)
    calls = []
    early = datetime.strptime(f"{today} 17:05", "%Y-%m-%d %H:%M")
    assert scheduler.run_topup_if_due(
        now=early, schedule_times=windows,
        topup=lambda **kw: calls.append("불렀다") or 1, refresh=lambda: None,
    ) == 0, "보충 시각 전인데 훑었다"
    assert not calls, "보충 시각 전인데 수집 함수를 불렀다"
    refreshed = []
    assert scheduler.run_topup_if_due(
        now=now, schedule_times=windows,
        topup=lambda **kw: 2, refresh=lambda: refreshed.append(True),
    ) == 2 and refreshed, "보충 시각인데 안 훑거나 화면을 다시 안 그렸다"
    assert scheduler.run_topup_if_due(
        now=now, schedule_times=[{"start": "20:00", "end": "23:59", "enabled": True}],
        topup=lambda **kw: 1, refresh=lambda: None,
    ) == 0, "23:59 마감인데 보충을 시도했다"

    print("47) 마지막 회차 보충 훑기: 통과 (정시 확정본 + 마감 뒤 덧붙이기, 23:50 마감선, 표식·배너, 한 번만)")


def test_new_article_placement():
    """새로 들어온 기사는 **제 소제목 안 언론사 순위 자리**에 끼워 들어간다.

    예전엔 담당자가 ↑↓를 한 번이라도 누르면 그 뒤에 색인된 기사가 전부 목록 맨 뒤로
    붙었다(apply_preview_order의 `known + unknown`). 그래서 조선일보 기사가 매일신문
    아래에 서는 일이 생겼다(2026-10-16 「10월 최근 경제동향」). 이 테스트가 보는 것:

      1) 언론사 순위 자리에 들어가고, **이미 자리를 잡은 기사는 한 칸도 안 움직인다**
      2) `[단독]`은 그 소제목 맨 위 구간으로 — sort_scoop_first와 같은 서열이다
         ([속보]는 그 함수와 똑같이 자리를 안 바꾼다)
      3) 소제목 **사이** 배치는 안 건드린다(한 줄 목록에서 각 소제목이 쓰던 칸 그대로)
      4) 끼워 넣을 대상이 없으면 입력을 그대로 돌려준다 — ↑↓를 안 누른 평소 경로는
         이 규칙이 아무 일도 하지 않는다
      5) **초안과 확정본이 같은 함수·같은 대상 판정을 쓴다** — 한쪽만 고치면 화면 2번이
         확정본 10번이 된다(CLAUDE.md가 되풀이해 경고하는 초안↔확정본 어긋남)
    """
    import inspect

    from app import preview_renderer, scraper
    from app.preview_order import urls_not_in_order
    from app.sorter import place_new_articles, place_new_in_group

    order = ["KBS", "조선일보", "한국일보", "매일신문"]

    def art(outlet, title="제목", url=None, group="그린북"):
        return {
            "outlet": outlet, "title": title, "url": url or outlet,
            "pub_date": "2026-10-16T10:30:00+09:00", "group": group,
        }

    # 1) 언론사 순위 자리 — 기존 기사는 그대로
    placed = [art("KBS"), art("한국일보"), art("매일신문")]
    out = place_new_in_group(placed + [art("조선일보")], {"조선일보"}, order)
    assert [a["outlet"] for a in out] == ["KBS", "조선일보", "한국일보", "매일신문"], (
        f"언론사 순위 자리에 안 들어갔다: {[a['outlet'] for a in out]}"
    )
    # 담당자가 손으로 흐트러뜨린 순서도 상대 순서 그대로 유지된다
    manual = [art("매일신문"), art("KBS"), art("한국일보")]
    out = place_new_in_group(manual + [art("조선일보")], {"조선일보"}, order)
    assert [a["outlet"] for a in out if a["outlet"] != "조선일보"] == [
        "매일신문", "KBS", "한국일보"
    ], "손수 맞춘 순서가 흐트러졌다 — 끼워 넣기는 기존 기사를 안 움직여야 한다"

    # 2) [단독]은 맨 위 구간으로, [속보]는 자리를 안 바꾼다
    with_scoop = [art("매일신문", "[단독] 먼저 난 특종"), art("KBS"), art("한국일보")]
    out = place_new_in_group(with_scoop + [art("조선일보")], {"조선일보"}, order)
    assert out[0]["title"].startswith("[단독]"), "[단독]이 맨 위에서 밀려났다"
    assert [a["outlet"] for a in out] == ["매일신문", "KBS", "조선일보", "한국일보"], (
        f"평범한 새 기사가 [단독] 아래 구간에 안 들어갔다: {[a['outlet'] for a in out]}"
    )
    out = place_new_in_group(
        with_scoop + [art("조선일보", "[단독] 새 특종")], {"조선일보"}, order
    )
    assert [a["outlet"] for a in out[:2]] == ["조선일보", "매일신문"], (
        f"새 [단독]이 맨 위 구간 안 언론사 순위 자리에 안 섰다: {[a['outlet'] for a in out]}"
    )
    out = place_new_in_group(
        [art("KBS"), art("한국일보")] + [art("조선일보", "[속보] 새 속보")], {"조선일보"}, order
    )
    assert [a["outlet"] for a in out] == ["KBS", "조선일보", "한국일보"], (
        "[속보]를 맨 위로 끌어올렸다 — sort_scoop_first는 [단독]만 올린다"
    )

    # 3) 소제목 사이 배치는 그대로
    flat = [
        art("매일신문", url="m", group="A"),
        art("KBS", url="k", group="B"),
        art("한국일보", url="h", group="A"),
        art("조선일보", url="c", group="A"),
    ]
    out = place_new_articles(flat, {"c"}, priority_outlets=order)
    assert [a["group"] for a in out] == ["A", "B", "A", "A"], (
        f"소제목이 쓰던 칸 패턴이 바뀌었다: {[a['group'] for a in out]}"
    )
    assert [a["outlet"] for a in out] == ["조선일보", "KBS", "매일신문", "한국일보"], (
        f"A 소제목 안에서 순위 자리에 안 들어갔다: {[a['outlet'] for a in out]}"
    )

    # 4) 대상이 없으면 손대지 않는다 — ↑↓를 안 누른 평소 경로
    assert place_new_articles(flat, set(), priority_outlets=order) is flat
    assert urls_not_in_order(flat, []) == set(), (
        "손수 맞춘 순서가 없는데 끼워 넣을 대상이 생겼다"
    )
    assert urls_not_in_order(flat, ["m", "k"]) == {"h", "c"}

    # 5) 초안과 확정본이 같은 함수·같은 대상 판정을 쓴다
    for module, name in ((preview_renderer, "초안"), (scraper, "확정본 저장·보충 훑기")):
        src = inspect.getsource(module)
        assert "place_new_articles(" in src, (
            f"{name} 경로가 공용 끼워 넣기 함수를 안 쓴다 — 두 화면의 순서가 갈린다"
        )
        assert "urls_not_in_order(" in src, (
            f"{name} 경로가 공용 대상 판정을 안 쓴다 — 같은 기사를 다르게 센다"
        )
    assert inspect.getsource(scraper).count("place_new_articles(") == 2, (
        "확정본 저장과 보충 훑기 두 자리 모두에서 불러야 한다"
    )

    print(
        "48) 새 기사 끼워 넣기: 통과 "
        "(소제목 안 언론사 순위 자리, [단독]만 맨 위, 기존 순서 보존, 초안·확정본 같은 함수)"
    )

def test_trend_end_day_follows_today_completion():
    """정책 단어 추이의 끝 날짜 — 오늘 예정 회차가 전부 저장되기 전까지는 어제다.

    지키는 것:
      1) 오늘 회차가 일부만 저장됐으면 끝 날짜는 어제 (덜 찬 오늘이 급락처럼 보이면 안 된다)
      2) 오늘 예정 회차가 전부 저장되고 **마지막 회차 보충까지 끝나면** 끝 날짜가 오늘로
         올라오고, 그 칸은 점선이 아니다 (보충 전엔 아직 어제 — 그리자마자 값이 올라간다)
      3) 예정 회차를 모를 때(설정이 비었을 때)는 어제 — 없는 회차를 지어내지 않는다
      4) 마지막 칸 밑 라벨이 「어제」/「오늘」로 갈린다(그래프 오른쪽 끝 값이 언제 것인지)
    """
    from app import last_slot_topup, trend_chart, trend_data

    _require_isolation()
    today = datetime.now().date()
    today_s = today.isoformat()
    yday_s = (today - timedelta(days=1)).isoformat()
    windows = [
        {"start": "09:00", "end": "11:00", "enabled": True},
        {"start": "11:00", "end": "14:00", "enabled": True},
    ]

    # 3) 예정 회차를 모르는 상태 (schedule_groups가 비어 있음)
    st = settings.load_settings()
    st["schedule_groups"] = []
    settings.SETTINGS_FILE.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    assert trend_data.todays_expected_slots(today_s) == []
    assert trend_data.last_complete_day(today_s) == yday_s, "예정 회차를 모르는데 오늘까지 그렸다"

    st["schedule_groups"] = [{"name": "매일", "times": windows, "enabled": True,
                              "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]}]
    settings.SETTINGS_FILE.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    assert trend_data.todays_expected_slots(today_s) == ["11:00", "14:00"]

    # 1) 회차가 하나도 없을 때 · 일부만 있을 때
    assert trend_data.last_complete_day(today_s) == yday_s
    storage.save_run("11:00", [_article("추이-1")], run_at=f"{today_s}T11:01:00")
    assert not trend_data.today_is_complete(today_s), "회차가 아직 남았는데 다 찬 날로 봤다"
    assert trend_data.last_complete_day(today_s) == yday_s

    buckets, unit = trend_data.build_buckets(yday_s, yday_s)
    trend_data.fill_bucket_status(buckets, unit, {yday_s: []}, today_s)
    assert buckets[-1]["yesterday"] and not buckets[-1]["is_today"]
    assert not buckets[-1]["partial"], "어제 칸이 점선으로 찍혔다"
    assert "어제" in trend_chart.render_chart(
        "t", buckets, [{"word": "세제", "color": "#000", "values": [1]}], unit)

    # 2) 남은 회차까지 저장되면 — 단 마지막 회차의 「보충 훑기」까지 끝나야 오늘이 된다
    storage.save_run("14:00", [_article("추이-2")], run_at=f"{today_s}T14:01:00")
    assert not trend_data.today_is_complete(today_s), (
        "마지막 회차 보충이 남았는데 다 찬 날로 봤다 — 그리자마자 오늘 칸이 올라간다"
    )
    assert trend_data.last_complete_day(today_s) == yday_s
    last_run = last_slot_topup.find_run(today_s, "14:00")
    storage.update_run_articles(
        {**last_run, "topup_at": f"{today_s}T14:31:00"}, last_run["articles"]
    )
    assert trend_data.today_is_complete(today_s)
    assert trend_data.last_complete_day(today_s) == today_s, "오늘 회차가 다 찼는데 어제에서 끊겼다"

    buckets, unit = trend_data.build_buckets(yday_s, today_s)
    trend_data.fill_bucket_status(buckets, unit, {yday_s: [], today_s: []}, today_s)
    assert buckets[-1]["is_today"] and not buckets[-1]["yesterday"]
    assert not buckets[-1]["partial"], "다 찬 오늘 칸이 「집계 중」 점선으로 찍혔다"
    svg = trend_chart.render_chart(
        "t", buckets, [{"word": "세제", "color": "#000", "values": [1, 2]}], unit)
    assert "오늘" in svg and "어제" not in svg, "마지막 칸 라벨이 오늘로 안 바뀌었다"

    # 주별 칸은 이번 주가 안 끝났으면 오늘이 다 찼어도 여전히 점선이다
    week_start = (today - timedelta(days=60)).isoformat()
    buckets, unit = trend_data.build_buckets(week_start, today_s)
    trend_data.fill_bucket_status(buckets, unit, {}, today_s)
    assert unit == "week"
    if today.weekday() != 6:  # 일요일이면 이번 주도 오늘로 끝난다
        assert buckets[-1]["partial"], "덜 찬 이번 주 칸의 점선이 사라졌다"

    print("✓ 추이 끝 날짜: 오늘 회차가 전부 저장된 뒤에만 오늘까지 그린다")


def test_trend_today_partial_column():
    """낮의 「오늘(집계 중)」 칸 — 선에서 떼어 점선으로 그리는 칸.

    지키는 것:
      1) 오늘이 덜 찼을 때만 칸이 생긴다 (다 찬 저녁부터는 오늘이 마지막 버킷이라 None —
         둘 다 그리면 같은 날이 두 번 선다)
      2) 칸 값은 오늘 저장된 회차에서 센다 · 축 밑에 `1/2회차`를 적는다
      3) 오늘 회차가 하나도 없으면 빈 원을 안 찍는다 (0건이 아니라 "모른다")
      4) 크로스헤어가 읽는 data-buckets에 이 칸이 함께 들어간다 — 빠지면 JS가 거꾸로
         계산하는 step이 어긋나 선과 크로스헤어의 x가 갈린다
    """
    import re

    from app import last_slot_topup, trend_chart, trend_data

    _require_isolation()
    today = datetime.now().date()
    today_s = today.isoformat()
    yday_s = (today - timedelta(days=1)).isoformat()

    st = settings.load_settings()
    st["schedule_groups"] = [{"name": "매일", "enabled": True,
                              "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
                              "times": [{"start": "09:00", "end": "11:00", "enabled": True},
                                        {"start": "11:00", "end": "14:00", "enabled": True}]}]
    settings.SETTINGS_FILE.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")

    # 3) 아직 오늘 회차가 하나도 없는 상태
    col = trend_data.today_partial_column(today_s, ["세제"])
    assert col and col["label"] == "오늘" and col["sub"] == "0/2회차"
    assert col["record"] is False
    buckets, unit = trend_data.build_buckets(yday_s, yday_s)
    trend_data.fill_bucket_status(buckets, unit, {yday_s: []}, today_s)
    series = [{"word": "세제", "color": "#000", "values": [1]}]
    svg = trend_chart.render_chart("t", buckets, series, unit, today=col, label_today=False)
    assert "오늘" in svg and "0/2회차" in svg
    assert svg.count("<circle") == 0, "오늘 회차가 없는데 빈 원을 찍어 0건이라고 말했다"

    # 1)·2) 한 회차가 저장되면 그 회차 기사에서 센다
    storage.save_run("11:00", [_article("세제 개편안 발표")], run_at=f"{today_s}T11:01:00")
    col = trend_data.today_partial_column(today_s, ["세제", "없는말"])
    assert col["sub"] == "1/2회차" and col["record"] is True
    assert col["values"] == {"세제": 1, "없는말": 0}

    svg = trend_chart.render_chart("t", buckets, series, unit, today=col, label_today=False)
    assert svg.count("<circle") == 1, "오늘 칸의 빈 원이 안 찍혔다"
    assert 'stroke-dasharray="3 3"' in svg, "오늘 칸을 가르는 점선이 없다"
    tip = json.loads(re.search(r"data-buckets='(\[.*?\])' data-series", svg).group(1))
    assert len(tip) == len(buckets) + 1 and tip[-1]["partial"], (
        "크로스헤어가 읽는 칸 목록에 오늘 칸이 빠졌다 — step이 어긋난다"
    )

    # 4) 라벨 숫자는 계속 다 찬 마지막 날 값이다(label_today=False)
    assert ">1</tspan>" in svg

    # 1) 오늘이 다 차면 칸이 사라진다 — 그때는 오늘이 마지막 버킷으로 선다
    storage.save_run("14:00", [_article("세제 추가")], run_at=f"{today_s}T14:01:00")
    last_run = last_slot_topup.find_run(today_s, "14:00")
    storage.update_run_articles({**last_run, "topup_at": f"{today_s}T14:31:00"}, last_run["articles"])
    assert trend_data.today_is_complete(today_s)
    assert trend_data.today_partial_column(today_s, ["세제"]) is None, (
        "오늘이 다 찼는데 집계 중 칸까지 그려 같은 날이 두 번 섰다"
    )

    print("✓ 추이 오늘 칸: 덜 찬 낮에만 점선으로 떼어 그리고, 회차가 없으면 점을 안 찍는다")


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


def test_env_key_transplant():
    """.env 씨앗 옮겨심기 — 키가 사는 자리를 설정 파일 한 곳으로 모은다.

    1) 설정에 없고 .env에만 있으면 옮겨 심고 .env의 그 줄을 지운다
    2) 설정에도 있으면 설정값을 덮지 않고 .env의 옛 줄만 지운다
    3) 메일 서버가 네이버·Gmail이 아니면 이메일만 그대로 둔다(지우면 메일을 못 보낸다)
    4) 고치기 전에 .env 백업을 남기고, 두 번째 실행은 아무것도 하지 않는다
    5) 키가 아닌 줄(TELEGRAM_CHAT_ID)·주석은 건드리지 않는다
    """
    _require_isolation()

    env_text = (
        "# 내가 적어둔 주석\n"
        "NAVER_CLIENT_ID=naverid0001\n"
        'NAVER_CLIENT_SECRET="naversec0002"\n'
        "\n"
        "TELEGRAM_BOT_TOKEN=111:AAAtoken\n"
        "TELEGRAM_CHAT_ID=99887766\n"
        "EMAIL_SMTP_HOST=smtp.naver.com\n"
        "EMAIL_SMTP_PORT=587\n"
        "EMAIL_SENDER_ADDRESS=me@naver.com\n"
        "EMAIL_SENDER_PASSWORD=apppw0003\n"
        "ANTHROPIC_API_KEY=sk-ant-0004\n"
    )

    def run(text, saved=None):
        """임시 .env 하나를 만들어 옮겨심기를 돌린다 — 실제 .env는 건드리지 않는다."""
        tmp_dir = Path(tempfile.mkdtemp())
        env_file = tmp_dir / ".env"
        env_file.write_text(text, encoding="utf-8")
        cred_file = tmp_dir / "credentials.json"
        if saved is not None:
            cred_file.write_text(json.dumps(saved), encoding="utf-8")
        with patch.object(credentials, "ENV_FILE", env_file), \
             patch.object(credentials, "BASE_DIR", tmp_dir), \
             patch.object(credentials, "CREDENTIALS_FILE", cred_file):
            result = credentials.transplant_env_keys()
            again = credentials.transplant_env_keys()
            leftover = credentials.env_leftover_keys()
            backups = [b.name for b in credentials.env_backup_files()]
        saved_now = json.loads(cred_file.read_text(encoding="utf-8")) if cred_file.exists() else {}
        return result, again, leftover, backups, env_file.read_text(encoding="utf-8"), saved_now

    # 1) 전부 .env에만 있는 상태
    result, again, leftover, backups, env_now, saved_now = run(env_text)
    assert result["moved"] == ["네이버 검색 API", "Claude API 키", "텔레그램 봇 토큰", "이메일 보내는 계정"], result
    assert result["cleared"] == [] and result["kept"] == []
    assert saved_now["naver"] == {
        "client_id": "naverid0001", "client_secret": "naversec0002",
        "saved_at": saved_now["naver"]["saved_at"], "from_env": True,
    }, saved_now["naver"]
    assert saved_now["llm"]["api_key"] == "sk-ant-0004"
    assert saved_now["telegram"]["bot_token"] == "111:AAAtoken"
    assert saved_now["email"]["service"] == "naver" and saved_now["email"]["password"] == "apppw0003"
    assert leftover == [], f".env에 키가 남았다: {leftover}"
    # 5) 키가 아닌 줄·주석은 그대로
    assert "# 내가 적어둔 주석" in env_now and "TELEGRAM_CHAT_ID=99887766" in env_now
    assert "NAVER_CLIENT_ID" not in env_now and "ANTHROPIC_API_KEY" not in env_now
    # 4) 백업 한 벌, 두 번째 실행은 아무것도 안 한다
    assert backups == [".env.backup-" + date.today().isoformat()], backups
    assert again == {"moved": [], "cleared": [], "kept": [], "backup": None}, again

    # 2) 설정에 이미 있는 키는 덮지 않고 .env 줄만 지운다
    result, _, leftover, _, _, saved_now = run(
        env_text, saved={"llm": {"api_key": "sk-ant-NEW", "saved_at": "2026-09-28T10:00:00"}}
    )
    assert saved_now["llm"]["api_key"] == "sk-ant-NEW", "설정값을 .env 옛 값이 덮었다"
    assert result["cleared"] == ["Claude API 키"] and "Claude API 키" not in result["moved"]
    assert leftover == []

    # 3) 모르는 메일 서버 — 이메일만 그대로 둔다
    result, _, leftover, _, env_now, saved_now = run(env_text.replace("smtp.naver.com", "smtp.korea.kr"))
    assert result["kept"] == ["이메일 보내는 계정"], result
    assert "email" not in saved_now
    assert leftover == ["EMAIL_SMTP_HOST", "EMAIL_SMTP_PORT", "EMAIL_SENDER_ADDRESS", "EMAIL_SENDER_PASSWORD"]
    assert "EMAIL_SENDER_PASSWORD=apppw0003" in env_now, "옮기지 못한 키를 지워버리면 메일을 못 보낸다"
    assert saved_now["llm"]["api_key"] == "sk-ant-0004", "이메일 때문에 나머지까지 멈추면 안 된다"

    # .env가 없거나 값이 빈 줄뿐이면 백업조차 뜨지 않는다
    _, _, _, backups, _, _ = run("NAVER_CLIENT_ID=\nANTHROPIC_API_KEY=\n")
    assert backups == [], backups

    # 6) 백업은 보관 기간이 지나면 앱이 스스로 치운다(담당자가 지울 파일이 아니다).
    #    시각은 「백업을 뜬 때」여야 한다 — copy2로 원본 .env의 시각을 물려받으면
    #    몇 년 전 .env의 백업이 뜨자마자 지워진다.
    tmp_dir = Path(tempfile.mkdtemp())
    env_file = tmp_dir / ".env"
    env_file.write_text(env_text, encoding="utf-8")
    old_time = (datetime.now() - timedelta(days=400)).timestamp()
    os.utime(env_file, (old_time, old_time))
    with patch.object(credentials, "ENV_FILE", env_file), \
         patch.object(credentials, "BASE_DIR", tmp_dir), \
         patch.object(credentials, "CREDENTIALS_FILE", tmp_dir / "credentials.json"):
        credentials.transplant_env_keys()
        assert len(credentials.env_backup_files()) == 1
        credentials.cleanup_env_backups()
        assert len(credentials.env_backup_files()) == 1, "방금 뜬 백업을 지워버렸다"
        fresh = credentials.env_backup_files()[0]
        stale = (datetime.now() - timedelta(days=credentials.ENV_BACKUP_RETENTION_DAYS + 1)).timestamp()
        os.utime(fresh, (stale, stale))
        credentials.cleanup_env_backups()
        assert credentials.env_backup_files() == [], "보관 기간이 지난 백업이 남았다"

    print("47) 키 옮겨심기: 통과 (.env -> 설정 파일, 설정값 우선, 모르는 메일 서버는 그대로, 백업·두 번째 실행 no-op)")



def test_search_operator_is_one_rule():
    """검색어 연산자가 앱 어디서나 같은 뜻인지 지킨다 — 띄어쓰기=덩어리, `+`=AND.

    이 규칙이 칸마다 갈려 있으면 담당자는 알 길이 없다. 실제로 갈려 있었다(2026-09-30):
    기관 기사 찾기는 띄어쓰기를 덩어리로 읽는데, 정기·수시 검색어는 문자열을 네이버에
    그대로 넘겨 네이버가 공백을 낱말로 쪼개 AND로 돌렸고(실측: `가상자산 과세` =
    `가상자산+과세` = 27,200건), 정책 단어 추이·형광펜은 `+`를 글자 그대로 찾아
    조합 검색어가 늘 0건이었다.

    규칙을 정하는 자리는 app.search_terms 하나다. 이 테스트는 (1) 그 번역이 맞는지와
    (2) 다른 파일이 자기 자리에서 규칙을 다시 만들지 않는지를 본다 — 새 검색어 칸이
    직접 `split("+")`을 하거나 키워드를 네이버에 그대로 넘기면 여기서 깨진다.
    """
    _require_isolation()
    from app.highlight import highlight_keywords
    from app.search_terms import naver_query, split_terms
    from app.trend_data import word_hits

    # (1) 띄어쓰기는 가르지 않고, `+`로만 가른다.
    assert split_terms("가상자산 과세+이형일") == ["가상자산 과세", "이형일"], split_terms("가상자산 과세+이형일")
    assert split_terms("부처 핸즈업") == ["부처 핸즈업"]

    # (2) 네이버 문법으로 옮길 때 덩어리만 겹따옴표로 감싼다(공백=AND인 네이버 규칙 상쇄).
    assert naver_query("가상자산 과세+이형일") == '"가상자산 과세" 이형일'
    assert naver_query("가상자산+과세") == "가상자산 과세"  # 낱말끼리는 AND 그대로
    assert naver_query("재경부") == "재경부"
    # 담당자가 직접 적은 겹따옴표는 두 겹으로 감싸지 않는다(질의가 깨진다).
    assert naver_query('"가상자산 과세"+국회') == '"가상자산 과세" 국회'

    # (3) 글자 대조 쪽(추이)도 같은 규칙 — 덩어리와 AND가 다른 값을 낸다.
    articles = [
        {"title": "가상자산 과세 유예 논란", "summary": "이형일 부총리"},
        {"title": "가상자산 시장 점검", "summary": "과세 문제는 별개"},
    ]
    assert word_hits("가상자산 과세", articles) == 1, "덩어리는 붙어 있는 것만 세야 한다"
    assert word_hits("가상자산+과세", articles) == 2, "`+`는 조각이 전부 든 기사를 세야 한다"

    # (4) 형광펜도 같은 규칙 — `+` 든 단어는 조각마다 칠하고, data-word는 등록 단어를
    #     그대로 둔다(팝오버의 색 바꾸기가 그 속성으로 자리를 찾는다).
    out = highlight_keywords("가상자산 과세 논란", [{"word": "가상자산+과세", "color": 0}])
    assert out.count("hl-word") == 2, f"조각마다 칠해야 한다 — {out}"
    assert 'data-word="가상자산+과세"' in out, f"data-word는 등록 단어 그대로 — {out}"

    # (5) 규칙을 다시 만드는 자리가 생기지 않았는지.
    app_dir = Path(__file__).resolve().parent.parent
    sources = sorted(app_dir.glob("app/**/*.py")) + [app_dir / "main.py"]
    for label, needle, allowed in [
        ("검색어를 `+`로 가르는 곳", 'split("+")', {"app/search_terms.py"}),
        ("네이버 질의를 만드는 곳", '"query": naver_query(', {"app/naver_api.py"}),
    ]:
        found = {
            str(path.relative_to(app_dir))
            for path in sources
            if needle in path.read_text(encoding="utf-8")
        }
        extra = found - allowed
        assert not extra, (
            f"{label}가 늘었습니다 — {sorted(extra)}. 검색어 규칙은 app.search_terms "
            f"한 곳에서만 정합니다(split_terms / naver_query를 부르세요). "
            f"자기 자리에서 다시 만들면 그 칸만 뜻이 달라집니다."
        )
        assert found, f"{label}를 못 찾았습니다 — 규칙이 옛 이름을 보고 있습니다({needle})"

    # 키워드를 번역 없이 네이버에 넘기던 옛 형태가 되살아나지 않았는지.
    naver_src = (app_dir / "app/naver_api.py").read_text(encoding="utf-8")
    assert '"query": keyword' not in naver_src, (
        "키워드를 네이버에 그대로 넘기고 있습니다 — naver_query(keyword)로 옮겨야 "
        "띄어쓰기가 덩어리로 검색됩니다."
    )

def test_outlet_override_is_one_place():
    """언론사명 직접 수정 — **입히는 자리가 `apply_summary_overrides()` 한 곳**인지.

    네이버 미러가 아닌 언론사 자체 도메인 기사는 oid가 없어 resolve_outlet이 이름을 못 찾고
    도메인 문자열(aienews.co.kr)을 그대로 돌려준다. 그 값은 제목과 달리 **보고서 한 줄
    (`ㅇ (언론사) 제목`)과 엑셀 「언론사명」으로 그대로 나가므로** 담당자가 고칠 길이 있어야
    하고, 「이 기사의 언론사가 무엇인가」는 제목과 같은 **기사에 관한 사실**이라 같은 칸
    (summary_overrides, URL 키 전역)에 산다.

    핵심은 저장이 아니라 **입히는 자리가 한 곳**이라는 것이다 — 화면·복사·txt·발송·엑셀이
    코드를 더 안 고쳐도 따라온다. 화면마다 따로 입히면 "화면엔 고친 이름, 보고서엔 도메인"
    이라는 어긋남이 생긴다(사진 추정 해제·숨김 판정에서 이미 세워둔 규칙이다).

    같이 보는 것: 부분 저장(언론사만 고쳐도 제목·요약은 그대로) · 편집 창 네 벌이 안
    어긋나는지(editSummary JS가 파일마다 복제돼 있다).
    """
    _require_isolation()
    from app import summary_overrides as so

    url = "https://aienews.co.kr/news/articleView.html?idxno=12345"
    article = {"outlet": "aienews.co.kr", "title": "가상자산 과세 2027년 시행 앞두고 기류 변화",
               "url": url, "summary": "2027년 시행을 앞둔 가상자산 과세를 두고 유예론이 나온다.",
               "pub_date": "2026-10-12T12:47:00+09:00", "group": "세제"}

    with tempfile.TemporaryDirectory() as tmp, \
         patch.object(so, "SUMMARY_OVERRIDES_FILE", Path(tmp) / "overrides.json"):
        # (1) 언론사만 고친다 — 제목·요약은 손대지 않는다(부분 저장).
        #     언론사 칸은 ✏️「기사 정보 직접 수정」에서만 들어오므로 manual=True다.
        #     [추가: 2026-10-12] 손으로 친 칸 이름이 같이 적힌다 — 「제목 다시 불러오기」가
        #     여러 건을 덮을 때 이 표시를 보고 건너뛴다(archive/TITLE_CHANGE_DESIGN.md §4).
        so.set_summary_override(url, None, None, "AI뉴스", manual=True)
        saved = so.load_summary_overrides()[url]
        assert saved == {"outlet": "AI뉴스", "manual": ["outlet"]}, saved

        # (2) 입히는 자리 한 곳이 그걸 입힌다 — 나머지 필드는 그대로.
        applied = so.apply_summary_overrides([article])[0]
        assert applied["outlet"] == "AI뉴스", applied["outlet"]
        assert applied["title"] == article["title"], "제목이 같이 바뀌면 안 됩니다"
        assert applied["pub_date"] == article["pub_date"], "발행시각이 같이 바뀌면 안 됩니다"

        # (3) 화면을 그리는 쪽은 이 한 목록만 받는다(render_confirmed_content 등이 입구에서
        #     apply_summary_overrides를 한 번 거친다) — 그래서 화면과 **보고서 한 줄**이
        #     같은 값을 본다. 화면마다 따로 입히면 여기서 둘이 갈린다.
        page = renderer.render_page(
            "14:00", so.apply_summary_overrides([article]), ["가상자산"], [],
            "ㅇ ({outlet}) {title}", "<{section}>",
        )
        assert "AI뉴스" in page, "화면에 고친 언론사가 안 보입니다"
        assert "ㅇ (AI뉴스) " in page, "복사·txt 한 줄이 옛 도메인 그대로입니다"
        assert "aienews.co.kr<" not in page, "화면에 옛 도메인이 남아 있습니다"

        # (4) 엑셀 「언론사명」도 같은 목록에서 구워진다.
        rows = renderer.build_export_rows(
            [{"name": "세제", "articles": so.apply_summary_overrides([article])}],
            "2026-10-12", "14:00", {},
        )
        assert rows[0]["언론사명"] == "AI뉴스", rows[0]["언론사명"]

        # (5) 되돌릴 수 있게 빈 칸은 기존 값을 그대로 둔다 — 제목만 다시 고쳐도 언론사는 유지.
        so.set_summary_override(url, "고친 제목", None, None, manual=True)
        again = so.load_summary_overrides()[url]
        assert again == {"outlet": "AI뉴스", "title": "고친 제목",
                         "manual": ["outlet", "title"]}, again

    # (6) 편집 창은 파일마다 복제돼 있다 — 네 벌 중 하나라도 언론사 칸을 빠뜨리면
    #     그 화면에서만 고칠 수 없게 되는데, 저장소가 전역이라 담당자는 왜 안 되는지 모른다.
    root = Path(__file__).resolve().parent.parent
    for name in ("renderer.py", "preview_renderer.py", "history_renderer.py", "live_renderer.py"):
        src = (root / "app" / name).read_text(encoding="utf-8")
        head = src.index("function editSummary(btn) {{")
        block = src[head:src.index("\n}}\n", head)]
        assert "esf-outlet" in block, f"{name}: 편집 창에 언론사 칸이 없습니다"
        assert "outlet: outletInput.value" in block, f"{name}: 저장할 때 언론사를 안 보냅니다"
        assert ".esf-row {{" in src, f"{name}: .esf-row CSS가 빠졌습니다"
    # 수시는 form 제출이라 모양이 다르지만 같은 저장소로 간다.
    adhoc_src = (root / "app" / "adhoc" / "renderer.py").read_text(encoding="utf-8")
    assert 'name="outlet"' in adhoc_src, "수시 편집 상자에 언론사 칸이 없습니다"
    # 메뉴 이름은 앱 어디서나 하나다(제목만 고치는 창이 아니게 됐다).
    for name in ("renderer.py", "live_renderer.py"):
        src = (root / "app" / name).read_text(encoding="utf-8")
        assert "기사 정보 직접 수정" in src, f"{name}: ⋯ 메뉴 이름이 안 맞습니다"
    assert "기사 정보 직접 수정" in adhoc_src, "수시: ⋯ 메뉴 이름이 안 맞습니다"

    print("48) 언론사명 직접 수정: 통과 (입히는 자리 한 곳 — 화면·보고서 한 줄·엑셀, 부분 저장, 편집 창 네 벌)")


def test_photo_override_is_one_place():
    """사진 추정 해제 — **입히는 자리가 `looks_like_photo_caption()` 맨 앞 한 곳**인지.

    이게 이 테스트의 핵심이다. 판정을 한 곳에만 두면 화면 여섯·「📷 사진 추정 (N)」 건수·
    수시 「사진 제외」·홈 부정 추정 범위가 코드를 더 안 고쳐도 따라온다. 화면마다 따로
    걸러내는 식으로 새면 "흐림은 사라졌는데 건수엔 남아 있는" 어긋남이 생긴다 —
    숨김 판정(is_hidden)·exclude_photo_articles에서 이미 같은 이유로 세워둔 규칙이다.

    나머지: 표식([포토])까지 이긴다 · 되돌리면 앱의 추정으로 돌아간다 ·
    ⋯ 메뉴 줄은 사진으로 잡힌 행과 끈 행에만 붙는다.
    """
    # 원문 층은 `photo_body.start()`를 안 부른 테스트에선 네트워크를 안 탄다(_started가 거짓)
    # — 그래서 이 테스트는 판정 인자를 따로 넘기지 않고 기본값 그대로 부른다.
    _require_isolation()
    from app.filters import looks_like_photo_caption, exclude_photo_articles
    from app.photo_overrides import set_photo_override, is_photo_cleared, photo_override

    guessed = {"url": "https://n.news.naver.com/mnews/article/001/11",
               "title": "발언하는 이형일 부총리", "outlet": "뉴스1", "summary": "재경부"}
    tagged = {"url": "https://n.news.naver.com/mnews/article/001/12",
              "title": "[포토] 답변하는 후보자", "outlet": "뉴스1", "summary": "재경부"}
    plain = {"url": "https://n.news.naver.com/mnews/article/001/13",
             "title": "재경부 예산 설명", "outlet": "뉴스1", "summary": "재경부"}

    # (1) 끄기 전 — 앱의 추정 그대로.
    assert looks_like_photo_caption(guessed), "제목 추정이 안 걸렸습니다"
    assert looks_like_photo_caption(tagged), "[포토] 표식이 안 걸렸습니다"
    assert not looks_like_photo_caption(plain), "일반 기사가 사진으로 잡혔습니다"
    assert photo_override(guessed["url"]) is None, "끄기 전엔 적어 둔 값이 없어야 합니다"

    # (2) 끄면 **추정도 표식도** 이긴다 — 담당자는 그 기사를 보고 누른 것이다.
    set_photo_override(guessed["url"], False)
    set_photo_override(tagged["url"], False)
    assert not looks_like_photo_caption(guessed), "해제가 제목 추정을 못 이겼습니다"
    assert not looks_like_photo_caption(tagged), \
        "해제가 [포토] 표식을 못 이겼습니다 — 담당자 판단이 표식보다 우선입니다"
    assert is_photo_cleared(guessed["url"]), "해제 기록이 안 남았습니다"

    # (3) 판정을 읽는 **다른 길**도 같이 따라온다(한 곳에서 입히므로).
    kept = [a["url"] for a in exclude_photo_articles([guessed, tagged, plain])]
    assert kept == [guessed["url"], tagged["url"], plain["url"]], \
        "exclude_photo_articles가 해제를 못 봤습니다 — 판정이 두 벌로 갈렸습니다"

    # (4) 화면: 흐림·칩이 사라지고 ⋯ 메뉴 줄이 「되돌리기」로 바뀐다.
    from app.renderer import render_article
    html_cleared = render_article(guessed, [])
    assert "is-photo" not in html_cleared and "사진 추정</span>" not in html_cleared, \
        "해제했는데 화면이 아직 흐리게 그립니다"
    assert "사진 추정 되돌리기" in html_cleared, "해제한 행에 되돌리는 자리가 없습니다"
    assert "사진 추정 해제" not in html_cleared, "되돌리는 행에 「해제」가 같이 떠 있습니다"

    html_photo = render_article({**guessed, "url": "https://n.news.naver.com/mnews/article/001/14"}, [])
    assert "사진 추정 해제" in html_photo, "사진으로 잡힌 행에 해제하는 자리가 없습니다"
    assert "is-photo" in html_photo, "해제 안 한 사진 행이 안 흐려집니다"

    html_plain = render_article(plain, [])
    assert "사진 추정 해제" not in html_plain and "사진 추정 되돌리기" not in html_plain, \
        "사진과 무관한 행에까지 메뉴 줄이 붙었습니다 — 누를 일 없는 줄로 모든 메뉴가 길어집니다"

    # (5) 되돌리면 앱의 추정으로 돌아간다(끈 적 없는 상태와 같아야 한다).
    set_photo_override(guessed["url"], None)
    assert photo_override(guessed["url"]) is None, "되돌렸는데 기록이 남았습니다"
    assert looks_like_photo_caption(guessed), "되돌렸는데 추정이 안 살아났습니다"
    print("48) 사진 추정 해제: 통과 (판정 한 곳, 표식까지 이김, 사진 행에만 메뉴 줄, 되돌리면 추정 복귀)")


def _photo_badge_declarations(src: str, name: str = "") -> set:
    """파일 텍스트에서 `.photo-badge` 규칙의 **선언만** 뽑는다(주석·줄바꿈은 떼고).

    같은 값인지를 글자 배치가 아니라 선언 집합으로 보는 이유는, 네 파일이 줄바꿈 자리만
    조금씩 다르게 복제돼 있기 때문이다(한 파일은 white-space·position을 한 줄에 붙여 뒀다).
    """
    import re

    # 줄 맨 앞에서 시작하는 규칙만 — `body.photo-gather .photo-badge {{ display: none; }}`
    # (사진 모아 보는 동안 칩을 감추는 줄)이 파일에 따라 먼저 나온다.
    m = re.search(r"(?m)^\s*\.photo-badge \{\{", src)
    assert m, f"{name}: .photo-badge 규칙이 아예 없습니다 — 그 화면에선 알약이 맨 글자로 뜹니다"
    head = m.start()
    body = src[m.end():src.index("}}", m.end())]
    body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)       # 설명 주석은 값이 아니다
    return {" ".join(d.split()) for d in body.split(";") if d.strip()}


def test_photo_badge_css_is_in_every_screen():
    """📷 사진 추정 알약 — **마크업을 내보내는 화면은 CSS도 같이 낸다**는 계약.

    `render_article`은 화면을 가리지 않고 `<span class="photo-badge">`와 `is-photo`를
    내보내는데, CSS는 화면 파일마다 복제돼 있다. 규칙이 빠진 화면에선 그 span이
    `.title-row`(flex) 안에서 블록으로 부풀어 **알약도 아닌 서식 없는 맨 글자**로 뜨고,
    흐림 규칙까지 없으면 사진 행이 눈에 띄지도 않는다 — 2026-10-16 정기 보관함이
    실제로 그 상태였다(배지 19개가 16px 맨 글자, 행도 안 흐림). 서버 로그엔 아무것도
    안 남고 테스트도 다 통과하므로, 누가 화면을 열어 보기 전까지 아무도 모른다.

    그래서 두 겹으로 지킨다:
      (1) 소스 — 네 벌(`renderer`·`preview_renderer`·`history_renderer`·`adhoc/renderer`)의
          선언이 **글자 하나까지 같은지**. 기관 기사 찾기는 행이 한 줄이라 알약을
          inline-flex로 따로 짜 뒀으므로 값까지 같을 수 없다 — 거기선 「알약 모양인가」만 본다.
      (2) 렌더링 — 실제로 그린 확정본·정기 보관함 HTML 안에 **마크업과 규칙이 함께**
          들어 있는지. 소스 검사만 두면 "규칙은 있는데 그 화면 템플릿에 안 꽂혔다"를 놓친다.
    """
    _require_isolation()
    root = Path(__file__).resolve().parent.parent
    screens = ("renderer.py", "preview_renderer.py", "history_renderer.py", "adhoc/renderer.py")
    sources = {name: (root / "app" / name).read_text(encoding="utf-8") for name in screens}

    # (1) 네 벌의 선언이 같은 값인가 — 기준은 render_article이 사는 renderer.py다.
    base = _photo_badge_declarations(sources["renderer.py"], "renderer.py")
    assert "border-radius: var(--r-pill)" in base and "font-size: var(--fs-xs)" in base, base
    for name in screens[1:]:
        got = _photo_badge_declarations(sources[name], name)
        assert got == base, (
            f"{name}의 .photo-badge가 renderer.py와 갈렸습니다 — 한쪽만 고치면 "
            f"그 화면의 알약만 다르게 보입니다.\n  여기만 있음: {sorted(got - base)}"
            f"\n  여기에 없음: {sorted(base - got)}"
        )

    # 흐림과 "또렷해지는 조건" — 체크 여부는 화면마다 다르지만(보관함엔 체크박스가 없다)
    # hover와 ⋯ 메뉴는 넷 다 있어야 한다. 메뉴 안에 「사진 추정 해제」가 있어서,
    # 흐린 채로 두면 판정이 맞는지 보면서 누를 수가 없다.
    for name, src in sources.items():
        assert ".article.is-photo {{ opacity: 0.55; }}" in src, f"{name}: 사진 행 흐림 규칙이 없습니다"
        assert ".article.is-photo:hover" in src, f"{name}: 마우스를 올려도 안 또렷해집니다"
        assert ".article.is-photo:has(.more-wrap.is-open)" in src, \
            f"{name}: ⋯ 메뉴를 연 행이 흐린 채로 남습니다 — 그 안에 「사진 추정 해제」가 있습니다"

    # 다섯째 — 기관 기사 찾기는 선택자(.art)도 레이아웃도 달라 값까지 같지 않다.
    agency = (root / "app" / "agency_archive_renderer.py").read_text(encoding="utf-8")
    assert ".art.is-photo {{ opacity: 0.55; }}" in agency, "기관 기사 찾기: 흐림 값이 다릅니다"
    assert ".art.is-photo:hover" in agency, "기관 기사 찾기: 마우스를 올려도 안 또렷해집니다"
    agency_badge = _photo_badge_declarations(agency, "agency_archive_renderer.py")
    assert "border-radius: var(--r-pill)" in agency_badge and "font-size: var(--fs-xs)" in agency_badge, \
        f"기관 기사 찾기: 알약이 아닙니다 {sorted(agency_badge)}"

    # (2) 그린 화면에 마크업과 규칙이 **함께** 있는가.
    photo = {"outlet": "뉴스1", "title": "발언하는 이형일 부총리",
             "url": "https://n.news.naver.com/mnews/article/001/21",
             "summary": "재경부", "pub_date": "2026-10-16T14:00:00+09:00", "group": "인사"}
    line_template, sub_format = "ㅇ ({outlet}) {title}", "<{section}>"

    confirmed = renderer.render_page("14:00", [photo], ["재경부"], [], line_template, sub_format)
    today = datetime.now().strftime("%Y-%m-%d")
    storage.save_run("14:00", [photo], run_at=f"{today}T14:01:00")
    archive = history_renderer.render_history_page(
        storage.list_runs_for_date(today), [], line_template, sub_format,
        all_dates=[today], latest_key=storage.latest_run_key(),
    )
    for screen, page in (("확정본", confirmed), ("정기 보관함", archive)):
        assert 'class="photo-badge"' in page, f"{screen}: 사진 기사인데 배지 마크업이 없습니다"
        assert "article is-photo" in page, f"{screen}: 사진 행에 is-photo가 안 붙었습니다"
        assert ".photo-badge {" in page, \
            f"{screen}: 배지를 내보내면서 CSS는 안 냅니다 — 화면엔 서식 없는 맨 글자로 뜹니다"
        assert ".article.is-photo { opacity: 0.55; }" in page, \
            f"{screen}: 배지는 있는데 행이 안 흐려집니다"

    print("61) 📷 사진 추정 알약: 통과 (네 벌 같은 값, 흐림·또렷 조건, 그린 화면에 마크업+CSS 함께)")


def test_agency_archive_filters():
    """기관 기사 찾기 거르기 줄 — **원문을 새로 받지 않는 것**이 이 테스트의 핵심이다.

    사진 판정(app.filters.looks_like_photo_caption)은 원문 층에서 모르는 URL을 만나면
    뒤에서 받아오도록 줄을 세운다. 다른 화면은 그 날 기사 수백 건이라 괜찮지만, 이
    화면은 한 번에 1년치(하루 400여 건 × 365)를 훑고 원문 판정은 90일만 남는다 —
    기본값 그대로 두면 화면을 열 때마다 옛 기사 수만 건을 새로 받으려 든다.
    그래서 `fetch_body=False`로 **적어 둔 판정만 읽는다**. 여기가 깨지면 담당자가
    이 화면을 열 때마다 네이버에 수만 번 요청이 나간다.

    나머지: 칸끼리 AND · 말머리 둘끼리만 OR · 필터는 **미루기(EAGER_ROWS) 전에** 걸어
    가져가기와 화면이 같은 목록을 보는 것.
    """
    _require_isolation()
    from unittest.mock import patch
    from app import agency_archive, photo_body
    from app.agency_archive_renderer import render_agency_archive_page
    from app.settings import save_outlet_selection

    day = datetime.now().strftime("%Y-%m-%d")
    photo_title = "발언하는 이형일 부총리"          # 제목 모양만으로 사진 추정에 걸린다
    articles = [
        {"url": "https://n.news.naver.com/mnews/article/001/1", "title": "[단독] 재경부 세제 개편",
         "outlet": "KBS", "summary": "재경부", "pub_date": f"{day}T10:00:00+09:00"},
        {"url": "https://n.news.naver.com/mnews/article/002/2", "title": "[속보] 재경부 추경 편성",
         "outlet": "블로그일보", "summary": "재경부", "pub_date": f"{day}T09:00:00+09:00"},
        {"url": "https://n.news.naver.com/mnews/article/003/3", "title": photo_title,
         "outlet": "KBS", "summary": "재경부 사진", "pub_date": f"{day}T08:00:00+09:00"},
        {"url": "https://n.news.naver.com/mnews/article/004/4", "title": "재경부 예산 설명",
         "outlet": "KBS", "summary": "재경부", "pub_date": f"{day}T07:00:00+09:00"},
        # 옛 고정 13곳엔 없지만 설정에서 체크할 수 있는 언론사 — (4)가 이 줄을 본다.
        {"url": "https://n.news.naver.com/mnews/article/005/5", "title": "뉴스1 재경부 국감",
         "outlet": "뉴스1", "summary": "재경부", "pub_date": f"{day}T06:00:00+09:00"},
    ]
    agency_archive._save_day({"date": day, "articles": articles, "last_seen": "", "collected_at": ""})
    # 「선택 언론사만」은 설정 체크를 보므로 먼저 골라 둔다 — 0곳이면 그 칸은 잠기고
    # 아무것도 안 거른다(아래 (4)에서 그 경우도 검증한다).
    save_outlet_selection(["KBS", "뉴스1"])

    def rows(**filters):
        html_out = render_agency_archive_page(preset="7d", filters=filters)
        return [a["title"] for a in articles if f">{a['title']}<" in html_out or a["title"] in html_out]

    # (1) 원문을 새로 받지 않는다 — 앱이 켜진 상태(_started=True)를 흉내 내고 줄 세우기를 센다.
    queued = []
    with patch.object(photo_body, "_started", True), \
            patch.object(photo_body, "_enqueue_locked", lambda url: queued.append(url)):
        html_all = render_agency_archive_page(preset="7d", filters={})
    assert queued == [], (
        f"기관 기사 찾기가 원문을 받으러 {len(queued)}건 줄을 세웠습니다 — 이 화면은 "
        "looks_like_photo_caption(..., fetch_body=False)로 적어 둔 판정만 읽어야 합니다."
    )

    # (2) 사진 추정은 화면에 표시되고(흐리게 + 알약), 「빼고」를 켜면 목록에서 빠진다.
    # 카메라는 이모지가 아니라 SVG다 — 다른 네 화면과 같은 `icon("camera")`.
    assert "is-photo" in html_all and "사진 추정</span>" in html_all, "사진 추정 표시가 없습니다"
    assert "📷" not in html_all, "기관 기사 찾기에 이모지 카메라가 남아 있습니다"
    assert photo_title not in render_agency_archive_page(preset="7d", filters={"nophoto": 1}), \
        "「사진 추정 빼고」가 사진 행을 못 뺐습니다"

    # (3) 말머리 둘은 서로 OR, 다른 칸과는 AND.
    both = render_agency_archive_page(preset="7d", filters={"scoop": 1, "flash": 1})
    assert "[단독] 재경부 세제 개편" in both and "[속보] 재경부 추경 편성" in both, "말머리 둘은 OR여야 합니다"
    assert "재경부 예산 설명" not in both, "말머리를 켰는데 말머리 없는 기사가 남았습니다"
    mixed = render_agency_archive_page(preset="7d", filters={"flash": 1, "major": 1})
    assert "[속보] 재경부 추경 편성" not in mixed, "말머리와 언론사 칸은 AND여야 합니다"

    # (4) 「선택 언론사만」은 설정 「언론사 선택 및 순서 지정」의 체크(outlet_order)를 본다.
    # 화면마다 다른 목록을 쓰면 같은 이름의 칸이 화면마다 다른 답을 낸다.
    from app import settings as settings_mod
    major = render_agency_archive_page(preset="7d", filters={"major": 1})
    assert "[단독] 재경부 세제 개편" in major and "블로그일보" not in major, \
        "「선택 언론사만」이 설정 체크(outlet_order)를 안 봅니다"
    assert "뉴스1 재경부 국감" in major, \
        "설정에서 체크한 언론사인데 「선택 언론사만」에서 빠졌습니다 — 화면마다 목록이 갈립니다"

    # 체크한 언론사가 0곳이면 칸은 잠기고, 주소로 켜도 한 건도 안 사라진다.
    saved = settings_mod.load_settings()
    saved["outlet_order"] = []
    settings_mod._write(saved)
    empty = render_agency_archive_page(preset="7d", filters={"major": 1})
    assert "블로그일보" in empty, "체크한 언론사가 0곳인데 「선택 언론사만」이 기사를 걸러냈습니다"
    assert "disabled" in empty, "체크한 언론사가 0곳인데 칸이 안 잠겼습니다"

    # (5) 미루기 전에 걸러야 화면과 가져가기가 같은 목록을 본다.
    src = (Path(__file__).resolve().parent.parent / "app/agency_archive_renderer.py").read_text(encoding="utf-8")
    assert src.index("if not _passes(hits, on):") < src.index("defer = eager_budget is not None"), \
        "필터가 미루기 판정보다 뒤에 있습니다 — 건수와 가져가기가 거짓이 됩니다"

    # (6) 빠른 설정은 화면이 채워 보내는 날짜 칸을 이긴다(아니면 버튼이 아무 일도 못 한다).
    from app.agency_archive_renderer import _current_preset, _resolve_range
    dates = agency_archive.available_dates()
    week = _resolve_range("7d", "", "", dates)
    assert _resolve_range("7d", "2020-01-01", "2020-01-02", dates) == week, \
        "빠른 설정을 눌렀는데 날짜 칸이 이겼습니다"
    assert _current_preset(*week, dates) == "7d", "다시 조회했을 때 눌러 둔 칸이 꺼집니다"

    print("49) 기관 기사 찾기 거르기: 통과 (원문 안 받음, 사진 빼기, 말머리 OR·칸끼리 AND, "
          "선택 언론사 = 설정 체크, 자르기 전 필터, 빠른 설정)")


def test_agency_archive_keeps_every_date():
    """기관 기사 찾기 — **걸린 날짜는 하나도 빠지지 않는다.**

    예전엔 전체 상한(MAX_ROWS=300)에 걸린 날짜가 통째로 사라졌다 — 나흘 2,657건을
    조회해도 오늘 하루가 300건을 다 써서 나머지 사흘이 접힌 줄조차 없이 「없는 날」이
    됐고, 머리줄 건수(300건)는 그날 참값(634건)과 달랐으며, 가져가기도 그 잘린 목록을
    담으면서 내보낸 파일엔 「300건만」이라는 말이 없었다.

    지금은 예산(EAGER_ROWS)이 **자르는 값이 아니라 미루는 값**이다 — 날짜 머리줄과 참
    건수는 언제나 다 그리고, 예산 밖 날짜는 펼칠 때 `/agency-archive/day?`로 받아온다.
    """
    _require_isolation()
    from app import agency_archive
    from app import agency_archive_renderer
    from app.agency_archive_renderer import (
        render_agency_archive_day_fragment,
        render_agency_archive_page,
    )

    today = datetime.now()
    counts = {0: 5, 1: 4, 2: 3}          # 오늘 5건 · 어제 4건 · 그제 3건
    for back, n in counts.items():
        day = (today - timedelta(days=back)).strftime("%Y-%m-%d")
        agency_archive._save_day({
            "date": day,
            "articles": [
                {"url": f"https://n.news.naver.com/mnews/article/00{back}/{i}",
                 "title": f"재경부 소식 {back}-{i}", "outlet": "KBS", "summary": "재경부",
                 "pub_date": f"{day}T1{i}:00:00+09:00"}
                for i in range(n)
            ],
            "last_seen": "", "collected_at": "",
        })

    # (1) 예산을 1건으로 줄여도 **세 날짜 머리줄이 다 나오고 건수는 참값**이다.
    with patch.object(agency_archive_renderer, "EAGER_ROWS", 1):
        html_out = render_agency_archive_page(preset="7d", filters={})
    for back, n in counts.items():
        day = (today - timedelta(days=back)).strftime("%Y-%m-%d")
        assert f'data-date="{day}"' in html_out, f"{day} 날짜 묶음이 통째로 사라졌습니다"
        assert f">{n}건</span>" in html_out, f"{day} 머리줄 건수가 참값({n}건)이 아닙니다"
    assert "lazy-day-holder" in html_out, "예산 밖 날짜가 지연 로딩으로 안 넘어갔습니다"
    assert "건만 보입니다" not in html_out, "숨기는 게 없는데 「N건만 보입니다」가 남아 있습니다"

    # (2) 예산이 넉넉하면 오늘은 처음부터 그려지고, 접힌 날짜는 머리줄만 있다.
    html_out = render_agency_archive_page(preset="7d", filters={})
    assert "재경부 소식 0-0" in html_out, "오늘 기사가 처음부터 안 그려집니다"
    assert "재경부 소식 1-0" not in html_out, "접힌 날짜 내용까지 구워 넣고 있습니다"

    # (3) 지연 조각은 그 하루를 **잘리지 않고** 돌려주고, 가져가기 재료를 같이 싣는다.
    yday = (today - timedelta(days=1)).strftime("%Y-%m-%d")
    frag = render_agency_archive_day_fragment(yday, word="", filters={})
    assert frag is not None and all(f"재경부 소식 1-{i}" in frag for i in range(4)), \
        "지연 조각이 그 날짜를 다 안 돌려줍니다"
    assert 'data-copy-text=' in frag and 'data-rows=' in frag, \
        "지연 조각에 가져가기 재료가 없습니다 — 펼쳐도 복사·엑셀에 안 담깁니다"

    # (4) 낱말·거르기를 같이 받는다 — 안 받으면 펼친 날짜만 조건이 풀린다.
    assert "재경부 소식 1-0" in render_agency_archive_day_fragment(yday, word="재경부", filters={})
    assert render_agency_archive_day_fragment(yday, word="없는낱말", filters={}) is None, \
        "지연 조각이 낱말을 안 봅니다"
    assert render_agency_archive_day_fragment(yday, word="", filters={"scoop": 1}) is None, \
        "지연 조각이 거르기를 안 봅니다"

    # (5) 쌓인 적 없는 날짜는 404(None) — holder가 남아 다시 펼쳐볼 수 있다.
    assert render_agency_archive_day_fragment("2020-01-01", word="", filters={}) is None

    print("50) 기관 기사 찾기 날짜 유지: 통과 (예산은 미루기지 자르기가 아님, 참 건수, 지연 조각이 낱말·거르기·가져가기 재료까지)")


def test_agency_archive_labels():
    """기관 기사 찾기에서 라벨을 **붙인다** — 그리고 그게 라벨 보관함에 거짓으로 안 적힌다.

    이 화면에만 있는 기사(평일 마지막 회차 뒤에 나온 기관 기사)는 회차 파일에 안
    들어가서 확정본·초안·정기 보관함 어디에도 안 나온다 — 라벨을 붙일 **다른 길이
    아예 없다.** 라벨은 이 앱에서 유일하게 나이로 안 지우는 장치라, 그 기사를 남길
    방법이 여기 말고 없다.

    이 테스트가 지키는 것 셋:
      (1) **공용 라벨 장치의 기본값 경로가 그대로다.** 확정본·초안·정기 보관함·수시는
          기사 줄이 `.article`이고 칩을 `<summary>` 밑 별도 줄에 단다. 이 화면을 열려고
          그 장치를 고쳤으므로, 기본값이 바뀌면 **이미 돌고 있는 화면 넷이 조용히
          망가진다**(라벨은 저장됐는데 화면이 안 바뀌어 담당자는 실패로 읽는다).
      (2) 이 화면은 `data-lab-root`·`data-lab-inline`으로 자기 생김새를 알려준다.
          **요약이 없어 `<details>`가 없는 행에도** 붙어야 한다 — 거기가 빠지면 같은
          화면 안에서 어떤 기사는 되고 어떤 기사는 안 되는, 재현이 어려운 버그가 된다.
      (3) 출처가 `agency`라 라벨 보관함이 `회차`·`✓ 스크랩됨`을 안 그린다. 회차에 실린
          적 없는 기사인데 그렇게 적으면 **화면이 아니라 파일에 거짓이 남고** 엑셀
          「스크랩일자·스크랩종료시간·소제목」까지 따라 나간다.
    """
    _require_isolation()
    import tempfile
    from unittest.mock import patch
    from app import agency_archive, labels as labels_mod
    from app.agency_archive_renderer import (
        render_agency_archive_day_fragment,
        render_agency_archive_page,
    )
    from app.labels import attach_label, labels_for_url, snapshot_source
    from app.renderer import label_popover_script
    from app.settings_server import _label_source_tag_html

    day = datetime.now().strftime("%Y-%m-%d")
    with_summary = "https://n.news.naver.com/mnews/article/001/1"
    no_summary = "https://n.news.naver.com/mnews/article/002/2"
    agency_archive._save_day({"date": day, "last_seen": "", "collected_at": "", "articles": [
        {"url": with_summary, "title": "재경부 세제 개편", "outlet": "KBS",
         "summary": "재경부가 세제를 고친다", "pub_date": f"{day}T22:10:00+09:00"},
        # 요약이 없는 기사 — 이 화면은 그런 행에 <details>를 **일부러** 안 그린다(행 높이).
        {"url": no_summary, "title": "재경부 예산 설명", "outlet": "KBS",
         "summary": "", "pub_date": f"{day}T22:20:00+09:00"},
    ]})

    # (1) 공용 장치의 기본값 경로 — 확정본 넷이 쓰는 길이 그대로 남아 있어야 한다.
    js = label_popover_script("127.0.0.1", 8765)
    assert 'el.closest(".article")' in js, \
        "공용 라벨 장치의 기본값(.article)이 사라졌습니다 — 확정본·초안·정기 보관함·수시가 멈춥니다"
    assert 'btn.closest("summary")' in js, \
        "공용 라벨 장치의 기본 칩 자리(<summary> 밑 .lab-row)가 사라졌습니다"
    assert "function labRoot" in js, "줄 찾기가 한 곳(labRoot)으로 안 모여 있습니다"

    # (2) 이 화면은 자기 생김새를 알려준다 — 요약 없는 행에도 빠짐없이.
    html_out = render_agency_archive_page(preset="7d")
    assert html_out.count('data-lab-root data-lab-inline=".a-title"') == 2, \
        "요약 없는 행에 라벨 장치 표시가 안 붙었습니다 — 그 행만 🏷가 먹통이 됩니다"
    assert html_out.count('data-lab-source="agency"') == 2, "🏷에 출처 표시가 빠졌습니다"
    assert 'data-scrap-end=""' in html_out and 'data-group=""' in html_out, \
        "없는 회차·소제목을 지어내고 있습니다"
    # 지연 조각도 같은 버튼을 낸다 — 안 그러면 펼쳐 본 날짜만 🏷가 없다.
    frag = render_agency_archive_day_fragment(day)
    assert frag is not None and frag.count('data-lab-source="agency"') == 2, \
        "지연 조각에 🏷가 빠졌습니다 — 펼쳐 본 날짜만 라벨을 못 답니다"
    assert "이미 쓴 라벨" in frag, "지연 조각의 팝오버에 「이미 쓴 라벨」 목록이 안 실렸습니다"

    # (3) 출처 — 파일에 `agency`로 남고, 라벨 보관함이 회차·스크랩 상태를 안 그린다.
    assert snapshot_source("agency") == {"kind": "agency"}
    # 라벨 저장소는 `_require_isolation()`이 안 막아 준다(그건 ARTICLES_DIR만 본다).
    # 그리고 `app.labels`는 `LABELS_FILE`을 **모듈 상수로** 들고 있어 `app.config`를
    # 패치하면 안 따라온다 — 반드시 `app.labels`를 패치한다(CLAUDE.md 「테스트 격리」).
    with tempfile.TemporaryDirectory() as tmp:
        with patch.object(labels_mod, "LABELS_FILE", Path(tmp) / "labels.json"):
            attach_label(
                {"url": with_summary, "outlet": "KBS", "title": "재경부 세제 개편",
                 "pub_date": f"{day}T22:10:00+09:00", "scrap_date": day, "scrap_end": ""},
                "밤사이", snapshot_source("agency"), group="",
            )
            assert labels_for_url(with_summary) == ["밤사이"]
            saved = json.loads((Path(tmp) / "labels.json").read_text(encoding="utf-8"))
            entry = saved[with_summary]
            assert entry["source"] == {"kind": "agency"}
            assert entry["scrap_end"] == "" and entry["group"] == "", \
                "없는 회차·소제목을 파일에 지어내 적었습니다"
            assert entry["scrap_date"] == day, "원장 날짜는 적어야 한다(그날 관측한 사실)"
    tag = _label_source_tag_html({"kind": "agency"})
    assert "기관 기사" in tag and "회차" not in tag, "원장 라벨에 「회차」라고 적고 있습니다"
    # 정기·수시 태그는 그대로 — 새 종류가 옛 두 종류를 건드리면 안 된다.
    assert "회차" in _label_source_tag_html({"kind": "regular"})
    assert "청문회" in _label_source_tag_html({"kind": "adhoc", "issue": "청문회"})

    print("52) 기관 기사 찾기 라벨: 통과 (공용 장치 기본값 그대로, 요약 없는 행도, 출처 agency라 회차·스크랩 안 적음)")


def test_live_labels():
    """오늘 전체 기사에서 라벨을 **붙인다** — 그리고 그게 라벨 보관함에 거짓으로 안 적힌다.

    이 화면에만 있는 기사가 있다: 「오늘 전체 기사」만 켠 그룹의 기사는 어느 회차에도
    안 들어가 확정본·초안·정기 보관함 어디에도 안 나온다 — 라벨을 붙일 **다른 길이
    아예 없다**(기관 기사 찾기와 같은 이유, 같은 처방).

    이 테스트가 지키는 것 넷:
      (1) **공용 라벨 장치의 기본값 경로가 그대로다.** 확정본·초안·정기 보관함·수시는
          기사 줄이 `.article`이고 칩을 `<summary>` 밑에 단다. 이 화면을 열려고 장치에
          칩 줄 자리(`data-lab-row-host`)를 더했으므로, 기본값이 바뀌면 **이미 돌고 있는
          화면 넷이 조용히 망가진다**(라벨은 저장됐는데 화면이 안 바뀐다).
      (2) 이 화면은 `data-lab-root`(줄이 `.article`이 아니다)와 `data-lab-row-host`
          (🏷가 `<summary>` 밖이라 칩 줄을 넣을 칸을 가리킨다)로 자기 생김새를 알려준다.
          **칩 줄은 기사 글 묶음 안(URL 줄 다음)**이다 — 행 오른쪽 버튼 묶음에 들어가면
          칩이 버튼 옆으로 선다.
      (3) 출처가 `live`라 라벨 보관함이 `회차`·`✓ 스크랩됨`을 안 그린다. 이 화면 기사의
          절반은 아직 어느 회차에도 안 실렸는데 그렇게 적으면 **화면이 아니라 파일에
          거짓이 남고** 엑셀 「스크랩일자·스크랩종료시간·소제목」까지 따라 나간다.
      (4) **라벨 팝오버가 상태색 행에 깔리지 않는다.** 이 화면은 스크랩됨·담아둠·자동
          선별 행에 `opacity`를 주는데, 그게 쌓임 맥락을 만들어 그 안에 뜨는 것은
          `z-index`를 올려도 행 밖으로 못 나간다(⋯ 메뉴가 이미 겪은 사고).
    """
    import json
    import tempfile
    from pathlib import Path
    from unittest.mock import patch

    from app.labels import attach_label, labels_for_url, snapshot_source
    from app.renderer import label_popover_script
    from app.settings_server import _label_source_tag_html
    from app import labels as labels_mod, live_renderer

    # (1) 공용 장치의 기본값 경로 — 확정본 넷이 쓰는 길이 그대로 남아 있어야 한다.
    js = label_popover_script("127.0.0.1", 8765)
    assert 'el.closest(".article")' in js, \
        "공용 라벨 장치의 기본값(.article)이 사라졌습니다 — 확정본·초안·정기 보관함·수시가 멈춥니다"
    assert 'btn.closest("summary")' in js, \
        "공용 라벨 장치의 기본 칩 자리(<summary> 밑 .lab-row)가 사라졌습니다"
    assert 'getAttribute("data-lab-row-host")' in js, \
        "칩 줄 자리를 화면이 알려주는 길이 없습니다 — 🏷가 <summary> 밖인 화면이 멈춥니다"

    article = {
        "outlet": "연합뉴스", "title": "가산세 3만건 넘어",
        "url": "https://n.news.naver.com/mnews/article/003/1",
        "summary": "요약", "pub_date": "2026-10-13T14:05:00+09:00",
    }
    # (2) 생김새를 알려주고, 칩 줄은 기사 글 묶음 안(URL 줄 다음)에 선다.
    row = live_renderer._render_row(
        article, True, [], set(), set(), set(), set(),
        labels={article["url"]: {"labels": ["세제", "국정감사"]}}, known_labels_html="",
    )
    assert "data-lab-root" in row and 'data-lab-row-host=".live-row-text"' in row, \
        "라벨 장치에 이 화면의 생김새를 안 알려줬습니다 — 🏷가 먹통이 됩니다"
    assert 'data-lab-source="live"' in row, "🏷에 출처 표시가 빠졌습니다"
    assert 'data-scrap-date=""' in row and 'data-scrap-end=""' in row and 'data-group=""' in row, \
        "없는 회차·소제목을 지어내고 있습니다"
    assert row.index("live-url") < row.index('<div class="lab-row"') < row.index("live-row-actions"), \
        "라벨 칩 줄이 기사 글 묶음 밖에 있습니다 — 행 오른쪽 버튼 옆으로 섭니다"
    assert row.index("add-btn") < row.index("lab-btn") < row.index("more-btn"), \
        "🏷 자리가 「상태 버튼 · 🏷 · ⋯ · 🗑」가 아닙니다"
    # 라벨이 0건이면 칩 줄 자체를 안 그린다(확정본과 같은 규칙).
    row0 = live_renderer._render_row(
        article, True, [], set(), set(), set(), set(), labels={}, known_labels_html="",
    )
    assert '<div class="lab-row"' not in row0 and "lab-count" not in row0, \
        "라벨이 없는데 빈 칩 줄·건수를 그렸습니다"
    assert "lab-btn" in row0, "라벨이 없으면 🏷까지 사라집니다"

    # (4) 상태색 행에 뜨는 것이 깔리지 않는다 — ⋯ 메뉴와 같은 처방을 함께 받는다.
    assert ".lab-pop:not(.hidden)" in live_renderer.render_live_error_page(), \
        "상태색 행에서 라벨 팝오버가 반투명해지고 아랫줄에 깔립니다(⋯ 메뉴와 같은 사고)"

    # (3) 출처 — 파일에 `live`로 남고, 라벨 보관함이 회차·스크랩 상태를 안 그린다.
    assert snapshot_source("live") == {"kind": "live"}
    with tempfile.TemporaryDirectory() as tmp:
        with patch.object(labels_mod, "LABELS_FILE", Path(tmp) / "labels.json"):
            attach_label(
                {"url": article["url"], "outlet": "연합뉴스", "title": "가산세 3만건 넘어",
                 "pub_date": article["pub_date"], "scrap_date": "", "scrap_end": ""},
                "세제", snapshot_source("live"), group="",
            )
            assert labels_for_url(article["url"]) == ["세제"]
            entry = json.loads((Path(tmp) / "labels.json").read_text(encoding="utf-8"))[article["url"]]
            assert entry["source"] == {"kind": "live"}
            assert entry["scrap_date"] == "" and entry["scrap_end"] == "" and entry["group"] == "", \
                "없는 회차·소제목을 파일에 지어내 적었습니다"
    tag = _label_source_tag_html({"kind": "live"})
    assert "오늘 전체 기사" in tag and "회차" not in tag, "이 화면의 라벨에 「회차」라고 적고 있습니다"
    # 옛 종류 셋은 그대로 — 새 종류가 그것들을 건드리면 안 된다.
    assert "회차" in _label_source_tag_html({"kind": "regular"})
    assert "기관 기사" in _label_source_tag_html({"kind": "agency"})
    assert "청문회" in _label_source_tag_html({"kind": "adhoc", "issue": "청문회"})

    print("60) 오늘 전체 기사 라벨: 통과 (공용 장치 기본값 그대로, 칩 줄은 글 묶음 안, 출처 live라 회차·스크랩 안 적음)")



def test_custom_group_with_taken_name_gets_its_own_box():
    """담당자가 친 이름이 그 회차에 이미 쓰이고 있어도 **새 칸이 따로 생긴다**.

    소제목을 가르는 값이 이름 문자열 하나뿐이라, 예전엔 「기타」를 만들면 AI가 분류
    못 한 기사를 담아 둔 「기타」 칸과 통째로 합쳐졌다(2026-10-02 제보 — 그 칸은 이름표가
    붙어 「재경부 1차관(후속)」으로 보이던 참이라 화면 기준 중복 검사에도 안 걸렸다).

    판정은 예나 지금이나 **지금 화면에 그 이름으로 보이는 칸이 있나** 하나뿐이다. 바뀐 건
    "안 겹친다고 통과시켜 놓고 속으로는 합쳐버리던" 쪽이다.
    """
    from app.labels import LABELS_FILE  # noqa: F401  (러너가 이미 격리해 둔다)

    tmp = Path(tempfile.mkdtemp())
    rid = ("2026-10-02", "09:30")
    with patch.object(custom_groups, "CUSTOM_GROUPS_FILE", tmp / "custom_groups.json"), \
         patch.object(curation, "GROUP_LABELS_FILE", tmp / "group_labels.json"):
        taken = {"기타", "청와대 인사"}
        assert custom_groups.unique_key("세제 개편", taken) == "세제 개편", "안 겹치는데 이름을 바꿨다"
        key = custom_groups.unique_key("기타", taken)
        assert key == "기타 (2)", key

        assert custom_groups.add_custom_group("기타", key=key) == key
        assert custom_groups.load_custom_groups() == [key], "속이름으로 저장되지 않았다"

        # 화면·복사·보관함이 모두 거치는 한 곳(display_group_name)에서 친 이름이 보인다.
        labels = curation.load_group_labels(rid)
        assert curation.display_group_name(key, labels) == "기타", labels
        assert curation.display_group_name("기타", labels) == "기타", "AI 기타 칸까지 건드렸다"

        # 이제 화면에 「기타」가 보이므로 또 만들려 하면 그때는 막힌다.
        assert curation.display_name_in_use("기타", {key}, round_id=rid), "같은 이름이 두 칸 허용됨"

        # 엑셀도 화면과 같은 이름 — 속이름이 데이터로 새면 담당자가 만든 적 없는 이름이 남는다.
        rows = renderer.build_export_rows(
            [{"name": key, "articles": [{"url": "u1", "title": "t", "outlet": "o"}]}],
            "2026-10-02", "09:30", labels,
        )
        assert rows[0]["소제목"] == "기타", rows[0]["소제목"]

        # ✏️로 고친 이름이 이기고, 그 매핑이 회차 이름표 파일로 복사되지는 않는다.
        curation.set_group_label(key, "현안 모음", round_id=rid)
        assert curation.display_group_name(key, curation.load_group_labels(rid)) == "현안 모음"
        saved = json.loads((tmp / "group_labels.json").read_text(encoding="utf-8"))
        assert saved[f"{rid[0]}|{rid[1]}"] == {key: "현안 모음"}, saved

        # 세 번째 「기타」도 자리를 따로 잡는다.
        assert custom_groups.unique_key("기타", taken) == "기타 (3)", "이미 쓴 속이름을 또 내줬다"

        # 소제목을 지워도 **화면 이름 매핑은 남는다** — 그 이름으로 저장된 기사가 아직
        # 있어서, 매핑이 먼저 죽으면 되살아난 칸이 속이름을 드러낸다(아래 테스트).
        custom_groups.remove_custom_group(key)
        assert custom_groups.load_custom_groups() == [], "지운 소제목이 목록에 남았다"
        assert custom_groups.load_custom_group_labels().get(key) == "기타", "지우면서 화면 이름까지 지웠다"


def test_custom_group_alias_outlives_the_day_and_the_delete():
    """직접 만든 소제목의 **속이름은 기사보다 오래 사는 매핑으로만 가려진다.**

    소제목 이름 목록(names)은 하루살이고 🗑로 지우면 그 자리에서 사라지는데, 그 이름으로
    저장된 기사는 배정 기록 30일·회차 파일 1년을 산다. 예전엔 매핑(labels)도 names와 같이
    죽어서, 칸은 기사를 안고 되살아나는데 가려 주던 이름만 사라졌다 — 보관함·복사·txt·
    엑셀에 「우리부 관련 (2)」가 그대로 나갔고(2026-10-19 제보), 이름표가 없어지니
    🤖 AI 배지를 막던 조건(display_name == original_name)까지 풀려 담당자가 만든 소제목에
    AI 배지가 붙었다.

    같이 지킨다: 「+ 새 소제목」이 피해 갈 이름은 **오늘 배정한 것만** 센다 — 어제 쓰고
    끝난 이름까지 피하면 속이름이 쓸데없이 많이 지어지고, 속이름이 많을수록 위 사고의
    표면이 넓어진다.
    """
    tmp = Path(tempfile.mkdtemp())
    with patch.object(custom_groups, "CUSTOM_GROUPS_FILE", tmp / "custom_groups.json"), \
         patch.object(curation, "GROUP_OVERRIDES_FILE", tmp / "group_overrides.json"), \
         patch.object(curation, "GROUP_LABELS_FILE", tmp / "group_labels.json"):
        rid = ("2026-10-19", "09:30")
        key = custom_groups.unique_key("우리부 관련", {"우리부 관련"})
        assert key == "우리부 관련 (2)", key
        custom_groups.add_custom_group("우리부 관련", key=key)

        # (1) 자정이 지나도 — 어제 회차를 보관함에서 열었을 때 — 친 이름이 보인다.
        tomorrow = datetime(2026, 10, 20, 9, 0)
        assert custom_groups.load_custom_groups(tomorrow) == [], "소제목 이름까지 하루를 넘겨 살았다"
        labels = curation.load_group_labels(rid)
        assert curation.display_group_name(key, labels) == "우리부 관련", labels

        # (2) 🗑로 지운 뒤에도 마찬가지다.
        custom_groups.remove_custom_group(key)
        assert curation.display_group_name(key, curation.load_group_labels(rid)) == "우리부 관련"

        # (3) 어제 배정만 남은 이름은 오늘 칸을 되살릴 수 없으니 번호를 만들지 않는다.
        curation.set_group_override("u-old", "세제 개편", now=datetime(2026, 10, 15, 9, 0))
        curation.set_group_override("u-today", "국정감사", now=datetime(2026, 10, 19, 9, 0))
        today_names = curation.group_override_names(now=datetime(2026, 10, 19, 10, 0))
        assert today_names == {"국정감사"}, today_names
        assert custom_groups.unique_key("세제 개편", today_names) == "세제 개편", "어제 이름을 피해 다녔다"
        assert custom_groups.unique_key("국정감사", today_names) == "국정감사 (2)"

    print("✓ 직접 만든 소제목 속이름: 자정·삭제를 넘겨 화면 이름이 남고, 오늘 배정만 피해 간다")


def test_etc_order_follows_the_user():
    """「기타」를 맨 뒤로 미는 건 **AI가 막 분류한 상태**까지다 — 담당자가 ▲▼로 옮기면 그 자리.

    예전엔 화면 이름이 「기타」면 무조건 맨 뒤로 눌러 두고 ▲▼도 안 그렸다. 그래서 담당자가
    직접 만든 「기타」(속이름 `기타 (2)`)까지 같이 잠겨, 한 화면에 선 두 칸 중 어느 것도 못
    옮겼다(2026-10-21 제보). 이제 ▲▼가 없는 칸은 📂 미분류 하나다.

    「담당자가 옮겼나」는 **그 회차 저장 순서에 그 이름이 있나** 하나로 읽는다 — 화면이
    보내는 목록이 곧 그것이라 판정과 저장이 같은 값을 본다.
    """
    from app import group_order
    from app.group_order import apply_group_order, is_order_locked, save_group_order
    from app.llm_classifier import UNCLASSIFIED_GROUP_NAME

    tmp = Path(tempfile.mkdtemp())
    rid = ("2026-10-21", "14:00")

    def names(groups):
        return [g["name"] for g in apply_group_order(groups, rid)]

    with patch.object(custom_groups, "CUSTOM_GROUPS_FILE", tmp / "custom_groups.json"), \
         patch.object(curation, "GROUP_LABELS_FILE", tmp / "group_labels.json"), \
         patch.object(group_order, "GROUP_ORDER_FILE", tmp / "group_order.json"):
        key = custom_groups.unique_key("기타", {"기타"})
        custom_groups.add_custom_group("기타", key=key)          # 화면엔 「기타」로 보이는 둘째 칸
        groups = [
            {"name": "기타", "articles": []},
            {"name": "우리부 관련", "articles": []},
            {"name": key, "articles": []},
            {"name": UNCLASSIFIED_GROUP_NAME, "articles": []},
        ]

        # 1) 저장된 순서가 없으면 예전 그대로 — 두 「기타」가 뒤로, 미분류는 그 뒤.
        assert names(groups) == ["우리부 관련", "기타", key, UNCLASSIFIED_GROUP_NAME], names(groups)

        # 2) ▲▼를 그릴지 묻는 자리는 📂 미분류만 막는다.
        labels = curation.load_group_labels(rid)
        assert not is_order_locked("기타", labels), "AI 기타의 ▲▼가 안 그려진다"
        assert not is_order_locked(key, labels), "직접 만든 「기타」의 ▲▼가 안 그려진다"
        assert is_order_locked(UNCLASSIFIED_GROUP_NAME, labels), "미분류에 ▲▼가 생겼다"

        # 3) 담당자가 옮긴 자리는 그대로 지킨다(화면이 보내는 목록 = 미분류만 뺀 지금 순서).
        save_group_order([key, "기타", "우리부 관련"], rid)
        assert names(groups) == [key, "기타", "우리부 관련", UNCLASSIFIED_GROUP_NAME], names(groups)

        # 4) 미분류는 저장 순서에 섞여 들어와도 늘 맨 뒤다(다음 회차의 다른 기사가 끼어든다).
        save_group_order([UNCLASSIFIED_GROUP_NAME, "기타", "우리부 관련"], rid)
        assert names(groups)[-1] == UNCLASSIFIED_GROUP_NAME, names(groups)

        # 5) 저장 순서에 없는 새 소제목은 자연 순서 그대로 뒤에 붙고, 「기타」는 제자리를 지킨다.
        save_group_order(["기타", "우리부 관련"], rid)
        grown = groups[:2] + [{"name": "국정감사", "articles": []}] + groups[2:]
        assert names(grown) == ["기타", "우리부 관련", "국정감사", key, UNCLASSIFIED_GROUP_NAME], names(grown)

    print("✓ 「기타」 순서: AI가 분류한 상태만 맨 뒤, 담당자가 옮긴 자리는 그대로")


def test_home_issue_card_uses_display_names():
    """홈 「오늘의 주요 주제」는 화면에 보이는 이름으로 묶고 「기타」를 가린다.

    저장된 원래 이름으로 보면 두 가지가 한꺼번에 샜다(2026-10-12 제보):

    1. 담당자가 직접 만든 「기타」는 속이름이 「기타 (5)」라(같은 회차에 이미 AI의
       「기타」가 있어 app.custom_groups.unique_key가 지은 것) `name == "기타"`
       정확일치를 빠져나가, 빼기로 한 짬뽕 칸이 순위에 올라오고 화면엔 담당자가
       지은 적 없는 「(5)」가 그대로 찍혔다.
    2. 회차마다 소제목을 새로 분류하므로 같은 사안이 회차마다 다른 AI 이름을 받는데,
       그걸 잇는 근거가 바로 담당자가 붙인 이름표다. 원래 이름만 보면 묶을 수가 없어
       한 사안이 두 줄로 갈라진다.

    판정 기준(이름표 적용 후)은 app.group_order._lock_rank와 같다.
    """
    tmp = Path(tempfile.mkdtemp())
    with patch.object(curation, "GROUP_LABELS_FILE", tmp / "group_labels.json"), \
         patch.object(curation, "HIDDEN_ARTICLES_FILE", tmp / "hidden.json"):
        etc_key = custom_groups.unique_key("기타", {"기타"})
        assert etc_key == "기타 (2)", etc_key
        custom_groups.add_custom_group("기타", key=etc_key)

        # 같은 사안인데 회차마다 AI가 다른 이름을 지었고(토큰이 안 겹쳐 원래 이름으로는
        # 절대 안 묶인다), 담당자가 이름표로 이어 뒀다.
        curation.set_group_label("대외경제장관회의", "대외경제장관회의 - 대미통상",
                                 round_id=("2026-10-12", "09:30"))
        curation.set_group_label("공급망 안보 품목 확대", "대외경제장관회의 - 대미통상(지속)",
                                 round_id=("2026-10-12", "11:00"))

        def _run(slot, pairs):
            return {
                "run_at": f"2026-10-12T{slot.replace(':', '')}00",
                "run_slot": slot,
                "articles": [
                    {"url": f"u-{slot}-{i}", "title": f"기사 {slot} {i}", "outlet": "A", "group": g}
                    for i, g in enumerate(pairs)
                ],
            }

        runs = [
            _run("09:30", ["대외경제장관회의", "대외경제장관회의", etc_key, etc_key, etc_key, "기타"]),
            _run("11:00", ["공급망 안보 품목 확대", "공급망 안보 품목 확대"]),
        ]

        articles = landing_renderer._report_articles(runs)
        assert not any("display_group" in a for r in runs for a in r["articles"]), \
            "저장된 기사 dict에 화면 전용 키가 새어 들어갔다"

        streams = landing_renderer._issue_streams(articles)
        names = [s["name"] for s in streams]
        assert all("기타" not in n for n in names), f"짬뽕 칸이 순위에 올라왔다: {names}"
        assert names == ["대외경제장관회의 - 대미통상"], names
        assert len(streams[0]["articles"]) == 4, "회차를 잇는 이름표를 못 보고 갈라졌다"


def test_login_lock():
    """로그인 자물쇠 — 켜기 전엔 아무것도 안 바뀌고, 켜면 막히고, 찾기는 둘로 푼다.

    이 단계는 **자물쇠까지**다(MULTIUSER_ROLLOUT_CHECKLIST.md 2단계 중 1·2번 사이).
    데이터를 사람별로 가르는 일은 저장 한 겹을 넣은 다음이라, 여기서 지키는 것은
    「켜기 전엔 예전과 똑같다 · 켜면 못 들어온다 · 본인 확인이 핸드폰+질문 둘이다」 셋이다.
    """
    from app import auth_session, users

    tmp = Path(tempfile.mkdtemp())
    with patch.object(users, "USERS_FILE", tmp / "users.json"), \
         patch.object(auth_session, "AUTH_SESSIONS_FILE", tmp / "sessions.json"), \
         patch.object(auth_session, "AUTH_ATTEMPTS_FILE", tmp / "attempts.json"), \
         patch.object(auth_session, "AUTH_CONFIG_FILE", tmp / "auth_config.json"):

        # 기본은 꺼짐 — 켜는 건 담당자가 하고, 꺼져 있는 동안 앱은 예전 그대로다.
        assert auth_session.is_login_required() is False, "자물쇠가 저절로 켜져 있다"

        # 비밀번호도 「질문의 답」도 평문으로 남지 않는다 — 둘 다 같은 물건이다.
        # 답에 띄어쓰기는 **적을 때** 막는다 — 「서울 초등」과 「서울초등」 중 무엇을
        # 적었는지 담당자가 기억해야 하는 상황을 아예 만들지 않는다.
        try:
            users.create_user("a@korea.kr", "sijang2026", "홍길동", "01012345678",
                              config.SECURITY_QUESTIONS[0], "서울 초등", consent=True)
            raise AssertionError("띄어쓰기가 든 답이 통과했다")
        except users.UserError as exc:
            assert "띄어쓰기" in str(exc), exc

        # 개인정보 수집·이용에 동의하지 않으면 계정이 만들어지지 않는다 — 화면의
        # 체크가 아니라 여기가 막는다(폼은 누구든 직접 보낼 수 있다).
        try:
            users.create_user("a@korea.kr", "sijang2026", "홍길동", "01012345678",
                              config.SECURITY_QUESTIONS[0], "서울초등")
            raise AssertionError("동의 없이 계정이 만들어졌다")
        except users.UserError as exc:
            assert str(exc) == config.PRIVACY_CONSENT_REQUIRED, exc
        assert users.user_count() == 0, "거부했는데 계정이 남았다"

        user = users.create_user("a@korea.kr", "sijang2026", "홍길동", "010-1234-5678",
                                 config.SECURITY_QUESTIONS[0], " 서울초등 ",
                                 department="디지털소통팀", consent=True)
        # 동의한 사실은 시각으로 남는다 — 아무 데도 안 남으면 받을 이유가 없다.
        assert user["consent_at"], user
        saved = (tmp / "users.json").read_text(encoding="utf-8")
        assert "sijang2026" not in saved and "서울" not in saved, "평문이 파일에 남았다"
        assert user["department"] == "디지털소통팀", user
        assert user["role"] == "admin", "첫 계정이 관리자가 아니다"
        assert users.create_user("b@korea.kr", "sijang2026", "김담당", "01099998888",
                                 config.SECURITY_QUESTIONS[1], "부산",
                                 consent=True)["role"] == "member"
        assert users.get_user(user["id"])["department"] == "디지털소통팀"

        # 번호는 숫자만 저장해 대조한다 — 하이픈 때문에 본인이 막히면 안 된다.
        assert user["phone"] == "01012345678", user["phone"]
        assert users.verify_recovery("a@korea.kr", "010 1234 5678"), "하이픈 모양 때문에 안 맞았다"
        assert users.verify_recovery("A@Korea.kr ", "01012345678"), "이메일 대소문자로 갈렸다"
        assert not users.verify_recovery("a@korea.kr", "01000000000"), "번호가 틀렸는데 통과"

        # **맞힐 때는 너그럽다** — 저장된 답엔 공백이 없으니, 치다가 공백이 끼어도
        # 본인이 잠길 이유가 없다(쓰는 쪽에 엄하고 읽는 쪽에 너그럽다).
        assert users.verify_answer(user, "서울초등") and users.verify_answer(user, " 서 울 초등 ")
        assert not users.verify_answer(user, "부산초등")

        # 로그인 횟수 잠금 — 질문 기반은 잠금이 없으면 자물쇠가 아니다.
        key = "login:a@korea.kr"
        for left in range(config.AUTH_MAX_LOGIN_FAILS - 1, -1, -1):
            assert auth_session.record_fail(key, config.AUTH_MAX_LOGIN_FAILS, config.AUTH_LOGIN_LOCK_MIN) == left
        assert auth_session.lock_remaining_sec(key) > 0, "다 틀렸는데 안 잠겼다"
        auth_session.clear_fails(key)
        assert auth_session.lock_remaining_sec(key) == 0

        # 세션은 쿠키에만 원본이 있고 파일엔 해시만 남는다.
        token, max_age = auth_session.create_session(user["id"], keep=False)
        assert token not in (tmp / "sessions.json").read_text(encoding="utf-8"), "토큰 원본이 파일에 남았다"
        assert auth_session.user_id_for_token(token) == user["id"]
        assert auth_session.user_id_for_token("아무거나") is None
        assert max_age == config.AUTH_SESSION_HOURS * 3600
        assert auth_session.create_session(user["id"], keep=True)[1] == config.AUTH_SESSION_DAYS_KEEP * 86400

        # 비밀번호를 바꾸면 그 사람의 로그인이 전부 끊긴다.
        users.set_password(user["id"], "newpass2026")
        auth_session.destroy_user_sessions(user["id"])
        assert auth_session.user_id_for_token(token) is None, "옛 비밀번호로 연 창이 살아 있다"
        assert users.verify_login("a@korea.kr", "newpass2026")
        assert not users.verify_login("a@korea.kr", "sijang2026")

        # 찾기 표는 단계를 들고 있고 한 번 쓰면 끝난다 — 1단계를 건너뛸 수 없다.
        ticket = auth_session.issue_ticket(user["id"], "answer")
        assert auth_session.ticket_user_id(ticket, "reset") is None, "단계를 건너뛸 수 있다"
        assert auth_session.ticket_user_id(ticket, "answer") == user["id"]
        auth_session.drop_ticket(ticket)
        assert auth_session.ticket_user_id(ticket, "answer") is None

        # 답은 비워 두면 그대로고, 적으면 바뀐다. 부서는 선택이라 비우면 비워진다.
        users.update_profile(user["id"], display_name="김담당", department="", answer="")
        assert users.verify_answer(users.get_user(user["id"]), "서울초등"), "빈 칸이 답을 지웠다"
        assert users.get_user(user["id"])["display_name"] == "김담당"
        assert users.get_user(user["id"])["department"] == "", "선택 칸을 비울 수 없다"

        # **질문만 바꾸고 답을 비워 두는 것은 막는다** — 그대로 두면 새 질문에 옛 답이
        # 짝지어져, 담당자가 자기 질문에 자기 답으로는 영영 못 들어간다.
        try:
            users.update_profile(user["id"], question=config.SECURITY_QUESTIONS[2], answer="")
            raise AssertionError("질문만 바꿔 놓고 통과했다")
        except users.UserError as exc:
            assert "새 답도 함께" in str(exc), exc
        assert users.get_user(user["id"])["security_question"] == config.SECURITY_QUESTIONS[0]

        users.update_profile(user["id"], question=config.SECURITY_QUESTIONS[2], answer="대전중앙")
        assert users.verify_answer(users.get_user(user["id"]), "대전중앙")
        assert users.get_user(user["id"])["security_question"] == config.SECURITY_QUESTIONS[2]

        # **자물쇠가 꺼져 있을 때만** 로그인 전 화면에 홈으로 가는 길이 있다.
        # 꺼져 있으면 로그인 없이도 앱을 다 쓰므로 가입 화면이 막다른 골목이 되면
        # 안 되고, 켜져 있으면 갈 수 있는 곳이 정말로 없다(그게 자물쇠다).
        from app import auth_renderer
        assert "홈으로" in auth_renderer.render_signup_page(), "꺼졌는데 돌아갈 길이 없다"
        assert "홈으로" in auth_renderer.render_login_page()

        auth_session.set_login_required(True)
        assert auth_session.is_login_required() is True
        assert "홈으로" not in auth_renderer.render_signup_page(), "자물쇠를 켰는데 홈으로 가는 길이 남았다"
        assert "홈으로" not in auth_renderer.render_forgot_page()

    print("51) 로그인 자물쇠: 통과 (기본 꺼짐, 평문 안 남김, 답 띄어쓰기 금지·질문만 바꾸기 금지, 부서, 횟수 잠금, 세션 해시·끊기, 단계 표, 꺼졌을 때만 홈으로)")


def test_key_paths_stay_narrow():
    """키를 쓰는 자리가 서비스마다 한 곳인지 지킨다 — 서버로 옮길 때의 이음매.

    나중에 여러 사람이 쓰게 되면 키는 담당자 컴퓨터가 아니라 서버에 둬야 한다
    (MULTIUSER_ROLLOUT_CHECKLIST.md §2). 그 작업이 기계적으로 끝나려면 「키를 읽는
    곳」과 「그 키로 바깥을 부르는 곳」이 서비스마다 하나여야 한다. 지금은 그렇고,
    이 테스트는 그 상태가 앞으로도 유지되는지만 본다 — 새 자리가 생기면 여기서 깨져
    "이것도 같이 옮겨야 한다"가 그 자리에서 드러난다.

    규칙을 늘릴 일이 생기면 허용 파일을 추가하지 말고 **왜 새 자리가 필요한지** 먼저
    따진다. 대개는 기존 함수를 부르면 되는 경우다.
    """
    _require_isolation()
    app_dir = Path(__file__).resolve().parent.parent
    sources = sorted(app_dir.glob("app/**/*.py")) + [app_dir / "main.py"]

    rules = [
        ("Claude 클라이언트를 만드는 곳", "anthropic.Anthropic(", {"app/llm_classifier.py"}),
        ("네이버 검색 API 주소", "openapi.naver.com", {"app/naver_api.py"}),
        # 끝의 "/bot{"까지 봐야 실제 호출만 잡힌다 — 설정 화면에는 담당자에게 chat id
        # 확인법을 알려주는 같은 주소가 글로 적혀 있다(부르는 자리가 아니다).
        ("텔레그램 API 호출", "api.telegram.org/bot{", {"app/telegram_bot.py", "app/telegram_bot_name.py"}),
        ("메일 서버 접속", "smtplib.SMTP(", {"app/email_sender.py"}),
        # .env를 읽는 곳 — 키 이름은 app.config 한 곳에서만 os.getenv로 읽는다.
        ("NAVER_CLIENT_ID를 .env에서 읽는 곳", 'os.getenv("NAVER_CLIENT_ID"', {"app/config.py"}),
        ("ANTHROPIC_API_KEY를 .env에서 읽는 곳", 'os.getenv("ANTHROPIC_API_KEY"', {"app/config.py"}),
        ("TELEGRAM_BOT_TOKEN을 .env에서 읽는 곳", 'os.getenv("TELEGRAM_BOT_TOKEN"', {"app/config.py"}),
        ("EMAIL_SENDER_PASSWORD를 .env에서 읽는 곳", 'os.getenv("EMAIL_SENDER_PASSWORD"', {"app/config.py"}),
        # .env 파일 자체를 건드리는 곳 — 옮겨심기 한 곳뿐이다.
        (".env 파일을 고치는 곳", "ENV_FILE", {"app/credentials.py"}),
    ]

    for label, needle, allowed in rules:
        found = {
            str(path.relative_to(app_dir))
            for path in sources
            if needle in path.read_text(encoding="utf-8")
        }
        extra = found - allowed
        assert not extra, (
            f"{label}가 늘었습니다 — {sorted(extra)}. 키를 서버로 옮길 때 같이 고쳐야 할 "
            f"자리가 하나 더 생겼다는 뜻입니다. 기존 함수를 부르는 길이 없는지 먼저 보세요 "
            f"(지금 자리: {sorted(allowed)})"
        )
        assert found, f"{label}를 한 곳도 못 찾았습니다 — 규칙이 옛 이름을 보고 있습니다({needle})"

    # .env 폴백 상수(app.config의 NAVER_CLIENT_SECRET 등)를 **실제로 가져다 쓰는** 모듈은
    # app.credentials 하나다 — 거기가 「설정값 > .env」 우선순위를 정하는 자리이고, 서버로
    # 옮길 때 고칠 자리도 거기다. 글자 대조가 아니라 import 구문을 본다(설정 화면 도움말에
    # 같은 이름이 글로 적혀 있어 문자열로는 못 가른다).
    env_constants = {
        "NAVER_CLIENT_ID", "NAVER_CLIENT_SECRET", "ANTHROPIC_API_KEY",
        "TELEGRAM_BOT_TOKEN", "EMAIL_SENDER_ADDRESS", "EMAIL_SENDER_PASSWORD",
        "EMAIL_SMTP_HOST", "EMAIL_SMTP_PORT",
    }
    importers = set()
    for path in sources:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "app.config":
                if any(alias.name in env_constants for alias in node.names):
                    importers.add(str(path.relative_to(app_dir)))
    assert importers == {"app/credentials.py"}, (
        f".env 폴백 상수를 가져다 쓰는 모듈이 늘었습니다 — {sorted(importers)}. "
        "키를 읽는 자리는 app/credentials.py 하나여야 합니다(그 모듈의 함수를 부르세요)."
    )

    print("48) 키 이음매: 통과 (네이버·Claude·텔레그램·이메일 각 한 곳, .env를 읽고 고치는 곳도 한 곳)")



def test_screen_js_strings_are_not_cut_by_newlines():
    """화면 JS의 문자열이 줄바꿈으로 끊기지 않는지 지킨다 — 한 줄이 화면을 통째로 먹통으로 만든다.

    렌더러가 내는 JS는 화면마다 **블록 하나**다. 그래서 문자열 리터럴 하나가 줄바꿈으로
    끊기면 그 블록 전체가 파싱에 실패하고, 그 화면의 버튼이 **전부** 죽는다(숨기기·↑↓·
    소제목·재분류·확인창까지). 사고 당시 초안 화면이 그랬다 — 되돌리기 확인창 문구를
    파이썬 템플릿에 `\\n`으로 적어(`\\\\n`이어야 한다) 파이썬이 진짜 줄바꿈을 박아 넣었다.

    **서버 로그에 아무것도 남지 않는 종류라 이 테스트가 유일한 그물이다.** 수집·발송은
    멀쩡히 돌고 화면도 열리므로, 담당자가 "클릭이 안 돼"라고 말해 주기 전까지 아무도
    모른다. 바로 위 줄의 같은 문구들은 `\\\\n`으로 멀쩡했고 이 함수만 한 겹이 빠져 있었다.

    JS를 따로 파싱하지 않고(린터·node를 쓰지 않는 저장소다) 문자열 리터럴만 훑는다 —
    `'…'`와 `"…"` 안에서 줄바꿈을 만나면 그 자리를 집는다. 주석·정규식 리터럴·백틱
    템플릿은 건너뛴다(`replace(/"/g, …)`의 따옴표를 문자열 시작으로 읽으면 안 된다).
    """
    _require_isolation()
    root = Path(__file__).resolve().parent.parent
    # 화면 JS로 나갈 글만 고른다 — CSS·HTML 조각에는 이 규칙이 없다.
    js_hints = ("function ", "addEventListener", "document.querySelector",
                "document.getElementById", "=>")
    word = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_$")

    def cut_literals(js):
        """줄바꿈으로 끊긴 문자열 리터럴이 시작된 줄 번호들."""
        bad, i, line, n, prev = [], 0, 1, len(js), ""
        while i < n:
            c = js[i]
            if c == "\n":
                line += 1
                i += 1
            elif c in " \t\r":
                i += 1
            elif c == "/" and i + 1 < n and js[i + 1] == "/":        # 한 줄 주석
                while i < n and js[i] != "\n":
                    i += 1
            elif c == "/" and i + 1 < n and js[i + 1] == "*":        # 여러 줄 주석
                i += 2
                while i + 1 < n and not (js[i] == "*" and js[i + 1] == "/"):
                    line += js[i] == "\n"
                    i += 1
                i += 2
            elif c == "/" and prev not in word and prev not in ")]":  # 정규식 리터럴
                i += 1
                while i < n and js[i] not in "/\n":
                    i += 2 if js[i] == "\\" else 1
                if i < n and js[i] == "/":   # 줄바꿈은 세지 않고 겉 루프에 맡긴다
                    i += 1
                prev = "/"
            elif c == "`":                                           # 백틱은 줄바꿈 허용
                i += 1
                while i < n and js[i] != "`":
                    if js[i] == "\\":
                        i += 1
                    else:
                        line += js[i] == "\n"
                    i += 1
                i += 1
                prev = "`"
            elif c in "\"'":
                quote, start, i = c, line, i + 1
                while i < n:
                    if js[i] == "\\":
                        i += 2
                    elif js[i] == quote:
                        i += 1
                        break
                    elif js[i] == "\n":
                        bad.append(start)
                        line += 1
                        i += 1
                        break
                    else:
                        i += 1
                prev = quote
            else:
                prev = c
                i += 1
        return bad

    def screen_js(tree):
        """f-string은 통째로 이어 붙여 본다 — 조각만 보면 리터럴이 반쪽만 들어와
        멀쩡한 코드가 걸린다(`f"… '{icon('refresh')}불러오는 중…';"`)."""
        in_fstring = {
            id(part)
            for node in ast.walk(tree) if isinstance(node, ast.JoinedStr)
            for part in ast.walk(node) if isinstance(part, ast.Constant)
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.JoinedStr):
                # {…} 자리는 값 하나로 바꿔 둔다 — 무엇이 들어오든 리터럴을 열거나 닫지 않는다.
                yield node.lineno, "".join(
                    p.value if isinstance(p, ast.Constant) and isinstance(p.value, str) else "0"
                    for p in node.values
                )
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                if id(node) not in in_fstring:
                    yield node.lineno, node.value

    broken = []
    scanned = 0
    for path in sorted(root.glob("app/**/*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for lineno, js in screen_js(tree):
            if not any(hint in js for hint in js_hints):
                continue
            scanned += 1
            for ln in cut_literals(js):
                broken.append(
                    f"{path.relative_to(root)} 상수(줄 {lineno})의 JS {ln}행 — "
                    f"{js.splitlines()[ln - 1].strip()[:90]}"
                )

    assert not broken, (
        "화면 JS의 문자열이 줄바꿈으로 끊겼습니다 — 그 화면의 버튼이 전부 죽습니다:\n  "
        + "\n  ".join(broken)
        + "\n파이썬 템플릿 안에서는 줄바꿈을 `\\\\n`으로 적습니다(`\\n`은 진짜 줄바꿈이 됩니다)."
    )
    # 그물이 실제로 글을 보고 있는지 — 렌더러가 늘 수십 덩어리를 낸다. 0이면 js_hints가
    # 옛 이름을 보고 있다는 뜻이고, 그때 이 테스트는 조용히 아무것도 안 지킨다.
    assert scanned >= 20, f"화면 JS를 {scanned}덩어리밖에 못 찾았습니다 — js_hints가 헛돌고 있습니다"

    print(f"53) 화면 JS 문자열: 통과 (JS {scanned}덩어리, 줄바꿈으로 끊긴 리터럴 없음)")


def test_title_refetch_compares_fairly():
    """「제목 다시 불러오기」가 안 바뀐 제목을 「바뀜」이라 하지 않는지 지킨다.

    여기가 이 기능의 생사다. 저장된 제목과 원문 제목을 **그대로** 비교하면 거의 모든
    기사에 「바뀜」이 붙고, 모든 줄에 붙는 표시는 아무것도 가르지 못한다. 실제로 다른
    것들을 실측으로 확인했다(2026-10-12) — 네이버 검색 API가 주는 제목엔 HTML 엔티티가
    남고(`&quot;`) `<b>` 강조 태그가 섞이며, 따옴표·말줄임표·줄표 모양과 공백이 원문
    페이지와 다르다.

    같은 날 「부정기사」 라벨 8건에 돌려 **오탐 0건·진짜 변경 1건**을 확인했다. 그 1건이
    아래 문화일보 사례다(단정 → 「우려」로 톤을 낮춘 기관 대응 교정).

    요약은 대조하지 않는다 — 저장값은 네이버 검색의 「검색어 주변 발췌」이고 원문은
    「본문 맨 앞」이라 다른 물건이라, 8건 중 5건이 거짓 「바뀜」이었다
    (archive/TITLE_CHANGE_DESIGN.md §3.3).
    """
    _require_isolation()
    from app.title_refetch import (
        compare, is_title_protected, pub_differs, summarize, titles_differ, touched_since,
    )

    # (1) 모양만 다른 건 「안 바뀜」이다 — 이게 깨지면 전 기사에 표시가 붙는다.
    same = [
        ("李대통령 &quot;청렴도 31위&quot;", '李대통령 "청렴도 31위"'),        # 엔티티
        ("정부, <b>가상자산</b> 과세 검토", "정부, 가상자산 과세 검토"),        # 강조 태그
        ("재경부 \u201c검토 단계\u201d", '재경부 "검토 단계"'),               # 둥근 따옴표
        ("기한 표시 다른 잣대...", "기한 표시 다른 잣대…"),                      # 말줄임표
        ("성장자금  162조   쌈짓돈 ", "성장자금 162조 쌈짓돈"),                  # 공백
        ("한국\u2013미국 공조", "한국-미국 공조"),                              # 줄표
    ]
    for stored, fetched in same:
        assert not titles_differ(stored, fetched), f"모양만 다른데 바뀜으로 봤다: {stored!r}"

    # (2) 진짜 바뀐 건 잡는다 — 2026-10-07 문화일보 실제 사례.
    old = "장관 관사 짓는게 미래대응?… 성장자금 162조 \u2018쌈짓돈\u2019 현실화"
    new = "장관 관사 짓는게 미래대응?… 성장자금 162조 \u2018쌈짓돈\u2019 현실화 우려"
    assert titles_differ(old, new)
    # 말머리를 떼는 것도 진짜 변화다(정규화가 말머리를 지우면 안 된다).
    assert titles_differ("[단독] 살균 맥주는", "살균 맥주는")

    # (3) 못 읽은 제목으로 멀쩡한 제목을 덮지 않는다.
    assert not titles_differ(old, None)
    assert not titles_differ(old, "   ")

    # (4) ✏️로 직접 고친 제목은 건드리지 않는다. 출처를 모르는 옛 항목도 안전한 쪽으로 둔다.
    assert is_title_protected({"title": "손으로 친 제목", "manual": ["title"]})
    assert is_title_protected({"title": "옛 항목"}), "출처를 모르면 보호해야 한다"
    assert not is_title_protected({"title": "앱이 가져온 제목", "refetched_at": "2026-10-12T10:00:00"})
    assert not is_title_protected({"summary": "요약만 고친 항목"}), "제목이 없으면 덮을 게 없다"
    assert not is_title_protected(None)

    # (5) 「원문 수정」은 담은 뒤의 변화만 센다 — 실측 8건 전부에 수정 시각이 있었다.
    assert not touched_since(None, "2026-10-12 15:45:11"), "처음 확인은 기준만 적는다"
    assert touched_since("2026-10-12 09:58:11", "2026-10-12 15:45:11")
    assert not touched_since("2026-10-12 15:45:11", "2026-10-12 15:45:11")
    assert not touched_since("2026-10-12 15:45:11", "2026-10-12 09:58:11"), "과거로는 안 센다"

    # (6) 발행 시각은 분 단위로 보고, 한쪽이라도 못 읽으면 「안 바뀜」이다.
    assert not pub_differs("2026-10-07T12:11:13+09:00", "2026-10-07 12:11:55")
    assert pub_differs("2026-10-07T12:11:13+09:00", "2026-10-07 12:40:00")
    assert not pub_differs(None, "2026-10-07 12:11:13")

    # (7) 제목이 바뀐 행에 「원문도 손댔다」를 겹쳐 적지 않는다(표시가 둘이면 뜻이 흐려진다).
    both = compare(
        {"title": old, "pub_date": "2026-10-07T12:11:13+09:00"},
        {"title": new, "pub_date": "2026-10-07 12:11:13", "modified_at": "2026-10-07 17:22:19"},
        seen_modified_at="2026-10-07 13:00:00",
    )
    assert both["title_changed"] and not both["touched"], both
    assert both["new_title"] == new and both["modified_at"] == "2026-10-07 17:22:19"

    # 보호된 제목은 바뀌었어도 안 바꾼다.
    kept = compare(
        {"title": old, "pub_date": None},
        {"title": new, "pub_date": None, "modified_at": None},
        override_entry={"title": old, "manual": ["title"]},
    )
    assert kept["protected"] and not kept["title_changed"] and kept["new_title"] is None, kept

    # (8) 못 받은 기사(None)는 「확인했다」에서 빠진다 — 확인과 못 함을 뭉개지 않는다.
    counts = summarize([both, kept, None])
    assert counts == {"checked": 2, "title_changed": 1, "touched": 0,
                      "pub_changed": 0, "protected": 1}, counts

    print("55) 제목 다시 불러오기 판정: 통과 (모양 차이 6종 오탐 0, 실제 변경 1건 잡음)")

def test_naver_page_stamps_do_not_cross_match():
    """원문 페이지의 **발행 시각과 기사수정 시각이 서로 섞이지 않는지** 지킨다.

    네이버 미러 페이지는 두 값을 나란히 찍고 이름이 겹친다 —
    `data-date-time` / `data-date-time-age-in-minutes` / `data-modify-date-time` /
    `data-modify-date-time-age-in-minutes` 넷이 한 덩어리에 있다. 발행 쪽 패턴이
    수정 값을 집으면 「+ 수기로 기사 추가」의 발행시각과 기사 정렬 자리가 **조용히**
    어긋나고, 수정 쪽이 발행 값을 집으면 「제목 다시 불러오기」가 손댄 적 없는 기사를
    손댔다고 한다(archive/TITLE_CHANGE_DESIGN.md §3.2).

    아래 HTML은 2026-10-07 문화일보 기사(021/0002823595) 페이지에서 그대로 떠 온
    것이다. 같은 날 라벨 붙은 미러 기사 8건 전수로도 확인했다 — 발행·수정이 섞인
    경우 0건, 손으로 잰 정답 5건과 전부 일치.
    """
    _require_isolation()
    from app.naver_api import _modified_at_from_html, _pub_date_from_html

    page = (
        '<span class="media_end_head_info_datestamp_time _ARTICLE_DATE_TIME" '
        'data-date-time="2026-10-07 12:11:13" data-date-time-age-in-minutes="7480">'
        '2026.10.07. 오후 12:11</span>'
        '<span class="media_end_head_info_datestamp_time _ARTICLE_MODIFY_DATE_TIME" '
        'data-modify-date-time="2026-10-07 17:22:19" '
        'data-modify-date-time-age-in-minutes="7169">2026.10.07. 오후 5:22</span>'
    )
    pub, mod = _pub_date_from_html(page), _modified_at_from_html(page)
    assert pub and pub.startswith("2026-10-07T12:11"), pub
    assert mod and mod.startswith("2026-10-07T17:22"), mod
    assert pub != mod, "발행 자리에 수정 시각이 들어갔다"

    # 수정된 적 없는 기사 — 수정 시각만 없고 발행은 그대로 읽힌다(None을 지어내지 않는다).
    only_pub = '<span data-date-time="2026-10-07 12:11:13">2026.10.07.</span>'
    assert _modified_at_from_html(only_pub) is None
    assert _pub_date_from_html(only_pub).startswith("2026-10-07T12:11")

    # 자체 도메인(미러가 아닌 곳)엔 수정 시각 후보가 없다 — 발행만 폴백으로 읽는다.
    own = '<meta property="article:published_time" content="2026-10-07T12:11:13+09:00" />'
    assert _modified_at_from_html(own) is None
    assert _pub_date_from_html(own).startswith("2026-10-07T12:11")

    print("56) 원문 페이지 시각 두 개: 통과 (발행·수정 안 섞임, 없는 값은 안 지어냄)")

def test_summary_override_provenance():
    """저장된 제목이 **누가 쓴 값인지** 가려지는지 지킨다.

    app.summary_overrides는 🔄(원문에서 가져옴)와 ✏️(직접 적음)를 같은 칸에 담는다.
    「제목 다시 불러오기」가 여러 건을 한꺼번에 덮을 때 둘을 못 가리면 **담당자가 손으로
    고친 제목이 조용히 사라진다**(CODING_CONVENTIONS §3). 실제로 이 기능을 설계할 때
    저장돼 있던 15건엔 출처가 하나도 안 적혀 있었다(archive/TITLE_CHANGE_DESIGN.md §4).

    칸 단위로 적는 이유: 하나로 묶으면 **언론사명만 ✏️로 고쳐도 제목까지 영영 잠긴다.**

    같이 보는 것: 기록용 칸(manual·refetched_at·seen_modified_at)이 **기사 dict로 새지
    않는지** — apply_summary_overrides가 항목을 통째로 병합하던 자리라, 새 칸을 더하면
    기사마다 그 키가 달라붙어 내보내기·저장 경로로 퍼진다.
    """
    _require_isolation()
    from app import summary_overrides as so
    from app.title_refetch import is_title_protected

    url = "https://n.news.naver.com/mnews/article/021/0002823595"
    with tempfile.TemporaryDirectory() as tmp, \
         patch.object(so, "SUMMARY_OVERRIDES_FILE", Path(tmp) / "overrides.json"):
        # (1) 🔄가 쓴 제목은 앱이 가져온 값이라 다시 덮어도 된다.
        so.set_summary_override(url, "원문에서 가져온 제목", None)
        entry = so.load_summary_overrides()[url]
        assert entry.get("refetched_at"), entry
        assert "manual" not in entry, entry
        assert not is_title_protected(entry)

        # (2) ✏️로 친 제목은 보호된다.
        so.set_summary_override(url, "손으로 친 제목", None, manual=True)
        entry = so.load_summary_overrides()[url]
        assert entry["manual"] == ["title"], entry
        assert is_title_protected(entry)

        # (3) 그 기사에 **직접** 🔄를 누르면 손으로 친 값보다 이쪽이 최신이다 — 잠금이 풀린다.
        so.set_summary_override(url, "다시 가져온 제목", None)
        entry = so.load_summary_overrides()[url]
        assert "manual" not in entry, entry
        assert not is_title_protected(entry)

        # (4) 칸 단위다 — 언론사만 ✏️로 고쳐도 제목은 안 잠긴다.
        so.set_summary_override(url, None, None, "문화일보", manual=True)
        entry = so.load_summary_overrides()[url]
        assert entry["manual"] == ["outlet"], entry
        assert not is_title_protected(entry), "언론사만 고쳤는데 제목이 잠겼습니다"

        # (5) 출처를 모르는 옛 항목은 보호한다 — 🔄였다고 증명할 수 없다.
        assert is_title_protected({"title": "이 기능 전에 저장된 제목"})

        # (6) 기준점은 옮겨 적힌다 — 안 옮기면 한 번 손댄 기사에 영영 표시가 붙는다.
        assert so.title_check_baseline(url) is None, "확인한 적 없으면 기준이 없다"
        so.record_title_check(url, "2026-10-07T17:22:19+09:00")
        assert so.title_check_baseline(url) == "2026-10-07T17:22:19+09:00"
        so.record_title_check(url, "2026-10-08T09:00:00+09:00")
        assert so.title_check_baseline(url) == "2026-10-08T09:00:00+09:00"
        so.record_title_check(url, None)  # 미러가 아니거나 수정된 적 없는 기사
        assert so.title_check_baseline(url) == "2026-10-08T09:00:00+09:00", "빈 값이 기준을 지웠다"

        # (7) **기록용 칸은 기사 dict로 새지 않는다.** 여기가 깨지면 article["manual"]이
        #     내보내기·저장 경로로 조용히 퍼진다.
        article = {"url": url, "title": "저장된 제목", "outlet": "문화일보", "pub_date": "x"}
        applied = so.apply_summary_overrides([article])[0]
        assert applied["outlet"] == "문화일보"
        for leaked in ("manual", "refetched_at", "seen_modified_at"):
            assert leaked not in applied, f"기록용 칸 {leaked}이(가) 기사로 샜습니다: {applied}"

    print("57) 제목 출처 가리기: 통과 (🔄/✏️ 칸 단위, 옛 항목 보호, 기준점 이동, 기록 칸 안 샘)")

def test_refetch_titles_bulk():
    """「⟳ 제목 다시 불러오기」가 여러 건을 한 번에 처리하는 길을 지킨다.

    이 경로가 꿰는 것: app.naver_api(페이지 받기) → app.title_refetch(판정) →
    app.summary_overrides(저장). 셋 중 하나라도 어긋나면 담당자는 **결과 줄 숫자와
    실제로 바뀐 것이 다른** 상태를 보게 되는데, 어느 쪽이 맞는지 알 길이 없다.

    특히 지키는 둘:
      · **저장은 맨 끝에 한 번이다.** 기사마다 쓰면 60건에 파일을 120번 읽고 쓰고,
        겹치면 앞 기사의 수정이 사라진다(여러 기사 숨기기를 요청 하나로 묶은 것과 같은 이유).
      · **「확인했다」와 「확인할 수 없다」를 뭉개지 않는다.** 언론사 자체 주소는 애초에
        안 보고, 접속 실패는 「안 바뀜」으로 굳히지 않는다 — 굳히면 그 기사는 영영 안 걸린다.
    """
    _require_isolation()
    from app import summary_overrides as so
    from app import title_refetch as tr

    mirror = "https://n.news.naver.com/mnews/article/%s"
    changed_url = mirror % "021/0002823595"   # 제목이 바뀐 기사(실제 사례)
    same_url = mirror % "016/0002705959"      # 그대로
    touched_url = mirror % "023/0004003276"   # 제목은 같고 원문만 손댐
    kept_url = mirror % "015/0005332784"      # ✏️로 고쳐 둔 제목 — 건너뛴다
    dead_url = mirror % "008/0005403105"      # 접속 실패
    own_url = "https://www.newspim.com/news/view/20260903000684"  # 자체 도메인

    page = {
        changed_url: {"title": "성장자금 162조 ‘쌈짓돈’ 현실화 우려",
                      "pub_date": "2026-10-07T12:11:13+09:00",
                      "modified_at": "2026-10-07T17:22:19+09:00"},
        same_url: {"title": "삼일천하로 끝난 기재부와 캠코의 굴욕",
                   "pub_date": "2026-10-06T14:40:10+09:00",
                   "modified_at": "2026-10-06T15:10:14+09:00"},
        touched_url: {"title": "재경부 방침에 논란 확대",
                      "pub_date": "2026-10-12T09:01:11+09:00",
                      "modified_at": "2026-10-12T15:45:11+09:00"},
        kept_url: {"title": "언론사가 바꾼 제목",
                   "pub_date": "2026-09-16T08:01:12+09:00",
                   "modified_at": "2026-09-16T12:00:15+09:00"},
        dead_url: None,
    }
    asked = []

    def fake_fetch(url):
        asked.append(url)
        return page.get(url)

    articles = [
        {"url": changed_url, "title": "성장자금 162조 ‘쌈짓돈’ 현실화", "pub_date": "2026-10-07T12:11:13+09:00"},
        {"url": same_url, "title": "삼일천하로 끝난 기재부와 캠코의 굴욕", "pub_date": "2026-10-06T14:40:10+09:00"},
        {"url": touched_url, "title": "재경부 방침에 논란 확대", "pub_date": "2026-10-12T09:01:11+09:00"},
        {"url": kept_url, "title": "손으로 친 제목", "pub_date": "2026-09-16T08:01:12+09:00"},
        {"url": dead_url, "title": "못 받는 기사", "pub_date": None},
        {"url": own_url, "title": "자체 도메인 기사", "pub_date": None},
    ]

    writes = []
    real_write = so._write

    with tempfile.TemporaryDirectory() as tmp, \
         patch.object(so, "SUMMARY_OVERRIDES_FILE", Path(tmp) / "overrides.json"), \
         patch("app.naver_api.fetch_for_title_check", fake_fetch), \
         patch.object(so, "_write", lambda d: (writes.append(1), real_write(d))[1]):
        # ✏️로 고쳐 둔 제목 하나와, 「원문만 손댐」을 재려면 기준점이 하나 필요하다.
        so.set_summary_override(kept_url, "손으로 친 제목", None, manual=True)
        so.record_title_check(touched_url, "2026-10-12T09:58:11+09:00")
        writes.clear()

        result = tr.refetch_titles(articles)
        rows, counts = result["rows"], result["counts"]

        # (1) 상태가 여섯 갈래로 갈린다.
        assert rows[changed_url]["state"] == "changed", rows[changed_url]
        assert rows[same_url]["state"] == "same", rows[same_url]
        assert rows[touched_url]["state"] == "touched", rows[touched_url]
        assert rows[kept_url]["state"] == "protected", rows[kept_url]
        assert rows[dead_url]["state"] == "failed", rows[dead_url]
        assert rows[own_url]["state"] == "skipped", rows[own_url]

        # (2) 자체 도메인은 **받아오지도 않는다** — 안 볼 걸 받으면 네트워크만 쓴다.
        assert own_url not in asked, asked
        assert len(asked) == 5, asked

        # (3) 결과 줄 숫자가 실제와 맞는다.
        assert counts == {"checked": 4, "title_changed": 1, "touched": 1,
                          "pub_changed": 0, "protected": 1, "skipped": 1, "failed": 1}, counts

        # (4) **저장은 한 번이다.** 기사마다 쓰면 여기서 깨진다.
        assert writes == [1], f"파일을 {len(writes)}번 썼습니다 — 한 번이어야 합니다"

        saved = so.load_summary_overrides()
        # (5) 바뀐 제목만 적히고, ✏️ 제목은 그대로다.
        assert saved[changed_url]["title"] == "성장자금 162조 ‘쌈짓돈’ 현실화 우려"
        assert saved[kept_url]["title"] == "손으로 친 제목", saved[kept_url]
        assert saved[kept_url]["manual"] == ["title"], "✏️ 표시가 지워졌습니다"
        assert "title" not in saved.get(same_url, {}), "안 바뀐 제목을 적었습니다"

        # (6) 기준점은 확인한 기사에만 — 못 받은 기사엔 안 적는다(확인한 셈이 되면 안 된다).
        assert saved[touched_url]["seen_modified_at"] == "2026-10-12T15:45:11+09:00"
        assert so.title_check_baseline(dead_url) is None, "못 받았는데 기준을 적었습니다"
        assert so.title_check_baseline(kept_url) == "2026-09-16T12:00:15+09:00", \
            "✏️라 건너뛴 기사도 확인은 했으므로 기준은 적어야 합니다"

        # (7) 한 번 더 돌리면 「손댔다」가 가라앉는다 — 안 그러면 영영 표시가 붙는다.
        again = tr.refetch_titles([articles[2]])
        assert again["rows"][touched_url]["state"] == "same", again["rows"][touched_url]

        # (8) **제목이 바뀐 이력이 남는다.** 이게 없으면 화면의 「✎ 제목 변경」 칩과 취소선
        #     옛 제목이 새로고침 한 번에 사라진다 — 담당자에게는 그 기록이 업무 자체다.
        #     옛 제목은 **덮기 전 값**(화면이 보고 있던 제목)이라야 한다.
        hist = so.title_history_of(changed_url)
        assert len(hist) == 1, hist
        assert hist[0]["from"] == "성장자금 162조 ‘쌈짓돈’ 현실화", hist[0]
        assert hist[0]["to"] == "성장자금 162조 ‘쌈짓돈’ 현실화 우려", hist[0]
        assert hist[0]["modified_at"] == "2026-10-07T17:22:19+09:00", hist[0]
        # 바뀌지 않은 쪽엔 이력이 없다 — 「원문만 손댐」은 우리 값이 한 글자도 안 바뀌었고,
        # ✏️ 제목은 애초에 건드리지 않았다.
        assert so.title_history_of(same_url) == []
        assert so.title_history_of(touched_url) == [], "원문만 손댄 기사엔 적을 이력이 없습니다"
        assert so.title_history_of(kept_url) == [], "✏️ 제목은 건드리지 않았으므로 이력도 없습니다"

        # (9) 또 바뀌면 **쌓인다**. 마지막 하나만 들고 있으면 언론사가 옛 제목으로 되돌렸을 때
        #     무엇이 어떤 차례로 바뀌었는지 영영 알 수 없다.
        page[changed_url] = {"title": "성장자금 162조 ‘쌈짓돈’ 현실화 우려 확산",
                             "pub_date": "2026-10-07T12:11:13+09:00",
                             "modified_at": "2026-10-08T09:10:00+09:00"}
        tr.refetch_titles([{"url": changed_url,
                            "title": "성장자금 162조 ‘쌈짓돈’ 현실화 우려",
                            "pub_date": "2026-10-07T12:11:13+09:00"}])
        hist = so.title_history_of(changed_url)
        assert [h["to"] for h in hist] == ["성장자금 162조 ‘쌈짓돈’ 현실화 우려",
                                           "성장자금 162조 ‘쌈짓돈’ 현실화 우려 확산"], hist
        # 가장 오래된 from이 곧 **처음 담았을 때의 제목**이다(화면이 마지막 줄에 그 값을 적는다).
        assert hist[0]["from"] == "성장자금 162조 ‘쌈짓돈’ 현실화", hist[0]

        # (10) 화면은 저장소를 **한 번만** 읽는다 — 이력 없는 기사는 아예 안 담는다.
        table = so.title_history_map()
        assert table[changed_url] == hist
        assert same_url not in table and touched_url not in table, table.keys()

    print("58) 제목 일괄 불러오기: 통과 (여섯 갈래, 자체 도메인 안 받음, 저장 한 번, 기준점 이동, 이력 쌓임)")

def test_title_watch_alert():
    """「제목 변경 알림」이 주기마다 돌고, 알리기만 하고 아무것도 덮지 않는 길을 지킨다.

    이 기능의 중심 규칙이 **「알리고 끝」**이라, 깨지면 담당자가 안 보는 사이에 화면 제목이
    바뀌어 이미 복사해 쓴 보고서와 말없이 어긋난다. 그래서 여기서 가장 강하게 보는 것이
    「summary_overrides를 한 글자도 안 쓴다」다.

    함께 지키는 것들:
      · **때가 아니면 파일 한 번 읽고 돌아온다** — tick마다 불리므로 비용이 0이어야 한다.
      · **라벨 파일을 못 읽은 것과 라벨이 0건인 것을 가르지 못하므로**(`load_labels`가 둘 다
        `{}`다) 라벨이 비면 `alerted` 가지치기를 건너뛴다 — 안 그러면 읽기 실패 한 번에
        알린 기록이 통째로 지워지고, 다음 주기에 그동안 알린 것 전부가 다시 간다.
      · **일하다 난 예외가 스케줄러까지 올라가지 않는다** — 올라가면 그 tick의 보관 기간
        정리 묶음이 통째로 건너뛰어진다.
      · **대상은 라벨 붙인 기사 중 네이버 미러 + 발행 나이 안쪽만.** 자체 도메인은 og:title을
        믿을 수 없고, 기한이 없으면 라벨이 쌓인 1년 뒤엔 작년 기사를 매일 아침 다시 받아온다.
      · **같은 변경을 두 번 알리지 않는다**(키는 URL + 다듬은 새 제목) — 반영하지 않고 그대로
        두기로 했을 때 같은 알림이 또 오면 안 된다. 같은 기사가 **또** 고쳐지면 새 제목이라
        다시 알리고, 언론사가 **옛 제목으로 되돌리면** 그건 이미 알린 제목이라 조용하다
        (URL당 마지막 하나만 기억하면 그 되돌림을 새것으로 보고 또 알린다).
      · **「제목」 칸을 켠 사람에게만 간다.** 예전 active_alert_chat_ids는 「단독이 아닌 모든
        kind」를 속보 칸으로 떨어뜨렸다 — 그대로면 끈 사람에게도 가고 왜 오는지 알 길이 없다.
      · **아무에게도 못 갔으면 알린 것으로 적지 않는다** — 다음 주기에 다시 보낸다.
    """
    _require_isolation()
    from app import labels as labels_mod
    from app import send_log
    from app import summary_overrides as so
    from app import telegram_recipients as tr_mod
    from app import title_watch as tw
    from app.send_result import SendResult

    mirror = "https://n.news.naver.com/mnews/article/%s"
    changed_url = mirror % "021/0002823595"   # 제목이 바뀐다
    same_url = mirror % "016/0002705959"      # 그대로
    old_url = mirror % "023/0004003276"       # 미러지만 발행이 너무 오래됐다
    own_url = "https://www.newspim.com/news/view/20260903000684"  # 자체 도메인

    now = datetime(2026, 10, 13, 9, 10)
    label_rows = {
        changed_url: {"labels": ["부정기사"], "title": "성장자금 162조 ‘쌈짓돈’ 현실화",
                      "pub_date": "2026-10-07T12:11:13+09:00", "outlet": "문화일보",
                      "labeled_at": "2026-10-07T13:00:00"},
        same_url: {"labels": ["별도 보고"], "title": "삼일천하로 끝난 기재부와 캠코의 굴욕",
                   "pub_date": "2026-10-12T14:40:10+09:00", "outlet": "헤럴드경제",
                   "labeled_at": "2026-10-12T15:00:00"},
        old_url: {"labels": ["부정기사"], "title": "작년 기사", "outlet": "조선일보",
                  "pub_date": "2026-08-01T09:01:11+09:00", "labeled_at": "2026-08-01T10:00:00"},
        own_url: {"labels": ["부정기사"], "title": "자체 도메인 기사", "outlet": "뉴스핌",
                  "pub_date": "2026-10-12T12:11:00+09:00", "labeled_at": "2026-10-12T13:00:00"},
    }
    page = {
        changed_url: {"title": "성장자금 162조 ‘쌈짓돈’ 현실화 우려",
                      "pub_date": "2026-10-07T12:11:13+09:00",
                      "modified_at": "2026-10-07T17:22:19+09:00"},
        same_url: {"title": "삼일천하로 끝난 기재부와 캠코의 굴욕",
                   "pub_date": "2026-10-12T14:40:10+09:00",
                   "modified_at": "2026-10-12T15:10:14+09:00"},
    }
    asked, sent = [], []

    def fake_fetch(url):
        asked.append(url)
        return page.get(url)

    def fake_send(text, chat_ids, now=None):
        sent.append((text, list(chat_ids)))
        return SendResult(ok=True, delivered=[{"target": c} for c in chat_ids])

    with tempfile.TemporaryDirectory() as tmp, \
         patch.object(labels_mod, "LABELS_FILE", Path(tmp) / "labels.json"), \
         patch.object(tw, "TITLE_WATCH_FILE", Path(tmp) / "title_watch.json"), \
         patch.object(so, "SUMMARY_OVERRIDES_FILE", Path(tmp) / "overrides.json"), \
         patch.object(tr_mod, "TELEGRAM_RECIPIENTS_FILE", Path(tmp) / "recipients.json"), \
         patch.object(send_log, "SEND_LOG_DIR", Path(tmp) / "send_log"), \
         patch("app.naver_api.fetch_for_title_check", fake_fetch), \
         patch("app.telegram_bot.send_text", fake_send):
        labels_mod.LABELS_FILE.write_text(json.dumps(label_rows, ensure_ascii=False), encoding="utf-8")
        tw._memo_last_run_at = None
        tick = lambda when: tw.watch_tick(when, background=False)

        # (1) 주기가 안 됐으면 아무것도 하지 않는다 — 네이버를 한 건도 부르지 않는다.
        assert tw.is_due(now, (now - timedelta(minutes=5)).isoformat()) is False
        assert tw.is_due(now, (now - timedelta(minutes=31)).isoformat()) is True
        # 적힌 시각이 미래여도 돈다(시계를 되돌렸거나 파일이 깨진 경우 — 안 그러면 몇
        # 시간이고 조용해진다).
        assert tw.is_due(now, (now + timedelta(hours=3)).isoformat()) is True

        # (2) 대상 고르기 — 자체 도메인·오래된 기사는 애초에 빼 둔다.
        picked = {a["url"] for a in tw.due_articles(labels_mod.load_labels(), now)}
        assert picked == {changed_url, same_url}, picked

        # (3) **「제목」 칸을 아무도 안 켰으면 확인조차 하지 않는다** — 네이버를 안 부르고,
        # 무엇보다 「알렸다」고 적지 않는다(적으면 나중에 켜도 그 변경이 영영 안 온다).
        assert tick(now) == 0
        assert asked == [], asked
        assert tw.load_state()["alerted"] == {}, tw.load_state()
        assert send_log.load_days(now) == [], send_log.load_days(now)

        # (4) 속보 칸만 켠 사람에게는 안 간다 — 예전 코드는 「단독이 아닌 모든 kind」를
        # 속보 칸으로 떨어뜨렸다.
        tr_mod.save_telegram_recipients([
            {"name": "속보만", "chat_id": "111", "alert_flash": True},
        ])
        assert tr_mod.active_alert_chat_ids("제목") == []
        assert tick(now) == 0
        assert asked == [], asked

        # (5) 「제목」 칸을 켜면 그때부터 돈다 — 끈 동안 지나간 변경도 첫 아침에 온다.
        tr_mod.save_telegram_recipients([
            {"name": "속보만", "chat_id": "111", "alert_flash": True},
            {"name": "제목받는사람", "chat_id": "222", "alert_title_change": True},
        ])
        assert tr_mod.active_alert_chat_ids("제목") == ["222"]
        assert tr_mod.active_alert_chat_ids("속보") == ["111"]
        assert tick(now) == 1

        assert len(sent) == 1, sent
        text, chat_ids = sent[0]
        assert chat_ids == ["222"], chat_ids
        # 전·후 제목이 둘 다, 받아온 그대로 들어간다(다듬은 값은 비교용이다).
        assert "전: 성장자금 162조 ‘쌈짓돈’ 현실화" in text, text
        assert "후: 성장자금 162조 ‘쌈짓돈’ 현실화 우려" in text, text
        assert "🏷 부정기사" in text and "문화일보" in text, text
        assert "17:22 수정" in text, text          # 수정 시각은 있을 때만
        assert same_url not in text, text          # 안 바뀐 기사는 안 적는다

        # (6) **아무것도 저장하지 않는다** — 이 기능의 중심 규칙.
        assert so.load_summary_overrides() == {}, so.load_summary_overrides()
        assert not so.SUMMARY_OVERRIDES_FILE.exists() or so.load_summary_overrides() == {}

        # (7) 같은 변경을 두 번 알리지 않는다 — 주기가 지나 다시 돌아도 조용하다.
        assert tick(now + timedelta(hours=1)) == 0
        assert len(sent) == 1, sent

        # (8) 같은 기사가 **또** 고쳐지면 다시 알린다(키가 URL만이 아니라서).
        page[changed_url]["title"] = "성장자금 162조 ‘쌈짓돈’ 현실화 우려 확산"
        assert tick(now + timedelta(hours=2)) == 1
        assert len(sent) == 2, sent
        assert "우려 확산" in sent[1][0], sent[1][0]

        # (8b) 언론사가 **옛 제목으로 되돌리면** 그건 이미 알린 제목이라 조용하다.
        # URL당 마지막 하나만 기억하면 여기서 또 한 통이 간다.
        page[changed_url]["title"] = "성장자금 162조 ‘쌈짓돈’ 현실화 우려"
        assert tick(now + timedelta(hours=3)) == 0, sent
        assert len(sent) == 2, sent

        # (9) 아무에게도 못 갔으면 알린 것으로 적지 않는다 — 다음 주기에 다시 보낸다.
        page[changed_url]["title"] = "또 바뀐 제목"
        with patch("app.telegram_bot.send_text",
                   lambda text, chat_ids, now=None: SendResult(ok=False, failures=[{"target": "222"}])):
            assert tick(now + timedelta(hours=4)) == 0
            assert "또 바뀐 제목" not in str(tw.load_state()["alerted"]), tw.load_state()
        assert tick(now + timedelta(hours=5)) == 1
        assert len(sent) == 3, sent

        # (10) **라벨 파일을 못 읽은 경우에는 가지치기를 하지 않는다.** load_labels가
        # 「없음」과 「못 읽음」에 똑같이 {}를 주므로, 그대로 지우면 읽기 실패 한 번에
        # 알린 기록이 통째로 날아가고 다음 주기에 그동안 알린 것 전부가 다시 간다.
        before = dict(tw.load_state()["alerted"])
        assert changed_url in before, before
        labels_mod.LABELS_FILE.write_text("{ 깨진 json", encoding="utf-8")
        tick(now + timedelta(hours=6))
        assert tw.load_state()["alerted"] == before, tw.load_state()

        # (11) 라벨을 **정말로** 떼면 알린 기록도 함께 버린다(파일이 영원히 자라지 않게).
        labels_mod.LABELS_FILE.write_text(json.dumps(
            {same_url: label_rows[same_url]}, ensure_ascii=False), encoding="utf-8")
        tick(now + timedelta(hours=7))
        assert changed_url not in tw.load_state()["alerted"], tw.load_state()

        # (12) 일하다 난 예외는 스케줄러까지 올라가지 않는다 — 올라가면 그 tick의
        # 보관 기간 정리가 통째로 건너뛰어진다.
        labels_mod.LABELS_FILE.write_text(json.dumps(label_rows, ensure_ascii=False), encoding="utf-8")
        def boom():
            raise PermissionError("labels.json을 읽을 수 없음")
        with patch("app.labels.load_labels", boom):
            assert tick(now + timedelta(hours=8)) == 0

    print("✓ 제목 변경 알림 — 30분마다, 알리기만 하고 덮지 않는다")


def test_refetch_titles_widget_contract():
    """「⟳ 제목 다시 불러오기」를 두 화면이 **같은 약속**으로 쓰는지 지킨다.

    장치는 app.renderer 한 곳이고(CSS·버튼·JS), 화면이 지킬 약속은 넷이다 —
    묶음 안의 빈 `.refetch-note` · 줄마다 `data-refetch-row`+`data-url`+`data-pub` ·
    제목 글자를 담은 `[data-refetch-title]` · 칩이 붙을 `[data-refetch-meta]`.
    하나라도 빠지면 **버튼은 눌리는데 아무 일도 안 일어난다**(화면은 멀쩡해 보이고
    서버 로그에도 안 남는다).

    특히 지키는 둘:
      · **결과 줄은 버튼이 맡는 묶음 *안*에 있어야 한다** — JS가 `scope.querySelector`로
        찾으므로 밖에 있으면 결과가 아무 데도 안 뜬다.
      · **확정본·초안엔 표식이 없어야 한다** — 버튼이 없는 화면에 표식만 달아 두면,
        나중에 문서 전체를 훑는 버튼이 생겼을 때 그 화면들까지 조용히 딸려 간다.
    """
    _require_isolation()
    import re as _re

    from app.renderer import (
        refetch_titles_button_html, refetch_titles_note_html, refetch_titles_script,
        render_article,
    )

    article = {"url": "https://n.news.naver.com/mnews/article/023/0004003321",
               "title": "신현송, 단일종목 레버리지 ETF 대응에 “위험 충분히 경고 못 해”",
               "outlet": "조선일보", "summary": "요약",
               "pub_date": "2026-10-12T13:40:00+09:00"}

    # (1) 기본은 꺼짐 — 확정본·초안은 한 글자도 안 바뀐다.
    assert "data-refetch" not in render_article(article, [])

    # (2) 켜면 표식 넷이 다 나온다.
    on = render_article(article, [], refetch_marks=True)
    for mark in ("data-refetch-row", "data-refetch-title", "data-refetch-meta",
                 'data-pub="2026-10-12T13:40:00+09:00"', 'data-url='):
        assert mark in on, f"{mark}이(가) 없습니다"

    # (3) 제목 글자만 담겼는지 — JS가 textContent로 읽어 서버로 보낸다. 언론사 이름이
    #     같이 들어가면 **엉뚱한 제목으로 대조**해 전 기사가 「바뀜」이 된다.
    inner = _re.search(r'<span data-refetch-title>(.*?)</span>', on, _re.S)
    assert inner, on[:400]
    assert "조선일보" not in inner.group(1), inner.group(1)
    assert "신현송" in inner.group(1)

    # (4) JS가 찾는 선택자와 서버가 내는 표식의 이름이 같은지 — 둘이 갈리면 조용히 먹통이다.
    js = refetch_titles_script("127.0.0.1", 8765)
    for sel in ("[data-refetch-row]", "[data-refetch-title]", "[data-refetch-meta]",
                ".refetch-note", "data-refetch-scope", "data-url", "data-pub"):
        assert sel in js, f"JS가 {sel}을(를) 안 봅니다"
    assert "/refetch-titles" in js

    # (5) 버튼의 scope가 가리키는 묶음 **안에** 결과 줄이 있어야 한다.
    #     정기 보관함: 버튼 scope가 details.slot이고 결과 줄은 그 안(본문 맨 위).
    from app import history_renderer as hist
    slot = hist._render_slot(
        {"run_at": "2026-10-12T14:00:00", "run_slot": "14:00", "articles": [article]},
        [], "ㅇ ({outlet}) {title}",
    )
    assert 'data-refetch-scope="details.slot"' in slot
    assert slot.count('class="refetch-note"') == 1, "결과 줄이 없거나 둘입니다"
    assert slot.index('class="refetch-note"') > slot.index("<summary>"), \
        "결과 줄이 details.slot 밖에 있습니다 — JS가 못 찾습니다"
    assert "data-refetch-row" in slot

    # 0건 회차엔 버튼도 결과 줄도 안 그린다 — 받아올 게 없다.
    empty = hist._render_slot(
        {"run_at": "2026-10-12T06:00:00", "run_slot": "06:00", "articles": []},
        [], "ㅇ ({outlet}) {title}",
    )
    assert "title-refetch-btn" not in empty and "refetch-note" not in empty

    # (6) 버튼 클래스 이름이 **옛 죽은 CSS(.refetch-btn)와 겹치면 안 된다** — 그 규칙은
    #     `opacity: 0`이라(평소 숨김·hover에 나타남) 겹치는 순간 버튼이 투명해진다.
    #     실제로 그렇게 한 번 사라졌다.
    btn = refetch_titles_button_html("details.slot")
    assert 'class="title-refetch-btn"' in btn, btn[:120]
    assert 'class="refetch-btn"' not in btn
    assert refetch_titles_note_html().count("refetch-note") == 1

    # (7) **저장된 이력이 있으면 서버가 세 조각을 그린다** — 「✎ 제목 변경」 칩·옛 제목
    #     취소선 줄·이력 블록. 여기가 깨지면 담당자가 버튼을 누른 직후에는 보이는데
    #     새로고침하면 사라지는, 지금 고친 바로 그 상태로 되돌아간다.
    story = [{"at": "2026-10-15T14:02:00", "from": "옛 제목",
              "to": article["title"], "modified_at": "2026-10-15 13:50:00"}]
    marked = render_article(article, [], refetch_marks=True,
                            refetch_history={article["url"]: story})
    assert 'class="chip-changed"' in marked and "toggleRefetchHist" in marked, marked[:400]
    assert 'class="refetch-was"' in marked and "옛 제목" in marked
    assert 'class="refetch-hist"' in marked
    # **세이지는 안 붙인다** — 「색은 방금, 칩은 사실」. 저장된 이력에 색까지 칠하면
    # 한 번 바뀐 기사는 영영 「방금 손댄 것」으로 보인다.
    assert "refetch-row-changed" not in marked, "저장된 이력에 세이지를 칠했습니다"
    # 이력 블록은 <details> **밖**이라야 눌러도 기사 요약이 같이 접혔다 펼쳐지지 않는다.
    assert marked.index('class="refetch-hist"') > marked.rindex("</details>"), \
        "이력 블록이 <details> 안에 있습니다 — 누르면 요약이 같이 토글됩니다"
    # 이력이 없으면 세 조각 다 안 그리고, refetch_marks가 꺼진 화면은 이력이 있어도
    # 한 글자도 안 바뀐다(버튼 없는 화면에 표식만 달지 않는다는 그 규칙).
    assert "chip-changed" not in render_article(article, [], refetch_marks=True)
    assert "chip-changed" not in render_article(article, [],
                                                refetch_history={article["url"]: story})
    # (8) 누른 직후와 새로고침 뒤가 **같은 마크업**이어야 한다 — 그래서 화면은 짓지 않고
    #     서버가 응답에 실어 보낸 chip/was/hist를 그대로 꽂는다.
    for key in ("v.chip", "v.was", "v.hist"):
        assert key in js, f"JS가 {key}를 안 씁니다 — 화면이 제 나름대로 짓고 있습니다"
    assert "toggleRefetchHist" in js

    print("59) 제목 불러오기 장치 계약: 통과 (표식 넷, 기본 꺼짐, 결과 줄 자리, 저장된 이력은 서버가 그림)")


def test_adhoc_tab_delete_stays_put():
    """수시 카드 탭의 × — 남의 탭을 지우면 보던 화면에 머물고, 지금 카드를 지울 때만
    남은 탭(오른쪽 먼저, 맨 끝이면 왼쪽)으로, 그마저 없을 때만 수시 보관함으로 간다.

    화면과 서버가 **같은 약속**을 지켜야 돈다 — 화면은 지금 보는 카드를 `from`으로 실어
    보내고(ADHOC_CARD_ID), 서버는 그 값으로 「남의 탭인가」를 가른다. 한쪽만 고치면 서버는
    어느 화면에서 눌렀는지 알 길이 없어 예전처럼 늘 보관함으로 튕긴다.

    이름(gone)은 **지우기 전에** 읽어야 한다 — 지운 뒤엔 카드가 trash에 있어 못 찾는다.
    """
    _require_isolation()
    from urllib.parse import quote

    from app.adhoc import card, renderer, routes

    class _H:
        def __init__(self):
            self.target = None
            self.code = None

        def _redirect(self, target):
            self.target = target

        def send_response(self, code):
            self.code = code

        def end_headers(self):
            pass

    def _delete(card_id, viewing):
        h = _H()
        routes._handle_delete(h, {"id": [card_id], "from": [viewing]})
        return h.target or ""

    with tempfile.TemporaryDirectory() as tmp:
        adhoc_dir = Path(tmp)
        (adhoc_dir / "cards").mkdir()
        (adhoc_dir / "trash").mkdir()
        with patch.object(card, "CARDS_DIR", adhoc_dir / "cards"), \
             patch.object(card, "TRASH_DIR", adhoc_dir / "trash"), \
             patch.object(card, "ISSUES_FILE", adhoc_dir / "issues.json"):
            issue = card.get_or_create_issue("증안펀드")
            now = datetime.now()
            tabs = [
                card.new_card(issue["id"], "증안펀드", "00:00", f"1{i}:00", ["증권시장안정펀드"],
                              now=now - timedelta(minutes=30 - i))
                for i in range(3)
            ]
            # 같은 날 확정본 — 탭 줄은 같은 종류끼리만 서므로 이웃 후보에 끼면 안 된다.
            bundle = card.new_bundle_card(
                "증안펀드", issue_id=issue["id"], basis_time="14:55", source_card_id=tabs[0]["id"],
            )

            # 화면이 지키는 약속 — 지금 보는 카드를 `from` 으로 함께 보낸다.
            page = renderer.render_card_page(tabs[2])
            assert f'var ADHOC_CARD_ID = "{tabs[2]["id"]}"' in page, "화면이 지금 카드 id를 안 들고 있음"
            assert "post('/adhoc/card/delete', {id: id, from: ADHOC_CARD_ID})" in page, \
                "deleteCard가 보던 카드를 안 보냄 — 서버가 남의 탭인지 가를 수 없다"

            # 1) 남의 탭 × — 보던 화면(#3)은 멀쩡하니 그 자리에 머문다. 이름이 안내에 실린다.
            target = _delete(tabs[0]["id"], tabs[2]["id"])
            assert f'/adhoc/card?id={tabs[2]["id"]}' in target, f"남의 탭을 지웠는데 화면이 바뀜: {target}"
            assert "gone=" in target and quote("증안펀드") in target, \
                f"지운 이름이 안 실림(지운 뒤에 읽었나): {target}"

            # 2) 지금 카드 — 오른쪽 탭으로. 확정본은 종류가 달라 후보가 아니다.
            target = _delete(tabs[1]["id"], tabs[1]["id"])
            assert f'/adhoc/card?id={tabs[2]["id"]}' in target, f"오른쪽 탭으로 안 감: {target}"
            assert bundle["id"] not in target, "다른 종류(확정본) 탭이 이웃 후보에 섞임"

            # 3) 맨 끝 탭이면 왼쪽으로 물러난다.
            left = card.new_card(issue["id"], "증안펀드", "00:00", "09:00", ["증권시장안정펀드"],
                                 now=now - timedelta(minutes=40))
            target = _delete(tabs[2]["id"], tabs[2]["id"])
            assert f'/adhoc/card?id={left["id"]}' in target, f"왼쪽 탭으로 안 물러남: {target}"

            # 4) 남은 탭이 없으면 그때만 보관함 — 거기서도 같은 초록 안내가 뜬다.
            target = _delete(left["id"], left["id"])
            assert target.startswith("/adhoc?") or target == "/adhoc", f"보관함으로 안 감: {target}"
            assert "gone=" in target, f"보관함 초록 안내가 안 실림: {target}"

            # 5) 옛 화면(캐시된 HTML)이 from 없이 보내도 안 깨진다 — 예전처럼 보관함으로.
            h = _H()
            routes._handle_delete(h, {"id": [bundle["id"]]})
            assert h.target and (h.target.startswith("/adhoc?") or h.target == "/adhoc"), h.target

    print("55) 수시 탭 × 삭제: 통과 (남의 탭은 제자리, 지금 카드는 오른쪽→왼쪽 탭, 없으면 보관함, 이름은 지우기 전에)")


def test_excel_merge():
    """「엑셀로 목록 만들기」 — 담당자가 들고 온 표를 보고서 형식으로 바꾼다.

    이 테스트가 지키는 것 다섯:
      (1) **표를 읽는 일이 조용히 틀리지 않는다** — 가운데 빈 줄은 건너뛰고, 날짜 칸은
          `2026-10-21 00:00:00`이 아니라 사람이 적는 모양이며, 제목 칸에만 링크가 걸린
          표(주소 열이 없는 표)는 **보이는 열 하나로 꺼내 준다**. 안 꺼내면 담당자는
          「표를 올렸는데 0건」을 만난다. 줄 수를 넘으면 **잘라 주지 않고 거절한다** —
          조용히 자르면 다 담긴 줄 알고 보고서를 보낸다.
      (2) **AI 배정이 이미 적힌 소제목을 한 글자도 안 건드린다.** 정기 초안의 「AI 기사
          배정」·수시 카드와 같은 안전장치다 — 빈 칸만 채운다.
      (3) **모델이 응답에서 빠뜨린 기사는 「기타」로 간다** — 한 번 눌러 미분류가 항상
          비도록(정기·수시가 같은 버그를 겪고 고친 자리).
      (4) **AI 키가 없으면 실패가 아니라 담당자의 선택이다** — 눌러도 같은 결과인 버튼을
          그리지 않고 앰버 안내로 바꾼다(빨강은 오류의 색이다).
      (5) **아무것도 저장하지 않는다** — 이 화면은 흐름 밖이라 정기 큐레이션 파일
          (배정·숨김·커스텀 소제목·소제목 순서)을 건드릴 이유가 없다.
    """
    import tempfile
    from pathlib import Path
    from unittest.mock import patch

    from openpyxl import Workbook

    import app.excel_merge as em
    from app.excel_merge import TableReadError, assign_subheadings, read_xlsx_rows
    from app.excel_merge_renderer import render_excel_merge_page

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)

        # (1) 표 읽기 ────────────────────────────────────────────────
        def _book(rows, link_col=None):
            wb = Workbook()
            ws = wb.active
            for r in rows:
                ws.append(r)
            if link_col:
                for i, url in link_col.items():
                    ws.cell(row=i, column=3).hyperlink = url
            path = tmp_dir / "t.xlsx"
            wb.save(path)
            return path.read_bytes()

        raw = _book(
            [
                ["소제목", "언론사", "기사제목", "발행일"],
                ["세제", "한국경제", "가상자산 과세 2년 유예", datetime(2026, 10, 21, 9, 14)],
                [None, None, None, None],
                ["", "매일경제", "종부세 동결", datetime(2026, 10, 21)],
            ],
            link_col={2: "https://n.news.naver.com/mnews/article/015/1",
                      4: "https://n.news.naver.com/mnews/article/009/2"},
        )
        rows = read_xlsx_rows(raw)
        assert len(rows) == 3, f"빈 줄을 안 건너뜀: {rows}"
        assert rows[1][3] == "2026-10-21 09:14", f"발행일 모양: {rows[1][3]}"
        assert rows[2][3] == "2026-10-21", f"시각 없는 날짜에 00:00을 붙임: {rows[2][3]}"
        assert rows[1][-1].startswith("http"), f"셀에 걸린 링크를 열로 안 꺼냄: {rows[1]}"
        assert rows[0][-1] == "", f"머리줄 자리에 엉뚱한 값: {rows[0]}"

        try:
            read_xlsx_rows(b"not an xlsx")
            assert False, "깨진 파일을 받아들임"
        except TableReadError:
            pass
        with patch.object(em, "MAX_TABLE_ROWS", 2):
            try:
                read_xlsx_rows(_book([["a"], ["b"], ["c"], ["d"]]))
                assert False, "줄 수를 넘겼는데 잘라서 돌려줌 — 거절해야 한다"
            except TableReadError as exc:
                assert "나눠서" in str(exc), f"할 일을 안 알려줌: {exc}"

        # (2)(3) AI 배정 ────────────────────────────────────────────
        items = [
            {"url": "u1", "title": "가상자산 과세 유예", "outlet": "한국경제", "section": "세제"},
            {"url": "u2", "title": "예산 총지출 증가", "outlet": "연합뉴스", "section": ""},
            {"url": "u3", "title": "외환시장 점검회의", "outlet": "서울경제", "section": ""},
        ]
        with patch.object(em, "llm_is_configured", lambda: True), \
             patch.object(em, "assign_to_existing", lambda arts, groups: {"u2": "세제"}) as _a, \
             patch.object(em, "classify_isolated", lambda *a, **k: (_ for _ in ()).throw(AssertionError(
                 "소제목이 이미 있는데 처음부터 분류했다"))):
            out = assign_subheadings(items)
        assigned = out["assigned"]
        assert "u1" not in assigned, "이미 적힌 소제목을 건드렸다"
        assert assigned["u2"] == "세제", assigned
        assert assigned["u3"] == "기타", f"응답에서 빠진 기사를 미분류로 남김: {assigned}"

        # 소제목이 하나도 없으면 처음부터 묶는다 — 「기타」 칸은 늘 열려 있다.
        blank = [dict(a, section="") for a in items]
        groups = [{"name": "세제·예산", "articles": [{"url": "u1"}, {"url": "u2"}], "summary": "…"}]
        with patch.object(em, "llm_is_configured", lambda: True), \
             patch.object(em, "classify_isolated", lambda *a, **k: groups), \
             patch.object(em, "assign_to_existing", lambda *a, **k: (_ for _ in ()).throw(AssertionError(
                 "소제목이 없는데 기존 칸에 끼워 넣으려 했다"))):
            out = assign_subheadings(blank)
        assert out["assigned"] == {"u1": "세제·예산", "u2": "세제·예산", "u3": "기타"}, out

        # (4) 키가 없으면 부르지 않는다 — 이유를 돌려주고 네트워크로 안 나간다.
        with patch.object(em, "llm_is_configured", lambda: False), \
             patch.object(em, "classify_isolated", lambda *a, **k: (_ for _ in ()).throw(AssertionError(
                 "키가 없는데 AI를 불렀다"))):
            assert assign_subheadings(blank) == {"reason": "no_key"}
        assert "LLM(AI) 연동" in em.ASSIGN_REASONS["no_key"], "할 일(키 등록)을 안 알려줌"

        # 화면: 키가 있으면 버튼, 없으면 앰버 안내 한 줄.
        with patch("app.excel_merge_renderer.llm_is_configured", lambda: True):
            html_on = render_excel_merge_page()
        with patch("app.excel_merge_renderer.llm_is_configured", lambda: False):
            html_off = render_excel_merge_page()
        assert 'id="ai"' in html_on and "AI 소제목 배정" in html_on, "AI 버튼이 없다"
        assert 'id="ai"' not in html_off, "키가 없는데 눌러도 같은 결과인 버튼을 그렸다"
        assert "LLM(AI) 연동" in html_off and 'class="warn"' in html_off, "키 없음을 안내하지 않는다"
        assert "error" not in html_off.split('class="warn">')[1][:200], "키 없음을 오류(빨강)로 그렸다"

        # 화면 계약: 세 단계 · 가져가기 셋 · **정기** 텍스트 형식을 구워 넣는다.
        for probe in ("① 표 붙여넣기", "② 열 맞추기", "③ 결과",
                      'action="/download-text"', 'action="/download-excel"'):
            assert probe in html_on, f"화면에 {probe}가 없다"

        # 흐름 밖 화면은 **정기** 텍스트 형식을 쓴다(CLAUDE.md 「텍스트 형식」) — 두 흐름의
        # 값을 다르게 두고 어느 쪽이 구워지는지 본다. 전용 형식 설정을 만들지 않는 근거다.
        both = {
            "article_line_template": "ㅇ ({outlet}) {title}",
            "subheading_format_template": "<{section}>",
            "adhoc_article_line_template": "— 수시전용 {outlet} {title}",
            "adhoc_subheading_format_template": "[[{section}]]",
            "outlet_order": ["KBS", "한국경제"],
        }
        with patch("app.excel_merge_renderer.load_settings", lambda: both):
            html_fmt = render_excel_merge_page()
        assert "ㅇ ({outlet}) {title}" in html_fmt and "<{section}>" in html_fmt, "정기 형식을 안 구웠다"
        assert "수시전용" not in html_fmt and "[[" not in html_fmt, "흐름 밖 화면인데 수시 값을 끌어왔다"

        # (5) 저장하지 않는다 — 정기 큐레이션 파일이 하나도 안 생긴다.
        for name in ("group_overrides.json", "hidden_articles.json",
                     "custom_groups.json", "group_order.json"):
            assert not (tmp_dir / name).exists(), f"{name}을 건드렸다"

    print("56) 엑셀로 목록 만들기: 통과 (빈 줄·날짜·셀 링크, 줄 수 초과는 거절, AI는 빈 소제목만·빠진 기사는 기타, 키 없으면 안내)")


if __name__ == "__main__":
    tests = [
        test_unclassified_sits_below_summary,
        test_empty_custom_group_appears_once_in_group_list,
        test_custom_group_with_taken_name_gets_its_own_box,
        test_custom_group_alias_outlives_the_day_and_the_delete,
        test_etc_order_follows_the_user,
        test_home_issue_card_uses_display_names,
        test_no_ai_key_is_not_a_failure,
        test_env_key_transplant,
        test_key_paths_stay_narrow,
        test_screen_js_strings_are_not_cut_by_newlines,
        test_login_lock,
        test_search_operator_is_one_rule,
        test_title_refetch_compares_fairly,
        test_naver_page_stamps_do_not_cross_match,
        test_summary_override_provenance,
        test_refetch_titles_bulk,
        test_refetch_titles_widget_contract,
        test_title_watch_alert,
        test_photo_override_is_one_place,
        test_photo_badge_css_is_in_every_screen,
        test_outlet_override_is_one_place,
        test_agency_archive_filters,
        test_agency_archive_keeps_every_date,
        test_agency_archive_labels,
        test_live_labels,
        test_excel_merge,
        test_draft_all_first_classified_hidden_shows_unclassified,
        test_partial_cache_survives_non_hidden_drops,
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
        test_adhoc_tab_delete_stays_put,
        test_adhoc_nav_today_only,
        test_photo_caption_name_list_title,
        test_photo_caption_candidate_title_and_cut_stamp,
        test_outlet_photo_tag,
        test_photo_body_verdict,
        test_undo_returns_only_what_the_user_touched,
        test_adhoc_past_date_collect,
        test_adhoc_reload_to_now,
        test_label_archive_copy_and_order,
        test_photo_gather_and_adhoc_bulk_hide,
        test_split_group,
        test_adhoc_four_step_flow,
        test_adhoc_screen_name,
        test_adhoc_move_to_other_issue_bundle,
        test_destination_switches,
        test_adhoc_raw_trash_and_rounds,
        test_adhoc_raw_filter_row,
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
        test_last_slot_topup_adds_late_articles,
        test_new_article_placement,
        test_trend_end_day_follows_today_completion,
        test_trend_today_partial_column,
        test_alert_scope_follows_keyword_switch,
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
                    # 기관 기사 원장 — 테스트가 쓴 가짜 날짜가 실제 data/agency_archive/에
                    # 남으면 담당자 화면에 그대로 보인다(스케줄러 테스트가 실제로 그랬다).
                    (agency_archive, "AGENCY_ARCHIVE_DIR", tmp_dir / "agency_archive"),
                    # 한 줄 메모 — 격리하지 않으면 실제 data/를 읽어, 오늘 그 회차에 메모를
                    # 써 둔 날에만 화면 테스트가 실패한다(메모가 있으면 툴바 버튼이 안 그려진다).
                    (manual_keyword_note, "MANUAL_KEYWORD_NOTE_FILE", tmp_dir / "manual_keyword_note.json"),
                    # 담당자가 직접 끈 사진 추정 — 경로와 함께 메모리 캐시도 비운다
                    # (서명으로 캐시하므로 앞 테스트의 값이 남으면 다음 테스트가 그걸 읽는다).
                    (photo_overrides, "PHOTO_OVERRIDES_FILE", tmp_dir / "photo_overrides.json"),
                    (photo_overrides, "_cache", None),
                    (photo_overrides, "_cache_sig", None),
                ):
                    more_patches.enter_context(patch.object(target, name, value))
                # [추가: 2026-09-17] LLM 분류 캐시도 임시 폴더로 — 예전엔 회차를 저장하는
                # 테스트마다 실제 data/llm_classification_cache.json에 가짜 항목이 쌓였다.
                # 메모리 캐시도 테스트마다 비워, 앞 테스트의 분류가 다음 테스트로 새지 않게 한다.
                # [추가: 2026-08-20] collect_run이 회차 저장 직전에 [단독]·[속보] 알림
                # "바닥" 경로(app.breaking_alert_sender.detect_and_alert)를 항상 거치므로,
                # 그 경로가 쓰는 파일도 임시 폴더로 옮겨야 이 테스트가 실제 data/를 안 건드린다.
                # [추가: 2026-09-28] 네이버 키를 가짜로 심는다 — 예전엔 이 자리에서
                # 실제 .env의 NAVER_CLIENT_ID로 폴백해 테스트가 개발자의 키에 기대고
                # 있었다(.env에서 키를 옮겨 심자 드러났다). 네트워크는 어차피 전부
                # mock이므로 값은 아무거나 되고, AI·텔레그램·이메일 키는 심지 않는다
                # (「키 없음」 동작을 검증하는 테스트들이 있다).
                (tmp_dir / "credentials.json").write_text(
                    '{"naver": {"client_id": "test-id", "client_secret": "test-secret"}}',
                    encoding="utf-8",
                )
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
