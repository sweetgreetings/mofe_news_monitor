"""「✂ 오늘만 여기서 끊기」의 실행부 — 초안 화면(서버 /cut-round·/cut-round-cancel)이 부른다.

시간표에 끼우는 규칙과 회차 기록 복사는 app.today_cuts, 여기는 "언제 무엇을 어떤 순서로"만 맡는다.

- **지금·지난 시각으로 끊기** — 네이버를 다시 검색하지 않고 **초안이 들고 있는 기사**로 곧바로
  확정본을 저장한다(담당자가 방금 본 것 = 확정본). 끊은 시각 뒤에 게시된 기사는 빼고, 그 순간
  네이버가 아직 색인하지 않은 기사는 이어지는 회차가 60분 거슬러 올라가 주워 담는다
  (app.scraper.collect_run의 COLLECT_LOOKBACK_MIN — 이미 있는 장치). 저장 순서가 계약이다:
  회차 파일을 먼저 저장하고 **그다음에** 시간표에 끊기를 적는다. 거꾸로 하면 그 사이 스케줄러
  tick이 "안 돈 지난 회차"로 보고 같은 회차를 한 번 더 수집한다.
- **앞 시각으로 끊기(예약)** — 시간표에만 적어 둔다. 그 시각이 되면 스케줄러가 여느 회차처럼
  수집한다. 그 전까지는 초안이 곧 그 회차라 머리줄이 「16시 기준」으로 바뀌고, 취소할 수 있다.
"""
import logging
import re
import threading
from datetime import datetime
from typing import Optional

from app import today_cuts, undo
from app.draft_seen import move_round
from app.settings import active_schedule_times, active_search_groups, group_in_scrap, load_settings
from app.storage import run_exists

logger = logging.getLogger(__name__)

_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

# 끊기·취소가 동시에 두 번 돌지 않게(두 탭에서 연달아 누름 등).
_lock = threading.Lock()


class CutError(Exception):
    """담당자에게 그대로 보여줄 거절 이유(한 문장)."""


def _draft_slot(settings: dict, now: datetime) -> Optional[dict]:
    from app.scheduler import next_pending_slot

    return next_pending_slot(now, active_schedule_times(settings, now))


def cut_state(now: Optional[datetime] = None) -> dict:
    """초안 화면이 머리줄 ✂·칩을 그리는 데 쓰는 값.

    반환: {"slot": 지금 초안 회차 또는 None, "pending": 예약해 둔(아직 안 온) 끊기 시각 또는 None,
           "done": 오늘 이미 끊어 확정본이 된 시각 목록}
    """
    now = now or datetime.now()
    settings = load_settings()
    slot = _draft_slot(settings, now)
    now_hm = now.strftime("%H:%M")
    ends = [c["end"] for c in today_cuts.cuts_for(now.date().isoformat())]
    pending = slot["end"] if slot and slot["end"] in ends and slot["end"] > now_hm else None
    done = [e for e in ends if e <= now_hm or run_exists(e)]
    return {"slot": slot, "pending": pending, "done": done}


def _draft_pool(settings: dict, slot: dict) -> Optional[list]:
    """초안이 마지막으로 그릴 때 쓴 검색 결과(초안 캐시). 이 회차 것이 아니면 None — 그땐
    어쩔 수 없이 네이버를 다시 검색한다(초안을 한 번도 안 연 상태 등)."""
    from app.preview_cache import load_preview_cache
    from app.preview_renderer import _preview_keywords_signature

    groups = active_search_groups(
        settings, [g for g in settings.get("keyword_groups", []) if group_in_scrap(g)]
    )
    cache = load_preview_cache()
    if cache is None or cache.get("signature") != _preview_keywords_signature(groups, slot):
        return None
    return list(cache.get("articles", []))


def cut_round(at: Optional[str] = None, now: Optional[datetime] = None) -> dict:
    """지금 초안 회차를 at(HH:MM, 생략하면 지금)에서 끊는다.

    반환: {"mode": "done"|"pending", "end": 끊은 시각, "from": 원래 회차 끝}
    거절하면 CutError(이유 한 문장).
    """
    from app.confirm_send import confirm_and_promote
    from app.scraper import collect_run

    with _lock:
        now = now or datetime.now()
        now_hm = now.strftime("%H:%M")
        at = (at or now_hm).strip()
        if not _HHMM_RE.match(at):
            raise CutError("시각을 HH:MM 모양으로 골라 주세요.")
        settings = load_settings()
        slot = _draft_slot(settings, now)
        if slot is None:
            raise CutError("지금 진행 중인 회차가 없어요.")
        if not (slot["start"] < at < slot["end"]):
            raise CutError(f"{slot['start']} 뒤, {slot['end']} 전 시각만 고를 수 있어요.")
        if run_exists(at):
            raise CutError(f"오늘 {at} 회차가 이미 있어요.")
        date_str = now.date().isoformat()
        origin = today_cuts.base_window_end(active_schedule_times(settings, now, with_cuts=False), at) or slot["end"]

        if at > now_hm:
            # 예약 — 지금부터 초안이 곧 끊는 회차다. 초안 기록을 그 이름으로 넘겨 둔다.
            today_cuts.copy_round_records(date_str, slot["end"], at, move_seen=True)
            undo.clear()
            today_cuts.add_cut(date_str, at, origin, now)
            return {"mode": "pending", "end": at, "from": origin}

        pool = _draft_pool(settings, slot)
        if pool is not None:
            # 끊은 시각 뒤에 게시된 기사는 이어지는 회차 몫이다(분 단위로 비교 — "지금"이면 전부).
            pool = [a for a in pool if (a.get("pub_date") or "")[11:16] <= at]
        today_cuts.copy_round_records(date_str, slot["end"], at, move_seen=True)
        try:
            result = collect_run(at, window_start=slot["start"], supplied_articles=pool)
        except Exception:
            # 저장 전에 실패했으면 초안이 원래 회차 그대로 이어지게 붙잡기 목록을 되돌린다.
            move_round((date_str, at), (date_str, slot["end"]))
            raise
        # 회차 파일이 저장된 뒤, 화면을 다시 그리기(confirm_and_promote) 전에 적는다 — 먼저 그리면
        # 정기 보관함이 이 회차를 끊은 회차로 모르고 ✂ 표식 없이 굳는다.
        today_cuts.add_cut(date_str, at, origin, now)
        undo.clear()
        confirm_and_promote(result)
        logger.info(
            "오늘만 회차 끊기 — %s 확정본 저장 (%d건, %s)",
            at, len(result.get("articles", [])), "초안 기사 그대로" if pool is not None else "다시 검색",
        )
        return {"mode": "done", "end": at, "from": origin}


def cancel_cut(end: str, now: Optional[datetime] = None) -> None:
    """예약해 둔(아직 시각이 안 온) 끊기를 취소한다. 이미 확정본이 된 끊기는 취소할 수 없다."""
    with _lock:
        now = now or datetime.now()
        date_str = now.date().isoformat()
        if today_cuts.cut_origin(date_str, end) is None:
            raise CutError("오늘 끊어 둔 시각이 아니에요.")
        if end <= now.strftime("%H:%M") or run_exists(end):
            raise CutError("이미 확정본이 만들어져 취소할 수 없어요.")
        times = active_schedule_times(load_settings(), now)
        nxt = next((w["end"] for w in times if w.get("start") == end), None)
        if nxt:
            # 예약해 둔 동안 다듬은 것을 이어지는 회차(원래 회차)로 돌려준다.
            today_cuts.copy_round_records(date_str, end, nxt, move_seen=True)
        today_cuts.remove_cut(date_str, end)
        undo.clear()
