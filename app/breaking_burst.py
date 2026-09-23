# [속보] 몰림 알림 — [속보]가 짧은 시간에 여러 언론사에서 한꺼번에 나오면 개별 알림과
# 별개로 요약 한 통을 더 보낸다(시안 mockups/BREAKING_BURST_MOCKUP.html A안).
#
# - 세는 재료는 app.alerted_urls의 오늘 발송 기록 하나다 — 네이버를 따로 부르지 않고,
#   홈 [속보] 칩과 같은 기록을 봐서 "알림은 왔는데 몰림 수는 다르다"가 생기지 않는다.
# - 기사 수가 아니라 **언론사 수**로 센다(한 언론사가 [속보]를 여러 건 내도 한 곳).
# - 창은 게시 시각 기준 "지금 − N분 ~ 지금". 앱을 켤 때 몰아서 받은 지난 기사는 창 밖이라
#   저절로 안 센다(몰린 시간이 이미 지났다).
# - [단독]은 대상이 아니다 — 한 언론사만 내는 기사라 몰리지 않는다(실측 30분 최대 3건).
# - 받는 사람은 [속보] 받는 사람, 조용한 시간 규칙도 개별 알림과 같다(send_text가 판단).
import logging
from datetime import datetime, timedelta
from typing import Optional

from app import send_log
from app.alert_event import pub_dt
from app.alerted_urls import alerted_items
from app.breaking_alert import load_breaking_alert_settings
from app.breaking_alert_state import load_state, save_state
from app.config import BURST_COOLDOWN_MIN, BURST_HISTORY_DAYS, BURST_MAX_OUTLET_NAMES, BURST_WINDOW_CHOICES
from app.filters import HEADLINE_TAG_RE, headline_kind
from app.telegram_bot import send_text as send_telegram_text
from app.telegram_recipients import active_alert_chat_ids, load_telegram_recipients

logger = logging.getLogger(__name__)

_KIND = "속보"


def _dated(items: list) -> list:
    """[(게시시각, 항목)]을 게시시각 순으로. 게시시각이 없는 항목은 뺀다."""
    out = [(dt, it) for it in items if (dt := pub_dt(it.get("pub_date"))) is not None]
    return sorted(out, key=lambda pair: pair[0])


def _distinct_outlets(pairs: list) -> list:
    """게시 순서대로 언론사 이름을 중복 없이."""
    names = []
    for _dt, it in pairs:
        name = (it.get("outlet") or "").strip()
        if name and name not in names:
            names.append(name)
    return names


def _strip_tag(title: str) -> str:
    return HEADLINE_TAG_RE.sub("", title or "").strip()


def build_burst_message(pairs: list) -> str:
    """몰림 알림 본문. pairs는 창 안 [속보]의 [(게시시각, 항목)], 게시 순."""
    outlets = _distinct_outlets(pairs)
    start, end = pairs[0][0], pairs[-1][0]
    shown = outlets[:BURST_MAX_OUTLET_NAMES]
    names = " · ".join(shown)
    if len(outlets) > len(shown):
        names += f" 외 {len(outlets) - len(shown)}곳"
    return (
        "🔥 [속보]가 몰리고 있어요\n"
        f"{start:%H:%M}~{end:%H:%M} 사이 {len(outlets)}개 언론사\n\n"
        f"{_strip_tag(pairs[0][1].get('title', ''))}\n"
        f"{names}"
    )


def check_and_alert_burst(now: Optional[datetime] = None) -> bool:
    """오늘 보낸 [속보] 기록으로 몰림을 판정해 맞으면 한 통 보낸다. 보냈으면 True.

    app.breaking_alert_sender.detect_and_alert가 [속보]를 새로 보낸 직후에만 부른다.
    한 번 울리면 BURST_COOLDOWN_MIN 동안 다시 울리지 않는다(상태 파일 last_burst_at).
    """
    now = now or datetime.now()
    config = load_breaking_alert_settings()
    if not config["burst_enabled"]:
        return False
    state = load_state()
    last = state.get("last_burst_at")
    if last:
        try:
            if now - datetime.fromisoformat(last) < timedelta(minutes=BURST_COOLDOWN_MIN):
                return False
        except (TypeError, ValueError):
            pass
    floor = now - timedelta(minutes=config["burst_window_min"])
    pairs = [
        (dt, it) for dt, it in _dated([it for it in alerted_items(now) if it.get("kind") == _KIND])
        if floor <= dt <= now
    ]
    if len(_distinct_outlets(pairs)) < config["burst_min_outlets"]:
        return False
    chat_ids = active_alert_chat_ids(_KIND)
    if chat_ids:
        message = build_burst_message(pairs)
        result = send_telegram_text(message, chat_ids)
        send_log.record(
            "burst", message.split("\n", 1)[0] + " · " + (message.split("\n")[1] if "\n" in message else ""),
            send_log.deliveries("telegram", result, {r["chat_id"]: r.get("name") for r in load_telegram_recipients()}),
            now=now,
        )
    # 받는 사람이 없어도 "울린 것"으로 적는다 — mark_alerted와 같은 이유(같은 판정 반복 방지).
    save_state({**load_state(), "last_burst_at": now.isoformat(timespec="seconds")})
    logger.info("[속보] 몰림 알림 — %d개 언론사 (%d분 창)", len(_distinct_outlets(pairs)), config["burst_window_min"])
    return True


def _max_window(pairs: list, window_min: int) -> list:
    """하루치 [(게시시각, 항목)] 중 window_min분 안에 언론사가 가장 많이 몰린 구간."""
    best, best_n = [], 0
    span = timedelta(minutes=window_min)
    for i, (start, _it) in enumerate(pairs):
        window = [p for p in pairs[i:] if p[0] - start < span]
        n = len(_distinct_outlets(window))
        if n > best_n:
            best, best_n = window, n
    return best


def burst_history(now: Optional[datetime] = None, days: int = BURST_HISTORY_DAYS) -> dict:
    """설정 화면 "지난 기록에 대 보면"용 — 저장된 정기 회차의 [속보]로, 날마다 창 길이별
    가장 많이 몰린 구간을 뽑는다. 화면 JS가 고른 기준으로 거른다.

    돌려주는 값: {"first": "M/D", "last": "M/D", "win": {창: [{d, n, s, e, t}]}}.
    재료가 폴링 기록이 아니라 정기 회차라 감시 그룹과 조금 다를 수 있다(화면 문구가 밝힌다).
    """
    from app.run_index import list_run_meta
    from app.storage import load_run_file

    now = now or datetime.now()
    floor = (now - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    by_day: dict = {}
    seen_urls: set = set()
    dates = set()
    for meta in list_run_meta():
        date_str = meta["run_at"][:10]
        if date_str < floor:
            continue
        dates.add(date_str)
        run = load_run_file(meta["path"])
        for article in (run or {}).get("articles", []):
            url = article.get("url")
            if not url or url in seen_urls or headline_kind(article.get("title", "")) != _KIND:
                continue
            seen_urls.add(url)
            dt = pub_dt(article.get("pub_date"))
            if dt is not None:
                by_day.setdefault(dt.date(), []).append((dt, article))
    result = {"first": "", "last": "", "win": {}}
    if dates:
        first, last = min(dates), max(dates)
        result["first"] = f"{int(first[5:7])}/{int(first[8:10])}"
        result["last"] = f"{int(last[5:7])}/{int(last[8:10])}"
    for window_min in BURST_WINDOW_CHOICES:
        rows = []
        for day in sorted(by_day):
            best = _max_window(sorted(by_day[day], key=lambda p: p[0]), window_min)
            rows.append({
                "d": f"{day.month}/{day.day}",
                "n": len(_distinct_outlets(best)),
                "s": f"{best[0][0]:%H:%M}",
                "e": f"{best[-1][0]:%H:%M}",
                "t": _strip_tag(best[0][1].get("title", "")),
            })
        result["win"][str(window_min)] = rows
    return result
