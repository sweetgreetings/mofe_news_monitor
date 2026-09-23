# Design Ref: ADHOC_DESIGN.md §3 — 카드 하나 = 파일 하나. 정기 스크랩의 전역 큐레이션
# 파일(hidden_articles.json/group_overrides.json/...)은 여기서 한 개도 읽거나 쓰지 않는다.
#
# 의존 방향은 app/adhoc/* -> app/* 한쪽으로만 흐른다(ADHOC_DESIGN.md §4) — app/config.py의
# DATA_DIR만 재사용하고, 그 아래 새 경로(data/adhoc/...)는 이 패키지가 스스로 관리한다.
# 이렇게 해두면 app/adhoc/ + data/adhoc/ 두 폴더만 지워도 정기 스크랩 쪽엔 아무 흔적도 안 남는다.
import json
import re
import secrets
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from app.atomic_write import atomic_write_text
from app.config import DATA_DIR
from app.settings import load_settings
from app.sorter import outlet_sort_key

ADHOC_DIR = DATA_DIR / "adhoc"
CARDS_DIR = ADHOC_DIR / "cards"
TRASH_DIR = ADHOC_DIR / "trash"
ISSUES_FILE = ADHOC_DIR / "issues.json"

ADHOC_DIR.mkdir(parents=True, exist_ok=True)
CARDS_DIR.mkdir(parents=True, exist_ok=True)
TRASH_DIR.mkdir(parents=True, exist_ok=True)

# ADHOC_DESIGN.md §6.1 — 검색어 최대 5개, OR 고정. 급하게 걸었다가 0건이 나오는 게
# 최악이라는 판단(HISTORY.md "수시 모니터링" 참고).
MAX_ADHOC_KEYWORDS = 5

# 검색 방식 「모두」(검색어가 모두 있는 기사만)의 검색어 상한. 「모두」는 같은 목록을
# must_keywords에도 넣어 저장하므로(is_match_all) 이 상한이 곧 「모두」의 상한이다.
# 상한이 3인 이유: 세 단어가 다 겹쳐야 걸러질 정도면 검색어 자체를 다시 잡는 게 맞다.
MAX_ADHOC_MUST_KEYWORDS = 3

# ADHOC_DESIGN.md §6.5 — 소제목 최대 5개(정기는 8개 — 사안이 한정적이라 늘리지 않는다).
# app.adhoc.classifier(LLM 자동 배정)와 이 파일의 add_custom_group(직접 만들기) 양쪽이
# 같은 상한을 봐야 하는데, classifier.py가 이미 이 파일(card.py)을 임포트하고 있어
# 거꾸로 이 파일이 classifier.py를 임포트하면 순환 임포트가 된다 — 그래서 상수를
# 더 아래 계층인 여기 둔다.
MAX_ADHOC_SUBHEADINGS = 5


# ADHOC_DESIGN.md §6.13 — 카드의 두 종류. 모음 카드(bundle)는 "검색을 안 하는 카드"라
# keywords/must_keywords가 비어 있고 window가 None이다. kind 필드가 아예 없는 옛 카드는
# 전부 collect로 읽는다(마이그레이션 없음).
KIND_COLLECT = "collect"
KIND_BUNDLE = "bundle"


class AdhocCardError(ValueError):
    """카드 스키마 검증 실패 (app.settings.SettingsError와 같은 패턴)."""


def card_kind(card: dict) -> str:
    return card.get("kind") or KIND_COLLECT


def is_bundle(card: dict) -> bool:
    return card_kind(card) == KIND_BUNDLE


def is_raw(card: dict) -> bool:
    """[추가: 2026-09-15] 로데이터 원본인가 — 이 날 이후에 만든 원본 카드에만 `raw`가 붙는다.

    수시 4단 흐름(새 수집 → 원본 → 확정본 → 수시 보관함, ADHOC_FLOW_SPLIT_MOCKUP.html)
    에서 원본은 **거르지 않은 로데이터**다 — 실시간 현황처럼 소제목·숨기기·라벨이 없고, 판단은
    전부 확정본에서 한다. 필드가 없는 옛 원본(그 전까지 소제목·숨김까지 하던 「원본 겸
    확정본」)은 예전 화면·예전 보관함 규칙을 그대로 쓴다 — 담당자가 정리해 둔 소제목·숨김이
    그대로 남아야 해서 옛 카드를 새 규칙으로 옮겨 적지 않는다(사용자 결정: 보관함에 그대로).
    """
    return card_kind(card) == KIND_COLLECT and bool(card.get("raw"))


def window_of(card: dict) -> dict:
    """카드의 수집 시간창 — 모음 카드는 None이므로 빈 값을 돌려준다.

    시간창을 읽는 쪽이 전부 이 함수를 거치게 해서 `card["window"]["end"]`가 모음에서
    터지지 않게 한다. 없는 값을 "23:59" 같은 그럴듯한 값으로 지어내지 않는 이유:
    이 값은 엑셀 "스크랩종료시간"·라벨 `scrap_end`로 그대로 나가는 데이터 계약이라
    (PRD.md 기능11), 모음에는 수집 시간창이 없다는 사실이 빈 값으로 남아야 맞다.
    """
    return card.get("window") or {"start": "", "end": ""}



# --- 카드별 잠금 (ADHOC_DESIGN.md §6.12) ---------------------------------------
# 정기의 hide_article이 겪었던 경합(app/curation.py의 _hidden_lock 도입 배경)과 같은
# 문제가 카드 단위로도 날 수 있다 — 단, 카드끼리는 독립이라 전역 락 하나로 묶으면
# 서로 다른 카드를 만지는 요청까지 불필요하게 막힌다. 카드 id별로 락을 따로 둔다.
_card_locks: dict[str, threading.Lock] = {}
_card_locks_meta_lock = threading.Lock()


def card_lock(card_id: str) -> threading.Lock:
    """이 카드 전용 락을 돌려준다(없으면 새로 만든다) — 읽기-수정-쓰기 전체를 감싸는 용도.

    사용 예:
        with card_lock(card_id):
            c = load_card(card_id)
            ... c["articles"] 등을 고친다 ...
            save_card(c)
    """
    with _card_locks_meta_lock:
        if card_id not in _card_locks:
            _card_locks[card_id] = threading.Lock()
        return _card_locks[card_id]


# --- 경로 --------------------------------------------------------------------


def _card_path(card_id: str) -> Path:
    return CARDS_DIR / f"{card_id}.json"


def _generate_card_id(now: Optional[datetime] = None) -> str:
    now = now or datetime.now()
    return f"{now.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"


def _generate_issue_id() -> str:
    return f"issue-{secrets.token_hex(4)}"


# --- 검증 ----------------------------------------------------------------------


def _validate_keywords(keywords: list[str]) -> None:
    cleaned = [k.strip() for k in keywords if k.strip()]
    if not cleaned:
        raise AdhocCardError("검색어를 하나 이상 입력해주세요.")
    if len(cleaned) > MAX_ADHOC_KEYWORDS:
        raise AdhocCardError(f"검색어는 최대 {MAX_ADHOC_KEYWORDS}개까지 등록할 수 있습니다.")


def _validate_must_keywords(must_keywords: list[str]) -> None:
    """「모두」 모드의 검색어 — 비어 있으면 「하나라도」 모드다."""
    cleaned = [k.strip() for k in must_keywords if k.strip()]
    if len(cleaned) > MAX_ADHOC_MUST_KEYWORDS:
        raise AdhocCardError(
            f"\u2018모두 있는\u2019은 검색어 {MAX_ADHOC_MUST_KEYWORDS}개까지예요."
        )


# --- 검색 방식 -------------------------------------------------------------------
#
# 조건은 검색어 칸 하나 + 방식(하나라도 / 모두)이다 — 새 수집 화면과 같은 모양.
# 저장은 따로 필드를 두지 않고 두 목록으로 한다:
#   하나라도: keywords=[...], must_keywords=[]
#   모두:     keywords=[...], must_keywords=keywords와 같은 목록(2개 이상)
# 「모두」를 must_keywords에도 넣는 이유는 1,000건 상한 검사(§6.4b)가 must_keywords를
# 보기 때문이다 — 조건 판정(article_in_condition)도 그대로 맞아떨어진다.


def is_match_all(card: dict) -> bool:
    """검색 방식이 「모두」인가. 검색어가 1개면 두 방식이 같은 뜻이라 「하나라도」로 본다."""
    keywords = card.get("keywords") or []
    return len(keywords) >= 2 and sorted(card.get("must_keywords") or []) == sorted(keywords)


def normalize_condition(card: dict) -> None:
    """옛 두 칸 조건(검색어 OR + 꼭 포함할 검색어 AND)을 칸 하나 + 방식 모양으로 고친다.

    조건 = (검색어 중 하나라도) 그리고 (꼭 포함할 검색어 전부).
    - 꼭 포함할 검색어 중 하나라도 검색어 칸에 있으면 앞 절은 뒤 절에 이미 들어 있으므로
      조건은 「꼭 포함할 검색어 전부」와 **같은 뜻**이다 → 그 단어들로 줄인다.
    - 겹치는 게 없으면(진짜 혼합) 새 모양으로 같은 뜻을 못 만든다 → 두 칸을 합쳐 「모두」로
      읽는다. 기사가 덜 나올 수는 있어도 보고서에 조건 밖 기사가 섞이지는 않는 쪽이다.
    검색할 단어 집합은 어느 경우든 그대로라 다시 검색할 필요가 없다.
    """
    keywords = list(card.get("keywords") or [])
    must = list(card.get("must_keywords") or [])
    if must and sorted(must) != sorted(keywords):
        words = must if set(must) & set(keywords) else list(dict.fromkeys(keywords + must))
        card["keywords"] = words
        must = words
    card["must_keywords"] = list(card["keywords"]) if must and len(card["keywords"]) >= 2 else []


_HHMM_RE = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")


def _validate_window(start: str, end: str) -> None:
    """"HH:MM" 형식(24시간제, 제로패딩)인지 확인하고, 순서를 확인한다.

    형식 검사가 따로 필요한 이유: 화면 입력칸이 `<input type="time">`이던 시절엔
    브라우저가 형식을 보장해줬지만, 24시간제 강제를 위해 일반 텍스트 입력(app/adhoc/
    renderer.py, 콜론 자동 삽입 JS)으로 바꾸면서 그 보장이 사라졌다 — 화면 JS 검증은
    우회 가능하므로 서버가 최종 방어선이다.

    "지금 시각보다 미래인가"는 실제 수집을 실행하는 시점의 시각과 맞춰봐야 하는 판단이라
    여기(카드 스키마 검증)가 아니라 수집을 실행하는 쪽(app/adhoc/collector.py)의
    책임이다 — ADHOC_DESIGN.md §6.2.
    """
    if not start or not end:
        raise AdhocCardError("수집 시간대를 입력해주세요.")
    if not _HHMM_RE.match(start) or not _HHMM_RE.match(end):
        raise AdhocCardError(f"시간은 00:00~23:59 형식으로 입력해주세요 ({start} ~ {end}).")
    if start >= end:
        raise AdhocCardError(f"끝나는 시각이 시작 시각보다 빠릅니다 ({start} ~ {end}).")


# --- 사안(issue) CRUD -----------------------------------------------------------


def load_issues() -> list[dict]:
    """등록된 사안 목록을 읽어온다. 파일이 없거나 손상됐으면 빈 목록으로 취급한다."""
    if not ISSUES_FILE.exists():
        return []
    try:
        data = json.loads(ISSUES_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, TypeError):
        return []
    return data.get("issues", [])


def _save_issues(issues: list[dict]) -> None:
    atomic_write_text(ISSUES_FILE, json.dumps({"issues": issues}, ensure_ascii=False, indent=2))


def find_issue(issue_id: str) -> Optional[dict]:
    return next((i for i in load_issues() if i["id"] == issue_id), None)


def find_issue_by_name(name: str) -> Optional[dict]:
    return next((i for i in load_issues() if i["name"] == name), None)


def get_or_create_issue(name: str, now: Optional[datetime] = None) -> dict:
    """이름으로 사안을 찾고, 없으면 새로 만들어 저장한다.

    사안명이 같으면 같은 사안으로 취급한다(대소문자·공백까지 완전 일치) — 이름을 바꾸는
    것과 새 사안을 만드는 것을 구분해야 하므로, 자동으로 비슷한 이름을 합치지 않는다
    (ADHOC_DESIGN.md 7절 — 사안 병합은 1차 범위 밖).
    """
    name = name.strip()
    if not name:
        raise AdhocCardError("사안 이름을 입력해주세요.")
    existing = find_issue_by_name(name)
    if existing is not None:
        return existing
    issue = {
        "id": _generate_issue_id(),
        "name": name,
        "last_keywords": [],
        "created_at": (now or datetime.now()).isoformat(timespec="seconds"),
    }
    _save_issues(load_issues() + [issue])
    return issue


def update_issue_last_keywords(issue_id: str, keywords: list[str]) -> None:
    """이 사안으로 방금 수집한 검색어를 기억해둔다 — 다음에 같은 사안을 고르면 자동으로
    채워진다(ADHOC_DESIGN.md §5.2, 원본 화면). 사안이 없으면 조용히 아무 일도 안 한다."""
    issues = load_issues()
    for issue in issues:
        if issue["id"] == issue_id:
            issue["last_keywords"] = list(keywords)
            _save_issues(issues)
            return


# --- 카드 CRUD -------------------------------------------------------------------


def new_card(
    issue_id: str,
    report_title: str,
    window_start: str,
    window_end: str,
    keywords: list[str],
    options: Optional[dict] = None,
    now: Optional[datetime] = None,
    must_keywords: Optional[list[str]] = None,
    raw: bool = True,
) -> dict:
    """카드를 새로 만들어 저장하고, 만든 카드를 돌려준다.

    articles/collect_log는 아직 비어있다 — 실제 검색·병합은 app/adhoc/collector.py(다음
    작업)가 이 카드를 받아 채운다. 여기서는 스키마와 저장만 책임진다.

    raw: 로데이터 원본으로 만든다(is_raw 참고). False는 옛 원본을 흉내 내야 하는 테스트용이다.
    """
    now = now or datetime.now()
    report_title = report_title.strip()
    if not report_title:
        raise AdhocCardError("사안명을 입력해주세요.")
    _validate_keywords(keywords)
    _validate_must_keywords(must_keywords or [])
    _validate_window(window_start, window_end)

    card = {
        "id": _generate_card_id(now),
        "issue_id": issue_id,
        "report_title": report_title,
        "collect_date": now.strftime("%Y-%m-%d"),
        "window": {"start": window_start, "end": window_end},
        "keywords": [k.strip() for k in keywords if k.strip()],
        # [수정: 2026-09-01] 수집 원본 화면에서 "검색어가 모두 있는 기사만"을 고르면
        # 여기에 같은 목록이 그대로 들어온다(§6.4a). 예전엔 항상 비어 있었다.
        "must_keywords": [k.strip() for k in (must_keywords or []) if k.strip()],
        "options": {
            "use_outlet_whitelist": False,
            "exclude_photo": False,
            "exclude_personnel": False,
            **(options or {}),
        },
        "status": "editing",
        "created_at": now.isoformat(timespec="seconds"),
        "archived_at": None,
        "collect_log": [],
        "group_order": [],
        "custom_groups": [],
        "group_summaries": {},
        "articles": [],
    }
    if raw:
        card["raw"] = True
    # 같은 날·같은 사안의 몇 번째 원본인가 — 화면에서 「인사청문회 #2」로 가른다(version_suffix).
    # 만들 때 적어 두는 이유: 매번 순서로 세면 가운데 원본을 지웠을 때 뒤 원본의 번호가 바뀐다.
    siblings = [
        c for c in cards_for_date(card["collect_date"])
        if card_kind(c) == KIND_COLLECT and issue_key(c) == issue_key(card)
    ]
    card["version"] = max((version_of(c, siblings) for c in siblings), default=0) + 1
    save_card(card)
    return card


def new_bundle_card(
    name: str, now: Optional[datetime] = None, issue_id: str = "", basis_time: str = "",
    source_card_id: str = "", source_version: int = 0,
) -> dict:
    """모음 카드를 만든다 (ADHOC_DESIGN.md §6.13) — 검색어·시간창 검증을 타지 않는
    유일한 생성자다.

    new_card와 분기로 합치지 않은 이유: 그 함수의 검증 세 줄(_validate_keywords /
    _validate_must_keywords / _validate_window)이 전부 "이 카드는 검색을 한다"는 전제
    위에 있다. if로 우회하기 시작하면 함수 하나가 두 가지를 어중간하게 말하게 된다.

    사안(issue)은 모음 이름 그대로 만들어 붙인다 — 수시 보관함이 사안 ▸ 날짜 ▸ 카드
    3단이라(§5.4) 사안 없이는 목록에 설 자리가 없고, 모음은 그 자체가 하나의 주제라
    별도 사안으로 서는 게 자연스럽다.

    issue_id: [추가: 2026-09-15] 원본에서 처음 보낼 때 저절로 만드는 확정본은 **그 원본의
    사안**에 붙인다(default_bundle_for) — 이름으로 다시 찾으면 담당자가 원본 사안명을 고쳐
    둔 경우 다른 사안으로 갈라져, 보관함에서 원본과 확정본이 다른 줄에 선다.

    basis_time: [추가: 2026-09-15] 이 확정본이 받는 **원본 기준 시각**(HH:MM) — 「불러올 때마다
    새 확정본」 규칙의 열쇠다(default_bundle_for). 원본에서 보내며 만들 때만 채운다.

    source_card_id: 이 확정본을 처음 만든 원본 — 같은 사안의 다른 판(#2)이 같은 기준 시각을
    가져도 이 확정본으로 섞여 들어오지 않게 한다(default_bundle_for).
    """
    now = now or datetime.now()
    name = name.strip()
    if not name:
        raise AdhocCardError("확정본 이름을 입력해주세요.")
    issue_id = issue_id or get_or_create_issue(name, now=now)["id"]
    basis_time = basis_time if _HHMM_RE.match(basis_time or "") else ""
    card = {
        "id": _generate_card_id(now),
        "kind": KIND_BUNDLE,
        "issue_id": issue_id,
        "report_title": name,
        "collect_date": now.strftime("%Y-%m-%d"),
        # 검색을 안 하므로 시간창·검색어·수집 이력은 구조적으로 비어 있다(window_of 참고).
        "window": None,
        "keywords": [],
        "must_keywords": [],
        "options": {
            "use_outlet_whitelist": False,
            "exclude_photo": False,
            "exclude_personnel": False,
        },
        "status": "editing",
        "created_at": now.isoformat(timespec="seconds"),
        "archived_at": None,
        "collect_log": [],
        "group_order": [],
        "custom_groups": [],
        "group_summaries": {},
        "articles": [],
    }
    if basis_time:
        card["basis_time"] = basis_time
    if source_card_id:
        card["source_card_id"] = source_card_id
    if source_version:
        # 처음 만든 원본의 판 번호 — 목록 이름(bundle_label)이 원본 탭과 같은 「#2」를 단다.
        card["source_version"] = int(source_version)
    save_card(card)
    return card


def load_card(card_id: str) -> Optional[dict]:
    path = _card_path(card_id)
    if not path.exists():
        return None
    try:
        card = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, TypeError):
        return None
    # must_keywords가 없는 옛 카드·두 칸이 섞인 옛 카드를 읽는 시점에 칸 하나 + 방식
    # 모양으로 맞춘다(normalize_condition). 파일은 다음 저장 때 따라 바뀐다.
    card.setdefault("keywords", [])
    normalize_condition(card)
    return card


def save_card(card: dict) -> None:
    atomic_write_text(_card_path(card["id"]), json.dumps(card, ensure_ascii=False, indent=2))


# 상단바 링크를 만들 때 뒤에서부터 몇 개까지 열어볼지. 종류(kind)를 가려야 할 때만
# 파일을 여는데, 대개 첫 한두 개에서 걸린다 — 전부 훑는 최악의 경우를 막는 상한이다.
_LATEST_LOOKBACK = 30


def latest_card_id(kind: Optional[str] = None) -> Optional[str]:
    """가장 최근에 만든 카드의 id — 상단바 링크의 목적지로만 쓴다.

    kind를 안 주면 카드 파일을 **한 개도 열지 않는다**: id가 `_generate_card_id`의
    `YYYYMMDD-HHMMSS-hex` 형식이라 파일명 사전순 정렬이 곧 시간순이다. list_cards()로
    구하면 카드 전부를 파싱해야 하는데(실측: 카드 365개 1,415ms —
    archive_renderer.render_archive_page 주석), 화면마다 있는 링크 하나 만들자고 치를
    비용이 아니다(CODING_CONVENTIONS.md §1).

    kind("collect"/"bundle")를 주면 종류가 파일 안에만 있으므로 최신순으로 최대
    _LATEST_LOOKBACK개까지만 열어보고 첫 일치를 돌려준다 — 상단바가 「원본 ↔
    확정본」을 오가려면 종류를 가려야 하기 때문이다. 그 안에 없으면 None(링크를 아예
    안 그린다).
    """
    stems = sorted((p.stem for p in CARDS_DIR.glob("*.json")), reverse=True)
    if not stems:
        return None
    if kind is None:
        return stems[0]
    for stem in stems[:_LATEST_LOOKBACK]:
        card = load_card(stem)
        if card is not None and card_kind(card) == kind:
            return stem
    return None


def list_cards(issue_id: Optional[str] = None) -> list[dict]:
    """저장된 카드를 최신순(collect_date·시작시각 내림차순)으로 돌려준다.

    app/run_index.py와 같은 방어적 파싱(깨진 파일 하나 때문에 전체가
    멈추지 않게) — 손상됐거나 필수 필드가 없는 카드 파일은 건너뛴다.
    """
    cards = []
    for path in CARDS_DIR.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            data["id"]
            data["created_at"]
        except (json.JSONDecodeError, KeyError, TypeError):
            continue
        if issue_id is not None and data.get("issue_id") != issue_id:
            continue
        cards.append(data)
    return sorted(cards, key=lambda c: c["created_at"], reverse=True)


# [추가: 2026-09-15] 카드 요약 캐시 — 새 수집의 「지난 수집」 1년치 목록과 날짜별 카드 찾기가
# 쓴다. 카드 파일은 기사까지 들어 있어 통째로 읽으면 비싸다(실측: 카드 365개 1,415ms —
# latest_card_id 주석). 파일마다 (mtime_ns, size) 서명이 같으면 예전에 뽑아둔 요약을 그대로
# 쓰고, 달라진 파일만 다시 연다 — 정기 회차 인덱스(app/run_index.py)와 같은 방식이다.
# 메모리에만 두므로 앱을 켤 때 한 번은 전부 연다.
_SUMMARY_FIELDS = (
    "id", "kind", "raw", "issue_id", "report_title", "collect_date",
    "keywords", "must_keywords", "created_at",
)
_summary_cache: dict[str, tuple] = {}
_summary_lock = threading.Lock()


def list_card_summaries() -> list[dict]:
    """저장된 카드마다 _SUMMARY_FIELDS만 담은 요약을 만든 시각 최신순으로 돌려준다.

    깨진 파일·필수 필드가 없는 파일은 list_cards()와 같이 건너뛴다. `kind`가 없는 옛 카드는
    collect로 채워 돌려준다(card_kind와 같은 규칙).
    """
    rows = []
    seen = set()
    with _summary_lock:
        for path in CARDS_DIR.glob("*.json"):
            key = str(path)
            try:
                stat = path.stat()
            except OSError:
                continue
            sig = (stat.st_mtime_ns, stat.st_size)
            hit = _summary_cache.get(key)
            if hit is not None and hit[0] == sig:
                summary = hit[1]
            else:
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    data["id"]
                    data["created_at"]
                except (json.JSONDecodeError, KeyError, TypeError, OSError):
                    summary = None
                else:
                    summary = {field: data.get(field) for field in _SUMMARY_FIELDS}
                    summary["kind"] = summary["kind"] or KIND_COLLECT
                _summary_cache[key] = (sig, summary)
            seen.add(key)
            if summary is not None:
                rows.append(summary)
        for key in [k for k in _summary_cache if k not in seen and k.startswith(str(CARDS_DIR))]:
            del _summary_cache[key]
    return sorted(rows, key=lambda s: s["created_at"], reverse=True)


def cards_for_date(date_str: str) -> list[dict]:
    """카드 화면 상단 "오늘의 수집 결과" 사안명 탭에 올릴 카드 목록 — 그 날짜에 만든
    카드 전부를, 만든 시간 오름차순(왼→오)으로.

    건수순이 아니라 시간순인 이유: 재수집으로 건수가 바뀔 때마다 탭이 자리를 바꾸면
    손이 기억한 위치를 잃는다. [수정: 2026-08-25] "보관(archived)" 상태·버튼은 없앴다 —
    카드는 만드는 순간부터 이미 수시 보관함에 있어 별도로 "보관 처리"할 게 없었고,
    실측(8/21~8/25, 카드 10개)에서 아무도 누른 적이 없었다(HISTORY.md 같은 항목 참고).
    이제 이 화면에서 카드를 빼는 유일한 길은 삭제뿐이다.

    [수정: 2026-09-15] 요약 캐시(list_card_summaries)로 그 날짜 카드만 골라 연다 — 예전엔
    카드 화면을 열 때마다 저장된 카드 전부를 파싱했다.
    """
    cards = [
        load_card(s["id"]) for s in list_card_summaries() if s.get("collect_date") == date_str
    ]
    return sorted((c for c in cards if c is not None), key=lambda c: c["created_at"])


# --- 같은 사안의 원본 판 번호 ---------------------------------------------------------
# 원본 하나 = 검색 조건 하나다. 조건을 바꾸려면 새로 수집하고, 같은 날 같은 사안의 원본이
# 여럿이면 화면에서 「인사청문회 #2」로 가른다. 첫 원본엔 번호를 안 붙인다 — 두 번째가 생기는
# 순간 이미 보던 탭 이름이 바뀌면 안 된다. 번호는 화면 전용이라 사안명·보고서 첫 줄엔 없다.


def issue_key(card: dict) -> str:
    """같은 사안인가를 가르는 열쇠 — 사안 id, 없는 옛 카드는 사안명."""
    return card.get("issue_id") or f'name:{card.get("report_title", "")}'


def raw_versions(card: dict, day_cards: Optional[list[dict]] = None) -> list[dict]:
    """card와 같은 날·같은 사안의 원본들(card 포함), 만든 순서대로. day_cards를 주면 그 목록에서
    고른다(이미 cards_for_date를 읽은 화면이 파일을 다시 열지 않게)."""
    if day_cards is None:
        day_cards = cards_for_date(card.get("collect_date") or "")
    key = issue_key(card)
    found = [c for c in day_cards if card_kind(c) == KIND_COLLECT and issue_key(c) == key]
    if not any(c["id"] == card["id"] for c in found):
        found.append(card)
    return sorted(found, key=lambda c: c.get("created_at") or "")


def version_of(card: dict, versions: Optional[list[dict]] = None) -> int:
    """몇 번째 원본인가. 만들 때 적어 둔 값이 먼저이고, 그 전 카드는 만든 순서로 센다."""
    if card.get("version"):
        return int(card["version"])
    versions = versions if versions is not None else raw_versions(card)
    ids = [c["id"] for c in versions]
    return ids.index(card["id"]) + 1 if card["id"] in ids else 1


def version_suffix(card: dict, day_cards: Optional[list[dict]] = None) -> str:
    """화면 이름 뒤에 붙일 「 #2」 — 확정본이거나 첫 원본이면 빈 문자열."""
    if is_bundle(card):
        return ""
    n = version_of(card, raw_versions(card, day_cards))
    return f" #{n}" if n >= 2 else ""


def version_ids(card: dict, day_cards: Optional[list[dict]] = None) -> set:
    """같은 사안 원본들의 id — 한 판에서 보낸 기사는 다른 판에서도 「✓ 보냄」으로 보인다."""
    return {c["id"] for c in raw_versions(card, day_cards)}


# --- 모음 카드 (ADHOC_DESIGN.md §6.13) -------------------------------------------


def bundles_for_date(date_str: str) -> list[dict]:
    """그 날짜의 모음 카드만 — 「모음으로 보내기」 드롭다운을 채우는 목록.

    날짜로 자르는 게 곧 자정 잠금이다(§6.13 규칙 2) — 어제 원본에서 오늘 모음으로는
    못 보내고, 그 반대도 마찬가지다. 카드 화면이 이미 같은 인자로 cards_for_date를
    부르고 있으므로(app.adhoc.renderer 사안 탭) 여기서 파일을 새로 여는 비용은 없다.
    """
    return [c for c in cards_for_date(date_str) if is_bundle(c)]


def default_bundle_for(source_card: dict, bundles: list[dict]) -> Optional[dict]:
    """[추가: 2026-09-15] 이 원본의 「확정본으로」가 기본으로 보낼 확정본 — bundles(그 날짜의
    확정본 목록) 중 **같은 사안**이면서 **원본 기준 시각이 지금 원본과 같은** 가장 최근 것.
    없으면 None이고, 그때는 처음 보내는 순간 routes가 새로 만든다(확정본은 따로 안 만들고
    저절로 생긴다).

    [수정: 2026-09-15, 2차] 「불러올 때마다 새 확정본」(사용자 결정, 시안
    ADHOC_RAW_TRASH_ROUND_MOCKUP.html) — 「지금까지 불러오기」가 정기의 회차 마감 같은 경계다.
    같은 원본 시각에서 보낸 기사는 몇 번에 나눠 눌러도 한 확정본으로 모이고, 다시 불러온 뒤
    보내면 새 확정본이 생긴다. 「보낼 때마다」로 나누면 기사 한 건씩 누를 때마다 한 건짜리
    확정본이 줄줄이 생겨서 기각했다. 사안으로 묶는 규칙은 그대로다 — 같은 날 같은 사안으로
    원본을 여러 장 만들어도(실측: 9/2 「이형일」 원본 3장) 같은 시각이면 보고서는 하나다.

    시각은 확정본에 적어둔 basis_time으로 맞춘다. 그 필드가 생기기 전의 확정본은 담긴 기사의
    원본 시각(bundle_basis_time)으로 대신 맞춘다 — 같은 날 먼저 쓰던 확정본을 그대로 이어 쓴다.
    """
    basis = window_of(source_card)["end"]
    same = [
        b for b in bundles
        if b.get("issue_id") == source_card.get("issue_id")
        and (b.get("basis_time") or bundle_basis_time(b)) == basis
        # 같은 사안의 다른 판(#2)이 만든 확정본은 기준 시각이 같아도 이어 쓰지 않는다 —
        # 조건이 다른 원본의 기사가 한 확정본에 섞인다. 이 필드가 없는 옛 확정본은 예전대로.
        and b.get("source_card_id", source_card["id"]) == source_card["id"]
    ]
    return max(same, key=lambda b: b["created_at"]) if same else None


def bundle_basis_time(bundle: dict) -> str:
    """[추가: 2026-09-15] 확정본 머리줄의 「N시 N분 기준」 — 담긴 기사를 **보낸 순간의 원본
    기준 시각** 중 가장 늦은 것(ADHOC_BUNDLE_HEADER_MOCKUP.html B안, 사용자 결정).

    원본의 **지금** 시각을 따라가지 않는다 — 원본을 더 불러와도 새로 보낸 게 없으면 확정본
    기사는 그대로라, 머리줄만 바뀌면 거짓말이 된다. 숨긴 기사 몫은 빼고, 이 기능 전에 보낸
    기사(시각이 안 적힘)만 있으면 빈 문자열이다 — 그때는 예전처럼 시각 없이 나간다(없는 값을
    지어내지 않는다).
    """
    times = [
        (a.get("sent_from") or {}).get("window_end") or ""
        for a in bundle.get("articles", [])
        if not a.get("hidden")
    ]
    return max((t for t in times if _HHMM_RE.match(t)), default="")


def bundle_time(bundle: dict) -> str:
    """[추가: 2026-09-15] 확정본의 「N시 N분 기준」 — 머리줄·탭·보관함 줄이 같이 쓴다.

    담긴 기사의 원본 시각(bundle_basis_time)이 먼저이고, 기사가 다 빠져 비었으면 만들 때 적어둔
    basis_time으로 물러선다(지어낸 값이 아니라 이 확정본이 받던 원본 시각이다). 둘 다 없는 옛
    확정본은 빈 문자열 — 예전처럼 시각 없이 나간다.
    """
    return bundle_basis_time(bundle) or bundle.get("basis_time") or ""


def bundle_label(bundle: dict) -> str:
    """확정본을 목록에서 가리키는 이름 — 「인사청문회 15:05」. 같은 날 같은 사안의 확정본이
    불러올 때마다 새로 생기므로 이름만으로는 서로 구분이 안 된다. 사안명(보고서 첫 줄로 나가는
    글자)엔 아무것도 안 붙이고, 목록에서만 시각을 곁들인다."""
    name = bundle["report_title"] + source_version_suffix(bundle)
    t = bundle_time(bundle)
    return f"{name} {t}" if t else name


_SOURCE_SUFFIX_CACHE: dict = {}


def source_version_suffix(bundle: dict) -> str:
    """확정본을 처음 만든 원본의 「 #2」 — 원본 탭 이름과 같은 번호를 목록 이름에 단다.
    첫 원본에서 만들었거나 원본에서 만든 확정본이 아니면 빈 문자열. 만들 때 적어 둔
    source_version이 먼저이고, 그 전 확정본은 원본 카드를 열어 센다(카드마다 한 번만)."""
    if bundle.get("source_version"):
        n = int(bundle["source_version"])
        return f" #{n}" if n >= 2 else ""
    # source_card_id가 생기기 전 확정본은 담긴 기사를 보낸 원본으로 대신 센다.
    src_id = bundle.get("source_card_id") or next(
        ((a.get("sent_from") or {}).get("card_id") for a in bundle.get("articles", [])
         if (a.get("sent_from") or {}).get("card_id")),
        "",
    )
    if not src_id:
        return ""
    if src_id not in _SOURCE_SUFFIX_CACHE:
        src = load_card(src_id)
        _SOURCE_SUFFIX_CACHE[src_id] = version_suffix(src) if src else ""
    return _SOURCE_SUFFIX_CACHE[src_id]


def sent_index(source_ids, bundles: list[dict]) -> dict:
    """{기사 URL: 그 기사를 받은 확정본 이름(bundle_label)} — 원본 목록에 「✓ 보냄」을 그리는 데 쓴다.

    source_ids: 원본 id 하나 또는 id 묶음. 원본 화면은 같은 사안의 모든 판(version_ids)을 넘긴다 —
    #1에서 보낸 기사가 #2에서 처리 안 한 기사로 보이면 같은 기사를 또 보내게 된다.

    **저장하지 않고 매번 다시 계산한다**(§6.13 규칙 5). 그래야 규칙 9(모음에서 숨기기가
    곧 보내기 취소)가 별도 배선 없이 성립한다 — 숨긴 기사는 아래에서 그냥 빠진다.
    """
    ids = {source_ids} if isinstance(source_ids, str) else set(source_ids)
    index: dict = {}
    for bundle in bundles:
        for article in bundle["articles"]:
            if article.get("hidden"):
                continue
            if (article.get("sent_from") or {}).get("card_id") not in ids:
                continue
            index.setdefault(article["url"], bundle_label(bundle))
    return index


# --- 로데이터 원본의 「🗑 숨김」 표시 (2026-09-15, ADHOC_RAW_TRASH_ROUND_MOCKUP.html) ------
# 실시간 현황의 🗑와 같은 동작이다 — 기사를 목록에서 **빼지 않고** 「숨김」 표시만 남긴다.
# 원본 카드의 raw_marks에 URL별로 적는다:
#   hidden — 원본에서 담당자가 🗑를 눌렀다
#   shown  — 확정본에서 뺀 기사를 원본에서 「처리 안 함」으로 되돌렸다(그 시각 at 이전에
#            확정본에서 숨긴 사본은 더는 「숨김」으로 치지 않는다)
# 확정본에서 뺀 기사가 원본에서도 「숨김」으로 보이는 건 따로 적지 않고 매번 계산한다
# (raw_row_states) — 정기에서 초안·확정본에서 숨긴 기사가 실시간에 「🗑 숨김」으로 보이는
# 것과 같은 뜻("이 기사는 안 쓴다")이다. 기사의 hidden 필드는 안 쓴다: 그 값은 조건 필터·
# 「목록 밖 기사」가 보는 값이라, 쓰면 기사가 목록에서 사라진다.
RAW_MARK_HIDDEN = "hidden"
RAW_MARK_SHOWN = "shown"


def mark_hidden(article: dict, now: Optional[datetime] = None) -> None:
    """기사를 숨기고 숨긴 시각을 남긴다 — 확정본에서 뺀 시각이 원본의 「shown」 표시보다
    늦으면 원본에서 다시 「숨김」으로 보여야 해서다(raw_row_states)."""
    article["hidden"] = True
    article["hidden_at"] = (now or datetime.now()).isoformat(timespec="seconds")


def raw_row_states(source_card: dict, bundles: list[dict], source_ids: Optional[set] = None) -> dict:
    """원본 행마다의 상태 — {URL: ("sent", 확정본 이름) | ("hidden", 확정본 이름 또는 "")}.

    처리 안 한 기사는 담지 않는다. 우선순위는 보냄 > 원본에서 숨김 > 확정본에서 뺌. 두 번째
    값은 화면 툴팁용이다 — 원본에서 숨긴 건 빈 문자열, 확정본에서 뺀 건 그 확정본 이름.
    저장하지 않고 매번 다시 계산한다(sent_index와 같은 원칙).

    보냄·확정본에서 뺌은 같은 사안의 모든 판(source_ids, 기본 version_ids)에서 보낸 사본을 본다.
    """
    ids = source_ids if source_ids is not None else version_ids(source_card)
    sent: dict = {}
    pulled: dict = {}  # 확정본에서 뺀 사본 — {url: (가장 늦게 숨긴 시각, 확정본 이름)}
    for bundle in bundles:
        label = bundle_label(bundle)
        for article in bundle["articles"]:
            if (article.get("sent_from") or {}).get("card_id") not in ids:
                continue
            url = article["url"]
            if not article.get("hidden"):
                sent.setdefault(url, label)
                continue
            at = article.get("hidden_at") or ""
            if url not in pulled or at > pulled[url][0]:
                pulled[url] = (at, label)
    marks = source_card.get("raw_marks") or {}
    states = {url: ("sent", label) for url, label in sent.items()}
    for url, mark in marks.items():
        if url not in states and mark.get("state") == RAW_MARK_HIDDEN:
            states[url] = ("hidden", "")
    for url, (at, label) in pulled.items():
        if url in states:
            continue
        mark = marks.get(url) or {}
        # 원본에서 「처리 안 함」으로 되돌린 뒤라면 그 전에 뺀 사본은 안 친다. 숨긴 시각이
        # 안 적힌 옛 사본("")은 언제나 되돌림보다 앞선 것으로 본다.
        if mark.get("state") == RAW_MARK_SHOWN and at <= (mark.get("at") or ""):
            continue
        states[url] = ("hidden", label)
    return states


def open_raw_urls(source_card: dict, bundles: list[dict]) -> list[str]:
    """원본에서 아직 처리 안 한 기사 URL(화면 순서 그대로가 아니라 카드 순서) — 툴바의
    「처리 안 한 N건 전부 확정본으로」가 보낼 목록. 화면에 보이는 기사(조건 안 + 숨기지
    않음) 중 raw_row_states에 없는 것이다 — 화면이 센 건수와 서버가 보내는 건수가 같아야
    한다(visible_group_urls와 같은 이유)."""
    from app.adhoc.collector import article_in_condition

    states = raw_row_states(source_card, bundles)
    return [
        a["url"]
        for a in source_card["articles"]
        if not a.get("hidden") and article_in_condition(a, source_card) and a["url"] not in states
    ]


def set_raw_mark(card_id: str, url: str, hide: bool, bundles: list[dict], now: Optional[datetime] = None) -> Optional[dict]:
    """원본의 🗑(hide=True) / 「🗑 숨김」 다시 누르기(hide=False).

    되돌릴 때 그 기사가 확정본에서 빠진 사본이면 「shown」을 지금 시각으로 적는다 — 적지
    않고 표시만 지우면 확정본 쪽 사본 때문에 곧바로 다시 「숨김」으로 보인다. 확정본과 얽힌
    게 없으면 표시를 그냥 지운다. 확정본 카드는 건드리지 않는다(원본 화면의 ↩ 되돌리기가 이
    카드 하나만 스냅샷하므로, 다른 카드까지 고치면 되돌리기가 반쪽이 된다).
    """
    now = now or datetime.now()
    with card_lock(card_id):
        c = load_card(card_id)
        if c is None:
            return None
        marks = c.setdefault("raw_marks", {})
        if hide:
            marks[url] = {"state": RAW_MARK_HIDDEN, "at": now.isoformat(timespec="seconds")}
        else:
            ids = version_ids(c)
            pulled = any(
                a["url"] == url and a.get("hidden")
                and (a.get("sent_from") or {}).get("card_id") in ids
                for b in bundles for a in b["articles"]
            )
            if pulled:
                marks[url] = {"state": RAW_MARK_SHOWN, "at": now.isoformat(timespec="seconds")}
            else:
                marks.pop(url, None)
        save_card(c)
        return c


def unsend_raw(card_id: str, url: str, bundles: list[dict], now: Optional[datetime] = None) -> list[str]:
    """[추가: 2026-09-22] 원본의 「✓ 보냄」 다시 누르기 = 보냄 취소.

    이 원본에서 보낸 그 기사의 사본을 오늘 확정본마다 숨긴다(확정본에서 🗑를 누른 것과 같다 —
    sent_index가 숨긴 사본을 안 센다). 그리고 원본 표시를 같은 시각의 「shown」으로 적는다:
    안 적으면 raw_row_states가 확정본에서 뺀 사본을 보고 「🗑 숨김」으로 그린다. 취소는
    "처리 안 함"으로 돌아가는 것이지 "안 쓴다"가 아니다.

    돌려주는 값은 사본을 숨긴 확정본 id 목록 — 되돌리기(undo.attach_link)가 그 사본을 다시
    보이게 하는 데 쓴다. 보낸 사본이 없으면 아무것도 안 쓰고 빈 목록.
    """
    now = now or datetime.now()
    stamp = now.isoformat(timespec="seconds")
    source = load_card(card_id)
    # 같은 사안의 다른 판에서 보낸 사본도 취소한다 — 이 판에도 「✓ 보냄」으로 보였으니까.
    ids = version_ids(source) if source is not None else {card_id}
    senders: set = set()
    touched: list[str] = []
    for bundle in bundles:
        with card_lock(bundle["id"]):
            b = load_card(bundle["id"])
            if b is None:
                continue
            hit = False
            for article in b["articles"]:
                sender = (article.get("sent_from") or {}).get("card_id")
                if article["url"] == url and not article.get("hidden") and sender in ids:
                    mark_hidden(article, now)
                    senders.add(sender)
                    hit = True
            if hit:
                save_card(b)
                touched.append(b["id"])
    # 「shown」은 이 원본과, 실제로 보냈던 판에 적는다 — 안 적은 판에선 뺀 사본이 「🗑 숨김」으로 보인다.
    for cid in ({card_id} | senders) if touched else set():
        with card_lock(cid):
            c = load_card(cid)
            if c is not None:
                c.setdefault("raw_marks", {})[url] = {"state": RAW_MARK_SHOWN, "at": stamp}
                save_card(c)
    return touched


def clear_raw_marks(card_id: str, urls: list[str]) -> None:
    """보낸 기사의 원본 표시를 지운다 — 체크박스로 「숨김」 행까지 골라 보냈으면 그건 담당자가
    마음을 바꾼 것이다. 지울 게 없으면 파일을 안 쓴다."""
    with card_lock(card_id):
        c = load_card(card_id)
        if c is None:
            return
        marks = c.get("raw_marks") or {}
        gone = [u for u in urls if u in marks]
        if not gone:
            return
        for u in gone:
            marks.pop(u)
        save_card(c)


def send_to_bundle(
    bundle_id: str,
    source_card: dict,
    urls: list[str],
    group_name: str = "",
    now: Optional[datetime] = None,
) -> tuple[int, str]:
    """원본 카드에서 고른 기사를 모음 카드로 **복사**한다 (§6.13 규칙 1).

    원본 카드는 이 함수가 한 글자도 건드리지 않는다 — 인자로 읽기만 한다. 그래서
    원본의 소제목 건수도 안 변하고 순서에도 구멍이 안 난다.

    group_name: 소제목을 통째로 보낼 때만 채워진다(§6.13 "소제목 단위로 통째 보내기" —
    담당자가 묶음을 통째로 집었다는 건 그 이름도 쓸 만하다고 판단한 것이므로 이름을
    들고 간다). 낱개로 보낼 때는 빈 문자열이고, 그때는 미분류로 들어간다.

    돌려주는 값: (실제로 보낸 건수, 실제로 쓴 소제목 이름). 소제목 상한에 걸려
    미분류로 떨어졌으면 이름 자리가 빈 문자열이다.
    """
    now = now or datetime.now()
    wanted_urls = set(urls)
    wanted = [a for a in source_card["articles"] if a["url"] in wanted_urls]
    if not wanted:
        return 0, ""

    with card_lock(bundle_id):
        bundle = load_card(bundle_id)
        if bundle is None:
            raise AdhocCardError("확정본을 찾을 수 없습니다. 새로고침 후 다시 시도해주세요.")
        if not is_bundle(bundle):
            raise AdhocCardError("확정본이 아닌 카드로는 보낼 수 없습니다.")

        existing = {a["url"]: a for a in bundle["articles"]}
        # [추가: 2026-09-15] 이 확정본에서 뺐던(숨긴) 기사를 다시 보내면 되살린다 — 자리·소제목은
        # 그대로. 예전엔 아래 규칙대로 조용히 건너뛰어, 원본에선 보냈는데 확정본엔 여전히 빠진
        # 채였다(보낸 표시도 안 붙어 버튼만 헛돌았다).
        revived = 0
        for article in wanted:
            copy_in = existing.get(article["url"])
            if copy_in is not None and copy_in.get("hidden"):
                copy_in["hidden"] = False
                copy_in.pop("hidden_at", None)
                copy_in["sent_from"] = {
                    "card_id": source_card["id"],
                    "report_title": source_card["report_title"],
                    "window_end": window_of(source_card)["end"],
                }
                revived += 1
        # 같은 URL은 1건(§6.13 규칙 4) — 먼저 들어온 것의 소제목 배정·순서를 지키려고
        # 덮어쓰지 않고 조용히 건너뛴다.
        fresh = [a for a in wanted if a["url"] not in existing]
        if not fresh:
            if revived:
                save_card(bundle)
            return revived, ""

        group = group_name.strip()
        if group and group not in current_group_names(bundle):
            if len(current_group_names(bundle)) >= MAX_ADHOC_SUBHEADINGS:
                # 이름 하나를 못 지키는 것보다, 담당자가 고른 기사가 통째로 안 가는
                # 쪽이 훨씬 나쁜 실패다 — 조용히 미분류로 떨어뜨리고 기사는 다 보낸다.
                group = ""
            else:
                bundle["custom_groups"].append(group)

        _insert_by_outlet(bundle, fresh, group or None, now, lambda article: {
            "card_id": source_card["id"],
            "report_title": source_card["report_title"],
            # [추가: 2026-09-15] 보낸 순간의 원본 기준 시각 — 확정본 머리줄의 「N시 N분
            # 기준」이 이 값을 쓴다(bundle_basis_time). 원본이 나중에 더 불러와도 이 기사가
            # 몇 시 기준 원본에서 골라졌는지는 안 변한다.
            "window_end": window_of(source_card)["end"],
        })

        save_card(bundle)
        return len(fresh) + revived, group


def _insert_by_outlet(bundle: dict, articles: list[dict], group: Optional[str], now: datetime, sent_from_of) -> None:
    """확정본에 기사를 복사해 **언론사 순위 자리에 끼워 넣는다** — 원본에서 보내기
    (send_to_bundle)와 다른 사안 확정본으로 옮기기(move_to_bundle)가 같이 쓴다.

    [추가: 2026-09-03] **확정본의 출발 순서는 언론사순이다** — 시간순인 수집 원본과
    갈리는 지점이다. 목록 전체를 다시 세우지 않고 **새로 온 기사만 제자리에 끼워
    넣는다**: 전체 재정렬은 담당자가 ↑↓로 맞춰둔 순서를 조용히 지워버리고, 그건
    CLAUDE.md "담당자 > AI: 정렬·분류 우선순위"가 금지한 바로 그 동작이다.
    정렬 키는 sort_by_outlet_priority와 같은 함수를 쓴다(app.sorter.outlet_sort_key)
    — 정렬과 삽입이 각자 키를 만들면 한쪽만 고쳤을 때 조용히 어긋난다.

    sent_from_of(article) → 복사본에 적을 sent_from. 보내기는 원본 카드를, 옮기기는 기사가
    원래 들고 있던 값을 그대로 적는다(그 기사를 고른 원본은 옮겨도 안 바뀐다).
    """
    outlet_order = load_settings().get("outlet_order") or None
    for article in sorted(articles, key=lambda a: outlet_sort_key(a, outlet_order)):
        copied = dict(article)
        copied["group"] = group
        copied["hidden"] = False
        copied.pop("hidden_at", None)
        # added_by="manual"이라야 모음의 조건 필터(article_in_condition)와 시간창
        # 검사(article_out_of_window)를 둘 다 통과한다 — §6.13 규칙 3.
        copied["added_by"] = "manual"
        copied["added_at"] = now.isoformat(timespec="seconds")
        sent_from = sent_from_of(article)
        if sent_from:
            copied["sent_from"] = sent_from
        key = outlet_sort_key(copied, outlet_order)
        # 같은 소제목 안에서 자기보다 순위가 낮은 첫 기사 **앞**에 선다. 다른 소제목
        # 기사는 건너뛴다 — 화면은 소제목별로 묶어 보여주므로(_article_groups) 같은
        # 소제목 안의 상대 순서만 뜻이 있다.
        at = len(bundle["articles"])
        for i, existing_article in enumerate(bundle["articles"]):
            if existing_article.get("group") != group:
                continue
            if outlet_sort_key(existing_article, outlet_order) > key:
                at = i
                break
        bundle["articles"].insert(at, copied)


def move_targets(bundle: dict, bundles: list[dict]) -> list[dict]:
    """[추가: 2026-09-17] 확정본의 「옮기기 ▾」 → 「다른 사안 확정본으로」에 올릴 확정본 —
    bundles(그 날짜 확정본) 중 **사안이 다른** 것만, 만든 순서대로.

    같은 사안의 다른 시각 확정본은 뺀다 — 사안을 가르는 동작이지 회차를 옮기는 동작이
    아니다(시안 ADHOC_MOVE_DROPDOWN_MOCKUP.html). 사안 id가 없는 옛 확정본은 사안명으로 맞춘다.
    """
    own = issue_key(bundle)
    return [b for b in bundles if b["id"] != bundle["id"] and issue_key(b) != own]


def move_to_bundle(
    source_id: str, target_id: str, urls: list[str], now: Optional[datetime] = None
) -> dict:
    """[추가: 2026-09-17] 확정본에서 고른 기사를 **다른 사안의 확정본으로 옮긴다** — 보내기
    (send_to_bundle, 복사)와 달리 이 확정본에선 빠진다(숨김 처리 — 좌하단 「숨긴 기사」에서
    되살릴 수 있고, 원본의 「✓ 보냄」은 받는 확정본이 이어받는다).

    받는 쪽 규칙은 원본에서 보낼 때와 같다: 소제목 없이(📂 미분류) 언론사 순위 자리에 들어가고,
    같은 URL이 이미 보이면 건너뛰고, 거기서 뺐던(숨긴) 사본이면 제자리에 되살린다.

    화면에 보이는 기사만 옮긴다(visible_urls_among — 숨기기와 같은 기준). 두 카드를 함께
    고치므로 락은 id 순서로 쥔다(두 확정본이 서로에게 동시에 옮겨도 교착이 안 생긴다).

    돌려주는 값: {"moved": 옮긴 건수, "added": 받는 쪽에 새로 넣은 URL, "revived": 되살린 URL}
    — 되돌리기(app.adhoc.undo.attach_link)가 받는 쪽을 원래대로 돌리는 데 쓴다.
    """
    now = now or datetime.now()
    if source_id == target_id:
        raise AdhocCardError("같은 확정본으로는 옮길 수 없습니다.")
    first, second = sorted([source_id, target_id])
    with card_lock(first), card_lock(second):
        source = load_card(source_id)
        target = load_card(target_id)
        if source is None or target is None:
            raise AdhocCardError("확정본을 찾을 수 없습니다. 새로고침 후 다시 시도해주세요.")
        if not (is_bundle(source) and is_bundle(target)):
            raise AdhocCardError("확정본끼리만 옮길 수 있습니다.")
        if source.get("collect_date") != target.get("collect_date"):
            raise AdhocCardError("다른 날짜의 확정본으로는 옮길 수 없습니다.")
        wanted = set(visible_urls_among(source, urls))
        if not wanted:
            return {"moved": 0, "added": [], "revived": []}

        existing = {a["url"]: a for a in target["articles"]}
        revived = []
        for url in wanted:
            copy_in = existing.get(url)
            if copy_in is not None and copy_in.get("hidden"):
                copy_in["hidden"] = False
                copy_in.pop("hidden_at", None)
                revived.append(url)
        fresh = [a for a in source["articles"] if a["url"] in wanted and a["url"] not in existing]
        _insert_by_outlet(target, fresh, None, now, lambda article: article.get("sent_from"))
        for article in source["articles"]:
            if article["url"] in wanted:
                mark_hidden(article, now)
        save_card(target)
        save_card(source)
        return {"moved": len(wanted), "added": [a["url"] for a in fresh], "revived": revived}


def bundle_sources(bundle: dict) -> list[tuple]:
    """모음 화면 상단 📥 칩 — [(원본 카드 id, 원본 이름, 건수)], 처음 들어온 순서.

    숨긴 기사는 안 센다(화면 목록과 칩의 건수가 어긋나면 안 된다). 이름은 저장된
    `sent_from.report_title`을 쓴다 — 원본 카드가 1년 뒤 삭제돼도 이 표시가 안 깨진다.
    """
    order: list = []
    counts: dict = {}
    names: dict = {}
    for article in bundle["articles"]:
        if article.get("hidden"):
            continue
        source = article.get("sent_from") or {}
        card_id = source.get("card_id")
        if not card_id:
            continue
        if card_id not in counts:
            order.append(card_id)
            names[card_id] = source.get("report_title") or "(이름 없음)"
        counts[card_id] = counts.get(card_id, 0) + 1
    return [(cid, names[cid], counts[cid]) for cid in order]


def delete_card(card_id: str, reason: str = "deleted", stamp: Optional[str] = None) -> bool:
    """카드를 지운다 — 바로 지우지 않고 trash로 옮긴다(CLAUDE.md 작업 규칙: 파일을 지워야
    할 때는 trash-can 폴더로 옮겨만 두고, 실제 삭제는 사람이 확인 후 한다).

    같은 이름으로 두 번 지워질(재사용될) 일은 없지만(카드 id에 생성 시각이 박혀있음),
    trash 안에서 이름이 겹치는 사고를 원천 차단하려고 옮기는 시각도 파일명에 붙인다.

    [추가: 2026-09-11] reason — 파일명 가운데 자리(`{id}.{reason}-{시각}.json`).
    담당자가 지운 것은 "deleted", 1년 보관 기한이 지나 옮긴 것은 "expired"다. 수시
    보관함의 「최근 삭제」(되살리기)는 "deleted"만 보여준다 — 기한으로 옮긴 카드를
    되살리면 다음 정리 때 곧바로 다시 옮겨질 뿐이다.
    stamp: 지운 시각("YYYYMMDDHHMMSS"). 여러 장을 한 번에 지울 때 같은 값을 넘기면
    「최근 삭제」에서 한 묶음으로 보인다.
    """
    # [추가: 2026-09-11] id는 화면 폼에서 그대로 온다 — 형식을 못 박지 않으면 `../issues` 같은
    # 값이 이 폴더 밖 파일(사안 목록)을 trash로 옮긴다. 보관함에서 여러 장을 한 번에 지우는
    # 길이 생기며 확인했다(카드 화면의 기존 삭제도 같은 구멍이었다).
    if not _CARD_ID_RE.match(card_id or ""):
        return False
    with card_lock(card_id):
        path = _card_path(card_id)
        if not path.exists():
            return False
        stamp = stamp or datetime.now().strftime("%Y%m%d%H%M%S")
        trashed_name = f"{card_id}.{reason}-{stamp}.json"
        path.replace(TRASH_DIR / trashed_name)
        return True


# [추가: 2026-09-11] 카드 id 형식(`_generate_card_id`) — 삭제·되살리기 요청이 경로 조각을
# 실어 와도 이 폴더 밖을 못 건드리게 못 박는다. 되살리기는 담당자가 지운 것(reason="deleted")만 고른다.
_CARD_ID_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]+$")
_DELETED_CARD_RE = re.compile(r"^(\d{8}-\d{6}-[0-9a-f]+)\.deleted-(\d{14})\.json$")


def list_deleted_cards() -> list[dict]:
    """담당자가 지운 카드 목록 — 지운 시각 최신 먼저. 각 카드 dict에 `_trash_name`과
    `_deleted_at`(datetime)을 덧붙여 돌려준다. 같은 id가 여럿이면(지웠다 되살렸다 다시
    지운 경우) 가장 최근 것 하나만. 이미 되살아나 카드 폴더에 있는 id는 뺀다.

    이 목록은 수시 보관함 한 화면을 그릴 때 한 번만 부른다 — 파일을 열어야 사안·시간창·
    건수를 알 수 있지만, 쌓이는 건 담당자가 손으로 지운 것뿐이라 수가 적다.
    """
    latest: dict = {}
    for path in TRASH_DIR.glob("*.json"):
        m = _DELETED_CARD_RE.match(path.name)
        if not m:
            continue
        card_id, stamp = m.groups()
        if card_id in latest and latest[card_id][1] >= stamp:
            continue
        latest[card_id] = (path, stamp)
    cards = []
    for card_id, (path, stamp) in latest.items():
        if _card_path(card_id).exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            data["id"]
        except (json.JSONDecodeError, KeyError, TypeError):
            continue
        data.setdefault("must_keywords", [])
        data["_trash_name"] = path.name
        data["_deleted_at"] = datetime.strptime(stamp, "%Y%m%d%H%M%S")
        cards.append(data)
    # 한 번에 지운 묶음 안에서는 만든 시각 최신 먼저 — 보관함 목록과 같은 순서다.
    return sorted(cards, key=lambda c: (c["_deleted_at"], c.get("created_at", "")), reverse=True)


def restore_card(trash_name: str) -> Optional[str]:
    """지운 카드를 되살린다. 성공하면 카드 id, 못 하면 None(형식이 틀린 이름·이미 없는
    파일·같은 id의 카드가 이미 있음 — 덮어쓰면 그쪽 작업이 사라진다)."""
    m = _DELETED_CARD_RE.match(trash_name or "")
    if not m:
        return None
    card_id = m.group(1)
    with card_lock(card_id):
        src = TRASH_DIR / trash_name
        if not src.exists() or _card_path(card_id).exists():
            return None
        src.replace(_card_path(card_id))
        return card_id


def delete_expired_cards(retention_days: Optional[int] = None, now: Optional[datetime] = None) -> list[str]:
    """생성된 지 retention_days일이 지난 카드를 trash로 옮긴다 (CLAUDE.md 기능9 규칙9,
    app.storage.delete_expired_runs와 같은 정책 — "정기와 1년으로 맞춘다").

    나이는 created_at 기준이다(수시는 회차 개념이 아니라 카드 하나가 곧 그 사안의 한
    수집 세션이므로, 정기의 run_at에 해당하는 값이 이거다 — 재수집은 같은 카드를
    병합만 하고 created_at을 갱신하지 않으므로, "이 카드를 처음 만든 시점"이 그대로
    나이 기준이 된다).

    라벨이 붙은 기사를 이 삭제가 지워버릴 걱정은 안 해도 된다 — app.labels가 라벨을
    붙이는 순간 기사 정보를 이미 별도 저장소로 복사해 카드 생애주기와 완전히
    독립시켜 뒀다(app/labels.py 모듈 설명 참고). 카드가 지워져도 라벨 목록엔 그대로 남는다.

    실제로 지우지 않고 delete_card()로 trash에 옮긴다(CLAUDE.md 작업 규칙 — 삭제는
    사람이 확인 후 직접). 반환값: trash로 옮긴 카드 id 목록.
    """
    from app.config import ADHOC_RETENTION_DAYS

    retention_days = ADHOC_RETENTION_DAYS if retention_days is None else retention_days
    now = now or datetime.now()
    cutoff = now - timedelta(days=retention_days)

    moved = []
    for path in CARDS_DIR.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            created_at = datetime.fromisoformat(data["created_at"])
            card_id = data["id"]
        except (json.JSONDecodeError, KeyError, ValueError, TypeError):
            # 판단 불가한 파일은 건드리지 않는다 — app.storage.delete_expired_runs와 같은 방어.
            continue
        if created_at < cutoff and delete_card(card_id, reason="expired"):
            moved.append(card_id)
    return moved


def append_collect_log(
    card_id: str,
    window_end: str,
    found: int,
    added: int,
    now: Optional[datetime] = None,
) -> Optional[dict]:
    """이번 수집 결과를 카드의 수집 이력에 한 줄 추가하고 저장한다(ADHOC_DESIGN.md §3
    collect_log). 실제 검색·기사 병합은 collector.py가 하고, 그 결과를 여기 기록만 한다.
    """
    with card_lock(card_id):
        c = load_card(card_id)
        if c is None:
            return None
        c["collect_log"].append(
            {
                "at": (now or datetime.now()).isoformat(timespec="seconds"),
                "window_end": window_end,
                "found": found,
                "added": added,
            }
        )
        save_card(c)
        return c


def last_collect_new_urls(card: dict) -> set[str]:
    """마지막 불러오기로 **새로** 들어온 기사 URL — 화면이 노랑으로 칠할 대상.

    [추가: 2026-09-15] 「지금까지 불러오기」로 한 번 누르기가 쉬워지면서, 누른 뒤
    "뭐가 새로 왔지?"를 📥 칩의 +N건 숫자만으로는 못 찾게 됐다. 기준은 저장된 값 둘을
    맞대는 것뿐이다: run_collect가 새 기사에 찍는 added_at과, 같은 now로 append_collect_log가
    찍는 마지막 수집 이력의 at. 그래서 **노랑 개수 = 📥 칩 마지막 「+N건」**이 된다(숨긴
    기사만큼 줄 수는 있다). 검색어 편집은 수집 이력을 안 남기므로(log=False) 그때 들어온
    기사는 칠하지 않는다 — 칩에 안 보이는 몫까지 칠하면 두 숫자가 어긋난다.

    **첫 수집은 칠하지 않는다**(수집 이력이 2줄 이상일 때만) — 그땐 전부가 새 기사라
    칠해봐야 정보가 없다(정기의 「새로 들어온 기사」가 기준선 없는 첫 열람엔 아무것도 안
    칠하는 것과 같은 규칙). 모음 카드는 수집 이력이 없어 자연히 빈 집합이다.
    """
    log = card.get("collect_log") or []
    if len(log) < 2:
        return set()
    last_at = log[-1]["at"]
    return {
        a["url"]
        for a in card.get("articles", [])
        if a.get("added_by") == "collect" and a.get("added_at") == last_at
    }


def current_group_names(card: dict) -> set:
    """이 카드에 지금 존재하는 소제목 이름 전부 — 자동 분류로 생긴 것 + 담당자가
    직접 만든 것(기사가 아직 없어도 포함). MAX_ADHOC_SUBHEADINGS 상한을 셀 때와
    "다른 소제목" 드롭다운을 채울 때 기준이 같아야 하므로 한 곳에 모아둔다.
    """
    return {a["group"] for a in card["articles"] if a.get("group")} | set(card["custom_groups"])


def add_custom_group(card_id: str, name: str) -> dict:
    """정기의 "+ 새 소제목 만들기"와 같은 용도 — 검색 결과로는 절대 안 나올 이름을
    미리 만들어두고, 기사가 없어도 화면에 유지한다(ADHOC_DESIGN.md §3.3 custom_groups).

    이름이 비어있거나, 이미 있는 소제목(자동 분류·직접 만든 것 통틀어)과 겹치거나,
    전체 소제목 개수가 이미 상한(MAX_ADHOC_SUBHEADINGS)이면 AdhocCardError.
    카드가 없으면 AdhocCardError(다른 함수들처럼 None을 돌려주지 않는 이유는, 이건
    사용자가 방금 입력한 이름을 검증하는 흐름이라 routes.py가 실패 사유를 그대로
    화면에 보여줘야 하기 때문이다 — 다른 카드 조작들처럼 "없으면 404"로 뭉뚱그리면
    "이름이 비었다"와 "카드를 못 찾았다"를 화면에서 구분할 수 없다).
    """
    name = name.strip()
    if not name:
        raise AdhocCardError("소제목 이름을 입력해주세요.")

    with card_lock(card_id):
        c = load_card(card_id)
        if c is None:
            raise AdhocCardError(f"카드를 찾을 수 없습니다: {card_id}")
        existing = current_group_names(c)
        if name in existing:
            raise AdhocCardError(f'이미 "{name}"라는 이름의 소제목이 있습니다.')
        if len(existing) >= MAX_ADHOC_SUBHEADINGS:
            raise AdhocCardError(f"소제목은 최대 {MAX_ADHOC_SUBHEADINGS}개까지 만들 수 있습니다.")
        c["custom_groups"].append(name)
        save_card(c)
        return c


def move_article_order(card_id: str, url: str, direction: str) -> Optional[dict]:
    """같은 소제목 "안"에서 기사 하나를 이웃과 맞바꾼다(정기의 app.curation.move_article과
    같은 방식 — 별도 순서 필드 없이 card["articles"] 리스트 안에서의 위치 자체가 화면
    표시 순서다. app.adhoc.renderer._article_groups가 이 리스트를 순서 그대로 훑어
    소제목별로 묶으므로, 여기서 두 기사의 리스트상 위치만 맞바꾸면 화면에 그대로 반영된다).

    이웃 관계는 "화면에 보이는(숨김 제외) 같은 소제목" 기준으로 계산하되, 실제로
    맞바꾸는 건 원본 리스트에서 그 두 기사가 있는 위치뿐이다 — 정기와 동일하게, 둘
    사이에 숨긴 기사가 끼어 있어도 그 기사의 위치는 건드리지 않는다.

    소제목 경계는 넘지 않는다(ADHOC_DESIGN.md에 정기 규칙21의 "경계에서 한 번 더 누르면
    옆 소제목으로 이동" 같은 요구가 없다 — 그 역할은 이미 "다른 소제목" 드롭다운이
    한다). 맨 위/아래에 닿았거나 기사·카드를 못 찾으면 조용히 그대로 둔다.
    """
    with card_lock(card_id):
        c = load_card(card_id)
        if c is None:
            return None
        articles = c["articles"]
        target = next((a for a in articles if a["url"] == url), None)
        if target is None or target.get("hidden"):
            return c
        siblings = [
            a["url"] for a in articles if a.get("group") == target.get("group") and not a.get("hidden")
        ]
        pos = siblings.index(url)
        neighbor_pos = pos - 1 if direction == "up" else pos + 1
        if neighbor_pos < 0 or neighbor_pos >= len(siblings):
            return c
        neighbor_url = siblings[neighbor_pos]
        i = next(i for i, a in enumerate(articles) if a["url"] == url)
        j = next(i for i, a in enumerate(articles) if a["url"] == neighbor_url)
        articles[i], articles[j] = articles[j], articles[i]
        save_card(c)
        return c


def bulk_move_articles(card_id: str, urls: list[str], target_group: str) -> Optional[dict]:
    """체크박스로 고른 기사 여러 건을 한 번에 target_group으로 옮긴다(정기의
    app.curation.bulk_reassign_group과 같은 용도) — 선택된 기사가 서로 다른
    소제목에 걸쳐 있어도 상관없다, 전부 target_group 하나로 모인다.

    target_group은 화면의 드롭다운이 "지금 이 카드에 실제로 있는 소제목" 중에서만
    고르게 하므로 여기서 새로 검증하지 않는다(add_custom_group과 달리 새 이름을
    만드는 동작이 아니다 — 상한 걱정도 없다).
    """
    with card_lock(card_id):
        c = load_card(card_id)
        if c is None:
            return None
        url_set = set(urls)
        for a in c["articles"]:
            if a["url"] in url_set:
                a["group"] = target_group
        save_card(c)
        return c


def visible_group_urls(card: dict, name: str) -> list[str]:
    """그 소제목에서 **지금 화면에 보이는** 기사의 URL — 조건 안(article_in_condition)
    이면서 숨기지 않은 것만. app.adhoc.renderer가 소제목 아래에 실제로 그리는 목록
    (visible)과 같은 기준이어야 한다: 화면에 안 보이는 기사까지 조용히 건드리면
    담당자가 "6건 숨김"이라고 읽은 버튼이 실제로는 9건을 숨기게 된다.

    app.adhoc.collector가 이 모듈을 import하므로(순환) 판정 함수는 여기서만 지연
    import한다 — app.classifier가 app.curation을 그렇게 쓰는 것과 같은 이유.
    """
    from app.adhoc.collector import article_in_condition

    return [
        a["url"]
        for a in card["articles"]
        if a.get("group") == name and not a.get("hidden") and article_in_condition(a, card)
    ]


def hide_group(card_id: str, name: str) -> Optional[dict]:
    """소제목 하나에 걸린 기사를 한 번에 전부 숨긴다 — 정기 확정본·초안의 소제목 헤더
    🗑️(app.renderer의 hideGroup)와 같은 동작이고, 결과도 "기사를 하나씩 숨긴 것"과
    똑같다(좌하단 「목록 밖 기사」에서 하나씩 복구, ↩ 되돌리기로 한 번에 복구).

    정기는 화면 JS가 기사마다 /hide-article을 따로 부르고 되돌리기를 3초 coalesce로
    억지로 한 스텝에 묶지만, 수시는 카드가 파일 하나라 여기서 한 번에 뒤집으면 된다 —
    부분 실패가 없고 되돌리기도 자연히 1스텝이다.

    **화면에 보이는 기사만 숨긴다**(visible_group_urls) — 조건 밖 기사는 이미 목록
    밖이라 여기서 hidden까지 씌우면, 나중에 조건을 되돌렸을 때 원래대로 돌아와야 할
    기사가 담당자가 본 적 없는 이유로 계속 안 보인다(ADHOC_DESIGN.md §6.4 "교체이지
    삭제가 아니다").
    """
    with card_lock(card_id):
        c = load_card(card_id)
        if c is None:
            return None
        targets = set(visible_group_urls(c, name))
        if not targets:
            return c
        for a in c["articles"]:
            if a["url"] in targets:
                mark_hidden(a)
        save_card(c)
        return c


def visible_urls_among(card: dict, urls: list[str]) -> list[str]:
    """urls 중 **지금 화면에 보이는** 기사만 — 조건 안이면서 숨기지 않은 것.
    visible_group_urls와 같은 기준이다(조건 밖 기사에 hidden을 씌우면 조건을 되돌렸을 때
    담당자가 본 적 없는 이유로 안 돌아온다)."""
    from app.adhoc.collector import article_in_condition

    wanted = set(urls)
    return [
        a["url"]
        for a in card["articles"]
        if a["url"] in wanted and not a.get("hidden") and article_in_condition(a, card)
    ]


def hide_articles(card_id: str, urls: list[str]) -> Optional[dict]:
    """[추가: 2026-09-15] 체크박스로 고른 기사 여러 건을 한 번에 숨긴다 — 선택 바의
    「🗑 숨기기」(정기 확정본·초안의 bulkHideSelected와 같은 자리). hide_group과 같은
    방식이라 한 번의 락 안에서 끝나고, 되돌리기(↩)도 자연히 한 걸음이다."""
    with card_lock(card_id):
        c = load_card(card_id)
        if c is None:
            return None
        targets = set(visible_urls_among(c, urls))
        if not targets:
            return c
        for a in c["articles"]:
            if a["url"] in targets:
                mark_hidden(a)
        save_card(c)
        return c


def ordered_group_names(card: dict) -> list[str]:
    """지금 화면에 실제로 보이는 소제목 순서를 그대로 계산한다 — 기사에서 처음 등장한
    순서 + 아직 기사가 없는 custom_groups를 그 뒤에 붙인 "기본 순서"에, 담당자가
    조정해둔 group_order가 있으면 그 순서를 우선 적용한다(정기 app.group_order와
    같은 개념 — 순서 조정을 아직 안 했으면 빈 리스트라 아무 효과가 없다).

    app.adhoc.renderer._article_groups가 소제목을 묶어 보여줄 때 쓰는 것과 반드시
    같은 규칙이어야 한다 — 그래서 그 로직을 여기 한 곳에만 두고 renderer는 이 함수를
    그대로 가져다 쓴다(두 곳에 각자 구현하면 미묘하게 어긋날 위험이 있다).
    """
    seen: set = set()
    named: list[str] = []
    for a in card["articles"]:
        g = a.get("group")
        if g and g not in seen:
            seen.add(g)
            named.append(g)
    for name in card["custom_groups"]:
        if name not in seen:
            seen.add(name)
            named.append(name)
    if card.get("group_order"):
        ranked = {name: i for i, name in enumerate(card["group_order"])}
        named.sort(key=lambda n: ranked.get(n, len(ranked)))
    return named


def move_group_order(card_id: str, name: str, direction: str) -> Optional[dict]:
    """소제목 자체의 표시 순서를 이웃과 맞바꾼다(기사 순서와는 별개 — 정기의
    app.group_order.save_group_order와 같은 개념). 결과를 card["group_order"]에
    전체 순서로 저장해두면, 나중에 새 소제목이 생겨도 이 목록에 없는 이름은
    ordered_group_names의 규칙대로 자동으로 맨 뒤에 붙는다.

    맨 위/아래에 닿았거나 그 이름의 소제목이 없으면 조용히 그대로 둔다
    (app.adhoc.card.move_article_order와 같은 경계 처리).
    """
    with card_lock(card_id):
        c = load_card(card_id)
        if c is None:
            return None
        order = ordered_group_names(c)
        if name not in order:
            return c
        pos = order.index(name)
        swap_pos = pos - 1 if direction == "up" else pos + 1
        if swap_pos < 0 or swap_pos >= len(order):
            return c
        order[pos], order[swap_pos] = order[swap_pos], order[pos]
        c["group_order"] = order
        save_card(c)
        return c


def remove_custom_group(card_id: str, name: str) -> dict:
    """담당자가 직접 만든 소제목을 지운다 — 정기의 "+ 새 소제목 만들기"가 빈 소제목의
    헤더 🗑️를 "이 빈 소제목 삭제"로 쓰는 것과 같은 조건: **화면에 보이는 기사가
    하나도 없을 때만** 지울 수 있다(visible_group_urls). 보이는 기사가 있으면 그
    기사들을 어디로 보낼지 정하지 않은 채 조용히 사라지게 둘 수 없으므로, 담당자가
    먼저 "다른 소제목"으로 다 옮기거나 숨긴 뒤 지우게 한다.

    add_custom_group과 짝을 이루는 함수라 실패 사유도 같은 방식(AdhocCardError)으로
    돌려준다 — routes.py가 화면에 그대로 보여줄 수 있게.
    """
    with card_lock(card_id):
        c = load_card(card_id)
        if c is None:
            raise AdhocCardError(f"카드를 찾을 수 없습니다: {card_id}")
        if name not in c["custom_groups"]:
            raise AdhocCardError(f'"{name}" 소제목을 찾을 수 없습니다.')
        if visible_group_urls(c, name):
            raise AdhocCardError('기사가 있는 소제목은 지울 수 없습니다 — 먼저 다른 소제목으로 옮겨주세요.')
        # [수정: 2026-09-02] 막는 기준을 "이 소제목에 배정된 기사가 하나라도 있는가"에서
        # "**화면에 보이는** 기사가 있는가"로 좁혔다 — 화면은 숨김·조건 밖을 뺀 목록으로
        # 🗑️를 "빈 소제목 삭제"로 바꿔 그리는데(app.adhoc.renderer) 서버만 숨긴 기사까지
        # 세고 있어서, 기사를 다 숨긴 커스텀 소제목은 버튼이 보이는데 누르면 에러가 났다.
        # 남은(숨김·조건 밖) 기사는 지우지 않고 미분류로 풀어둔다 — 없어진 소제목 이름을
        # 계속 달고 있으면 나중에 복구하거나 조건을 되돌렸을 때 갈 곳이 사라진다.
        for a in c["articles"]:
            if a.get("group") == name:
                a["group"] = None
        c["custom_groups"].remove(name)
        c["group_order"] = [n for n in c["group_order"] if n != name]
        c["group_summaries"].pop(name, None)
        save_card(c)
        return c
