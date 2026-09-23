# Design Ref: PRD.md 기능2 규칙 1·2·3·7 — 제목 분석으로 소제목(최대 5개) 자동 생성·배정 (규칙 기반, LLM 없음)
from collections import Counter, OrderedDict
from typing import Optional

from app.assigned_groups import load_assigned_groups
from app.config import DEFAULT_KEYWORDS, MAX_SUBHEADINGS
from app.custom_groups import load_custom_groups
from app.llm_classifier import (
    UNCLASSIFIED_GROUP_NAME,
    assign_to_existing,
    build_assign_candidates,
    cached_group_layout,
    classify_with_llm,
    seed_cache,
)
from app.tokenizer import STOPWORDS, tokenize


def forced_group_target_names() -> list:
    """classify_articles/classify_for_finalize의 custom_group_names로 넘길 이름 전체 —
    담당자가 "+ 새 소제목 만들기"로 직접 만든 소제목(app.custom_groups) + "🤖 미분류
    배정"이 새로 지어낸 소제목(app.assigned_groups). _apply_forced_groups는 이 목록에
    없는 이름으로의 강제 이동을 조용히 무시하므로, 두 출처를 합쳐 넘겨야 사용자·AI 어느
    쪽이 만든 소제목이든 다음 렌더링에서 사라지지 않는다.
    """
    return load_custom_groups() + load_assigned_groups()


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
    groups: list,
    forced_groups: dict,
    order_index: dict,
    custom_group_names: Optional[list] = None,
    revivable_groups: Optional[list] = None,
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

    [수정: 2026-09-17] revivable_groups(app.llm_classifier.cached_group_layout — 이번 회차
    AI 분류에 있던 [(이름, 요약)])도 위 규칙의 예외다. AI 첫 분류로 들어간 기사가 전부
    숨겨지면 _rebuild가 그 소제목을 빼는데, 「AI 기사 배정」 등으로 그리로 배정된 기사가
    아직 보이면 소제목이 남아야 한다 — 안 그러면 그 기사들이 📂 미분류로 떨어지고 이
    이름에 붙은 이름표·순서까지 함께 사라진다(2026-09-17 14:00 회차 「확대거시경제금융회의」,
    HISTORY.md 참고). 캐시 순서상 원래 자리에 빈 칸으로 끼워두고, 끝까지 비면 마지막
    필터가 버린다 — 가리키는 기사까지 다 숨기면 지금처럼 사라진다. 이번 회차 캐시 이름만
    받으므로 다른 회차의 옛 배정이 이름을 되살리지는 않는다.
    """
    groups = list(groups)
    if revivable_groups:
        present = {g["name"] for g in groups}
        targets = set(forced_groups.values())
        cache_names = [name for name, _ in revivable_groups]
        for idx, (name, summary) in enumerate(revivable_groups):
            if name in present or name not in targets:
                continue
            # 캐시 순서상 바로 앞에 있으면서 지금 목록에도 있는 소제목 뒤에 끼운다.
            pos = 0
            for prev in reversed(cache_names[:idx]):
                if prev in present:
                    pos = next(i for i, g in enumerate(groups) if g["name"] == prev) + 1
                    break
            groups.insert(pos, {"name": name, "articles": [], "summary": summary})
            present.add(name)
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
    allow_llm_call: bool = False,
    force_llm: bool = False,
    round_id: Optional[tuple] = None,
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

    [수정: 2026-08-14] allow_llm_call 기본값을 True에서 False로 뒤집었다 — "사람이 소제목을
    바꾸면 AI가 멋대로 되돌린다"(2026-08-14 09:30 회차 사고)의 근본 원인은 순서 계산·
    숨기기·이동 같은 "그냥 지금 상태를 다시 보여줘"에 불과한 호출들이 이 매개변수를
    아예 안 넘겨서 조용히 "필요하면 AI를 다시 불러도 됨" 권한을 기본값으로 갖고 있었던
    것이다(app.curation.move_article/bulk_move_articles, app.classifier.snapshot_group_names가
    전부 그랬다). 진짜로 AI가 소제목을 새로 지어도 되는
    순간은 정확히 둘뿐이다 — 초안 첫 자동분류(app.preview_renderer, 회차당 1회)와 담당자가
    "🤖/↻ 전체 기사 재분류" 버튼을 직접 눌렀을 때(force_llm=True). 그 두 곳만 이제
    allow_llm_call=True를 명시적으로 넘긴다. 나머지(큐레이션 조작·화면 표시·내보내기)는
    기본값을 그대로 두면 자동으로 안전하다 — "권한 없음"이 기본이고 "권한 있음"은 예외로
    드러나야, 새 호출부를 추가할 때도 실수로 권한이 새지 않는다.

    round_id: [추가: 2026-08-14] 이 분류가 속한 회차 식별자(app.llm_classifier.
    round_id_for_slot/round_id_for_run) — "다음 회차는 전 회차 메모리를 안 쓴다"는
    원칙을 지키려면 호출부가 이걸 넘겨줘야 한다. 안 넘기면(None) 회차 구분 없이
    동작한다(수시 모니터링처럼 회차 개념이 없는 호출부 전용 — 정기/초안 호출부는
    전부 넘긴다).
    """
    if not articles:
        return []

    # [추가: 2026-08-11] 소제목만 Claude에게 맡긴다(app/llm_classifier.py). 키가 없거나
    # 호출이 실패하면 None이 와서 아래 규칙 기반 분류가 그대로 이어진다 — 이 함수의
    # 기존 동작은 하나도 바뀌지 않았고, LLM은 성공했을 때만 앞에서 가로챈다.
    #
    # allow_llm_call/force_llm은 "지금 이 호출이 AI에게 새 소제목을 지어도 되는 순간인가"를
    # 명시하는 스위치다(위 [수정: 2026-08-14] 참고) — 기본값(False)은 "안 된다"이고, 회차
    # 첫 자동분류·재분류 버튼 두 곳만 True를 명시해서 예외로 드러낸다.
    llm_groups = classify_with_llm(articles, max_subheadings, allow_llm_call, force_llm, round_id=round_id)
    if llm_groups is not None:
        if forced_groups:
            order_index = {a["url"]: i for i, a in enumerate(articles)}
            llm_groups = _apply_forced_groups(
                llm_groups,
                forced_groups,
                order_index,
                custom_group_names,
                cached_group_layout(articles, max_subheadings, round_id),
            )
        return llm_groups

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


def classify_for_finalize(
    articles: list,
    keywords: Optional[list] = None,
    max_subheadings: int = MAX_SUBHEADINGS,
    forced_groups: Optional[dict] = None,
    custom_group_names: Optional[list] = None,
    round_id: Optional[tuple] = None,
) -> tuple:
    """회차가 초안에서 확정본으로 넘어가는 시점(app.scraper.collect_run) 전용 분류.

    [추가: 2026-08-13] 예전엔 이 시점에 classify_articles(allow_llm_call=True, 기본값)를
    그대로 불러 **항상 전체를 새로 분류**했다. "직전 이름을 재사용하라"는 프롬프트 부탁이
    있긴 하지만 강제가 아니라서, 초안에서 숨긴 기사를 제외하고 분류했던 기사 구성이
    확정 저장 시점엔(원본 그대로) 달라지면 AI가 "묶음이 실제로 달라졌다"고 판단해 새
    이름을 지어버렸다 — 그 결과 group_order.json/group_labels.json/group_overrides.json에
    쌓인 담당자의 순서·이름표·이동 작업이 통째로 무효가 됐다(2026-08-13 17시 회차 사고,
    HISTORY.md 참고).

    여기서는 먼저 API를 새로 부르지 않고(allow_call=False) 캐시된 초안 분류를 재사용한다
    (_find_reusable/_find_partial, app.llm_classifier). 그 사이 새로 들어온 기사는
    캐시에 없으므로 UNCLASSIFIED_GROUP_NAME 임시 칸에 모이는데, 이걸 그대로 저장하지
    않고 assign_to_existing(app.llm_classifier) — "묶어라"가 아니라 "이미 있는 소제목
    중 하나를 골라라"만 시키는, 이름을 바꿀 방법 자체가 없는 API — 로 기존 소제목에
    끼워 넣는다. /assign-unclassified 버튼(app.settings_server._handle_assign_unclassified)과
    동일한 방식·동일한 안전장치(사용자 커스텀 소제목은 후보 제외, MAX_SUBHEADINGS 넘으면
    "기타")를 쓴다.

    초안에서 한 번도 분류된 적 없는 회차(예: 아무도 열어보지 않은 심야 06:00 회차)는
    재사용할 캐시가 없어 classify_with_llm이 None을 돌려주고, 그때만 지금까지처럼
    classify_articles로 처음부터 분류한다 — 지킬 기존 담당자 작업이 없으니 새 이름이
    나와도 안전하다.

    반환: (groups, new_forced_groups, new_subheading_names). new_forced_groups는 미분류
    기사를 배정하며 새로 생긴 {url: 소제목 이름} — 호출하는 쪽이
    app.curation.set_group_override로 영구 저장해야 다음 회차·재분류에도 이어진다
    (assign_to_existing이 이름을 지어낸 경우 "기존 소제목처럼" 계속 유지되려면 필요).

    [추가: 2026-08-20] new_subheading_names — new_forced_groups를 배정하며 기존에
    없던 이름으로 새로 생긴 소제목만 모은 목록("기타"는 제외 — 오버플로용 받이일 뿐
    의미 있는 새 주제가 아니다). 호출하는 쪽(app.scraper.collect_run)이 회차 파일에
    같이 남겨, 확정본이 "이 소제목은 담당자가 초안에서 못 본 사이 새로 생겼다"를
    화면에 알려주는 데 쓴다(마감 후 자동 배정 표시 — HISTORY.md 참고).

    round_id: [추가: 2026-08-14] app.llm_classifier.round_id_for_run(run)으로 만든 값을
    호출부(app.scraper.collect_run)가 넘긴다 — round_id_for_slot(slot)과 같은 슬롯이면
    같은 값이 나오도록 맞춰뒀으므로, 이 회차 직전 초안의 캐시를 정확히 찾아 재사용한다
    (다른 날·다른 회차의 캐시는 URL이 아무리 겹쳐도 후보에서 제외된다).
    """
    if not articles:
        return [], {}, []
    custom_group_names = list(custom_group_names or [])
    forced_groups = dict(forced_groups or {})

    llm_groups = classify_with_llm(articles, max_subheadings, allow_call=False, round_id=round_id)
    order_index = {a["url"]: i for i, a in enumerate(articles)}
    if llm_groups is not None and forced_groups:
        llm_groups = _apply_forced_groups(
            llm_groups,
            forced_groups,
            order_index,
            custom_group_names,
            cached_group_layout(articles, max_subheadings, round_id),
        )
    # 초안 분류의 기사를 담당자가 전부 숨겼고 배정된 기사로 되살아난 소제목도 없으면 전부
    # 미분류다 — 끼워 넣을 기존 소제목이 없으니 분류가 아예 없던 회차와 같이 처음부터 분류한다.
    # 배정으로 소제목이 하나라도 남으면 초안 화면 그대로 이어 간다.
    if llm_groups is not None and all(g["name"] == UNCLASSIFIED_GROUP_NAME for g in llm_groups):
        llm_groups = None
    if llm_groups is None:
        # [수정: 2026-08-14] 여기서만 allow_llm_call=True를 명시한다 — 초안에서 한 번도
        # 분류된 적 없는 회차(예: 아무도 열어보지 않은 심야 06:00 회차)라 재사용할 캐시가
        # 전혀 없는 경우다(docstring 위쪽 참고). classify_articles의 새 기본값(False)을
        # 그냥 물려받으면 이 회차는 소제목 없이 규칙 기반으로만 떨어지는데, 지킬 기존
        # 담당자 작업이 없는 이 경우엔 처음부터 AI로 분류하는 게 원래 의도였다.
        return (
            classify_articles(
                articles,
                keywords,
                max_subheadings,
                forced_groups,
                custom_group_names,
                allow_llm_call=True,
                round_id=round_id,
            ),
            {},
            [],
        )

    unclassified = next((g for g in llm_groups if g["name"] == UNCLASSIFIED_GROUP_NAME), None)
    new_forced: dict = {}
    # [추가: 2026-08-20] assign_to_existing이 진짜로 새로 지어낸 이름만 모은다("기타"는
    # 상한 초과 시의 도피처일 뿐이라 제외) — 아래 return의 new_subheading_names로 나가,
    # 확정본이 "마감 시 신규 생성" 배지를 붙이는 근거가 된다.
    new_subheading_names: list = []
    if unclassified and unclassified["articles"]:
        # 순환 임포트 회피(app.curation이 이미 app.classifier를 임포트한다) — app.llm_classifier.
        # _build_prompt와 동일한 이유로 지연 임포트한다.
        from app.curation import load_group_labels

        custom_set = set(custom_group_names)
        candidates = build_assign_candidates(
            llm_groups,
            custom_set,
            load_group_labels(round_id),
            {url for url, name in (forced_groups or {}).items() if name not in (UNCLASSIFIED_GROUP_NAME, "기타")},
        )
        existing_names = [c["name"] for c in candidates]
        assigned = assign_to_existing(unclassified["articles"], candidates) if candidates else None

        room = max(max_subheadings - len(existing_names), 0)
        assigned_urls = set()
        for url, name in (assigned or {}).items():
            if name not in existing_names and name not in new_subheading_names:
                if len(new_subheading_names) >= room:
                    name = "기타"
                else:
                    new_subheading_names.append(name)
            new_forced[url] = name
            assigned_urls.add(url)
        # assign_to_existing이 실패했거나(None) 일부 기사를 빠뜨렸으면 남는 기사는
        # "기타"로 흡수한다 — 확정본에 "📂 소제목 미분류" 임시 칸이 그대로 남으면 안 된다
        # (PRD: 그 이름은 화면 전용 임시 칸이지 실제 소제목이 아니다).
        for a in unclassified["articles"]:
            if a["url"] not in assigned_urls:
                new_forced[a["url"]] = "기타"

        llm_groups = _apply_forced_groups(
            llm_groups, new_forced, order_index, custom_group_names + new_subheading_names + ["기타"]
        )

    # [추가: 2026-08-13] 이 결과를 확정본 전체 기사 구성 그대로 캐시에 심어둔다 — 이후
    # 확정본에서 기사를 숨기거나 옮기는 첫 조작(app.settings_server._handle_move_article 등)
    # 이 API를 새로 부르지 않고 이 결과를 그대로 재사용하게 한다 — seed_cache docstring 참고.
    # [수정: 2026-08-14] round_id를 그대로 넘긴다 — 안 그러면 여기서 심는 항목의 round_id가
    # None이 되어, 큐레이션 핸들러가 round_id_for_run(run)으로 조회할 때 못 찾는다.
    seed_cache(articles, llm_groups, max_subheadings, round_id=round_id)

    return llm_groups, new_forced, new_subheading_names


def groups_from_snapshot(articles: list, custom_group_names: Optional[list] = None) -> list:
    """각 기사에 저장돼 있는 "group" 필드만 읽어 소제목 묶음을 복원한다 — 재분류하지 않는다.

    [추가: 2026-08-12] 확정본(app.renderer.render_page)이 쓴다. 예전엔 확정본도 렌더링할
    때마다 classify_articles를 다시 불렀는데, 그게 실제 사고를 냈다: 담당자가 ✏️로 소제목
    이름을 바꾸면 그 저장 핸들러가 화면을 다시 그리고 → 그 과정에서 재분류가 일어나 →
    LLM이 새 이름을 지어버려 → 방금 저장한 이름표가 사라진 옛 이름에 묶여 고아가 됐다.
    (특히 앱 재시작 직후엔 LLM 캐시가 비어 있어 진짜 API 호출이 나가므로 거의 확실히 발생.)

    회차 파일에는 이미 답이 들어 있다 — app.scraper.collect_run이 저장할 때, 그리고 기사를
    옮기거나 숨길 때마다 각 큐레이션 핸들러가 snapshot_group_names로 "group"을 갱신한다.
    즉 저장된 값은 항상 최신이고, 확정본은 그걸 그대로 읽기만 하면 된다("지난 기사"
    화면(app.history_renderer)이 이미 이 방식으로 동작해 왔고, 그래서 그쪽은 이 문제가
    없었다). 재분류는 담당자가 "🤖 전체 기사 재분류"를 직접 눌렀을 때만 일어난다.

    custom_group_names: "+ 새 소제목"으로 만들어둔 빈 소제목 — 기사가 하나도 없어서
    스냅샷에는 안 나타나므로, 여기서 빈 묶음으로 따로 끼워넣어야 화면에 계속 보이고
    기사를 옮길 목적지로도 남는다(_apply_forced_groups가 하던 일과 같은 이유).

    [수정: 2026-08-12] AI_RULES.md 참고 — 소제목별 요약도 이제 LLM이 분류와 같은 호출에서
    쓰므로, 재분류하지 않는 이 화면이 그 요약을 보여주려면 요약도 같이 스냅샷돼 있어야
    한다. 요약은 그룹 단위지만 저장은 기사 단위(각 기사의 "group_summary" 필드, snapshot_
    group_names가 붙임)라 그룹의 첫 기사에서 읽어 복원한다 — "group"과 똑같은 이유
    (회차 파일 구조를 기사 배열 하나로 단순하게 유지하려고)로 같은 자리에 얹었다.
    """
    grouped: "OrderedDict[str, list]" = OrderedDict()
    for article in articles:
        grouped.setdefault(article["group"], []).append(article)
    for name in custom_group_names or []:
        grouped.setdefault(name, [])
    return [
        {"name": name, "articles": items, "summary": items[0].get("group_summary", "") if items else ""}
        for name, items in grouped.items()
    ]


def groups_for_confirmed_run(
    articles: list,
    keywords: Optional[list] = None,
    forced_groups: Optional[dict] = None,
    custom_group_names: Optional[list] = None,
    round_id: Optional[tuple] = None,
) -> list:
    """확정된 회차의 소제목 묶음을 얻는다 — 저장된 스냅샷이 있으면 재분류하지 않고 그대로 쓴다.

    [추가: 2026-08-12] 확정본 화면(app.renderer.render_page)과 발송/복사 텍스트
    (latest_confirmed_report)가 **둘 다** 이걸 써야 한다. 한쪽만 스냅샷을 읽으면 화면과
    실제로 나가는 내용이 서로 다른 소제목으로 갈릴 수 있다.

    "group" 필드가 없는 옛 회차(이 스냅샷 기능이 생기기 전에 저장된 것)는 예전처럼
    분류해서 보여준다 — 하위 호환.

    [수정: 2026-08-14] allow_llm_call=False를 명시한다 — 이건 화면 표시(읽기)일 뿐이고,
    화면을 열어보는 행위가 AI에게 새 소제목을 지을 권한을 주면 안 된다("담당자가 소제목을
    바꿔도 화면만 새로 그리면 AI가 되돌린다"는 사고의 재발 지점이 될 수 있는 곳이라
    기본값에 기대지 않고 여기서도 눈에 보이게 적어둔다).
    """
    if articles and all("group" in a for a in articles):
        return groups_from_snapshot(articles, custom_group_names)
    return classify_articles(
        articles,
        keywords,
        forced_groups=forced_groups,
        custom_group_names=custom_group_names,
        allow_llm_call=False,
        round_id=round_id,
    )


def snapshot_group_names(
    articles: list,
    keywords: Optional[list] = None,
    forced_groups: Optional[dict] = None,
    custom_group_names: Optional[list] = None,
    force_llm: bool = False,
    round_id: Optional[tuple] = None,
) -> list:
    """지금 이 순간의 소제목 분류 결과를 각 기사에 "group" 필드로 붙여 돌려준다
    (저장된 회차가 나중에 "지난 기사 더보기"에서 당시 소제목 구성 그대로 복원할 수
    있도록 — app.history_renderer, app.scraper.collect_run, app.settings_server의
    최신 회차 큐레이션 핸들러 참고).

    최신 회차는 화면에 보이는 동안 재분류가 계속 다시 일어나므로(app.renderer.render_page가
    매번 classify_articles를 새로 부름), 이 스냅샷은 "지금 저장하는 시점"의 결과일 뿐이다 —
    이후 이 회차가 최신 회차로 남아있는 동안 숨기기·이동 등으로 다시 바뀌면, 그때마다
    호출하는 쪽이 이 함수를 다시 불러 저장된 값을 최신으로 갱신해야 한다.

    force_llm: [추가: 2026-08-11] 확정본의 "🤖 소제목 분류 요청하기" 버튼 전용 — 캐시를
    무시하고 LLM을 다시 호출한다(app.settings_server._handle_regenerate_subheadings_final).
    다른 모든 호출부(정기 수집·큐레이션 핸들러)는 기본값 False다 — 이 함수를 부르는
    쪽은 대부분 "지금 이 순간의 분류 결과를 저장해두는" 큐레이션 액션(숨기기·이동·
    이름변경 뒤 재스냅샷)이라, AI가 소제목을 새로 지을 권한이 없어야 한다([수정:
    2026-08-14] 아래 참고 — allow_llm_call=False를 명시적으로 넘긴다). force_llm=True일
    때만 그 권한이 살아난다(app.llm_classifier.classify_with_llm이 force를 allow_call보다
    우선하도록 되어 있다).

    [수정: 2026-08-14] **분류에는 숨긴 기사를 넣지 않는다** — app.scraper.collect_run과
    똑같은 입력(filter_hidden을 거친 목록)이 되게 맞춘 것으로, 2026-08-14 09:30 회차
    사고의 원인이었다: collect_run은 숨긴 기사를 뺀 9건으로 분류해 그 구성으로 캐시를
    심어두는데(classify_for_finalize -> seed_cache), 큐레이션 핸들러(_handle_move_article
    등)는 숨긴 기사까지 포함한 14건을 그대로 넘겼다. 기사 집합이 달라 캐시가 안 맞고
    (_find_reusable은 "줄어들기만 한" 경우만 재사용한다 — 여기선 오히려 늘어난 쪽이다)
    결국 진짜 API 재호출이 나가 소제목 이름이 통째로 새로 지어졌고, 이름을 키로 저장되는
    담당자의 작업(group_order/group_labels/group_overrides)이 한꺼번에 무효가 됐다.
    ("세제 개편 발표" -> "세제 개편 관련", 손수 만든 "<우리부 관련>" 옆에 AI가 지은
    "우리부 관련"이 따로 생겨 같은 이름 소제목이 두 개로 보임.)

    숨긴 기사는 분류에서만 빠지고 반환 목록에는 그대로 남는다(원래 "group" 값을 유지) —
    회차 파일에서 기사가 사라지면 안 되고, 나중에 숨김을 풀었을 때 제자리로 돌아와야 한다.

    [수정: 2026-08-14] 반환 순서도 분류 결과 순(소제목별로 뭉친 순서)이 아니라 **입력
    순서 그대로**다. 화면의 소제목 안 순서는 이 리스트 순서를 그대로 따르므로
    (groups_from_snapshot), 큐레이션 조작 한 번마다 목록이 재배열되면 담당자가 ↑/↓로
    잡아둔 순서가 흔들린다. collect_run이 이미 쓰던 방식과 같다.
    """
    # app.curation이 app.classifier를 임포트하므로(순환) 여기서만 지연 임포트한다.
    from app.curation import filter_hidden

    visible = filter_hidden(articles)
    # [수정: 2026-08-14] allow_llm_call=False를 명시한다 — 이 함수의 호출부는 전부
    # 큐레이션 액션 뒤 재스냅샷(숨기기/이동/이름변경)이거나 되돌리기다. force_llm=True일
    # 때만(재분류 버튼) app.llm_classifier.classify_with_llm이 이 값을 무시하고 실제로
    # 호출한다 — force가 allow_call보다 우선하도록 그 함수에서 이미 처리해둔다.
    groups = classify_articles(
        visible,
        keywords,
        forced_groups=forced_groups,
        custom_group_names=custom_group_names,
        allow_llm_call=False,
        force_llm=force_llm,
        round_id=round_id,
    )
    # [수정: 2026-08-12] "group"과 같은 이유로 "group_summary"도 기사마다 함께 찍어둔다 —
    # groups_from_snapshot이 재분류 없이 이 값만 읽어 화면에 쓴다(g.get: 규칙 기반 분류는
    # summary 키 자체가 없다).
    group_by_url = {a["url"]: (g["name"], g.get("summary", "")) for g in groups for a in g["articles"]}
    snapshotted = []
    for a in articles:
        name, summary = group_by_url.get(a["url"], (a.get("group", "기타"), a.get("group_summary", "")))
        snapshotted.append({**a, "group": name, "group_summary": summary})
    return snapshotted


# [추가: 2026-08-27] 규칙 기반(단어 빈도) 폴백으로 저장된 회차를 사후에 알아보는 판정.
# 소제목 이름이 3개 이상이면서 **전부 한 어절**(공백 없음)이면 폴백으로 본다.
_RULE_BASED_MIN_NAMES = 3


def looks_rule_based(articles: list) -> bool:
    """이 회차의 소제목이 규칙 기반(단어 빈도) 폴백으로 지어진 이름인지 판정한다.

    회차 파일에 저장된 "group" 필드만 보므로 **이미 저장된 옛 회차에도 소급 적용된다** —
    app.scraper.collect_run이 classification_degraded를 제대로 남기기 시작한 것은
    2026-08-27부터라(HISTORY.md "classification_degraded 배선"), 그 이전 회차는 이 판정이
    유일한 판별 수단이다. 저장된 데이터만 읽고 API를 부르지 않는다.

    **회차 단위로 판단하고 이름 단위로는 판단하지 않는다.** 실측(2026-08-27, 저장된 80개
    회차 전수)에서 "소제목 이름 중 한 어절짜리 비율"이 이렇게 갈렸다:

        0%       68개  정상
        10~30%    8개  '반부패정책협의회' '중앙지방협력회의' '금리정책' '예결위'
                       — 전부 **정상 AI 이름**이다
        100%      4개  2026-08-27_06-00 / 2026-08-11_17-00 / 2026-08-25_09-30 /
                       2026-08-22_10-00 — 전부 규칙 기반 폴백 회차

    0.3과 1.0 사이가 완전히 비어 있고 100%인 4개는 오탐이 0건이었다. 반면 이름 하나하나를
    보고 "한 어절이면 버린다"로 걸렀다면 위 10~30% 구간의 멀쩡한 이름 10개를 같이 죽인다.

    _RULE_BASED_MIN_NAMES(3) 미만은 판정하지 않는다 — 소제목이 1~2개뿐인 작은 회차는
    정상 AI 이름이 우연히 전부 한 어절일 수 있어(예: '중앙지방협력회의' 하나뿐인 회차)
    표본이 너무 적다. 틀리는 방향이 "폴백을 못 알아본다"여야지 "멀쩡한 회차를 폴백으로
    몰아붙인다"이면 안 된다.
    """
    names = set()
    for article in articles or []:
        name = (article.get("group") or "").strip()
        # "기타"는 자동/규칙 양쪽 모두에서 나오는 받이라 판별에 쓸 수 없고,
        # 📂 소제목 미분류는 코드가 붙인 고정 이름이라 AI가 지은 이름이 아니다.
        if not name or name == "기타" or name == UNCLASSIFIED_GROUP_NAME:
            continue
        names.add(name)
    if len(names) < _RULE_BASED_MIN_NAMES:
        return False
    return all(" " not in name for name in names)


def run_looks_rule_based(run: Optional[dict]) -> bool:
    """회차 dict를 그대로 받는 looks_rule_based 래퍼 — 저장된 classification_degraded가
    있으면 그 값을 우선한다(수집 시점에 실제로 관측한 사실이 사후 추정보다 정확하다).

    2026-08-27 이전 회차 파일에는 그 필드가 아예 없으므로(위 docstring 참고) 그때만
    이름 모양으로 추정한다.
    """
    if not run:
        return False
    if run.get("classification_degraded"):
        return True
    return looks_rule_based(run.get("articles") or [])
