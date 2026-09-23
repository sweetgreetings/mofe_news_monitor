# Design Ref: archive/DESIGN.md §3 기술 선택 — 서버 프레임워크 없이 .env에서 네이버 API 키를 불러오는 설정 모듈
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

# [수정: 2026-08-20] 앱을 통째로 막던 RuntimeError를 없앴다 — 배포본을 받은 사람마다
# 자기 키를 설정 화면(/naver)에서 등록할 수 있게 하려면, 키가 없는 상태로도 일단
# 앱은 켜져야 한다(그래야 그 화면을 열 수 있다). 이 상수는 이제 "설정에 저장된 값이
# 없을 때의 폴백"일 뿐이고, 실제 우선순위 판단(설정 > .env)은 app.credentials가
# 맡는다. 키가 정말 하나도 없는 상태는 app.credentials.naver_is_configured()로
# 화면·스케줄러가 각자 감지해 안내한다(app.naver_api.NaverNotConfiguredError).
NAVER_CLIENT_ID = os.getenv("NAVER_CLIENT_ID")
NAVER_CLIENT_SECRET = os.getenv("NAVER_CLIENT_SECRET")

# [추가: 2026-08-03] 텔레그램 자동/수동 전송 기능용 — 네이버 키와 달리 이 기능 자체가
# 선택 사항(기본 꺼짐)이라, 없어도 앱 실행을 막지 않는다(app.telegram_bot.is_configured가
# 미설정을 감지해 전송만 조용히 건너뛴다).
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# [추가: 2026-08-06] 이메일 전송 기능용 — 텔레그램과 같은 이유로 선택 사항(기본 꺼짐)이라
# 없어도 앱 실행을 막지 않는다(app.email_sender.is_configured가 미설정을 감지해 전송만
# 조용히 건너뛴다). SMTP 호스트/포트를 고정하지 않고 .env에서 읽어, 네이버 메일·구글
# 메일·회사 메일 서버 등 어떤 SMTP든 쓸 수 있게 했다(포트 기본값만 흔한 587로 둠).
EMAIL_SMTP_HOST = os.getenv("EMAIL_SMTP_HOST")
EMAIL_SMTP_PORT = int(os.getenv("EMAIL_SMTP_PORT", "587"))
EMAIL_SENDER_ADDRESS = os.getenv("EMAIL_SENDER_ADDRESS")
EMAIL_SENDER_PASSWORD = os.getenv("EMAIL_SENDER_PASSWORD")
# [추가: 2026-09-18] 설정 화면(연동 › 이메일 보내는 계정)에서 고를 수 있는 메일 서비스.
# 「직접 입력」은 두지 않는다 — 기관 메일(korea.kr 등)은 외부 앱의 SMTP 발송을 막는 경우가
# 많다(사용자 결정). .env에 다른 서버를 적어 둔 경우만 그 값을 그대로 쓴다.
# domain이 있으면 보내는 사람 주소가 그 도메인이어야 한다(Gmail은 회사 도메인 계정도 있어 비움).
EMAIL_SERVICES = {
    "naver": {"label": "네이버", "host": "smtp.naver.com", "port": 587, "domain": "naver.com"},
    "gmail": {"label": "Gmail", "host": "smtp.gmail.com", "port": 587, "domain": ""},
}

# PRD.md 기능1 규칙 2 — [수정: 2026-07-25] 키워드 그룹 기능 도입. [수정: 2026-07-25]
# 처음엔 "기관 정보" 그룹을 삭제·수정 불가로 고정했었는데, "이용자 소속 기관이 바뀔 수도
# 있다"는 피드백으로 다른 그룹과 완전히 동등하게(이름·키워드·OR/AND 수정, 그룹 자체
# 삭제까지) 바꿨다. BASE_GROUP_NAME/KEYWORDS는 이제 "최초 설치 시 한 번만 심어주는
# 기본값"일 뿐, 이후로는 그냥 사용자가 저장한 keyword_groups의 평범한 항목 중 하나다
# (app.settings.load_settings의 1회성 마이그레이션 참고). 그룹 간에는 항상 OR(아무
# 그룹이나 조건을 만족하면 채택)이고, 그룹 안에서는 그 그룹의 OR/AND를 따른다.
BASE_GROUP_NAME = "기관 정보 (기본)"
BASE_GROUP_KEYWORDS = ["재정경제부", "재경부", "기획재정부", "기재부", "정부", "구윤철", "이형일"]
DEFAULT_KEYWORD_GROUP_NAME = "키워드 그룹"
# [수정: 2026-07-25] 그룹 자체가 더는 "고정 1개 + 사용자 5개"가 아니라 전부 동등한
# 6개(예전 상한 그대로 유지)라, 그룹당 키워드 상한도 기존 기관 정보 그룹의 키워드
# 개수(7개)를 그대로 수용하도록 5 -> 7로 올렸다(잘라내는 손실 없이 마이그레이션되도록).
# [수정: 2026-08-13] 그룹/키워드 상한을 늘리고 싶다는 요청으로 재검토했다. 예전엔
# "늘리면 실시간 현황이 버거워진다"는 감으로 6·7에 묶어뒀는데, 실측해보니 진짜
# 병목은 개수가 아니라 동시성이었다(app.naver_api._MAX_CONCURRENT_KEYWORD_SEARCHES
# 참고 — 100키워드·동시성 8에서 429 0%를 2회 재확인). 게다가 캐시가 키워드 단위
# 증분으로 바뀌고(app.live_cache) 콜드 스타트도 백그라운드+대기화면으로 가려져서
# (app.live_renderer.generate_live_page_or_wait), 상한을 올려도 사용자가 그 비용을
# 체감할 일이 없다. 8×10=80은 실측한 100보다 20% 낮아 여유가 있고, 딱 떨어지는
# 숫자라 설정 화면에서 관리하기도 더 쉽다.
MAX_KEYWORD_GROUPS = 8
MAX_KEYWORDS_PER_GROUP = 10

# [추가: 2026-08-13] /outlets 화면 언론사 순서 버튼("5개 ↑"/"5개 ↓")이 한 번에 옮기는
# 칸수 — 실측(선택 언론사 41개 기준) "맨 아래→맨 위" 이동이 1칸(40회)에서 8회로 줄어드는
# 값을 사용자가 직접 골랐다. 목표 지점까지 남은 칸이 이보다 적으면 move_outlet이
# 알아서 끝까지만 옮긴다(예전의 "맨 위"/"맨 아래" 버튼 역할을 겸함 — 별도 버튼 불필요).
OUTLET_ORDER_JUMP_STEP = 5

# classifier.py·renderer.py·summarizer.py가 keywords 인자 없이 직접 호출될 때(주로 낮은
# 단계 호출·테스트)만 쓰는 기본값 — 설정 화면 저장값과는 무관하다.
DEFAULT_KEYWORDS = list(BASE_GROUP_KEYWORDS)

# PRD.md 기능1 규칙 1·2 — 자동 실행 시각의 기본값(설정 화면에서 바꾸기 전 최초 상태).
# [수정: 2026-07-24] 회차별 시간창 수집 — 각 회차가 "시작~끝" 구간을 직접 갖는다(그
# 구간에 게시된 기사만 수집). 화면 헤더·저장 파일명은 end를 그대로 쓴다("언론 모니터링
# 09:00 기준"). "HH:MM"은 반드시 24시간제 제로패딩 형식이어야 한다(예: "9:00"이 아니라
# "09:00") — 문자열 사전식 비교로 시각 순서를 정하므로 패딩이 깨지면 순서가 어긋난다.
SCHEDULE_TIMES = [
    {"start": "00:00", "end": "09:00"},
    {"start": "09:00", "end": "10:30"},
    {"start": "10:30", "end": "13:30"},
    {"start": "13:30", "end": "16:30"},
]
# PRD.md 기능1 규칙 20 — [추가: 2026-07-24] 설정 화면에서 등록 가능한 시각 개수 범위.
# 너무 잦으면(예: 5분 간격) 거의 같은 내용이 계속 새 회차로 쌓여 "지난 기사" 목록만
# 어수선해지므로, 업무 시간 내 촘촘히 잡아도(1~2시간 간격) 충분한 상한으로 10개를 둔다.
MIN_SCHEDULE_TIMES = 1
MAX_SCHEDULE_TIMES = 10
# [추가: 2026-07-29, 수정: 2026-08-10] 스크랩 시간대 "그룹" — 주중/주말처럼 상황별로
# 여러 시간대 세트를 만들어둔다. 그룹 자체의 개수 상한이지, 그룹 안 시간대 개수(위
# MIN/MAX_SCHEDULE_TIMES)와는 별개다. [수정: 2026-08-10] 그룹마다 독립적인 "사용"
# on/off와 요일 체크를 두는 방식으로 바뀌었다 — 공휴일처럼 평일인데 주말 스케줄을 써야
# 할 때, 새 그룹을 만들어 그것만 "사용"을 켜고 나머지를 꺼두면 요일 체크와 무관하게
# 그 그룹이 그대로 적용된다(app.settings.pick_active_group_index). 상세 규칙은
# app.settings.validate_schedule_groups 참고.
MAX_SCHEDULE_GROUPS = 5
DEFAULT_SCHEDULE_GROUP_NAME = "기본"
# [추가: 2026-08-10] 요일 체크박스 값(영문 키, 폼 필드/저장 데이터 공용)과 화면 표시용
# 한글 라벨. 순서(월~일)가 화면에 그대로 쓰인다.
WEEKDAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
WEEKDAY_LABELS_KO = {"mon": "월", "tue": "화", "wed": "수", "thu": "목", "fri": "금", "sat": "토", "sun": "일"}

# PRD.md 7절 — 수집 데이터 보관 기간
# [수정: 2026-08-18] 7 -> 365. "작년 이맘때 이 사안이 어떻게 다뤄졌나"를 되짚어야 한다는
# 요구가 실제로 있었다. 용량은 논점이 아니다 — 실측(현재 파일을 1년치로 복제):
# 정기 1,460개 22.8MB + 수시 365카드 13.7MB = 40MB 미만. 진짜 병목이던 "매번 전 회차를
# 파싱하던 자리" 여섯 곳은 app/run_index.py 도입으로 먼저 정리했다(HISTORY.md 참고) —
# 그 정리 없이 이 값만 올리면 담당자가 🗑️를 누를 때마다 22.8MB를 읽게 된다.
RETENTION_DAYS = 365

# [추가: 2026-08-18] 수시 모니터링 카드(data/adhoc/cards/)도 정기와 같은 1년으로 맞춘다
# (PRD.md 기능9 규칙9 — "두 보관함의 정책이 다르면 담당자가 어느 쪽이 언제까지 남는지
# 따로 기억해야 한다"). 값은 RETENTION_DAYS와 같지만, 도메인이 달라(회차 파일 vs 카드
# 파일) 상수는 분리해 둔다 — 나중에 둘을 다시 벌릴 일이 생겨도 한쪽만 바꾸면 된다.
ADHOC_RETENTION_DAYS = 365

# [추가: 2026-08-25] 소제목 강제 배정 기록(data/group_overrides.json)의 보관 기간 —
# 위 두 상수(회차 파일·수시 카드 = 1년)와 **일부러 다르다**. 2026-08-21에 이 정리 기준을
# "지금 살아있는 기사 URL에 없으면 삭제"에서 "나이"로 바꾸면서 RETENTION_DAYS(365)를
# 그대로 빌려 썼는데, 그건 성격이 다른 값을 재사용한 것이었다:
#   - 회차 파일 1년: "작년 이맘때 이 사안이 어떻게 다뤄졌나"를 되짚기 위한 **열람용 자산**.
#   - 배정 기록: "초안에서 옮긴 걸 확정될 때까지 지킨다"가 전부인 **작업 중 임시 기록**.
#     회차가 확정되는 순간 그 배정 결과는 회차 파일의 "group" 필드에 스냅샷되므로
#     (app.classifier.snapshot_group_names), 그 뒤로 이 기록을 읽을 일이 사실상 없다.
#     지켜야 하는 구간이 몇 시간인데 1년을 들고 있을 이유가 없었다.
# 실측(2026-08-25)으로 확인한 실제 비용은 파일 크기가 아니라 **되돌리기 스택 증폭**이다 —
# app/undo.py가 이 파일을 최대 20단계까지 통째로 복사하므로, 1년치(약 20,600건 · 3MB)로
# 자라면 undo_stack.json이 65MB가 되고 큐레이션 클릭 1회당 비용이 5ms -> 250ms(50배)로
# 뛴다(숨기기·이동 버튼을 누를 때마다 그 65MB를 다시 쓴다). 30일이면 위 "확정될 때까지
# 지킨다"는 목적에는 차고 넘치면서 이 증폭이 통째로 사라진다.
GROUP_OVERRIDE_RETENTION_DAYS = 30

# [추가: 2026-08-12] "지난 기사"(history.html) 화면 용량 다이어트 — 최근 EAGER_HISTORY_DAYS
# 일치만 페이지에 전체 내용을 바로 담고, 그보다 오래된 날짜는 펼칠 때 서버에서 그때
# 불러온다(app.history_renderer). 실측: 7일 전부를 한 파일에 담으면 3.4MB(기사 카드
# 1,037개) — 대부분의 조회가 최근 날짜에 몰리는데도 매번 전부 내려받는 게 낭비였다.
EAGER_HISTORY_DAYS = 2

# PRD.md 기능1 규칙 8 — 스크랩 실패 시 재시도 정책 (5분 간격, 최대 3회 재시도)
MAX_RETRIES = 3
RETRY_INTERVAL_SEC = 300

# [추가: 2026-08-10, 수정: 2026-08-10] 확정·전송 유예 시간(초) — 회차가 수집되면 곧바로
# 완성본이 되지 않고 초안에 "확정 대기" 상태로 잠깐 머무른다(담당자가 검토 후 (확정)
# 클릭). 담당자가 있을 땐 확정과 전송을 각자 원하는 때에 따로 누르면 되고, 수집 시각
# 기준 이 시간이 지나도록 손을 안 대면(부재중) 그 시점에 확정과 전송이 한꺼번에 자동
# 처리된다(app.confirm_send.check_pending_confirm_and_send) — "바빠서 미처 확인 못한
# 채로 검토 없이 나가는 것"을 막기 위한 안전장치(사용자 요청)이면서도, 손 놓고 있을 때
# 확정→전송이 각각 5분씩 두 번 늘어지지 않게(총 5분) 한 것.
CONFIRM_SEND_GRACE_SEC = 300

# [추가: 2026-08-11] 자동발송 사용 여부와 유예 시간(분)을 설정 화면(/auto-send)에서 바꾼다.
# 위 CONFIRM_SEND_GRACE_SEC는 이제 "설정이 아직 없을 때의 기본값"으로만 쓰인다.
#
# 배경: `/telegram`·`/email` 화면에 "정기 스크랩 완료 시 자동 전송" 체크박스가 각각 있었는데,
# 오늘 확정 단계를 걷어내며 발송 경로가 바뀌면서 **아무도 그 값을 읽지 않는 고아 설정**이
# 됐다(체크를 풀어놔도 유예가 지나면 그냥 나갔다). 실제로 2026-08-11 17시 회차가 소제목이
# 망가진 채로 자동발송된 뒤에 발견됐다. 개별 Telegram/Email 발송 버튼을 (발송) 하나로 합친
# 것과 같은 이유로, 자동발송도 채널별로 쪼개지 않고 하나로 통합해 되살린다.
#
# 유예 상한 이유: app.confirm_send.check_pending_confirm_and_send가 load_latest_run()
# 하나만 검사하므로, 유예가 회차 간격보다 길면 다음 회차가 수집되는 순간 이전 회차가
# 조용히 발송되지 않은 채 묻힌다. [수정: 2026-08-12] 60 → 30 — app.settings.
# validate_schedule_windows가 수집 시간을 30분 단위로만 지정하게 강제하면서(사용자 요청),
# 회차 사이 최소 간격도 이제 30분으로 줄어들 수 있다. 유예 상한을 그 최소 간격과 같은
# 30분으로 맞춰, 아무리 촘촘하게 회차를 잡아도 유예가 다음 회차를 잡아먹지 않게 한다.
DEFAULT_AUTO_SEND_ENABLED = True
DEFAULT_AUTO_SEND_GRACE_MIN = 5
MAX_AUTO_SEND_GRACE_MIN = 30

# PRD.md 디자인 방향 — [수정: 2026-07-24] 어두운 남색 배경에 흰 글자(구버전)가 링크 등에서
# 가독성이 떨어진다는 피드백으로, 밝은 배경 + 카드 구조의 라이트 테마로 전환.
COLOR_BG = "#F8FAFC"
COLOR_CARD = "#FFFFFF"
COLOR_HEADER = "#1E3A5F"
COLOR_ACCENT = "#2563EB"
COLOR_TEXT = "#1F2937"
COLOR_TEXT_MUTED = "#6B7280"
# [수정: 2026-08-27] 정책 단어 추이 선 색 — 예전엔 COLOR_HEADER/COLOR_ACCENT/회색 3개를
# 재사용했는데, 전용 화면(/trend)에서 최대 8개 단어를 동시에 비교해야 하면서 그 3색으로는
# CVD(색각 이상) 검증을 통과하지 못했다(dataviz 스킬 validate_palette.js — 1번째는 명도가
# 밴드 밖, 3번째는 채도가 바닥이라 회색으로 읽힘). 8개 categorical 슬롯 모두 검증 통과한
# 램프로 교체. [수정: 2026-09-15] 홈 카드도 이제 전부 쓴다(예전엔 앞 3개만). 같은 단어가 홈과
# 전용 화면에서 항상 같은 색이 되도록 슬롯 순서를 공유한다(HISTORY.md "홈 화면 재구성" 참고).
COLOR_TREND_1 = "#2a78d6"  # 파랑
COLOR_TREND_2 = "#eb6834"  # 주황
COLOR_TREND_3 = "#1baf7a"  # 아쿠아
COLOR_TREND_4 = "#eda100"  # 노랑
COLOR_TREND_5 = "#e87ba4"  # 자홍
COLOR_TREND_6 = "#008300"  # 초록
COLOR_TREND_7 = "#4a3aa7"  # 보라
COLOR_TREND_8 = "#e34948"  # 빨강
COLOR_BORDER = "#E5E7EB"
COLOR_HOVER = "#EFF6FF"
COLOR_ERROR = "#DC2626"  # 밝은 배경에서도 잘 보이도록 새로 정한 오류 문구 색상
# [추가: 2026-07-25] 실시간 기사 현황 배지·버튼의 연한 배경. 글자·테두리는 기존 COLOR_ERROR를
# 그대로 재사용한다(새 빨간색을 따로 만들지 않음) — "지금 실시간"이라는 느낌만 옅게 준다.
COLOR_LIVE_BG = "#FEF2F2"

# ---------------------------------------------------------------------------
# [추가: 2026-08-21] 화면 색 팔레트 — 앱 전체의 색 값은 전부 여기 있다.
#
# 그 전에는 113개 색 중 위의 10개(COLOR_BG~COLOR_LIVE_BG)와 형광펜 5색만 이름이
# 있었고, 나머지 98개는 렌더러 8개 파일의 CSS 문자열 안에 직접 박혀 있었다. 같은
# 뜻의 색이 파일마다 따로 적혀 있어(예: [단독] 제목색 #B91C1C가 네 파일에 네 번),
# 한 화면에서 톤을 바꾸면 나머지 화면은 옛 색으로 남는 구조였다. 그래서 색 값은
# 전부 이 파일로 모으고, 각 렌더러는 아래 PALETTE를 `.format(**PALETTE, ...)`로
# 받아 쓰기만 한다.
#
# 이름은 색 이름(파랑·노랑)이 아니라 **뜻**으로 붙인다 — 나중에 톤을 바꿔도 이름이
# 거짓말을 하지 않게. 지금 값이 같아도 뜻이 다르면 이름을 따로 둔다(예: 발송 버튼과
# [속보] 제목색은 둘 다 #15803D지만 서로 다른 규칙이라, 한쪽만 바꿔야 할 때 이름이
# 갈라져 있어야 나머지가 딸려가지 않는다). 지금 겹치는 자리는 각 항목 주석에 적어뒀다.
# ---------------------------------------------------------------------------

# ── 채운 버튼·배지 위에 얹는 글자 ─────────────────────────────────────────
# 카드 면(COLOR_CARD)과 값은 같지만 뜻이 다르다 — 이쪽은 "진한 바탕 위 글자"다.
COLOR_ON_FILL = "#FFFFFF"

# ── 파랑 = 담당자가 하는 일 (복사·내보내기·만들기·설정) ───────────────────
COLOR_ACCENT_PRESSED = "#1D4ED8"        # 파란 버튼을 눌렀을 때
# [수정: 2026-08-21] 파랑 연한 톤 8종을 값 기준 3종(soft 배경 / line 테두리 / line-hover
# 테두리)으로 모았다. 이름은 그대로 뒀다 — 화면마다 "톤온톤 hover"·"켜진 칩 배경"·
# "패널 테두리" 등 쓰임은 계속 갈라 부르되, 실제 화면에 나타나는 파랑은 이제 몇 종뿐이다.
COLOR_ACCENT_TONAL = COLOR_HOVER        # 툴바 버튼 톤온톤 hover
COLOR_ACCENT_CHIP_BG = COLOR_HOVER      # 켜진 키워드 칩 배경
COLOR_ACCENT_CHIP_HOVER = COLOR_HOVER   # 켜진 칩을 다시 hover
COLOR_ACCENT_BORDER = "#BFDBFE"         # 파란 계열 버튼·칩 테두리 — 나머지 연한 파랑들의 기준값
COLOR_ACCENT_BORDER_SOFT = COLOR_ACCENT_BORDER  # 접히는 편집 패널 테두리
COLOR_ACCENT_PANEL_BG = COLOR_CARD      # 그 편집 패널 안쪽 바탕
COLOR_GHOST_BORDER = COLOR_ACCENT_BORDER  # 고스트(테두리만) 버튼
COLOR_GHOST_BORDER_HOVER = "#A9C6F5"    # 고스트 버튼 hover — 유일하게 남긴 "더 진한" 변형(hover 피드백용)
COLOR_HEADER_PRESSED = "#16304D"        # 남색 버튼(배너의 "열기")을 눌렀을 때

# ── 연보라 = AI가 하는 일 (🤖 재분류·미분류 배정) ─────────────────────────
# 일부러 꽉 찬 색을 안 쓴다 — 누르면 API 비용이 나가므로 "눌러야 할 것"처럼 보이면 안 된다.
COLOR_AI_BG = "#F0EAFB"
COLOR_AI_BG_HOVER = "#E7DBFA"
COLOR_AI_TEXT = "#4A3F8F"
COLOR_AI_TEXT_STRONG = "#372C6E"        # AI 배너 안 강조 글자
COLOR_AI_TEXT_MUTED = "#7C6FA8"         # AI 배너 닫기 버튼
COLOR_AI_BORDER = "#DCD0F2"
COLOR_AI_BORDER_HOVER = "#CBB8ED"
COLOR_AI_MARK = "#A78BFA"               # 확정 후 추가된 기사 왼쪽 띠
COLOR_AI_BANNER_BG = "#F4EFFC"          # 그 안내 배너 바탕

# ── 기사 한 줄의 상태 ─────────────────────────────────────────────────────
COLOR_ROW_HOVER = "#F3F4F6"             # 마우스 올린 행 (모든 화면 공통 중립 회색)
COLOR_ROW_MOVED = "#E7EFE8"             # 방금 옮긴 기사·소제목 (세이지)
COLOR_ROW_NEW = "#FFF9C4"               # 지난번 봤을 때 없던 새 기사
# [수정: 2026-09-04] 담아둔 기사를 이 화면으로 올렸을 때 — COLOR_ROW_NEW(노랑)에서
# COLOR_ROW_MOVED(세이지)로 옮겼다. 노랑은 "이 화면에서 처음 보는 기사"인데,
# 📌 담아둔 기사는 승격 전부터 같은 화면 아래 구획에 떠 있어(승격 드롭다운이 그 구획
# 안에 있으니 물리적으로 안 볼 수가 없다) 담당자에게 새 기사가 아니다. 실제로
# is-new-arrival 기준선이 .article[data-url] 전부를 담고 담아둔 기사 구획도 거기
# 포함되므로, 앱 자신도 승격된 기사를 "새 도착 아님"으로 판정한다 — 그 판정과
# 어긋나는 색을 칠하고 있었다. 승격은 "내가 방금 만진 결과가 어디 갔나"라서
# ↑↓·소제목 이동과 같은 축이다. 배경은 HISTORY.md "담아둔 기사 승격 색" 참고.
COLOR_ROW_PROMOTED = COLOR_ROW_MOVED    # 담아둔 기사를 이 화면으로 올렸을 때(값은 이동과 같음)
COLOR_PILL_BG = "#F3F4F6"               # 회색 알약 배지 바탕(행 hover와 값은 같음)

# ── 말머리 강조 ([단독]/[속보]) ───────────────────────────────────────────
COLOR_SCOOP_TEXT = "#B91C1C"            # [단독] 제목 글자
COLOR_SCOOP_BG = "#FEF2F2"              # [단독] 기사 행 바탕 (COLOR_LIVE_BG와 값 같음)
COLOR_SCOOP_BAR = COLOR_ERROR           # [단독] 기사 행 왼쪽 띠
COLOR_FLASH_TEXT = COLOR_SCOOP_TEXT     # [수정: 2026-08-21] 초록→빨강. [단독]과 같은 "말머리" 축인데
                                        # 색이 정반대였다 — 이제 [단독]과 같은 값을 쓴다(강조 정도만 다름).
COLOR_UNDATED_BAR = "#EF9F27"           # 발행시각을 못 읽은 기사 왼쪽 띠

# ── 발송 ──────────────────────────────────────────────────────────────────
# [수정: 2026-08-10] 원래 밝은 연두(#5EC26A)가 "임시" 느낌이 난다는 피드백 —
# 관공서 톤에 맞는 진한 그린(신뢰감·중후함)으로 교체.
COLOR_SEND = "#15803D"

# ── 실시간 현황 행 배지 ────────────────────────────────────────────────
# [수정: 2026-08-21] ✔️ 스크랩됨은 파랑(accent)으로, 🤖 자동 선별은 연보라(ai_*)로
# 옮겨서 이 절에서 빠졌다 — 둘 다 이 화면 전용 색이 아니라 앱 전체가 쓰는 팔레트를
# 그대로 참조한다(스크랩됨=담당자가 확정한 결과, 자동 선별=AI가 한 일). 남은 건
# "아직 결정 안 됨"이라는, 정말 이 화면에만 있는 상태 하나뿐이다.
COLOR_PIN_GRAY = "#9AA4B2"              # 📌 담아둠 ("아직 결정 안 됨")
COLOR_PIN_GRAY_BG = "#F3F5F7"
# 초안·확정본의 📌 담아둔 기사 칸 (캐러멜). 미분류 앰버(= 담당자가 아직 모르는 것)를 한 단계
# 눌러 어둡게 한 색 — 같은 계열 안에서 "한 번은 보고 골라둔 것"이라는 다음 단계로 읽힌다.
COLOR_PINNED_BAR = "#B7803F"            # 칸 왼쪽 띠
COLOR_PINNED_BG = "#FAF3EA"             # 칸 바탕
COLOR_PINNED_TEXT = "#7A4E1C"           # 건수 칩·제목 옆 뱃지 글자, 칸 안 버튼 글자
COLOR_PINNED_CHIP_BG = "#F1E3D0"        # 건수 칩·뱃지 바탕
COLOR_PINNED_BORDER = "#DFC5A3"         # 건수 칩·뱃지·칸 안 버튼 테두리

# ── 어느 흐름의 화면인가 (홈 메뉴 카드) ───────────────────────────────────
# 보관함은 같은 색을 한 단계 흐리게 해서 "같은 흐름의 과거"임을 표시한다.
COLOR_ARCHIVE_REG_BG = "#EDF1F6"        # 정기 보관함
COLOR_ARCHIVE_REG_TEXT = "#46586F"
COLOR_ADHOC_BG = "#E3F1EE"              # 수시 모니터링 (청록)
COLOR_ADHOC_TEXT = "#146B5E"
# [추가: 2026-09-02] 모음 카드(ADHOC_DESIGN.md §6.13)가 쓰는 옅은 청록 테두리 —
# 「…로 보냄」 칩·📥 출처 칩·「모음」 배지가 공유한다. COLOR_ADHOC_TEXT의 알파
# 버전이지만 그 계열에서 유일하게 "선"으로만 쓰이는 자리라 이름을 따로 둔다.
COLOR_ADHOC_BORDER_SOFT = "rgba(20, 107, 94, 0.24)"
COLOR_ARCHIVE_ADHOC_BG = "#F0F7F5"      # 수시 보관함
# [추가: 2026-09-15] 홈 흐름도 칸 사이 화살표·말풍선 「다음 단계」 줄의 화살표.
# 값은 COLOR_TOGGLE_OFF와 같다(뜻은 다름 — 저쪽은 꺼진 스위치).
COLOR_FLOW_ARROW = "#CBD5E1"
# [추가: 2026-09-15] 홈 흐름도 D안 — 흐름의 한 단계가 아닌 칸의 점선 테두리(흰 바탕).
# 실시간은 초안 위에 매단 곁가지(안 들러도 초안은 알아서 모인다), 검색어·새 수집은 줄 맨 앞의
# 「조건을 정하는 입구」다. 값은 기존 세트를 빌리되 뜻이 달라 이름을 나눈다.
COLOR_FLOW_LIVE_SIDE_BORDER = "#FCA5A5"                  # COLOR_ERROR_BORDER_MID와 값 같음
COLOR_FLOW_ENTRY_REG_BORDER = "#BFDBFE"                  # COLOR_ACCENT_BORDER와 값 같음
COLOR_FLOW_ENTRY_ADHOC_BORDER = "rgba(20, 107, 94, 0.45)"  # COLOR_ADHOC_TEXT의 알파 버전
# [추가: 2026-09-15] 홈 흐름도 줄 이름표(정기 / 수시) 왼쪽 3px 띠 — 「이 줄 = 이 이름」을 잇는다.
# 입구 칸 점선(#BFDBFE)은 3px 띠로 칠하면 너무 옅어 정기만 한 단계 진하게 둔다.
COLOR_FLOW_ROW_REG_BAR = "#93C5FD"
COLOR_FLOW_ROW_ADHOC_BAR = COLOR_FLOW_ENTRY_ADHOC_BORDER
# 워드클라우드 빈도 5단계 — 진할수록 많이 나온 단어.
COLOR_WORDCLOUD_TIERS = (COLOR_HEADER, COLOR_ACCENT, "#3B7DDB", "#7AA5E8", "#A9C3EF")

# ── 라벨 (앰버, 정기·수시를 가로지르는 별도 축) ───────────────────────────
COLOR_LABEL_TEXT = "#8A5A14"
COLOR_LABEL_BG = "#FBF0DA"
COLOR_LABEL_BORDER = "rgba(138, 90, 20, 0.28)"
COLOR_LABEL_BORDER_SOFT = "rgba(138,90,20,0.22)"
# 📷 사진 추정 배지 — 라벨과 같은 앰버 축이지만 뜻이 달라 이름을 나눠 둔다.
# [수정: 2026-08-21] 앰버 제거 — 이 배지가 붙는 행은 이미 opacity로 흐려져 있어(§확정
# 본·초안의 .is-photo) 앰버까지 얹을 이유가 없었다. 중립 회색으로 낮춘다.
COLOR_PHOTO_BADGE_TEXT = COLOR_TEXT_MUTED
COLOR_PHOTO_BADGE_BG = COLOR_ROW_HOVER  # 순서상 COLOR_DIVIDER_SOFT보다 앞이라 값이 같은 이 상수를 쓴다
COLOR_PHOTO_BADGE_BORDER = COLOR_BORDER

# ── 상태: 정상 / 저장됨 ───────────────────────────────────────────────────
COLOR_OK_TEXT = "#166534"
COLOR_OK_BG = "#F0FDF4"
COLOR_OK_BORDER = "#BBF7D0"
# [수정: 2026-08-21] 초록에서 파랑으로 — "초록 = 발송" 하나로 좁히기로 하면서, 저장
# 성공도 다른 "정상/완료" 신호와 같은 파랑(정보)으로 옮겼다. 값은 accent 계열을 그대로
# 참조한다(새 색을 만들지 않는다).
COLOR_SAVED_TEXT = COLOR_ACCENT
COLOR_SAVED_BG = COLOR_HOVER
COLOR_SAVED_BORDER = COLOR_ACCENT_BORDER
COLOR_SAVED_DOT = COLOR_ACCENT

# ── 상태: 주의 / 저장 안 함 ───────────────────────────────────────────────
COLOR_WARN_TEXT = "#92400E"
COLOR_WARN_BG = "#FFFBEB"
COLOR_WARN_BORDER = "#FDE68A"
COLOR_WARN_CHIP_BG = "#FEF3C7"
COLOR_WARN_ACCENT = "#B45309"           # "오늘 적용 중" 뱃지 글자
COLOR_WARN_SUB = "#A16207"              # 그 아래 보조 설명
COLOR_WARN_DOT = "#F59E0B"              # 저장 대기 점, 중복 키워드 외곽선

# ── 상태: 오류 / 막힘 ─────────────────────────────────────────────────────
COLOR_ERROR_BG = "#FEF2F2"              # COLOR_LIVE_BG와 값 같음(뜻은 다름)
COLOR_ERROR_BORDER = "#FECACA"
COLOR_ERROR_BORDER_SOFT = "#F5C2C2"
COLOR_ERROR_DISABLED = "#FCA5A5"        # 저장 못 하는 상태의 저장 버튼
COLOR_ERROR_BORDER_MID = "#FCA5A5"      # 경고 상자 테두리(위와 값 같음, 뜻은 다름)
COLOR_ERROR_PLACEHOLDER = "#F87171"
COLOR_ERROR_STRONG = "#991B1B"          # API 호출량 과다(hot) 뱃지

# ── AI 분류 실패 배너 (초안 전용) ─────────────────────────────────────────
# [수정: 2026-08-21] 이 배너만의 빨강 세트를 없애고 "오류" 세트를 그대로 쓴다 — 실패를
# 알리는 배너라 뜻이 정확히 오류다. 시도 이력(보조 정보)은 위험 신호가 아니므로 danger가
# 아닌 muted 글자로 낮춘다.
COLOR_DEGRADED_BG = COLOR_ERROR_BG
COLOR_DEGRADED_BORDER = COLOR_ERROR_BORDER
COLOR_DEGRADED_TEXT = COLOR_ERROR
COLOR_DEGRADED_RULE = COLOR_ERROR_BORDER
COLOR_DEGRADED_SUB = COLOR_TEXT_MUTED

# ── 회차 마감 안내 배너 (노란 상단 고정 띠) ───────────────────────────────
# [수정: 2026-08-21] 이 배너만의 노랑 세트를 없애고 "주의" 세트를 그대로 쓴다 — 뜻이
# "회차가 끝났으니 확인하라"는 주의 신호이지, 새 세트가 필요한 고유한 뜻이 아니었다.
COLOR_ROUND_OVER_BG = COLOR_WARN_BG
COLOR_ROUND_OVER_BORDER = COLOR_WARN_BORDER
COLOR_ROUND_OVER_TEXT = COLOR_WARN_TEXT
COLOR_ROUND_OVER_TEXT_STRONG = COLOR_WARN_TEXT
COLOR_ROUND_OVER_DISMISS = COLOR_WARN_TEXT

# ── 수시 모니터링 전용 ────────────────────────────────────────────────────
# [수정: 2026-08-21] 이 배지만의 주황 세트를 없애고 "주의" 세트를 그대로 쓴다.
COLOR_OUT_BG = COLOR_WARN_BG            # 조건 밖으로 빠진 기사
COLOR_OUT_BORDER = COLOR_WARN_BORDER
COLOR_OUT_TAG_BG = COLOR_WARN_CHIP_BG
COLOR_OUT_TAG_TEXT = COLOR_WARN_TEXT
# [수정: 2026-08-21] 앰버는 라벨에만 남기기로 하면서, 이 칩은 수시 화면의 정체성 색인
# 청록(COLOR_ADHOC_TEXT)으로 옮겼다 — "앰버=라벨" 원칙을 지키면서도 여전히 파랑(주
# 검색어)과는 구분되는 색이 필요해서다.
COLOR_MUST_BG = COLOR_ADHOC_BG          # 꼭 포함할 검색어(AND) 칩
COLOR_MUST_TEXT = COLOR_ADHOC_TEXT
COLOR_MUST_BORDER = COLOR_ADHOC_TEXT
COLOR_MUST_BOX_BG = COLOR_CARD
COLOR_MUST_ACCENT = COLOR_ADHOC_TEXT
COLOR_QUERY_MARK = COLOR_HOVER          # [수정: 2026-08-21] 앰버→파랑. 라벨이 아니라 검색 일치 표시라서.
# [수정: 2026-08-21] 이 태그만의 주황 세트를 없애고 "주의" 세트를 그대로 쓴다.
COLOR_SNAP_GONE_BG = COLOR_WARN_BG      # 원본이 사라진 라벨 기사 태그
COLOR_SNAP_GONE_TEXT = COLOR_WARN_TEXT
COLOR_SNAP_GONE_BORDER = COLOR_WARN_BORDER

# ── 담당자가 직접 만든 소제목 (정기·수시 공통) ────────────────────────────
COLOR_CUSTOM_GROUP_BORDER = "#7DD3FC"
COLOR_CUSTOM_TAG_BG = "#E0F2FE"
COLOR_CUSTOM_TAG_TEXT = "#0369A1"

# ── 중립 회색 ─────────────────────────────────────────────────────────────
COLOR_DIVIDER_SOFT = "#F1F5F9"          # 아주 옅은 구분선·칩 바탕
COLOR_TOGGLE_OFF = "#CBD5E1"            # 꺼진 토글 track — 스위치 부품이라 고유 톤을 남긴다
COLOR_TOGGLE_OFF_TEXT = "#64748B"
COLOR_TEXT_SOFT = "#4B5563"             # 본문보다 한 단계 연한 글자
COLOR_TEXT_FAINT = "#9CA3AF"            # 안내·빈 값 글자
COLOR_ORDER_BTN_DISABLED = "#D1D5DB"    # 언론사 순서 화살표 — 목록 끝이라 못 누름
COLOR_FLOAT_INPUT_BORDER = "#D1D5DB"    # 색 칸 위에 얹힌 흰 입력칸의 테두리(초안 📌 칸의 「+ 수기로 기사 추가」)
# [수정: 2026-08-21] 중립 회색 13종을 값 기준 이 8~10개로 모았다. 아래 6개는 전부 위
# 회색들과 같은 값을 가리키는 별칭이다 — 이름(쓰임)은 그대로 두되 색만 합쳤다.
COLOR_SURFACE_SOFT = COLOR_DIVIDER_SOFT  # 접힌 날짜 블록 바탕
COLOR_SURFACE_OFF = COLOR_CARD          # 꺼진 키워드 그룹 바탕
COLOR_TEXT_FAINT_ALT = COLOR_TEXT_FAINT  # 검색어 기재 줄의 돋보기 아이콘
COLOR_SEP_FAINT = COLOR_TEXT_FAINT      # 값 사이 구분 기호(·, +)
COLOR_DASH_BORDER = COLOR_BORDER        # 점선 입력칸 테두리
COLOR_PLACEHOLDER = COLOR_TEXT_FAINT
COLOR_CHIP_OFF_BORDER = COLOR_TOGGLE_OFF  # 꺼진 키워드 칩 테두리 — "꺼짐" 표시는 토글과 같은 톤
# [추가: 2026-09-17] 텔레그램 받는 사람 화면 — 받는 것 칩(꺼짐 점선·켜짐 실선)과 「알림」 칩.
# 새 색은 없다: 켜진 [단독]·[속보]는 말머리 빨강, 정기는 파랑, 수시는 청록 계열 값을 그대로 쓴다.
COLOR_SCOOP_CHIP_BORDER = COLOR_ERROR_BORDER_MID   # 켜진 [단독]·[속보] 칩 테두리
COLOR_ADHOC_BORDER_STRONG = COLOR_FLOW_ENTRY_ADHOC_BORDER  # 켜진 수시 칩 테두리(청록 알파)
COLOR_NOTIFY_CHIP_BG = COLOR_PILL_BG               # 「알림」 칩 — 늘 값이 있어 켜짐/꺼짐이 없는 중립 회색
COLOR_NOTIFY_CHIP_BORDER = COLOR_TOGGLE_OFF
# 화면 이름 칩 — 초안·확정본·수시 원본·수시 확정본 제목 맨 앞의 (초안)/(확정본)/(원본) 칩.
# 새 색은 없다: 다듬기 전 화면(초안·원본)은 중립 회색, 보고서 화면은 흐름의 색(정기 파랑·수시 청록).
COLOR_SCREEN_TAG_WORK_BG = COLOR_DIVIDER_SOFT      # (초안)·(원본)
COLOR_SCREEN_TAG_WORK_BORDER = COLOR_BORDER
COLOR_SCREEN_TAG_WORK_TEXT = COLOR_TEXT_MUTED
COLOR_SCREEN_TAG_REG_BG = COLOR_HOVER              # 정기 (확정본)
COLOR_SCREEN_TAG_REG_BORDER = COLOR_ACCENT_BORDER
COLOR_SCREEN_TAG_REG_TEXT = COLOR_ACCENT
COLOR_NOTIFY_CHIP_TEXT = COLOR_TEXT_SOFT
COLOR_KWPOP_FLASH = COLOR_ROW_NEW       # 키워드 칩 추가 순간 반짝임 — "방금 바뀜" 노랑 재사용
# [추가: 2026-09-11] 워드클라우드 제외어 칩 테두리 — 값은 꺼진 키워드 칩과 같지만 뜻이
# 다르다(제외어엔 ON/OFF가 없다 — 검색어 칩의 파랑 "검색 ON"을 안 빌리려고 중립 회색).
COLOR_WC_EXCLUDE_CHIP_BORDER = COLOR_TOGGLE_OFF

# ── 홈 남색 카드(「부정 추정 기사」 + [단독]/[속보], app.landing_renderer) ──
# 홈에서 유일하게 짙은 바탕을 쓰는 카드(시안 mockups/HOME_NEGATIVE_GUESS_MOCKUP.html).
# 바탕 위 글자는 전부 이 카드 전용 색이다.
COLOR_BOARD_BG = COLOR_HEADER               # 남색 바탕(제목색과 같은 값, 뜻이 달라 따로 둔다)
COLOR_BOARD_TEXT = "#CBD5E1"                # 바탕 위 보통 글자(「HH:MM 회차까지」)
COLOR_BOARD_NEW = "#FDE68A"                 # 「+N」(가장 최근 회차에서 더해진 수)
COLOR_BOARD_RULE = "rgba(255,255,255,.18)"  # [단독]/[속보] 앞 세로선
COLOR_BOARD_HOVER = "rgba(255,255,255,.12)" # 칩·버튼 hover
COLOR_BOARD_NEG = "#FCA5A5"                 # 「부정 추정 기사」·[속보] 칩(바탕 위)
COLOR_BOARD_SCOOP = "#FDA4AF"               # [단독] 칩(바탕 위)
COLOR_BOARD_NEW_CHIP_TEXT = COLOR_WARN_TEXT # 목록의 「new」 칩 — 주의 계열 재사용
COLOR_BOARD_NEW_CHIP_BG = "#FEF3C7"

# 각 렌더러가 `.format(**PALETTE, ...)`로 통째로 받아 쓰는 표.
# 키 이름은 CSS 템플릿 안 `{...}` 자리표시자 이름이 된다 — 위 상수 이름에서
# COLOR_ 접두어만 뗀 소문자다. 이 표에 없는 색은 화면 어디에도 쓰지 않는다.
PALETTE = {
    "bg": COLOR_BG,
    "card": COLOR_CARD,
    "header": COLOR_HEADER,
    "accent": COLOR_ACCENT,
    "text": COLOR_TEXT,
    "muted": COLOR_TEXT_MUTED,
    "border": COLOR_BORDER,
    "hover": COLOR_HOVER,
    "error": COLOR_ERROR,
    "live_bg": COLOR_LIVE_BG,
    "on_fill": COLOR_ON_FILL,
    "accent_pressed": COLOR_ACCENT_PRESSED,
    "accent_tonal": COLOR_ACCENT_TONAL,
    "accent_chip_bg": COLOR_ACCENT_CHIP_BG,
    "accent_chip_hover": COLOR_ACCENT_CHIP_HOVER,
    "accent_border": COLOR_ACCENT_BORDER,
    "accent_border_soft": COLOR_ACCENT_BORDER_SOFT,
    "accent_panel_bg": COLOR_ACCENT_PANEL_BG,
    "ghost_border": COLOR_GHOST_BORDER,
    "ghost_border_hover": COLOR_GHOST_BORDER_HOVER,
    "header_pressed": COLOR_HEADER_PRESSED,
    "ai_bg": COLOR_AI_BG,
    "ai_bg_hover": COLOR_AI_BG_HOVER,
    "ai_text": COLOR_AI_TEXT,
    "ai_text_strong": COLOR_AI_TEXT_STRONG,
    "ai_text_muted": COLOR_AI_TEXT_MUTED,
    "ai_border": COLOR_AI_BORDER,
    "ai_border_hover": COLOR_AI_BORDER_HOVER,
    "ai_mark": COLOR_AI_MARK,
    "ai_banner_bg": COLOR_AI_BANNER_BG,
    "row_hover": COLOR_ROW_HOVER,
    "row_moved": COLOR_ROW_MOVED,
    "row_new": COLOR_ROW_NEW,
    "row_promoted": COLOR_ROW_PROMOTED,
    "pill_bg": COLOR_PILL_BG,
    "scoop_text": COLOR_SCOOP_TEXT,
    "scoop_bg": COLOR_SCOOP_BG,
    "scoop_bar": COLOR_SCOOP_BAR,
    "flash_text": COLOR_FLASH_TEXT,
    "undated_bar": COLOR_UNDATED_BAR,
    "send": COLOR_SEND,
    "pin_gray": COLOR_PIN_GRAY,
    "pin_gray_bg": COLOR_PIN_GRAY_BG,
    "pinned_bar": COLOR_PINNED_BAR,
    "pinned_bg": COLOR_PINNED_BG,
    "pinned_text": COLOR_PINNED_TEXT,
    "pinned_chip_bg": COLOR_PINNED_CHIP_BG,
    "pinned_border": COLOR_PINNED_BORDER,
    "arch_reg_bg": COLOR_ARCHIVE_REG_BG,
    "arch_reg_text": COLOR_ARCHIVE_REG_TEXT,
    "adhoc_bg": COLOR_ADHOC_BG,
    "adhoc_text": COLOR_ADHOC_TEXT,
    "adhoc_border_soft": COLOR_ADHOC_BORDER_SOFT,
    "screen_tag_work_bg": COLOR_SCREEN_TAG_WORK_BG,
    "screen_tag_work_border": COLOR_SCREEN_TAG_WORK_BORDER,
    "screen_tag_work_text": COLOR_SCREEN_TAG_WORK_TEXT,
    "arch_adhoc_bg": COLOR_ARCHIVE_ADHOC_BG,
    "flow_arrow": COLOR_FLOW_ARROW,
    "flow_live_side_border": COLOR_FLOW_LIVE_SIDE_BORDER,
    "flow_entry_reg_border": COLOR_FLOW_ENTRY_REG_BORDER,
    "flow_entry_adhoc_border": COLOR_FLOW_ENTRY_ADHOC_BORDER,
    "flow_row_reg_bar": COLOR_FLOW_ROW_REG_BAR,
    "flow_row_adhoc_bar": COLOR_FLOW_ROW_ADHOC_BAR,
    "label_text": COLOR_LABEL_TEXT,
    "label_bg": COLOR_LABEL_BG,
    "label_border": COLOR_LABEL_BORDER,
    "label_chip_border": COLOR_LABEL_BORDER_SOFT,
    "trend_1": COLOR_TREND_1,
    "trend_2": COLOR_TREND_2,
    "trend_3": COLOR_TREND_3,
    "trend_4": COLOR_TREND_4,
    "trend_5": COLOR_TREND_5,
    "trend_6": COLOR_TREND_6,
    "trend_7": COLOR_TREND_7,
    "trend_8": COLOR_TREND_8,
    "photo_badge_text": COLOR_PHOTO_BADGE_TEXT,
    "photo_badge_bg": COLOR_PHOTO_BADGE_BG,
    "photo_badge_border": COLOR_PHOTO_BADGE_BORDER,
    "ok_text": COLOR_OK_TEXT,
    "ok_bg": COLOR_OK_BG,
    "ok_border": COLOR_OK_BORDER,
    "saved_text": COLOR_SAVED_TEXT,
    "saved_bg": COLOR_SAVED_BG,
    "saved_border": COLOR_SAVED_BORDER,
    "saved_dot": COLOR_SAVED_DOT,
    "warn_text": COLOR_WARN_TEXT,
    "warn_bg": COLOR_WARN_BG,
    "warn_border": COLOR_WARN_BORDER,
    "warn_chip_bg": COLOR_WARN_CHIP_BG,
    "warn_accent": COLOR_WARN_ACCENT,
    "warn_sub": COLOR_WARN_SUB,
    "warn_dot": COLOR_WARN_DOT,
    "board_bg": COLOR_BOARD_BG,
    "board_text": COLOR_BOARD_TEXT,
    "board_new": COLOR_BOARD_NEW,
    "board_rule": COLOR_BOARD_RULE,
    "board_hover": COLOR_BOARD_HOVER,
    "board_neg": COLOR_BOARD_NEG,
    "board_scoop": COLOR_BOARD_SCOOP,
    "board_new_chip_text": COLOR_BOARD_NEW_CHIP_TEXT,
    "board_new_chip_bg": COLOR_BOARD_NEW_CHIP_BG,
    "error_bg": COLOR_ERROR_BG,
    "error_border": COLOR_ERROR_BORDER,
    "error_border_soft": COLOR_ERROR_BORDER_SOFT,
    "error_disabled": COLOR_ERROR_DISABLED,
    "error_border_mid": COLOR_ERROR_BORDER_MID,
    "error_placeholder": COLOR_ERROR_PLACEHOLDER,
    "error_strong": COLOR_ERROR_STRONG,
    "degraded_bg": COLOR_DEGRADED_BG,
    "degraded_border": COLOR_DEGRADED_BORDER,
    "degraded_text": COLOR_DEGRADED_TEXT,
    "degraded_rule": COLOR_DEGRADED_RULE,
    "degraded_sub": COLOR_DEGRADED_SUB,
    "round_over_bg": COLOR_ROUND_OVER_BG,
    "round_over_border": COLOR_ROUND_OVER_BORDER,
    "round_over_text": COLOR_ROUND_OVER_TEXT,
    "round_over_text_strong": COLOR_ROUND_OVER_TEXT_STRONG,
    "round_over_dismiss": COLOR_ROUND_OVER_DISMISS,
    "out_bg": COLOR_OUT_BG,
    "out_border": COLOR_OUT_BORDER,
    "out_tag_bg": COLOR_OUT_TAG_BG,
    "out_tag_text": COLOR_OUT_TAG_TEXT,
    "must_bg": COLOR_MUST_BG,
    "must_text": COLOR_MUST_TEXT,
    "must_border": COLOR_MUST_BORDER,
    "must_box_bg": COLOR_MUST_BOX_BG,
    "must_accent": COLOR_MUST_ACCENT,
    "query_mark": COLOR_QUERY_MARK,
    "snap_gone_bg": COLOR_SNAP_GONE_BG,
    "snap_gone_text": COLOR_SNAP_GONE_TEXT,
    "snap_gone_border": COLOR_SNAP_GONE_BORDER,
    "custom_group_border": COLOR_CUSTOM_GROUP_BORDER,
    "custom_tag_bg": COLOR_CUSTOM_TAG_BG,
    "custom_tag_text": COLOR_CUSTOM_TAG_TEXT,
    "divider_soft": COLOR_DIVIDER_SOFT,
    "surface_soft": COLOR_SURFACE_SOFT,
    "surface_off": COLOR_SURFACE_OFF,
    "toggle_off": COLOR_TOGGLE_OFF,
    "toggle_off_text": COLOR_TOGGLE_OFF_TEXT,
    "text_soft": COLOR_TEXT_SOFT,
    "text_faint": COLOR_TEXT_FAINT,
    "order_btn_disabled": COLOR_ORDER_BTN_DISABLED,
    "float_input_border": COLOR_FLOAT_INPUT_BORDER,
    "text_faint_alt": COLOR_TEXT_FAINT_ALT,
    "sep_faint": COLOR_SEP_FAINT,
    "dash_border": COLOR_DASH_BORDER,
    "placeholder": COLOR_PLACEHOLDER,
    "chip_off_border": COLOR_CHIP_OFF_BORDER,
    "kwpop_flash": COLOR_KWPOP_FLASH,
    "wc_exclude_chip_border": COLOR_WC_EXCLUDE_CHIP_BORDER,
    "scoop_chip_border": COLOR_SCOOP_CHIP_BORDER,
    "adhoc_border_strong": COLOR_ADHOC_BORDER_STRONG,
    "notify_chip_bg": COLOR_NOTIFY_CHIP_BG,
    "notify_chip_border": COLOR_NOTIFY_CHIP_BORDER,
    "notify_chip_text": COLOR_NOTIFY_CHIP_TEXT,
}

# 모양 토큰 — 색처럼 값은 여기 한 곳에만 둔다(CLAUDE.md 디자인 「모양」, 시안
# mockups/SHAPE_RULES_MOCKUP.html). 모양이 뜻을 말한다: 네모 = 누르면 무언가를 한다,
# 큰 네모 = 담는 틀, 알약 = 상태·건수·분류를 알려 준다, 동그라미 = 떠 있는 버튼·숫자 배지.
# CSS엔 숫자 대신 `var(--r-md)`처럼 CSS 변수로 적는다 — 템플릿마다 `.format`·f-string·
# 그냥 문자열이 섞여 있어 중괄호 자리표시자를 쓸 수 없는 곳이 있어서다. 변수 정의
# (SHAPE_TOKENS_CSS)는 상단바 CSS(`app.topnav.topnav_style`)와 홈이 한 번씩 낸다.
RADIUS_SM = "4px"        # 행 안 작은 컨트롤(「다른 소제목」 드롭다운, 행 아이콘 버튼)
RADIUS_MD = "6px"        # 버튼 · 입력칸 · 드롭다운 · 기사 행
RADIUS_LG = "10px"       # 카드 · 배너 · 팝오버 · 창 · 흐름도 칸
RADIUS_PILL = "999px"    # 칩 · 건수 · 상단바 지금 화면
RADIUS_CIRCLE = "50%"    # 떠 있는 버튼 · 점
CONTROL_H_SM = "24px"    # 기사 행 안
CONTROL_H_TB = "28px"    # 툴바 버튼(확정본·초안·수시) — 입력칸보다 한 단계 낮다
CONTROL_H_MD = "30px"    # 입력칸 (기본)
CONTROL_H_LG = "36px"    # 폼의 대표 버튼(수집 · 저장 · 조회), 설정 화면 버튼
FAB_LG = "56px"          # 발송 · 휴지통
FAB_SM = "48px"          # ↩ 되돌리기 · 목차
SHADOW_FLOAT = "0 4px 12px rgba(15, 23, 42, 0.12)"   # 떠 있는 버튼 · 선택 바
SHADOW_POP = "0 8px 24px rgba(15, 23, 42, 0.14)"     # 팝오버 · 드롭다운 메뉴 · 말풍선
SHADOW_MODAL = "0 16px 40px rgba(15, 23, 42, 0.22)"  # 확인창 · 이름 고르기 창

# 글자 크기 여섯 단계(CLAUDE.md 디자인 「글자 크기」, 시안 mockups/TYPE_SCALE_MOCKUP.html).
# CSS엔 `font-size: var(--fs-md)`처럼 적는다. 아이콘 크기를 font-size로 정하는 곳(↩·⋯·✏️·▲▼·×)과
# 홈 머리 제목·워드클라우드·손글씨 캡션은 이 단계 밖이다.
FONT_XS = "0.72rem"      # 숫자 배지 · 작은 칩 안 보조 글자 · 캡션
FONT_SM = "0.8rem"       # 메타 줄 · 칩 · 행 안 컨트롤 · 작은 안내
FONT_MD = "0.88rem"      # 버튼 · 툴바 · 입력칸 · 배너 · 안내문 · 펼친 기사 요약
FONT_BASE = "1rem"       # 기사 제목 · 본문
FONT_LG = "1.1rem"       # 소제목 · 카드 제목 · 날짜 줄 · 빈 화면 안내
FONT_XL = "1.3rem"       # 화면 제목(모든 화면 같은 크기)

# 「+ 새 소제목」류 단추의 글자 «+» — 폰트가 그리는 십자는 한글보다 얇고 낮아 혼자
# 묻힌다. SVG 아이콘으로 바꿔도 1em 박스라 2px 커질 뿐이어서(실측), 글자 그대로 두고
# 키우는 쪽을 골랐다. 이 CSS는 상단바(app.topnav.topnav_style)가 함께 낸다.
PLUS_GLYPH_CSS = (
    ".plus-glyph { font-size: 1.2em; font-weight: 600; line-height: 0; margin-right: 4px; }"
)

SHAPE_TOKENS_CSS = (
    ":root { "
    f"--fs-xs: {FONT_XS}; --fs-sm: {FONT_SM}; --fs-md: {FONT_MD}; "
    f"--fs-base: {FONT_BASE}; --fs-lg: {FONT_LG}; --fs-xl: {FONT_XL}; "
    f"--r-sm: {RADIUS_SM}; --r-md: {RADIUS_MD}; --r-lg: {RADIUS_LG}; "
    f"--r-pill: {RADIUS_PILL}; --r-circle: {RADIUS_CIRCLE}; "
    f"--h-sm: {CONTROL_H_SM}; --h-tb: {CONTROL_H_TB}; --h-md: {CONTROL_H_MD}; --h-lg: {CONTROL_H_LG}; "
    f"--fab-lg: {FAB_LG}; --fab-sm: {FAB_SM}; "
    f"--sh-float: {SHADOW_FLOAT}; --sh-pop: {SHADOW_POP}; --sh-modal: {SHADOW_MODAL}; "
    "}"
)

# PRD.md 기능1 규칙 6 — 기사 제목·요약 내 형광펜 단어 하이라이트 색상·기본값·최대 개수.
# [수정: 2026-07-23] 하이라이트 대상은 검색 키워드(DEFAULT_KEYWORDS)와 완전히 별개 설정이다.
# [수정: 2026-07-25] 단어별로 색이 다르면 훑어보기 더 빠르다는 요청으로 단어마다 색 하나를
# 배정한다. [수정: 2026-07-25] 처음엔 등록 순서로 자동 배정했는데, 단어를 하나 지우면
# 뒤에 있던 단어들 색이 자리별로 밀려 바뀌는 문제가 있었다 — 색이 "단어"가 아니라
# "자리"에 붙어 있었기 때문. 그래서 각 단어에 색 인덱스를 직접 저장해 고정한다(사용자가
# 팔레트에서 직접 골라 바꿈, 자유 색상 선택은 아님 — 비슷한 색을 골라 헷갈리는 사고를
# 막기 위해 이 5색 팔레트 안에서만 고른다).
# [수정: 2026-08-07] 기존 5색(네온 라임·비비드 오렌지·비비드 하늘색·비비드 핑크·비비드
# 보라)이 특히 "재정경제부"처럼 거의 모든 기사에 등장하는 단어에 쓰이면 화면 전체가 너무
# 튀어 눈이 피로하다는 피드백으로 더스티 파스텔 5색으로 한 번 교체했었는데, 그마저도
# 여전히 톤이 있다는 피드백으로 아주 연한 파스텔(핑크·퍼플·옐로우·민트·파랑) 5색으로 다시
# 교체했다.
# [수정: 2026-08-11] 이번엔 반대로 "너무 안 보인다"는 피드백 — 시안 3단계(살짝 상향/
# 뚜렷하게/가장 선명) 중 "뚜렷하게" 안을 골라 적용했다. 맨 처음 네온 톤(2026-08-07 이전)
# 만큼 비비드하진 않으면서도 지금보다는 확실히 눈에 띄는 중간 지점 — "재정경제부"처럼
# 거의 모든 기사에 등장하는 단어에 써도 예전만큼 피로하지 않을 정도로 조정했다. 인덱스
# (순서)는 계속 그대로라 이미 저장된 단어별 색 배정(highlight_keywords의 "color": 인덱스)
# 은 그대로 유지되고 실제 렌더링 색만 바뀐다.
HIGHLIGHT_COLORS = ["#FFE7E7", "#EDE2FA", "#FFF8DE", "#DFF5E9", "#E4F1FC"]
# [추가: 2026-07-24] 하이라이트 배경 위 글자색. 기본 글자색을 그대로 쓰면 밝은 배경과
# 대비가 약해 잘 안 보이므로, 짙은 색으로 바꿔 대비를 준다. 팔레트 5색 모두 파스텔 톤이라
# 이 짙은 글자색 하나로 다섯 색 전부 대비가 충분하다.
HIGHLIGHT_TEXT_COLOR = COLOR_TEXT
# [수정: 2026-07-25] {"word": 단어, "color": 팔레트 인덱스} 형태로 바뀌었다 — 색을 단어에
# 직접 고정하기 위해서(위 설명 참고).
DEFAULT_HIGHLIGHT_KEYWORDS = [{"word": "재정경제부", "color": 0}, {"word": "재경부", "color": 1}]

# [수정: 2026-07-25] 5 -> 20으로 확대 — 형광펜 단어를 이제 검색 키워드 화면의 🖍️ 버튼으로만
# 추가하게 되면서(직접 입력 없음), 검색 키워드가 많아지면 형광펜도 그만큼 늘 수 있다.
# 팔레트는 여전히 5색뿐이라 6번째 단어부터는 자연히 같은 색을 여러 단어가 나눠 쓴다.
MAX_HIGHLIGHT_KEYWORDS = 20

# PRD.md 기능2 규칙 1 — 소제목은 매 회차 자동 생성.
#
# [수정: 2026-08-11] 5 → 8. 사안이 많은 회차에서 소제목 하나에 기사가 지나치게 몰린다는
# 판단(사용자 요청). 실측 근거 — 상한 5일 때 61건 회차가 [44, 12, 4, 1]로 갈려서
# **44건이 한 덩어리**였는데, 8로 올리니 [25, 21, 8, 7]이 되고 1건짜리 소제목도 0개였다.
# 23건 회차도 최대 덩어리 13 → 10, 36건 회차도 15 → 12로 줄었다.
#
# "상한을 올리면 기사 적은 회차가 잘게 쪼개진다"는 우려가 있었고 실제로 옛 프롬프트에서는
# 7건 회차가 1건짜리 5개로 갈라졌지만, 소제목 프롬프트를 쟁점 기준으로 바꾸면서 넣은
# "기사가 10건 미만으로 적을 때는 억지로 쪼개지 마라" 규칙이 이를 막는다 — 같은 7건
# 회차를 상한 5와 8로 각각 돌려 결과가 완전히 동일함을 확인했다(app.llm_classifier
# _SYSTEM_PROMPT 참고). 프롬프트가 하한을 지키므로 상한만 넉넉히 두면 된다.
#
# [수정: 2026-08-20] 8 -> 15. 재정경제위 있는 날처럼 기사 40건 넘는 회차에서 8개로는
# 부족해 기타에 30~75%가 몰리는 걸 실측으로 확인(예: 08-20 09:30 회차 61건 중 46건).
# 반대로 "상한을 올리면 적은 회차가 잘게 쪼개진다"는 우려도 실측으로 재현됐다 —
# 14건 회차를 상한 15로 돌리자 1건짜리가 5→9개로 늘어 밀도 1.8→1.3까지 떨어졌다
# ("10건 미만이면 안 쪼갠다"는 절대 건수 규칙이 12~20건 구간엔 안 걸렸기 때문).
# 그래서 AI_RULES.md에 "묶음당 평균 2건 이상, 미달이면 가까운 1건짜리끼리 합쳐라"는
# 밀도 규칙을 추가해 상한과 별개로 과다분할을 억제한다 — 76건 회차에서 상한 15를
# 줘도 실제로는 11~15개만 쓰고 1건짜리가 0~1개로 유지됨을 확인했다(AI가 필요한
# 만큼만 만들지, 상한을 채우려 억지로 쪼개지 않는다). 비용은 입력 토큰(기사 배치)이
# 거의 그대로라 상한과 무관하고, 출력(소제목 요약)만 늘어 회당 2~3원 증가에 그친다
# (LLM_COST_USAGE.md 참고). 모델은 그대로 Haiku 유지 — Sonnet 5는 코드가 thinking을
# 명시 안 해 기본으로 켜지는 바람에 14건짜리 쉬운 회차에서도 출력의 88%가 안 보이는
# 추론에 먹혀 max_tokens=2000은커녕 4096도 부족했고, 비용도 6배 이상 뛰었다 — 이
# 작업은 패턴 매칭에 가까워 그 값을 안 쓴다.
MAX_SUBHEADINGS = 15

# [추가: 2026-09-01] 「AI 모든 기사 재분류」 확인창에서 "이건 안 건드립니다"를 알리는 줄.
# 확정본(app/renderer.py)·초안(app/preview_renderer.py) 두 화면과, 초안의 두 갈래(보통 /
# AI 분류 폴백 상태)가 **글자 하나까지 같은 문장**을 써야 한다 — 같은 버튼을 누르는데
# 상황에 따라 안심 문구의 말투가 달라지면 담당자가 "이건 다른 뜻인가?" 하고 멈춘다.
# 실제로 예전엔 세 자리에 같은 문장이 따로 적혀 있어 한 곳만 고치면 갈라졌다.
#
# "그대로 유지됩니다"가 아니라 "영향을 받지 않습니다"인 이유: 직접 만든 소제목은 내용은
# 그대로지만 목록에서의 **위치는 밀릴 수 있다**(재분류로 다른 소제목 이름이 전부 새로
# 생기면서 group_order 버킷이 비므로). "그대로"는 위치까지 약속하는 것처럼 읽혀 그
# 만큼이 과장이었다.
RECLASSIFY_SAFE_NOTE = "(직접 만든 소제목과 그 안의 기사, 숨긴 기사, 담아둔 기사는 영향을 받지 않습니다.)"

# [추가: 2026-08-11] 소제목 생성에 쓰는 Claude 모델 (app/llm_classifier.py).
# 원래 PRD는 "API 비용 회피"를 이유로 LLM을 아예 안 쓰기로 했지만, 규칙 기반
# 단어빈도 분류가 만들어내는 소제목("등을"·"방안" 같은 단어 하나가 그대로 소제목이 됨)이
# 실사용에서 계속 어색하다는 판단으로 소제목에 한해서만 도입했다. 키워드 없거나 호출이
# 실패하면 예외 없이 기존 규칙 기반으로 되돌아가므로(app/llm_classifier.py 참고),
# 이 기능이 죽어도 앱은 예전과 똑같이 동작한다.
#
# 모델은 여기 한 줄만 바꾸면 교체된다. 우선 가장 저렴한 Haiku로 시작한다.
#
# [수정: 2026-08-20] /llm 설정 화면에 모델 선택 라디오를 잠깐 뒀다가 도로 뺐다 — 소제목
# 분류는 이 앱이 `thinking` 파라미터를 안 넘기는데, Sonnet 5/Opus 5는 그 상태에서
# thinking이 기본으로 켜져 출력의 88%가 안 보이는 추론에 먹히고 max_tokens=4096도
# 넘겨 조용히 규칙 기반으로 폴백했다(실측, HISTORY.md "MAX_SUBHEADINGS 8 → 15" 항목).
# 게다가 [연결 테스트]는 max_tokens=1짜리 최소 호출이라 이 실패를 못 잡는다 — 골라도
# 되는 것처럼 보이는데 고르면 깨지는 옵션이었다. 이 판단(어느 모델을 쓸지)은 API
# 키처럼 사용자마다 다른 값이 아니라 실측으로 정해지는 엔지니어링 결정이라, 화면이
# 아니라 코드로 되돌렸다. 다시 바꾸고 싶으면 이 한 줄을 고치고 재실측할 것.
LLM_MODEL = "claude-haiku-4-5"
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

# PRD.md 기능2 규칙 5 — 소제목별 요약 길이 제한
# [수정: 2026-09-10] MAIN_KEYWORD_COUNT(하단 "AI가 추출한 주요 키워드" 개수)는 그 블록을
# 없애면서 함께 지웠다 — 워드클라우드는 예전부터 LANDING_KEYWORD_COUNT를 쓴다.
SUMMARY_MAX_SENTENCES = 3
# [수정: 2026-08-12] 150 -> 250. AI_RULES.md 참고 — 실제 요약(app.llm_classifier)이
# 도입되며 150자로는 문장이 자주 중간에 잘렸다.
SUMMARY_MAX_CHARS = 250

# archive/DESIGN.md §3 — SQLite 대신 로컬 JSON 파일로 기사 데이터 저장
DATA_DIR = BASE_DIR / "data"
SETTINGS_FILE = DATA_DIR / "settings.json"
# [추가: 2026-08-20] 설정 화면(/naver, /llm)에서 등록한 네이버·Claude 키 저장소 —
# app.credentials 참고. 하나로 합친 이유: 두 화면의 저장·마스킹·연결 테스트·삭제
# 로직이 완전히 동일해 모듈도 하나로 충분하고, 앞으로 다른 연동 키(텔레그램 토큰,
# SMTP 등)가 이 화면 패턴을 따라오면 같은 파일에 필드만 늘리면 된다. data/ 폴더는
# 이미 .gitignore라 커밋되지 않는다.
CREDENTIALS_FILE = DATA_DIR / "credentials.json"
ARTICLES_DIR = DATA_DIR / "articles"
# PRD.md 기능1 규칙 19 — [추가: 2026-07-24] 숨긴 기사 URL 목록 (원본 회차 데이터와 분리 보관)
HIDDEN_ARTICLES_FILE = DATA_DIR / "hidden_articles.json"
# [추가: 2026-09-02] 숨긴 기사를 "한 번에 숨긴 덩어리"로 묶는 시간 창(초).
# app.undo.push의 coalesce_sec(3.0)과 **같은 값이어야 한다** — ↩ 되돌리기가 "한 걸음"이라
# 부르는 단위와 휴지통이 "한 묶음"이라 부르는 단위가 어긋나면, 담당자가 팝오버에서 본
# 묶음 하나가 ↩ 두 번에 나뉘어 되돌아가는 일이 생긴다.
HIDDEN_BATCH_WINDOW_SEC = 3.0
# [추가: 2026-09-04, 뜻 확장: 2026-09-16] 숨김이 **유지되는** 기간(일) = 휴지통에
# **보이는** 기간. 2026-09-04엔 이 둘이 갈려 있었다(필터는 오늘 하루, 열람만 7일) —
# 그래서 자정이 지나면 숨김이 저절로 풀렸고, 휴지통의 지난 날짜 줄은 되살릴 것이 없는
# 기록이었다. 2026-09-16에 사용자 결정으로 **판정도 이 값**을 쓴다(app.curation의
# load_hidden_urls / active_hidden_dates):
#   - 휴지통에 보이면 언제든 복구된다("보이는데 못 되살리는 줄"이 없다).
#   - 정기 보관함이 filter_hidden을 거치므로, 어제 발송한 보고서와 오늘 보관함이 어긋나지 않는다.
# **이 값을 더 늘리지 않는다** — app.undo가 hidden_articles.json을 통째로 20단계 복사해서,
# 1년치(실측 환산 약 3.6만 건·11MB)면 되돌리기 스택이 200MB대가 된다(2026-08-25에
# group_overrides를 1년 → 30일로 줄인 것과 같은 이유). 7일인 이유는 그대로다 — 이 값이
# 답하는 질문은 "어제·그저께 뭘 뺐더라"라 며칠 안에 끝나고, 더 오래 남겨야 하는 기사는
# 라벨(1년 보관)이라는 길이 이미 있다. 8일째엔 기록이 만료돼 그 기사가 다시 보인다.
HIDDEN_VIEW_DAYS = 7
# PRD.md 기능1 규칙 21 — [추가: 2026-07-24] 소제목 경계를 넘어 수동으로 옮긴 기사의 강제 소제목 기록
GROUP_OVERRIDES_FILE = DATA_DIR / "group_overrides.json"
# PRD.md 기능2 규칙 8 — [추가: 2026-07-24] 자동 생성된 소제목 단어에 사용자가 붙인 표시용 이름
GROUP_LABELS_FILE = DATA_DIR / "group_labels.json"
# [추가: 2026-07-30] 사용자가 "+ 새 소제목 만들기"로 직접 만든 소제목 이름 목록 — 자동
# 분류(app.classifier.classify_articles)로는 절대 안 나오는 이름이라, 기사가 아직 하나도
# 없어도(빈 소제목) 화면에 계속 떠 있게 하려면 따로 기억해둬야 한다. group_overrides.json/
# group_labels.json처럼 자정에 비워지지 않고 계속 유지된다 — 사용자가 🗑️로 직접 지우기
# 전까지는 다음날에도 그대로 남아있어야 "미리 만들어두고 나중에 채운다"가 가능하다.
# [추가: 2026-08-12] 초안 소제목 분류 결과(LLM 캐시)를 메모리 말고 파일에도 남긴다 —
# 앱을 재시작하면 메모리 캐시(app.llm_classifier._cache)가 통째로 비어, 좋은 이름으로
# 분류돼 있던 초안이 규칙 기반(단어 빈도, "일자리"·"의원" 같은 한 단어)으로 되돌아가는
# 실사고가 있었다. 날짜로 자동 만료시키지 않는다 — 캐시 키가 URL 집합이라 내용 기반으로
# 자연히 무효화되고(어제 URL은 오늘 안 쓰임), _CACHE_MAX로 크기도 이미 제한돼 있다.
LLM_CLASSIFICATION_CACHE_FILE = DATA_DIR / "llm_classification_cache.json"
# [추가: 2026-08-13] LLM 소제목 분류 시스템 프롬프트의 원본 파일. app/llm_classifier.py가
# 코드에 하드코딩하는 대신 이 파일을 그대로 읽어 API에 보낸다 — 프롬프트를 고치려면
# 코드가 아니라 이 문서를 고치면 된다(data/ 밖에 둔 이유: 실행 데이터가 아니라
# PRD.md/README.md처럼 사람이 관리하는 문서라서).
AI_RULES_FILE = BASE_DIR / "AI_RULES.md"
CUSTOM_GROUPS_FILE = DATA_DIR / "custom_groups.json"
# [추가: 2026-08-26] "🤖 미분류 배정"이 새로 지어낸 소제목 이름 — custom_groups.json과
# 같은 {date, names} 자정 초기화 패턴이지만, 담당자가 만든 게 아니라 AI가 배정 중에
# 만들었다는 점이 다르다(app.assigned_groups). 이 목록에 없으면 app.classifier.
# _apply_forced_groups가 "분류 결과에 없는 이름"으로 보고 조용히 무시해, 방금 배정한
# 기사가 다음 새로고침에 미분류로 되돌아간다 — 실제로 그렇게 되던 버그를 고치며 추가.
ASSIGNED_GROUPS_FILE = DATA_DIR / "assigned_groups.json"
# [추가: 2026-09-03] 초안의 "회차당 자동 분류 1회" 소진 기록 — {date, slots:[회차 end]}.
# 이 기록이 프로세스 메모리에만 있던 동안, 앱을 껐다 켜면 같은 회차인데 기회가 다시 열려
# 초안을 여는 것만으로 전체 재분류가 한 번 더 돌았다(소제목 이름·구성·순서가 통째로 새로
# 지어지고, 이름을 키로 저장하는 이름표·순서·배정이 전부 무효가 된다 — 2026-09-02에만
# 재시작 7회, 실제로 11:00/14:00/17:00 회차가 여러 벌 분류됐다). 파일로 남겨 재시작이
# 기회를 되살리지 못하게 한다(app.auto_classify_turn).
AUTO_CLASSIFY_TURNS_FILE = DATA_DIR / "auto_classify_turns.json"
# [추가: 2026-08-03] 사용자가 ↑/↓로 직접 정한 소제목 화면 순서(이름 목록). group_labels.json
# 처럼 이름 기준으로 저장하고, 사라진 소제목은 조용히 무시한다(app.group_order).
GROUP_ORDER_FILE = DATA_DIR / "group_order.json"
# [추가: 2026-08-05] 기사 줄에 마우스를 올리면 나오는 "🔄 원문에서 다시 가져오기" 버튼이
# 저장하는 곳 — 네이버 API의 title/description은 가끔 이상한 지점(사진 설명, 문장 중간)에서
# 잘려 있는데, 이 버튼을 누르면 원문 페이지의 og:title/og:description으로 그 기사 하나만
# 덮어쓴다. {url: {"title": ..., "summary": ...}} 형태 — 두 필드 중 실제로 더 나은 값을
# 찾은 쪽만 저장한다(app.summary_overrides).
SUMMARY_OVERRIDES_FILE = DATA_DIR / "summary_overrides.json"
# PRD.md 기능10(라벨) — [추가: 2026-08-18] 담당자가 기사 한 건에 직접 붙이는 자유 입력
# 태그. URL 키 전역 저장소로, 정기(확정본·초안)·수시(수집 확정본) 어느 화면에서 붙였든
# 같은 항목을 함께 본다 — summary_overrides.json과 같은 이유로 app/adhoc/ 밖, 이
# 최상위에 둔다(app/adhoc/* → app/* 단방향 의존 규칙을 어기지 않으려면 두 흐름이 함께
# 쓰는 저장소는 반드시 여기 있어야 한다). 값은 {url: {labels, scrap_date, scrap_end,
# pub_date, outlet, title, group, source, labeled_at}} — 회차 파일이 1년 뒤 삭제돼도
# 라벨 붙은 기사는 이 파일에 독립된 사본으로 남는다(app.labels).
LABELS_FILE = DATA_DIR / "labels.json"
# 라벨 전용 되돌리기 스택. app/undo.py(정기 큐레이션, 자정 초기화)·app/adhoc/undo.py
# (카드별)와 별개다 — 라벨은 회차·카드 어느 쪽에도 안 매이므로 자정에 비우지 않는다.
LABEL_UNDO_FILE = DATA_DIR / "label_undo.json"
# [추가: 2026-08-05] "+ 직접 키워드 작성하기" — 하단 AI 요약 블록과 별개로, 이용자가
# 직접 자유 서식으로 적어두는 메모/키워드 한 줄. 완성본·초안 화면 상단(소제목 목록 위)에
# 보이고, 헤더 바로 아래 "- {text}" 줄로 복사/txt/텔레그램 텍스트에도 그대로 포함된다.
# {"text": "..."} 형태의 전역 값 하나(회차별로 나뉘지 않음) — 초안에 적어두면 그 회차가
# 완성본으로 넘어갈 때도 같은 문구가 자연스럽게 이어진다(app.manual_keyword_note).
MANUAL_KEYWORD_NOTE_FILE = DATA_DIR / "manual_keyword_note.json"
# [추가: 2026-08-06] 이메일 받는 사람 목록 — [{"name":..., "email":..., "enabled": bool}, ...].
# 텔레그램(챗 아이디 1개 고정)과 달리 이메일은 주소만 알면 바로 등록할 수 있어 여러 명을
# 둘 수 있다. enabled로 지우지 않고 켜고 끄기만 해서, 휴가 등으로 잠깐 다른 사람에게만
# 보내고 싶을 때 삭제·재등록 없이 토글만으로 바꿀 수 있다(app.email_recipients).
EMAIL_RECIPIENTS_FILE = DATA_DIR / "email_recipients.json"
# [추가: 2026-08-07] 텔레그램도 이메일과 같은 방식(여러 받는 사람 + 켜고 끄기)으로
# 바꾸면서 생긴 파일 — [{"name":..., "chat_id":..., "enabled": bool}, ...].
# app.telegram_recipients가 최초 1회, 기존 .env의 TELEGRAM_CHAT_ID를 "나"라는 이름으로
# 자동으로 이 목록에 옮겨 담아준다(이미 쓰고 있던 받는 사람을 잃지 않도록).
TELEGRAM_RECIPIENTS_FILE = DATA_DIR / "telegram_recipients.json"
# 텔레그램 봇 이름(받는 사람 대화방 맨 위에 보이는 이름) — 이름 자체는 텔레그램에 저장되고,
# 이 파일엔 「앱이 이 봇에 이름을 건 적 있는가」만 봇 id별로 적는다(app.telegram_bot_name).
TELEGRAM_BOT_NAME_FILE = DATA_DIR / "telegram_bot_name.json"
DEFAULT_TELEGRAM_BOT_NAME = "🤖 재경부 AI 뉴스 알림(디지털소통팀)"
# [추가: 2026-08-20] [단독]·[속보] 기사 알림(/breaking-alert) 사용자 설정 — 감시 대상
# 그룹·시간대·주기·몰아보내기 여부. 정기 스크랩 설정(data/settings.json)과 분리한
# 이유는 app.telegram_recipients.json과 같다 — 이 기능 하나만의 값이라 전역 설정을
# 매번 통째로 읽고 쓰는 경합에 얹지 않는다(app.breaking_alert).
BREAKING_ALERT_FILE = DATA_DIR / "breaking_alert.json"
# [추가: 2026-08-20] 위 설정과 분리한 실행 상태(마지막 폴링 시각 등, app.breaking_alert_state) —
# 같은 파일이면 설정 화면에서 "저장"을 누를 때마다 폴링 시각까지 덮어써 간격 계산이
# 매번 리셋된다.
BREAKING_ALERT_STATE_FILE = DATA_DIR / "breaking_alert_state.json"

# 홈 「부정 추정 기사」(app.negative_guess) — 회차마다 판정한 결과. 자정에 새로 시작한다.
NEGATIVE_GUESS_FILE = DATA_DIR / "negative_guess.json"
NEGATIVE_GUESS_KEYWORDS = ("재정경제부", "재경부")   # 제목·요약에 이 단어가 나온 정기 기사만(기관 전용)
NEGATIVE_GUESS_RETRY_MIN = 5                         # 판정이 실패한 회차는 5분 뒤 다시(회차마다 최대 3번)
NEGATIVE_GUESS_MAX_ATTEMPTS = 3
# [추가: 2026-08-20] 이미 알림을 보낸 기사 URL(app.alerted_urls) — 정기 회차 검사·수시
# 폴링·밤사이 몰아보내기 세 경로가 같은 기사를 동시에 찾아도 한 번만 보내도록 막는다.
# live_cache.json과 같은 "date 필드 불일치 = 자정 자동 리셋" 패턴.
ALERTED_URLS_FILE = DATA_DIR / "alerted_urls.json"

# 발송 기록(app.send_log, 화면 /send-log) — 앱이 보낸 모든 메시지(정기·수시 보고서, [단독]·[속보]·
# 몰림 알림, 호출 한도 경고)가 누구에게 도착했는지. 날짜마다 파일 하나, 90일 보관.
SEND_LOG_DIR = DATA_DIR / "send_log"
SEND_LOG_RETENTION_DAYS = 90
# [추가: 2026-08-20] 네이버 뉴스 검색 API 하루 호출 수(app.api_usage) — [단독]·[속보]
# 폴링이 일일 한도를 넘기지 않도록 감시한다. 정기 스크랩·실시간 현황·수시 모니터링
# 호출도 전부 여기 잡힌다(app.naver_api._search_one_keyword의 요청 지점 하나에서 기록).
API_USAGE_FILE = DATA_DIR / "api_usage.json"

# ── 로깅 (app/logging_setup.py) ─────────────────────────────────────────────
# 실행 기록을 파일로 남기는 자리. `data/` 아래에 두는 이유는 이 폴더가 이미
# "앱이 실행 중에 만드는 것"의 자리이고 .gitignore에도 들어 있기 때문이다.
LOG_DIR = DATA_DIR / "logs"
LOG_FILE = LOG_DIR / "app.log"

# [추가: 2026-08-26] 홈 화면 "정책 단어 추이"·전용 화면(/trend)에서 담당자가 지켜보기로
# 고른 단어(하나의 저장소를 두 화면이 공유) — app/home_trend.py. 등록 검색어 건수 자동
# 산정(TOP3)이 아니라 담당자가 직접 고른 것을 쓰는 이유는 HISTORY.md "홈 화면 재구성"
# 참고 — 요약하면 자동 TOP3는 매일 "재정경제부/구윤철/기획재정부"처럼 같은 대상의 다른
# 이름만 나오거나(건수 기준), 급등 기준으로 하면 선이 매일 바뀌어 추이 그래프의
# 전제(같은 대상을 계속 지켜본다)가 깨진다.
# [수정: 2026-08-27] 저장 상한(MAX_TREND_WORDS)과 홈 표시 개수(HOME_TREND_DISPLAY_COUNT)를
# 분리했다 — 전용 화면은 최대 8개까지 비교해도 "확 튀는 게 있으면 보인다"는 목적에는
# 쓸모가 있지만(사용자 판단), 홈 카드는 매 요청마다 다시 그리는 화면이라 선을 3개로
# 묶어야 한눈에 읽힌다.
# [수정: 2026-09-15] 다시 합쳤다 — 홈도 저장된 단어를 전부 그린다(사용자 결정). 앞 3개만
# 그리니 /trend에서 4번째로 추가한 단어가 홈에 말없이 안 나와 "저장이 안 됐나"로 읽혔다.
# 추이를 보는 그래프라 선이 겹쳐도 튀는 단어는 보인다는 판단이고, 날짜마다 찍던 점을
# 빼서(app.trend_chart) 선이 늘어난 만큼의 어지러움을 덜었다.
HOME_TREND_WORDS_FILE = DATA_DIR / "home_trend_words.json"
MAX_TREND_WORDS = 8
# 로그 보관 기간(일). 회차 파일의 RETENTION_DAYS(365)와 일부러 다르다 — 회차는
# "작년 이맘때"를 되짚는 열람용 자산이지만 로그는 최근 사고를 파는 도구라,
# 실제로 쓰이는 구간이 길어야 2주다(2026-08-26의 두 진단 모두 당일~이틀 안이었다).
LOG_RETENTION_DAYS = 14
# 화면(터미널)은 지금까지처럼 경고 이상만 — 담당자가 보는 곳을 INFO로 채우면
# 정작 경고가 묻힌다. 자세한 기록은 파일에만 남긴다.
CONSOLE_LOG_LEVEL = logging.WARNING
FILE_LOG_LEVEL = logging.INFO
# [추가: 2026-07-25] 실시간 기사 현황에서 "→ 스크랩" 버튼으로 담아둔 기사 목록. 예정된
# 회차 데이터(articles/*.json)와 별개로, 당일 자정까지만 유지되고 자정이 지나면 자동으로 비워진다.
MANUAL_ARTICLES_FILE = DATA_DIR / "manual_articles.json"
# [추가: 2026-07-27] "스크랩 초안"에서 직접 추가한 기사를 "위로" 눌러 예약해두면, 다음 정식
# 회차가 실제로 저장될 때(app.scraper.collect_run) 자동으로 그 회차 기사 목록에 합쳐진다 —
# manual_articles.json과 마찬가지로 당일 자정까지만 유지된다.
DRAFT_PENDING_ARTICLES_FILE = DATA_DIR / "draft_pending_articles.json"
# [추가: 2026-07-30] 스크랩 초안(preview.html)에서 ↑/↓로 같은 소제목 안에서 정리해둔
# 순서 — group_overrides.json(소제목 "사이" 이동)과 짝을 이루는, 소제목 "안" 순서
# 저장소다. 정식 회차가 저장되는 순간 한 번 반영되고 비워진다(app.scraper.collect_run) —
# manual_articles.json과 같은 패턴으로 당일 자정에도 자동으로 만료된다.
PREVIEW_ORDER_FILE = DATA_DIR / "preview_order.json"
# [추가: 2026-07-29] 실시간 기사 현황 증분 캐시 — 새로고침마다 당일 전체를 처음부터 다시
# 검색하지 않고, 지난번에 어디까지 봤는지(last_seen_pub_date) 기억해뒀다가 그 이후 것만
# 추가로 검색해 병합한다(app.live_cache). 당일 자정까지만 유효, 등록된 키워드 그룹이
# 바뀌면(keywords_signature 불일치) 무효화되고 처음부터 다시 시작한다.
LIVE_CACHE_FILE = DATA_DIR / "live_cache.json"
# [추가: 2026-07-30] 스크랩 초안(preview.html) 증분 캐시 — live_cache와 같은 원리지만
# 기준이 "당일 전체"가 아니라 "지금 진행 중인 회차"다. 회차가 바뀌면(정식 스크랩이
# 끝나 다음 회차로 넘어가면) signature가 달라져 자동으로 무효화되고 그 회차의
# 시작 시각부터 새로 모은다(app.preview_cache, app.preview_renderer).
PREVIEW_CACHE_FILE = DATA_DIR / "preview_cache.json"
# [추가: 2026-09-11] 초안에 한 번이라도 보인 기사 — 그 회차 동안 초안·확정본에서 붙잡아
# 둔다(app.draft_seen). 담당자가 숨기거나 조건(검색어·선택 언론사·사진/인사 제외)을 바꾸지
# 않는 한, 다시 검색했을 때 결과가 흔들려도 초안에서 안 빠지고 확정본에도 들어간다.
# 한 회차 분만 담는다(회차가 바뀌면 새로 시작).
DRAFT_SEEN_FILE = DATA_DIR / "draft_seen.json"

# 「✂ 오늘만 여기서 끊기」(초안) — 날짜별로 끊은 시각을 적는다. 오늘 날짜 것만 시간표에
# 끼워 넣고(app.today_cuts.apply_cuts), 지난 날짜 기록은 정기 보관함의 ✂ 표시에 쓴다.
TODAY_CUTS_FILE = DATA_DIR / "today_cuts.json"

# archive/DESIGN.md §1/§3 — 스크립트가 생성하는 화면 파일
OUTPUT_HTML_PATH = BASE_DIR / "index.html"
HISTORY_HTML_PATH = BASE_DIR / "history.html"
LANDING_HTML_PATH = BASE_DIR / "home.html"
# [추가: 2026-07-25] 실시간 기사 현황 — 예정된 회차와 무관하게, 요청이 올 때마다 당일
# 0시~현재까지 라이브로 다시 검색해 만드는 화면 (home.html의 워드클라우드 갱신과 같은
# "요청마다 새로 생성" 패턴).
LIVE_HTML_PATH = BASE_DIR / "live.html"
# [추가: 2026-07-27] 다음 예정 회차 미리보기 — 아직 회차가 끝나기 전에, 지금까지의
# 시간창(회차 시작~현재)으로 미리 수집·분류해 보여준다(live.html처럼 요청마다 새로 생성,
# 저장 없음). 여기서 숨기기/소제목 이동을 해두면 hidden_articles.json/group_overrides.json에
# 그대로 기록되므로, 정식 회차가 자동으로 돌 때 그 큐레이션이 자연히 반영된다.
PREVIEW_HTML_PATH = BASE_DIR / "preview.html"
LOGO_PATH = BASE_DIR / "logo.svg"

# archive/DESIGN.md §3 — 설정 저장을 위한 유일한 "서버" 예외. 기본은 로컬(127.0.0.1)에서만 연다.
SETTINGS_SERVER_PORT = 8765
# [추가: 2026-07-31] 화면에 박히는 모든 fetch/링크 주소와 서버가 실제로 붙는(bind) 주소를
# 이 값 하나로 통일한다. 기본값 "127.0.0.1"이면 지금까지와 완전히 동일(이 컴퓨터
# 자신만 접속 가능)하게 동작한다. 다른 네트워크(예: Tailscale)의 컴퓨터에서 접속하게
# 하려면 .env에 SERVER_HOST=<서버의 실제 주소>(예: Tailscale이 부여한 100.x.x.x)를
# 넣어두면 된다 — 그러면 화면에 박히는 주소도, 서버가 듣는 주소도 이 값을 따라간다
# (app.settings_server.create_settings_server). 기본값이 아닐 때만 0.0.0.0으로 리스닝해
# "설정을 안 건드리면 예전처럼 이 컴퓨터에서만 접속 가능"이라는 안전한 기본 동작이
# 그대로 유지된다.
SETTINGS_SERVER_HOST = os.getenv("SERVER_HOST", "127.0.0.1").strip() or "127.0.0.1"

# [추가: 2026-07-25] 워드클라우드에서 직접 빼고 싶은 단어(검색 키워드와 별개, 최소
# 개수 제한 없음 — 하나도 안 넣는 것도 유효). 검색 키워드 설정과 같은 방식·최대 개수.
MAX_WORDCLOUD_EXCLUDE_WORDS = 15

# [추가: 2026-08-06] 이메일 받는 사람 최대 인원 — 담당자 한 명이 여러 동료에게 전달하는
# 용도라 팀 규모를 넘어설 일이 거의 없다고 보고 넉넉히 잡았다.
MAX_EMAIL_RECIPIENTS = 15
# [추가: 2026-08-07] 텔레그램도 이메일과 같은 이유·같은 상한.
MAX_TELEGRAM_RECIPIENTS = 15

# [추가: 2026-08-21] 증분 검색(초안·실시간 현황)의 하한을 "지금"보다 이 분 수만큼 뒤로
# 물린다 — 네이버가 기사를 발행시각 순서대로 색인하지 않기 때문이다. 두 화면 모두
# "지금까지 본 가장 최신 pub_date"를 다음 검색의 하한(초과)으로 써왔는데, 그 뒤에
# 색인된 기사의 발행시각이 그 값보다 과거면 검색 범위 밖에 영영 남는다(초안에서는
# 미분류로도 안 뜨고, 확정본 마감 수집에서야 처음 나타나 "마감 후 자동 배정"이 붙는다).
# 실측(2026-08-21, 배지 도입 이후 전체 회차): 자동 배정 9건 중 7건이 이 경우였고 색인
# 지연은 최소 17~19분이었다 — 그 두 배 남짓을 여유로 잡은 값이다(HISTORY.md "늦게
# 색인된 기사가 초안·실시간 현황에서 통째로 빠지던 문제" 참고). 겹쳐 받은 기사는 URL
# 중복 제거로 걸러지므로 화면·건수는 그대로다.
SEARCH_LOOKBACK_MIN = 60

# [추가: 2026-09-03] 정기 회차 수집(app.scraper.collect_run)의 하한을 그 회차 시작보다
# 이 분 수만큼 더 앞으로 물린다 — 위 SEARCH_LOOKBACK_MIN이 초안·실시간 현황에 해준 것을
# 정기 회차에도 똑같이 해주는 값이다(그때 정기 쪽엔 같은 방어를 안 넣었다).
#
# 왜 필요한가: 회차 창은 (시작, 마감]이고 다음 회차의 하한은 정확히 이 회차의 마감이라,
# 마감 순간 네이버 검색이 아직 안 준 기사는 **어느 회차에도 못 들어가고 영영 사라졌다**
# (다음 회차는 pub_date <= after로 걸러낸다). 실측(2026-09-03 11:00 회차): 마감 시각에
# 6건이 빠졌고 3분 뒤에도 여전히 없었으며 18분 뒤에 6건 전부 나타났다.
#
# 값이 60인 이유: 실측 지연이 3~18분이라 여유가 크고, 초안·실시간 현황이 이미 쓰는
# SEARCH_LOOKBACK_MIN과 같은 값이라 앱 안에 "되돌아보는 폭"이 하나로 유지된다. 넉넉히
# 잡아도 부작용이 없다 — 오늘 앞 회차에 이미 실린 URL은 collect_run이 제외하므로
# 중복 게재가 원리적으로 0이기 때문이다(그래서 이 값을 키우는 쪽이 항상 안전한 방향).
COLLECT_LOOKBACK_MIN = 60

# [추가: 2026-09-03] 하루 마지막 회차만 수집 실행을 이만큼 늦춘다(app.scheduler).
# 시간창도 회차 이름(run_slot)도 그대로이고 **수집하는 시각만** 뒤로 민다.
# 마지막 회차는 위 COLLECT_LOOKBACK_MIN의 구제를 못 받는다 — 주워갈 다음 회차가 없고,
# 다음날 첫 회차는 전날 기사를 안 본다(_is_today_kst). 하루 끝이라 30분 늦어도 급할 게
# 없다는 판단(사용자 결정). 실증: 2026-09-02 19:30 회차가 앱 재시작 때문에 20:01에
# 수집됐는데 아무 문제가 없었다.
LAST_SLOT_COLLECT_DELAY_MIN = 30

# [추가: 2026-08-24] 실시간 현황 상단 "검색어 하나가 결과를 대부분 차지" 경고 배지의
# 문턱값 — 앱 버그가 아니라 담당자의 검색어 선택 문제라(HISTORY.md 같은 섹션 참고)
# 코드가 강제로 걸러내지 않고, 담당자가 스스로 알아채고 좁힐 수 있게 안내만 한다.
# 실측(2026-08-24, 등록 키워드 21개, 결과 1,968건): 처음엔 "코스피가 포함된 검색어
# 그룹(추가 검색어) 전체가 61%"라는 그룹 단위 숫자로 문턱을 잡았는데, 실제 배지가
# 봐야 할 건 그 그룹 안에서도 코스피 **하나**가 유독 넓다는 사실이라 키워드 단위로
# 다시 재보니 코스피 33.3%(656/1,968) — 2위인 '예산결산특별위원회'(23.2%)와도 10%p
# 차이로 뚜렷이 떨어져 있고, 그 아래로는 10%대부터 시작해 완만하게 줄어든다. 25%는
# 이 실측 분포에서 코스피만 걸러내는(2위는 안 걸리는) 값이다. 비율만으로 판단하면
# 이른 아침처럼 결과가 아직 적을 때(예: 5건 중 3건=60%) 우연한 쏠림까지 경고가 뜨므로,
# 최소 절대 건수도 함께 요구한다 — 100건은 위 실측 사례(656건)에 비해 충분히 작아
# 조기에 알리면서도, 정상적인 하루 초반 누적 규모보다는 커서 "우연한 쏠림"과 구분된다.
LIVE_HEAVY_KEYWORD_RATIO = 0.25
LIVE_HEAVY_KEYWORD_MIN_COUNT = 100

# [추가: 2026-08-20] 네이버 뉴스 검색 오픈 API 일일 호출 한도 — 개발자센터 애플리케이션
# 등록 시 기본으로 부여되는 값(네이버 개발자센터 "일일 사용량"에 항상 표시됨). 이 앱이
# 정할 수 있는 값이 아니라 네이버가 정한 상수라, [단독]·[속보] 알림 설정 화면의 예상
# 호출량 문구에도 그대로 노출한다(app.settings_server.render_breaking_alert_settings).
NAVER_DAILY_CALL_LIMIT = 25000
# [추가: 2026-08-20] 위 한도의 이 비율(%)에 닿으면 [단독]·[속보] 알림의 "폴링"만
# 자동으로 멈춘다(app.api_usage.should_pause_polling) — 정기 스크랩·실시간 현황·수시
# 모니터링은 절대 안 멈춘다(본업이 우선이고, 폴링은 부가 기능이라 먼저 양보한다,
# 사용자 결정). 80%는 "정기·수시가 갑자기 몰리는 날"의 여유를 20%p 남겨두는 값 —
# 감으로 고른 값이니 문제가 생기면 조정할 것(CODING_CONVENTIONS §1).
API_USAGE_WARN_RATIO = 0.8

# [추가: 2026-08-20] [단독]·[속보] 알림 폴링 주기(분)의 상하한·기본값. 3분 미만은
# 실측(CLAUDE.md "동시 호출 상한")상 위험 동시성 근처는 아니지만 호출 수만 불필요하게
# 늘어 하한으로 잡았다. 30분을 넘으면 "수시 감시"라는 취지가 무색해진다.
MIN_BREAKING_ALERT_INTERVAL_MIN = 3
MAX_BREAKING_ALERT_INTERVAL_MIN = 30
DEFAULT_BREAKING_ALERT_INTERVAL_MIN = 5
# [추가: 2026-08-20] 기본 감시 시간대 — 등록된 회차 시간대(기본 09:30~19:30)를 여유
# 있게 덮는 낮 구간. 실측(7일치 저장 기사 20건)상 0~9시 발행 말머리 기사가 2건뿐이라
# 새벽까지 덮을 실익이 낮다(HISTORY.md "[단독]·[속보] 기사 알림" 참고).
DEFAULT_BREAKING_ALERT_START = "09:00"
DEFAULT_BREAKING_ALERT_END = "20:00"

# [속보] 몰림 알림 — 짧은 시간에 여러 언론사가 [속보]를 내면 한 통 더 보낸다(app.breaking_burst).
# 기본값 30분·5곳은 실측(8/11~9/22 저장 회차, [속보] 있던 16일)에서 4일 울리고 넷 다
# 한 사건을 여러 언론사가 한꺼번에 보도한 날이었던 기준이다(HISTORY.md "[속보] 몰림 알림").
BURST_WINDOW_CHOICES = (20, 30, 60)
BURST_MIN_OUTLETS_CHOICES = (3, 4, 5, 6, 8, 10)
DEFAULT_BURST_WINDOW_MIN = 30
DEFAULT_BURST_MIN_OUTLETS = 5
# 한 번 울린 뒤 다시 울리지 않는 시간 — 같은 사건의 뒤따르는 보도로 또 울리지 않게.
BURST_COOLDOWN_MIN = 60
# 메시지에 이름을 적는 언론사 수 상한(넘으면 "외 N곳").
BURST_MAX_OUTLET_NAMES = 10
# 설정 화면 "지난 기록에 대 보면"이 훑는 날 수.
BURST_HISTORY_DAYS = 42

# [속보] 같은 사건 묶기 (app.alert_event) — 한 사건을 여러 언론사가 동시에 [속보]로 내면
# 알림이 그 수만큼 쏟아진다. 제목 토큰의 자카드 유사도가 이 값 이상이고 게시 시각이
# 창 안이면 같은 사건으로 본다. 실측(2026-09-23, 저장 회차 34일 203건): 0.3/0.4/0.5로
# 접히는 [속보]가 32·25·22건이고 [단독]끼리 걸리는 쌍은 어느 값에서도 0건이었다 —
# 넉넉한 0.3은 다른 사건을 묶을 위험만 키우므로 가운데 값을 쓴다.
ALERT_EVENT_SIMILARITY = 0.4
ALERT_EVENT_WINDOW_MIN = 60

# PRD.md 기능1 규칙 18 — 우선 Pretendard, 없으면 Noto Sans KR, 그래도 없으면 시스템 기본
FONT_STACK = "'Pretendard', 'Noto Sans KR', sans-serif"

# [추가: 2026-07-26] "뉴스가 잠잠"/"구름이 잠잠" 빈 상태 문구 전용 손글씨체(Gaegu, OFL 라이선스,
# Google Fonts). 인터넷 CDN에 의존하면 오프라인일 때 깨지므로, 필요한 글자(뉴/스/가/잠/구/름/이)만
# 골라낸 초소형 서브셋 woff2를 base64로 파일에 직접 박아 넣는다(모든 정적 화면이 자기완결적으로
# 열리는 이 앱의 원칙과 동일). 실제 @font-face 규칙은 각 렌더러의 스타일 블록에서 만든다.
CUTE_FONT_NAME = "Gaegu"
CUTE_FONT_BASE64 = (
    "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"
)




# PRD.md 기능1 규칙 5 — [추가: 2026-07-23] 기사 출력 줄 형식 템플릿 (URL 줄·날짜 미표시는 범위 밖)
DEFAULT_ARTICLE_LINE_TEMPLATE = "ㅇ ({outlet}) {title}"

# [추가: 2026-08-12] 소제목 형식 템플릿 — "메일머지" 기능의 두 번째 항목. {section}
# 자리표시자 하나만 치환한다. 기본값 <{section}>은 2026-08-12 이전까지 모든 소제목
# 이름에 자동으로 붙던 꺾쇠와 같은 모양이다 — 그때는 "누군가는 다르게 쓰고 싶을 수
# 있다"는 이유로 강제 적용을 없앴는데, 이번엔 사용자가 직접 켜고 고를 수 있는
# 설정값이라 같은 이유로 막을 필요가 없다(강제냐 선택이냐의 차이).
DEFAULT_SUBHEADING_FORMAT_TEMPLATE = "<{section}>"

# PRD.md 기능3 규칙 3 — [추가: 2026-07-23] 진입 화면 워드클라우드에 뽑을 최대 키워드 개수
# [수정: 2026-07-26] 12 -> 20 (사용자 요청) — 마우스 오버 시 실제 언급 횟수를 툴팁으로
# 보여주게 되면서, 크기만으로 헷갈리던 하위권 단어도 늘려서 보여줄 여지가 생겼다.
LANDING_KEYWORD_COUNT = 20

DATA_DIR.mkdir(exist_ok=True)
ARTICLES_DIR.mkdir(exist_ok=True)
