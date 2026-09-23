# Design Ref: 사용자 결정(2026-09-17) — 수시 확정본도 텔레그램으로 보낸다
# (mockups/TELEGRAM_RECIPIENTS_MOCKUP.html).
#
# 수시엔 회차 마감이 없어 자동발송이 없다 — 담당자가 확정본 화면에서 (발송)을 누를 때만
# 나간다. 받는 사람은 텔레그램 받는 사람 화면에서 미리 정해둔 명단이고, 누를 때 뜨는
# 확인창에서 **그 한 번만** 몇 명을 뺄 수 있다(excluded — 저장하지 않는다).
# 이메일로는 보내지 않는다(이메일 명단엔 수시 칸이 없다).
from datetime import datetime
from typing import Optional

from app.adhoc.card import card_lock, is_bundle, load_card, save_card
from app.adhoc.renderer import _has_visible_articles, build_adhoc_plain_text, build_adhoc_summary_text
from app import send_log
from app.adhoc.card import bundle_label
from app.report_message import content_labels, plan_messages, send_planned
from app.settings import load_settings
from app.telegram_bot import is_configured as telegram_is_configured
from app.telegram_recipients import report_recipients

# 카드에 남기는 발송 기록 줄 수 상한 — 화면은 마지막 기록만 쓴다.
_SEND_LOG_MAX = 20


class AdhocSendError(Exception):
    """보내지 못한 이유 — 카드 화면 오류 배너에 그대로 싣는다."""


def send_bundle(card_id: str, excluded_chat_ids: Optional[set] = None, now: Optional[datetime] = None) -> dict:
    """확정본 하나를 텔레그램으로 보낸다. 돌려주는 값은 {"sent": 받은 사람 수, "failed": [이름…]}.

    두 번째 발송부터는 머리줄 앞에 "(수정)"을 붙인다(정기와 같은 규칙).
    """
    now = now or datetime.now()
    excluded = set(excluded_chat_ids or ())
    if not telegram_is_configured():
        raise AdhocSendError("텔레그램 봇 토큰이 설정되지 않아 보내지 못했어요.")

    # 보내는 동안(네트워크, 사람마다 최대 10초)엔 카드 락을 쥐지 않는다 — 그 사이 같은 카드의
    # 다른 동작이 멈추면 안 된다. 텍스트는 누른 순간의 카드로 만들고, 기록만 다시 락 안에서 남긴다.
    c = load_card(card_id)
    if c is None:
        raise AdhocSendError("카드를 찾지 못했어요.")
    if not is_bundle(c):
        raise AdhocSendError("발송은 확정본에서만 할 수 있어요.")
    if not _has_visible_articles(c):
        raise AdhocSendError("보낼 기사가 없어요.")
    recipients = [r for r in report_recipients("adhoc") if r["chat_id"] not in excluded]
    if not recipients:
        raise AdhocSendError("보낼 사람이 없어요 — 텔레그램 받는 사람에서 수시 칸을 켜주세요.")

    settings = load_settings()
    text = build_adhoc_plain_text(c, settings)
    if c.get("send_count", 0) >= 1:
        text = f"(수정){text}"
    summary = build_adhoc_summary_text(c, settings)
    result = send_planned(plan_messages(recipients, "adhoc", text, summary))

    name_by_id = {r["chat_id"]: r["name"] for r in recipients}
    left_out = [r["name"] for r in report_recipients("adhoc") if r["chat_id"] in excluded]
    send_log.record(
        "adhoc",
        f"{bundle_label(c)} 확정본",
        send_log.deliveries("telegram", result, name_by_id, content_labels(recipients, "adhoc", summary)),
        auto=False,
        send_no=c.get("send_count", 0) + 1,
        card_id=card_id,
        note=f"확인창에서 뺌: {', '.join(left_out)}" if left_out else "",
        now=now,
    )
    failed_ids = {f["target"] for f in result.failures if f.get("target")}
    if any(f.get("target") is None for f in result.failures):
        delivered = []
    else:
        delivered = [r for r in recipients if r["chat_id"] not in failed_ids]

    with card_lock(card_id):
        c = load_card(card_id)
        if c is not None:
            if delivered:
                c["send_count"] = c.get("send_count", 0) + 1
                c["sent_at"] = now.isoformat(timespec="seconds")
            c["send_log"] = (c.get("send_log") or [])[-(_SEND_LOG_MAX - 1):] + [{
                "at": now.isoformat(timespec="seconds"),
                "to": [r["name"] for r in delivered],
                "failed": [
                    {"target": name_by_id.get(f.get("target"), f.get("target") or "설정"), "error": f["error"]}
                    for f in result.failures
                ],
            }]
            save_card(c)
    return {"sent": len(delivered), "failed": [name_by_id.get(t, t) for t in failed_ids]}
