# Design Ref: PRD.md 4절 — 앱 실행 시 자동으로 스케줄이 돌고, 브라우저가 열려 화면으로 연결됨
import errno
import logging
import socket
import sys
import threading
import time
import webbrowser

from app import photo_body
from app.adhoc.card import delete_expired_cards
from app.breaking_alert_sender import catch_up_on_wake
from app.config import LANDING_HTML_PATH, SETTINGS_SERVER_HOST, SETTINGS_SERVER_PORT
from app.confirm_send import confirm_and_promote
from app.today_cuts import after_round_collected
from app.curation import cleanup_group_overrides
from app.history_renderer import generate_history_page
from app.landing_renderer import generate_landing_page
from app.logging_setup import setup_logging
from app.renderer import generate_screen, generate_waiting_page
from app.scheduler import run_due_slot, run_scheduler
from app.scraper import collect_run_with_retry
from app.settings_server import create_settings_server
from app.storage import delete_expired_runs
from app.telegram_bot_name import apply_default_bot_name_once

logger = logging.getLogger(__name__)


def _wait_for_server(host: str, port: int, timeout: float = 5.0) -> bool:
    """설정 서버가 실제로 accept 가능해질 때까지 최대 timeout초 짧게 폴링한다.

    [추가: 2026-08-19] 브라우저를 file://가 아니라 설정 서버가 서빙하는 http:// 로
    열도록 바꾸면서 생긴 경합 — 서버는 별도 데몬 스레드(serve_forever)에서
    뜨는데, 스레드 시작과 bind 완료 사이엔 시간차가 있다. 그 틈에 열면 "연결 거부"가
    뜬다. 소켓 connect 성공 여부만 보는 가장 싼 방법 — HTTP 요청까지 보낼 필요 없다."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.3):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def _open_home_in_browser() -> None:
    """홈 화면을 브라우저로 연다 (로컬 모드 전용).

    [수정: 2026-08-19] file://로 열던 것을 설정 서버가 서빙하는 http://로 바꿨다.
    index.html/history.html의 다운로드·엑셀 버튼이 루트-상대 경로(/download-text 등)를
    쓰는데, file:// 오리진에서는 그게 file:///download-text로 풀려 죽은 링크였다(정기·
    수시가 서로 다른 오리진으로 갈라져 있던 문제와 같은 근본 원인). 정기 화면끼리의 이동
    링크는 전부 상대경로(home.html 등)라 오리진을 안 가려 http로 열어도 동작은 그대로다.
    서버 스레드가 막 시작된 시점이라 bind 전에 열릴 수 있어 짧게 폴링(_wait_for_server)한
    뒤 열고, 끝내 안 뜨면 예전처럼 file://로 폴백한다 — "아무것도 안 열리는" 상황만은 피한다.

    [수정: 2026-09-02] main()의 마지막 단계에 인라인으로 있던 것을 함수로 뺐다 — 앱을
    중복 실행했을 때(포트 자물쇠에 걸린 경우) **이미 켜져 있는 앱의 창을 열어주는** 데
    같은 코드를 그대로 쓰기 위해서다. 그 경로에서는 서버가 이미 남의 프로세스에서
    떠 있으므로 _wait_for_server가 즉시 성공한다.
    """
    home_url = f"http://{SETTINGS_SERVER_HOST}:{SETTINGS_SERVER_PORT}/home.html"
    if not _wait_for_server(SETTINGS_SERVER_HOST, SETTINGS_SERVER_PORT):
        logger.warning("설정 서버가 5초 안에 뜨지 않아 임시로 파일을 직접 엽니다 — 다운로드 버튼 등 일부 기능은 안 될 수 있습니다")
        home_url = LANDING_HTML_PATH.as_uri()
    try:
        webbrowser.open(home_url)
    except webbrowser.Error:
        logger.warning(
            "브라우저를 자동으로 열지 못했습니다 — http://127.0.0.1:%s/home.html 로 직접 접속해주세요",
            SETTINGS_SERVER_PORT,
        )


def _scrape_and_render(run_slot: str, window_start: str) -> dict:
    """한 회차를 수집해 곧바로 확정본으로 반영한다(확정 대기 없음).

    스케줄러(app.scheduler)가 회차마다 scrape(run_slot, window_start) 형태로 호출한다
    (PRD.md 기능1 규칙 2 — 회차별 시간창 수집. window_start~run_slot 구간의 기사만 모음).

    [수정: 2026-08-10] 수집 즉시 완성본을 만들던 것을, 확정·전송 2단계 유예 도입으로
    "확정 대기" 상태 저장까지만 하도록 바꿨었다.
    [되돌림: 2026-08-11] 그 "확정 대기" 단계를 없애고 다시 수집 즉시 확정하도록 되돌렸다 —
    (확정)은 눌러도 확정본에서 계속 숨기기/이동/수정이 가능해 그 자체로 되돌릴 수 없는
    동작이 아닌데(진짜 되돌릴 수 없는 건 전송뿐), 그 사이 초안이 "다음 회차 미리보기"가
    아니라 "확정 대기 화면"을 보여줘서 이용자가 "왜 다음 회차로 안 넘어가지?"라고
    혼란스러워한다는 피드백 때문이다(실사용 중 실제로 발생). 전송 쪽 5분 유예와
    카운트다운은 그대로 남는다 — app.confirm_send.check_pending_confirm_and_send.
    완성본 갱신·지난 기사/진입 화면 갱신은 전부
    app.confirm_send.confirm_and_promote 안에서 함께 처리된다.
    """
    result = collect_run_with_retry(run_slot, window_start)
    confirm_and_promote(result)
    # 「✂ 오늘만 여기서 끊기」로 예약해 둔 회차였으면, 그동안 다듬은 이름표·순서·메모를
    # 이어지는 회차 초안에도 넘긴다(app.today_cuts.after_round_collected). 실패해도 회차는 이미 저장됐다.
    try:
        after_round_collected(result)
    except Exception:
        logger.exception("끊은 회차 기록을 다음 회차로 넘기지 못했습니다 — 다음 회차 초안은 새로 시작합니다")
    return result


def _apply_default_bot_name() -> None:
    try:
        apply_default_bot_name_once()
    except Exception:
        logger.exception("봇 기본 이름 걸기 실패 — 다음에 앱을 켤 때 다시 시도합니다")


def main(open_browser: bool = True) -> None:
    """앱 진입점: 켜지자마자 놓친 회차가 있으면 즉시 1회 수집한 뒤, 스케줄 루프와 설정
    저장 서버를 백그라운드로 띄우고 브라우저로 화면을 연다 (PRD.md 4절).

    open_browser: False면 브라우저를 열지 않는다(`--no-browser`, autostart/ 참고).
    [추가: 2026-09-03] 로그인할 때 launchd가 이 앱을 백그라운드로 띄우게 하면서 필요해졌다 —
    launchd는 앱이 죽으면 다시 살리는데(KeepAlive), 그때마다 브라우저 창이 새로 뜨면
    담당자가 쓰던 화면을 밀어낸다. 자동 실행은 "회차를 안 놓치는 것"만 하고, 화면을 여는
    건 담당자가 앱을 직접 실행했을 때만 한다 — 그때는 포트 자물쇠에 걸려 곧바로
    _open_home_in_browser()로 빠지므로, 손으로 켜는 행동이 그대로 "화면 열기"가 된다."""
    # [추가: 2026-08-26] 무엇보다 먼저 로깅을 켠다 — 아래 정리·수집 단계에서 나는
    # 경고·예외가 파일에 남아야 사후에 원인을 좁힐 수 있다(app/logging_setup.py).
    setup_logging()

    # [추가: 2026-09-02] **앱 중복 실행 자물쇠 — 데이터를 건드리는 어떤 단계보다 먼저 온다.**
    # 설정 서버 포트(기본 127.0.0.1:8765)를 여기서 동기로 bind해, 실패하면 "이미 앱이
    # 켜져 있다"는 뜻으로 읽고 브라우저만 그 앱으로 열어준 뒤 조용히 물러난다.
    #
    # 예전엔 이 bind가 아래 데몬 스레드 안에서 일어났다 — 두 번째로 켠 앱은 그 스레드만
    # 죽고 **스케줄러는 그대로 돌아서**, 두 프로세스가 같은 회차를 각자 수집해 같은 파일에
    # 번갈아 저장하고(2026-09-02 17:00 회차: 17:00:11 / 17:00:16 두 번 저장), 서로의 LLM
    # 분류 캐시를 통째로 덮어썼다(캐시는 프로세스 시작 때 한 번만 읽고 저장할 땐 메모리
    # 전체를 쓴다 — app.llm_classifier._ensure_cache_hydrated/_persist_cache_locked).
    # 그 결과 담당자가 초안에서 다듬어둔 소제목이 확정본에서 통째로 다른 이름으로
    # 갈아엎히고, clear_preview_order()가 두 번 불려 ↑/↓로 정리한 순서까지 사라졌다.
    # 배경은 HISTORY.md "앱 중복 실행" 참고.
    #
    # 자물쇠를 PID 파일이 아니라 포트로 잡는 이유: 앱이 강제 종료돼도 OS가 포트를 풀어줘
    # "죽은 자물쇠"가 남지 않는다. 실측(2026-09-02): HTTPServer가 SO_REUSEADDR를 켜 두지만
    # 다른 프로세스가 LISTEN 중이면 bind는 그대로 EADDRINUSE로 실패한다.
    try:
        settings_server = create_settings_server()
    except OSError as exc:
        # EADDRINUSE만 "이미 켜져 있다"로 읽는다 — 다른 bind 실패(권한 등)까지 같은
        # 메시지로 뭉뚱그리면 있지도 않은 중복 실행을 찾아 헤매게 된다. 그 경우엔 서버가
        # 어차피 못 뜨므로, 예전처럼 스레드만 조용히 죽은 채 수집만 도는 상태로 두지 않고
        # 그대로 올려 눈에 띄게 실패시킨다.
        if exc.errno != errno.EADDRINUSE:
            logger.error("설정 서버 포트를 열지 못했습니다 (%s:%s) — %s", SETTINGS_SERVER_HOST, SETTINGS_SERVER_PORT, exc)
            raise
        logger.warning(
            "이 앱이 이미 실행 중입니다 — 두 번째 창은 열지 않고 원래 창으로 보냅니다 "
            "(중복 실행하면 회차가 두 번 수집되고 초안 작업이 사라집니다)"
        )
        if open_browser and SETTINGS_SERVER_HOST == "127.0.0.1":
            _open_home_in_browser()
        return

    # 원문으로 사진 기사를 가리는 뒤쪽 작업(app.photo_body) — 자물쇠 뒤에서만 켠다. 켜기 전엔
    # looks_like_photo_caption이 원문을 받으러 가지 않아 테스트·진단 스크립트가 안전하다.
    photo_body.start()

    # 며칠 꺼뒀다 켠 경우, 정리(cleanup)는 스케줄 루프 안에서만 비동기로 도는데(scheduler.py)
    # 그걸 기다리면 아래에서 만들 "정기 보관함"이 보관 기간을 넘긴 회차까지 잠깐 보여줄 수 있다.
    # 화면을 만들기 전에 한 번 먼저 정리한다.
    try:
        delete_expired_runs()
        cleanup_group_overrides()
        delete_expired_cards()
    except Exception:
        logger.exception("시작 시 보관 기간 정리 실패 — 스케줄 루프가 이어서 재시도합니다")

    try:
        # 오늘 지나온 회차 중 아직 안 돌았으면 지금 바로 실행 (스케줄 루프의 tick을 기다리지 않음).
        # 실패해도 앱을 죽이지 않는다 — 곧 시작될 스케줄 루프의 첫 tick이 자연히 다시 시도한다.
        run_due_slot(scrape=_scrape_and_render)
    except Exception:
        logger.exception("시작 시 회차 수집 실패 — 스케줄 루프가 이어서 재시도합니다")

    try:
        # [추가: 2026-08-20] [단독]·[속보] "몰아보내기" — 앱이 밤사이 꺼져 있었을
        # 경우, 자정부터 지금까지 감시 대상 키워드를 한 번 검색해 놓친 걸 모아 보낸다.
        # 위 run_due_slot과 마찬가지로 실패해도 앱을 죽이지 않는다(다음 폴링 tick이
        # 이어서 감시한다).
        catch_up_on_wake()
    except Exception:
        logger.exception("시작 시 [단독]·[속보] 몰아보내기 실패 — 이후 폴링이 이어서 감시합니다")

    threading.Thread(
        target=run_scheduler,
        kwargs={"scrape": _scrape_and_render, "cleanup_adhoc_cards": delete_expired_cards},
        daemon=True,
    ).start()
    # 위에서 이미 bind해둔 서버 객체를 그대로 돌린다(여기서 다시 열지 않는다 — 자물쇠를
    # 놓쳤다 다시 잡는 틈이 생기면 그 사이 두 번째 앱이 끼어들 수 있다).
    threading.Thread(target=settings_server.serve_forever, daemon=True).start()
    # 이 봇에 앱이 이름을 건 적이 없으면 기본 이름을 한 번 건다(네트워크라 뒤에서).
    threading.Thread(target=_apply_default_bot_name, daemon=True).start()

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
    if open_browser and SETTINGS_SERVER_HOST == "127.0.0.1":
        _open_home_in_browser()
    elif not open_browser:
        logger.warning(
            "백그라운드 모드(--no-browser)로 실행 중입니다 — 화면은 http://%s:%s/home.html 입니다",
            SETTINGS_SERVER_HOST, SETTINGS_SERVER_PORT,
        )
    else:
        # [참고] 화면(터미널)에는 WARNING 이상만 찍는다(app/logging_setup.py의
        # CONSOLE_LOG_LEVEL) — 이 안내는 반드시 눈에 띄어야 하므로 info가 아니라
        # warning으로 남긴다(실제로는 정상 상태 안내). 파일에는 INFO까지 남는다.
        logger.warning(
            "원격 서버 모드(SERVER_HOST=%s)입니다 — 다른 컴퓨터에서 http://%s:%s/home.html 로 접속하세요",
            SETTINGS_SERVER_HOST, SETTINGS_SERVER_HOST, SETTINGS_SERVER_PORT,
        )

    threading.Event().wait()  # 데몬 스레드가 계속 돌도록 메인 스레드를 대기시킨다 (Ctrl+C로 종료)


if __name__ == "__main__":
    # 인자는 이 하나뿐이라 argparse를 들이지 않는다 — 있으면 브라우저를 안 연다.
    main(open_browser="--no-browser" not in sys.argv[1:])
