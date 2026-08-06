# Design Ref: 스크랩 초안(preview.html) "+ 새 소제목 만들기" — 자동 분류로는 절대 안 나오는
# 이름을 사용자가 미리 만들어두고, 기사가 하나도 없어도 화면에 계속 노출해 체크박스/
# 드롭다운으로 기사를 옮겨 담을 수 있게 한다(app.preview_renderer).
import json
from datetime import datetime
from typing import Optional

from app.atomic_write import atomic_write_text
from app.config import CUSTOM_GROUPS_FILE
from app.group_order import load_group_order, save_group_order


def _today_str(now: Optional[datetime] = None) -> str:
    return (now or datetime.now()).strftime("%Y-%m-%d")


def load_custom_groups(now: Optional[datetime] = None) -> list:
    """사용자가 직접 만든 소제목 이름 목록을 읽어온다 (순서: 만든 순서 그대로).

    [수정: 2026-08-05] 원래는 지우기 전까지 무기한 유지됐으나("📌 담아두기"와 반대로
    자정에도 안 비워짐), 실사용해보니 하루만 쓰고 마는 소제목이 계속 쌓여 다음 날에도
    빈 소제목으로 남는 게 불편하다는 피드백 — app.manual_articles와 동일한 방식으로
    저장된 날짜가 오늘이 아니면(자정이 지났으면) 빈 목록 취급한다. 파일을 그 자리에서
    지우진 않고, 다음 add_custom_group 호출 때 오늘 날짜로 덮어써진다. 이미 기사가 옮겨진
    소제목이 자정 이후 사라지면 그 기사들은 app.classifier의 자기 치유 로직에 따라
    자동 분류(또는 "기타")로 조용히 되돌아간다 — 데이터가 없어지는 건 아니다.
    """
    if not CUSTOM_GROUPS_FILE.exists():
        return []
    try:
        data = json.loads(CUSTOM_GROUPS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, TypeError):
        return []
    if isinstance(data, list):
        # [수정: 2026-08-05] 이 필드가 생기기 전(날짜 없이 이름 배열만 저장하던 옛 형식)
        # 데이터는 바로 비우지 않는다 — 이미 만들어둔 소제목이 이 기능이 배포된 순간
        # 바로 사라지면 "왜 갑자기 없어졌지" 하는 데이터 유실처럼 보인다. 오늘 막
        # 만든 것으로 간주해 오늘 자정까지는 그대로 살려주고, 그 이후부터 정상적으로
        # 날짜 기준 초기화가 적용되도록 오늘 날짜로 한 번 마이그레이션해 다시 쓴다.
        _write(data, now)
        return data
    if not isinstance(data, dict) or data.get("date") != _today_str(now):
        return []
    names = data.get("names", [])
    return names if isinstance(names, list) else []


def _write(names: list, now: Optional[datetime] = None) -> None:
    atomic_write_text(
        CUSTOM_GROUPS_FILE, json.dumps({"date": _today_str(now), "names": names}, ensure_ascii=False)
    )


def add_custom_group(name: str, now: Optional[datetime] = None) -> Optional[str]:
    """새 소제목 이름을 추가한다. 이미 있는 이름이면 조용히 무시(중복 방지)한다.

    반환값은 없음(호출하는 쪽은 성공 여부를 신경 쓸 필요가 없다 — 이미 있어도 결과적으로
    "그 이름의 소제목이 존재한다"는 상태는 똑같이 보장되기 때문).

    [추가: 2026-08-05] 새로 만든 소제목은 화면 맨 위에, 만든 순서대로 나오도록
    `data/group_order.json`에도 같이 등록한다(`_place_at_top_of_order`) — 예전엔
    맨 아래(자연 순서 맨 뒤)에 붙었는데, 소제목이 몇 개 쌓이면 스크롤해서 찾아야 해
    불편하다는 피드백. 이건 "새로 만들 때의 기본 위치"일 뿐이라, 이후 ↑/↓나 소제목
    미니 목차로 자유롭게 다시 옮기는 건 그대로 가능하다(다른 소제목 순서 조정과 동일).
    """
    name = name.strip()
    if not name:
        return None
    names = load_custom_groups(now)
    if name not in names:
        names.append(name)
        _write(names, now)
        _place_at_top_of_order(name, names)
    return name


def _place_at_top_of_order(name: str, all_custom_names: list) -> None:
    """방금 만든 소제목을, 기존에 순서가 등록돼 있던 다른 커스텀 소제목들 바로 다음
    (만든 순서를 유지) — 그러면서도 커스텀이 아닌 소제목보다는 항상 앞서도록 끼워
    넣는다. 아직 순서가 하나도 등록된 적 없으면(group_order.json이 비어 있으면)
    맨 앞에 놓는다.
    """
    order = [n for n in load_group_order() if n != name]
    earlier_customs_in_order = [n for n in all_custom_names if n != name and n in order]
    insert_at = max((order.index(n) + 1 for n in earlier_customs_in_order), default=0)
    order.insert(insert_at, name)
    save_group_order(order)


def remove_custom_group(name: str, now: Optional[datetime] = None) -> None:
    """소제목을 지운다 — 빈 소제목 헤더의 🗑️(삭제) 버튼이 호출한다.

    이미 기사가 들어가 있는 소제목이라도 이 목록에서만 빠질 뿐, 그 기사들의 소속
    (group_overrides.json)은 그대로 남는다 — 다만 app.classifier._apply_forced_groups는
    이 목록(custom_group_names)에 없는 이름을 빈 그룹으로 미리 만들어주지 않으므로,
    지운 뒤에는 그 기사들도 자동 분류(또는 "기타")로 조용히 되돌아간다(자정 지나 이
    목록 전체가 비워질 때와 동일한 자기 치유 동작).
    """
    names = [n for n in load_custom_groups(now) if n != name]
    _write(names, now)
