# Design Ref: 사용자 요청(2026-08-07) — 텔레그램 전송 받는 사람 여러 명 관리(켜고 끄기 토글).
# [수정: 2026-09-17] 받는 사람마다 "무엇을 받나"(정기·수시 × 기사·요약, [단독]·[속보])와
# "언제 알림이 울리나"(notify)를 정한다 — 시안 mockups/TELEGRAM_RECIPIENTS_MOCKUP.html.
import json
from datetime import datetime
from typing import Optional

from app.atomic_write import atomic_write_text
from app.config import TELEGRAM_CHAT_ID, TELEGRAM_RECIPIENTS_FILE

# 받는 것 여섯 칸. 저장 키 = 화면 폼 필드 꼬리 이름.
RECEIVE_FIELDS = (
    "alert_scoop", "alert_flash",
    "regular_articles", "regular_summary",
    "adhoc_articles", "adhoc_summary",
)

# 이 기능과 함께 생긴 키 — 옛 기록인지 가르는 기준(_normalize_recipient).
_NEW_FORMAT_KEYS = ("regular_articles", "regular_summary", "adhoc_articles", "adhoc_summary", "notify")

NOTIFY_MODES = ("always", "work", "custom")
NOTIFY_DAYS = ("daily", "weekday")
# 「업무 시간」 = 월~금 09:00~18:00. 화면 문구(app.settings_server._TELEGRAM_TIPS)와 같은 값.
WORK_NOTIFY = {"mode": "custom", "days": "weekday", "start": "09:00", "end": "18:00"}
# 이미 등록된 사람(notify가 없는 옛 기록)은 하루 종일 — 지금까지 24시간 울리고 있었으므로
# 본인이 바꾼 적 없는 알림이 어느 날 조용해지면 안 된다. 새로 추가하는 칸만 업무 시간.
LEGACY_NOTIFY = {"mode": "always"}
NEW_RECIPIENT_NOTIFY = {"mode": "work"}


def _valid_hhmm(value) -> bool:
    if not isinstance(value, str) or len(value) != 5 or value[2] != ":":
        return False
    hh, mm = value[:2], value[3:]
    return hh.isdigit() and mm in ("00", "30") and 0 <= int(hh) <= 23


def normalize_notify(notify) -> dict:
    """알림 설정을 저장 가능한 모양으로 다듬는다. 깨진 값은 하루 종일로 물러선다 —
    틀리는 방향이 "울려야 할 때 조용함"이면 안 된다(알림을 놓친 줄도 모른다)."""
    if not isinstance(notify, dict) or notify.get("mode") not in NOTIFY_MODES:
        return dict(LEGACY_NOTIFY)
    mode = notify["mode"]
    if mode != "custom":
        return {"mode": mode}
    days = notify.get("days") if notify.get("days") in NOTIFY_DAYS else "weekday"
    start = notify.get("start") if _valid_hhmm(notify.get("start")) else "09:00"
    end = notify.get("end") if _valid_hhmm(notify.get("end")) else "18:00"
    if start == end:
        # 시작=끝이면 울리는 시간이 0분이다 — 뜻 없는 상태라 하루 종일로 읽는다.
        return dict(LEGACY_NOTIFY)
    return {"mode": "custom", "days": days, "start": start, "end": end}


def _normalize_recipient(r: dict) -> dict:
    """옛 형식도 읽어준다 — enabled(정기 발송)는 regular_articles로 옮긴다."""
    out = {
        "name": (r.get("name") or r.get("chat_id", "") or "").strip(),
        "chat_id": str(r.get("chat_id", "") or "").strip(),
    }
    # 새 형식 키가 하나라도 있으면 없는 칸은 꺼짐이다. 옛 형식(새 키가 하나도 없음)만
    # enabled를 정기 기사로 읽는다 — 안 그러면 「정기 요약」만 켠 사람이 기사까지 받는다.
    is_new_format = any(k in r for k in _NEW_FORMAT_KEYS)
    for field in RECEIVE_FIELDS:
        if field in r:
            out[field] = bool(r.get(field))
        elif field == "regular_articles" and not is_new_format:
            out[field] = bool(r.get("enabled", True))
        else:
            out[field] = False
    out["notify"] = normalize_notify(r.get("notify")) if "notify" in r else dict(LEGACY_NOTIFY)
    return out


def load_telegram_recipients() -> list:
    """등록된 받는 사람 목록(등록 순서 그대로). 옛 형식도 새 필드로 읽어준다.

    파일이 아직 없고 .env에 TELEGRAM_CHAT_ID가 남아 있으면 예전 "챗 아이디 1개" 방식으로
    쓰던 사람을 잃지 않도록 "나"로 한 번만 등록해준다.
    """
    if not TELEGRAM_RECIPIENTS_FILE.exists():
        if TELEGRAM_CHAT_ID:
            seeded = [_normalize_recipient({"name": "나", "chat_id": TELEGRAM_CHAT_ID, "enabled": True})]
            _write(seeded)
            return seeded
        return []
    try:
        data = json.loads(TELEGRAM_RECIPIENTS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    return [_normalize_recipient(r) for r in data if isinstance(r, dict)]


def _write(recipients: list) -> None:
    # enabled는 regular_articles를 그대로 적어 두는 파생값이다 — 옛 코드로 되돌려도 정기
    # 기사 목록을 받던 사람에게 그대로 가게 남겨 둔다.
    rows = [{**r, "enabled": bool(r.get("regular_articles"))} for r in recipients]
    atomic_write_text(TELEGRAM_RECIPIENTS_FILE, json.dumps(rows, ensure_ascii=False, indent=2))


def save_telegram_recipients(recipients: list) -> None:
    """받는 사람 목록 전체를 통째로 저장한다. chat id가 빈 칸은 버린다."""
    cleaned = [_normalize_recipient(r) for r in recipients if str(r.get("chat_id", "") or "").strip()]
    _write(cleaned)


# --- 받는 것 ---------------------------------------------------------------------------

def active_recipient_chat_ids() -> list:
    """정기 기사 목록을 받는 사람의 chat id(옛 경로 /telegram-send-* 호환용)."""
    return [r["chat_id"] for r in load_telegram_recipients() if r["regular_articles"]]


def active_alert_chat_ids(kind: str) -> list:
    """[단독]/[속보] 즉시 알림을 받기로 한 사람의 chat id. kind는 "단독"|"속보"."""
    field = "alert_scoop" if kind == "단독" else "alert_flash"
    return [r["chat_id"] for r in load_telegram_recipients() if r.get(field, False)]


def report_recipients(flow: str) -> list:
    """정기("regular")·수시("adhoc") 보고서를 받는 사람 — 기사·요약 중 하나라도 켠 사람."""
    return [
        r for r in load_telegram_recipients()
        if r[f"{flow}_articles"] or r[f"{flow}_summary"]
    ]


# --- 알림 시간 ---------------------------------------------------------------------------

def notify_window(notify: dict) -> Optional[dict]:
    """알림이 울리는 구간 {days, start, end}. 하루 종일이면 None."""
    notify = normalize_notify(notify)
    if notify["mode"] == "always":
        return None
    if notify["mode"] == "work":
        return {k: WORK_NOTIFY[k] for k in ("days", "start", "end")}
    return {k: notify[k] for k in ("days", "start", "end")}


def rings_now(notify: dict, now: Optional[datetime] = None) -> bool:
    """지금 이 사람에게 소리·진동 알림이 울리나. 아니면 메시지는 조용히 간다.

    시작이 끝보다 늦으면(22:00~07:00) 자정을 넘는 구간이고, 요일(월~금)은 **그 구간이
    시작된 날** 기준이다 — 금요일 22:00에 시작한 구간은 토요일 새벽까지 이어진다.
    """
    window = notify_window(notify)
    if window is None:
        return True
    now = now or datetime.now()
    hhmm = now.strftime("%H:%M")
    start, end = window["start"], window["end"]
    weekday = now.weekday()
    if start < end:
        in_time, start_day = start <= hhmm < end, weekday
    elif hhmm >= start:
        in_time, start_day = True, weekday
    elif hhmm < end:
        in_time, start_day = True, (weekday - 1) % 7
    else:
        in_time, start_day = False, weekday
    if not in_time:
        return False
    return window["days"] == "daily" or start_day < 5


def silent_chat_ids(now: Optional[datetime] = None) -> set:
    """지금 알림 없이 보내야 하는 chat id. 등록되지 않은 chat id는 여기 없다(울린다)."""
    now = now or datetime.now()
    return {r["chat_id"] for r in load_telegram_recipients() if not rings_now(r["notify"], now)}


def _hh(hhmm: str) -> str:
    """"07:00" → "7", "08:30" → "8:30"."""
    hh, mm = hhmm.split(":")
    return str(int(hh)) + ("" if mm == "00" else f":{mm}")


def notify_label(notify: dict) -> str:
    """칸에 보이는 짧은 글자 — 하루 종일 / 업무 시간 / 평일 8:30–19 / 매일 7–22."""
    notify = normalize_notify(notify)
    if notify["mode"] == "always":
        return "하루 종일"
    if notify["mode"] == "work":
        return "업무 시간"
    days = "매일" if notify["days"] == "daily" else "평일"
    return f"{days} {_hh(notify['start'])}–{_hh(notify['end'])}"


def notify_sentence(notify: dict) -> str:
    """말풍선 본문 — 하루 종일 울립니다. / 월~금 09:00 ~ 18:00에 울립니다."""
    window = notify_window(notify)
    if window is None:
        return "하루 종일 울립니다."
    days = "매일" if window["days"] == "daily" else "월~금"
    overnight = " (다음 날)" if window["start"] > window["end"] else ""
    return f'{days} {window["start"]} ~ {window["end"]}{overnight}에 울립니다.'
