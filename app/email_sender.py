# Design Ref: 사용자 요청(2026-08-06) — 정기 스크랩 완료/초안 화면에서 이메일로도 결과를 보낸다.
import html
import logging
import re
import smtplib
from email.mime.text import MIMEText

from app.config import EMAIL_SENDER_ADDRESS, EMAIL_SENDER_PASSWORD, EMAIL_SMTP_HOST, EMAIL_SMTP_PORT
from app.send_result import SendResult

logger = logging.getLogger(__name__)

_URL_PATTERN = re.compile(r"https?://[^\s<]+")


def is_configured() -> bool:
    """.env에 SMTP 호스트·보내는 사람 주소·비밀번호가 모두 설정돼 있는지."""
    return bool(EMAIL_SMTP_HOST and EMAIL_SENDER_ADDRESS and EMAIL_SENDER_PASSWORD)


def _linkify(text: str) -> str:
    """일반 텍스트를 그대로 HTML 메일 본문으로 쓸 수 있게 변환한다.

    [수정: 2026-08-06] 처음엔 순수 텍스트(text/plain)로 보냈는데, 네이버 메일 앱에서
    기사 URL이 클릭 안 되는 문자로만 표시된다는 제보 — 일부 메일 클라이언트는 순수
    텍스트 안 URL을 자동으로 링크 처리해주지 않는다. HTML로 바꿔 URL만 실제
    <a href> 링크로 감싸고, 나머지는 <pre>로 줄바꿈·공백을 그대로 유지한다.
    """
    escaped = html.escape(text)
    linked = _URL_PATTERN.sub(lambda m: f'<a href="{m.group(0)}">{m.group(0)}</a>', escaped)
    return f'<pre style="font-family: inherit; white-space: pre-wrap; word-break: break-all;">{linked}</pre>'


def _describe_error(exc: Exception) -> str:
    """SMTP 예외를 담당자가 바로 알아볼 수 있는 한국어 한 줄로 바꾼다.

    [추가: 2026-08-26] 정기 보관함의 발송 실패 표시(빨간 점 툴팁)가 이 문구를 그대로
    쓴다 — 예전엔 이 원인이 logger.exception 안에만 있어 로그를 열어보지 않으면
    아무도 알 수 없었다.
    """
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return "SMTP 인증 실패 — 보내는 계정의 주소·비밀번호를 확인해주세요"
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return "받는 사람 주소가 서버에서 거부됐습니다"
    if isinstance(exc, (smtplib.SMTPConnectError, smtplib.SMTPServerDisconnected, OSError)):
        return "메일 서버에 연결할 수 없습니다"
    return f"전송 실패({exc.__class__.__name__})"


def send_text(subject: str, body: str, recipients: list) -> SendResult:
    """텍스트를 받는 사람들에게 각각 보낸다(한 사람씩 개별 발송 — 받는 사람끼리 서로의
    주소가 노출되지 않도록).

    미설정이거나 받는 사람이 없거나 전송에 실패해도 예외를 던지지 않고
    SendResult(ok=False, ...)만 반환한다 — app.telegram_bot.send_text와 같은 이유
    (정기 스크랩이 이 실패로 멈추면 안 된다). 받는 사람이 여럿일 때 일부만 실패해도
    나머지에게는 계속 보낸다.

    [수정: 2026-08-26] 반환 타입이 bool에서 SendResult로 바뀌었다 — app.telegram_bot.
    send_text와 같은 이유(정기 보관함에 실패 사유를 보여주려면 "안 됐다"만으로는
    부족하다). SendResult는 bool()로 평가하면 예전과 완전히 같이 동작한다.
    """
    if not is_configured():
        logger.warning("이메일 미설정(EMAIL_SMTP_HOST/EMAIL_SENDER_ADDRESS/EMAIL_SENDER_PASSWORD) — 전송을 건너뜁니다.")
        return SendResult(False, [{"target": None, "error": "이메일 채널이 설정되지 않았습니다"}])
    if not recipients:
        logger.warning("이메일 받는 사람이 없습니다 — 전송을 건너뜁니다.")
        return SendResult(False, [])

    ok = True
    failures = []
    try:
        with smtplib.SMTP(EMAIL_SMTP_HOST, EMAIL_SMTP_PORT, timeout=10) as server:
            server.starttls()
            server.login(EMAIL_SENDER_ADDRESS, EMAIL_SENDER_PASSWORD)
            html_body = _linkify(body)
            for recipient in recipients:
                message = MIMEText(html_body, "html", "utf-8")
                message["Subject"] = subject
                message["From"] = EMAIL_SENDER_ADDRESS
                message["To"] = recipient
                try:
                    server.sendmail(EMAIL_SENDER_ADDRESS, [recipient], message.as_string())
                except smtplib.SMTPException as exc:
                    logger.exception("이메일 전송 실패(%s)", recipient)
                    ok = False
                    failures.append({"target": recipient, "error": _describe_error(exc)})
    except (smtplib.SMTPException, OSError) as exc:
        # 연결·로그인 단계에서 터진 오류라 받는 사람 전원에게 못 나갔다 — 전원을
        # 실패로 기록한다(개별 sendmail까지 못 간 사람들도 이유는 알아야 한다).
        logger.exception("이메일 서버 연결 중 오류")
        return SendResult(False, [{"target": r, "error": _describe_error(exc)} for r in recipients])
    return SendResult(ok, failures)
