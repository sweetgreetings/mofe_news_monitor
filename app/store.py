"""저장소 한 겹 — 저장 모듈이 「파일인지 DB인지」를 모르게 한다.

**왜 있나.** 지금은 도메인 모듈 42개가 각자 자기 파일을 직접 읽고 쓴다
(`labels.py` → `labels.json`). 여러 사람이 쓰게 되면 그 42곳이 전부
「누구의 데이터인가」와 「파일인가 Postgres인가」를 알아야 하는데, 그건
42곳에 같은 규칙을 42번 적는 일이다. 이 한 겹이 그 둘을 혼자 안다
(MULTIUSER_ROLLOUT_CHECKLIST.md 2단계 작업 순서 1번).

**이 겹을 넣는 동안 앱 동작은 한 글자도 바뀌지 않아야 한다.** 그래서 모양을
지금 코드와 1:1로 맞췄다:

- **예외를 삼키지 않는다.** `read_json`은 `Path.read_text` + `json.loads`가
  내던 그대로 `OSError`·`ValueError`를 올린다. 모듈마다 「없으면 빈 것 /
  깨졌으면 다시 만들기 / 경고 로그」가 다른데, 이 겹이 기본값을 정해 주면
  그 판단이 조용히 하나로 뭉개진다. 지금 각 모듈이 들고 있는 try/except가
  그대로 계약이다.
- **쓰기 모양도 그대로다.** `ensure_ascii=False`는 42곳이 전부 쓰므로 여기
  기본값이고, `indent`는 자리마다 달라(`undo_stack.json`은 하루 6.9MB라
  들여쓰기가 곧 용량이다) 부르는 쪽이 적는다.

**키는 `DATA_DIR` 기준 상대 경로다**(`key_of`). 지금은 그 키가 곧 파일
경로이지만, Postgres로 갈 때 바뀌는 자리는 이 모듈 안 `_fs_*` 함수들뿐이고
부르는 쪽은 Path를 그대로 넘긴다 — 테스트가 임시 폴더로 갈아끼우는 방식
(`patch.object(labels, "LABELS_FILE", …)`)도 그대로 돈다.

**새 저장 자리를 만들면 반드시 이 모듈을 거친다.** `tests/test_integration.py`
`test_storage_goes_through_one_place`가 그 상태를 지킨다 — 파일 API를 직접
부르는 자리가 생기면 거기서 깨져 "이것도 같이 옮겨야 한다"가 그 자리에서
드러난다.
"""
import json
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from app.config import DATA_DIR

# 임시 파일 이름에 스레드 ID를 넣어 동시에 쓰는 다른 스레드와 겹치지 않게 한다
# (`app/atomic_write.py`가 화면 HTML에 쓰는 것과 같은 방식·같은 이유).
_TMP_SUFFIX = ".{ident}.tmp"

# 사람별 데이터가 사는 칸 — `data/users/{계정 id}/…`
_USERS_SEGMENT = "users"


# ──────────────────── 사람별인가 공용인가 ────────────────────
# **가르는 기준은 이미 이 저장소에 있다** — CLAUDE.md 「기관 기사 원장」의 그 표다:
# 「이 기사에 관한 사실」인가(제목·요약·사진 판정·라벨 — 누가 보든 같아야 한다),
# 아니면 「이 기사를 내 보고서에 어쩔 건가」인가(숨김·소제목 배정·순서 — 사람마다
# 다르다). ③ 사용자 경계는 그 기준을 저장소 전체로 넓힌 것뿐이다.
#
# **칸 이름은 `DATA_DIR` 바로 아래 첫 조각이다** — `articles/2026-10-30_17-00.json`도
# `adhoc/cards/x.json`도 첫 조각(`articles`·`adhoc`)으로 갈리므로 표가 한 줄이면 된다.
#
# **표에 없는 칸은 쓸 수 없다**(`_resolve`가 거부한다) — 새 저장 자리를 만들면
# 「이건 사람별인가 공용인가」를 여기서 한 번 정하고 지나가게 한다. 빠뜨리면
# 「공용인 줄 알았는데 사람별이었다」가 조용히 생기고, 그건 남의 보고서가 내 화면에
# 섞이는 사고다. `tests/test_integration.py` `test_user_boundary`가 지킨다.

_PER_USER = {
    # 내 회차 — 내 검색어가 만든 것이고, 지운 것도 내 것이다
    "articles", "articles_index.json", "articles_trash",
    # 내가 보고서에서 뺀 기사
    "hidden_articles.json", "hidden_archive.json",
    # 내가 맞춘 소제목 — 배정·이름표·순서·직접 만든 것·AI가 한 번 배정했는지
    "group_overrides.json", "group_labels.json", "group_order.json",
    "custom_groups.json", "assigned_groups.json",
    # 내가 맞춘 초안 — 순서·붙잡은 기사·예약 기사·담아둔 기사
    "preview_order.json", "draft_seen.json", "draft_pending_articles.json",
    "manual_articles.json",
    # 내가 쓴 한 줄 메모 · 내가 끊은 회차 · 내 되돌리기
    "manual_keyword_note.json", "today_cuts.json", "undo_stack.json",
    # 내 회차에 딸린 캐시 — 남의 회차 분류를 내 회차에 쓰면 소제목이 섞인다
    "llm_classification_cache.json", "preview_cache.json",
    "auto_classify_turns.json", "negative_guess.json",
    # 내 수시 카드(원본·확정본·사안·되돌리기·휴지통이 모두 이 칸 아래다)
    "adhoc",
}

_SHARED = {
    # ── 이 기사에 관한 사실 — 누가 보든 같아야 한다 ──
    "summary_overrides.json", "photo_overrides.json", "schedule_overrides.json",
    "photo_body_verdicts.json",
    # 라벨은 「이 기사가 무엇인가」라 두 흐름을 가로지르는 URL 키 전역 저장소다
    # (CLAUDE.md 「라벨」). 제목 변경 감시도 그 라벨을 보고 돈다.
    "labels.json", "label_undo.json", "title_watch.json",
    # 기관 기사 원장 — 「그날 무엇이 있었나」는 사람과 무관한 책이고,
    # 범위도 app/config.py 상수 고정이라 사람마다 다를 수 없다
    "agency_archive",
    # ── 앱이 한 벌로 쓰는 것 ──
    # 설정 — 지금은 전부 관리자 설정이라 한 벌이다. **검색어·형광펜·형식은
    # 사람별이 될 자리인데 `settings.json` 한 칸에 수집 시간·언론사·자동발송과
    # 섞여 있어** 파일 단위로 가를 수 없다(쪼개는 일은 따로다).
    "settings.json",
    # 공용 키 — 서버로 옮길 예정이고, 호출량도 그 키 하나로 센다
    "credentials.json", "api_usage.json",
    # [단독]·[속보] 알림 — 감시 범위가 공용 검색어 설정에 달려 있다
    "alerted_urls.json", "breaking_alert.json", "breaking_alert_state.json",
    # 받는 사람 명단·봇 이름 — 봇 이름은 봇에 붙은 값이라 바꾸면 모두에게 바뀐다
    "telegram_recipients.json", "email_recipients.json", "telegram_bot_name.json",
    # 발송 기록 — 「앱이 보낸 모든 메시지」라 보고서와 알림이 한 줄씩 섞여 있다
    # (사람별로 가르려면 줄마다 누가 보냈는지를 먼저 적어야 한다)
    "send_log",
    # 정책 단어 추이에 올린 단어 — 한 벌(최대 8개)이라 모두의 그래프가 같다
    "home_trend_words.json",
    # 오늘 전체 기사의 키워드별 캐시 — **키가 키워드라 사람 사이에 섞이지 않고**,
    # 공유하면 각자의 새로고침이 각자 네이버를 부르지 않는다
    # (MULTIUSER_ROLLOUT_CHECKLIST.md §네이버 쿼터의 「사용자 간 공유」가 이것이다)
    "live_cache.json",
    # 계정·세션·잠금·자물쇠 스위치, 그리고 로그
    "users.json", "auth_sessions.json", "auth_attempts.json", "auth_config.json",
    "logs",
    # 사람별 데이터가 사는 칸 자체 — 그 안쪽은 이미 누구 것인지 적혀 있다
    _USERS_SEGMENT,
}


# ──────────────────── 지금 누구의 데이터인가 ────────────────────
# **스레드마다 따로 쥔다** — 설정 서버는 요청마다 스레드가 생기고(`ThreadingHTTPServer`)
# 스케줄러는 또 다른 스레드다. 전역 변수 하나로 두면 한 사람의 요청이 다른 사람의
# 요청과 스케줄러까지 끌고 간다.
#
# **아무도 걸지 않으면 `None`이고, 그때 경로는 지금과 똑같다.** 혼자 쓰는 지금이
# 그 상태다 — 이 표와 맥락을 넣어도 파일이 한 개도 옮겨 가지 않는다.
_local = threading.local()


def current_owner() -> Optional[str]:
    """지금 이 스레드가 「누구의 데이터」를 다루는 중인가. 안 걸려 있으면 `None`."""
    return getattr(_local, "owner", None)


def set_owner(user_id: Optional[str]) -> None:
    """이 스레드가 다룰 사람을 정한다. `None`이면 사람 구분 없는 예전 경로."""
    _local.owner = user_id or None


@contextmanager
def owner(user_id: Optional[str]):
    """`with store.owner(uid):` — 끝나면 **원래 값으로 되돌린다**.

    되돌리는 것이 계약이다. 설정 서버의 스레드는 요청 사이에 재사용되므로
    (`ThreadingHTTPServer`가 요청마다 새로 만들지만 스케줄러·백그라운드 스레드는
    오래 산다), 걸어 두고 안 풀면 **다음 일이 남의 데이터를 만진다.**
    """
    before = current_owner()
    set_owner(user_id)
    try:
        yield
    finally:
        set_owner(before)


# ───────────────────────────── 키 ─────────────────────────────

def _rel(path: Path) -> Optional[str]:
    """`DATA_DIR` 기준 상대 경로. **밖에 있으면 `None`.**

    밖이 되는 경우는 셋이다 — 테스트가 갈아끼운 임시 폴더, 설치할 때 쓰는 `.env`,
    저장소에 함께 든 `AI_RULES.md`. 그 셋은 사람별로 가를 대상이 아니라서
    `_resolve`가 손대지 않는다(테스트 격리가 그대로 도는 것도 이 덕이다).
    """
    try:
        return str(path.resolve().relative_to(Path(DATA_DIR).resolve()))
    except ValueError:
        return None


def _resolve(path: Path) -> Path:
    """그 자리의 **실제 경로** — 사람별 칸이고 지금 누가 걸려 있으면 그 사람 폴더로.

    - `DATA_DIR` 밖이면 그대로(`_rel`).
    - 표에 없는 칸이면 **거부한다** — 새 저장 자리를 만들면서 「사람별인가 공용인가」를
      정하지 않고 지나가는 일을 막는다.
    - 공용 칸이거나 걸린 사람이 없으면 그대로. **혼자 쓰는 지금이 이 경우라 경로가
      한 글자도 안 바뀐다.**
    """
    rel = _rel(path)
    if rel is None or rel == ".":
        return path
    top = rel.split("/")[0]
    if top not in _PER_USER and top not in _SHARED:
        raise KeyError(
            f"저장소 칸 '{top}'이 사람별·공용 어느 쪽인지 안 정해져 있습니다 — "
            "app/store.py의 _PER_USER / _SHARED 표에 한 줄을 더하세요. "
            "「이 기사에 관한 사실」이면 공용, 「내 보고서에 어쩔 건가」면 사람별입니다."
        )
    uid = current_owner()
    if not uid or top not in _PER_USER:
        return path
    return Path(DATA_DIR) / _USERS_SEGMENT / uid / rel


def key_of(path) -> str:
    """그 자리의 논리 이름 — `DATA_DIR` 기준 상대 경로(`adhoc/cards/x.json`).

    Postgres로 갈 때 행을 찾는 열이 될 값이다. `DATA_DIR` 밖(테스트의 임시
    폴더·`.env` 씨앗)이면 파일 이름만 돌려준다 — 그 둘은 옮길 대상이 아니다.
    """
    p = Path(path)
    try:
        return str(p.resolve().relative_to(Path(DATA_DIR).resolve()))
    except ValueError:
        return p.name


# ──────────────────────────── 읽기 ────────────────────────────

def read_text(path) -> str:
    """원문 그대로. 없거나 못 읽으면 `OSError`(지금과 같다)."""
    return _fs_read_text(_resolve(Path(path)))


def read_json(path):
    """JSON을 파이썬 값으로. 없으면 `OSError`, 깨졌으면 `ValueError`."""
    return json.loads(read_text(path))


# ──────────────────────────── 쓰기 ────────────────────────────

def write_text(path, text: str) -> None:
    """원자적으로 쓴다 — 임시 파일에 쓴 뒤 교체해, 겹쳐 써도 반쪽 파일이 안 남는다."""
    target = _resolve(Path(path))
    # 사람별 칸으로 옮겨 쓰는 경우에만 그 사람 폴더를 만든다 — 공용 칸은 예전처럼
    # 폴더가 있다고 보고 바로 쓴다(없으면 `OSError`를 올리던 그 동작 그대로).
    if target != Path(path):
        _fs_ensure_dir(target.parent)
    _fs_write_text(target, text)


def write_json(path, data, *, indent: Optional[int] = None) -> None:
    """JSON으로 쓴다. `ensure_ascii=False`는 늘 켜짐, 들여쓰기는 부르는 쪽이 정한다."""
    write_text(path, json.dumps(data, ensure_ascii=False, indent=indent))


# ───────────────────── 있는지 · 치우기 · 옮기기 ─────────────────────

def exists(path) -> bool:
    return _fs_exists(_resolve(Path(path)))


def delete(path, *, missing_ok: bool = True) -> None:
    _fs_delete(_resolve(Path(path)), missing_ok=missing_ok)


def move(src, dst) -> None:
    """옮긴다(같은 저장소 안). 받는 자리의 폴더는 만들어 둔다."""
    target = _resolve(Path(dst))
    _fs_ensure_dir(target.parent)
    _fs_move(_resolve(Path(src)), target)


def ensure_dir(path) -> None:
    _fs_ensure_dir(_resolve(Path(path)))


def list_files(directory, pattern: str = "*.json") -> list:
    """그 칸에 든 것들 — 이름 사전순. 칸이 없으면 빈 목록."""
    return _fs_list(_resolve(Path(directory)), pattern)


def signature(path):
    """바뀌었는지 보는 값 `(mtime_ns, size)`. 없으면 `None`.

    메모리 캐시가 「이 파일 다시 읽어야 하나」를 이것으로 판정한다
    (`run_index`·`curation`·`adhoc/card`). DB로 가면 갱신 시각이 그 자리를 받는다.
    """
    return _fs_signature(_resolve(Path(path)))


# ─────────────────── 아래가 파일 백엔드다 ───────────────────
# Postgres로 갈 때 갈아끼우는 자리는 여기뿐이다. 위의 공개 함수는 그대로 둔다.

def _fs_read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _fs_write_text(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + _TMP_SUFFIX.format(ident=threading.get_ident()))
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _fs_exists(path: Path) -> bool:
    return path.exists()


def _fs_delete(path: Path, *, missing_ok: bool) -> None:
    path.unlink(missing_ok=missing_ok)


def _fs_move(src: Path, dst: Path) -> None:
    src.replace(dst)


def _fs_ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _fs_list(directory: Path, pattern: str) -> list:
    try:
        return sorted(directory.glob(pattern))
    except OSError:
        return []


def _fs_signature(path: Path):
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)
