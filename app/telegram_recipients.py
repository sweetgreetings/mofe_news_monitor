# Design Ref: 사용자 요청(2026-08-07) — 텔레그램 전송 받는 사람 여러 명 관리(켜고 끄기 토글).
# app.email_recipients와 완전히 동일한 구조·동작이다.
import json

from app.atomic_write import atomic_write_text
from app.config import TELEGRAM_CHAT_ID, TELEGRAM_RECIPIENTS_FILE


def load_telegram_recipients() -> list:
    """등록된 받는 사람 목록을 읽어온다(순서: 등록한 순서 그대로).

    각 항목은 {"name": str, "chat_id": str, "enabled": bool} 형태다. enabled가 꺼진
    사람은 목록에는 남아있지만 실제 전송(active_recipient_chat_ids) 대상에서는 빠진다.

    [추가: 2026-08-07] 이 파일이 아직 한 번도 저장된 적 없고(파일 없음) .env에
    TELEGRAM_CHAT_ID가 남아있으면, 예전 "챗 아이디 1개 고정" 방식으로 이미 쓰고 있던
    사람을 잃지 않도록 "나"라는 이름으로 한 번만 자동 등록해준다(이후로는 이 파일이
    기준이라 .env 값은 다시 참조하지 않는다).
    """
    if not TELEGRAM_RECIPIENTS_FILE.exists():
        if TELEGRAM_CHAT_ID:
            seeded = [{"name": "나", "chat_id": TELEGRAM_CHAT_ID, "enabled": True}]
            _write(seeded)
            return seeded
        return []
    try:
        data = json.loads(TELEGRAM_RECIPIENTS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, TypeError):
        return []
    return data if isinstance(data, list) else []


def _write(recipients: list) -> None:
    atomic_write_text(TELEGRAM_RECIPIENTS_FILE, json.dumps(recipients, ensure_ascii=False, indent=2))


def save_telegram_recipients(recipients: list) -> None:
    """받는 사람 목록 전체를 통째로 저장한다(app.email_recipients.save_email_recipients와 동일한 패턴)."""
    cleaned = [
        {
            "name": (r.get("name") or r.get("chat_id", "")).strip(),
            "chat_id": r["chat_id"].strip(),
            "enabled": bool(r.get("enabled")),
        }
        for r in recipients
        if r.get("chat_id", "").strip()
    ]
    atomic_write_text(TELEGRAM_RECIPIENTS_FILE, json.dumps(cleaned, ensure_ascii=False, indent=2))


def active_recipient_chat_ids() -> list:
    """실제로 전송해야 할 켜진 사람들의 chat id만 뽑아 돌려준다."""
    return [r["chat_id"] for r in load_telegram_recipients() if r.get("enabled", True)]
