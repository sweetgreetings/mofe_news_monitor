# Design Ref: 사용자 결정(2026-09-17) — 텔레그램 받는 사람마다 정기·수시 보고서를 「기사」·
# 「요약」 중 무엇으로 받을지 고른다(mockups/TELEGRAM_RECIPIENTS_MOCKUP.html).
#
# 정기(app.confirm_send)와 수시(app.adhoc.send)가 같은 규칙으로 메시지를 짓도록 여기 한
# 곳에 둔다 — 두 흐름이 각자 조립하면 한쪽만 고쳤을 때 받는 사람이 보는 모양이 갈린다.
from app.send_result import SendResult
from app.telegram_bot import send_text as send_telegram_text


def compose(text: str, summary: str, want_articles: bool, want_summary: bool) -> str:
    """한 사람에게 갈 메시지.

    - 기사만: 기사 목록 그대로(지금까지 나가던 것).
    - 요약만: 보고서 머리(첫 빈 줄 앞 — 회차 헤더와 키워드 메모)만 남기고 그 아래 요약.
    - 둘 다: **한 통**에 기사 목록 + 그 아래 요약(두 통으로 나누지 않는다).
    """
    if want_summary and summary:
        if want_articles:
            return f"{text.rstrip()}\n\n{summary}\n"
        head = text.split("\n\n", 1)[0].rstrip()
        return f"{head}\n\n{summary}\n"
    return text


def plan_messages(recipients: list, flow: str, text: str, summary: str, degraded: bool = False) -> list:
    """받는 사람을 받을 메시지별로 묶는다 — [(메시지, [chat_id…]), …].

    요약을 골랐는데 보낼 요약이 없거나(빈 문자열) AI 분류가 실패한 보고서면(degraded —
    요약이 AI가 쓴 게 아니라 첫 기사 발췌다) 기사 목록을 같이 보낸다. 요약만 받는 사람이
    그 회차를 통째로 부실하게 받는 것보다 낫다.
    """
    summary_usable = bool(summary) and not degraded
    buckets: dict = {}
    for r in recipients:
        buckets.setdefault(_wants(r, flow, summary_usable), []).append(r["chat_id"])
    return [
        (compose(text, summary, want_articles, want_summary), chat_ids)
        for (want_articles, want_summary), chat_ids in buckets.items()
    ]


def _wants(recipient: dict, flow: str, summary_usable: bool) -> tuple:
    """이 사람이 이번에 받는 것 (기사 목록?, 요약?) — plan_messages와 발송 기록이 같은 판단을 쓴다."""
    want_summary = recipient.get(f"{flow}_summary", False) and summary_usable
    want_articles = recipient.get(f"{flow}_articles", False) or not want_summary
    return want_articles, want_summary


_CONTENT_LABELS = {(True, False): "기사 목록", (False, True): "요약만", (True, True): "기사 + 요약"}


def content_labels(recipients: list, flow: str, summary: str, degraded: bool = False) -> dict:
    """{chat_id: 받은 것} — 발송 기록(app.send_log)에 사람마다 무엇이 갔는지 적는다."""
    summary_usable = bool(summary) and not degraded
    return {r["chat_id"]: _CONTENT_LABELS[_wants(r, flow, summary_usable)] for r in recipients}


def email_message(text: str, summary: str, degraded: bool = False) -> str:
    """이메일 본문 — 받는 사람별 선택이 없으니 늘 기사 목록 + 그 아래 요약(텔레그램 「둘 다」와
    같은 모양). 요약이 없거나 AI 분류가 실패한 보고서면 기사 목록만(plan_messages와 같은 판단)."""
    return compose(text, summary, True, bool(summary) and not degraded)


def send_planned(plan: list) -> SendResult:
    """plan_messages 결과를 보낸다. 전원 성공이어야 ok(SendResult의 기존 판단과 같다)."""
    ok, failures, delivered = True, [], []
    for message, chat_ids in plan:
        result = send_telegram_text(message, chat_ids)
        ok = ok and bool(result)
        failures.extend(result.failures)
        delivered.extend(result.delivered)
    return SendResult(ok, failures, delivered)
