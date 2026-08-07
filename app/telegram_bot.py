# Design Ref: 사용자 요청(2026-08-03) — 정기 스크랩 완료/초안 수동 전송 시 텔레그램 봇으로 결과를 보낸다.
# [수정: 2026-08-07] 챗 아이디 1개 고정(.env) 방식에서 이메일 전송과 같은 "여러 받는 사람
# + 켜고 끄기" 방식으로 바꿨다 — app.telegram_recipients 참고.
import logging

import requests

from app.config import TELEGRAM_BOT_TOKEN

logger = logging.getLogger(__name__)

# 텔레그램 sendMessage 1건당 글자 수 제한. 기사 제목·URL 한 줄이 이 값을 넘길 일은
# 사실상 없다고 보고, 줄 단위로만 잘라도 충분하다고 가정한다.
_MESSAGE_LIMIT = 4096


def is_configured() -> bool:
    """.env에 TELEGRAM_BOT_TOKEN이 설정돼 있는지 — 받는 사람은 봇 토큰과 별개로
    설정 화면에서 관리하므로(app.telegram_recipients), 여기서는 토큰만 확인한다."""
    return bool(TELEGRAM_BOT_TOKEN)


def _split_text(text: str, limit: int = _MESSAGE_LIMIT) -> list:
    """줄바꿈 경계에서만 잘라 limit자 이하의 조각으로 나눈다 (기사 중간이 잘리지 않도록)."""
    if len(text) <= limit:
        return [text]
    lines = text.split("\n")
    chunks = []
    current: list = []
    for line in lines:
        candidate = current + [line]
        if len("\n".join(candidate)) > limit and current:
            chunks.append("\n".join(current))
            current = [line]
        else:
            current = candidate
    if current:
        chunks.append("\n".join(current))
    return chunks


def send_text(text: str, chat_ids: list) -> bool:
    """텍스트를 받는 사람들(chat_id 목록)에게 각각 보낸다.

    토큰 미설정이거나 받는 사람이 없거나 전송에 실패해도 예외를 던지지 않고 False만
    반환한다 — 호출하는 쪽(스케줄러의 정기 수집 루프 등)이 이 실패로 멈추면 안 되기
    때문이다(Naver API처럼 3회 재시도하지 않는 이유도 같다: 텔레그램이 잠깐 안 되더라도
    스크랩·화면 갱신 자체는 항상 정상적으로 끝나야 한다). 받는 사람이 여럿일 때 일부만
    실패해도 나머지에게는 계속 보낸다(app.email_sender.send_text와 동일한 방침).
    """
    if not is_configured():
        logger.warning("텔레그램 미설정(TELEGRAM_BOT_TOKEN) — 전송을 건너뜁니다.")
        return False
    if not chat_ids:
        logger.warning("텔레그램 받는 사람이 없습니다 — 전송을 건너뜁니다.")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    chunks = _split_text(text)
    ok = True
    for chat_id in chat_ids:
        for chunk in chunks:
            try:
                resp = requests.post(url, data={"chat_id": chat_id, "text": chunk}, timeout=10)
                if not resp.ok:
                    logger.warning("텔레그램 전송 실패(%s, chat_id=%s): %s", resp.status_code, chat_id, resp.text)
                    ok = False
            except requests.exceptions.RequestException:
                logger.exception("텔레그램 전송 중 오류(chat_id=%s)", chat_id)
                ok = False
    return ok
