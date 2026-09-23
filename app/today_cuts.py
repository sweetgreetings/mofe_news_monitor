"""「✂ 오늘만 여기서 끊기」 — 진행 중인 회차를 수집 시간 설정을 건드리지 않고 오늘만 둘로 나눈다.

담당자가 초안에서 「15:57까지 확정본으로」를 고르면 그 시각이 오늘 시간표의 새 회차 끝이 된다.
예) 14:00~17:00 회차를 15:57에 끊으면 오늘만 14:00~15:57 · 15:57~17:00 두 회차가 된다.

- **끼워 넣는 곳은 app.settings.active_schedule_times 한 곳뿐이다.** 스케줄러·초안·확정본 수집
  창·실시간·홈·정기 보관함이 전부 그 함수를 거치므로, 여기서 한 번 끼우면 모두 같은 회차를
  본다. 두 곳에서 따로 끼우면 스케줄러와 초안이 서로 다른 회차를 보게 된다.
- **오늘 날짜의 끊기만 시간표에 들어간다.** 자정이 지나면 저절로 원래 수집 시간으로 돌아간다
  (settings.json은 한 글자도 안 바뀐다). 지난 날짜 기록은 지우지 않고 정기 보관함의 ✂ 표시에 쓴다.
- 이 모듈은 app.settings가 import하므로 config·atomic_write 말고는 모듈 최상단에서 가져오지
  않는다(순환 import). 회차 기록 복사처럼 무거운 일은 함수 안에서 늦게 가져온다.

저장 형식 `data/today_cuts.json`:
    {"2026-09-21": [{"end": "15:57", "from": "17:00", "at": "2026-09-21T15:57:12"}], ...}
`from`은 설정상 원래 회차 끝(보관함 툴팁용), `at`은 끊은 순간.
"""
import copy
import json
import logging
import threading
from datetime import date, datetime
from typing import Optional

from app.atomic_write import atomic_write_text
from app.config import TODAY_CUTS_FILE

logger = logging.getLogger(__name__)

_lock = threading.Lock()

# 날짜 기록을 이만큼만 남긴다(보관함 ✂ 표시용 — 회차 파일 보관 기간 365일보다 조금 넉넉히).
_MAX_DATES = 400


def _load() -> dict:
    try:
        data = json.loads(TODAY_CUTS_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(data: dict) -> None:
    keep = sorted(data)[-_MAX_DATES:]
    atomic_write_text(TODAY_CUTS_FILE, json.dumps({k: data[k] for k in keep}, ensure_ascii=False, indent=2))


def cuts_for(date_str: str) -> list:
    """그 날짜에 끊은 기록(끝 시각 순). 형식이 깨진 항목은 버린다."""
    items = _load().get(date_str, [])
    if not isinstance(items, list):
        return []
    good = [c for c in items if isinstance(c, dict) and isinstance(c.get("end"), str) and len(c["end"]) == 5]
    return sorted(good, key=lambda c: c["end"])


def cut_origin(date_str: str, end: str) -> Optional[str]:
    """그 날짜의 그 회차가 끊어서 생긴 회차면 원래 회차 끝(예: "17:00"), 아니면 None."""
    for c in cuts_for(date_str):
        if c["end"] == end:
            return c.get("from") or ""
    return None


def apply_cuts(times: list, day: Optional[date] = None) -> list:
    """시간표(times)에 그 날짜의 끊기를 끼워 넣은 **새** 목록을 돌려준다.

    끊은 시각이 어느 시간대 안(시작 < 끊기 < 끝)에도 안 들어가면(그날 수집 시간 설정을 고쳐 그
    칸을 지운 경우 등) 그 끊기는 무시한다. 나뉜 두 칸은 원래 칸의 enabled 등 다른 값을 물려받는다.
    """
    day = day or date.today()
    ends = [c["end"] for c in cuts_for(day.isoformat())]
    if not ends:
        return times
    result = []
    for window in times:
        start, end = window.get("start", ""), window.get("end", "")
        inside = sorted({e for e in ends if start < e < end})
        if not inside:
            result.append(window)
            continue
        edges = [start] + inside + [end]
        for a, b in zip(edges, edges[1:]):
            result.append({**window, "start": a, "end": b})
    return result


def base_window_end(times: list, hhmm: str) -> Optional[str]:
    """끊기 전 설정 시간표에서 hhmm이 들어가는 칸의 끝 — 보관함 툴팁의 「원래 N시 회차」."""
    for window in times:
        if window.get("start", "") < hhmm < window.get("end", ""):
            return window["end"]
    return None


def add_cut(date_str: str, end: str, origin: str, now: Optional[datetime] = None) -> None:
    now = now or datetime.now()
    with _lock:
        data = _load()
        items = [c for c in data.get(date_str, []) if isinstance(c, dict) and c.get("end") != end]
        items.append({"end": end, "from": origin, "at": now.isoformat(timespec="seconds")})
        data[date_str] = sorted(items, key=lambda c: c.get("end", ""))
        _save(data)
    logger.info("오늘만 회차 끊기 — %s %s (원래 %s 회차)", date_str, end, origin)


def remove_cut(date_str: str, end: str) -> bool:
    with _lock:
        data = _load()
        items = [c for c in data.get(date_str, []) if isinstance(c, dict)]
        kept = [c for c in items if c.get("end") != end]
        if len(kept) == len(items):
            return False
        if kept:
            data[date_str] = kept
        else:
            data.pop(date_str, None)
        _save(data)
    logger.info("오늘만 회차 끊기 취소 — %s %s", date_str, end)
    return True


# ── 회차 이름으로 저장된 기록 복사 ────────────────────────────────────────────────
#
# 초안에서 다듬은 것들은 회차 이름((날짜, 회차 끝))으로 저장된다. 끊는 순간 회차 이름이 바뀌므로
# 원래 회차 몫을 새 회차 이름으로 **복사**한다(옮기지 않는다 — 나뉜 두 회차가 모두 쓴다).
# 회차 이름으로 저장하는 새 저장소가 생기면 여기에 넣는다.
#   ① AI 분류 캐시 — _find_reusable/_find_partial이 회차가 같아야만 재사용한다. 빠지면 확정본이
#      AI를 새로 불러 소제목 이름이 바뀐다.
#   ② 소제목 이름표 group_labels  ③ 소제목 순서 group_order  ④ 키워드 메모
#   ⑤ 자동 분류 1회 기록 auto_classify_turns  ⑥ 초안에 보인 기사 draft_seen(한 회차 분만 담는
#      저장소라 이것만은 **옮긴다**)  ⑦ AI 재분류 실패 횟수(메모리)


def _copy_bucket(path, src_key: str, dst_key: str) -> None:
    """{회차 키: 값} 모양 파일에서 src 칸을 dst 칸으로 복사(덮어씀). src가 없으면 그대로 둔다."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, TypeError):
        return
    if not isinstance(data, dict) or src_key not in data:
        return
    data[dst_key] = copy.deepcopy(data[src_key])
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))


def copy_round_records(date_str: str, src_end: str, dst_end: str, move_seen: bool = True) -> None:
    """(date_str, src_end) 회차 이름으로 저장된 기록을 (date_str, dst_end)로 복사한다.

    한 저장소가 실패해도 나머지는 계속 간다 — 이 복사는 "이어 쓰기"를 위한 것이라, 하나가
    빠지면 그 부분만 새로 시작할 뿐 회차 자체가 망가지지는 않는다.
    """
    from app import auto_classify_turn, curation, draft_seen, group_order, llm_classifier
    from app import manual_keyword_note, reclassify_attempts

    src, dst = (date_str, src_end), (date_str, dst_end)
    src_key, dst_key = llm_classifier.round_key(src), llm_classifier.round_key(dst)
    steps = [
        ("AI 분류 캐시", lambda: llm_classifier.copy_round_cache(src, dst)),
        ("소제목 이름표", lambda: _copy_bucket(curation.GROUP_LABELS_FILE, src_key, dst_key)),
        ("소제목 순서", lambda: _copy_bucket(group_order.GROUP_ORDER_FILE, src_key, dst_key)),
        ("한 줄 메모", lambda: manual_keyword_note.copy_note(src, dst)),
        ("자동 분류 기록", lambda: auto_classify_turn.copy_turn(src_end, dst_end)),
        ("재분류 실패 횟수", lambda: reclassify_attempts.copy_attempts(src, dst)),
    ]
    if move_seen:
        steps.append(("초안에 보인 기사", lambda: draft_seen.move_round(src, dst)))
    for name, step in steps:
        try:
            step()
        except Exception:
            logger.exception("회차 끊기 — %s 복사 실패 (%s → %s), 이 부분만 새로 시작합니다", name, src_end, dst_end)


def after_round_collected(run: Optional[dict]) -> None:
    """예약해 둔 끊기 회차가 제시각에 수집된 직후 부른다(main._scrape_and_render).

    예약해 둔 동안 초안은 끊는 회차(예: 16:00) 이름으로 다듬어졌다. 그 뒤를 잇는 회차(17:00)
    초안에도 이름표·순서·메모가 이어지도록 끊는 회차 몫을 다음 회차로 다시 복사한다.
    초안에 보인 기사 목록은 옮기지 않는다(다음 회차는 새 기사로 시작한다).
    """
    if not run:
        return
    date_str = (run.get("run_at") or "")[:10]
    end = run.get("run_slot") or ""
    if cut_origin(date_str, end) is None:
        return
    from app.settings import active_schedule_times, load_settings

    day = date.fromisoformat(date_str)
    times = active_schedule_times(load_settings(), datetime.combine(day, datetime.min.time()))
    nxt = next((w["end"] for w in times if w.get("start") == end), None)
    if nxt:
        copy_round_records(date_str, end, nxt, move_seen=False)
