# Design Ref: 사용자 요청(2026-08-03) — 소제목이 화면에 나열되는 순서를 ↑/↓로 직접 바꿀 수 있게 한다.
import json
from typing import Optional

from app import store
from app.config import GROUP_ORDER_FILE
from app.curation import display_group_name, load_group_labels
from app.llm_classifier import ETC_GROUP_NAME, UNCLASSIFIED_GROUP_NAME, round_key


def _lock_rank(name: str, labels: dict, ordered_names=()) -> int:
    """자연 순서 뒤로 미는 자리 — 0이면 제자리, 1이면 아직 손대지 않은 기타, 2이면 📂 미분류
    (목록 맨 뒤에 이 순서로 붙는다).

    기타는 **화면에 보이는 이름**으로 판단한다(이름표 적용 후) — 담당자가 평범한 소제목의
    이름표를 "기타"로 바꾸는 일이 실제로 있고, 보고서를 받는 사람에겐 그것도 똑같이
    <기타>로 보이기 때문이다. 미분류는 이름을 바꿀 수 없는 칸이라 원래 이름으로 본다.

    ordered_names(그 회차에 저장된 소제목 순서)에 그 이름이 있으면 기타도 0이다 —
    담당자가 ▲▼로 자리를 정한 칸이라 그 자리가 코드보다 앞선다. AI가 막 분류한 회차는
    저장된 순서가 비어 있으니 예전처럼 맨 뒤로 간다.
    """
    if name == UNCLASSIFIED_GROUP_NAME:
        return 2
    if display_group_name(name, labels).strip() == ETC_GROUP_NAME and name not in ordered_names:
        return 1
    return 0


def is_order_locked(name: str, labels: dict) -> bool:
    """이 소제목이 순서를 바꿀 수 없는 칸인가 — **📂 미분류 하나뿐이다.**

    화면이 ▲▼를 그릴지, 순서를 저장할 목록에 넣을지 판단할 때 쓴다. 미분류는 이름을
    바꿀 수도 없고 이름으로 저장하면 다음 회차의 (내용이 전혀 다른) 미분류가 그 자리에
    끼어들기 때문에 순서 저장 목록 자체에서 빠진다.

    labels는 더 읽지 않지만 화면 다섯 자리가 같은 모양으로 부르고 있어 인자는 남긴다.
    """
    return name == UNCLASSIFIED_GROUP_NAME


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
        data = store.read_json(GROUP_ORDER_FILE)
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
        data = store.read_json(GROUP_ORDER_FILE) if GROUP_ORDER_FILE.exists() else {}
    except (json.JSONDecodeError, TypeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data[round_key(round_id)] = cleaned
    store.write_json(GROUP_ORDER_FILE, data, indent=2)


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
    # 위로 올라오는 경우가 있었다(제보). 이 칸엔 ▲▼가 없으므로 한 번 잘못 배치되면
    # 담당자가 손으로 되돌릴 방법이 없다 — 상대 순서는 그대로 두고 이 칸만 맨 뒤로 옮겨
    # 항상 마지막에 오도록 코드로 보장한다.
    # "기타"는 저장된 순서에 없을 때만 그 앞에 민다 — 자연 순서가 "기사 목록에서 먼저
    # 나온 순"이라 AI가 막 분류한 회차에선 기타가 맨 위나 가운데에 끼는 일이 흔하다.
    # 담당자가 ▲▼로 옮겨 그 이름이 저장된 순서에 들어오면 그 자리를 그대로 지킨다.
    # sorted는 안정 정렬이라 뒤로 밀리지 않는 소제목끼리의 상대 순서는 그대로다.
    labels = load_group_labels(round_id)
    return sorted(ordered, key=lambda g: _lock_rank(g["name"], labels, order))
