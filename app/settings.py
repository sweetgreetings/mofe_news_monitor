# Design Ref: PRD.md 기능1 규칙 6·15·16·20 — 검색 키워드/언론사 선택/형광펜 단어/수집 시각, 코드 수정 없이 화면에서 변경
import json
import re
from typing import Optional

from app.atomic_write import atomic_write_text
from app.config import (
    BASE_GROUP_KEYWORDS,
    BASE_GROUP_NAME,
    DEFAULT_ARTICLE_LINE_TEMPLATE,
    DEFAULT_HIGHLIGHT_KEYWORDS,
    DEFAULT_INCLUDE_PERSONNEL_IN_SCRAP,
    DEFAULT_INCLUDE_PHOTO_IN_SCRAP,
    DEFAULT_KEYWORD_GROUP_NAME,
    DEFAULT_SCHEDULE_GROUP_NAME,
    DEFAULT_TELEGRAM_AUTO_SEND,
    HIGHLIGHT_COLORS,
    MAX_HIGHLIGHT_KEYWORDS,
    MAX_KEYWORD_GROUPS,
    MAX_KEYWORDS_PER_GROUP,
    MAX_SCHEDULE_GROUPS,
    MAX_SCHEDULE_TIMES,
    MAX_WORDCLOUD_EXCLUDE_WORDS,
    MIN_SCHEDULE_TIMES,
    SCHEDULE_TIMES,
    SETTINGS_FILE,
)
from app.naver_api import ALL_OUTLET_NAMES

VALID_MODES = ("OR", "AND")
VALID_DIRECTIONS = ("up", "down", "top", "bottom")
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
        "include_in_scrap": True,
        "disabled_keywords": [],
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
        "schedule_groups": [
            {
                "name": DEFAULT_SCHEDULE_GROUP_NAME,
                "times": [{**dict(w), "enabled": True} for w in SCHEDULE_TIMES],
                "active": True,
            }
        ],
        "wordcloud_exclude_words": [],
        "include_photo_in_scrap": DEFAULT_INCLUDE_PHOTO_IN_SCRAP,
        "include_personnel_in_scrap": DEFAULT_INCLUDE_PERSONNEL_IN_SCRAP,
        "telegram_auto_send": DEFAULT_TELEGRAM_AUTO_SEND,
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
    settings.setdefault("wordcloud_exclude_words", [])
    settings.setdefault("include_photo_in_scrap", DEFAULT_INCLUDE_PHOTO_IN_SCRAP)
    settings.setdefault("include_personnel_in_scrap", DEFAULT_INCLUDE_PERSONNEL_IN_SCRAP)
    settings.setdefault("telegram_auto_send", DEFAULT_TELEGRAM_AUTO_SEND)

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
            {"name": DEFAULT_SCHEDULE_GROUP_NAME, "times": raw_schedule, "active": True}
        ]
        settings.pop("schedule_times", None)
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
    - [추가: 2026-07-25] include_in_scrap — 이 그룹이 예정된 회차 스크랩에도 쓰일지.
      실시간 기사 현황은 항상 그룹 전체(그룹 간 OR)를 쓰지만, 정식 스크랩은 이 플래그가
      켜진 그룹만 검색한다(app.scraper.collect_run). 최소 1개 그룹은 켜져 있어야
      한다 — 다 꺼두면 스크랩이 매번 조용히 0건이 되어버린다.
    - [추가: 2026-07-25] disabled_keywords — 체크 해제(꺼짐)된 키워드 목록. 지워진 게
      아니라 검색에서만 빠진 상태라 keywords에는 그대로 남아있다. keywords에 없는
      단어가 섞여 있으면(예: 꺼둔 채로 그 칸을 del로 지운 경우) 조용히 걸러낸다.
    """
    cleaned = []
    for group in groups:
        name = (group.get("name") or "").strip()
        keywords = [k.strip() for k in group.get("keywords", []) if k.strip()]
        mode = group.get("mode", "OR")
        include_in_scrap = bool(group.get("include_in_scrap"))
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
                "include_in_scrap": include_in_scrap,
                "disabled_keywords": disabled_keywords,
            }
        )

    if not cleaned:
        raise SettingsError("키워드 그룹은 최소 1개는 있어야 합니다.")
    if len(cleaned) > MAX_KEYWORD_GROUPS:
        raise SettingsError(f"키워드 그룹은 최대 {MAX_KEYWORD_GROUPS}개까지 가능합니다 (현재 {len(cleaned)}개).")
    if not any(g["include_in_scrap"] for g in cleaned):
        raise SettingsError('"정기 스크랩에도 포함"으로 켜둔 그룹이 최소 1개는 있어야 합니다.')
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
    합친다. 소제목 분류·"🤖 주요 키워드" 제외, 워드클라우드 색 구분처럼 "저장된 회차
    데이터가 어떤 검색어로 모였는지"가 필요한 곳에서 쓴다 — 저장된 회차는 항상
    include_in_scrap 그룹으로만 모이므로(app.scraper.collect_run), 이 함수도 그
    그룹들만 본다. 실시간 기사 현황(전체 그룹 OR)은 이 함수를 쓰지 않고
    app.naver_api.search_articles_by_groups에 settings["keyword_groups"] 전체를
    직접 넘긴다.
    """
    flat = []
    for group in settings.get("keyword_groups", []):
        if not group.get("include_in_scrap"):
            continue
        flat.extend(group["keywords"])
    seen = set()
    result = []
    for keyword in flat:
        if keyword not in seen:
            seen.add(keyword)
            result.append(keyword)
    return result


def active_search_groups(settings: dict, groups: Optional[list] = None) -> list:
    """실제 검색에 넘길 그룹 목록 — 각 그룹의 keywords에서 disabled_keywords(체크
    해제해 꺼둔 단어)를 뺀 상태로 돌려준다.

    groups를 생략하면 settings["keyword_groups"] 전체를 쓴다(실시간 기사 현황,
    app.live_renderer). app.scraper.collect_run은 먼저 include_in_scrap이 켜진
    그룹만 추려 groups로 넘긴 뒤 이 함수로 꺼둔 키워드까지 마저 제외한다.

    한 그룹의 키워드가 전부 꺼져 있으면 그 그룹은 빈 keywords로 넘어가는데,
    app.naver_api.search_articles_by_groups는 빈 목록을 그냥 "이 그룹은 기여하는
    기사 없음"으로 처리하므로 오류 없이 안전하다.
    """
    source = groups if groups is not None else settings.get("keyword_groups", [])
    result = []
    for group in source:
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


def save_scrap_page_settings(include_photo_in_scrap: bool, include_personnel_in_scrap: bool) -> dict:
    """정기 스크랩(예정된 회차)에 포토/현장 기사·인사 발령 기사를 포함할지 여부를 저장한다."""
    settings = load_settings()
    settings["include_photo_in_scrap"] = bool(include_photo_in_scrap)
    settings["include_personnel_in_scrap"] = bool(include_personnel_in_scrap)
    _write(settings)
    return settings


def save_telegram_settings(auto_send: bool) -> dict:
    """정기 회차 스크랩 완료 시 텔레그램 자동 전송 여부를 저장한다."""
    settings = load_settings()
    settings["telegram_auto_send"] = bool(auto_send)
    _write(settings)
    return settings


def save_article_line_template(template: str) -> dict:
    """기사 출력 줄 형식을 저장한다 (PRD.md 기능1 규칙 5). 검증은 validate_article_line_template 참고."""
    cleaned = validate_article_line_template(template)
    settings = load_settings()
    settings["article_line_template"] = cleaned
    _write(settings)
    return settings


def validate_schedule_windows(windows: list) -> list:
    """회차별 시작~종료 시간창 목록이 PRD 규칙을 어겼는지 검사해, 정리된(종료 시각
    오름차순) 목록을 돌려준다 (PRD.md 기능1 규칙 2·20 — 회차별 시간창 수집).

    - {"start": "HH:MM", "end": "HH:MM", "enabled": bool} 형태를 받는다. 둘 다 빈 칸이면
      그 자리는 삭제.
    - 하나만 채워져 있으면 불완전한 값이라 에러.
    - 각 값은 "HH:MM" 24시간제 형식이어야 한다.
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

    [추가: 2026-07-29] 주중/주말처럼 상황별로 시간대 세트를 여러 개 만들어두고 그 중
    정확히 1개만 "지금 적용 중"(active)으로 켜는 기능 — 스케줄러는 활성 그룹의
    시간대만 본다(app.settings.active_schedule_times, app.scheduler).

    - 그룹 하나는 {"name": str, "times": [...], "active": bool} 형태. times는 그룹
      안에서 validate_schedule_windows와 같은 규칙(개별 on/off 포함)을 그대로 적용한다.
    - 이름도 시간대도 없는 빈 그룹은 무시한다(칸을 지운 것으로 간주) — 이름만 있고
      시간대가 없으면(또는 그 반대) validate_schedule_windows가 알아서 에러를 낸다.
    - 그룹은 최소 1개, 최대 MAX_SCHEDULE_GROUPS개.
    - 활성(active) 그룹은 화면에서 라디오 버튼이라 항상 정확히 1개만 선택되지만,
      방금 그 그룹을 지웠거나(활성 표시가 아예 안 실려옴) 폼이 깨져 여러 개로 왔으면
      첫 번째 그룹을 활성으로 강제한다 — 조용히 넘어가되 절대 0개/2개로 저장되지 않는다.
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
        cleaned.append({"name": name, "times": times, "active": bool(group.get("active"))})

    if not cleaned:
        raise SettingsError("스크랩 시간대 그룹은 최소 1개 있어야 합니다.")
    if len(cleaned) > MAX_SCHEDULE_GROUPS:
        raise SettingsError(f"스크랩 시간대 그룹은 최대 {MAX_SCHEDULE_GROUPS}개까지 가능합니다.")

    if sum(1 for g in cleaned if g["active"]) != 1:
        for i, g in enumerate(cleaned):
            g["active"] = i == 0
    return cleaned


def save_schedule_groups(groups: list) -> dict:
    """검증 후 스크랩 시간대 그룹 목록을 저장한다."""
    cleaned = validate_schedule_groups(groups)
    settings = load_settings()
    settings["schedule_groups"] = cleaned
    _write(settings)
    return settings


def active_schedule_times(settings: dict) -> list:
    """지금 적용 중인(활성) 시간대 그룹의 시간대 목록을 돌려준다.

    app.scheduler가 매 tick마다 이걸 거쳐 "지금 어떤 시간대를 따라야 하는지" 얻는다 —
    활성 그룹을 바꾸면(저장 즉시) 다음 tick부터 바로 반영된다(규칙20과 같은 패턴).
    """
    groups = settings.get("schedule_groups", [])
    for group in groups:
        if group.get("active"):
            return group["times"]
    return groups[0]["times"] if groups else []


def move_outlet(name: str, direction: str) -> dict:
    """선택된 언론사 순서에서 name을 옮긴다 (PRD.md 기능1 규칙 16, 버튼 방식).

    up/down은 한 칸씩, top/bottom은 맨 위/맨 아래로 한 번에 옮긴다. [수정: 2026-07-26]
    한 번은 UI가 번잡하다는 이유로 top/bottom을 없앴었지만, 선택 언론사가 20개가
    넘으면 한 칸씩 옮기는 게 여전히 비효율적이라는 실사용 피드백으로 다시 추가했다
    (이번엔 ⇈/⇊ 아이콘 대신 "맨 위"/"맨 아래" 텍스트 버튼 — 아이콘보다 뜻이 분명함).
    맨 위에서 "위로"(또는 이미 맨 위인데 "맨 위로"), 맨 아래에서 "아래로"(또는 이미 맨
    아래인데 "맨 아래로")를 누르면 조용히 아무 일도 하지 않는다 (버튼이 원래 그 위치에서
    비활성이어야 자연스럽지만, 정적 HTML 폼이라 서버에서도 한 번 더 방어한다).
    """
    if direction not in VALID_DIRECTIONS:
        raise SettingsError(f"방향은 {', '.join(VALID_DIRECTIONS)} 중 하나여야 합니다: {direction!r}")

    settings = load_settings()
    order = settings.get("outlet_order", [])
    if name not in order:
        raise SettingsError(f"선택되지 않은 언론사입니다: {name!r}")

    idx = order.index(name)
    if direction in ("up", "down"):
        swap_with = idx - 1 if direction == "up" else idx + 1
        if 0 <= swap_with < len(order):
            order[idx], order[swap_with] = order[swap_with], order[idx]
            settings["outlet_order"] = order
            _write(settings)
    elif direction == "top" and idx != 0:
        order.pop(idx)
        order.insert(0, name)
        settings["outlet_order"] = order
        _write(settings)
    elif direction == "bottom" and idx != len(order) - 1:
        order.pop(idx)
        order.append(name)
        settings["outlet_order"] = order
        _write(settings)
    return settings
