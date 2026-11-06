"""화면 파일 셋(`index.html`·`history.html`·`home.html`)을 **뒤에서 한 번만** 다시 쓴다.

**왜 있나.** 큐레이션 클릭(기사 숨기기·소제목 이동)은 저장을 끝낸 뒤 화면 셋을 다시
써 두고 나서야 응답했다. 그런데 **담당자가 보는 화면은 그 파일을 안 읽는다** — 요청마다
새로 그린 글을 그대로 응답하고(`app/settings_server.py` `_SCREEN_HTML_ROUTES`), 앱도
`http://`로 연다(`main.py` — `file://`은 서버가 5초 안에 안 뜰 때의 폴백이다). 그 파일은
**`file://`로 열어 둔 탭**을 위한 보험인데, 그 보험료를 담당자가 가장 많이 누르는 동작에서
매번 치르고 있었다.

**실측 2026-11-06**(회차 402개·기사 112건, 왕복 횟수를 셈): 기사 한 건 숨기기가 **93번**
이고 그중 **86번(92%)이 화면 셋을 다시 쓰는 것**이다. 빼면 **7번**이다(DB에선 1.5초 → 0.1초).

**그래서 파일을 그만 쓰는 게 아니라, 기다리지 않는다.** 맡겨 두고 바로 응답하면
`file://` 탭의 약속은 그대로 지켜진다(1초쯤 뒤에 최신이 된다).

**접을 때 맡긴 일을 흘려보낸다.** 앱에는 정상 종료 자리가 없고(LaunchAgent가 그냥
죽인다) 이 스레드는 데몬이라, **마지막 클릭 뒤 1초 안에 앱이 꺼지면 그 한 번의 쓰기가
빠진다.** 다음 클릭이 다시 쓰므로 어긋남은 거기서 끝난다 — 종료 자리를 만들어 기다리게
하는 것보다 이쪽이 싸다(그 파일을 읽는 것은 `file://` 탭 하나다).

**여러 번 눌러도 한 번만 쓴다.** 쓰는 동안 들어온 요청은 「다시 써야 한다」는 표시 하나로
모이므로, 열 번 연달아 눌러도 쓰는 일은 최대 두 번이다(지금 도는 한 번 + 그 뒤 한 번).

**누구의 화면인가는 맡길 때의 사람을 들고 간다**(`store.current_owner`). 뒤에서 도는
스레드는 소유자를 물려받지 않으므로(「스케줄러」 절과 같은 이유), 안 걸면 바닥 주인의
회차로 남의 화면을 덮는다. 파일이 한 벌이라 **마지막에 맡긴 사람의 화면이 남는다** —
그건 지금도 그렇다(요청이 차례로 같은 파일을 덮었다).
"""
import logging
import threading

from app import store
from app.history_renderer import generate_history_page
from app.landing_renderer import generate_landing_page
from app.renderer import generate_screen

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_wake = threading.Event()      # 할 일이 생겼다
_idle = threading.Event()      # 맡긴 일이 없다(테스트가 기다리는 자리)
_idle.set()
_pending = False
_owner = None
_thread = None


def _write_all(owner) -> None:
    """설정 서버가 예전에 응답 전에 하던 그 일 — 순서까지 같다."""
    with store.owner(owner):
        try:
            generate_screen()
        except RuntimeError:
            # 저장된 회차가 하나도 없을 때 — 지난 기사·진입 화면은 그래도 다시 만든다.
            pass
        generate_history_page()
        generate_landing_page()


def _loop() -> None:
    while True:
        _wake.wait()
        with _lock:
            _wake.clear()
            if not _pending:
                _idle.set()
                continue
            owner = _owner
            _clear_pending()
        try:
            _write_all(owner)
        except Exception:
            # **여기서 죽으면 그 뒤 모든 클릭의 화면 파일이 조용히 멈춘다.**
            logger.warning("화면 파일을 다시 쓰지 못했습니다 — 다음 클릭에 다시 해 봅니다", exc_info=True)
        with _lock:
            if not _pending:
                _idle.set()


def _clear_pending() -> None:
    global _pending
    _pending = False


def request_refresh() -> None:
    """화면 셋을 다시 쓰라고 맡긴다 — **부른 쪽은 기다리지 않는다.**

    맡기는 값은 표시 하나와 「누가 맡겼나」뿐이다. 스레드는 처음 맡길 때 뜨고
    (중복 실행 자물쇠 뒤라 요청이 들어온 뒤다) 그 뒤로는 계속 돈다.
    """
    global _pending, _owner, _thread
    with _lock:
        _pending = True
        _owner = store.current_owner()
        _idle.clear()
        if _thread is None or not _thread.is_alive():
            _thread = threading.Thread(target=_loop, name="screen-files", daemon=True)
            _thread.start()
    _wake.set()


def wait_idle(timeout: float = 10.0) -> bool:
    """맡긴 일이 끝날 때까지 기다린다 — **테스트와 앱을 접을 때만** 쓴다."""
    return _idle.wait(timeout)
