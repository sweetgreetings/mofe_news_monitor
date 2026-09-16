# Design Ref: 스크랩 초안 "🤖 미분류 배정"(app.settings_server._handle_assign_unclassified)이
# 기존 소제목 어디에도 안 맞아 새로 지어낸 이름을 기록해둔다. app.custom_groups(담당자가
# "+ 새 소제목 만들기"로 직접 만든 이름)와 파일 형식은 같지만, 이건 AI가 배정 도중 만든
# 이름이라는 점이 다르다 — 다음 렌더링에서 app.classifier._apply_forced_groups가 "분류
# 결과에 없는 이름"으로 보고 조용히 무시하지 않도록, 두 출처를 모두 forced_group_target_
# names()(app.classifier)로 합쳐 넘긴다.
import json
from datetime import datetime
from typing import Optional

from app.atomic_write import atomic_write_text
from app.config import ASSIGNED_GROUPS_FILE


def _today_str(now: Optional[datetime] = None) -> str:
    return (now or datetime.now()).strftime("%Y-%m-%d")


def load_assigned_groups(now: Optional[datetime] = None) -> list:
    """오늘 "미분류 배정"이 새로 지어낸 소제목 이름 목록(만든 순서 그대로).

    app.custom_groups.load_custom_groups와 동일하게 자정이 지나면(저장된 날짜가
    오늘이 아니면) 빈 목록 취급한다 — 하루만 쓰고 마는 임시 소제목이 다음 날에도
    빈 채로 남는 걸 막는다. 커스텀 소제목과 달리 화면 맨 위로 끌어올리지 않는다
    (add_assigned_group 참고) — "미분류 배정"은 기존 분류를 건드리지 않는다는
    원칙이라, 새로 생긴 소제목도 순서를 흔들지 않고 자연스러운 자리(맨 뒤)에 둔다.
    """
    if not ASSIGNED_GROUPS_FILE.exists():
        return []
    try:
        data = json.loads(ASSIGNED_GROUPS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(data, dict) or data.get("date") != _today_str(now):
        return []
    names = data.get("names", [])
    return names if isinstance(names, list) else []


def add_assigned_group(name: str, now: Optional[datetime] = None) -> None:
    """새로 배정된 소제목 이름을 오늘 목록에 추가한다(이미 있으면 조용히 무시)."""
    name = name.strip()
    if not name:
        return
    names = load_assigned_groups(now)
    if name not in names:
        names.append(name)
        atomic_write_text(
            ASSIGNED_GROUPS_FILE,
            json.dumps({"date": _today_str(now), "names": names}, ensure_ascii=False),
        )
