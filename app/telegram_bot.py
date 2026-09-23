# Design Ref: 사용자 요청(2026-08-03) — 정기 스크랩 완료/초안 수동 전송 시 텔레그램 봇으로 결과를 보낸다.
# [수정: 2026-08-07] 챗 아이디 1개 고정(.env) 방식에서 이메일 전송과 같은 "여러 받는 사람
# + 켜고 끄기" 방식으로 바꿨다 — app.telegram_recipients 참고.
import logging
from datetime import datetime
from typing import Optional

import requests

from app import credentials
from app.send_result import SendResult
from app.telegram_recipients import silent_chat_ids

logger = logging.getLogger(__name__)

# 텔레그램 sendMessage 1건당 글자 수 제한. 기사 제목·URL 한 줄이 이 값을 넘길 일은
# 사실상 없다고 보고, 줄 단위로만 잘라도 충분하다고 가정한다.
_MESSAGE_LIMIT = 4096


def is_configured() -> bool:
    """봇 토큰이 있는지(설정 화면 › 연동 › 텔레그램 발송 계정, 없으면 .env) — 받는 사람은
    봇 토큰과 별개로 관리하므로(app.telegram_recipients), 여기서는 토큰만 확인한다."""
    return bool(credentials.telegram_bot_token())


def check_bot_token(token: str) -> tuple:
    """[연결 확인] — 메시지는 보내지 않고 이 토큰이 어느 봇인지만 묻는다(getMe·getMyName).
    (ok, 담당자에게 보여줄 한 줄)."""
    token = (token or "").strip()
    if not token:
        return False, "봇 토큰을 넣어 주세요."
    base = f"https://api.telegram.org/bot{token}"
    try:
        resp = requests.get(f"{base}/getMe", timeout=10)
    except requests.exceptions.RequestException:
        return False, "텔레그램에 연결하지 못했습니다 — 인터넷 연결을 확인해 주세요."
    if resp.status_code in (401, 404):
        return False, "텔레그램이 이 토큰을 받지 않았습니다 — 복사할 때 앞뒤가 잘리지 않았는지 확인해 주세요."
    if not resp.ok:
        return False, f"텔레그램이 거절했습니다(HTTP {resp.status_code})."
    try:
        username = resp.json()["result"]["username"]
    except (ValueError, KeyError, TypeError):
        return False, "텔레그램 응답을 읽지 못했습니다."
    name = ""
    try:
        name_resp = requests.get(f"{base}/getMyName", timeout=10)
        if name_resp.ok:
            name = str(name_resp.json()["result"]["name"])
    except (requests.exceptions.RequestException, ValueError, KeyError, TypeError):
        pass
    who = f"@{username} ({name})" if name else f"@{username}"
    return True, f"{who} 에 연결됩니다."


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


def _describe_error(status_code: int, body: str) -> str:
    """텔레그램 API 오류를 담당자가 바로 알아볼 수 있는 한국어 한 줄로 바꾼다.

    [추가: 2026-08-26] 정기 보관함의 발송 실패 표시(빨간 점 툴팁)가 이 문구를 그대로
    쓴다 — app.confirm_send까지는 이미 이 문구가 있었는데(logger.warning), 회차
    파일에는 저장되지 않아 로그를 열어보지 않는 한 아무도 원인을 알 수 없었다.
    """
    if status_code == 401:
        return "봇 토큰이 유효하지 않습니다(401) — 토큰이 만료됐거나 잘못됐을 수 있습니다"
    if status_code == 403:
        # 발송 기록(/send-log)이 이유마다 고치는 방법을 다르게 적는다 — 텔레그램이 본문에 말해 주는
        # 만큼은 갈라 둔다(못 가르면 예전 문구 그대로).
        lowered = body.lower()
        if "blocked by the user" in lowered:
            return "받는 사람이 봇을 차단했습니다(403)"
        if "initiate conversation" in lowered:
            return "받는 사람이 아직 봇과 대화를 시작하지 않았습니다(403)"
        if "deactivated" in lowered:
            return "받는 사람의 텔레그램 계정이 없어졌습니다(403)"
        return "받는 사람이 봇을 차단했거나 대화를 시작하지 않았습니다(403)"
    if status_code == 400 and "chat not found" in body.lower():
        return "chat_id를 찾을 수 없습니다(400) — 받는 사람 설정을 확인해주세요"
    if status_code == 429:
        return "요청이 너무 잦습니다(429) — 잠시 후 다시 시도됩니다"
    return f"전송 실패(HTTP {status_code})"


def send_text(text: str, chat_ids: list, now: Optional[datetime] = None) -> SendResult:
    """텍스트를 받는 사람들(chat_id 목록)에게 각각 보낸다.

    토큰 미설정이거나 받는 사람이 없거나 전송에 실패해도 예외를 던지지 않고
    SendResult(ok=False, ...)만 반환한다 — 호출하는 쪽(스케줄러의 정기 수집 루프 등)이
    이 실패로 멈추면 안 되기 때문이다(Naver API처럼 3회 재시도하지 않는 이유도 같다:
    텔레그램이 잠깐 안 되더라도 스크랩·화면 갱신 자체는 항상 정상적으로 끝나야 한다).
    받는 사람이 여럿일 때 일부만 실패해도 나머지에게는 계속 보낸다(app.email_sender.
    send_text와 동일한 방침).

    [수정: 2026-08-26] 반환 타입이 bool에서 SendResult로 바뀌었다 — 정기 보관함에
    발송 실패 사유를 보여주려면(사용자 요청) "안 됐다"만이 아니라 "왜 안 됐는지",
    "누구에게 안 갔는지"가 회차 파일까지 전달돼야 한다. SendResult는 bool()로 평가하면
    예전과 완전히 같이 동작해(ok만 본다) 이 함수를 쓰던 기존 코드는 하나도 안 고쳤다.
    """
    if not is_configured():
        logger.warning("텔레그램 봇 토큰 미설정(설정 › 연동 또는 .env) — 전송을 건너뜁니다.")
        return SendResult(False, [{"target": None, "error": "텔레그램 봇 토큰이 설정되지 않았습니다"}])
    if not chat_ids:
        logger.warning("텔레그램 받는 사람이 없습니다 — 전송을 건너뜁니다.")
        return SendResult(False, [])
    url = f"https://api.telegram.org/bot{credentials.telegram_bot_token()}/sendMessage"
    chunks = _split_text(text)
    # [추가: 2026-09-17] 받는 사람마다 정한 알림 시간 밖이면 알림 없이 보낸다
    # (disable_notification) — 메시지는 그대로 가고 소리·진동·화면 알림만 없다. 모든 발송
    # (정기·수시·[단독]·[속보]·호출 한도 경고)이 이 함수를 거치므로 여기 한 곳이면 된다.
    # 명단에 없는 chat id는 울린다(app.telegram_recipients.silent_chat_ids).
    silent = silent_chat_ids(now)
    ok = True
    failures = []
    delivered = []
    for chat_id in chat_ids:
        chat_ok = True
        chat_error = None
        for chunk in chunks:
            try:
                payload = {"chat_id": chat_id, "text": chunk}
                if chat_id in silent:
                    payload["disable_notification"] = "true"
                resp = requests.post(url, data=payload, timeout=10)
                if not resp.ok:
                    logger.warning("텔레그램 전송 실패(%s, chat_id=%s): %s", resp.status_code, chat_id, resp.text)
                    chat_ok = False
                    if chat_error is None:
                        chat_error = _describe_error(resp.status_code, resp.text)
            except requests.exceptions.Timeout:
                logger.exception("텔레그램 전송 시간 초과(chat_id=%s)", chat_id)
                chat_ok = False
                if chat_error is None:
                    chat_error = "연결 시간 초과"
            except requests.exceptions.RequestException:
                logger.exception("텔레그램 전송 중 오류(chat_id=%s)", chat_id)
                chat_ok = False
                if chat_error is None:
                    chat_error = "네트워크 연결 실패"
        if not chat_ok:
            ok = False
            failures.append({"target": chat_id, "error": chat_error})
        else:
            delivered.append({"target": chat_id, "silent": chat_id in silent, "parts": len(chunks)})
    return SendResult(ok, failures, delivered)
