# Design Ref: 스크랩 초안(preview.html) "+ 새 소제목 만들기" — 자동 분류로는 절대 안 나오는
# 이름을 사용자가 미리 만들어두고, 기사가 하나도 없어도 화면에 계속 노출해 체크박스/
# 드롭다운으로 기사를 옮겨 담을 수 있게 한다(app.preview_renderer).
import json
from datetime import datetime
from typing import Optional

from app import store
from app.config import CUSTOM_GROUPS_FILE


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
        data = store.read_json(CUSTOM_GROUPS_FILE)
    except (json.JSONDecodeError, TypeError):
        return []
    if isinstance(data, list):
        # [수정: 2026-08-05] 이 필드가 생기기 전(날짜 없이 이름 배열만 저장하던 옛 형식)
        # 데이터는 바로 비우지 않는다 — 이미 만들어둔 소제목이 이 기능이 배포된 순간
        # 바로 사라지면 "왜 갑자기 없어졌지" 하는 데이터 유실처럼 보인다. 오늘 막
        # 만든 것으로 간주해 오늘 자정까지는 그대로 살려주고, 그 이후부터 정상적으로
        # 날짜 기준 초기화가 적용되도록 오늘 날짜로 한 번 마이그레이션해 다시 쓴다.
        _write(data, now=now)
        return data
    if not isinstance(data, dict) or data.get("date") != _today_str(now):
        return []
    names = data.get("names", [])
    return names if isinstance(names, list) else []


def load_custom_group_labels(now: Optional[datetime] = None) -> dict:
    """직접 만든 소제목의 {속이름: 화면에 보일 이름} — 둘이 다를 때만 들어 있다.

    소제목을 가르는 값은 이름 문자열 하나뿐이라, 담당자가 친 이름이 그 회차에 이미
    쓰이고 있으면(대표적으로 "기타" — AI가 분류 못 한 기사를 담는 받이로 늘 쓴다) 같은
    칸에 합쳐져 **새 칸이 아예 안 생긴다**. 그래서 겹칠 때만 속이름을 따로 짓고
    (unique_key) 화면에는 담당자가 친 이름을 보인다 — 그 매핑이 여기 산다.

    **이 매핑은 이름표(data/group_labels.json)가 아니라 여기 있어야 한다.** 이름표는
    회차 단위인데 직접 만든 소제목은 하루를 산다 — 이름표에 적으면 다음 회차 화면에서
    속이름이 그대로 드러난다. 읽는 쪽은 app.curation.load_group_labels 한 곳이고,
    그 함수가 이 매핑을 밑에 깔고 그 위에 회차 이름표를 얹는다(✏️로 고친 이름이 이긴다).

    **소제목 이름(names)은 하루를 살지만 이 매핑은 날짜로 비우지 않는다.** 둘의 수명이
    달라야 하는 이유는 하나다 — 속이름으로 저장된 **기사**(회차 파일의 group, 배정 기록)가
    이름보다 오래 살기 때문이다. 매핑이 먼저 죽으면 그 칸은 기사를 안고 그대로 남는데
    가려 주던 이름만 사라져 「우리부 관련 (2)」 같은 속이름이 화면·복사·txt·엑셀로
    그대로 나간다(2026-10-19 사고). 오래된 매핑이 엉뚱한 칸에 잘못 입혀질 위험도 없다 —
    여기 적히는 건 「이름 (N)」 꼴, 즉 담당자가 친 이름이 겹쳐서 앱이 지어 준 속이름뿐이고
    AI도 담당자도 그런 이름을 직접 짓지 않는다.
    """
    if not CUSTOM_GROUPS_FILE.exists():
        return {}
    try:
        data = store.read_json(CUSTOM_GROUPS_FILE)
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    labels = data.get("labels", {})
    return dict(labels) if isinstance(labels, dict) else {}


def unique_key(name: str, taken, now: Optional[datetime] = None) -> str:
    """담당자가 친 이름을 그대로 쓸 수 있으면 그대로, 이미 쓰이고 있으면 "이름 (2)"처럼
    안 겹치는 속이름을 지어 돌려준다.

    taken은 그 회차에 이미 존재하는 **속이름** 전부다(화면에 지금 안 보이는 것까지 —
    기사가 0건이라 안 그려진 칸도 나중에 기사가 들어오면 되살아나 합쳐진다). 막는 판정이
    아니라 안 겹치는 자리를 고르는 일이라, taken은 넉넉할수록 안전하고 담당자에게는
    아무것도 달라 보이지 않는다.

    속이름이 사람 눈에 띌 자리는 없지만(화면·복사·txt·발송·엑셀·보관함이 모두 화면 이름을
    쓴다) 라벨 스냅샷처럼 붙는 순간 값을 복사해 두는 자리가 있어, 혹시 새더라도 읽히도록
    "기타 (2)" 꼴을 쓴다 — 무의미한 식별자를 쓰지 않는 이유가 그것이다.
    """
    name = name.strip()
    if not name:
        return name
    used = set(taken or ()) | set(load_custom_groups(now))
    if name not in used:
        return name
    n = 2
    while f"{name} ({n})" in used:
        n += 1
    return f"{name} ({n})"


# 화면 이름 매핑(labels)의 상한. 나이로 지우지 않는 대신 개수로만 막는다 — 하루 두세
# 개가 쌓여도 1년치 회차(RETENTION_DAYS)를 덮고, 넘치면 오래된 것(적힌 순서가 곧 만든
# 순서다)부터 버린다. 매핑 한 줄은 수십 바이트라 파일이 커질 걱정은 없고, 상한을 두는
# 이유는 영원히 자라는 저장소를 만들지 않기 위해서다.
_MAX_LABELS = 1000


def _write(names: list, labels: Optional[dict] = None, now: Optional[datetime] = None) -> None:
    payload = {"date": _today_str(now), "names": names}
    if labels:
        if len(labels) > _MAX_LABELS:
            labels = dict(list(labels.items())[-_MAX_LABELS:])
        payload["labels"] = labels
    store.write_json(CUSTOM_GROUPS_FILE, payload)


def add_custom_group(name: str, key: Optional[str] = None, now: Optional[datetime] = None) -> Optional[str]:
    """새 소제목을 추가하고 **실제로 저장한 속이름**을 돌려준다. 이미 있으면 조용히 무시한다.

    key는 호출부가 unique_key로 미리 구해 둔 속이름이다 — 담당자가 친 이름이 그 회차에
    이미 쓰이고 있을 때만 달라진다. 미리 구하는 이유는 되돌리기 때문이다: 되돌리기 스냅샷은
    "동작 전" 상태를 찍어야 하는데, 그러려면 무엇을 지울지(그 속이름을 가리키던 배정 기록)
    를 저장 전에 알아야 한다(app.settings_server._handle_add_custom_group).

    **새로 만든 소제목은 목록 맨 아래에 온다** — 이 함수는 `group_order.json`을 아예
    건드리지 않고, 순서는 전적으로 자연 순서에 맡긴다. 자연 순서에서 커스텀 소제목이
    맨 뒤라는 건 두 자리가 함께 보장한다: 기사가 없으면 렌더러가 `empty_custom_groups`를
    목록 끝에 붙이고(app.preview_renderer/app.renderer), 기사가 있으면
    app.classifier._apply_forced_groups가 그 이름을 groups 끝에 끼워 넣는다.
    (📂 소제목 미분류는 app.group_order.apply_group_order가 언제나 그보다 더 뒤로 민다.)

    [수정: 2026-08-27] 2026-08-05에 넣었던 "새 소제목은 맨 위"(`_place_at_top_of_order`)를
    걷어냈다 — 사용자 요청으로 기본 위치를 다시 아래로 되돌린 것이고, 동시에 회차마다
    위치가 달라지던 문제도 같이 없앴다(만든 그 회차만 맨 위, 다음 회차부터는 group_order
    버킷이 비어 자연 순서를 타 맨 아래였다). 이후 ▲▼나 소제목 미니 목차로 자유롭게 다시
    옮기는 건 그대로 가능하다 — "기본 위치"만 바뀐다.
    """
    name = name.strip()
    if not name:
        return None
    key = (key or name).strip() or name
    names = load_custom_groups(now)
    labels = load_custom_group_labels(now)
    if key not in names:
        names.append(key)
        # 속이름과 화면 이름이 같으면 매핑을 안 적는다 — 적어 두면 담당자가 나중에 ✏️로
        # 이름을 지워 원래대로 돌려도 옛 이름이 따라붙는 것처럼 보인다.
        if key != name:
            labels[key] = name
        _write(names, labels, now)
    return key


def remove_custom_group(name: str, now: Optional[datetime] = None) -> None:
    """소제목을 지운다 — 빈 소제목 헤더의 🗑️(삭제) 버튼이 호출한다.

    이미 기사가 들어가 있는 소제목이라도 이 목록에서만 빠질 뿐, 그 기사들의 소속
    (group_overrides.json)은 그대로 남는다 — 다만 app.classifier._apply_forced_groups는
    이 목록(custom_group_names)에 없는 이름을 빈 그룹으로 미리 만들어주지 않으므로,
    지운 뒤에는 그 기사들도 자동 분류(또는 "기타")로 조용히 되돌아간다(자정 지나 이
    목록 전체가 비워질 때와 동일한 자기 치유 동작).
    """
    names = [n for n in load_custom_groups(now) if n != name]
    # [수정: 2026-10-19] 화면 이름 매핑은 **같이 지우지 않는다.** 이 목록에서 빠져도 그
    # 이름으로 저장된 기사(배정 기록·회차 파일)는 그대로 남고, _apply_forced_groups가
    # 그 기사가 보이는 한 칸을 되살린다 — 매핑만 지우면 되살아난 칸이 속이름을 드러낸다.
    # (load_custom_group_labels 참고: 매핑은 기사보다 오래 살아야 한다.)
    _write(names, load_custom_group_labels(now), now)
