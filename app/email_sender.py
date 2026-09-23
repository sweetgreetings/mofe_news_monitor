# Design Ref: 사용자 요청(2026-08-06) — 정기 스크랩 완료/초안 화면에서 이메일로도 결과를 보낸다.
import html
import logging
import re
import smtplib
from email.mime.text import MIMEText
from email.utils import formataddr

from app.config import EMAIL_SERVICES
from app.credentials import (
    email_is_configured,
    email_sender_address,
    email_sender_name,
    email_sender_password,
    email_smtp_host,
    email_smtp_port,
)
from app.send_result import SendResult

logger = logging.getLogger(__name__)

_URL_PATTERN = re.compile(r"https?://[^\s<]+")


def is_configured() -> bool:
    """SMTP 서버·보내는 사람 주소·비밀번호가 모두 있는지 — [수정: 2026-09-18] 설정 화면
    (연동 › 이메일 보내는 계정) 값이 먼저, 없으면 .env(app.credentials)."""
    return email_is_configured()


def _from_header(address: str, name: str) -> str:
    """보내는 사람 칸 — 이름이 있으면 「재경부 디소팀 <주소>」, 없으면 주소만."""
    return formataddr((name, address), charset="utf-8") if name else address


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
        logger.warning("이메일 보내는 계정 미설정(설정 › 연동 또는 .env) — 전송을 건너뜁니다.")
        return SendResult(False, [{"target": None, "error": "이메일 채널이 설정되지 않았습니다"}])
    if not recipients:
        logger.warning("이메일 받는 사람이 없습니다 — 전송을 건너뜁니다.")
        return SendResult(False, [])

    ok = True
    failures = []
    delivered = []
    address = email_sender_address()
    from_header = _from_header(address, email_sender_name())
    try:
        with smtplib.SMTP(email_smtp_host(), email_smtp_port(), timeout=10) as server:
            server.starttls()
            server.login(address, email_sender_password())
            html_body = _linkify(body)
            for recipient in recipients:
                message = MIMEText(html_body, "html", "utf-8")
                message["Subject"] = subject
                message["From"] = from_header
                message["To"] = recipient
                try:
                    server.sendmail(address, [recipient], message.as_string())
                    delivered.append({"target": recipient})
                except smtplib.SMTPException as exc:
                    logger.exception("이메일 전송 실패(%s)", recipient)
                    ok = False
                    failures.append({"target": recipient, "error": _describe_error(exc)})
    except (smtplib.SMTPException, OSError) as exc:
        # 연결·로그인 단계에서 터진 오류라 받는 사람 전원에게 못 나갔다 — 전원을
        # 실패로 기록한다(개별 sendmail까지 못 간 사람들도 이유는 알아야 한다).
        logger.exception("이메일 서버 연결 중 오류")
        # 연결이 중간에 끊겼으면 그 전에 받은 사람은 받은 것으로 둔다.
        done = {d["target"] for d in delivered}
        return SendResult(
            False,
            [{"target": r, "error": _describe_error(exc)} for r in recipients if r not in done],
            delivered,
        )
    return SendResult(ok, failures, delivered)


def check_sender_address(service: str, address: str) -> str:
    """보내는 사람 주소가 고른 서비스와 맞지 않으면 그 이유 한 줄, 맞으면 빈 문자열.
    네이버는 자기 도메인 주소로만 보낼 수 있어 저장 전에 막는다(서버가 거절한다)."""
    address = (address or "").strip()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", address):
        return "보내는 사람 주소를 메일 주소 형식으로 입력해주세요."
    domain = EMAIL_SERVICES[service]["domain"]
    if domain and not address.lower().endswith("@" + domain):
        return f"{EMAIL_SERVICES[service]['label']}로 보내려면 @{domain} 주소여야 합니다."
    return ""


def send_test_mail(service: str, address: str, password: str, name: str) -> tuple[bool, str]:
    """설정 화면의 [시험 메일 보내기] — 저장 전 값으로 보내는 사람 주소 자신에게 한 통.
    받는 사람 명단에는 보내지 않는다(시험 메일이 다른 사람에게 가면 안 된다)."""
    if service not in EMAIL_SERVICES:
        return False, "메일 서비스를 골라주세요."
    problem = check_sender_address(service, address)
    if problem:
        return False, problem
    if not (password or "").strip():
        return False, "앱 비밀번호를 입력해주세요."
    address = address.strip()
    spec = EMAIL_SERVICES[service]
    message = MIMEText(_linkify("언론 모니터링 앱의 시험 메일입니다. 이 메일이 보이면 보내는 계정 설정이 맞습니다."), "html", "utf-8")
    message["Subject"] = "[시험] 언론 모니터링 이메일 발송"
    message["From"] = _from_header(address, (name or "").strip())
    message["To"] = address
    try:
        with smtplib.SMTP(spec["host"], spec["port"], timeout=10) as server:
            server.starttls()
            server.login(address, password.strip())
            server.sendmail(address, [address], message.as_string())
    except (smtplib.SMTPException, OSError) as exc:
        logger.warning("시험 메일 실패: %s", exc.__class__.__name__)
        return False, _describe_error(exc)
    return True, f"{address}(으)로 시험 메일을 보냈습니다 — 받은편지함을 확인해주세요."
