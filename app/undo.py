# Design Ref: 사용자 요청(2026-08-11) — "AI 재분류가 마음에 안 들 때", "숨기려던 게
# 아니었을 때", "<9>에 있던 기사를 <1>에 넣으려다 <2>에 잘못 넣었을 때" 되돌릴 수단.
#
# 세 가지 동작의 "역연산"을 각각 만드는 대신, **되돌릴 수 있는 상태를 통째로 스냅샷**
# 하는 방식을 골랐다. 이유:
#   (1) 역연산은 동작마다 따로 짜야 하고 하나라도 빠뜨리면 조용히 어긋난다. 반면 스냅샷은
#       "직전 상태로 되돌린다"는 정의 하나로 숨기기·이동·이름변경·순서변경·재분류를 전부
#       한 번에 커버한다(나중에 큐레이션 동작이 늘어도 자동으로 따라온다).
#   (2) 이 앱의 큐레이션 상태는 전부 작은 JSON 파일 몇 개뿐이라 통째로 뜨는 비용이 거의 없다.
#
# 특히 놓치기 쉬운 것: **LLM 캐시도 같이 떠야 한다.** 확정본 화면은 매 렌더링마다
# classify_articles를 다시 부르고 그 답을 app.llm_classifier의 메모리 캐시에서 가져온다 —
# 회차 파일의 "group" 필드만 되돌려놓으면 화면은 여전히 캐시에 남은 새 분류를 보여줘서
# "되돌렸는데 안 되돌아간" 것처럼 보인다. 그래서 캐시와 _last_names까지 스냅샷에 포함한다.
#
# [수정: 2026-08-24] 위 목록의 "순서변경"이 실제로는 안 됐다 — 확정본의 ↑/↓(단건·일괄)는
# 소제목을 넘지 않는 이동이라 group_overrides.json을 안 건드리고, 결과가 오직 회차 파일
# (data/articles/*.json) 안 articles 리스트의 순서로만 존재한다. 그런데 그 회차 파일은
# 처음부터 _SNAPSHOT_FILES에 없었다 — 그래서 ↑/↓ 뒤 되돌리기를 눌러도(눌렀다는 전제
# 하에) 실제로는 아무 것도 안 바뀌는 상태였다. `_snapshot_latest_run_file`/
# `_restore_latest_run_file`로 회차 파일 원본도 같이 뜨고 되돌리게 했다 —
# HISTORY.md 같은 섹션 참고.
import json
import threading
from datetime import date, datetime
from typing import Optional

from app.atomic_write import atomic_write_text
from app.config import (
    ASSIGNED_GROUPS_FILE,
    CUSTOM_GROUPS_FILE,
    DRAFT_PENDING_ARTICLES_FILE,
    GROUP_LABELS_FILE,
    GROUP_ORDER_FILE,
    GROUP_OVERRIDES_FILE,
    HIDDEN_ARTICLES_FILE,
    MANUAL_ARTICLES_FILE,
    PREVIEW_ORDER_FILE,
)

UNDO_FILE = HIDDEN_ARTICLES_FILE.with_name("undo_stack.json")

# 되돌리기 단계 상한. 사람이 실제로 "몇 단계 전"까지 기억하는 범위를 넘으면 오히려
# 위험하고(엉뚱한 시점으로 점프), 파일도 커진다.
_MAX_ENTRIES = 20

# 스냅샷에 포함할 큐레이션 상태 파일들. 여기 추가하기만 하면 그 상태도 자동으로
# 되돌리기 대상이 된다.
_SNAPSHOT_FILES = {
    "hidden": HIDDEN_ARTICLES_FILE,
    "overrides": GROUP_OVERRIDES_FILE,
    "labels": GROUP_LABELS_FILE,
    "order": GROUP_ORDER_FILE,
    "custom": CUSTOM_GROUPS_FILE,
    "preview_order": PREVIEW_ORDER_FILE,
    # [추가: 2026-08-26] 📌 담아둔 기사 / 초안 예약 목록. "담아둔 기사 → 초안" 승격은
    # 이 두 파일만 건드리므로, 여기 없으면 되돌리기를 눌러도 아무 일이 안 일어난다
    # (2026-08-24의 회차 파일 누락과 같은 종류의 빈틈 — 라벨만 맞고 실제로는 no-op).
    # "AI 기사 배정"(담아둔 기사 전체를 한 번에 초안으로) 버튼이 생기면서 한 번에 여러
    # 건이 넘어가게 돼, 되돌릴 수 없으면 실수의 대가가 커진다.
    "manual": MANUAL_ARTICLES_FILE,
    "draft_pending": DRAFT_PENDING_ARTICLES_FILE,
    # [추가: 2026-09-15] AI가 새로 지은 소제목 이름 목록(「AI 기사 배정」·「AI 기사 나누기」).
    # 이게 빠져 있으면 되돌린 뒤에도 그 이름이 "목적지"로 계속 등록돼 있어, 나중에 같은
    # 이름이 다시 나올 때 엉뚱한 기사가 빈 칸에 끌려올 수 있다.
    "assigned": ASSIGNED_GROUPS_FILE,
}

# 파일 읽기-수정-쓰기가 여러 스레드에서 겹치지 않게 한다
# (app.curation의 hidden_articles 락과 같은 이유 — ThreadingHTTPServer).
_lock = threading.Lock()


def _read_raw(path) -> Optional[str]:
    """파일 내용을 문자열 그대로 읽는다 — 형식을 해석하지 않으므로 어떤 구조든 그대로 복원된다.

    파일이 아직 없으면 None(= "이 시점엔 파일이 없었다")을 돌려주고, 복원할 때 그 상태
    (파일 없음)까지 그대로 재현한다.
    """
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def _write_raw(path, text: Optional[str]) -> None:
    if text is None:
        path.unlink(missing_ok=True)
    else:
        atomic_write_text(path, text)


def _snapshot_llm_cache() -> dict:
    """app.llm_classifier의 메모리 캐시를 JSON으로 저장 가능한 형태로 뜬다.

    캐시 키가 (round_id, frozenset(url), 최대소제목수) 튜플이라 그대로는 JSON이 안
    되므로 리스트로 펴서 담는다. 지연 임포트하는 이유는 순환 임포트 방지
    (llm_classifier -> (없음)이지만, 이 모듈이 settings_server에서 불리는 순서상
    최상단 임포트로 두면 초기화 순서가 얽힐 수 있어 보수적으로 함수 안에서 불러온다).

    [수정: 2026-08-14] 캐시 키가 (frozenset, 최대소제목수) 2요소에서 맨 앞에 round_id가
    추가된 3요소로 바뀌었다(회차 경계를 넘는 재사용 방지, app.llm_classifier 참고) —
    이 함수가 옛 2요소 언패킹을 그대로 쓰고 있어서 실제 캐시에 항목이 하나라도 있으면
    undo_push 호출마다(숨기기·이동·이름변경 전부) ValueError로 크래시하는 회귀가 있었다.
    큐레이션 액션이 전부 undo_push를 먼저 부르므로 방치하면 앱의 모든 큐레이션 버튼이
    막히는 심각한 문제였다 — round_id 도입 직후 실제로 재현해 발견하고 바로 고쳤다.
    """
    from app import llm_classifier as L

    # [추가: 2026-08-12] 이번 프로세스에서 아직 한 번도 분류를 안 했으면 메모리 캐시가
    # 비어 있다 — 그 상태로 스냅샷을 뜨면 디스크에 남아있던 캐시를 빈 값으로 덮어써
    # 잃어버린다. 먼저 디스크에서 불러온 뒤 뜬다.
    L._ensure_cache_hydrated()
    with L._cache_lock:
        return {
            "entries": [
                {
                    "round_id": list(round_id) if round_id else None,
                    "urls": sorted(urls),
                    "max": max_sub,
                    "groups": [[name, list(u), summary] for name, u, summary in groups],
                }
                for (round_id, urls, max_sub), groups in L._cache.items()
            ],
            "last_names": list(L._last_names),
            "last_round_id": list(L._last_round_id) if L._last_round_id else None,
        }


def _restore_llm_cache(data: dict) -> None:
    from app import llm_classifier as L

    with L._cache_lock:
        L._cache.clear()
        for entry in data.get("entries", []):
            round_id = tuple(entry["round_id"]) if entry.get("round_id") else None
            key = (round_id, frozenset(entry["urls"]), entry["max"])
            L._cache[key] = [
                (item[0], list(item[1]), item[2] if len(item) > 2 else "")
                for item in entry["groups"]
            ]
        L._last_names = list(data.get("last_names", []))
        L._last_round_id = tuple(data["last_round_id"]) if data.get("last_round_id") else None
        # [추가: 2026-08-12] 메모리만 되돌리고 디스크(app.llm_classifier
        # ._persist_cache_locked가 쓰는 LLM_CLASSIFICATION_CACHE_FILE)를 안 맞추면,
        # 되돌린 직후 앱이 재시작될 때 디스크에 남아있던 "되돌리기 전" 캐시가 다시
        # 불러와져 방금 한 되돌리기가 조용히 무효화된다.
        L._persist_cache_locked()


def _snapshot_latest_run_file() -> Optional[dict]:
    """지금 최신 회차 파일의 원본 내용을 통째로 뜬다.

    [추가: 2026-08-24] `_SNAPSHOT_FILES` 6개 어디에도 안 남고 **회차 파일 안의 기사
    순서**만 바꾸는 액션(확정본의 ↑/↓ 단건·일괄 이동, `app.curation.move_article`/
    `bulk_move_articles`)이 있다 — 이 둘은 소제목을 넘지 않는 순서 변경이라
    `group_overrides.json`을 안 건드리고(`new_override`는 항상 None), 결과는 오직
    회차 파일(`data/articles/*.json`)의 `articles` 리스트 순서로만 존재한다. 이 파일이
    스냅샷에 없으면 `undo_push`를 아무리 불러도 되돌리기를 눌렀을 때 실제로는 아무것도
    안 바뀐다 — 회차 파일은 건드리지 않은 채 나머지 6개 파일만(내용이 애초에 그대로라서)
    복원하는 셈이기 때문이다. `list_run_meta()`가 없으면(회차가 하나도 없음) None.
    """
    from app.storage import list_run_meta

    for meta in list_run_meta():  # 이미 run_at 내림차순 — 첫 항목이 최신
        path = meta["path"]
        return {"path": str(path), "text": _read_raw(path)}
    return None


def _restore_latest_run_file(data: Optional[dict]) -> None:
    """`_snapshot_latest_run_file`이 뜬 내용을 그 파일 경로에 그대로 되돌려 쓴다.

    스냅샷 시점 이후 그 회차 파일 자체가 삭제됐거나(보관 기간 만료 등) `data`가 아예
    없으면(옛 되돌리기 기록 — 이 필드가 생기기 전에 쌓인 스냅샷) 조용히 건너뛴다 —
    회차 파일 복원은 "가능하면 하는" 보강일 뿐, 실패했다고 나머지 6개 파일 복원까지
    막을 이유는 없다(undo() 쪽에서 순서상 이미 나머지가 끝난 뒤에 불린다).
    """
    if not data:
        return
    from pathlib import Path

    _write_raw(Path(data["path"]), data["text"])


def _load() -> dict:
    """오늘치 되돌리기 기록을 읽는다 — 날짜가 바뀌었으면 빈 상태로 시작한다.

    자정 초기화는 app.manual_articles·app.custom_groups와 같은 lazy reset 방식이다.
    어제 회차 상태로 되돌리는 건 의미가 없고 오히려 위험하기 때문.
    """
    try:
        data = json.loads(UNDO_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"date": date.today().isoformat(), "entries": []}
    if data.get("date") != date.today().isoformat():
        return {"date": date.today().isoformat(), "entries": []}
    return data


def _save(data: dict) -> None:
    atomic_write_text(UNDO_FILE, json.dumps(data, ensure_ascii=False, indent=2))


def push(
    label: str,
    coalesce_sec: float = 3.0,
    touched: Optional[list] = None,
    batch: Optional[str] = None,
) -> None:
    """지금 상태를 되돌리기 스택에 쌓는다 — **되돌릴 동작을 실행하기 직전에** 부른다.

    label은 화면 버튼 툴팁에 "○○ 되돌리기"로 그대로 쓰이므로, 사람이 읽고 무엇이
    되돌아갈지 알 수 있는 짧은 말로 넘긴다(예: "기사 숨기기", "AI 소제목 분류").

    coalesce_sec: 같은 label의 스냅샷이 이 시간 안에 이미 쌓여 있으면 새로 쌓지 않는다.
    일괄 숨기기·소제목 통째로 숨기기는 화면이 /hide-article을 기사 수만큼 **동시에**
    호출하는 구조라(Promise.all), 그대로 두면 되돌리기를 기사 수만큼 눌러야 한다 —
    이미 쌓인 첫 스냅샷이 그 묶음 전체의 "직전 상태"이므로 하나로 합치는 게 맞다.
    (에디터의 Ctrl+Z가 빠른 연속 입력을 한 덩어리로 묶는 것과 같은 이유.)

    [추가: 2026-09-15] touched — 이 동작에서 **담당자가 직접 손댄 기사 URL**. 되돌린 뒤
    그 기사만 세이지(.just-moved)로 칠하는 데 쓴다. 상태를 비교(diff)해서 구하지 않는
    이유: ↑↓는 이웃과 자리를 맞바꾸는 동작이라 결과만 보면 누가 옮긴 기사인지 가를 수
    없고, 세이지는 "담당자가 방금 손댄 것만"이라는 뜻이라 밀려난 이웃을 칠하면 안 된다
    (사용자 결정). 기사를 짚지 않는 동작(이름 변경·AI 재분류·소제목 만들기)은 안 넘긴다.
    batch — 화면이 **한 번의 동작**을 여러 요청으로 나눠 보낼 때(일괄·소제목 통째 숨기기)
    붙이는 식별자. 합쳐질 때 같은 batch면 URL을 덧붙이고(체크한 기사 전부가 손댄 것),
    다르면 **마지막 동작의 URL로 갈아끼운다** — 3초 안에 서로 다른 기사를 따로따로
    눌렀으면 되돌리기는 한 번에 되지만 칠하는 건 마지막으로 누른 기사뿐이다(미니 목차
    초록이 "마지막으로 옮긴 한 칸뿐"인 것과 같은 규칙).
    """
    touched = [url for url in (touched or []) if url]
    with _lock:
        data = _load()
        entries = data["entries"]
        if entries and entries[-1]["label"] == label:
            last = entries[-1]
            elapsed = (datetime.now() - datetime.fromisoformat(last["at"])).total_seconds()
            if elapsed < coalesce_sec:
                old = (list(last.get("touched") or []), last.get("batch"))
                if batch and last.get("batch") == batch:
                    merged = list(last.get("touched") or [])
                    merged.extend(url for url in touched if url not in merged)
                    last["touched"] = merged
                else:
                    last["touched"] = touched
                    last["batch"] = batch
                # 같은 기사 ↑ 연타처럼 기록이 안 바뀌면 파일을 다시 쓰지 않는다
                # (스택 파일이 수 MB라 쓰기 한 번에 수십 ms — 실측 7MB·17ms).
                if (last["touched"], last["batch"]) != old:
                    _save(data)
                return
        data["entries"].append(
            {
                "label": label,
                "at": datetime.now().isoformat(timespec="seconds"),
                "files": {key: _read_raw(path) for key, path in _SNAPSHOT_FILES.items()},
                "llm_cache": _snapshot_llm_cache(),
                "run_file": _snapshot_latest_run_file(),
                "touched": touched,
                "batch": batch,
            }
        )
        # 오래된 것부터 버려 상한을 지킨다.
        data["entries"] = data["entries"][-_MAX_ENTRIES:]
        _save(data)


def peek_label() -> Optional[str]:
    """다음에 되돌릴 동작의 이름 — 되돌릴 게 없으면 None(버튼을 숨기는 근거)."""
    entries = _load()["entries"]
    return entries[-1]["label"] if entries else None


def undo() -> Optional[dict]:
    """가장 최근 스냅샷으로 상태를 되돌린다. 되돌릴 게 없으면 None.

    반환값은 `{"label": 되돌린 동작 이름, "touched": 그 동작에서 담당자가 손댄 기사 URL}` —
    화면이 되돌린 뒤 그 기사만 세이지로 칠하는 데 쓴다(push의 touched 참고). 이 필드가
    생기기 전에 쌓인 기록은 touched가 빈 목록이라 되돌리기만 되고 칠해지지 않는다.
    """
    with _lock:
        data = _load()
        if not data["entries"]:
            return None
        entry = data["entries"].pop()
        for key, path in _SNAPSHOT_FILES.items():
            # [수정: 2026-09-15] 스냅샷에 그 키가 **아예 없으면**(그 파일이 목록에 들어오기 전에
            # 쌓인 기록) 건드리지 않는다. 예전처럼 get(key)의 None을 "그땐 파일이 없었다"로
            # 읽으면, 새로 추가한 파일(assigned)이 옛 기록을 되돌리는 순간 통째로 지워진다.
            if key in entry["files"]:
                _write_raw(path, entry["files"][key])
        _restore_llm_cache(entry.get("llm_cache", {}))
        _restore_latest_run_file(entry.get("run_file"))
        _save(data)
        return {"label": entry["label"], "touched": list(entry.get("touched") or [])}
