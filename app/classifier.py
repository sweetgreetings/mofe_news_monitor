# Design Ref: PRD.md 기능2 규칙 1·2·3·7 — 제목 분석으로 소제목(최대 5개) 자동 생성·배정 (규칙 기반, LLM 없음)
from collections import Counter, OrderedDict
from typing import Optional

from app.config import DEFAULT_KEYWORDS, MAX_SUBHEADINGS
from app.tokenizer import STOPWORDS, tokenize


def _assign(articles: list, token_sets: list, topic_words: list) -> tuple:
    """기사를 topic_words 중 (빈도순으로) 처음 걸리는 소제목에 배정한다.

    반환: (OrderedDict[소제목 -> 기사 리스트], 어디에도 안 걸린 기사 리스트).
    입력 순서(언론사 우선순위)를 유지하며 배정한다 (PRD 규칙 7).
    """
    groups = OrderedDict((w, []) for w in topic_words)
    leftover = []
    for article, tokens in zip(articles, token_sets):
        chosen = next((w for w in topic_words if w in tokens), None)
        if chosen is not None:
            groups[chosen].append(article)
        else:
            leftover.append(article)
    # 아무 기사도 못 담은 소제목은 버린다.
    used = OrderedDict((w, arts) for w, arts in groups.items() if arts)
    return used, leftover


def _apply_forced_groups(
    groups: list, forced_groups: dict, order_index: dict, custom_group_names: Optional[list] = None
) -> list:
    """사용자가 소제목 경계를 넘어 수동으로 옮긴 기사를 지정된 소제목으로 강제 이동한다
    (PRD.md 기능1 규칙 21 — ↑/↓로 소제목 자체를 바꾸는 경우).

    지정된 소제목이 이번 회차 자동 분류 결과에 없으면(예: 그 사이 관련 기사가 다
    숨겨지거나 삭제돼 그 소제목 자체가 사라짐) 조용히 무시하고 자동 분류를 그대로
    둔다 — 사라진 소제목을 억지로 되살리지 않는다(자기 치유적 동작).

    [수정: 2026-07-30] 강제로 옮긴 기사를 예전엔 target["articles"]의 맨 끝에 무조건
    append했다 — 그래서 소제목을 넘어간 기사는 항상 그 소제목 맨 아래에 꽂히고, 그 뒤
    ↑로 아무리 옮기려 해도(app.curation.move_article이 같은 소제목 내 순서를 바꿔
    저장해도) classify_articles가 다시 호출될 때마다 이 함수가 또 맨 끝으로 되돌려놔서
    사실상 그 기사만 위/아래 조정이 먹통이 되는 버그였다("그룹 내 기사 순서는 입력
    순서를 유지한다"는 원칙이 강제 이동 기사에는 적용되지 않았던 것). order_index
    (원본 articles 리스트에서의 위치)로 최종 정렬해, 강제 이동 기사도 다른 기사와
    똑같이 "입력 순서 유지" 원칙을 따르게 한다 — 이후 위/아래 이동이 입력 순서를 바꾸는
    방식(같은 소제목 내 스왑)으로 저장되면 다음 렌더링에도 그 순서가 그대로 반영된다.

    [추가: 2026-07-30] custom_group_names(app.custom_groups로 사용자가 "+ 새 소제목
    만들기"로 미리 만들어둔 이름)는 위 "자동 분류 결과에 없으면 무시" 규칙의 예외다 —
    자동 분류로는 절대 안 나오는 이름이라 항상 "결과에 없는" 상태일 텐데, 그렇다고
    무시해버리면 사용자가 일부러 만든 소제목으로는 기사를 영영 옮길 수 없게 된다.
    이 이름들은 미리 빈 소제목으로 끼워넣어 두고, 끝까지 아무 기사도 안 들어오면
    마지막 필터(어차피 빈 그룹은 버림)에서 자연히 걸러진다.
    """
    groups = list(groups)
    if custom_group_names:
        existing = {g["name"] for g in groups}
        for name in custom_group_names:
            if name not in existing:
                groups.append({"name": name, "articles": []})
                existing.add(name)

    group_by_name = {g["name"]: g for g in groups}
    for url, target_name in forced_groups.items():
        target = group_by_name.get(target_name)
        if target is None:
            continue
        moved = None
        for g in groups:
            for a in g["articles"]:
                if a["url"] == url:
                    moved = a
                    break
            if moved is not None:
                g["articles"] = [a for a in g["articles"] if a["url"] != url]
                break
        if moved is not None and moved not in target["articles"]:
            target["articles"].append(moved)
            target["articles"].sort(key=lambda a: order_index[a["url"]])
    return [g for g in groups if g["articles"]]


def classify_articles(
    articles: list,
    keywords: Optional[list] = None,
    max_subheadings: int = MAX_SUBHEADINGS,
    forced_groups: Optional[dict] = None,
    custom_group_names: Optional[list] = None,
) -> list:
    """기사 목록을 소제목별로 분류한다.

    반환: [{"name": 소제목, "articles": [기사, ...]}, ...] (최대 max_subheadings개).
    각 기사는 정확히 하나의 소제목에만 담기며, 어디에도 안 걸리는 기사는 "기타"로 모은다.
    소제목 순서는 빈도 높은 순, "기타"는 항상 맨 뒤. 그룹 내 기사 순서는 입력 순서를 유지한다.

    [수정: 2026-07-24] 분류 기준을 기사 제목이 아니라 네이버 요약(description)으로 바꿨다.
    제목은 언론사마다 표현이 제각각이라 같은 사건을 다뤄도 겹치는 단어가 잘 안 잡히는데,
    요약문은 보도자료 내용을 비슷한 문장으로 옮기는 경우가 많아 "○○회의" 같은 공통
    단어가 더 잘 드러난다. 요약이 비어 있으면 제목으로 대신한다.

    forced_groups: {기사 url: 소제목 이름} — 자동 분류 결과와 무관하게 이 소제목으로
    강제 이동한다(규칙21). 자동 분류를 모두 마친 뒤 마지막에 적용한다.
    custom_group_names: [추가: 2026-07-30] 사용자가 "+ 새 소제목 만들기"로 직접 만든
    소제목 이름 목록(app.custom_groups) — 자동 분류로는 절대 안 나오는 이름이라
    _apply_forced_groups에 미리 빈 그룹으로 끼워 넣어야 이 소제목으로의 강제 이동이
    (자동 분류 결과에 없다는 이유로) 조용히 무시되지 않는다.
    """
    if not articles:
        return []

    keywords = keywords if keywords is not None else DEFAULT_KEYWORDS
    # 검색 키워드는 거의 모든 제목/요약에 있어 소제목으로 쓸모없으므로 불용어에 포함한다.
    stopwords = STOPWORDS | {k.lower() for k in keywords}

    token_sets = [tokenize(a.get("summary") or a["title"], stopwords) for a in articles]

    # 문서 빈도(몇 개 제목에 등장했는가) 2 이상인 단어만 소제목 후보로 삼는다.
    doc_freq = Counter(word for tokens in token_sets for word in tokens)
    first_seen = {}
    for i, tokens in enumerate(token_sets):
        for word in tokens:
            first_seen.setdefault(word, i)
    # 빈도·최초등장이 같을 때 단어 자체를 마지막 tie-break로 두어, set 순회 순서
    # (PYTHONHASHSEED)에 상관없이 항상 같은 결과가 나오도록 한다.
    candidates = sorted(
        (w for w, c in doc_freq.items() if c >= 2 and w != "기타"),
        key=lambda w: (-doc_freq[w], first_seen[w], w),
    )

    # 소제목 후보 개수를 줄여가며, 총 그룹 수(소제목 + 기타)가 최대치 이하가 되는 최대 구성을 찾는다.
    for num_topics in range(min(len(candidates), max_subheadings), -1, -1):
        used, leftover = _assign(articles, token_sets, candidates[:num_topics])
        total_groups = len(used) + (1 if leftover else 0)
        if total_groups <= max_subheadings:
            break

    result = [{"name": w, "articles": arts} for w, arts in used.items()]
    if leftover:
        result.append({"name": "기타", "articles": leftover})
    if forced_groups:
        order_index = {a["url"]: i for i, a in enumerate(articles)}
        result = _apply_forced_groups(result, forced_groups, order_index, custom_group_names)
    return result


def snapshot_group_names(
    articles: list,
    keywords: Optional[list] = None,
    forced_groups: Optional[dict] = None,
    custom_group_names: Optional[list] = None,
) -> list:
    """지금 이 순간의 소제목 분류 결과를 각 기사에 "group" 필드로 붙여 돌려준다
    (저장된 회차가 나중에 "지난 기사 더보기"에서 당시 소제목 구성 그대로 복원할 수
    있도록 — app.history_renderer, app.scraper.collect_run, app.settings_server의
    최신 회차 큐레이션 핸들러 참고).

    최신 회차는 화면에 보이는 동안 재분류가 계속 다시 일어나므로(app.renderer.render_page가
    매번 classify_articles를 새로 부름), 이 스냅샷은 "지금 저장하는 시점"의 결과일 뿐이다 —
    이후 이 회차가 최신 회차로 남아있는 동안 숨기기·이동 등으로 다시 바뀌면, 그때마다
    호출하는 쪽이 이 함수를 다시 불러 저장된 값을 최신으로 갱신해야 한다.
    """
    groups = classify_articles(articles, keywords, forced_groups=forced_groups, custom_group_names=custom_group_names)
    return [{**a, "group": g["name"]} for g in groups for a in g["articles"]]
