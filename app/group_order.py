# Design Ref: 사용자 요청(2026-08-03) — 소제목이 화면에 나열되는 순서를 ↑/↓로 직접 바꿀 수 있게 한다.
import json
from typing import Optional

from app.atomic_write import atomic_write_text
from app.config import GROUP_ORDER_FILE
from app.curation import display_group_name, load_group_labels
from app.llm_classifier import ETC_GROUP_NAME, UNCLASSIFIED_GROUP_NAME, round_key


def _lock_rank(name: str, labels: dict) -> int:
    """순서가 코드로 고정된 칸의 자리 — 0이면 일반 소제목, 1이면 기타, 2이면 📂 미분류
    (목록 맨 뒤에 이 순서로 붙는다).

    [추가: 2026-09-10] 기타는 **화면에 보이는 이름**으로 판단한다(이름표 적용 후) —
    담당자가 평범한 소제목의 이름표를 "기타"로 바꾸는 일이 실제로 있고(2026-09-10에만
    두 회차), 보고서를 받는 사람에겐 그것도 똑같이 <기타>로 보이기 때문이다. 반대로
    원래 "기타"에 진짜 이름표를 붙였다면(실측 1건: "공공기관 통폐합") 그건 이제 받이가
    아니라 일반 소제목이라 옮길 수 있어야 한다. 미분류는 이름을 바꿀 수 없는 칸이라
    원래 이름으로 본다.
    """
    if name == UNCLASSIFIED_GROUP_NAME:
        return 2
    if display_group_name(name, labels).strip() == ETC_GROUP_NAME:
        return 1
    return 0


def is_order_locked(name: str, labels: dict) -> bool:
    """이 소제목이 순서가 고정된 칸(기타·미분류)인가 — 화면이 ▲▼를 그릴지 판단할 때 쓴다.

    고정 칸은 담당자가 ▲▼로 옮길 수 없다(화면이 버튼을 안 그린다) — 옮겨도
    apply_group_order가 도로 뒤로 보내 "눌러도 아무 일이 안 일어나는" 버튼이 되기 때문.
    labels는 화면이 이미 읽어둔 그 회차의 이름표를 넘긴다(apply_group_order와 같은 기준).
    """
    return _lock_rank(name, labels) > 0


def load_group_order(round_id: Optional[tuple] = None) -> list:
    """이 회차(round_id)에 저장된 소제목 순서(이름 목록)를 읽어온다. 없거나 형식이
    이상하면 빈 목록.

    [수정: 2026-08-14] 파일 전체가 아니라 round_id로 지정한 회차의 순서만 읽는다
    (app.curation.load_group_labels와 같은 이유·같은 방식) — "소제목은 그날그날
    다르게 쓰여도 된다"는 원칙에 따라, 순서도 이름표처럼 회차를 넘어 새지 않는다.
    """
    if not GROUP_ORDER_FILE.exists():
        return []
    try:
        data = json.loads(GROUP_ORDER_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(data, dict):
        return []
    bucket = data.get(round_key(round_id))
    return bucket if isinstance(bucket, list) else []


def save_group_order(order: list, round_id: Optional[tuple] = None) -> None:
    """이 회차(round_id)의 소제목 순서(이름 목록)를 통째로 저장한다.

    ↑/↓ 버튼을 누른 화면이 "지금 화면에 실제로 보이는 순서"를 그대로 계산해(자바스크립트,
    getAllGroups 방식) 한 번에 통째로 보내주므로, 서버는 검증 없이 그대로 저장하기만 한다.
    """
    cleaned = [str(name) for name in order if isinstance(name, str) and name]
    try:
        data = json.loads(GROUP_ORDER_FILE.read_text(encoding="utf-8")) if GROUP_ORDER_FILE.exists() else {}
    except (json.JSONDecodeError, TypeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data[round_key(round_id)] = cleaned
    atomic_write_text(GROUP_ORDER_FILE, json.dumps(data, ensure_ascii=False, indent=2))


def apply_group_order(groups: list, round_id: Optional[tuple] = None) -> list:
    """classify_articles가 만든 자연 순서(빈도순)에 이 회차에 저장된 사용자 지정 순서를 얹는다.

    저장된 순서에 있는 소제목은 그 순서 그대로, 저장된 순서에 없는 소제목(오늘 새로
    등장했거나 group_order.json 저장 이후 새로 생긴 것)은 자연 순서를 유지한 채 맨 뒤에
    자연스럽게 이어붙인다 — group_labels.json과 같은 "이름 기준으로 저장하고, 사라지면
    조용히 무시한다"는 원칙을 그대로 따른다.
    """
    order = load_group_order(round_id)
    by_name = {g["name"]: g for g in groups}
    ordered = [by_name.pop(name) for name in order if name in by_name]
    ordered.extend(by_name.values())
    # [추가: 2026-08-24] 📂 소제목 미분류는 소제목이 아니라 대기 칸이다 — 저장된 순서나
    # 생성 시점(_rebuild가 leftover를 맨 뒤에 append하는 등)에 따라 사용자 폴더보다
    # 위로 올라오는 경우가 있었다(제보). 2026-08-21에 이 칸의 ▲▼를 없앴으므로 한 번
    # 잘못 배치되면 담당자가 손으로 되돌릴 방법이 없다 — 상대 순서는 그대로 두고 이
    # 칸만 맨 뒤로 옮겨 항상 마지막에 오도록 코드로 보장한다.
    # [수정: 2026-09-10] "기타"도 같은 방식으로 미분류 바로 앞에 고정한다(사용자 결정,
    # 판단 기준은 _lock_rank). 자연 순서가 "기사 목록에서 먼저 나온 순"이라 기타가 맨 위나
    # 가운데에 끼는 일이 흔했다. 예전에 저장된 순서에 기타가 중간에 적혀 있어도 이제는
    # 맨 뒤로 간다. sorted는 안정 정렬이라 고정 칸이 아닌 소제목끼리의 상대 순서는 그대로다.
    labels = load_group_labels(round_id)
    return sorted(ordered, key=lambda g: _lock_rank(g["name"], labels))
