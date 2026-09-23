# Design Ref: 사용자 요청(2026-08-20) — [단독]·[속보] 기사 알림. 세 경로가 이 모듈의
# detect_and_alert() 하나를 공유한다:
#   (1) "바닥"(app.scraper.collect_run이 직접 호출) — 정기 회차가 어차피 검색한 결과를
#       그대로 검사한다, 추가 네이버 호출 0회. 이 모듈의 다른 함수와 달리 알림 설정
#       (enabled 여부·감시 그룹)을 전혀 보지 않고 항상 동작한다 — 설정 화면을 다 꺼도
#       정기 회차 검사만은 계속된다는 게 이 기능의 약속이라서다(CLAUDE.md 참고).
#   (2) "폴링"(poll_and_alert_tick, app.scheduler가 매 tick마다 호출) — 설정된 감시
#       시간대·주기로 별도 검색을 돌려 더 빨리 알아챈다. 감시 그룹이 실제로 필요한
#       유일한 경로.
#   (3) "몰아보내기" — 감시가 안 돌던 사이에 나온 기사를 자정까지 거슬러 모아 보낸다.
#       부르는 자리가 둘이다: (3a) catch_up_on_wake — main.py가 앱 시작 시 1회.
#       (3b) poll_and_alert_tick의 그날 첫 폴링 — 앱이 계속 켜져 있어 (3a)가 안 도는
#       경우를 메운다. 설정 화면의 체크박스가 "감시 시간 밖에 나온 기사는 다음 감시
#       시작 때 몰아서 받기"라고 약속하는 게 바로 (3b)다(예전엔 (3a)밖에 없어서, 밤새
#       켜둔 컴퓨터에서는 그 약속이 지켜지지 않았다). (3b)는 별도 검색이 아니라 그날
#       첫 폴링의 하한(after)을 감시 시작 시각 대신 자정으로 내리는 것이라, 네이버
#       추가 호출이 사실상 없다.
# 세 경로 모두 app.alerted_urls로 중복 발송을 막는다 — 같은 기사를 세 경로가 동시에
# 발견해도 한 번만 나간다.
import logging
from datetime import datetime
from typing import Callable, Optional

from app import send_log
from app.alert_event import folded_by_sent, group_by_event
from app.alerted_urls import already_alerted_urls, alerted_items, mark_alerted
from app.api_usage import should_pause_polling
from app.breaking_alert import is_within_window, load_breaking_alert_settings
from app.breaking_alert_state import load_state, save_state
from app.breaking_burst import check_and_alert_burst
from app.config import BURST_MAX_OUTLET_NAMES, DEFAULT_ARTICLE_LINE_TEMPLATE
from app.filters import headline_kind
from app.naver_api import kst_today_at, search_keywords
from app.settings import active_search_groups, load_settings
from app.telegram_bot import send_text as send_telegram_text
from app.telegram_recipients import active_alert_chat_ids, load_telegram_recipients

logger = logging.getLogger(__name__)

_ALERT_KINDS = ("단독", "속보")
# 같은 사건 묶기를 적용하는 말머리 — [속보]만이다(이유는 app.alert_event 맨 위 주석).
_FOLD_KIND = "속보"


def _line_for(article: dict, template: str) -> str:
    return template.format(outlet=article.get("outlet", ""), title=article.get("title", ""))


# [추가: 2026-09-16] "밤사이·새벽"이라고 부를 수 있는 구간(사용자 결정) — 23시부터
# 다음 날 6시 전까지. 06:00 정각은 밤이 아니다(감시 시간대 기본 시작이 06:00이다).
_NIGHT_START_HOUR = 23
_NIGHT_END_HOUR = 6


def _default_header(kind: str, articles: list) -> str:
    count = len(articles)
    suffix = "가" if count == 1 else f" {count}건이"
    return f"🚨 [{kind}] 기사{suffix} 올라왔어요"


def _all_published_at_night(articles: list) -> bool:
    """이 묶음의 기사가 **전부** 밤사이(23시~6시)에 나왔는지. 한 건이라도 발행시각을
    모르면 False다 — 모르는 걸 "밤"이라고 단정하지 않는다(게시 시각 줄과 같은 규칙)."""
    for article in articles:
        raw = article.get("pub_date")
        if not raw:
            return False
        try:
            parsed = datetime.fromisoformat(raw)
        except (TypeError, ValueError):
            return False
        if not (parsed.hour >= _NIGHT_START_HOUR or parsed.hour < _NIGHT_END_HOUR):
            return False
    return True


def _pub_time_text(article: dict, now: datetime) -> str:
    """[추가: 2026-09-16] 알림 줄에 붙일 게시 시각(사용자 요청). 이 기능이 알려주는 건
    "새 [단독]이 떴다"인데, 세 경로 모두 발견까지 시차가 있어(폴링 주기 최대 30분,
    정기 회차는 회차 간격, 네이버 색인 지연 몇 분) 받는 사람이 방금 나온 기사인지
    두 시간 전 기사인지 구분할 수 없었다.

    - "게시" 라벨을 붙인다 — 텔레그램은 메시지마다 자기 전송 시각을 옆에 찍으므로,
      시각만 덩그러니 있으면 그 둘이 헷갈린다(화면 _format_pub_time은 같은 이유의
      반대 사정 — 거기엔 다른 종류의 시각이 없어 라벨을 뗐다).
    - pub_date가 없거나 형식이 깨졌으면 그 줄을 통째로 뺀다. 없는 시각을 지어내지
      않는다(화면과 같은 규칙).
    - 오늘이 아니면 날짜까지 적는다. 세 경로 모두 당일 기사만 다루지만, 그 전제가
      언젠가 깨졌을 때 조용히 틀린 시각을 보여주면 안 된다.
    """
    raw = article.get("pub_date")
    if not raw:
        return ""
    try:
        parsed = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return ""
    hhmm = parsed.strftime("%H:%M")
    if parsed.date() == now.date():
        return f"{hhmm} 게시"
    return f"{parsed.month}/{parsed.day} {hhmm} 게시"


def _article_block(article: dict, template: str, now: datetime) -> str:
    """기사 한 건의 덩어리 — 제목 줄 / URL / 게시 시각.

    [수정: 2026-09-16] 게시 시각이 URL **다음** 줄이다(사용자 결정 — 처음엔 제목과 URL
    사이였다). 제목 줄과 URL은 예전부터 붙어 있던 한 쌍이라 그 사이에 뭘 끼우면 원래
    있던 두 줄이 갈라진다 — 새로 더한 값은 덩어리 맨 아래에 얹는다.

    제목 줄 뒤에 이어 붙이지 않고 자기 줄에 두는 것은 그대로다: 제목이 길면 텔레그램이
    알아서 줄을 접어 시각이 건마다 다른 자리에 떨어진다(여러 건을 훑을 때 시각이 같은
    자리에 있어야 읽힌다). 시각을 못 읽은 기사는 예전과 똑같은 2줄짜리로 남는다."""
    lines = [_line_for(article, template), article["url"], _pub_time_text(article, now)]
    return "\n".join(line for line in lines if line)


def _event_block(group: list, template: str, now: datetime) -> str:
    """같은 사건 묶음 한 덩어리 — 대표 기사 블록 + 나머지 언론사 이름 한 줄.

    같은 사건을 15개 언론사가 낸 날(2026-09-22 실측)엔 이 줄 하나가 블록 14개를 대신한다.
    이름을 적는 상한은 몰림 알림과 같은 값(BURST_MAX_OUTLET_NAMES)을 쓴다 — 두 메시지의
    언론사 나열이 같은 모양이어야 한다. 언론사 이름을 하나도 못 읽으면 건수만 적는다
    (없는 이름을 지어내지 않는다)."""
    lead, rest = group[0], group[1:]
    block = _article_block(lead, template, now)
    if not rest:
        return block
    lead_name = (lead.get("outlet") or "").strip()
    names: list = []
    for article in rest:
        name = (article.get("outlet") or "").strip()
        if name and name != lead_name and name not in names:
            names.append(name)
    if not names:
        return f"{block}\n↳ 같은 사건 {len(rest)}건"
    shown = names[:BURST_MAX_OUTLET_NAMES]
    line = " · ".join(shown)
    if len(names) > len(shown):
        line += f" 외 {len(names) - len(shown)}곳"
    return f"{block}\n↳ 같은 사건: {line}"


def _record_item(article: dict, kind: str, now: datetime, folded: bool = False) -> dict:
    """app.alerted_urls에 남길 한 건. folded=True는 같은 사건이라 통에 안 실린 기사다."""
    item = {
        "url": article["url"],
        "kind": kind,
        "outlet": article.get("outlet", ""),
        "title": article.get("title", ""),
        "pub_date": article.get("pub_date"),
        "at": now.isoformat(timespec="seconds"),
    }
    if folded:
        item["folded"] = True
    return item


def _recipient_names() -> dict:
    """{chat_id: 이름} — 발송 기록에 사람 이름으로 적는다."""
    return {r["chat_id"]: r.get("name") for r in load_telegram_recipients()}


def _send_batch(kind: str, articles: list, header: str, settings: dict, now: datetime) -> None:
    """이 말머리(kind)를 받기로 한 사람에게만 보낸다 — [단독]/[속보] 수신자가 서로
    다를 수 있어(app.telegram_recipients.active_alert_chat_ids) kind마다 따로 보낸다."""
    chat_ids = active_alert_chat_ids(kind)
    if not chat_ids:
        return
    template = settings.get("article_line_template", DEFAULT_ARTICLE_LINE_TEMPLATE)
    # [속보]는 한 통 안에서 같은 사건끼리 묶는다(app.alert_event) — 15개 언론사가 같은
    # 사건을 낸 통이 블록 15개가 아니라 1개 + 언론사 한 줄이 된다. [단독]은 묶지 않는다.
    if kind == _FOLD_KIND:
        blocks = [_event_block(group, template, now) for group in group_by_event(articles)]
    else:
        blocks = [_article_block(a, template, now) for a in articles]
    body = "\n\n".join(blocks)
    result = send_telegram_text(f"{header}\n\n{body}", chat_ids)
    send_log.record(
        send_log.alert_kind(kind), header,
        send_log.deliveries("telegram", result, _recipient_names()),
        articles=articles, now=now,
    )


def detect_and_alert(
    articles: list,
    header_fn: Optional[Callable[[str, list], str]] = None,
    now: Optional[datetime] = None,
) -> int:
    """articles 중 [단독]/[속보] 말머리 기사를 찾아, 아직 안 보낸 것만 대상 수신자에게
    보낸다. 돌려주는 값은 이번에 새로 처리한(=alerted_urls에 새로 추가한) 기사 수 —
    호출부 로그·테스트 검증용.

    header_fn(kind, articles): 메시지 맨 위 한 줄을 만드는 함수 — 생략하면 기본 문구
    ("🚨 [단독] 기사가 올라왔어요")를 쓴다. 몰아보내기 두 경로(catch_up_on_wake,
    poll_and_alert_tick의 그날 첫 폴링)가 각자의 "… 게시된" 문구로 바꿔 부른다.
    [수정: 2026-09-16] 인자가 건수가 아니라 기사 목록이다 — 앱 시작 몰아보내기가
    "밤사이·새벽"이라고 말해도 되는지를 그 묶음의 발행시각으로 판단하기 때문이다.
    """
    now = now or datetime.now()
    seen = already_alerted_urls(now)
    candidates: dict = {}
    for article in articles:
        url = article.get("url")
        if not url or url in seen:
            continue
        kind = headline_kind(article.get("title", ""))
        if kind not in _ALERT_KINDS:
            continue
        # 같은 기사가 여러 키워드에 동시에 걸려 이 목록에 여러 번 들어온다(폴링·몰아보내기는
        # 키워드별 결과를 평평하게 합친다) — 한 번만 담는다. 예전엔 이걸 안 걸러 한 메시지에
        # 같은 기사가 두세 번 실렸다(HISTORY.md "[단독]·[속보] 같은 기사 중복").
        seen.add(url)
        candidates.setdefault(kind, []).append(article)

    if not candidates:
        return 0

    # [추가] 오늘 이미 알림이 나간 사건의 후속 [속보]는 **새 통을 만들지 않는다**
    # (app.alert_event) — 폴링이 3분마다 돌아 한 사건이 여러 통으로 갈라지므로, 한 통 안
    # 묶기만으로는 울리는 횟수가 그대로다. 접은 기사도 아래에서 기록엔 남겨(홈 목록·몰림
    # 판정) 어디서도 사라지지 않는다.
    folded = []
    flash_candidates = candidates.get(_FOLD_KIND)
    if flash_candidates:
        sent_flash = [it for it in alerted_items(now) if it.get("kind") == _FOLD_KIND]
        fresh = []
        for article in flash_candidates:
            previous = folded_by_sent(article, sent_flash) if sent_flash else None
            if previous is None:
                fresh.append(article)
            else:
                folded.append(article)
                logger.info(
                    "[속보] 같은 사건이라 새 알림을 보내지 않음 — %s (앞서 보낸 기사: %s)",
                    article.get("title", "")[:40], (previous.get("title") or "")[:40],
                )
        if fresh:
            candidates[_FOLD_KIND] = fresh
        else:
            candidates.pop(_FOLD_KIND)

    settings = load_settings()
    header_fn = header_fn or _default_header
    newly_handled = []
    # [추가: 2026-08-26] 보낸 기사의 표시 정보도 같이 남긴다 — 홈 화면이 "오늘 [단독] N건"을
    # 이 기록 하나만 보고 그리게 하기 위해서다. 회차 파일에서 다시 세면 알림이 실제로
    # 나간 것과 숫자가 어긋난다(app/alerted_urls.py 맨 위 주석 참고).
    sent_items = []
    for kind, arts in candidates.items():
        _send_batch(kind, arts, header_fn(kind, arts), settings, now)
        newly_handled.extend(a["url"] for a in arts)
        sent_items.extend(_record_item(a, kind, now) for a in arts)

    # 접은 기사도 같은 기록에 남긴다 — 홈 [속보] 목록엔 그대로 보이고, 몰림 판정도 이
    # 기록으로 세므로 "개별 알림은 한 통, 정말 몰리면 몰림 알림이 전체 언론사 수를 알림"이
    # 된다. folded 표시를 달아 "왜 이 기사는 어느 통에도 없나"를 나중에 되짚을 수 있게 한다.
    newly_handled.extend(a["url"] for a in folded)
    sent_items.extend(_record_item(a, _FOLD_KIND, now, folded=True) for a in folded)

    mark_alerted(newly_handled, now, items=sent_items)
    # [속보]를 새로 보냈으면 몰림인지 본다 — 개별 알림 뒤에 요약 한 통(app.breaking_burst).
    # 기록을 남긴 뒤에 불러야 방금 보낸 기사까지 센다. **접기만 한 경우에도 본다** — 몰림
    # 알림이야말로 이때 필요한 정보이고(접힌 기사가 그 통의 언론사 목록으로 드러난다),
    # 여기서 빼면 쏟아진 사실이 아무 데도 남지 않는다.
    if _FOLD_KIND in candidates or folded:
        try:
            check_and_alert_burst(now)
        except Exception:
            logger.exception("[속보] 몰림 판정 실패 — 개별 알림은 이미 보냈습니다")
    return len(newly_handled)


def _watched_keywords(settings: dict, group_names: list) -> list:
    """감시 대상으로 고른 그룹들의 활성 키워드를 순서 유지·중복 없이 모은다.

    그룹의 OR/AND 매칭 규칙(app.naver_api.match_articles_to_groups)은 여기서 재현하지
    않는다 — 알림은 "이 그룹이 검색하는 키워드 중 뭐든 걸리면 살펴본다"는 넓은 감시이지,
    회차에 넣을지 말지를 정확히 가르는 판단이 아니다(app.live_renderer의
    matched_keywords 태그도 같은 이유로 그룹 로직이 아니라 키워드 단위로 계산한다)."""
    groups = active_search_groups(settings, settings.get("keyword_groups", []))
    selected = set(group_names)
    flat, seen = [], set()
    for group in groups:
        if group.get("name") not in selected:
            continue
        for keyword in group.get("keywords", []):
            if keyword not in seen:
                seen.add(keyword)
                flat.append(keyword)
    return flat


def _resolve_after(last_poll_at: Optional[str], now: datetime, window_start: str) -> datetime:
    """폴링의 검색 하한(after)을 정한다. 직전 폴링이 오늘 안에 있었으면 그 시각부터
    (초는 버리고 분까지만 — 몇 초 겹쳐 다시 조회해도 app.alerted_urls가 걸러주므로
    무해하다), 오늘 첫 폴링이거나 직전 폴링이 어제 것이면(자정을 건너뛰었으면) 감시
    시작 시각부터로 되돌린다."""
    if last_poll_at:
        last_poll_dt = datetime.fromisoformat(last_poll_at)
        if last_poll_dt.date() == now.date():
            return kst_today_at(last_poll_dt.strftime("%H:%M"))
    return kst_today_at(window_start)


def _notify_pause_once(now: datetime) -> None:
    """호출 한도 경고는 하루 한 번만 보낸다(중복 알림 방지) — app.breaking_alert_state에
    "오늘 이미 알렸다"는 날짜를 남겨둔다."""
    state = load_state()
    today = now.strftime("%Y-%m-%d")
    if state.get("pause_notified_date") == today:
        return
    chat_ids = sorted(set(active_alert_chat_ids("단독")) | set(active_alert_chat_ids("속보")))
    if chat_ids:
        result = send_telegram_text(
            "⚠️ 오늘 네이버 API 호출이 한도의 80%를 넘어 [단독]·[속보] 수시 감시를 잠시 "
            "멈췄습니다. 정기 회차 검사는 그대로 동작합니다.",
            chat_ids,
        )
        send_log.record(
            "quota", "네이버 API 호출 한도 80% 넘음 — [단독]·[속보] 감시 멈춤",
            send_log.deliveries("telegram", result, _recipient_names()), now=now,
        )
    save_state({**state, "pause_notified_date": today})


def poll_and_alert_tick(now: Optional[datetime] = None) -> None:
    """스케줄러 tick마다 불린다. 감시가 켜져 있고, 감시 그룹이 1개 이상이고, 지금이
    감시 시간대이고, 마지막 폴링 이후 설정한 주기가 지났을 때만 실제로 검색을 돈다 —
    그 외엔 즉시 반환(비용 0). 호출 한도의 80%를 넘었으면 검색 대신 경고만 보낸다.

    [추가: 2026-09-16] 그날 첫 폴링은 "몰아보내기"를 겸한다 — 하한을 감시 시작 시각
    대신 자정으로 내리고 문구도 바꿔 보낸다(위 모듈 주석 (3b)). 별도 검색이 아니라
    같은 검색의 하한만 넓히는 것이라 추가 호출이 사실상 없다.
    """
    now = now or datetime.now()
    config = load_breaking_alert_settings()
    if not config["enabled"] or not config["group_names"]:
        return
    now_hm = now.strftime("%H:%M")
    if not is_within_window(now_hm, config["start"], config["end"]):
        return

    state = load_state()
    last_poll_at = state.get("last_poll_at")
    if last_poll_at:
        elapsed_min = (now - datetime.fromisoformat(last_poll_at)).total_seconds() / 60
        if elapsed_min < config["interval_min"]:
            return

    if should_pause_polling(now):
        _notify_pause_once(now)
        return

    settings = load_settings()
    keywords = _watched_keywords(settings, config["group_names"])
    today = now.strftime("%Y-%m-%d")
    if not keywords:
        save_state({**state, "last_poll_at": now.isoformat(timespec="seconds"), "polled_date": today})
        return

    # polled_date는 "오늘 폴링을 이미 시작했다"는 표시다. catch_up_enabled와 무관하게
    # 늘 남기는 이유 — 그래야 하루 중간에 몰아보내기를 켜도 그날 아침 기사가 뒤늦게
    # 쏟아지지 않는다(그 판단은 그날 첫 폴링에서 한 번만 한다).
    catch_up_due = config["catch_up_enabled"] and state.get("polled_date") != today
    if catch_up_due:
        after = kst_today_at("00:00")
        header_fn = _window_catch_up_header
    else:
        after = _resolve_after(last_poll_at, now, config["start"])
        header_fn = None
    try:
        results, _failed, _capped = search_keywords(keywords, after=after)
    except Exception:
        # 실패하면 polled_date도 남기지 않는다 — 다음 폴링이 몰아보내기를 다시 시도한다.
        logger.exception("[단독]·[속보] 폴링 검색 실패 — 다음 폴링에 다시 시도합니다")
        return
    flat_articles = [a for arts in results.values() for a in arts]
    sent = detect_and_alert(flat_articles, header_fn=header_fn, now=now)
    if catch_up_due:
        logger.info(
            # "발송"이라 쓰지 않는다 — 돌려받는 수에는 같은 사건이라 접은 [속보]도 든다.
            "[단독]·[속보] 감시 시작 몰아보내기 — 자정부터 재확인, %d건 처리 (키워드 %d개)",
            sent,
            len(keywords),
        )
    save_state({**state, "last_poll_at": now.isoformat(timespec="seconds"), "polled_date": today})


def _catch_up_header(kind: str, articles: list) -> str:
    """(3a) 앱 시작 시 — 앱이 꺼져 있던 사이를 메운다.

    [수정: 2026-09-16] 문구가 둘로 갈린다(사용자 제보 — 16시 24분 기사에 "밤사이·새벽"이
    붙었다). 이 경로는 자정부터 지금까지를 훑으므로 **낮에 앱을 다시 켜면 그 낮 기사까지
    딸려 온다** — 즉 "밤사이·새벽"은 이 경로가 메우는 구간의 한 경우일 뿐인데 늘 그렇게
    말하고 있었다. 이제 묶음 전체가 실제로 밤(23시~6시)에 나왔을 때만 그렇게 부르고,
    아니면 이 경로가 언제나 참인 사실("앱을 켜기 전에")만 말한다.

    [수정: 2026-09-16, 2차] "놓친" → "게시된"(사용자 결정). "놓친"은 우리가 못 잡았다는
    자책인데 (a) 받는 사람에게 필요한 정보가 아니고, (b) 사실인지도 확실치 않다(앱이
    도는 동안 폴링이 한도로 멈춰 있었을 수도 있다). 반면 "게시"는 기사 줄의 `16:24 게시`가
    이미 쓰는 말이라 머리말과 줄이 같은 단어로 이어진다. 밤이 아닐 때 "앱이 꺼져 있던
    사이" 대신 "앱을 켜기 전에"로 바꾼 것도 같은 이유다 — 앱이 언제 꺼졌는지는 아무
    데도 안 남지만, 이 경로가 자정~지금(=앱을 켠 순간)을 훑는다는 건 언제나 참이다."""
    if _all_published_at_night(articles):
        return f"🌙 밤사이·새벽에 게시된 [{kind}] {len(articles)}건"
    return f"📴 앱을 켜기 전에 게시된 [{kind}] {len(articles)}건"


def _window_catch_up_header(kind: str, articles: list) -> str:
    """(3b) 그날 첫 폴링 시 — 설정 화면 체크박스 문구("감시 시간 밖에 나온 기사는 다음
    감시 시작 때 몰아서 받기")를 그대로 쓴다. 두 문구를 하나로 합치지 않는 이유는 둘이
    메우는 구간이 실제로 다르기 때문이다 — (3a)는 "앱이 꺼져 있던 동안"이라 감시 시간
    한복판일 수도 있고, (3b)는 언제나 "감시 시간 밖"이다. [수정: 2026-09-16, 2차]
    "나온" → "게시된" — 세 문구가 같은 말을 쓰게 맞췄다(위 _catch_up_header 참고)."""
    return f"🌙 감시 시간 밖에 게시된 [{kind}] {len(articles)}건"


def catch_up_on_wake(now: Optional[datetime] = None) -> None:
    """앱이 켜질 때(main.py) 한 번 호출한다 — 자정부터 지금까지 감시 대상 키워드를
    검색해, 앱이 꺼져 있던 사이(밤사이·컴퓨터 종료 등)에 게시된 [단독]·[속보]를 몰아서
    보낸다. catch_up_enabled가 꺼져 있거나 감시가 꺼져 있으면 아무 일도 하지 않는다.
    """
    now = now or datetime.now()
    config = load_breaking_alert_settings()
    if not config["enabled"] or not config["catch_up_enabled"] or not config["group_names"]:
        return
    settings = load_settings()
    keywords = _watched_keywords(settings, config["group_names"])
    if not keywords:
        return
    try:
        results, _failed, _capped = search_keywords(keywords, after=kst_today_at("00:00"))
    except Exception:
        logger.exception("[단독]·[속보] 몰아보내기 검색 실패 — 다음 폴링이 이어서 감시합니다")
        return
    flat_articles = [a for arts in results.values() for a in arts]
    detect_and_alert(flat_articles, header_fn=_catch_up_header, now=now)
    # polled_date를 같이 남겨, 이미 메운 구간을 그날 첫 폴링(3b)이 또 훑지 않게 한다.
    save_state({
        **load_state(),
        "last_poll_at": now.isoformat(timespec="seconds"),
        "polled_date": now.strftime("%Y-%m-%d"),
    })
