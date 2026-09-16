# Design Ref: PRD.md 기능1 규칙 6·15·16·20 — 검색 키워드/언론사 선택/형광펜 단어/수집 시각, 코드 수정 없이 화면에서 변경
import json
import re
from datetime import datetime
from typing import Optional

from app.atomic_write import atomic_write_text
from app.config import (
    BASE_GROUP_KEYWORDS,
    BASE_GROUP_NAME,
    DEFAULT_ARTICLE_LINE_TEMPLATE,
    DEFAULT_AUTO_SEND_ENABLED,
    DEFAULT_AUTO_SEND_GRACE_MIN,
    DEFAULT_HIGHLIGHT_KEYWORDS,
    DEFAULT_EXCLUDE_PERSONNEL_IN_SCRAP,
    DEFAULT_EXCLUDE_PHOTO_IN_SCRAP,
    DEFAULT_KEYWORD_GROUP_NAME,
    DEFAULT_SCHEDULE_GROUP_NAME,
    DEFAULT_SUBHEADING_FORMAT_TEMPLATE,
    MAX_AUTO_SEND_GRACE_MIN,
    HIGHLIGHT_COLORS,
    MAX_HIGHLIGHT_KEYWORDS,
    MAX_KEYWORD_GROUPS,
    MAX_KEYWORDS_PER_GROUP,
    MAX_SCHEDULE_GROUPS,
    MAX_SCHEDULE_TIMES,
    OUTLET_ORDER_JUMP_STEP,
    MAX_WORDCLOUD_EXCLUDE_WORDS,
    MIN_SCHEDULE_TIMES,
    SCHEDULE_TIMES,
    SETTINGS_FILE,
    WEEKDAY_KEYS,
    WEEKDAY_LABELS_KO,
)
from app.naver_api import ALL_OUTLET_NAMES

VALID_MODES = ("OR", "AND")
VALID_DIRECTIONS = ("up", "down", "jumpup", "jumpdown")
_TIME_PATTERN = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


class SettingsError(ValueError):
    """설정값이 PRD 규칙을 어겼을 때 (예: 그룹 6개째 추가, 알 수 없는 언론사)."""


def _base_group() -> dict:
    # [추가: 2026-07-25] include_in_scrap=True — 예정된 회차 스크랩(app.scraper.collect_run)은
    # 이 플래그가 켜진 그룹만 검색한다(기관정보만 우선 스크랩, 나머지 주제어 그룹은
    # 실시간 기사 현황에서만 검색되고 "→ 스크랩"으로 사람이 골라 옮겨야 정식 반영됨).
    # 새로 추가하는 사용자 그룹은 기본이 꺼짐이다(app.settings_server._parse_group).
    # [수정: 2026-07-29] 한때 이 그룹만 삭제 불가·OR 고정("deletable" 필드)이었는데,
    # 담당 기관이 바뀔 수도 있고 다른 조사 대상 그룹과 완전히 동등하게 두는 게 낫다는
    # 판단(사용자 요청)으로 그 특수 취급을 없앴다 — 이제 다른 그룹과 똑같이 이름·
    # 키워드·OR/AND 자유롭게 바꾸거나 그룹째 지울 수 있다("그룹이 하나도 안 남으면
    # 에러" 규칙이 최소 1개는 계속 보장한다, validate_keyword_groups).
    return {
        "name": BASE_GROUP_NAME,
        "keywords": list(BASE_GROUP_KEYWORDS),
        "mode": "OR",
        "include_in_live": True,
        "include_in_scrap": True,
        "disabled_keywords": [],
        "enabled": True,
    }


def _default_settings() -> dict:
    return {
        "keyword_groups": [_base_group()],
        # [추가: 2026-07-25] 기관 정보 그룹을 더는 코드에서 고정 주입하지 않고, 이렇게
        # 최초 1회만 keyword_groups에 심어둔다 — 이후로는 다른 그룹과 완전히 동등하게
        # 사용자가 이름·키워드·OR/AND를 바꾸거나 그룹째 지울 수 있다. 이 표시가 있으면
        # load_settings가 다시 심지 않는다(사용자가 정말 다 지운 것과 구분하기 위해).
        "base_group_seeded": True,
        "outlet_order": [],
        "highlight_keywords": [dict(item) for item in DEFAULT_HIGHLIGHT_KEYWORDS],
        "article_line_template": DEFAULT_ARTICLE_LINE_TEMPLATE,
        "subheading_format_template": DEFAULT_SUBHEADING_FORMAT_TEMPLATE,
        "schedule_groups": [
            {
                "name": DEFAULT_SCHEDULE_GROUP_NAME,
                "times": [{**dict(w), "enabled": True} for w in SCHEDULE_TIMES],
                "enabled": True,
                "days": [],
            }
        ],
        "wordcloud_exclude_words": [],
        "exclude_photo_in_scrap": DEFAULT_EXCLUDE_PHOTO_IN_SCRAP,
        "exclude_personnel_in_scrap": DEFAULT_EXCLUDE_PERSONNEL_IN_SCRAP,
        "auto_send_enabled": DEFAULT_AUTO_SEND_ENABLED,
        "auto_send_grace_min": DEFAULT_AUTO_SEND_GRACE_MIN,
    }


def _migrate_keyword_groups(old_keywords: list, old_mode: str) -> list:
    """[추가: 2026-07-25] 키워드 그룹 도입 이전(평평한 keywords+mode) 형식을 옮긴다.

    기관 정보 그룹을 맨 앞에 두고, 옛 키워드 중 거기 이미 포함된 것은 버리고(중복 등록
    방지) 남는 게 있으면 사용자 그룹 1개("키워드 그룹1")로 이어붙인다.
    """
    groups = [_base_group()]
    remaining = [k for k in old_keywords if k not in BASE_GROUP_KEYWORDS][:MAX_KEYWORDS_PER_GROUP]
    if remaining:
        groups.append(
            {
                "name": f"{DEFAULT_KEYWORD_GROUP_NAME}1",
                "keywords": remaining,
                "mode": old_mode if len(remaining) >= 2 else "OR",
            }
        )
    return groups


def _write(settings: dict) -> None:
    atomic_write_text(SETTINGS_FILE, json.dumps(settings, ensure_ascii=False, indent=2))


def _migrate_schedule_times(raw_times: list) -> list:
    """[수정: 2026-07-24] 회차별 시간창 수집 도입 이전(문자열 시각 목록, 예: ["09:00", "10:30"])
    형식을 {start, end} 목록으로 바꾼다.

    첫 항목은 자정부터, 이후 항목은 바로 앞 항목의 끝 시각부터 시작하는 창으로 간주한다
    (예전엔 회차 사이 시간창이라는 개념이 없었지만, 이게 가장 자연스러운 해석이다).
    사용자가 설정 화면에서 원하는 대로 다시 조정하면 된다.
    """
    windows = []
    prev_end = "00:00"
    for end in sorted(raw_times):
        windows.append({"start": prev_end, "end": end})
        prev_end = end
    return windows


def load_settings() -> dict:
    """저장된 설정을 읽어온다. 파일이 없으면 기본값으로 만들어두고 반환한다.

    옛 버전(언론사 선택·형광펜 단어·수집 시각 설정 기능 이전)에 저장된 파일에는 그
    필드가 없을 수 있어, 없으면 기본값으로 채워 반환한다 (하위 호환). schedule_times가
    옛 문자열 목록 형식이면 {start, end} 형식으로 자동 변환해 다시 저장한다. keywords+mode
    (키워드 그룹 도입 이전 형식)가 남아 있으면 keyword_groups로 옮겨 다시 저장한다.
    highlight_keywords가 옛 문자열 목록 형식이면 {word, color} 형식으로 옮긴다.
    """
    if not SETTINGS_FILE.exists():
        settings = _default_settings()
        _write(settings)
        return settings
    settings = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    settings.setdefault("outlet_order", [])
    settings.setdefault("highlight_keywords", [dict(item) for item in DEFAULT_HIGHLIGHT_KEYWORDS])
    settings.setdefault("article_line_template", DEFAULT_ARTICLE_LINE_TEMPLATE)
    settings.setdefault("subheading_format_template", DEFAULT_SUBHEADING_FORMAT_TEMPLATE)
    settings.setdefault("wordcloud_exclude_words", [])
    # [추가: 2026-08-11] 옛 include_*(포함할지) -> 새 exclude_*(제외할지) 1회 이관.
    # 의미가 정반대라 값을 뒤집어 옮긴다 — "포함 안 함"이 곧 "제외함"이므로, 이미 쓰던
    # 사람의 실제 수집 동작은 이름만 바뀌고 그대로 유지된다. 옛 키는 지워서 다음부터는
    # 이 분기를 타지 않게 한다.
    for old, new in (
        ("include_photo_in_scrap", "exclude_photo_in_scrap"),
        ("include_personnel_in_scrap", "exclude_personnel_in_scrap"),
    ):
        if old in settings:
            settings.setdefault(new, not settings[old])
            del settings[old]
    settings.setdefault("exclude_photo_in_scrap", DEFAULT_EXCLUDE_PHOTO_IN_SCRAP)
    settings.setdefault("exclude_personnel_in_scrap", DEFAULT_EXCLUDE_PERSONNEL_IN_SCRAP)
    # [수정: 2026-08-11] 채널별 telegram_auto_send/email_auto_send는 통합 설정으로 대체됐다.
    # 옛 값을 이어받지 않고 버리는 이유: 그 설정은 발송 경로가 바뀐 뒤로 **아무도 읽지 않는
    # 고아값**이었다(꺼놔도 자동 발송됐다). 즉 저장된 False는 "자동 발송을 끄고 싶다"는
    # 의사가 아니라 아무 효력 없던 잔재라, 그대로 옮기면 지금까지 실제로 나가던 자동 발송이
    # 갑자기 멈춘다. 통합 설정은 실제 동작(=켜짐)을 기본값으로 시작한다.
    settings.pop("telegram_auto_send", None)
    settings.pop("email_auto_send", None)
    settings.setdefault("auto_send_enabled", DEFAULT_AUTO_SEND_ENABLED)
    settings.setdefault("auto_send_grace_min", DEFAULT_AUTO_SEND_GRACE_MIN)

    # [추가: 2026-07-25] 형광펜 단어가 색 인덱스 없이 문자열 목록뿐이던 옛 형식이면,
    # 그때와 같은 규칙(등록 순서대로 색 배정)으로 옮겨 처음 보는 화면이 갑자기 색이
    # 달라 보이지 않게 한다.
    raw_highlight = settings.get("highlight_keywords", [])
    if raw_highlight and isinstance(raw_highlight[0], str):
        settings["highlight_keywords"] = [
            {"word": word, "color": i % len(HIGHLIGHT_COLORS)} for i, word in enumerate(raw_highlight)
        ]
        _write(settings)

    if "keywords" in settings:
        settings["keyword_groups"] = _migrate_keyword_groups(
            settings.pop("keywords"), settings.pop("mode", "OR")
        )
        settings["base_group_seeded"] = True
        _write(settings)
    settings.setdefault("keyword_groups", [])

    # [추가: 2026-07-25] 기관 정보 그룹을 고정 주입에서 "최초 1회만 심어주는 기본값"으로
    # 바꾸면서, 이미 keyword_groups가 있는(=위 "keywords" 마이그레이션도 이미 끝났거나
    # 애초에 없었던) 기존 설치본에는 아직 기관 정보 그룹이 들어있지 않을 수 있다.
    # base_group_seeded가 없으면 이번이 그 첫 로드이므로 한 번만 앞에 심고 표시해 둔다.
    if not settings.get("base_group_seeded"):
        settings["keyword_groups"] = [_base_group()] + settings["keyword_groups"]
        settings["base_group_seeded"] = True
        _write(settings)

    # [추가: 2026-07-25] "정기 스크랩 포함" 플래그 도입 이전에 저장된 그룹에는 이 필드가
    # 없다 — 없으면 켬으로 간주한다(하위 호환: 이 필드가 생겼다고 기존에 스크랩되던
    # 그룹이 갑자기 스크랩에서 빠지면 사용자가 당황한다). 새로 저장되는 그룹은
    # save_keyword_groups가 항상 명시적으로 값을 채우므로 이 보정을 거칠 일이 없다.
    changed = False
    for group in settings.get("keyword_groups", []):
        if "include_in_scrap" not in group:
            group["include_in_scrap"] = True
            changed = True
        # [수정: 2026-07-29] "삭제 불가" 특수 취급을 없애면서 deletable 필드 자체가
        # 더 이상 쓸모없어졌다 — 예전에 이 필드가 저장된 적 있는 설치본(특히 기관정보
        # 그룹에 deletable: false로 박혀있던 경우)은 여기서 지워서, 화면·데이터 양쪽
        # 모두에서 "이 그룹만 특별하다"는 흔적이 남지 않게 한다.
        if "deletable" in group:
            del group["deletable"]
            changed = True
        # [추가: 2026-07-25] 키워드별 ON/OFF 토글 이전 저장분에는 이 필드가 없다 — 없으면
        # 전부 켜진 것으로 간주한다(하위 호환).
        if "disabled_keywords" not in group:
            group["disabled_keywords"] = []
            changed = True
        # [추가: 2026-08-12] 그룹 전체 on/off 토글 도입 이전 저장분에는 이 필드가 없다 —
        # 없으면 켬으로 간주한다(하위 호환: 이 필드가 생겼다고 기존에 검색되던 그룹이
        # 갑자기 빠지면 사용자가 당황한다). include_in_scrap과 같은 이유·같은 패턴.
        if "enabled" not in group:
            group["enabled"] = True
            changed = True
        # [추가: 2026-09-15] 「그룹 스위치 + 정기 체크」가 목적지 스위치 두 개(실시간 /
        # 초안·확정본)로 바뀌기 전 저장분 — 옛 enabled=False는 "둘 다 끔"이었으므로
        # 정기 체크가 남아 있어도 초안·확정본은 끈 채로 옮긴다(그대로 옮기면 담당자가 꺼
        # 둔 그룹이 어느 날 갑자기 정기에 들어간다). 규칙은 group_in_live/group_in_scrap
        # 한 곳에 있다.
        if "include_in_live" not in group:
            live, scrap = group_in_live(group), group_in_scrap(group)
            group["include_in_live"] = live
            group["include_in_scrap"] = scrap
            changed = True
        # enabled는 이제 "어딘가에 쓰이나"를 저장만 해 두는 파생값이다 — 옛 코드로 되돌려도
        # 정기 그룹이 그대로 수집되게 남겨 둔다. 파일을 손으로 고쳐 어긋났으면 맞춘다.
        in_use = bool(group["include_in_live"]) or bool(group["include_in_scrap"])
        if group["enabled"] != in_use:
            group["enabled"] = in_use
            changed = True
    if changed:
        _write(settings)

    # [수정: 2026-07-29] 스크랩 시간대 "그룹"(주중/주말처럼 세트를 여러 개 두고 그 중
    # 1개만 활성) 도입 — 이전 저장분(schedule_groups가 아예 없음)은 그때까지의
    # schedule_times(문자열 목록/구버전 dict 목록/on-off 필드 없는 목록 등 어떤 형식이든)를
    # 하나의 그룹("기본", 활성)으로 그대로 옮긴다. 이후로는 schedule_times 키 자체를 안 쓴다.
    if "schedule_groups" not in settings:
        raw_schedule = settings.get("schedule_times")
        if not raw_schedule:
            raw_schedule = [dict(w) for w in SCHEDULE_TIMES]
        elif isinstance(raw_schedule[0], str):
            raw_schedule = _migrate_schedule_times(raw_schedule)
        for window in raw_schedule:
            window.setdefault("enabled", True)
        settings["schedule_groups"] = [
            {"name": DEFAULT_SCHEDULE_GROUP_NAME, "times": raw_schedule, "enabled": True, "days": []}
        ]
        settings.pop("schedule_times", None)
        _write(settings)

    # [수정: 2026-08-10] 그룹당 라디오 "active"(정확히 1개만 켜짐) 대신, 그룹마다 독립적인
    # "enabled" on/off + "days"(요일) 체크로 바뀌었다 — 이전 저장분("active" 필드가 있고
    # "enabled"/"days"가 없음)은 active였던 그룹을 enabled=True, days=[]("요일 무관하게
    # 항상 적용" — 예전에 유일하게 켜져 있던 그 그룹과 동작이 똑같다)로, 나머지는
    # enabled=False, days=[]로 그대로 옮긴다. 이렇게 하면 마이그레이션 직후에도 기존
    # 사용자의 스케줄 동작이 조금도 안 바뀐다.
    schedule_groups_changed = False
    for group in settings.get("schedule_groups", []):
        if "active" in group:
            group["enabled"] = bool(group.pop("active"))
            schedule_groups_changed = True
        if "enabled" not in group:
            group["enabled"] = True
            schedule_groups_changed = True
        if "days" not in group:
            group["days"] = []
            schedule_groups_changed = True
    if schedule_groups_changed:
        _write(settings)
    return settings


def validate_article_line_template(template: str) -> str:
    """출력 줄 템플릿이 PRD 규칙을 어겼는지 검사한다 (PRD.md 기능1 규칙 5).

    {outlet}·{title} 자리표시자를 각각 정확히 1번씩 포함해야 한다 — 하나라도 없으면
    화면에서 언론사명이나 제목이 통째로 사라지고, 여러 번 있으면 같은 내용이 중복
    표시되므로 둘 다 막는다.
    """
    template = template.strip()
    if not template:
        raise SettingsError("출력 형식은 비워둘 수 없습니다.")
    for token in ("{outlet}", "{title}"):
        count = template.count(token)
        if count != 1:
            raise SettingsError(f'"{token}"를 정확히 1번 포함해야 합니다 (현재 {count}번).')
    return template


def validate_subheading_format_template(template: str) -> str:
    """소제목 형식 템플릿(메일머지 두 번째 항목)이 규칙을 어겼는지 검사한다.

    {section} 자리표시자를 정확히 1번 포함해야 한다 — validate_article_line_template과
    같은 이유(하나도 없으면 소제목 이름이 통째로 사라지고, 여러 번 있으면 중복 표시).
    """
    template = template.strip()
    if not template:
        raise SettingsError("소제목 형식은 비워둘 수 없습니다.")
    count = template.count("{section}")
    if count != 1:
        raise SettingsError(f'"{{section}}"를 정확히 1번 포함해야 합니다 (현재 {count}번).')
    return template


def validate_keyword_groups(groups: list) -> list:
    """키워드 그룹 목록이 PRD 규칙(키워드 그룹 기능)에 맞는지 검사해, 정리된 목록을 돌려준다.

    - 그룹명·키워드는 앞뒤 공백을 지우고, 빈 키워드는 버린다.
    - 그룹명·키워드가 전부 비어 있으면 그 그룹은 그냥 무시한다(칸을 지운 것으로 간주).
    - 이름이 있는데 키워드가 하나도 없거나, 반대로 키워드는 있는데 이름이 없으면 에러.
    - 그룹당 키워드는 MAX_KEYWORDS_PER_GROUP개까지, 그룹은 MAX_KEYWORD_GROUPS개까지.
    - mode는 "OR" 또는 "AND"만 허용하며, 키워드가 1개뿐이면 강제로 "OR"로 맞춘다.
    - [수정: 2026-07-25] "기관 정보" 그룹도 다른 그룹과 완전히 동등하게 여기서 다룬다 —
      더는 특별 취급하지 않는다(이름·키워드 수정, 그룹 자체 삭제까지 전부 허용). 대신
      그룹이 하나도 안 남으면 에러로 막는다 — 전부 지우면 수집이 조용히 매번 0건이
      되어버리는데, 그게 의도적인 설정 변경인지 실수인지 이 화면만으로는 구분할 수
      없기 때문이다(비워두고 싶으면 그룹을 만들고 검색이 거의 안 걸릴 키워드를 넣는 등
      다른 방법을 쓰는 게 낫다).
    - [수정: 2026-09-15] 목적지 스위치 두 개 — include_in_live(화면의 「실시간」)와
      include_in_scrap(화면의 「초안·확정본」)은 서로 독립이다. 예전엔 enabled(그룹 전체)
      + include_in_scrap(그중 정기) 계층이라 "실시간 ⊇ 정기"였고 「꺼짐 + 정기 체크」라는
      뜻 없는 조합이 가능했다. 지금은 네 조합이 전부 뜻이 있고, 「초안·확정본만」(실시간을
      뒤덮는 넓은 검색어를 보고서에만 남기기)이 새로 생겼다. 둘 다 끄면 그룹 꺼짐이다 —
      꺼도 disabled_keywords는 그대로 보존된다.
      초안·확정본을 켠 그룹은 최소 1개 있어야 한다 — 다 꺼두면 회차가 매번 조용히 0건이
      되는데, 의도적인 설정인지 실수인지 이 화면만으로는 구분할 수 없다. 실시간은 0개여도
      막지 않는다(실시간 화면이 비는 것뿐이다).
      enabled는 이제 live or scrap을 저장만 해 두는 파생값이다(옛 코드 호환).
    - [추가: 2026-07-25] disabled_keywords — 체크 해제(꺼짐)된 키워드 목록. 지워진 게
      아니라 검색에서만 빠진 상태라 keywords에는 그대로 남아있다. keywords에 없는
      단어가 섞여 있으면(예: 꺼둔 채로 그 칸을 del로 지운 경우) 조용히 걸러낸다.
    - [추가: 2026-08-19] 같은 그룹 안 중복 키워드는 에러 없이 조용히 하나로 합친다
      (처음 나온 자리를 유지). app.naver_api가 이미 키워드 단위(search_articles_by_
      groups의 seen_keywords)·URL 단위(match_articles_to_groups의 seen_urls) 양쪽에서
      중복을 걷어내므로 검색 결과·API 호출 어디에도 영향이 없는 순수한 화면 실수라,
      에러로 막으면 오히려 위험하다 — 한 번 중복이 저장된 채로 남으면 그걸 지우려는
      다음 저장 시도까지 이 화면에서 막혀버린다("잠긴 화면을 그 화면에서만 풀 수 있는"
      상태). 반면 그룹을 가로지르는 중복은 의도적일 수 있어(그룹마다 OR/AND가 달라
      같은 단어가 한쪽엔 단독으로, 다른 쪽엔 다른 단어와 AND로 묶일 수 있다) 그대로 둔다
      — 화면(app.settings_server)이 노란 테두리로 알려만 줄 뿐 여기서 막지 않는다.
    """
    cleaned = []
    for group in groups:
        name = (group.get("name") or "").strip()
        keywords = [k.strip() for k in group.get("keywords", []) if k.strip()]
        seen_kw = set()
        deduped_keywords = []
        for k in keywords:
            if k not in seen_kw:
                seen_kw.add(k)
                deduped_keywords.append(k)
        keywords = deduped_keywords
        mode = group.get("mode", "OR")
        include_in_live = group_in_live(group)
        include_in_scrap = group_in_scrap(group)
        disabled_keywords = [k for k in group.get("disabled_keywords", []) if k in keywords]
        if not name and not keywords:
            continue
        if not name:
            raise SettingsError("그룹명을 입력해야 합니다.")
        if not keywords:
            raise SettingsError(f'"{name}" 그룹에는 키워드를 최소 1개 입력해야 합니다.')
        if len(keywords) > MAX_KEYWORDS_PER_GROUP:
            raise SettingsError(f'"{name}" 그룹의 키워드는 최대 {MAX_KEYWORDS_PER_GROUP}개까지 가능합니다.')
        if mode not in VALID_MODES:
            raise SettingsError(f"검색 방식은 OR 또는 AND만 가능합니다: {mode!r}")
        cleaned.append(
            {
                "name": name,
                "keywords": keywords,
                "mode": mode if len(keywords) >= 2 else "OR",
                "include_in_live": include_in_live,
                "include_in_scrap": include_in_scrap,
                "disabled_keywords": disabled_keywords,
                "enabled": include_in_live or include_in_scrap,
            }
        )

    if not cleaned:
        raise SettingsError("키워드 그룹은 최소 1개는 있어야 합니다.")
    if len(cleaned) > MAX_KEYWORD_GROUPS:
        raise SettingsError(f"키워드 그룹은 최대 {MAX_KEYWORD_GROUPS}개까지 가능합니다 (현재 {len(cleaned)}개).")
    # 화면 JS blockingReason과 같은 문구여야 한다(app.settings_server _KEYWORD_GROUPS_SCRIPT)
    if not any(g["include_in_scrap"] for g in cleaned):
        raise SettingsError('"초안·확정본"을 켠 그룹이 최소 1개는 있어야 합니다.')
    return cleaned


def save_keyword_groups(groups: list) -> dict:
    """검증 후 키워드 그룹 목록을 저장한다. 기존에 저장돼 있던 outlet_order 등 다른
    설정은 그대로 보존한다 (통째로 덮어써서 지워버리지 않도록)."""
    cleaned = validate_keyword_groups(groups)
    settings = load_settings()
    settings["keyword_groups"] = cleaned
    _write(settings)
    return settings


def all_search_keywords(settings: dict) -> list:
    """예정된 회차 스크랩(정식 수집)에 실제로 쓰이는 모든 키워드를 순서 유지한 채 평평하게
    합친다. 소제목 분류 제외어, 워드클라우드 색 구분처럼 "저장된 회차
    데이터가 어떤 검색어로 모였는지"가 필요한 곳에서 쓴다 — 저장된 회차는 항상
    include_in_scrap 그룹으로만 모이므로(app.scraper.collect_run), 이 함수도 그
    그룹들만 본다. 실시간 기사 현황(전체 그룹 OR)은 이 함수를 쓰지 않고
    app.naver_api.search_articles_by_groups에 settings["keyword_groups"] 전체를
    직접 넘긴다.
    """
    flat = []
    for group in settings.get("keyword_groups", []):
        if not group_in_scrap(group):
            continue
        flat.extend(group["keywords"])
    seen = set()
    result = []
    for keyword in flat:
        if keyword not in seen:
            seen.add(keyword)
            result.append(keyword)
    return result


def group_in_live(group: dict) -> bool:
    """이 그룹을 실시간 현황에서 검색하나(화면의 「실시간」 스위치).

    [추가: 2026-09-15] include_in_live가 없는 dict(목적지 스위치 이전 형식 — load_settings가
    옮기기 전 값, 옛 테스트 입력)는 옛 규칙으로 읽는다: 그룹이 켜져 있으면 실시간."""
    if "include_in_live" in group:
        return bool(group["include_in_live"])
    return bool(group.get("enabled", True))


def group_in_scrap(group: dict) -> bool:
    """이 그룹을 초안·확정본(정기 회차)에서 검색하나(화면의 「초안·확정본」 스위치).

    옛 형식은 "켜져 있으면서 정기 체크"다 — 꺼진 그룹의 정기 체크는 원래 아무 데서도
    검색되지 않았으므로 그대로 옮기면 담당자가 꺼 둔 그룹이 정기에 들어간다."""
    if "include_in_live" in group:
        return bool(group.get("include_in_scrap"))
    return bool(group.get("include_in_scrap")) and bool(group.get("enabled", True))


def group_in_use(group: dict) -> bool:
    """어느 쪽에든 쓰이나 — 둘 다 끄면 그룹 꺼짐이다."""
    return group_in_live(group) or group_in_scrap(group)


def active_search_groups(settings: dict, groups: Optional[list] = None) -> list:
    """실제 검색에 넘길 그룹 목록 — 각 그룹의 keywords에서 disabled_keywords(체크
    해제해 꺼둔 단어)를 뺀 상태로 돌려준다.

    [수정: 2026-09-15] groups를 생략하면 **「실시간」을 켠 그룹**만 쓴다(실시간 기사 현황,
    app.live_renderer) — 예전엔 켜진 그룹 전부였다. 초안·확정본(app.scraper.collect_run,
    app.preview_renderer)은 먼저 group_in_scrap으로 추린 그룹을 groups로 넘기고, 이
    함수는 꺼둔 키워드만 마저 뺀다.

    groups를 넘기면 둘 다 끈(어디에도 안 쓰는) 그룹만 뺀다 — [단독]·[속보] 감시처럼
    "쓰이는 그룹 전부"가 필요한 호출부가 그 뜻으로 부른다.

    한 그룹의 키워드가 전부 꺼져 있으면 그 그룹은 빈 keywords로 넘어가는데,
    app.naver_api.search_articles_by_groups는 빈 목록을 그냥 "이 그룹은 기여하는
    기사 없음"으로 처리하므로 오류 없이 안전하다.
    """
    if groups is not None:
        source = groups
    else:
        source = [g for g in settings.get("keyword_groups", []) if group_in_live(g)]
    result = []
    for group in source:
        if not group_in_use(group):
            continue
        disabled = set(group.get("disabled_keywords", []))
        active_keywords = [k for k in group["keywords"] if k not in disabled]
        result.append({**group, "keywords": active_keywords})
    return result


def save_outlet_selection(selected: list) -> dict:
    """설정 화면 체크박스에서 고른 언론사 집합을 저장한다 (PRD.md 기능1 규칙 16).

    이미 순서가 잡혀 있던 언론사는 그 순서를 그대로 유지하고, 새로 체크된 언론사만
    맨 뒤에 이어 붙인다. 체크 해제된 언론사는 순서 목록에서 빠진다. 알 수 없는
    언론사명이 섞여 있으면 에러를 낸다 (화면 체크박스 값은 항상 알려진 이름이어야 정상).
    """
    unknown = [name for name in selected if name not in ALL_OUTLET_NAMES]
    if unknown:
        raise SettingsError(f"알 수 없는 언론사입니다: {', '.join(unknown)}")

    settings = load_settings()
    current_order = settings.get("outlet_order", [])
    selected_set = set(selected)

    kept = [name for name in current_order if name in selected_set]
    newly_added = [name for name in selected if name not in current_order]
    settings["outlet_order"] = kept + newly_added
    _write(settings)
    return settings


def save_highlight_keywords(items: list) -> dict:
    """형광펜 단어(+색상) 목록을 저장한다 (PRD.md 기능1 규칙 6).

    [수정: 2026-07-25] 직접 입력은 없앴다 — 검색 키워드 화면의 🖍️ 버튼(add_highlight_keyword/
    toggle_highlight_keyword)으로만 추가되고, 이 화면은 색 변경·제거만 한다. items는
    [{"word": ..., "color": 팔레트 인덱스}, ...] 형태(화면에서 del로 지운 항목은 이미
    빠진 채로 넘어온다). 검색 키워드(save_keyword_groups)와는 완전히 별개 필드다.
    0개~MAX_HIGHLIGHT_KEYWORDS개까지 허용(최소 개수 제한 없음 — 하나도 없으면 하이라이트를
    아예 안 하는 것도 유효한 선택). 같은 단어가 중복되면(예: 화면 새로고침 타이밍) 첫
    항목만 남긴다. color가 범위를 벗어나면(예: 손상된 값) 0으로 되돌린다.
    """
    cleaned = []
    seen = set()
    for item in items:
        word = (item.get("word") or "").strip()
        if not word or word.lower() in seen:
            continue
        seen.add(word.lower())
        try:
            color = int(item.get("color", 0))
        except (TypeError, ValueError):
            color = 0
        if not (0 <= color < len(HIGHLIGHT_COLORS)):
            color = 0
        cleaned.append({"word": word, "color": color})

    if len(cleaned) > MAX_HIGHLIGHT_KEYWORDS:
        raise SettingsError(f"형광펜 단어는 최대 {MAX_HIGHLIGHT_KEYWORDS}개까지 가능합니다 (현재 {len(cleaned)}개).")
    settings = load_settings()
    settings["highlight_keywords"] = cleaned
    _write(settings)
    return settings


def toggle_highlight_keyword(word: str) -> dict:
    """검색 키워드 화면의 🖍️ 버튼 — 이미 형광펜에 있으면 빼고, 없으면 다음 순번 색으로
    추가한다. 다음 색은 지금까지 등록된 개수를 팔레트 크기로 나눈 나머지로 정한다
    (등록 순서상 "다음 차례" 색 — 5개를 넘으면 자연히 앞 색부터 다시 돈다).
    """
    word = word.strip()
    settings = load_settings()
    items = settings.get("highlight_keywords", [])
    existing_index = next((i for i, item in enumerate(items) if item["word"].lower() == word.lower()), None)

    if existing_index is not None:
        items = items[:existing_index] + items[existing_index + 1 :]
    else:
        if len(items) >= MAX_HIGHLIGHT_KEYWORDS:
            raise SettingsError(f"형광펜 단어는 최대 {MAX_HIGHLIGHT_KEYWORDS}개까지 가능합니다.")
        items = items + [{"word": word, "color": len(items) % len(HIGHLIGHT_COLORS)}]

    settings["highlight_keywords"] = items
    _write(settings)
    return settings


def cycle_highlight_color(word: str) -> str:
    """[추가: 2026-08-04] 본문에서 형광펜 단어를 직접 클릭하면 그 단어의 색을 팔레트
    안에서 다음 색으로 바꾼다 — 색 하나가 그 단어에 전역으로 묶여 있으므로(제목·요약,
    모든 기사에 공통), 여기서 바뀐 색은 등장하는 모든 자리에 똑같이 반영돼야 한다
    (호출하는 쪽이 반환값으로 받은 색을 data-word가 같은 모든 요소에 즉시 적용).
    등록되지 않은 단어면 아무것도 하지 않고 빈 문자열을 돌려준다(호출하는 쪽에서
    실패로 처리).
    """
    settings = load_settings()
    items = settings.get("highlight_keywords", [])
    index = next((i for i, item in enumerate(items) if item["word"].lower() == word.lower()), None)
    if index is None:
        return ""
    next_color = (items[index].get("color", 0) + 1) % len(HIGHLIGHT_COLORS)
    items[index]["color"] = next_color
    settings["highlight_keywords"] = items
    _write(settings)
    return HIGHLIGHT_COLORS[next_color]


def add_highlight_keyword(word: str) -> dict:
    """형광펜 화면의 "+ 추가" 입력창 — 검색 키워드에 없는 단어도 형광펜에 바로 등록할 수
    있게 한다. [추가: 2026-07-26] toggle_highlight_keyword와 달리 이미 있으면 조용히
    빼는 게 아니라 에러를 낸다 — 사용자가 직접 텍스트를 입력해 "추가"를 눌렀는데 아무
    반응이 없으면 눌렸는지조차 알 수 없어 혼란스럽다는 판단(검색 키워드의 🖍️로 이미
    들어와 있던 단어를 몰라서 또 입력한 경우도 포함). 색은 toggle과 같은 규칙(다음
    순번 색)으로 정한다.
    """
    word = word.strip()
    if not word:
        raise SettingsError("추가할 단어를 입력해주세요.")
    settings = load_settings()
    items = settings.get("highlight_keywords", [])
    if any(item["word"].lower() == word.lower() for item in items):
        raise SettingsError("이미 등록된 형광펜 단어입니다.")
    if len(items) >= MAX_HIGHLIGHT_KEYWORDS:
        raise SettingsError(f"형광펜 단어는 최대 {MAX_HIGHLIGHT_KEYWORDS}개까지 가능합니다.")

    settings["highlight_keywords"] = items + [{"word": word, "color": len(items) % len(HIGHLIGHT_COLORS)}]
    _write(settings)
    return settings


def save_wordcloud_exclude_words(words: list) -> dict:
    """진입 화면 워드클라우드에서 빼고 싶은 단어를 저장한다.

    검색 키워드·형광펜 단어와는 완전히 별개 필드다. 0개~MAX_WORDCLOUD_EXCLUDE_WORDS개까지
    허용하며(최소 개수 제한 없음), 빈 문자열은 버리고 앞뒤 공백은 지운다. AND/OR 같은
    검색 방식 개념은 없다 — 그냥 단어 목록일 뿐이다.
    """
    cleaned = [w.strip() for w in words if w.strip()]
    if len(cleaned) > MAX_WORDCLOUD_EXCLUDE_WORDS:
        raise SettingsError(
            f"워드클라우드 제외어는 최대 {MAX_WORDCLOUD_EXCLUDE_WORDS}개까지 가능합니다 (현재 {len(cleaned)}개)."
        )
    settings = load_settings()
    settings["wordcloud_exclude_words"] = cleaned
    _write(settings)
    return settings


def save_scrap_page_settings(exclude_photo_in_scrap: bool, exclude_personnel_in_scrap: bool) -> dict:
    """자동선별(정기 회차 수집) 시 [포토]/[인사] 기사를 제외할지 여부를 저장한다."""
    settings = load_settings()
    settings["exclude_photo_in_scrap"] = bool(exclude_photo_in_scrap)
    settings["exclude_personnel_in_scrap"] = bool(exclude_personnel_in_scrap)
    _write(settings)
    return settings


def save_auto_send_settings(enabled: bool, grace_min) -> dict:
    """자동 발송 사용 여부와 유예 시간(분)을 저장한다 (설정 화면 /auto-send).

    유예 상한이 MAX_AUTO_SEND_GRACE_MIN인 이유는 app.config의 주석 참고 —
    회차 간격보다 길면 이전 회차가 조용히 안 나가고 묻힌다.
    """
    try:
        minutes = int(str(grace_min).strip())
    except (TypeError, ValueError):
        raise SettingsError("자동 발송 시간은 숫자로 입력해주세요.")
    if not 1 <= minutes <= MAX_AUTO_SEND_GRACE_MIN:
        raise SettingsError(f"자동 발송 시간은 1~{MAX_AUTO_SEND_GRACE_MIN}분 사이로 입력해주세요.")
    settings = load_settings()
    settings["auto_send_enabled"] = bool(enabled)
    settings["auto_send_grace_min"] = minutes
    _write(settings)
    return settings


def auto_send_grace_sec(settings: Optional[dict] = None) -> int:
    """자동 발송 유예 시간을 초로 — app.confirm_send와 app.renderer(카운트다운)가 공유한다."""
    settings = settings if settings is not None else load_settings()
    return int(settings.get("auto_send_grace_min", DEFAULT_AUTO_SEND_GRACE_MIN)) * 60


def is_auto_send_enabled(settings: Optional[dict] = None) -> bool:
    settings = settings if settings is not None else load_settings()
    return bool(settings.get("auto_send_enabled", DEFAULT_AUTO_SEND_ENABLED))


def save_article_line_template(template: str) -> dict:
    """기사 출력 줄 형식을 저장한다 (PRD.md 기능1 규칙 5). 검증은 validate_article_line_template 참고."""
    cleaned = validate_article_line_template(template)
    settings = load_settings()
    settings["article_line_template"] = cleaned
    _write(settings)
    return settings


def save_subheading_format_template(template: str) -> dict:
    """소제목 형식을 저장한다(메일머지 두 번째 항목). 검증은 validate_subheading_format_template 참고."""
    cleaned = validate_subheading_format_template(template)
    settings = load_settings()
    settings["subheading_format_template"] = cleaned
    _write(settings)
    return settings


def validate_schedule_windows(windows: list) -> list:
    """회차별 시작~종료 시간창 목록이 PRD 규칙을 어겼는지 검사해, 정리된(종료 시각
    오름차순) 목록을 돌려준다 (PRD.md 기능1 규칙 2·20 — 회차별 시간창 수집).

    - {"start": "HH:MM", "end": "HH:MM", "enabled": bool} 형태를 받는다. 둘 다 빈 칸이면
      그 자리는 삭제.
    - 하나만 채워져 있으면 불완전한 값이라 에러.
    - 각 값은 "HH:MM" 24시간제 형식이어야 한다.
    - [추가: 2026-08-12] 분(MM)은 00 또는 30만 허용한다(30분 단위). 자동발송 유예 시간
      (설정 /auto-send, 최대 MAX_AUTO_SEND_GRACE_MIN=30분)과 겹치지 않으려면 회차 사이
      최소 간격이 30분은 되어야 하는데, 분을 자유롭게 입력하면 09:00~09:10처럼 그보다
      짧은 창도 만들 수 있었다(사용자 요청). 두 시각이 다 30분 단위면 시작~종료 차이는
      항상 30분 이상이 된다. 설정 화면의 분 입력도 00/30 두 값만 고를 수 있는 <select>로
      바꿔뒀지만(app/settings_server.py `_minute_select_options`), 폼 데이터는 조작될 수
      있으므로 여기서도 다시 검사한다.
    - 종료는 시작보다 늦어야 한다 (당일 안에서만 — 자정을 넘기는 창은 지원하지 않음).
    - 종료 시각은 화면 헤더("언론 모니터링 [종료시각] 기준")·저장 파일명으로도 쓰이므로
      중복될 수 없다.
    - 개수는 MIN_SCHEDULE_TIMES~MAX_SCHEDULE_TIMES개여야 한다 (규칙20) — 꺼둔(enabled=False)
      시간대도 칸 자체는 유지되는 것이므로 이 개수에 포함된다.
    - [추가: 2026-07-29] enabled — 개별 시간대 on/off. 평일/휴일처럼 상황에 따라 켜고 끌
      시간대를 지우지 않고 남겨둘 수 있다(다시 켤 때 시작~종료를 재입력할 필요가 없음).
      생략하면 켜진 것으로 간주한다. 최소 하나는 켜져 있어야 한다 — 전부 꺼두면 스케줄러가
      매번 아무것도 실행하지 않는데(조용한 0건), 의도한 설정 변경인지 실수인지 이 화면만으로는
      구분할 수 없기 때문이다(키워드 그룹의 "최소 1개는 include_in_scrap" 규칙과 같은 이유).
    """
    cleaned = []
    for w in windows:
        start = (w.get("start") or "").strip()
        end = (w.get("end") or "").strip()
        if not start and not end:
            continue
        if not start or not end:
            raise SettingsError(f'시작·종료 시각을 모두 입력해야 합니다 (시작 "{start}", 종료 "{end}").')
        if not _TIME_PATTERN.match(start):
            raise SettingsError(f'시작 시각 형식이 올바르지 않습니다: "{start}" (예: 09:00)')
        if not _TIME_PATTERN.match(end):
            raise SettingsError(f'종료 시각 형식이 올바르지 않습니다: "{end}" (예: 09:00)')
        if start[3:] not in ("00", "30"):
            raise SettingsError(f'시작 시각({start})은 30분 단위(예: 09:00, 09:30)로만 지정할 수 있습니다.')
        if end[3:] not in ("00", "30"):
            raise SettingsError(f'종료 시각({end})은 30분 단위(예: 09:00, 09:30)로만 지정할 수 있습니다.')
        if end <= start:
            raise SettingsError(f'종료 시각({end})은 시작 시각({start})보다 늦어야 합니다.')
        cleaned.append({"start": start, "end": end, "enabled": w.get("enabled", True)})

    if not (MIN_SCHEDULE_TIMES <= len(cleaned) <= MAX_SCHEDULE_TIMES):
        raise SettingsError(f"수집 시간대는 {MIN_SCHEDULE_TIMES}~{MAX_SCHEDULE_TIMES}개여야 합니다 (현재 {len(cleaned)}개).")

    cleaned.sort(key=lambda w: w["end"])
    ends = [w["end"] for w in cleaned]
    if len(set(ends)) != len(ends):
        raise SettingsError("종료 시각이 중복된 시간대가 있습니다.")
    if not any(w["enabled"] for w in cleaned):
        raise SettingsError("적어도 하나의 시간대는 켜져 있어야 합니다.")
    return cleaned


def validate_schedule_groups(groups: list) -> list:
    """스크랩 시간대 "그룹" 목록을 검사해 정리된 목록을 돌려준다.

    [추가: 2026-07-29, 수정: 2026-08-10] 주중/주말처럼 상황별로 시간대 세트를 여러 개
    만들어두는 기능 — 처음엔 라디오 버튼으로 정확히 1개만 "활성"이었지만, 공휴일처럼
    평일인데 주말 스케줄을 써야 하는 경우를 다루기 위해 그룹마다 독립적인 "enabled"
    on/off와 "days"(요일) 체크로 바뀌었다. 실제로 어느 그룹이 지금 적용되는지는
    app.settings.pick_active_group_index/active_schedule_times가 판단한다:
    enabled 그룹이 1개뿐이면 요일과 무관하게 그 그룹을 쓰고(= 연휴 등 수동 오버라이드),
    2개 이상이면 그 중 오늘 요일이 체크된 그룹을 쓴다.

    - 그룹 하나는 {"name": str, "times": [...], "enabled": bool, "days": [...]} 형태.
      times는 그룹 안에서 validate_schedule_windows와 같은 규칙(개별 on/off 포함)을
      그대로 적용한다. days는 WEEKDAY_KEYS(mon~sun) 중에서만 허용하고, 모르는 값은
      조용히 무시한다.
    - 이름도 시간대도 없는 빈 그룹은 무시한다(칸을 지운 것으로 간주) — 이름만 있고
      시간대가 없으면(또는 그 반대) validate_schedule_windows가 알아서 에러를 낸다.
    - 그룹은 최소 1개, 최대 MAX_SCHEDULE_GROUPS개, 그 중 최소 1개는 enabled여야 한다
      (전부 꺼두면 아무 회차도 실행되지 않으므로).
    - enabled인 그룹끼리 같은 요일을 동시에 체크할 수 없다 — 어느 그룹을 적용해야
      할지 애매해지기 때문에 저장 시점에 막는다("요일이 겹칩니다" 에러).
    """
    cleaned = []
    for group in groups:
        name = (group.get("name") or "").strip()
        raw_times = group.get("times", [])
        has_any_time_value = any(
            (w.get("start") or "").strip() or (w.get("end") or "").strip() for w in raw_times
        )
        if not name and not has_any_time_value:
            continue
        if not name:
            raise SettingsError("시간대 그룹의 이름을 입력해야 합니다.")
        times = validate_schedule_windows(raw_times)
        days = [d for d in (group.get("days") or []) if d in WEEKDAY_KEYS]
        cleaned.append({"name": name, "times": times, "enabled": bool(group.get("enabled")), "days": days})

    if not cleaned:
        raise SettingsError("스크랩 시간대 그룹은 최소 1개 있어야 합니다.")
    if len(cleaned) > MAX_SCHEDULE_GROUPS:
        raise SettingsError(f"스크랩 시간대 그룹은 최대 {MAX_SCHEDULE_GROUPS}개까지 가능합니다.")
    if not any(g["enabled"] for g in cleaned):
        raise SettingsError("적어도 하나의 그룹은 사용 중이어야 합니다.")

    enabled_groups = [g for g in cleaned if g["enabled"]]
    for day in WEEKDAY_KEYS:
        claiming = [g["name"] for g in enabled_groups if day in g["days"]]
        if len(claiming) > 1:
            names = ", ".join(f'"{n}"' for n in claiming)
            raise SettingsError(f"요일이 겹칩니다: {names} 그룹이 모두 '{WEEKDAY_LABELS_KO[day]}'에 체크되어 있습니다.")
    return cleaned


def save_schedule_groups(groups: list) -> dict:
    """검증 후 스크랩 시간대 그룹 목록을 저장한다."""
    cleaned = validate_schedule_groups(groups)
    settings = load_settings()
    settings["schedule_groups"] = cleaned
    _write(settings)
    return settings


def pick_active_group_index(groups: list, now: Optional[datetime] = None) -> Optional[int]:
    """지금 이 순간 "적용해야 할" 그룹의 인덱스를 돌려준다. 그룹이 하나도 없으면 None.

    [추가: 2026-08-10] enabled 그룹이 정확히 1개면 요일과 무관하게 그 그룹을 쓴다 —
    공휴일 등 특별한 날에 다른 그룹을 전부 꺼두고 이 그룹 하나만 켜두면, 요일 체크를
    따로 신경 쓸 필요 없이 그 그룹이 그대로 적용된다(수동 오버라이드). enabled 그룹이
    2개 이상이면 그 중 오늘 요일이 체크된 첫 번째 그룹을 쓰고, 아무도 오늘 요일을
    체크하지 않은 경우(설정 공백)는 안전하게 첫 번째 enabled 그룹으로 넘어간다.
    enabled 그룹이 하나도 없으면(정상적으로는 validate_schedule_groups가 막지만,
    저장 파일을 직접 건드린 경우 등 방어적으로) 그룹 0번을 쓴다.

    설정 화면(app.settings_server)이 "오늘 적용 중" 배지를 그리는 데도 이 함수를
    그대로 쓴다 — 저장 전 화면에 떠 있는(아직 settings.json에 안 쓰인) 그룹 목록에도
    적용해야 하므로, dict 전체가 아니라 groups 리스트 자체를 받는다.
    """
    if not groups:
        return None
    now = now or datetime.now()
    today_key = WEEKDAY_KEYS[now.weekday()]
    enabled_indices = [i for i, g in enumerate(groups) if g.get("enabled", True)]
    if not enabled_indices:
        return 0
    if len(enabled_indices) == 1:
        return enabled_indices[0]
    for i in enabled_indices:
        if today_key in (groups[i].get("days") or []):
            return i
    return enabled_indices[0]


def active_schedule_times(settings: dict, now: Optional[datetime] = None) -> list:
    """지금 적용 중인 시간대 그룹의 시간대 목록을 돌려준다.

    app.scheduler가 매 tick마다 이걸 거쳐 "지금 어떤 시간대를 따라야 하는지" 얻는다 —
    그룹의 enabled/days를 바꾸면(저장 즉시) 다음 tick부터 바로 반영된다(규칙20과 같은
    패턴). 어느 그룹이 "지금 적용 중"인지의 실제 판단은 pick_active_group_index가 한다.
    """
    groups = settings.get("schedule_groups", [])
    index = pick_active_group_index(groups, now)
    return groups[index]["times"] if index is not None else []


def move_outlet(name: str, direction: str) -> dict:
    """선택된 언론사 순서에서 name을 옮긴다 (PRD.md 기능1 규칙 16, 버튼 방식).

    up/down은 한 칸씩, jumpup/jumpdown은 OUTLET_ORDER_JUMP_STEP(5)칸씩 한 번에 옮긴다.
    [수정: 2026-07-26] 한 번은 UI가 번잡하다는 이유로 순간이동 버튼을 없앴었지만,
    선택 언론사가 20개가 넘으면 한 칸씩 옮기는 게 여전히 비효율적이라는 실사용
    피드백으로 다시 추가했다(당시엔 "맨 위"/"맨 아래"로 한 번에 끝까지).
    [수정: 2026-08-13] "맨 위"/"맨 아래"를 없애고 "5개 위로"/"5개 아래로"로 바꿨다 —
    끝까지 순간이동하는 버튼은 ↑/↓와 아예 다른 색으로 표시해야 했는데(실수로 누르면
    되돌리기 번거로움), N칸 이동은 ↑/↓와 "같은 종류의 동작, 배수만 다른 것"이라 위계를
    나눌 필요 자체가 없어진다(사용자 판단). 목표 지점까지 남은 칸이 5보다 적으면
    끝까지만 옮긴다 — 그 경계에서는 이 버튼이 예전 "맨 위"/"맨 아래" 역할을 자연히
    겸하므로 별도 버튼이 필요 없다.
    맨 위에서 "위로"(jumpup 포함), 맨 아래에서 "아래로"(jumpdown 포함)를 누르면 조용히
    아무 일도 하지 않는다(버튼이 원래 그 위치에서 비활성이어야 자연스럽지만, 정적 HTML
    폼이라 서버에서도 한 번 더 방어한다).
    """
    if direction not in VALID_DIRECTIONS:
        raise SettingsError(f"방향은 {', '.join(VALID_DIRECTIONS)} 중 하나여야 합니다: {direction!r}")

    settings = load_settings()
    order = settings.get("outlet_order", [])
    if name not in order:
        raise SettingsError(f"선택되지 않은 언론사입니다: {name!r}")

    idx = order.index(name)
    last = len(order) - 1
    if direction in ("up", "down"):
        target = idx - 1 if direction == "up" else idx + 1
    elif direction == "jumpup":
        target = max(0, idx - OUTLET_ORDER_JUMP_STEP)
    else:
        target = min(last, idx + OUTLET_ORDER_JUMP_STEP)

    if 0 <= target <= last and target != idx:
        order.pop(idx)
        order.insert(target, name)
        settings["outlet_order"] = order
        _write(settings)
    return settings
