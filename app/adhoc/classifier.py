# Design Ref: ADHOC_DESIGN.md §6.5 — "🤖 소제목 배정" 버튼 하나의 뒤에서 실제로 무엇을
# 부를지 정한다. 자동 호출은 0회(버튼을 눌러야만 여기 들어온다)이고, 최대 5개까지만
# 소제목을 만든다(정기는 8개 — 사안이 한정적이라 더 늘리지 않는다).
#
# 정기와 같은 LLM 계층(app/llm_classifier.py)을 그대로 쓰되, 카드에 이미 있는 소제목은
# 절대 건드리지 않는다 — "재분류는 파괴적이라서 아낀다"(CODING_CONVENTIONS.md §6-3)는
# 원칙이 카드에도 그대로 적용된다:
#   - 소제목이 하나도 없으면(최초) classify_with_llm로 처음부터 묶는다.
#   - 소제목이 이미 있으면(재클릭·재수집 후) assign_to_existing로 미분류만 기존 소제목에
#     끼워 넣는다 — 담당자가 move-article로 옮겨둔 배치가 절대 흔들리지 않는다. 이건
#     정기 스크랩 초안의 "🤖 미분류 배정"(app/settings_server.py _handle_assign_unclassified)과
#     정확히 같은 안전장치이자 같은 코드 경로다.
from typing import Optional

import app.llm_classifier as llm_classifier
from app.llm_classifier import assign_to_existing, classify_with_llm

from app.adhoc.collector import articles_in_condition
from app.adhoc.card import MAX_ADHOC_SUBHEADINGS, card_lock, current_group_names, load_card, save_card


def _classify_isolated(articles: list, max_subheadings: int) -> Optional[list]:
    """정기 스크랩의 전역 `_last_names`(직전 소제목 이름 이어쓰기 힌트)를 건드리지
    않고 classify_with_llm을 호출한다. 안 그러면 양방향으로 오염된다(ADHOC_DESIGN.md §6.5):

      (1) 호출 전 — 정기 회차의 소제목 이름이 이 카드 프롬프트에 "직전에 쓰던 이름"으로
          잘못 들어가 엉뚱한 이름을 재사용하려 든다. 그래서 호출 직전에 비워둔다.
      (2) 호출 후 — classify_with_llm이 끝나면 `_last_names`를 이 카드의 이름으로
          덮어쓰고, **그 상태로 캐시 파일까지 디스크에 저장**한다(_persist_cache_locked가
          classify_with_llm 안에서 이미 호출됨). 메모리만 되돌리고 끝내면, 다음에 앱을
          재시작할 때 _ensure_cache_hydrated가 디스크의 이 카드 이름을 다시 불러와
          정기 쪽을 오염시킨다 — 그래서 되돌린 뒤 캐시 파일도 다시 써서 디스크까지
          맞춰둔다(실제로 이 문제를 실측으로 확인하고 고쳤다: 최초 구현은 메모리만
          되돌리고 디스크는 그대로 둬, 재시작 시나리오에서 오염이 남아 있었다).
    """
    saved = list(llm_classifier._last_names)
    llm_classifier._last_names = []
    try:
        return classify_with_llm(articles, max_subheadings=max_subheadings)
    finally:
        with llm_classifier._cache_lock:
            llm_classifier._last_names = saved
            llm_classifier._persist_cache_locked()


def classify_card(card_id: str) -> Optional[dict]:
    """카드의 미분류(숨김 제외) 기사를 LLM으로 배정하고 저장한다.

    분류할 게 없거나(모두 이미 배정됨) API 호출이 실패하면 카드를 건드리지 않고 그대로
    돌려준다 — "조용히 규칙 기반으로/그대로 폴백"이라는 이 모듈군의 원칙 그대로,
    다만 규칙 기반 대체 분류는 없으므로 실패 시 그냥 "아직 미분류"로 남는다(담당자가
    다시 눌러볼 수 있다). 카드가 없으면 None.

    담당자가 "+ 소제목 추가"로 직접 만든 소제목(card["custom_groups"])은 기사가 있든
    없든 AI의 배정 후보에서 항상 빠진다 — 특정 기사만 넣으려고 비워둔 칸일 수 있는데
    모델은 그 의도를 알 수 없다(정기 스크랩의 같은 원칙, app/settings_server.py
    _handle_assign_unclassified 참고). 다만 소제목 개수 상한(MAX_ADHOC_SUBHEADINGS)은
    같이 센다 — 안 그러면 직접 만든 소제목 자리를 AI가 몰라서 상한을 넘겨버린다.
    """
    with card_lock(card_id):
        c = load_card(card_id)
        if c is None:
            return None

        # 조건 밖 기사는 화면에 없으므로 분류 대상도 아니다 — 넣으면 화면에 안 보이는
        # 기사로 소제목이 만들어지고 API 토큰도 그만큼 더 쓴다(ADHOC_DESIGN.md §6.4).
        visible = [a for a in articles_in_condition(c) if not a["hidden"]]
        unclassified = [a for a in visible if a["group"] is None]
        if not unclassified:
            return c

        custom = set(c["custom_groups"])
        # "기타"·직접 만든 소제목은 후보에서 뺀다 — assign_to_existing은 애초에 "기타"를
        # 배정 대상으로 받아들이지 않는다(ASSIGN_SYSTEM_PROMPT 참고). 후보는 화면에 실제
        # 보이는(숨김 제외) 기사가 쓰는 이름만 본다.
        llm_candidates = sorted({a["group"] for a in visible if a["group"]} - custom - {"기타"})
        by_url = {a["url"]: a for a in c["articles"]}
        # 지금 이미 차 있는 자리 — add_custom_group과 같은 기준(current_group_names,
        # 숨긴 기사 포함 전체)으로 세야 "몇 개 남았는지"가 두 기능에서 어긋나지 않는다.
        room = MAX_ADHOC_SUBHEADINGS - len(current_group_names(c))

        if not llm_candidates:
            if room <= 0:  # 직접 만든 소제목만으로 이미 상한 — AI가 새로 지을 자리가 없다
                return c
            groups = _classify_isolated(visible, room)
            if groups is None:
                return c
            for g in groups:
                for a in g["articles"]:
                    by_url[a["url"]]["group"] = g["name"]
                if g.get("summary"):  # "기타"는 항상 빈 요약 — 빈 문자열을 저장하지 않는다
                    c["group_summaries"][g["name"]] = g["summary"]
        else:
            assigned = assign_to_existing(unclassified, llm_candidates)
            if not assigned:
                return c
            # 모델이 기존 소제목에 없는 새 이름을 제안할 수 있다 — 남은 자리 안에서만
            # 새 소제목으로 받아들이고, 자리가 없으면 "기타"로 보낸다(정기의
            # _handle_assign_unclassified와 동일한 상한 처리).
            accepted_new: set = set()
            for url, name in assigned.items():
                if name not in llm_candidates:
                    if name not in accepted_new:
                        if len(accepted_new) >= room:
                            name = "기타"
                        else:
                            accepted_new.add(name)
                by_url[url]["group"] = name
            # [수정: 2026-08-24] assign_to_existing이 unclassified의 부분집합만 돌려줄
            # 수 있다 — 모델이 기사를 응답에서 통째로 빠뜨리거나 "기타"/빈 값으로 답해
            # 그 함수 내부에서 걸러졌을 때다. 예전엔 그런 기사가 조용히 미분류(group=None)
            # 그대로 남아, 한 번 눌러도 "몇 건만 배정되고 나머지는 그대로"로 보였다(정기의
            # 같은 버그, app.settings_server._handle_assign_unclassified와 동일한 원인이라
            # 같이 고친다). "기타"는 room 계산과 무관하게 항상 열려 있는 칸이다.
            for a in unclassified:
                if a["url"] not in assigned:
                    by_url[a["url"]]["group"] = "기타"

        save_card(c)
        return c
