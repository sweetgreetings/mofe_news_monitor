# Design Ref: 스크랩 초안(preview.html) "+ 새 소제목 만들기" — 자동 분류로는 절대 안 나오는
# 이름을 사용자가 미리 만들어두고, 기사가 하나도 없어도 화면에 계속 노출해 체크박스/
# 드롭다운으로 기사를 옮겨 담을 수 있게 한다(app.preview_renderer).
import json
from typing import Optional

from app.atomic_write import atomic_write_text
from app.config import CUSTOM_GROUPS_FILE


def load_custom_groups() -> list:
    """사용자가 직접 만든 소제목 이름 목록을 읽어온다 (순서: 만든 순서 그대로)."""
    if not CUSTOM_GROUPS_FILE.exists():
        return []
    try:
        names = json.loads(CUSTOM_GROUPS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, TypeError):
        return []
    return names if isinstance(names, list) else []


def _write(names: list) -> None:
    atomic_write_text(CUSTOM_GROUPS_FILE, json.dumps(names, ensure_ascii=False))


def add_custom_group(name: str) -> Optional[str]:
    """새 소제목 이름을 추가한다. 이미 있는 이름이면 조용히 무시(중복 방지)한다.

    반환값은 없음(호출하는 쪽은 성공 여부를 신경 쓸 필요가 없다 — 이미 있어도 결과적으로
    "그 이름의 소제목이 존재한다"는 상태는 똑같이 보장되기 때문).
    """
    name = name.strip()
    if not name:
        return None
    names = load_custom_groups()
    if name not in names:
        names.append(name)
        _write(names)
    return name


def remove_custom_group(name: str) -> None:
    """소제목을 지운다 — 빈 소제목 헤더의 🗑️(삭제) 버튼이 호출한다.

    이미 기사가 들어가 있는 소제목이라도 이 목록에서만 빠질 뿐, 그 기사들의 소속
    (group_overrides.json)은 그대로 남는다 — app.classifier.classify_articles가
    실제로 기사가 있으면 이 목록과 무관하게 그 소제목을 계속 보여주므로 문제없다.
    """
    names = [n for n in load_custom_groups() if n != name]
    _write(names)
