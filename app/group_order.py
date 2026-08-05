# Design Ref: 사용자 요청(2026-08-03) — 소제목이 화면에 나열되는 순서를 ↑/↓로 직접 바꿀 수 있게 한다.
import json

from app.atomic_write import atomic_write_text
from app.config import GROUP_ORDER_FILE


def load_group_order() -> list:
    """저장된 소제목 순서(이름 목록)를 읽어온다. 파일이 없거나 형식이 이상하면 빈 목록."""
    if not GROUP_ORDER_FILE.exists():
        return []
    try:
        data = json.loads(GROUP_ORDER_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def save_group_order(order: list) -> None:
    """전체 소제목 순서(이름 목록)를 통째로 저장한다.

    ↑/↓ 버튼을 누른 화면이 "지금 화면에 실제로 보이는 순서"를 그대로 계산해(자바스크립트,
    getAllGroups 방식) 한 번에 통째로 보내주므로, 서버는 검증 없이 그대로 저장하기만 한다.
    """
    cleaned = [str(name) for name in order if isinstance(name, str) and name]
    atomic_write_text(GROUP_ORDER_FILE, json.dumps(cleaned, ensure_ascii=False, indent=2))


def apply_group_order(groups: list) -> list:
    """classify_articles가 만든 자연 순서(빈도순)에 저장된 사용자 지정 순서를 얹는다.

    저장된 순서에 있는 소제목은 그 순서 그대로, 저장된 순서에 없는 소제목(오늘 새로
    등장했거나 group_order.json 저장 이후 새로 생긴 것)은 자연 순서를 유지한 채 맨 뒤에
    자연스럽게 이어붙인다 — group_labels.json·group_overrides.json과 같은 "이름 기준으로
    저장하고, 사라지면 조용히 무시한다"는 원칙을 그대로 따른다.
    """
    order = load_group_order()
    by_name = {g["name"]: g for g in groups}
    ordered = [by_name.pop(name) for name in order if name in by_name]
    ordered.extend(by_name.values())
    return ordered
