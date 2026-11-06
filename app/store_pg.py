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
_MAX_CONNECTIONS = 6
_POOL: queue.LifoQueue = queue.LifoQueue()
_SLOTS = threading.BoundedSemaphore(_MAX_CONNECTIONS)

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


def _new_connection():
    """새 연결 하나. psycopg가 없으면 바로 말해 준다."""
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - 설치 안내용
        raise RuntimeError(
            "DATABASE_URL이 설정돼 있는데 psycopg가 없습니다 — "
            "`pip install -r requirements-postgres.txt`로 설치하세요."
        ) from exc
    # autocommit: 이 백엔드의 한 호출이 곧 한 문장이라 트랜잭션을 따로 열지 않는다.
    return psycopg.connect(database_url(), autocommit=True)


def _acquire():
    """연결 하나를 빌린다 — 주머니에 있으면 그것, 없으면 새로 만든다."""
    if not _SLOTS.acquire(timeout=_ACQUIRE_TIMEOUT_SEC):
        raise RuntimeError(
            f"Postgres 연결을 {_ACQUIRE_TIMEOUT_SEC:.0f}초 기다렸지만 자리가 안 났습니다."
        )
    try:
        while True:
            try:
                conn = _POOL.get_nowait()
            except queue.Empty:
                return _new_connection()
            if not conn.closed:
                return conn
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
            _POOL.put(conn)
    _SLOTS.release()


def close_all() -> None:
    """쥐고 있는 연결을 전부 놓는다 — 앱을 접을 때와 테스트용."""
    while True:
        try:
            conn = _POOL.get_nowait()
        except queue.Empty:
            return
        try:
            conn.close()
        except Exception:
            pass


def _run(sql: str, params: tuple = (), *, fetch: str = ""):
    """문장 하나를 돌린다. 연결이 끊겨 있으면 **한 번** 다시 붙어 본다.

    노트북이 잠들었다 깨면 연결이 죽어 있는데, 그때 화면 한 번이 실패하고 끝나면
    담당자는 앱이 고장난 것으로 읽는다.
    """
    for attempt in (1, 2):
        conn = _acquire()
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
