# Design Ref: 사용자 요청(2026-09-23) — 발송해도 받는 사람에게 갔는지 확인할 곳이 없었다.
# 시안 mockups/SEND_LOG_MOCKUP.html.
#
# 앱이 보낸 모든 메시지 — 정기·수시 보고서, [단독]·[속보]·몰림 알림, 호출 한도 경고 — 를
# **발송 한 번 = 기록 한 줄**로 남긴다. 사람마다 도착했는지·못 받았으면 왜인지까지.
#
# 「읽었는지」는 적지 않는다. 텔레그램 봇은 읽음 표시를 받을 수 없고, 메일 읽음 확인(추적
# 이미지)은 Gmail 미리 받기·기관 메일 이미지 차단 때문에 틀리게 나온다 — 틀린 값을 적느니
# 안 적는다. 그래서 결과 말도 채널마다 다르다: 텔레그램은 "도착"(텔레그램이 그 대화방에
# 넣었다), 이메일은 "메일 서버가 받음"(나중에 반송될 수 있다).
#
# 저장: data/send_log/{YYYY-MM-DD}.json = {"entries": [기록…]}(오래된 것부터), 90일 보관.
# 폴더 경로는 호출 시점에 이 모듈의 SEND_LOG_DIR을 읽는다(테스트가 patch.object로 바꾼다).
import json
import logging
import threading
from datetime import datetime, timedelta
from typing import Optional

from app.atomic_write import atomic_write_text
from app.config import SEND_LOG_DIR, SEND_LOG_RETENTION_DAYS

logger = logging.getLogger(__name__)

# 종류 — 화면의 칩·필터가 이 키를 쓴다. 보고서 둘과 알림 넷.
REPORT_KINDS = ("regular", "adhoc")
ALERT_KINDS = ("scoop", "flash", "burst", "quota")
_ALERT_KIND_BY_HEADLINE = {"단독": "scoop", "속보": "flash"}

_lock = threading.Lock()


def alert_kind(headline: str) -> str:
    """app.filters.headline_kind의 값("단독"/"속보") → 기록의 종류 키."""
    return _ALERT_KIND_BY_HEADLINE.get(headline, "flash")


def _day_file(date_str: str):
    return SEND_LOG_DIR / f"{date_str}.json"


def _read_day(date_str: str) -> list:
    path = _day_file(date_str)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.warning("발송 기록 파일을 읽지 못했습니다: %s", path)
        return []
    entries = data.get("entries") if isinstance(data, dict) else None
    return entries if isinstance(entries, list) else []


def _cleanup(now: datetime) -> None:
    """보관 기간이 지난 날짜 파일을 지운다 — 기록을 쓸 때마다 한 번(파일이 90개 남짓이라 가볍다)."""
    cutoff = (now - timedelta(days=SEND_LOG_RETENTION_DAYS)).strftime("%Y-%m-%d")
    if not SEND_LOG_DIR.exists():
        return
    for path in SEND_LOG_DIR.glob("*.json"):
        if path.stem < cutoff:
            try:
                path.unlink()
            except OSError:
                logger.warning("지난 발송 기록을 지우지 못했습니다: %s", path)


def deliveries(channel: str, result, names: dict, contents: Optional[dict] = None) -> list:
    """SendResult 하나를 사람별 줄로 푼다 — 받은 사람 먼저, 못 받은 사람 뒤.

    names: {chat_id 또는 이메일 주소: 이름}. contents: {chat_id: 받은 것}(정기·수시 보고서).
    target이 None인 실패(토큰 없음처럼 채널 자체 문제)는 이름 「설정」 한 줄로 남긴다.
    """
    if result is None:
        return []
    contents = contents or {}
    rows = []
    # 테스트가 send_text를 bool로 바꿔치기해도 기록 때문에 발송 경로가 깨지지 않게.
    for d in getattr(result, "delivered", []):
        target = d.get("target")
        rows.append({
            "channel": channel,
            "name": names.get(target) or str(target),
            "target": target,
            "ok": True,
            "silent": bool(d.get("silent")),
            "parts": d.get("parts") or 1,
            "content": contents.get(target, ""),
        })
    for f in getattr(result, "failures", []):
        target = f.get("target")
        rows.append({
            "channel": channel,
            "name": (names.get(target) or str(target)) if target else "설정",
            "target": target,
            "ok": False,
            "error": f.get("error") or "",
            "content": contents.get(target, ""),
        })
    return rows


def _status(rows: list) -> str:
    ok = sum(1 for r in rows if r["ok"])
    if not rows:
        return "skipped"
    if ok == len(rows):
        return "ok"
    return "partial" if ok else "failed"


def _signature(entry: dict) -> tuple:
    """되풀이 판정 — 같은 결과·같은 이유면 같은 기록으로 본다."""
    return (
        entry.get("status"),
        entry.get("note", ""),
        tuple(sorted((r.get("channel"), str(r.get("target")), r.get("ok"), r.get("error", "")) for r in entry.get("deliveries", []))),
    )


def record(
    kind: str,
    title: str,
    rows: list,
    *,
    auto: Optional[bool] = None,
    send_no: int = 0,
    run_key: str = "",
    card_id: str = "",
    articles: Optional[list] = None,
    note: str = "",
    skipped: bool = False,
    coalesce: str = "",
    now: Optional[datetime] = None,
) -> Optional[dict]:
    """발송 한 번을 적는다. 어떤 실패도 발송 자체를 막으면 안 되므로 예외는 삼킨다.

    - send_no: 이 보고서의 몇 번째 발송인가(1부터, 2부터 "(수정)"). 알림은 0.
    - skipped: 보내려다 안 보낸 것(자동발송 건너뜀·받는 사람 없음) — rows 없이 note만.
    - coalesce: 자동발송은 실패(일부 실패 포함)하면 스케줄러 tick(10초)마다 다시 시도한다. 같은
      키로 적힌 오늘의 마지막 기록이 같은 결과면 새 줄을 만들지 않는다 — 실패는 `repeat`(시도한
      횟수)·`last_at`만 고치고, 건너뜀은 아무것도 안 쓴다.
    """
    now = now or datetime.now()
    entry = {
        "at": now.isoformat(timespec="seconds"),
        "kind": kind,
        "title": title,
        "status": "skipped" if skipped else _status(rows),
        "deliveries": [] if skipped else rows,
    }
    if auto is not None:
        entry["auto"] = bool(auto)
    if send_no:
        entry["send_no"] = send_no
    if run_key:
        entry["run_key"] = run_key
    if card_id:
        entry["card_id"] = card_id
    if articles:
        entry["articles"] = [
            {"outlet": a.get("outlet", ""), "title": a.get("title", ""), "url": a.get("url", "")}
            for a in articles
        ]
    if note:
        entry["note"] = note
    if coalesce:
        entry["coalesce"] = coalesce
    date_str = now.strftime("%Y-%m-%d")
    try:
        with _lock:
            entries = _read_day(date_str)
            if coalesce and entry["status"] in ("failed", "partial", "skipped"):
                last = next((e for e in reversed(entries) if e.get("coalesce") == coalesce), None)
                if last is not None and _signature(last) == _signature(entry):
                    if entry["status"] == "skipped":
                        return last
                    last["repeat"] = last.get("repeat", 1) + 1
                    last["last_at"] = entry["at"]
                    entry = last
                else:
                    entries.append(entry)
            else:
                entries.append(entry)
            SEND_LOG_DIR.mkdir(parents=True, exist_ok=True)
            atomic_write_text(_day_file(date_str), json.dumps({"entries": entries}, ensure_ascii=False, indent=1))
            _cleanup(now)
    except OSError:
        logger.exception("발송 기록을 남기지 못했습니다(%s · %s)", kind, title)
        return None
    return entry


def load_days(now: Optional[datetime] = None) -> list:
    """[(날짜, [기록… 최신순]), …] 날짜도 최신순. 보관 기간 안의 기록이 있는 날만."""
    now = now or datetime.now()
    cutoff = (now - timedelta(days=SEND_LOG_RETENTION_DAYS)).strftime("%Y-%m-%d")
    if not SEND_LOG_DIR.exists():
        return []
    days = []
    for path in sorted(SEND_LOG_DIR.glob("*.json"), reverse=True):
        if path.stem < cutoff:
            continue
        entries = _read_day(path.stem)
        if entries:
            days.append((path.stem, list(reversed(entries))))
    return days


_CHANNEL_LABELS = {"telegram": "텔레그램", "email": "이메일"}


def tally(entry: dict) -> list:
    """[(채널 이름, 받은 수, 전체 수)] — 화면의 「텔레그램 3/4」. 채널 자체 실패(「설정」 줄)도 한 명으로 센다."""
    out = []
    for channel, label in _CHANNEL_LABELS.items():
        rows = [r for r in entry.get("deliveries", []) if r.get("channel") == channel]
        if rows:
            out.append((label, sum(1 for r in rows if r.get("ok")), len(rows)))
    return out


def tally_text(entry: dict) -> str:
    return " · ".join(f"{label} {ok}/{total}" for label, ok, total in tally(entry))


def latest_for_run(run_key: str) -> Optional[dict]:
    """이 정기 회차(「YYYY-MM-DD|HH:MM」)의 가장 최근 발송 기록(보내지 않음은 뺀다).
    회차는 그날 보내므로 그 날짜 파일 하나만 본다."""
    date_str = run_key.split("|", 1)[0]
    for entry in reversed(_read_day(date_str)):
        if entry.get("run_key") == run_key and entry.get("status") != "skipped":
            return entry
    return None
