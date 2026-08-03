# Design Ref: 사용자 요청(2026-08-03) — 정기 스크랩 완료/초안 수동 전송 시 텔레그램 봇으로 결과를 보낸다.
import logging

import requests

from app.config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)

# 텔레그램 sendMessage 1건당 글자 수 제한. 기사 제목·URL 한 줄이 이 값을 넘길 일은
# 사실상 없다고 보고, 줄 단위로만 잘라도 충분하다고 가정한다.
_MESSAGE_LIMIT = 4096


def is_configured() -> bool:
    """.env에 TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID가 둘 다 설정돼 있는지."""
    return bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)


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


def send_text(text: str) -> bool:
    """텍스트를 텔레그램으로 보낸다.

    토큰/챗아이디 미설정이거나 전송에 실패해도 예외를 던지지 않고 False만 반환한다 —
    호출하는 쪽(스케줄러의 정기 수집 루프 등)이 이 실패로 멈추면 안 되기 때문이다
    (Naver API처럼 3회 재시도하지 않는 이유도 같다: 텔레그램이 잠깐 안 되더라도
    스크랩·화면 갱신 자체는 항상 정상적으로 끝나야 한다).
    """
    if not is_configured():
        logger.warning("텔레그램 미설정(TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID) — 전송을 건너뜁니다.")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    ok = True
    for chunk in _split_text(text):
        try:
            resp = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": chunk}, timeout=10)
            if not resp.ok:
                logger.warning("텔레그램 전송 실패(%s): %s", resp.status_code, resp.text)
                ok = False
        except requests.exceptions.RequestException:
            logger.exception("텔레그램 전송 중 오류")
            ok = False
    return ok
