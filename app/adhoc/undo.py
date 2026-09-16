# Design Ref: ADHOC_DESIGN.md §7 — 1차 범위에서 뺐던 ↩ 되돌리기. app/undo.py(정기)는
# 여러 개의 작은 전역 큐레이션 파일 + LLM 메모리 캐시를 통째로 스냅샷하는 구조라
# 카드 모델(자기완결 문서 하나)에 그대로 옮기면 안 맞았다 — 그래서 미뤄뒀던 것인데,
# 막상 옮겨보니 카드가 스스로 완결돼 있는 덕에 오히려 더 단순해진다:
#   - 정기: 되돌릴 상태가 여러 파일에 흩어져 있어 전부 모아 떠야 하고, 화면이 회차
#     파일이 아니라 LLM 메모리 캐시에서 분류를 다시 읽으므로 캐시까지 따로 맞춰야 한다.
#   - 수시: 카드 파일 하나가 사실이다(render_card_page가 article["group"]을 그대로
#     읽지, 매번 재분류하지 않는다) — classify_card가 이미 결과를 카드에 박아 저장해
#     두므로, **카드 JSON 전체를 통째로 스냅샷**하기만 하면 그 결과도 자연히 같이
#     되돌아간다. 별도 캐시 동기화가 필요 없다.
#
# 대신 스냅샷 하나가 카드 전체(기사 본문 포함)라 정기보다 무겁다 — 그래서 상한을
# 정기(20)보다 낮춘 10으로 잡았고, 같은 동작이 연달아 일어나면(coalesce_sec, 정기와
# 같은 값) 하나로 합쳐 불필요한 스냅샷을 줄인다.
#
# 범위: hide·unhide·move-article·move-order·bulk-move·classify·add-custom-group·
# 검색어 추가/삭제·꼭 포함할 검색어 추가/삭제(ADHOC_DESIGN.md §6.4a)·
# [추가: 2026-09-15] 로데이터 원본의 「숨김」 표시 붙이기/떼기(raw_marks — 원본 카드 안에만 남는다)
# 만 되돌린다. report_title(사안명)은 담당자가 직접 타이핑한 자유 서식 텍스트라
# "잘못 눌렀다"보다는 "다시 고쳐 쓴다"에 가까워서 뺐다 — 정기가 소제목 이름(자동 생성
# 대상)은 undo에 넣으면서 담당자가 직접 적는 "키워드 작성 메모"는 넣지 않는 것과
# 같은 구분이다.
import json
import threading
from datetime import datetime
from typing import Optional

from app.atomic_write import atomic_write_text

from app.adhoc.card import ADHOC_DIR, card_lock, load_card, save_card

UNDO_DIR = ADHOC_DIR / "undo"
UNDO_DIR.mkdir(parents=True, exist_ok=True)

_MAX_ENTRIES = 10

_locks: dict[str, threading.Lock] = {}
_locks_meta_lock = threading.Lock()


def _lock_for(card_id: str) -> threading.Lock:
    with _locks_meta_lock:
        if card_id not in _locks:
            _locks[card_id] = threading.Lock()
        return _locks[card_id]


def _path(card_id: str):
    return UNDO_DIR / f"{card_id}.json"


def _load(card_id: str) -> dict:
    try:
        return json.loads(_path(card_id).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"entries": []}


def _save(card_id: str, data: dict) -> None:
    atomic_write_text(_path(card_id), json.dumps(data, ensure_ascii=False))


def push(card_id: str, label: str, coalesce_sec: float = 3.0) -> None:
    """지금 카드 상태를 되돌리기 스택에 쌓는다 — 되돌릴 동작을 실행하기 **직전에** 부른다.

    label은 화면 ↩ 버튼 툴팁에 "○○ 되돌리기"로 그대로 쓰인다. 카드가 없으면 조용히
    아무 일도 안 한다(뒤이어 호출되는 실제 동작이 어차피 카드 없음을 처리한다).

    coalesce_sec: 같은 label이 이 시간 안에 이미 쌓여 있으면 새로 쌓지 않는다(정기
    app.undo.push와 같은 이유 — 다만 수시는 지금 한 번에 여러 URL을 동시 호출하는
    동작이 없어 정기만큼 절실하진 않다. 그래도 더블클릭·연타에는 여전히 유효하다).
    """
    with _lock_for(card_id):
        card = load_card(card_id)
        if card is None:
            return
        data = _load(card_id)
        entries = data["entries"]
        if entries and entries[-1]["label"] == label:
            elapsed = (datetime.now() - datetime.fromisoformat(entries[-1]["at"])).total_seconds()
            if elapsed < coalesce_sec:
                return
        entries.append(
            {"label": label, "at": datetime.now().isoformat(timespec="seconds"), "snapshot": card}
        )
        data["entries"] = entries[-_MAX_ENTRIES:]
        _save(card_id, data)


def peek_label(card_id: str) -> Optional[str]:
    """다음에 되돌릴 동작의 이름 — 되돌릴 게 없으면 None(↩ 버튼을 숨기는 근거)."""
    entries = _load(card_id)["entries"]
    return entries[-1]["label"] if entries else None


def undo(card_id: str) -> Optional[str]:
    """가장 최근 스냅샷으로 카드를 되돌린다. 되돌릴 게 없으면 None.

    반환값은 되돌린 동작의 label — 호출부가 화면에 알려주는 데 쓸 수 있다.
    카드 파일을 실제로 덮어쓰는 동안은 card_lock도 같이 쥔다 — 되돌리는 바로 그 순간
    다른 요청이 같은 카드를 고치고 있으면 안 되기 때문이다(undo 전용 락은 되돌리기
    스택 파일만 보호할 뿐, 카드 파일 자체의 동시 쓰기는 못 막는다).
    """
    with _lock_for(card_id):
        data = _load(card_id)
        if not data["entries"]:
            return None
        entry = data["entries"].pop()
        with card_lock(card_id):
            save_card(entry["snapshot"])
        _save(card_id, data)
        return entry["label"]
