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
from pathlib import Path
from typing import Optional

from app.config import DATA_DIR

# 임시 파일 이름에 스레드 ID를 넣어 동시에 쓰는 다른 스레드와 겹치지 않게 한다
# (`app/atomic_write.py`가 화면 HTML에 쓰는 것과 같은 방식·같은 이유).
_TMP_SUFFIX = ".{ident}.tmp"


# ───────────────────────────── 키 ─────────────────────────────

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
    return _fs_read_text(Path(path))


def read_json(path):
    """JSON을 파이썬 값으로. 없으면 `OSError`, 깨졌으면 `ValueError`."""
    return json.loads(read_text(path))


# ──────────────────────────── 쓰기 ────────────────────────────

def write_text(path, text: str) -> None:
    """원자적으로 쓴다 — 임시 파일에 쓴 뒤 교체해, 겹쳐 써도 반쪽 파일이 안 남는다."""
    _fs_write_text(Path(path), text)


def write_json(path, data, *, indent: Optional[int] = None) -> None:
    """JSON으로 쓴다. `ensure_ascii=False`는 늘 켜짐, 들여쓰기는 부르는 쪽이 정한다."""
    write_text(path, json.dumps(data, ensure_ascii=False, indent=indent))


# ───────────────────── 있는지 · 치우기 · 옮기기 ─────────────────────

def exists(path) -> bool:
    return _fs_exists(Path(path))


def delete(path, *, missing_ok: bool = True) -> None:
    _fs_delete(Path(path), missing_ok=missing_ok)


def move(src, dst) -> None:
    """옮긴다(같은 저장소 안). 받는 자리의 폴더는 만들어 둔다."""
    dst = Path(dst)
    ensure_dir(dst.parent)
    _fs_move(Path(src), dst)


def ensure_dir(path) -> None:
    _fs_ensure_dir(Path(path))


def list_files(directory, pattern: str = "*.json") -> list:
    """그 칸에 든 것들 — 이름 사전순. 칸이 없으면 빈 목록."""
    return _fs_list(Path(directory), pattern)


def signature(path):
    """바뀌었는지 보는 값 `(mtime_ns, size)`. 없으면 `None`.

    메모리 캐시가 「이 파일 다시 읽어야 하나」를 이것으로 판정한다
    (`run_index`·`curation`·`adhoc/card`). DB로 가면 갱신 시각이 그 자리를 받는다.
    """
    return _fs_signature(Path(path))


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
