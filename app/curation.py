# Design Ref: PRD.md 기능1 규칙 19·21, 기능2 규칙 8 — 기사 숨김/되돌리기, 소제목 순서·경계 넘나들기, 소제목 이름 바꾸기
import json
from datetime import datetime
from typing import Optional, Tuple

from app.atomic_write import atomic_write_text
from app.classifier import classify_articles
from app.config import GROUP_LABELS_FILE, GROUP_OVERRIDES_FILE, HIDDEN_ARTICLES_FILE
from app.custom_groups import load_custom_groups


def _today_str(now: Optional[datetime] = None) -> str:
    return (now or datetime.now()).strftime("%Y-%m-%d")


def _load_records(now: Optional[datetime] = None) -> list:
    """오늘 숨긴 기록만 [{"url":..., "hidden_at":...}, ...] 형태로 읽어온다.

    [수정: 2026-07-26] 익일 0시가 지나면 전부 비워진다(사용자 요청) — 숨김은 "오늘
    화면만 정리하는" 용도라 다음날까지 남겨둘 필요가 없다는 판단. app.manual_articles와
    같은 패턴으로, 파일을 그 자리에서 지우진 않고 읽을 때 오늘 것만 걸러내며, 다음
    hide_article/unhide_article 호출 때 오늘치로 자연히 덮어써진다. 옛 형식(평평한
    URL 문자열 목록)은 애초에 언제 숨겼는지 알 수 없어 이 새 정책상 그냥 버려진다
    (오늘 것이라고 확신할 수 없으므로).
    """
    if not HIDDEN_ARTICLES_FILE.exists():
        return []
    try:
        raw = json.loads(HIDDEN_ARTICLES_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    if raw and isinstance(raw[0], str):
        return []
    today = _today_str(now)
    return [record for record in raw if record.get("hidden_at", "").startswith(today)]


def _write_records(records: list) -> None:
    atomic_write_text(HIDDEN_ARTICLES_FILE, json.dumps(records, ensure_ascii=False, indent=2))


def load_hidden_urls(now: Optional[datetime] = None) -> set:
    """숨긴 기사의 URL 집합을 읽어온다(순서·시각과 무관 — filter_hidden의 빠른 포함 여부 확인용)."""
    return {record["url"] for record in _load_records(now)}


def load_hidden_records(now: Optional[datetime] = None) -> list:
    """오늘 숨긴 기록을 최근에 숨긴 순서대로(hidden_at 내림차순) 돌려준다.

    "숨긴 기사 관리" 화면 전용 — filter_hidden처럼 포함 여부만 필요한 곳은
    load_hidden_urls()의 set을 그대로 쓰면 된다.
    """
    return sorted(_load_records(now), key=lambda record: record["hidden_at"], reverse=True)


def hide_article(
    url: str,
    now: Optional[datetime] = None,
    outlet: Optional[str] = None,
    title: Optional[str] = None,
    pub_date: Optional[str] = None,
) -> None:
    """기사 하나를 숨김 처리한다. 원본 회차 JSON은 그대로 두고, 화면에 그릴 때만 제외한다.

    [추가: 2026-07-27] outlet/title/pub_date를 넘기면 숨긴 기록에 같이 저장한다 —
    "다음 회차 초안"(app.preview_renderer)에서 숨긴 기사는 정식 회차로 저장된 적이 없어
    "숨긴 기사 관리" 화면이 기존 방식(정식 회차에서 URL로 역조회, app.settings_server
    ._known_articles_by_url)으로는 언론사·제목을 못 찾아 URL만 보였다 — 숨기는 시점에
    화면에 이미 떠 있는 정보를 그대로 실어 보내면 어디서 숨겼든 항상 보여줄 수 있다.
    생략하면(기존 호출부·정식 회차에서 숨긴 경우) None으로 저장되고, 화면 쪽이 그때는
    기존 방식대로 정식 회차 역조회로 대체한다(하위 호환).
    """
    records = _load_records(now)
    if not any(record["url"] == url for record in records):
        # [수정: 2026-07-26] 초 단위(timespec="seconds")가 아니라 마이크로초까지 그대로
        # 남긴다 — 사용자가 여러 기사를 1초 안에 연달아 숨기면 초 단위로는 시각이 같아져,
        # 정렬(내림차순)이 안정 정렬 특성상 오히려 먼저 숨긴 게 위로 가는 사고가 난다.
        records.append(
            {
                "url": url,
                "hidden_at": (now or datetime.now()).isoformat(),
                "outlet": outlet,
                "title": title,
                "pub_date": pub_date,
            }
        )
        _write_records(records)


def unhide_article(url: str, now: Optional[datetime] = None) -> None:
    """숨김을 해제해 다시 화면에 보이게 한다."""
    records = [record for record in _load_records(now) if record["url"] != url]
    _write_records(records)


def load_group_overrides() -> dict:
    """소제목 경계를 넘어 수동으로 옮긴 기사의 강제 소제목 기록을 읽어온다.

    {기사 url: 소제목 이름} 형태. 파일이 없거나 손상됐으면 빈 dict로 취급한다.
    """
    if not GROUP_OVERRIDES_FILE.exists():
        return {}
    try:
        return json.loads(GROUP_OVERRIDES_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, TypeError):
        return {}


def _write_overrides(overrides: dict) -> None:
    atomic_write_text(GROUP_OVERRIDES_FILE, json.dumps(overrides, ensure_ascii=False, indent=2))


def set_group_override(url: str, group_name: str) -> None:
    """기사 하나를 특정 소제목에 강제로 배정한다 (PRD.md 기능1 규칙 21)."""
    overrides = load_group_overrides()
    overrides[url] = group_name
    _write_overrides(overrides)


def move_article(
    articles: list, keywords: Optional[list], url: str, direction: str, overrides: Optional[dict] = None
) -> Tuple[list, Optional[tuple]]:
    """기사 하나를 위/아래로 옮긴다 (PRD.md 기능1 규칙 21).

    같은 소제목 안이면 그 기사와 이웃의 위치를 원본 리스트에서 맞바꿔 순서만 바꾼다
    (다른 소제목은 전혀 건드리지 않는다). 소제목의 맨 위/아래에 닿으면, 한 번 더
    누를 때 바로 앞/뒤 소제목으로 기사를 통째로 옮긴다 — 이건 순서가 아니라 분류
    자체를 바꾸는 것이라 즉시 리스트를 바꿀 수 없고, "이 기사는 이제 이 소제목"이라는
    강제 배정을 새로 만들어야 다음 렌더링에도 유지된다.

    overrides: 지금까지 쌓인 강제 배정 기록. classify_articles에 그대로 반영해 "현재
    화면에 실제로 보이는 소제목 구성" 기준으로 위/아래를 판단한다.

    [수정: 2026-07-27] classify_articles에는 articles를 그대로 넘기지 않고
    filter_hidden(articles)를 넘긴다 — 안 그러면 숨긴 기사가 여전히 같은 소제목의
    "보이지 않는 이웃"으로 남아있어, 화면에는 기사가 1개만 보이는 소제목인데도
    백엔드는 여전히 2개짜리로 보고 "같은 소제목 내 순서 변경"으로 처리해버린다 —
    그 순서 변경 대상이 숨긴(안 보이는) 기사라 화면엔 아무 변화가 없어 "이동이 안 된다"는
    버그가 났다. 소제목 경계 판단(group_index/target_index)은 항상 화면에 보이는
    구성 기준이어야 하고, 실제 순서를 맞바꿀 때는(아래) 원본 articles 전체에서 위치를
    찾으므로 숨긴 기사도 원래 저장 순서 그대로 남는다(삭제되지 않는다).
    반환: (new_articles, new_override).
      - new_override가 None이면 new_articles만 반영하면 된다 (같은 소제목 내 순서 변경).
      - new_override가 (url, 소제목이름) 튜플이면, 호출하는 쪽이 set_group_override로
        저장해야 한다 (소제목 경계를 넘은 경우). 이때 new_articles는 원본과 동일하다.
      - 더 옮길 곳이 없거나 url을 못 찾으면 (articles, None)을 그대로 돌려준다.
    """
    groups = classify_articles(
        filter_hidden(articles), keywords, forced_groups=overrides, custom_group_names=load_custom_groups()
    )
    group_index = next(
        (i for i, g in enumerate(groups) if any(a["url"] == url for a in g["articles"])), None
    )
    if group_index is None:
        return articles, None

    group_urls = [a["url"] for a in groups[group_index]["articles"]]
    pos = group_urls.index(url)
    swap_with = pos - 1 if direction == "up" else pos + 1

    if 0 <= swap_with < len(group_urls):
        other_url = group_urls[swap_with]
        index_by_url = {a["url"]: i for i, a in enumerate(articles)}
        i, j = index_by_url[url], index_by_url[other_url]
        new_articles = list(articles)
        new_articles[i], new_articles[j] = new_articles[j], new_articles[i]
        return new_articles, None

    target_index = group_index - 1 if direction == "up" else group_index + 1
    if not (0 <= target_index < len(groups)):
        return articles, None  # 맨 처음/맨 마지막 소제목이라 더 옮길 곳이 없음

    return articles, (url, groups[target_index]["name"])


def bulk_reassign_group(urls: list, target_group: str) -> None:
    """체크박스로 선택한 기사 여러 개를 한 번에 target_group으로 옮긴다
    (스크랩 초안의 "선택한 기사 옮기기" 하단 바, 기사별 "다른 소제목으로" 드롭다운).

    move_article과 달리 "이웃과 스왑"이 아니라 "그냥 이 소제목으로 배정"이라 순서
    계산이 필요 없다 — 이미 있는 set_group_override를 URL 개수만큼 반복 호출하면
    끝이고, 그 안에서 몇 번째로 보일지는(입력 순서 유지 원칙) app.classifier
    ._apply_forced_groups가 알아서 정한다.
    """
    for url in urls:
        if url:
            set_group_override(url, target_group)


def load_group_labels() -> dict:
    """자동 생성된 소제목 단어(topic word)에 사용자가 붙인 표시용 이름을 읽어온다.

    {소제목 단어: 표시할 이름} 형태. 분류 로직 자체(어떤 기사가 어느 그룹인지)는 항상
    원래 소제목 단어로만 판단하고, 이 이름표는 화면에 보여줄 때만 마지막에 적용한다 —
    그래야 소제목 경계 넘나들기(move_article)의 대상 지정도 계속 원래 단어 기준으로
    안정적으로 동작한다.
    """
    if not GROUP_LABELS_FILE.exists():
        return {}
    try:
        return json.loads(GROUP_LABELS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, TypeError):
        return {}


def _write_labels(labels: dict) -> None:
    atomic_write_text(GROUP_LABELS_FILE, json.dumps(labels, ensure_ascii=False, indent=2))


def set_group_label(topic_word: str, label: str) -> None:
    """소제목 단어의 표시 이름을 정한다 (PRD.md 기능2 규칙 8).

    이 단어가 나중에 다른 회차에서도 소제목으로 다시 잡히면 같은 이름표가 계속
    적용된다. 원래 단어와 똑같은 이름으로 "바꾸면" 이름표를 지운 것으로 본다
    (되돌리기를 별도 버튼 없이 자연스럽게 처리).
    """
    labels = load_group_labels()
    if label == topic_word:
        labels.pop(topic_word, None)
    else:
        labels[topic_word] = label
    _write_labels(labels)


def display_group_name(name: str, labels: dict) -> str:
    """소제목 단어를 화면에 보여줄 이름으로 바꾼다 (이름표가 없으면 원래 단어 그대로)."""
    return labels.get(name, name)


def filter_hidden(articles: list) -> list:
    """숨긴 기사를 제외한 목록을 돌려준다.

    화면 렌더링·복사용 텍스트·키워드 추출·워드클라우드 등 기사 목록을 쓰는 모든 곳이
    이 함수를 거친 뒤의 목록만 사용해야, 숨긴 기사가 어디에도 다시 새어나가지 않는다.
    """
    hidden = load_hidden_urls()
    return [a for a in articles if a["url"] not in hidden]
