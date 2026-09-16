# Design Ref: 사용자 결정(2026-09-15) — 「AI 기사 나누기」. 초안에서 소제목 하나(또는 그 안의
# 일부 기사)를 체크하면 하단바에 뜨는 연보라 버튼으로, 고른 기사만 AI가 쟁점별로 새 소제목에
# 나눠 담는다. 시안은 SUBHEADING_SPLIT_MOCKUP.html.
#
# 이 모듈은 **AI 응답을 받은 뒤 무엇을 어디로 옮길지 정하는 순수 계산**만 한다(파일·API 없음) —
# 저장은 app.settings_server._handle_split_group이 「AI 기사 배정」과 같은 경로(group_overrides +
# assigned_groups)로 한다. 규칙을 한곳에 모아 테스트로 못 박으려고 따로 뺐다.
from typing import Optional

from app.config import MAX_SUBHEADINGS
from app.llm_classifier import ETC_GROUP_NAME

# 이보다 적게 고르면 버튼을 잠근다 — 2건씩 두 묶음도 안 나오는 크기다(화면 JS와 같은 값).
MIN_SPLIT_ARTICLES = 4


def plan_split(
    original_name: str,
    selected_urls: list,
    llm_groups: list,
    whole: bool,
    other_names,
    subheading_count: int,
    etc_exists: bool,
    original_display: Optional[str] = None,
    max_subheadings: int = MAX_SUBHEADINGS,
) -> Optional[dict]:
    """AI가 돌려준 묶음(llm_groups)을 실제로 옮길 계획으로 바꾼다. 나뉜 게 없으면 None.

    약속은 「고른 기사만 나누고, 다른 소제목은 한 글자도 안 건드린다」 하나다. 그래서:

    - AI가 원래 이름(또는 담당자가 붙인 표시 이름)을 그대로 쓴 묶음은 **제자리에 남긴다** —
      여러 쟁점에 두루 걸치는 기사가 원래 칸에 남는 자연스러운 모양이다.
    - 이미 다른 소제목이 쓰는 이름과 겹치면 그 칸에 섞지 않고 **제자리에 남긴다** — 섞으면
      "다른 소제목은 그대로"가 거짓말이 된다. 예외는 「기타」 하나다: 어디에도 안 맞는 기사를
      모으는 받이라 이미 있으면 거기로 합친다(뜻이 같아 보고서도 정확하다).
    - 새 소제목은 칸이 남는 만큼만 만든다(max_subheadings). 넘치는 묶음은 작은 것부터
      제자리에 남긴다. 원래 칸이 비게 되면(전체를 골랐고 남는 기사도 없으면) 그 한 칸만큼
      더 쓸 수 있다.
    - 나눈 결과의 목적지가 둘 이상이어야 "나눴다"로 친다(원래 칸에 남는 기사·안 고른 나머지도
      목적지 하나로 센다). 전체를 골랐는데 AI가 새 이름 하나로 통째로 묶어 돌려준 건 이름만
      바꾼 것이라 아무것도 안 바꾼다 — 이름만 슬쩍 바뀌는 게 가장 나쁜 결과다.

    반환: {"moves": {url: 목적지 이름}, "new_names": [새로 등록할 이름(AI 순서)],
    "stay": [제자리에 남는 url]}. moves에는 실제로 칸이 바뀌는 기사만 담긴다.
    """
    selected = set(selected_urls)
    stay_aliases = {original_name, (original_display or original_name)}
    other = set(other_names or ()) - stay_aliases

    stay: list = []
    etc_urls: list = []
    new_groups: list = []  # [(이름, [url…])] — AI 순서 그대로, 같은 이름은 하나로 합친다
    for g in llm_groups or []:
        name = (g.get("name") or "").strip()
        urls = [a["url"] for a in g.get("articles", []) if a.get("url") in selected]
        if not urls:
            continue
        if not name or name in stay_aliases:
            stay.extend(urls)
        elif name == ETC_GROUP_NAME and etc_exists:
            etc_urls.extend(urls)
        elif name in other:
            stay.extend(urls)
        else:
            existing = next((pair for pair in new_groups if pair[0] == name), None)
            if existing is not None:
                existing[1].extend(urls)
            else:
                new_groups.append((name, list(urls)))
    # AI가 빠뜨린 기사(없어야 하지만)는 제자리에 둔다 — 조용히 엉뚱한 칸으로 가지 않게.
    covered = set(stay) | set(etc_urls) | {u for _, urls in new_groups for u in urls}
    stay.extend(u for u in selected_urls if u not in covered)

    # 칸 수 상한. 큰 묶음부터 살리고(동점이면 AI 순서), 넘치는 건 제자리로.
    by_size = sorted(range(len(new_groups)), key=lambda i: (-len(new_groups[i][1]), i))
    keep = len(new_groups)
    while keep > 0:
        original_stays = bool(stay) or keep < len(new_groups) or not whole
        allowed = max_subheadings - subheading_count + (0 if original_stays else 1)
        if keep <= allowed:
            break
        keep -= 1
    kept = set(by_size[:keep])
    for i, (_, urls) in enumerate(new_groups):
        if i not in kept:
            stay.extend(urls)

    moves = {u: ETC_GROUP_NAME for u in etc_urls}
    new_names = []
    for i, (name, urls) in enumerate(new_groups):
        if i in kept:
            new_names.append(name)
            for u in urls:
                moves[u] = name

    destinations = set(moves.values())
    if stay or not whole:
        destinations.add(original_name)
    if len(destinations) < 2 or not moves:
        return None
    return {"moves": moves, "new_names": new_names, "stay": stay}


def order_with_new_names(order: list, original_name: str, new_names: list, locked) -> list:
    """소제목 순서 목록에 새 이름을 원래 소제목 **바로 뒤**에 끼운다.

    새 소제목이 목록 끝이 아니라 원래 자리에 들어가야 "그 칸이 나뉘었다"로 읽힌다
    (app.group_order.apply_group_order는 저장된 순서에 없는 이름을 맨 뒤에 붙인다).
    원래 이름이 목록에 없으면(순서 고정 칸인 기타를 나눈 경우 등) 옮길 수 있는 소제목의
    맨 뒤에 붙인다 — 기타·📂 미분류는 어차피 그 뒤에 온다. locked(기타 같은 고정 칸)는
    저장 목록에 넣지 않는다(화면 JS moveGroupOrder가 보내는 목록과 같은 규칙).
    """
    add = [n for n in new_names if n not in locked and n not in order]
    if original_name in order:
        i = order.index(original_name) + 1
        return order[:i] + add + order[i:]
    return order + add
