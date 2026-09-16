# Design Ref: CLAUDE.md 작업 절차(검증 루프) — 사후 진단에 필요한 실행 기록을 남긴다.
"""앱 전체 로깅 설정 — 화면(stderr)과 파일 양쪽에 남긴다.

**왜 파일이 필요한가**: 이 앱은 지금까지 `logging.basicConfig`을 아예 설정하지 않아
기본 lastResort 핸들러가 WARNING 이상만 화면에 찍고 그대로 사라졌다. 그 결과
"몇 시간 전 그 회차가 왜 그렇게 저장됐나"를 사후에 확인할 방법이 없었다 —
2026-08-26에 두 건을 연달아 겪었다:

  1. "기타" 재정리(`app.llm_classifier._refine_etc_bucket`)가 실패했는지 여부를
     확인할 수 없어, 09:30·11:00 회차의 높은 "기타" 비율 원인을 못 좁혔다.
  2. 어제 19:30 회차가 오늘 날짜 파일(`2026-08-26_19-30.json`)로 저장된 걸 발견했는데,
     자정을 어떻게 넘겨 저장됐는지 추적할 기록이 없었다.

두 경우 모두 코드에는 이미 `logger.warning`/`logger.exception`이 있었다 — 남을 곳이
없었을 뿐이다. 그래서 새 로그를 추가하는 것보다 **남는 곳을 만드는 것**이 먼저다.

**설계 결정**

- *화면은 지금 그대로 WARNING 이상*. 담당자가 보는 터미널을 INFO로 채우면 정작
  경고가 묻힌다. 자세한 기록은 파일에만 남긴다(INFO 이상).
- *하루 단위 회전, `LOG_RETENTION_DAYS`(14일) 보관*. 회차 파일은 1년이지만 로그는
  성격이 다르다 — "작년 이맘때"를 되짚는 자산이 아니라 최근 사고를 파는 도구라,
  실제로 쓰이는 구간은 길어야 2주다(위 두 사건 모두 당일~이틀 안이었다).
  무한정 쌓게 두면 `data/` 폴더만 조용히 커진다.
- *`delay=True`*. 실제로 로그가 한 줄 나기 전까지 파일을 만들지 않는다 — 통합 테스트나
  단순 import만으로 빈 로그 파일이 생기지 않게.
- *예외를 밖으로 안 낸다*. 로그 디렉터리를 못 만드는 상황(권한 등)에서 앱이 못 뜨면
  안 된다 — 이 앱의 "부가 기능은 절대 본업을 멈추지 않는다" 원칙과 같다.
  파일 로깅만 조용히 포기하고 화면 로깅은 그대로 살린다.
"""
import logging
import logging.handlers

from app.config import CONSOLE_LOG_LEVEL, FILE_LOG_LEVEL, LOG_FILE, LOG_RETENTION_DAYS

_configured = False

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def setup_logging() -> None:
    """루트 로거에 화면·파일 핸들러를 단다. 여러 번 불러도 한 번만 적용된다."""
    global _configured
    if _configured:
        return
    _configured = True

    root = logging.getLogger()
    # 루트는 두 핸들러 중 낮은 쪽까지 통과시키고, 실제 문턱은 핸들러별로 건다.
    root.setLevel(min(CONSOLE_LOG_LEVEL, FILE_LOG_LEVEL))

    formatter = logging.Formatter(_FORMAT, datefmt=_DATEFMT)

    console = logging.StreamHandler()
    console.setLevel(CONSOLE_LOG_LEVEL)
    console.setFormatter(formatter)
    root.addHandler(console)

    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.TimedRotatingFileHandler(
            LOG_FILE,
            when="midnight",
            backupCount=LOG_RETENTION_DAYS,
            encoding="utf-8",
            delay=True,
        )
        file_handler.setLevel(FILE_LOG_LEVEL)
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError:
        # 파일 로깅만 포기하고 계속 간다 — 위 docstring "예외를 밖으로 안 낸다" 참고.
        root.warning("로그 파일을 열지 못해 화면에만 기록합니다 (경로: %s)", LOG_FILE, exc_info=True)
        return

    # 파일 첫 줄에 "언제 켰는지"를 남긴다 — 회전된 파일을 나중에 열었을 때
    # 그날 앱이 몇 번 재시작했는지가 바로 보인다(스케줄러 따라잡기 판단에 필요).
    root.info("앱 로깅 시작 — 화면 %s / 파일 %s (%s, %d일 보관)",
              logging.getLevelName(CONSOLE_LOG_LEVEL), logging.getLevelName(FILE_LOG_LEVEL),
              LOG_FILE, LOG_RETENTION_DAYS)
