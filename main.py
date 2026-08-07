# Design Ref: PRD.md 4절 — 앱 실행 시 자동으로 스케줄이 돌고, 브라우저가 열려 화면으로 연결됨
import logging
import threading
import webbrowser

from app.config import LANDING_HTML_PATH, SETTINGS_SERVER_HOST, SETTINGS_SERVER_PORT
from app.history_renderer import generate_history_page
from app.landing_renderer import generate_landing_page
from app.renderer import build_latest_plain_text, generate_screen, generate_waiting_page
from app.scheduler import run_due_slot, run_scheduler
from app.scraper import collect_run_with_retry
from app.settings import load_settings
from app.settings_server import run_settings_server
from app.storage import delete_expired_runs, is_today, load_latest_run
from app.telegram_bot import send_text
from app.telegram_recipients import active_recipient_chat_ids
from app.email_recipients import active_recipient_emails
from app.email_sender import send_text as send_email_text

logger = logging.getLogger(__name__)


def _scrape_and_render(run_slot: str, window_start: str) -> dict:
    """한 회차를 수집한 뒤 곧바로 메인/지난 기사/진입 화면을 다시 만든다.

    스케줄러(app.scheduler)가 회차마다 scrape(run_slot, window_start) 형태로 호출한다
    (PRD.md 기능1 규칙 2 — 회차별 시간창 수집. window_start~run_slot 구간의 기사만 모음).
    09:00 이후 10:30·13:30·16:30에도 화면 파일이 계속 최신으로 갱신된다 (수집만 하고
    화면을 안 만들면, 하루 종일 첫 회차 화면에서 멈춰 있게 된다). 진입 화면 워드클라우드는
    접속할 때마다 별도로 다시 계산되므로(app.landing_renderer) 여기서도 한 번 갱신해 두면
    되지만 필수는 아니다 — 최신 저장 데이터를 미리 반영해 둔다.

    [추가: 2026-07-26] 이번 회차가 0건이고 오늘 이미 다른 회차가 있었다면, 메인 화면은
    그 이전 회차 화면을 그대로 두고 갱신을 건너뛴다 — "10:30 기준"인데 사실은 아무것도
    없다는 표시(💤)로 바뀌는 것보다, 아직 유효한 이전 회차 기사를 계속 보여주는 쪽이
    낫다는 판단(사용자 논의 결과). 오늘 첫 회차라서 비교할 이전 회차가 없으면 지금처럼
    💤를 그대로 보여준다 — 그 경우엔 달리 보여줄 게 없기 때문. history.html은 이 회차를
    여전히 💤로 기록한다(사실 그대로의 기록이라 다른 문제).
    """
    existing = load_latest_run()
    had_today_run = existing is not None and is_today(existing)

    result = collect_run_with_retry(run_slot, window_start)
    if result["articles"] or not had_today_run:
        generate_screen()
        # [추가: 2026-08-03] 정기 회차가 실제로 화면에 반영될 때만(=위 조건과 동일) 텔레그램
        # 자동 전송도 함께 시도한다 — 화면 갱신을 건너뛴 경우(0건+오늘 이미 다른 회차 있음)
        # 까지 보내면 방금 회차가 아니라 예전 회차 내용을 다시 보내는 꼴이라 혼란만 준다.
        settings = load_settings()
        if settings.get("telegram_auto_send", False):
            chat_ids = active_recipient_chat_ids()
            if chat_ids:
                plain_text = build_latest_plain_text()
                if plain_text:
                    send_text(plain_text, chat_ids)
        # [추가: 2026-08-06] 텔레그램과 같은 이유·같은 조건(화면이 실제로 갱신될 때만) —
        # 이메일판. 받는 사람이 하나도 없으면(app.email_recipients) 굳이 시도하지 않는다.
        if settings.get("email_auto_send", False):
            recipients = active_recipient_emails()
            if recipients:
                plain_text = build_latest_plain_text()
                if plain_text:
                    send_email_text(plain_text.split("\n", 1)[0], plain_text, recipients)
    generate_history_page()
    generate_landing_page()
    return result


def main() -> None:
    """앱 진입점: 켜지자마자 놓친 회차가 있으면 즉시 1회 수집한 뒤, 스케줄 루프와 설정
    저장 서버를 백그라운드로 띄우고 브라우저로 화면을 연다 (PRD.md 4절)."""
    # 며칠 꺼뒀다 켠 경우, 정리(cleanup)는 스케줄 루프 안에서만 비동기로 도는데(scheduler.py)
    # 그걸 기다리면 아래에서 만들 "지난 기사 더보기"가 7일 지난 회차까지 잠깐 보여줄 수 있다.
    # 화면을 만들기 전에 한 번 먼저 정리한다.
    try:
        delete_expired_runs()
    except Exception:
        logger.exception("시작 시 보관 기간 정리 실패 — 스케줄 루프가 이어서 재시도합니다")

    try:
        # 오늘 지나온 회차 중 아직 안 돌았으면 지금 바로 실행 (스케줄 루프의 tick을 기다리지 않음).
        # 실패해도 앱을 죽이지 않는다 — 곧 시작될 스케줄 루프의 첫 tick이 자연히 다시 시도한다.
        run_due_slot(scrape=_scrape_and_render)
    except Exception:
        logger.exception("시작 시 회차 수집 실패 — 스케줄 루프가 이어서 재시도합니다")

    threading.Thread(target=run_scheduler, kwargs={"scrape": _scrape_and_render}, daemon=True).start()
    threading.Thread(target=run_settings_server, daemon=True).start()

    try:
        generate_screen()
    except RuntimeError:
        # 오늘 첫 회차 이전에 앱을 켠 경우, 또는 자정이 지나 어제 회차만 남아있는 경우.
        generate_waiting_page()
    # 아래 둘은 회차가 하나도 없어도 예외를 내지 않으므로(빈 목록/빈 워드클라우드로 처리),
    # generate_screen의 성공 여부와 무관하게 항상 만든다.
    generate_history_page()
    generate_landing_page()

    # [수정: 2026-07-31] SERVER_HOST를 기본값(127.0.0.1)이 아닌 값으로 설정했다면
    # (Tailscale 등으로 다른 컴퓨터에서 접속하는 원격 서버로 쓰겠다는 의도) 이 컴퓨터
    # 자신은 화면을 볼 필요가 없고, 애초에 브라우저가 안 깔린 헤드리스 환경일 수도 있다.
    # webbrowser.open이 예외를 내면(브라우저를 못 찾음) 메인 스레드가 죽으면서 위에서
    # 띄운 데몬 스레드(스케줄러·설정 서버)까지 전부 강제 종료되므로, 원격 서버 모드에서는
    # 아예 건너뛰고 로컬 모드에서도 혹시 모를 예외를 대비해 감싼다.
    if SETTINGS_SERVER_HOST == "127.0.0.1":
        try:
            webbrowser.open(LANDING_HTML_PATH.as_uri())
        except webbrowser.Error:
            logger.warning(
                "브라우저를 자동으로 열지 못했습니다 — http://127.0.0.1:%s/home.html 로 직접 접속해주세요",
                SETTINGS_SERVER_PORT,
            )
    else:
        # [참고] 이 프로젝트는 logging.basicConfig을 따로 설정하지 않아, 기본
        # lastResort 핸들러가 WARNING 이상만 화면에 찍는다 — 이 안내는 반드시 눈에
        # 띄어야 하므로 info가 아니라 warning으로 남긴다(실제로는 정상 상태 안내).
        logger.warning(
            "원격 서버 모드(SERVER_HOST=%s)입니다 — 다른 컴퓨터에서 http://%s:%s/home.html 로 접속하세요",
            SETTINGS_SERVER_HOST, SETTINGS_SERVER_HOST, SETTINGS_SERVER_PORT,
        )

    threading.Event().wait()  # 데몬 스레드가 계속 돌도록 메인 스레드를 대기시킨다 (Ctrl+C로 종료)


if __name__ == "__main__":
    main()
