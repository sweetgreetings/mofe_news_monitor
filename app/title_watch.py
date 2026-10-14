# Design Ref: archive/TITLE_CHANGE_DESIGN.md §2·§7 — 거기서 「뒤에서 지켜보기」를 기각하고
# 「몇 주 써 보고 정한다」로 미뤘던 알림이다. 사용자 결정(2026-10-13)으로 **라벨 붙인
# 기사만**으로 범위를 좁혀 되살렸다(회차 전체를 네 번 보던 원안은 그대로 기각).
"""라벨 붙인 기사의 제목이 원문에서 바뀌었는지 주기마다 돌아보고 텔레그램으로 알린다.

**비교 규칙을 새로 만들지 않는다** — 담당자가 「제목 다시 불러오기」 버튼을 누를 때 쓰는
app.title_refetch를 `apply=False`로 그대로 부른다. 이 모듈이 더하는 건 둘뿐이다:

- **때맞춰 도는 것** — 마지막 확인에서 TITLE_WATCH_INTERVAL_MIN이 지난 첫 tick.
- **이미 알린 건 다시 안 알리는 것** — 담당자가 반영하지 않고 그대로 두기로 했을 때
  매일 아침 같은 알림이 또 오면 안 된다. 키는 **URL + 다듬은 새 제목**이라, 같은 기사가
  두 번 고쳐지면 둘째 변경은 다시 알린다(URL만 쓰면 둘째를 놓친다).

**제목을 알아서 덮어쓰지 않는다**(`apply=False`가 그 뜻이다). 담당자가 안 보는 사이에
화면 제목이 바뀌면 이미 복사해 쓴 보고서와 말없이 어긋나고, ✏️로 손수 적은 제목까지
조용히 사라진다. 알림은 **알리고 끝**이고, 반영은 라벨 보관함의 그 버튼이 맡는다.

**감시 대상은 라벨 붙인 기사뿐이다.** 라벨은 이 앱에서 나이로 안 지우는 유일한 장치라
제목 오차가 가장 오래 남는 자리다(회차 기사는 그날 쓰고 끝난다). 거기에 발행 나이
제한(TITLE_WATCH_MAX_AGE_DAYS)과 건수 상한(TITLE_WATCH_MAX_ARTICLES)이 붙는다 —
라벨이 쌓이면 기한 없이는 작년 기사 수백 건을 매일 아침 다시 받아오게 된다.

**받는 사람이 「제목」 칸을 켜야 돈다**(기본 꺼짐) — 아무도 안 켰으면 확인조차 하지 않아
네트워크도 파일 쓰기도 없다. 끈 상태에서 「알렸다」고 적어 두면 나중에 켜도 그 변경이
영영 오지 않으므로, 끈 동안은 아무것도 적지 않는 쪽이 맞다.

LLM도 네이버 검색 API도 안 쓴다 — 이미 아는 주소를 직접 열어 글자를 비교한다.
"""
import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.atomic_write import atomic_write_text
from app.config import (
    TITLE_WATCH_FILE,
    TITLE_WATCH_INTERVAL_MIN,
    TITLE_WATCH_MAX_AGE_DAYS,
    TITLE_WATCH_MAX_ARTICLES,
)
from app.title_refetch import normalize_title, parse_stamp

logger = logging.getLogger(__name__)

_KST = timezone(timedelta(hours=9))
_lock = threading.Lock()

# 마지막으로 확인한 시각을 메모리에도 들고 있는다 — 디스크에 못 쓰는 상태(꽉 참·권한)에서
# 파일값만 보면 주기 판정이 매 tick 참이 되어 **같은 알림을 10초마다 다시 보낸다**.
# 앱을 다시 켜면 잊지만, 그때는 한 주기 일찍 한 번 더 도는 것뿐이다.
_memo_last_run_at: Optional[str] = None


# ──────────────────────────────────────────────────────────────────────────────
# 순수 함수 — 저장소·네트워크 없음
# ──────────────────────────────────────────────────────────────────────────────

def is_due(now: datetime, last_run_at: Optional[str]) -> bool:
    """지금 돌아볼 때인가 — 마지막 확인에서 주기가 지났으면 참.

    tick마다 불러도 비용이 0이어야 하므로(run_topup_if_due·poll_and_alert_tick과 같은
    모양) 판정이 파일 한 번 읽기로 끝난다.

    한 번도 안 돌았으면(`None`) 참 — 켠 직후 곧바로 한 번 본다. **적힌 시각이 지금보다
    미래면 참**이다(시계를 되돌렸거나 파일이 깨진 경우) — 거짓으로 두면 그 시각이 올
    때까지 몇 시간이고 조용해진다.
    """
    seen = parse_stamp(last_run_at)
    if seen is None:
        return True
    base = now.astimezone(_KST).replace(tzinfo=None) if now.tzinfo else now
    if seen > base:
        return True
    return (base - seen).total_seconds() >= TITLE_WATCH_INTERVAL_MIN * 60


def _age_stamp(entry: dict) -> Optional[datetime]:
    """나이를 재는 기준 시각 — 발행 시각, 없으면 라벨 붙인 시각.

    발행 시각을 모르는 기사를 기한 없음으로 두면 영원히 매일 아침 다시 받아온다.
    라벨 붙인 시각은 늘 있으므로(app.labels가 적는다) 그걸 대신 쓴다.
    """
    return parse_stamp(entry.get("pub_date")) or parse_stamp(entry.get("labeled_at"))


def due_articles(labels: dict, now: datetime) -> list:
    """이번 아침에 돌아볼 기사 — `{"url", "title", "pub_date", "labels", "outlet"}` 목록.

    labels: app.labels.load_labels() 결과. **제목은 화면이 보여주는 값을 넘겨야 하므로**
    부르는 쪽이 app.summary_overrides.apply_summary_overrides를 입힌 뒤 넘긴다.

    고르는 규칙 셋:
      · 발행(없으면 라벨 붙인) 뒤 TITLE_WATCH_MAX_AGE_DAYS 안
      · 언론사 자체 도메인은 애초에 빼 둔다 — app.title_refetch가 어차피 `skipped`로
        돌려주지만, 여기서 빼지 않으면 못 보는 기사가 건수 상한 자리를 차지한다
      · 최신 것부터 TITLE_WATCH_MAX_ARTICLES건까지
    """
    from app.photo_body import is_mirror_url

    # parse_stamp는 KST naive를 돌려주므로 기준도 naive로 맞춘다(섞으면 비교에서 터진다).
    base = now.astimezone(_KST).replace(tzinfo=None) if now.tzinfo else now
    cutoff = base - timedelta(days=TITLE_WATCH_MAX_AGE_DAYS)
    rows = []
    for url, entry in labels.items():
        if not url or not is_mirror_url(url):
            continue
        stamp = _age_stamp(entry)
        if stamp is None or stamp < cutoff:
            continue
        rows.append((stamp, {
            "url": url,
            "title": entry.get("title") or "",
            "pub_date": entry.get("pub_date"),
            "labels": list(entry.get("labels") or []),
            "outlet": entry.get("outlet") or "",
        }))
    rows.sort(key=lambda pair: pair[0], reverse=True)
    return [row for _, row in rows[:TITLE_WATCH_MAX_ARTICLES]]


# URL 하나가 들고 있는 「이미 알린 제목」의 최대 개수 — 한 기사 제목이 이보다 더 여러 번
# 바뀌는 일은 없다시피 하고, 상한이 없으면 파일이 천천히 자란다.
_MAX_ALERTED_TITLES = 10


def alerted_titles(alerted: dict, url: str) -> list:
    """그 URL에 대해 이미 알린 제목들(다듬은 값). 옛 형식(문자열 하나)도 읽어준다."""
    seen = alerted.get(url)
    if isinstance(seen, str):
        return [seen]
    return [t for t in seen if isinstance(t, str)] if isinstance(seen, list) else []


def unalerted(found: list, alerted: dict) -> list:
    """이번에 알릴 것만 — 같은 URL에 같은 새 제목을 이미 알렸으면 뺀다.

    `alerted`는 `{url: [다듬은 새 제목, …]}`이다. **목록이어야 한다** — 마지막 하나만
    기억하면 제목이 A→B→C로 바뀐 뒤 언론사가 **C를 B로 되돌렸을 때** B를 다시 알린다
    (저장값은 여전히 A라 「바뀜」이고, 마지막으로 알린 건 C라 B가 새것으로 보인다).

    다듬어 비교하므로 따옴표 모양만 달라진 같은 제목을 두 번 알리지 않는다
    (app.title_refetch.normalize_title과 같은 규칙).
    """
    return [
        item for item in found
        if normalize_title(item["new_title"]) not in alerted_titles(alerted, item["url"])
    ]


def build_message(items: list, now: datetime) -> str:
    """알림 한 통 — 여러 건이면 한 통으로 묶는다([속보] 같은 사건 묶기와 같은 규칙).

    **「전/후」 제목은 받아온 그대로** 적는다(다듬은 값은 비교용이다). 라벨 이름을 적는
    이유는 회차 대신 그게 「왜 이 기사를 지켜보고 있었나」이기 때문이다 — 「부정기사」가
    바뀐 것과 「테스트」가 바뀐 것은 급한 정도가 다르다.

    수정 시각을 못 읽으면 그 줄에서 뺀다 — 지어내지 않는다([단독]·[속보] 알림이 게시
    시각을 못 읽을 때와 같다).
    """
    head = f"✎ 라벨 붙인 기사 제목이 변경됐어요 {len(items)}건"
    blocks = []
    for item in items:
        where = " · ".join(filter(None, [
            f"({item['outlet']})" if item.get("outlet") else "",
            f"🏷 {' + '.join(item['labels'])}" if item.get("labels") else "",
        ]))
        lines = [where] if where else []
        lines.append(f"전: {item['old_title']}")
        lines.append(f"후: {item['new_title']}")
        stamp = parse_stamp(item.get("modified_at"))
        tail = f"{stamp.strftime('%H:%M')} 수정 · {item['url']}" if stamp else item["url"]
        lines.append(tail)
        blocks.append("\n".join(lines))
    foot = "라벨 보관함에서 「제목 다시 불러오기」를 누르면 반영됩니다."
    return f"{head}\n\n" + "\n\n".join(blocks) + f"\n\n{foot}"


# ──────────────────────────────────────────────────────────────────────────────
# 여기부터는 바깥을 쓴다 — 위쪽 순수 함수와 섞지 않는다.
# ──────────────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    """`{"last_run_at": "...", "alerted": {url: [다듬은 제목, …]}}`.

    깨져 있으면 처음부터 시작한다 — 그 한 번은 이미 알린 것이 다시 올 수 있지만,
    고착되지는 않는다(알린 것을 적을 자리가 다시 생긴다).
    """
    blank = {"last_run_at": None, "alerted": {}}
    if not TITLE_WATCH_FILE.exists():
        return blank
    try:
        data = json.loads(TITLE_WATCH_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, TypeError):
        logger.warning("제목 변경 알림 상태 파일을 읽지 못했습니다 — 처음부터 시작합니다.")
        return blank
    if not isinstance(data, dict):
        return blank
    alerted = data.get("alerted")
    return {
        "last_run_at": data.get("last_run_at"),
        "alerted": alerted if isinstance(alerted, dict) else {},
    }


def _save_state(state: dict, keep_urls: Optional[set] = None) -> bool:
    """상태를 적는다. 돌려주는 값은 **정말로 디스크에 썼는가**.

    `keep_urls`를 주면 라벨이 떼인 기사를 `alerted`에서 버린다 — 지켜볼 이유가
    없어진 기사라 기록만 남으면 파일이 영원히 자란다.

    **부르는 쪽이 「라벨을 정말로 읽었다」를 확인한 뒤에만 `keep_urls`를 준다.**
    `app.labels.load_labels()`는 파일이 깨졌을 때도 `{}`를 돌려주므로(「라벨 없음」과
    「못 읽었음」이 같은 값이다), 그대로 넘기면 읽기 실패 한 번에 **알린 기록이 통째로
    지워지고** 다음 주기에 그동안 알린 것 전부가 한꺼번에 다시 간다.
    """
    alerted = state.get("alerted") or {}
    if keep_urls is not None:
        alerted = {u: t for u, t in alerted.items() if u in keep_urls}
    try:
        atomic_write_text(TITLE_WATCH_FILE, json.dumps(
            {"last_run_at": state.get("last_run_at"), "alerted": alerted},
            ensure_ascii=False, indent=2,
        ))
        return True
    except OSError:
        logger.warning("제목 변경 알림 상태를 저장하지 못했습니다.")
        return False


def _send(items: list, chat_ids: list, now: datetime) -> bool:
    """텔레그램으로 한 통 보내고 발송 기록에 적는다. 한 명이라도 받았으면 참.

    받는 사람은 watch_tick이 맨 앞에서 이미 확인했다 — 여기서 비어 있을 수 없다.
    """
    from app import send_log
    from app.telegram_bot import send_text as send_telegram_text
    from app.telegram_recipients import load_telegram_recipients

    text = build_message(items, now)
    head = text.split("\n", 1)[0]
    names = {r["chat_id"]: r.get("name") for r in load_telegram_recipients()}
    result = send_telegram_text(text, chat_ids)
    send_log.record(
        "title_change", head,
        send_log.deliveries("telegram", result, names),
        articles=[{"title": i["new_title"], "url": i["url"]} for i in items],
        now=now,
    )
    return bool(result.delivered)


def _run_once(state: dict, chat_ids: list, now: datetime) -> int:
    """한 주기 분을 실제로 돈다 — 받아오고, 대조하고, 바뀐 게 있으면 한 통 보낸다.

    **여기서 나는 예외는 부르는 쪽이 받는다**(스케줄러까지 올라가면 그 tick의 뒷일
    — 보관 기간 정리 묶음 — 이 통째로 건너뛰어진다).
    """
    from app.labels import load_labels
    from app.summary_overrides import apply_summary_overrides
    from app.title_refetch import refetch_titles

    labels = load_labels()
    # 라벨이 0건이면 **가지치기를 하지 않는다** — 정말 다 떼었을 수도 있지만 파일을 못
    # 읽었을 수도 있고(둘이 같은 값이다), 뒤쪽이면 알린 기록이 통째로 날아간다.
    keep = set(labels) if labels else None
    targets = due_articles(labels, now)
    if not targets:
        state["last_run_at"] = now.isoformat(timespec="seconds")
        _save_state(state, keep_urls=keep)
        return 0

    # 화면과 같은 제목을 봐야 한다 — 라벨 보관함도 overrides를 입힌 제목을 보여준다.
    targets = apply_summary_overrides(targets)
    by_url = {a["url"]: a for a in targets}
    # apply=False — 보기만 한다. 반영은 담당자가 버튼을 누를 때(모듈 설명 참고).
    result = refetch_titles(targets, apply=False)

    found = [
        {**by_url[url], **verdict, "url": url}
        for url, verdict in result["rows"].items()
        if verdict.get("state") == "changed"
    ]
    fresh = unalerted(found, state["alerted"])
    counts = result["counts"]
    logger.info(
        "제목 변경 확인: %d건 확인 · 변경 %d건(새로 알릴 것 %d건) · 못 받음 %d건",
        counts.get("checked", 0), counts.get("title_changed", 0), len(fresh),
        counts.get("failed", 0),
    )
    state["last_run_at"] = now.isoformat(timespec="seconds")
    if not fresh:
        _save_state(state, keep_urls=keep)
        return 0

    if not _send(fresh, chat_ids, now):
        # 아무에게도 못 갔다 — 알린 것으로 적지 않아 다음 주기에 다시 보낸다.
        # (주기가 짧아 따로 재시도 횟수를 둘 필요가 없다.)
        logger.warning("제목 변경 알림을 아무에게도 보내지 못했습니다 — 다음 주기에 다시 보냅니다.")
        _save_state(state, keep_urls=keep)
        return 0

    for item in fresh:
        seen = alerted_titles(state["alerted"], item["url"])
        seen.append(normalize_title(item["new_title"]))
        state["alerted"][item["url"]] = seen[-_MAX_ALERTED_TITLES:]
    _save_state(state, keep_urls=keep)
    return len(fresh)


def watch_tick(now: Optional[datetime] = None, *, background: bool = True) -> int:
    """스케줄러 tick이 매번 부른다 — **돌아볼 때가 아니면 파일 한 번 읽고 바로 돌아온다.**

    돌려주는 값은 이번에 알린 기사 수. `background=True`(기본)면 일을 스레드에 넘기고
    **0을 돌려주고 즉시 끝난다** — 받아오기가 느린 날 tick을 몇 분씩 붙잡으면 그 뒤의
    회차 수집·자동발송 유예 확인이 그만큼 밀린다(app.negative_guess가 스레드를 쓰는
    것과 같은 이유). 테스트는 `background=False`로 끝까지 기다린다.
    """
    from app.telegram_recipients import active_alert_chat_ids

    now = now or datetime.now(_KST)
    # **아무도 「제목」 칸을 켜지 않았으면 확인조차 하지 않는다.** 칸은 기본이 꺼짐이라
    # 이 기능은 켜기 전까지 완전히 조용하다(네트워크·파일 쓰기 0).
    #
    # 「보내려다 받는 사람이 없었다」로 적고 넘어가면 안 된다 — 그러면 알린 것으로 기록돼
    # 나중에 칸을 켜도 **그 변경은 영영 오지 않는다**(키가 URL + 새 제목이라 그 제목이
    # 또 바뀌지 않는 한 다시 걸리지 않는다). 지금 안 보고 두면 켠 뒤 첫 주기에 온다.
    chat_ids = active_alert_chat_ids("제목")
    if not chat_ids:
        return 0
    # 앞 주기가 아직 돌고 있으면 그냥 넘어간다(기다리지 않는다 — tick을 막으면 안 된다).
    if not _lock.acquire(blocking=False):
        return 0

    released = False
    try:
        state = load_state()
        # 저장이 실패한 동안에도 주기를 지키려고 마지막 확인 시각을 메모리에도 들고 있다
        # — 디스크에 못 쓰는 상태에서 파일값만 보면 `is_due`가 매 tick 참이 되어 같은
        # 알림을 10초마다 다시 보낸다.
        last = max(filter(None, [state.get("last_run_at"), _memo_last_run_at]), default=None)
        if not is_due(now, last):
            return 0

        def _go() -> int:
            global _memo_last_run_at
            try:
                _memo_last_run_at = now.isoformat(timespec="seconds")
                return _run_once(state, chat_ids, now)
            except Exception:
                # 여기서 막지 않으면 스케줄러까지 올라가 그 tick의 보관 기간 정리가
                # 통째로 건너뛰어진다(app.labels가 읽기 권한 오류를 안 잡는다).
                logger.exception("제목 변경 확인 중 오류 — 다음 주기에 다시 시도합니다")
                return 0
            finally:
                _lock.release()

        released = True   # _go가 책임지고 푼다
        if background:
            threading.Thread(target=_go, name="title-watch", daemon=True).start()
            return 0
        return _go()
    finally:
        if not released:
            _lock.release()
