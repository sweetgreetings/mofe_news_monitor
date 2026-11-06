"""저장 한 겹의 **Postgres 백엔드** — Supabase에 두고 여러 컴퓨터가 같은 데이터를 본다.

**왜 있나.** 「각자 자기 컴퓨터에 앱을 깔고 데이터는 한 곳을 공유」하는 구도를 쓰려면
저장소가 파일이 아니라 공용 DB여야 한다. 그러면 **다른 컴퓨터에서 화면을 열 문제가
통째로 사라진다** — 각자 자기 `localhost`를 보고, 터널·포트 개방·고정 주소가 필요 없다.
(MULTIUSER_ROLLOUT_CHECKLIST.md 2단계 순서 4번.)

**갈아끼우는 자리가 여기 하나다.** `app/store.py`의 공개 함수 아홉이 백엔드 함수
아홉을 부르게 되어 있어(①에서 그 모양으로 만들어 뒀다) 도메인 모듈 42개는 한 글자도
안 바뀐다.

**켜는 법**: `.env`에 `DATABASE_URL`을 넣는다. **없으면 파일 백엔드 그대로다** —
그래서 지금 혼자 쓰는 상태가 한 글자도 안 바뀐다.

**`DATA_DIR` 밖의 파일은 여기로 오지 않는다**(`store._rel`이 `None`을 돌려주는 것들 —
`.env`·`AI_RULES.md`·테스트의 임시 폴더). 그것들은 늘 디스크에 있다.

**키가 곧 행이다.** `labels.json` · `users/{계정}/articles/2026-10-30_17-00.json` 처럼
`DATA_DIR` 기준 상대 경로가 기본키이고, 사람별 칸은 키 안에 이미 누구인지 적혀 있다.
JSON 덩어리를 한 칸에 담는 모양이라 `undo_stack.json`처럼 큰 자리는 쓰기가 무겁다 —
베타에서 재 보고, 무거우면 그 자리만 따로 손본다(EXPERT_QUESTIONS.md 3·4번).
"""
import logging
import os
import queue
import threading
import time
from fnmatch import fnmatch
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# 연결 주소 — Supabase 프로젝트의 「Connection string」(URI). 없으면 이 백엔드는 꺼진다.
_ENV_KEY = "DATABASE_URL"

_TABLE = "app_store"

_SCHEMA = f"""
create table if not exists {_TABLE} (
    key        text primary key,
    body       text not null,
    updated_at timestamptz not null default now()
);
"""

# **연결은 돌려 쓴다 — 스레드마다 하나씩 쥐면 안 된다.**
# 설정 서버는 요청마다 새 스레드를 만든다(ThreadingHTTPServer). 스레드마다 연결을
# 쥐면 그 스레드가 끝나도 연결이 곧바로 닫히지 않아, 화면을 몇 번 열면 Supabase
# 풀(session mode, 15개)이 바닥난다 — 실측 2026-11-05: 앱을 띄운 지 14분 만에
# `EMAXCONNSESSION: max clients reached`가 나고 모든 화면이 무한 로딩이 됐다.
# 그래서 **한도 있는 주머니**에 담아 빌려 쓰고 돌려준다. 한도는 풀보다 넉넉히
# 낮게 둔다 — 같은 DB를 보는 다른 컴퓨터·psql이 들어올 자리를 남겨야 한다.
#
# **주머니에 담는 것은 `(연결, 쉬기 시작한 시각)` 짝이다** — 아래 `_trim_idle`이
# 오래 쉰 것을 닫으려면 언제부터 쉬었는지를 알아야 한다.
_MAX_CONNECTIONS = 6
_POOL: queue.LifoQueue = queue.LifoQueue()
_SLOTS = threading.BoundedSemaphore(_MAX_CONNECTIONS)

# **오래 쉰 연결은 놓아 준다.** 쥐고만 있으면 둘이 함께 나빠진다 — ① 아무 일도 안 하는
# 새벽에도 Supabase 15자리 중 6을 차지하고(두 대면 12, 세 대면 아예 안 붙는다) ② 쉬는
# 연결이 늘 여섯 개 쌓여 있어, 서버가 그걸 닫을 때마다 여러 개가 **함께** 죽는다
# (실측 2026-11-06: 09:02·13:16·14:39 세 번. 마지막엔 여덟 자리가 한꺼번에 다시 붙다가
# 그중 하나가 「자리가 꽉 찼다」에 걸려 [단독]·[속보] 폴링 한 번이 건너뛰어졌다).
# 그래서 **서버가 닫기 전에 우리가 먼저 닫는다.** tick이 10초마다 DB를 두드리므로
# 쓰는 연결 한둘은 늘 따뜻하고, 화면 한 장이 한꺼번에 빌려 쓴 나머지는 2분 뒤에 빠진다.
#
# **재는 시계는 `time.time()`이다 — `monotonic`이 아니다.** 노트북이 잠든 동안
# `monotonic`은 멈추므로(macOS는 `CLOCK_UPTIME_RAW`), 여덟 시간 자고 깨어난 연결이
# 「방금 쉬기 시작한 것」으로 읽혀 멀쩡한 줄 알고 집게 된다. 그게 바로 우리가 피하려는
# 그 연결이다. 사람이 시계를 돌려 놓으면 멀쩡한 연결을 한 번 더 닫을 뿐이다
# (다시 붙는 값 하나 — 실측 2026-11-06: 94ms. 왕복 15.6ms보다 비싸지만, tick이
# 10초마다 지나가 쓰는 연결 하나는 늘 따뜻해 거의 치워지지 않는다).
_IDLE_MAX_SEC = 120.0
# 치우는 일은 한 번에 한 스레드만 한다 — 겹쳐 돌면 주머니를 비운 틈에 다른 스레드가
# 「빈 주머니」를 보고 새로 붙어, 치우려는 일이 연결을 오히려 늘린다.
_TRIM_LOCK = threading.Lock()

# 빌릴 자리가 날 때까지 기다리는 한도. 여기서 지치면 화면이 오류로 끝나는 게
# 영영 도는 것보다 낫다 — 담당자가 무엇이 잘못됐는지 알 수 있다.
_ACQUIRE_TIMEOUT_SEC = 30.0

_schema_ready = False
_schema_lock = threading.Lock()


def database_url() -> Optional[str]:
    """지금 설정된 연결 주소. 없으면 `None`(= 파일 백엔드를 쓴다)."""
    return (os.getenv(_ENV_KEY) or "").strip() or None


def is_enabled() -> bool:
    return database_url() is not None


# **「자리가 꽉 찼다」는 응답일 때만 기다렸다 다시 붙는다.** 서버가 쉬는 연결을 한꺼번에
# 닫으면 우리 스레드들이 같은 순간에 새로 붙는데, 그때 서버 쪽 자리가 잠깐 모자랄 수
# 있다 — 1초면 풀리는 일인데 지금은 거기서 곧바로 포기해 **그 일이 통째로 날아갔다**
# (실측 2026-11-06 14:39: [단독]·[속보] 폴링 한 번).
#
# **비밀번호·주소가 틀린 경우는 기다리지 않는다** — 기다려도 안 되는 일이고, 켤 때
# 수백 번 부르는 자리라 한 번에 1.6초씩 붙으면 앱이 멈춘 것처럼 보인다. 그래서 **아래
# 말이 든 오류만** 다시 해 본다. 목록은 실측으로만 채운다(사진 표식 층과 같은 방식).
_POOL_FULL_MARKS = (
    "max clients",           # Supabase(supavisor, session mode): (EMAXCONNSESSION)
    "max_client_conn",       # pgbouncer
    "too many clients",      # Postgres: sorry, too many clients already
    "too many connections",  # Postgres: too many connections for role …
)

# 기다리는 시간 — 이만큼 더 해 본다(여기 둘이면 모두 세 번). 자리를 쥔 채 기다리므로
# 합이 `_ACQUIRE_TIMEOUT_SEC`(30초)보다 훨씬 짧아야 한다.
_CONNECT_RETRY_WAITS_SEC = (0.4, 1.2)


def _looks_pool_full(exc: Exception) -> bool:
    """「서버 쪽 자리가 꽉 찼다」는 오류인가 — 기다렸다 다시 해 볼 값어치가 있는 것만 참."""
    text = str(exc).lower()
    return any(mark in text for mark in _POOL_FULL_MARKS)


def _new_connection():
    """새 연결 하나. psycopg가 없으면 바로 말해 준다.

    **자리가 꽉 찼다면 잠깐 기다려 다시 붙는다**(위 `_POOL_FULL_MARKS`). 그 밖의
    오류는 기다려도 안 되는 일이라 그대로 올린다.
    """
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - 설치 안내용
        raise RuntimeError(
            "DATABASE_URL이 설정돼 있는데 psycopg가 없습니다 — "
            "`pip install -r requirements-postgres.txt`로 설치하세요."
        ) from exc

    # autocommit: 이 백엔드의 한 호출이 곧 한 문장이라 트랜잭션을 따로 열지 않는다.
    for wait in _CONNECT_RETRY_WAITS_SEC:
        try:
            return psycopg.connect(database_url(), autocommit=True)
        except Exception as exc:
            if not _looks_pool_full(exc):
                raise
            logger.warning("Postgres 자리가 꽉 찼습니다 — %.1f초 뒤 다시 붙어 봅니다.", wait)
            time.sleep(wait)
    # 마지막 한 번 — 여기서 실패하면 그대로 올린다(포기할 때는 조용히 넘기지 않는다).
    return psycopg.connect(database_url(), autocommit=True)


def _trim_idle() -> None:
    """오래 쉰 연결을 닫는다 — 서버가 닫기 전에 우리가 먼저(위 `_IDLE_MAX_SEC`).

    주머니를 통째로 훑는다. **위에서부터 보다가 멈추면 안 된다** — 꺼내는 순서가
    마지막에 넣은 것부터(LifoQueue)라 **맨 위 하나만 계속 돌려 쓰이고 바닥의 것들이
    그대로 썩는다.** 훑은 뒤에는 넣은 순서를 되돌려 놓는다(따뜻한 것이 다시 맨 위로).
    """
    if not _TRIM_LOCK.acquire(blocking=False):
        return  # 다른 스레드가 이미 치우고 있다 — 겹쳐 돌 일이 아니다
    try:
        now = time.time()
        keep = []
        while True:
            try:
                item = _POOL.get_nowait()
            except queue.Empty:
                break
            conn, idle_since = item
            if conn.closed or now - idle_since > _IDLE_MAX_SEC:
                try:
                    conn.close()
                except Exception:
                    pass
            else:
                keep.append(item)
        for item in reversed(keep):  # 꺼낸 순서의 거꾸로 = 원래 순서
            _POOL.put(item)
    finally:
        _TRIM_LOCK.release()


def _acquire(*, fresh: bool = False):
    """연결 하나를 빌린다 — 주머니에 있으면 그것, 없으면 새로 만든다.

    **`fresh`면 주머니를 건너뛰고 새로 붙는다.** 서버가 쉬고 있는 연결을 닫을 때는
    주머니 안 여러 개가 한꺼번에 죽는데, **psycopg는 써 보기 전까지 그걸 모른다**
    (`conn.closed`가 거짓이라 멀쩡한 것처럼 건네진다). 그래서 다시 해 볼 때 주머니에서
    또 집으면 **또 죽은 것을 집어** 그대로 포기한다 — 실측 2026-11-06 09:02: 그 때문에
    tick 하나가 통째로 건너뛰어졌다(사람 둘 몫의 수집·자동발송·정리가 함께 밀렸고,
    로그에는 「연결이 끊겨 다시 붙습니다」 두 줄과 ERROR만 남았다).
    """
    if not _SLOTS.acquire(timeout=_ACQUIRE_TIMEOUT_SEC):
        raise RuntimeError(
            f"Postgres 연결을 {_ACQUIRE_TIMEOUT_SEC:.0f}초 기다렸지만 자리가 안 났습니다."
        )
    try:
        # 빌리기 전에 오래 쉰 것을 치운다 — tick이 10초마다 여기를 지나므로 이 한 줄이
        # 곧 「쉬는 연결을 쥐고 있지 않기」다(따로 도는 스레드를 두지 않는다).
        _trim_idle()
        while not fresh:
            try:
                conn, _ = _POOL.get_nowait()
            except queue.Empty:
                break
            if not conn.closed:
                return conn
        return _new_connection()
    except Exception:
        _SLOTS.release()
        raise


def _release(conn, *, broken: bool = False) -> None:
    """빌린 연결을 돌려준다. 상한 것은 닫아 버리고 자리만 비운다."""
    if conn is not None:
        if broken or conn.closed:
            try:
                conn.close()
            except Exception:
                pass
        else:
            # 돌려주는 지금이 「쉬기 시작한 시각」이다.
            _POOL.put((conn, time.time()))
    _SLOTS.release()


def close_all() -> None:
    """쥐고 있는 연결을 전부 놓는다 — 앱을 접을 때와 테스트용."""
    while True:
        try:
            conn, _ = _POOL.get_nowait()
        except queue.Empty:
            return
        try:
            conn.close()
        except Exception:
            pass


def _run(sql: str, params: tuple = (), *, fetch: str = ""):
    """문장 하나를 돌린다. 연결이 끊겨 있으면 **새 연결로 한 번** 다시 해 본다.

    노트북이 잠들었다 깨면 연결이 죽어 있는데, 그때 화면 한 번이 실패하고 끝나면
    담당자는 앱이 고장난 것으로 읽는다.

    **다시 할 때는 주머니를 건너뛴다**(`fresh=True`) — 거기서 또 집으면 같이 죽은
    연결을 또 집는다(`_acquire` 참고). 죽은 줄 알게 된 연결은 `broken=True`로 닫아
    주머니에서 내보내므로, 여러 개가 함께 죽어도 몇 번 오가는 사이에 저절로 치워진다.
    """
    for attempt in (1, 2):
        conn = _acquire(fresh=attempt == 2)
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                if fetch == "one":
                    out = cur.fetchone()
                elif fetch == "all":
                    out = cur.fetchall()
                else:
                    out = cur.rowcount
        except Exception:
            _release(conn, broken=True)
            if attempt == 2:
                raise
            logger.warning("Postgres 연결이 끊겨 다시 붙습니다.")
            continue
        _release(conn)
        return out
    return None


def ensure_schema() -> None:
    """표를 한 번 만든다(이미 있으면 아무 일도 안 한다)."""
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        _run(_SCHEMA)
        _schema_ready = True


# ─────────────────── 백엔드 함수 아홉 ───────────────────
# `app/store.py`의 `_fs_*`와 **같은 약속**을 지킨다 — 특히 없는 자리를 읽으면
# `FileNotFoundError`(OSError의 한 종류)를 올린다. 도메인 모듈 42개가 들고 있는
# try/except가 그 예외를 잡고 있어서, 여기서 다른 예외를 내면 그 판단이 전부 어긋난다.

def read_text(key: str) -> str:
    ensure_schema()
    row = _run(f"select body from {_TABLE} where key = %s", (key,), fetch="one")
    if row is None:
        raise FileNotFoundError(key)
    return row[0]


def write_text(key: str, text: str) -> None:
    ensure_schema()
    _run(
        f"""insert into {_TABLE} (key, body, updated_at) values (%s, %s, now())
            on conflict (key) do update set body = excluded.body, updated_at = now()""",
        (key, text),
    )


def exists(key: str) -> bool:
    ensure_schema()
    return _run(f"select 1 from {_TABLE} where key = %s", (key,), fetch="one") is not None


def delete(key: str, *, missing_ok: bool) -> None:
    ensure_schema()
    removed = _run(f"delete from {_TABLE} where key = %s", (key,))
    if not removed and not missing_ok:
        raise FileNotFoundError(key)


def move(src_key: str, dst_key: str) -> None:
    """옮긴다 — 받는 자리에 이미 있으면 덮는다(`Path.replace`와 같은 약속)."""
    ensure_schema()
    _run(f"delete from {_TABLE} where key = %s", (dst_key,))
    moved = _run(f"update {_TABLE} set key = %s where key = %s", (dst_key, src_key))
    if not moved:
        raise FileNotFoundError(src_key)


def ensure_dir(key: str) -> None:
    """DB엔 폴더가 없다 — 할 일이 없다."""
    return None


def _head(prefix: str) -> str:
    """키 접두사 — 칸 이름 뒤에 `/`. 뿌리(`.`)면 빈 문자열이다."""
    return f"{prefix}/" if prefix and prefix != "." else ""


def _in_box(rows, head: str, pattern: str, recursive: bool = False):
    """그 칸에 **바로 든** 행만 골라 `(키, 행)`으로 흘린다.

    `Path.glob("*.json")`이 아래 폴더로 내려가지 않는 것과 같아야 하므로, 접두사
    뒤에 `/`가 더 있는 키(= 아래 칸의 것)는 뺀다. 이름 대조는 `fnmatch`로 한다 —
    `*.deleted-*.json` 같은 모양을 SQL `like`로 옮기면 틈이 생긴다.
    """
    for row in rows:
        key = row[0]
        rest = key[len(head):]
        if "/" in rest and not recursive:
            continue
        if fnmatch(rest, pattern):
            yield key, row


def _sig_of(updated_at) -> tuple:
    """바뀌었는지 보는 값 — 파일의 `(mtime_ns, size)` 자리를 **갱신 시각만** 받는다.

    **길이를 세지 않는다.** 예전엔 `length(body)`를 둘째 값으로 넣었는데, 그 함수는
    따로 압축해 보관된 본문(TOAST)을 **전부 펼쳐야** 글자 수를 셀 수 있다 — 실측
    2026-11-06: 회차 404개의 서명 목록이 **105ms**이고 길이를 빼면 **17ms**다(본문
    합계 28MB를 쿼리마다 다 읽고 있었다). 회차는 365일 쌓이므로 1,600개면 그만큼
    는다. 왕복 한 번이 15ms인 자리에서 쿼리 하나가 105ms인 것은 「느린 쿼리가 하나도
    없다」는 전제가 깨진 첫 자리다.

    **갱신 시각만으로 충분하다** — `write_text`가 쓸 때마다 `updated_at = now()`로
    고쳐 적으므로 내용이 바뀌면 이 값이 반드시 바뀐다. 파일에서 `size`를 함께 본 것은
    `mtime`의 해상도가 거칠어서였고, 여기선 마이크로초다. **그래서 `updated_at`을 안
    고치는 쓰기 경로를 만들지 않는다** — 만들면 바뀐 자리를 아무도 못 알아본다.

    둘째 값 `0`은 **자리를 지키려고 둔다** — 부르는 쪽(`app/run_index.py`)이 둘을
    풀어 쓰므로 모양이 백엔드마다 달라지면 그 자리가 깨진다. 값은 같은지 보는 데만
    쓰이고 「크기」로 읽는 자리는 없다.
    """
    return (int(updated_at.timestamp() * 1_000_000_000), 0)


def list_keys(prefix: str, pattern: str, *, recursive: bool = False) -> list:
    """그 칸에 **바로 든** 키들 — 이름 사전순. `recursive`면 아래 칸까지."""
    ensure_schema()
    head = _head(prefix)
    rows = _run(
        f"select key from {_TABLE} where key like %s order by key", (head + "%",), fetch="all"
    ) or []
    return [key for key, _ in _in_box(rows, head, pattern, recursive)]


def signature(key: str):
    """바뀌었는지 보는 값 — 파일의 `(mtime_ns, size)` 자리를 갱신 시각·길이가 받는다."""
    ensure_schema()
    row = _run(f"select updated_at from {_TABLE} where key = %s", (key,), fetch="one")
    if row is None:
        return None
    return _sig_of(row[0])


def list_signatures(prefix: str, pattern: str) -> dict:
    """`{키: 서명}` — **목록과 서명을 한 번에** 받는다(왕복 한 번).

    키마다 따로 물으면 키 수만큼 왕복이 된다 — 회차 403개면 403회(5.8초)이고, 홈은
    그걸 11번 되풀이해 63.8초를 썼다(실측 2026-11-05 · 홈 전체의 87%). 파일일 때는
    `stat()`이 1µs라 「목록 따로 · 서명 따로」가 공짜였지만, **DB에선 묻는 횟수가
    유일한 비용이다.** 서명은 갱신 시각 하나라 본문도 길이도 담지 않아(`_sig_of` 참고)
    한 번에 받아도 가볍다.
    """
    ensure_schema()
    head = _head(prefix)
    rows = _run(
        f"select key, updated_at from {_TABLE} where key like %s order by key",
        (head + "%",),
        fetch="all",
    ) or []
    return {key: _sig_of(row[1]) for key, row in _in_box(rows, head, pattern)}


# ─────────────────── 지금 data/를 DB로 올리기 ───────────────────

def upload_from_files(data_dir, *, dry_run: bool = False) -> dict:
    """디스크의 `data/`를 통째로 DB에 올린다 — **한 번 하는 이사**다.

    이미 DB에 있는 키는 **건드리지 않는다**(덮지 않는다) — 두 번 돌려도 안전하고,
    올리다 멈췄으면 다시 돌려 마치면 된다. 로그·임시 파일은 올리지 않는다.
    """
    data_dir = Path(data_dir)
    skip_top = {"logs"}
    done, already, skipped = [], [], []
    if not dry_run:
        ensure_schema()
    for path in sorted(data_dir.rglob("*")):
        if not path.is_file():
            continue
        key = path.relative_to(data_dir).as_posix()
        top = key.split("/")[0]
        if top in skip_top or path.name.endswith(".tmp"):
            skipped.append(key)
            continue
        if not dry_run and exists(key):
            already.append(key)
            continue
        if not dry_run:
            write_text(key, path.read_text(encoding="utf-8"))
        done.append(key)
    return {"uploaded": done, "already": already, "skipped": skipped}


def _safe_rel(key: str) -> Optional[Path]:
    """키를 폴더 안 상대 경로로 바꾼다 — 폴더 밖으로 나가는 키는 `None`.

    키는 `DATA_DIR` 기준 상대 경로라 평소엔 멀쩡하지만, **받아 적는 쪽에서 한 번 더
    본다** — 올라간 키가 `../`나 절대 경로면 받는 순간 폴더 밖 파일을 덮는다.
    """
    if not key or key.startswith("/") or "\\" in key:
        return None
    rel = Path(key)
    if rel.is_absolute() or any(part in ("..", "") for part in rel.parts):
        return None
    return rel


def download_to_files(dest_dir, *, dry_run: bool = False) -> dict:
    """DB를 통째로 폴더에 받는다 — `upload_from_files`의 반대이고 **백업이다**.

    **`data/`에 받지 않는 것이 이 함수의 전제다.** 받는 곳은 호출부가 정하는 빈
    날짜 폴더다 — `data/`에 덮으면 DB가 상했을 때(테스트가 덮어쓴 2026-11-05 같은
    경우) 성한 디스크 원본까지 같이 날아간다. 그 폴더가 마지막 구명줄이었다.

    로그는 애초에 DB에 없다. 폴더 밖을 가리키는 키는 `skipped`로 비켜 둔다.
    """
    dest_dir = Path(dest_dir)
    ensure_schema()
    rows = _run(f"select key, body from {_TABLE} order by key", fetch="all") or []
    done, skipped = [], []
    for key, body in rows:
        rel = _safe_rel(key)
        if rel is None:
            skipped.append(key)
            continue
        if not dry_run:
            target = dest_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding="utf-8")
        done.append(key)
    return {"downloaded": done, "skipped": skipped}


def restore_from_files(src_dir, *, dry_run: bool = False) -> dict:
    """폴더의 내용으로 DB를 **덮는다** — `upload_from_files`와 달리 이미 있어도 덮는다.

    되돌리기 전용이라 부르는 쪽이 반드시 사람에게 한 번 묻는다(`supabase_setup.py`).
    폴더에 없는 키는 **DB에 그대로 둔다** — 그 폴더를 뜬 뒤에 생긴 것일 수 있어
    지우는 쪽이 더 위험하다.
    """
    src_dir = Path(src_dir)
    if not dry_run:
        ensure_schema()
    done, skipped = [], []
    for path in sorted(src_dir.rglob("*")):
        if not path.is_file():
            continue
        key = path.relative_to(src_dir).as_posix()
        if key.split("/")[0] == "logs" or path.name.endswith(".tmp"):
            skipped.append(key)
            continue
        if not dry_run:
            write_text(key, path.read_text(encoding="utf-8"))
        done.append(key)
    return {"restored": done, "skipped": skipped}
