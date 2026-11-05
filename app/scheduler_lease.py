"""스케줄러를 **한 대만** 돌리게 하는 임차(lease) — 앱이 여러 대에서 켜질 때.

**중복 실행 자물쇠(포트 `127.0.0.1:8765`)는 한 대 안에서만 듣는다.** 다른 컴퓨터는
그 포트를 모르므로 자물쇠에 안 걸리고, 두 대가 켜져 있으면 **같은 회차를 두 번
모으고 자동발송이 두 번 나간다**(`main.py`가 중복 실행에 대고 경고하는 그 증상이다).
사람별 저장소가 갈라 둔 것은 「누구의 데이터인가」이지 「누가 모으는가」가 아니다.

그래서 **「지금 누가 수집 담당인가」를 DB에 적고 시간을 걸어 둔다.** 한 대가 그 자리를
쥐고 tick마다 갱신하고, 나머지는 그 tick의 일을 통째로 건너뛴다. 쥔 쪽이 죽거나
노트북이 잠들면 `LEASE_SEC`이 지나 임차가 풀리고 다른 대가 이어받는다.

**파일로 쓸 때는 아무것도 하지 않는다**(`hold()`가 늘 참) — 그때는 한 대뿐이라는 것을
포트 자물쇠가 이미 보장하고, 혼자 쓰는 지금의 동작이 한 글자도 안 바뀐다.

## 왜 DB의 한 문장인가

읽고 나서 쓰면 두 대가 같은 순간에 「비어 있네」를 보고 **둘 다 가져간다.** 그래서
`insert … on conflict do update … where`를 **한 문장**으로 돌린다 — 조건에 맞을 때만
행이 바뀌고, 바뀐 행을 돌려받은 쪽만 임차를 쥔다. 판정과 쓰기가 갈라질 틈이 없다.

## 왜 JSON이 아닌가

몸통은 `holder\\n만료시각` 두 줄짜리 맨 글자다. `body::jsonb`로 읽으면 **몸통이 한 번
깨지는 순간 그 문장이 매번 오류를 내고, 그러면 어느 대도 임차를 못 쥐어 수집이 통째로
멈춘다.** `split_part`는 깨진 글자에도 그냥 빈 값을 돌려줘 **다음 tick에 누군가 이어받는다.**
만료 시각은 **고정 폭 UTC ISO**라 글자 비교가 곧 시각 비교다.
"""
from __future__ import annotations

import logging
import os
import socket
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# 임차가 살아 있는 시간. tick(10초)보다 넉넉히 길어야 한다 — 갱신이 한두 번 늦어도
# 남이 가로채지 않을 만큼. 쥔 쪽이 죽으면 이 시간 뒤에 다른 대가 이어받는다.
LEASE_SEC = 60

# DB에 적히는 자리. `DATA_DIR` 기준 상대 경로 모양이라 백업(`내려받기`)에도 같이 담기고,
# 되돌려도 만료된 임차라 아무 영향이 없다.
LEASE_KEY = "scheduler_lease"

_SEP = "\n"

# 이 프로세스를 가리키는 이름 — 사람이 로그에서 읽을 수 있게 컴퓨터 이름을 앞에 둔다.
# 같은 컴퓨터에서 두 번 켜도 갈리도록 뒤에 임의의 글자를 붙인다(포트 자물쇠가 막지만,
# 임차가 그 가정에 기대지 않게 한다).
INSTANCE_ID = f"{socket.gethostname()}#{os.getpid()}#{uuid.uuid4().hex[:6]}"


def _pg():
    """Postgres를 쓰고 있으면 그 모듈, 아니면 `None`."""
    from app import store_pg

    return store_pg if store_pg.is_enabled() else None


def _utc_iso(moment: datetime) -> str:
    """고정 폭 UTC ISO — 글자 비교가 곧 시각 비교가 되게 한다."""
    if moment.tzinfo is None:
        moment = moment.astimezone()
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def hold(now: Optional[datetime] = None) -> bool:
    """지금 이 프로세스가 **공용 일(수집·발송·정리)을 해도 되는가**.

    참이면 임차를 새로 쥐었거나 갱신한 것이고, 거짓이면 다른 대가 쥐고 있다는 뜻이라
    **그 tick의 일을 통째로 건너뛴다.**

    DB가 잠깐 안 될 때는 **거짓**이다 — 모르면 하지 않는 쪽이 안전하다. 두 대가 모두
    「모르겠으니 일단 한다」로 가면 막으려던 중복이 그대로 일어난다.
    """
    backend = _pg()
    if backend is None:
        return True  # 파일로 쓸 때는 포트 자물쇠가 이미 한 대를 보장한다

    moment = now or datetime.now()
    body = INSTANCE_ID + _SEP + _utc_iso(moment + timedelta(seconds=LEASE_SEC))
    sql = (
        f"insert into {backend._TABLE} (key, body, updated_at) values (%s, %s, now()) "
        f"on conflict (key) do update set body = excluded.body, updated_at = now() "
        f"where split_part({backend._TABLE}.body, %s, 1) = %s "
        f"   or split_part({backend._TABLE}.body, %s, 2) < %s "
        f"returning key"
    )
    params = (LEASE_KEY, body, _SEP, INSTANCE_ID, _SEP, _utc_iso(moment))
    try:
        backend.ensure_schema()
        return backend._run(sql, params, fetch="one") is not None
    except Exception:
        logger.warning("스케줄러 임차를 확인하지 못했습니다 — 이번 tick은 건너뜁니다.", exc_info=True)
        return False


def holder() -> Optional[str]:
    """지금 임차를 쥔 쪽의 이름 — 아무도 없거나 만료됐으면 `None`. 진단용이다."""
    backend = _pg()
    if backend is None:
        return INSTANCE_ID
    # 저장소 한 겹(`store.read_text`)을 거치지 않는다 — 여기는 **저장소를 읽는 게
    # 아니라 임차 줄 하나를 들여다보는 진단**이고, 그 겹은 파일/DB를 가르는 자리라
    # 파일로 쓸 때는 이 키 자체가 없다(위에서 이미 돌아갔다).
    try:
        row = backend._run(
            f"select body from {backend._TABLE} where key = %s", (LEASE_KEY,), fetch="one"
        )
    except Exception:
        return None
    if not row:
        return None
    name, _, until = (row[0] or "").partition(_SEP)
    if not until or until < _utc_iso(datetime.now()):
        return None
    return name


def release() -> None:
    """쥐고 있던 임차를 놓는다 — 내 것일 때만. 앱을 정상 종료할 때 쓰면 다른 대가
    `LEASE_SEC`을 기다리지 않고 바로 이어받는다. 실패해도 만료가 받아 준다."""
    backend = _pg()
    if backend is None:
        return
    try:
        backend._run(
            f"delete from {backend._TABLE} where key = %s "
            f"and split_part(body, %s, 1) = %s",
            (LEASE_KEY, _SEP, INSTANCE_ID),
        )
    except Exception:
        logger.warning("스케줄러 임차를 놓지 못했습니다 — 만료를 기다립니다.", exc_info=True)
