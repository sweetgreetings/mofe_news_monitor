# Design Ref: 사용자 요청(2026-08-18, q2-a) — 라벨 붙이기/떼기/이름변경/합치기/삭제도
# 좌하단 ↩ 되돌리기 대상이다. app/undo.py(정기 큐레이션)와 같은 "통째로 스냅샷" 방식을
# 쓰지만 별도 파일·별도 스택이다:
#   - 라벨은 정기 큐레이션 상태(app/undo.py의 _SNAPSHOT_FILES)와 무관한 별개 파일 하나
#     (data/labels.json)만 뜨면 되므로 함께 묶을 이유가 없다.
#   - 회차·카드 어느 쪽에도 안 매이므로(정기처럼 자정 초기화, 수시처럼 카드별 스택 둘 다
#     안 맞는다) 그냥 전역 스택 하나에 계속 쌓는다.
#   - **합치기는 특히 이 되돌리기가 없으면 복구가 물리적으로 불가능**하다 — 두 라벨을
#     합친 뒤엔 어느 기사가 원래 어느 라벨이었는지 아무 데도 안 남는다(rename도 마찬가지).
import json
import threading
from datetime import datetime
from typing import Optional

from app.atomic_write import atomic_write_text
from app.config import LABEL_UNDO_FILE, LABELS_FILE

_MAX_ENTRIES = 20

_lock = threading.Lock()


def _read_raw() -> Optional[str]:
    try:
        return LABELS_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def _write_raw(text: Optional[str]) -> None:
    if text is None:
        LABELS_FILE.unlink(missing_ok=True)
    else:
        atomic_write_text(LABELS_FILE, text)


def _load() -> list:
    try:
        data = json.loads(LABEL_UNDO_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def _save(entries: list) -> None:
    atomic_write_text(LABEL_UNDO_FILE, json.dumps(entries, ensure_ascii=False, indent=2))


def push(label: str, coalesce_sec: float = 3.0) -> None:
    """지금 라벨 저장소 상태를 되돌리기 스택에 쌓는다 — **동작 실행 직전**에 부른다.

    label은 화면 툴팁에 "○○ 되돌리기"로 그대로 쓰인다. coalesce_sec 안에 같은 label이
    이미 쌓여 있으면 새로 쌓지 않는다(app.undo.push와 같은 이유 — 일괄 동작을 한 덩어리로
    묶는다).
    """
    with _lock:
        entries = _load()
        if entries and entries[-1]["label"] == label:
            elapsed = (datetime.now() - datetime.fromisoformat(entries[-1]["at"])).total_seconds()
            if elapsed < coalesce_sec:
                return
        entries.append(
            {"label": label, "at": datetime.now().isoformat(timespec="seconds"), "file": _read_raw()}
        )
        entries = entries[-_MAX_ENTRIES:]
        _save(entries)


def peek_label() -> Optional[str]:
    entries = _load()
    return entries[-1]["label"] if entries else None


def undo() -> Optional[str]:
    with _lock:
        entries = _load()
        if not entries:
            return None
        entry = entries.pop()
        _write_raw(entry["file"])
        _save(entries)
        return entry["label"]
