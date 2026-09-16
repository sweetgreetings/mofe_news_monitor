# CLAUDE.md

이 파일은 Claude Code가 이 저장소에서 일할 때 지켜야 할 **지금 유효한 규칙과 구조만** 담는다.

- **왜 이렇게 됐는지**(사고 경위, 실측 수치, 기각한 대안, 시행착오)는 [HISTORY.md](HISTORY.md)에 같은 항목명으로 있다. 규칙 끝의 「→ H: 항목명」이 그 자리다.
- **원칙**(왜 그렇게 하는가)은 [CODING_CONVENTIONS.md](CODING_CONVENTIONS.md). 새 기능을 만들거나 동작을 바꾸기 전에 먼저 읽는다. 특히 §1(추측하지 말고 잰다) · §3(담당자 작업은 조용히 사라지지 않는다) · §4(화면 전용 표시가 보고서로 새면 안 된다).
- 2026-09-16 이전의 긴 판(배경 서술 포함)은 [archive/CLAUDE_2026-09-16_정리전_전체본.md](archive/CLAUDE_2026-09-16_정리전_전체본.md)에 그대로 있다. 이 파일에서 빠진 설명이 필요하면 거기서 찾는다.
- **이 파일에는 날짜 태그(`[수정: …]`)와 경위를 쓰지 않는다.** 규칙이 바뀌면 규칙 문장을 고치고, 경위는 HISTORY.md에 쓴다.

## 문서 지도

| 문서 | 역할 |
|---|---|
| CLAUDE.md | 지금 유효한 규칙 (이 파일) |
| [HISTORY.md](HISTORY.md) | 변경 경위·실측·기각안 |
| [CODING_CONVENTIONS.md](CODING_CONVENTIONS.md) | 설계·UX·검증 원칙 |
| [PRD.md](PRD.md) / [prd_lite.md](prd_lite.md) | 기획서 (기능 번호 "기능N 규칙M"의 출처) |
| [DESIGN.md](DESIGN.md) | 초기 설계 문서 |
| [ADHOC_DESIGN.md](ADHOC_DESIGN.md) | 수시 모니터링 설계 근거 (§ 번호로 인용됨) |
| [MORNING_ISSUES_DESIGN.md](MORNING_ISSUES_DESIGN.md) | 실시간 우선 구조 설계 근거 |
| [AI_RULES.md](AI_RULES.md) | LLM 시스템 프롬프트 **원본** (코드가 이 파일을 직접 읽는다 — 고치면 바로 반영) |
| [LLM_COST_USAGE.md](LLM_COST_USAGE.md) | Claude API 호출 지점·비용 |
| [MULTIUSER_ROLLOUT_CHECKLIST.md](MULTIUSER_ROLLOUT_CHECKLIST.md) | 다중 사용자 확장 시 준비 항목 (현재 구조엔 해당 없음) |
| [README.md](README.md) | 사용자용 소개·설치 |
| `mockups/` | 결정된 화면 시안. 규칙 옆 「시안」 링크가 가리킨다 |

## 프로젝트 개요

재정경제부 언론 모니터링 담당자 한 명이 로컬에서 쓰는 웹 앱. 설정된 검색어 그룹으로 네이버 뉴스를 주기적으로 모아 소제목별로 묶고 `localhost`에서 보여준다.

- 실행: `python3 main.py` (스케줄러 + 설정·큐레이션 서버 + 브라우저 열기). `--no-browser`는 자동 실행용.
- 테스트: `python3 tests/test_integration.py`. 린터·타입체커 없음.
- 의존성: `requirements.txt` — `requests`, `python-dotenv`, `anthropic`, `openpyxl`.
- 운영 환경: macOS(자동 실행은 macOS LaunchAgent). 파일명은 Windows에서도 쓸 수 있게 `:`를 쓰지 않는다(`09:00` → `09-00`). 사용자 1명, 로그인 없음.
- 범위 밖: 다른 뉴스 소스, 계정·로그인, 원격·모바일 접속.

## 화면 용어 — 내부 용어와 분리한다

코드·데이터·URL의 내부 용어는 **정기 / 수시**(`/adhoc`, `data/adhoc/`, `include_in_scrap`, `kind: "collect"|"bundle"` …)이고 바뀌지 않는다. 바뀌는 건 화면 글자뿐이다.

| 정기 모니터링 | 수시 모니터링 |
|---|---|
| 검색어 (`/keywords`) | 새 수집 (`/adhoc/new`) |
| 초안 (`preview.html`) — 실시간현황이 옆에 붙음 | 원본 (`/adhoc/card`, `kind: collect`, 로데이터) |
| 확정본 (`index.html`) | 확정본 (`/adhoc/card`, `kind: bundle`) |
| 정기 보관함 (`history.html`) | 수시 보관함 (`/adhoc`, 확정본만 쌓임) |

- 파일명 종류 자리: `언론모니터링` / `수시모니터링`.
- 라벨 출처 태그: 정기는 `회차`, 수시는 사안명만.
- **본업 화면은 자기를 해명하지 않는다.** "이 숫자엔 수시가 안 섞였다" 같은 한정어를 붙이지 않는다.
- **흐름 이름(`정기`/`수시`)은 두 흐름이 한 화면에 나란히 설 때만 붙인다**: 홈 흐름도의 줄 이름표, 두 보관함 이름, 라벨 보관함 빈 화면 안내(`정기 초안·확정본, 수시 확정본 화면에서 …`). 홈 흐름도 안에선 보관함 칸을 둘 다 `보관함`이라 쓴다(줄이 이미 말한다).
- 수시 화면에는 정기를 가리키는 말이 하나도 없다.
- `사안`이라는 말은 카드가 다루는 대상의 이름(`사안명` 등)으로만 쓴다.
- 「오늘의 수집 결과」 같은 일반 서술, 새 카드를 만드는 동작 버튼(`새 수집 시작`, `+ 새 수집`)은 화면 이름이 아니라서 위 표를 따르지 않는다.
- 「정기 스크랩」이라는 말이 화면에 남는 곳은 `/breaking-alert`의 API 호출량 안내 한 곳뿐이다.
- 기각한 이름: `정기함`/`수시함`·`결과`·`보고용`·`수집 원본`/`수집 확정본`(옛 이름). → H: 화면 용어

## 구조

FastAPI·SQLite·APScheduler를 쓰는 PRD 원안은 폐기됐다(DESIGN.md §3).

- **웹 프레임워크 없음.** 파이썬이 정적 HTML(`index.html`, `history.html`, `home.html`, `preview.html`, `live.html`)을 다시 만든다.
- **예외 하나: `app/settings_server.py`** — `http.server`만 쓰는 `127.0.0.1:8765` 서버. 설정 저장·큐레이션을 처리하고 정적 화면도 같은 출처로 서빙한다(브라우저가 `http://` → `file://` 이동을 막으므로).
  - **이 포트가 곧 중복 실행 자물쇠다.** `main.py`가 데이터를 건드리는 어떤 단계보다 먼저, main 스레드에서 동기로 bind한다. `OSError`면 이미 켜진 앱의 홈만 브라우저로 열고 바로 끝낸다. PID 파일은 쓰지 않는다(강제 종료 시 죽은 자물쇠가 남는다). **새 백그라운드 작업은 반드시 이 자물쇠 뒤에 둔다.** → H: 앱 중복 실행
- **저장소: `data/` 아래 JSON 파일.** SQLite 없음. 쓰기는 `app/atomic_write.py`.
- **로깅** (`app/logging_setup.py`, `data/logs/app.log`): `main()` 첫 줄에서 한 번 설정. 터미널은 WARNING 이상, 파일은 INFO 이상, 하루 회전, 14일 보관. 로그 폴더를 못 만들면 파일 로깅만 포기한다. 테스트는 설정하지 않는다.
- **외부 API**: 네이버 뉴스 검색(`.env` 또는 설정 화면 `/naver`), 텔레그램(`TELEGRAM_BOT_TOKEN`, 선택), Claude API(`ANTHROPIC_API_KEY` 또는 `/llm`, 선택). 키 저장 우선순위는 설정 화면 값 > `.env`(`app/credentials.py`). **키가 없거나 호출이 실패해도 앱은 멈추지 않는다** — 경고 로그만 남기고 건너뛰거나 규칙 기반으로 폴백한다.
- **비밀값**: `.env`·`data/`는 `.gitignore`에 있고 커밋하지 않는다.

### 회차 파일

- 경로 `data/articles/{run_at의 날짜}_{run_slot}.json`. **파일명을 만드는 곳은 `app/storage.py` `save_run` 하나뿐**이고 날짜는 `run_at`에서 뽑는다(`_date_from_run_at`). 나머지(`update_run_articles`/`confirm_run`/`record_send`/`app.undo`)는 `_find_run_file`로 기존 경로를 찾는다.
- 조회는 파일명이 아니라 `run_at`·인덱스를 본다. **파일명에 의존하는 유일한 함수는 `run_exists(run_slot, date_str)`**(스케줄러의 "이미 돌았나")다 — 이 규칙이 깨지면 회차가 누락되거나 덮인다. → H: 회차 파일 날짜 불일치
- **메타 인덱스** (`app/run_index.py`, `data/articles_index.json`): `run_at`/`run_slot`/`confirmed`/기사 수 캐시. 진실은 항상 회차 파일이고, 조회마다 `(mtime_ns, size)`로 바뀐 파일만 다시 읽는다. 깨지면 통째로 다시 만든다. 정렬 키에 `run_slot` 동점 처리가 있다(같은 초에 저장된 두 회차).
- **테스트 격리**: 폴더 경로는 `app.storage.ARTICLES_DIR`를 호출 시점에 읽는다. `app.config`에서 직접 import하면 테스트의 `patch.object`를 못 따라가 실제 `data/`를 건드린다.
- 회차 전체를 파싱하는 `list_all_runs()`는 지금 쓰는 곳이 없다. 대개 `list_run_meta()`나 `list_runs_for_date()`로 충분하다.
- `save_run`은 저장마다 파일명·회차·기사 수를 INFO로 남긴다.

### 스케줄러 (`app/scheduler.py`)

- 10초 폴링 루프. 회차는 `{start, end}` 시간창(1~10개, 분은 `:00`/`:30`만). `end`가 회차의 정체성(파일명·헤더), `start`는 수집 하한.
- **회차 그룹**(최대 5, 예: 주중/주말) — 그룹마다 `enabled`와 `days`. 켜진 그룹이 1개면 요일 무관하게 그것(휴일 예외용), 2개 이상이면 오늘 요일을 포함한 그룹, 없으면 첫 켜진 그룹(`app/settings.py` `pick_active_group_index`). 켜진 그룹 ≥1, 같은 요일을 두 그룹이 가질 수 없다.
- **오늘 놓친 회차는 전부, 오래된 것부터 보충한다** (`find_slots_to_run`). 순서가 계약이다 — `collect_run`의 `_already_published_urls`가 앞 회차 기사를 걸러내므로 최신부터 돌리면 앞 회차가 기사를 뺏긴다.
- 회차 하나가 실패해도 나머지는 간다. 같은 회차는 하루 3번(`MAX_RETRIES`)까지만 시도하고, 이 횟수는 메모리에만 둔다(앱 재시작 시 초기화).
- 여러 회차를 한꺼번에 채워도 **자동 발송은 가장 최근 회차만** 한다.
- 어제 이전은 채우지 않는다(네이버 API로 불가능).
- **하루 마지막 회차만 수집을 30분(`LAST_SLOT_COLLECT_DELAY_MIN`) 늦춘다.** 회차 이름·시간창은 그대로. 보충 실행은 즉시 돌고, 늦춘 시각이 자정을 넘기면 늦추지 않는다. 다음날 첫 회차가 줍게 하는 안은 기각.

### 자동 실행 (`autostart/`)

- macOS LaunchAgent. **설치는 담당자가 직접** `bash autostart/install.sh`로 한다.
- `KeepAlive`는 `{SuccessfulExit: false}` — `true`면 중복 실행 자물쇠로 정상 종료한 경우까지 무한 재시작한다.
- **앱 폴더는 `~/press-monitor`.** 데스크탑·서류·다운로드 폴더는 macOS가 백그라운드 접근을 조용히 막아 자동 실행이 안 된다. `install.sh`는 보호 폴더면 경고하고, 등록 뒤 실제로 떴는지 확인해 실패하면 종료 코드 1을 낸다. 폴더를 옮기면 `install.sh`를 다시 돌린다. `~/Desktop/my_app`은 이 폴더로 가는 심볼릭 링크다(`BASE_DIR`가 `resolve()`라 한 곳으로 모인다).
- 컴퓨터가 잠들어 있으면 여전히 못 모은다(`sudo pmset`은 담당자 몫). 놓친 회차는 되살릴 수 없으므로 소급 수집 기능은 없다.

## 정기 모니터링 — 수집

### 검색어 그룹 (`/keywords`)

- 그룹 최대 8개(`MAX_KEYWORD_GROUPS`), 그룹당 키워드 최대 10개. 그룹 사이는 OR, 그룹 안은 그룹별 OR/AND. 모든 그룹이 편집·삭제 가능.
- **그룹마다 목적지 스위치가 둘, 서로 독립이다.** `실시간`(`include_in_live`) / `초안·확정본`(`include_in_scrap`). 네 조합 모두 유효하고, 둘 다 끄면 그룹 꺼짐(`꺼짐` 칩, 흐리게, `disabled_keywords` 보존).
- **저장을 막는 규칙은 「초안·확정본을 켠 그룹 ≥1」 하나뿐.** 문구 `"초안·확정본"을 켠 그룹이 최소 1개는 있어야 합니다.`는 화면 JS `blockingReason`과 `validate_keyword_groups`에서 같아야 한다.
- **읽는 쪽은 반드시 `app.settings`의 `group_in_live`/`group_in_scrap`/`group_in_use`를 거친다.** 옛 형식 dict도 옛 규칙으로 읽어준다. `load_settings`의 이관: 옛 `enabled=False`는 정기 체크가 있어도 초안·확정본을 끈 채로 옮긴다. `enabled`는 `live or scrap`을 저장해 두는 파생값이다(알림 감시 그룹 목록·추이 화면 검색어 칩이 이 뜻으로 읽는다).
- 화면 편집은 메모리에서만, **「저장」에서만 `POST /save`**(`_KEYWORD_GROUPS_SCRIPT`). 상태 안내는 하단 저장 바 하나. 캡션: `저장하면 실시간·초안엔 바로, 확정본엔 이번 회차 마감부터 반영됩니다.`
- 맨 위 안내는 한 줄: `키워드를 누르면 지우지 않고 잠시 빼고, 두 번 누르면 글자를 고칩니다.`
- 같은 그룹 안 중복 키워드는 조용히 하나로 합친다(첫 자리 유지). 다른 그룹 간 중복은 허용하고 노란 테두리로만 알린다.
- 카드 모양: 머리줄 `[그룹명][꺼짐][실시간 ◯][초안·확정본 ◯][🗑]`(스위치 `.dest-sw`, 켜진 쪽 이름표가 진해짐, 스위치+🗑은 `.head-ctrl`로 함께 줄바꿈) + 칩 + 아랫줄 `[1개라도 포함 | 모두 포함]` ↔ `6/10 · N개 뺌`. 카드 두 장씩은 창 폭 780px 초과일 때만. 그룹명 칸 규칙은 `.group-head .group-name-input`으로 명시도를 올린다. 누를 때 전체를 다시 그리지 않는다(포커스 유지).
- 값이 안 변하는 「실시간」·「정기」 배지는 달지 않는다. 시안 [mockups/KEYWORDS_TWO_SWITCH_MOCKUP.html](mockups/KEYWORDS_TWO_SWITCH_MOCKUP.html). → H: 검색어 — 그룹 스위치 + 정기 체크 → 목적지 스위치 두 개

### 네이버 검색 (`app/naver_api.py`)

- `search_articles_by_groups` = `search_keywords` + `match_articles_to_groups`. 그룹 간 같은 키워드는 한 번만 검색.
- **동시 검색 8개**(`_MAX_CONCURRENT_KEYWORD_SEARCHES`, 실측 상한). 정기·수시·알림을 통틀어 전역 `_GLOBAL_REQUEST_SEM`(8)이 HTTP 요청 하나하나에 걸린다.
- 실패한 키워드는 3번 재시도(429는 `Retry-After` 따름) 후 **`failed_keywords`로 보고한다 — 빈 결과로 바꿔치지 않는다**("오늘 0건"과 구별돼야 함). `collect_run`은 실패가 있으면 예외를 올려 회차 전체 재시도(3회 × 5분)로 넘기고, 실시간현황은 그 키워드 캐시를 건드리지 않고 경고 배너를 띄운다.
- 당일(KST) 기사만, 건수 상한 없음.
- **키워드당 최신 1000건이 네이버 한계**(`_MAX_START`)이고 기간 파라미터가 없어 날짜는 최신순 페이지를 넘기며 직접 거른다. 한계에 닿으면(`start > _MAX_START`) 결과가 조용히 잘리므로 WARNING 로그를 남긴다. 화면 표시는 아직 없다. `_search_one_keyword`는 이 판정을 `capped`로 돌려준다.
- `pubDate`를 못 읽은 기사는 건너뛴다. 예외: 실시간현황만 `include_unparsed_dates=True`로 `pub_date: None`을 담아온다.
- 잘린 제목("...") 재요청(`_fetch_full_title`)은 시간창 안 기사만 모아 동시 10개(`_TITLE_REFETCH_CONCURRENCY`)로. 네이버가 아닌 언론사 사이트라 전역 세마포어와 별개다.
- **언론사 판별** (`resolve_outlet`): 네이버 링크의 **oid**로 `NAVER_OID_OUTLETS`에서 확정하고, 없을 때만 원본 도메인으로 추정한다(도메인은 계열 매체를 못 가른다). **oid 표는 실측으로만 채운다**(`og:article:author` 확인). 설정 목록에 있는 매체는 그 목록의 이름 표기를 그대로 쓴다(비교가 문자열이다). 표에 목록 밖 매체가 있어도 체크박스 목록은 안 늘어난다.
- **매일경제 빨간 표식**: `outlet_display_label(outlet, url)` — 매일경제인데 oid로 확정 안 됐을 때만(매경이코노미일 수 있음). 적용: `render_article`, 실시간 `_render_row`, `render_hidden_page` — 셋 다 URL을 넘긴다. 저장값·복사 텍스트엔 안 들어간다.

### 회차 수집 (`app/scraper.py` `collect_run`)

- 각 회차는 `(start, end]`에 발행된 기사를 모은다. **실제로는 `start`보다 60분(`COLLECT_LOOKBACK_MIN`, 당일 0시가 바닥) 앞부터 훑는다** — 마감 순간 네이버가 아직 색인하지 않은 기사를 다음 회차가 줍기 위해서다.
- **다시 딸려온 앞 회차 기사는 `_already_published_urls`(오늘 저장된 회차들의 URL)로 뺀다. 이 제외는 화이트리스트·정렬·중복제거보다 먼저 돈다.** 대조는 URL로만. 그래서 이 값은 넉넉할수록 안전하다(튜닝 노브가 아니다).
- 회수된 기사에는 `late_pickup`/`late_pickup_slot`을 달아 저장하고, 1건이라도 있으면 INFO 로그.
- 순서: 숨김 제외(`filter_hidden`) → 화이트리스트 → 정렬 → 사진·인사 제외(설정 시) → 제목 중복 제거 → `classify_for_finalize`.
- **저장한 회차 파일을 다시 읽어 돌려준다.** 필드를 손으로 나열해 돌려주면 `confirm_run`이 파일을 덮어쓸 때 새 필드가 사라진다. → H: AI 분류 실패가 조용히 묻히던 3중 결함
- `matched_keywords`(기사가 걸린 검색어 전부)를 기사에 저장한다. 그 회차 수집 당시 기준이고 나중에 안 바뀐다.
- 오늘 최신 회차가 없으면 `index.html`은 「아직 스크랩 시간 전입니다」 안내(`generate_waiting_page`).
- 빈 회차 → 「💤」. 헤더 `언론 모니터링 [HH:MM] 기준`(회차 시각).

### 중복 제거·정렬

- **같은 제목은 하나만 남긴다**(`deduplicate_by_title`, 정렬 후 첫 기사). 언론사·판본이 달라도 제목이 다르면 둘 다 둔다.
- **정렬은 완전 정렬**: `app/sorter.py` `sort_by_outlet_priority` = `(언론사 순위, 발행시각 최신순, URL)`. 정기·초안·수시가 모두 이 함수를 쓴다. 결정론적이어야 초안과 확정본이 같은 대표 기사를 고른다. 발행시각이 없으면 그 언론사 안에서 맨 뒤. → H: 초안에서 숨긴 기사가 확정본에서 되살아나던 문제
- 이 순서는 **출발 순서일 뿐**이고 `sort_scoop_first`와 담당자의 ↑↓가 뒤에 적용돼 항상 이긴다. 수집 뒤 어떤 단계도 언론사순으로 다시 정렬하지 않는다.
- 언론사 순위: 방송(KBS, MBC) → 고정 주요 언론사 목록 → 나머지(버리지 않고 뒤로). 설정에서 화이트리스트를 고르면 체크한 곳만, 고른 순서대로(`save_outlet_selection`, `filter_by_outlet_whitelist`).
- `/outlets` 순서 버튼: `move_outlet(name, up|down|jumpup|jumpdown)`, jump는 5칸이고 끝에서 멈춘다. 네 버튼 같은 색(`muted`), jump 쌍만 테두리. 옮긴 줄은 세이지 1.2초.
- 부활 문제를 제목 대조로 막지 않는다. 남은 구멍: 실시간현황은 중복 제거를 안 하므로 거기서 대표 아닌 쪽을 숨기면 확정본엔 대표가 뜬다(그대로 둔다).

### 사진·인사 기사

- `/scrap-page`의 `exclude_photo_in_scrap`/`exclude_personnel_in_scrap`, **둘 다 기본 꺼짐.** 꺼둔 채로 쓰는 게 기본 워크플로다(판정이 틀려도 화면에 남아 담당자가 처리할 수 있다).
- **사진 판정은 `app/filters.py` `looks_like_photo_caption` 하나.** 화면 배지와 수집 제외가 같은 판정을 본다.
  1. 표식 층: 제목의 괄호 말머리 `[포토]` 계열(`PHOTO_TAG_RE`, 위치 무관) + 매체 표식 `[헤럴드pic]`만. **`pic`/`픽` 일반으로 넓히지 않는다**(일반 기사 표식과 겹친다). 새 매체 표식은 실측으로 전부 캡션인 것만 더한다. 부분일치 대조는 쓰지 않는다.
  2. 추정 층: 요약 신호(재판매 문구·사진기자단·기자 이메일·촬영 날짜 도장·연도에서 잘린 도장·도장 없이 `…[이/가] N일 … 하고 있다.`로 끊김)와 제목 모양 신호(`~하는 + 직책`, `~하는 + 이름-이름`(**하이픈만**), 받침ㄴ 서술어 + 이름 + 직책, `~하는 + 한글 3자 이름 + 후보(자)`).
  - 요약 신호는 **요약의 끝**에 있을 때만 인정(`_summary_signal_at_tail`, 뒤에 한글 10자 이상 이어지면 본문으로 본다).
  - `[속보]` 기사는 어느 층에도 안 걸린다.
  - **새 신호는 전수 실측으로 오탐 0을 확인한 뒤에만 더한다.** 실측 기록은 HISTORY.md "사진기사 추정 필터".
- **꺼둔 상태 표시**: 확정본·초안(`render_article` 공용)에서 행을 흐리게(`opacity:0.55`, `.article.is-photo`) + `📷 사진 추정` 칩. hover·체크 시 또렷. **라벨은 표식이든 추정이든 「📷 사진 추정」 하나**이고 차이는 툴팁만(`photo_badge_tip(title)` 한 곳, 실제 붙은 말머리를 적는다). 두 번째 라벨을 만들지 않는다.
- **「📷 사진 추정 모아 보기 (N)」** — 확정본·초안(툴바), 수시 원본·확정본(목록 바로 위 줄). 0건이면 안 그린다. 사진 추정 행만 한 목록으로 모으고 행마다 원래 소제목 이름표(`.pg-from`, 미분류는 없음). 흐림·칩은 뗀다. sticky 띠 「☐ 전체 선택 · … · [원래 화면으로]」, 숨기기는 선택 바의 🗑(확인창). **화면만 바꾸는 보기**라 저장·복사·발송은 안 변한다. 모아 보는 동안 다른 소제목·📌·AI 요약·메모·배너·목차·↑↓는 숨고 「보기 순서」는 소제목별로 잠근다. 끄면 행이 정확히 제자리로 돌아간다. 상태는 탭 단위 `sessionStorage`(`photoGather:{날짜}:{회차}` / `photoGather:preview:{ROUND_DEADLINE_MS}` / `adhocPhotoGather:{카드 id}`); 수시는 스크롤 복원보다 먼저 되살린다. 코드는 정기 `app/renderer.py` `photo_gather_style/script/button_html`, 수시 `app/adhoc/renderer.py` `_PHOTO_GATHER_JS` — **동작을 바꾸면 둘 다 바꾼다.** 소제목에서 사진을 늘 빼두는 안은 기각(화면과 보고서가 어긋난다). 시안 [mockups/PHOTO_GROUP_MOCKUP.html](mockups/PHOTO_GROUP_MOCKUP.html).
- 켜두면 판정된 기사가 수집에서 빠져 화면에 없다(확인하려면 설정을 꺼야 한다).

### 초안에 보인 기사 유지 (`app/draft_seen.py`, `data/draft_seen.json`)

- **담당자가 숨기거나 조건을 바꾸지 않는 한, 초안에 한 번 보인 기사는 그 회차 초안에 남고 확정본에도 들어간다.**
  1. 같은 제목이면 먼저 보인 쪽이 대표를 지킨다(`deduplicate_by_title(..., prefer=seen_order)`, 순번은 1, 2, 3…).
  2. 조건을 바꾸면 다시 거른다 — 언론사·사진/인사 제외·숨김·이미 실림은 붙잡은 기사에도 같은 자리에서 걸린다. 검색어가 바뀌면 목록을 새로 시작한다(`search_condition(groups)`).
  3. 네이버 검색에서 사라져도 남긴다(기사 dict 전체 저장).
- 합치는 자리는 초안 `_compute_preview_articles`와 확정본 `collect_run`의 "이미 실림" 제외 **바로 앞**, 회차 창 밖 기사는 안 넣는다. 한 회차 분만 담는다. 숨겨진 기사도 목록에서 지우지 않는다. "위로" 예약 기사(pending)는 제외.
- 빠진 기사 로그(`_log_draft_drops`): 알려진 이유는 INFO, 이유를 모르면 WARNING(이 약속이 새고 있다는 뜻).
- 실시간현황의 `🤖 자동 선별`은 이 목록을 안 본다.

## 정기 모니터링 — 실시간현황 (`app/live_renderer.py`, `/live.html`)

- 요청마다 새로 그린다(수동 새로고침만). `실시간` 스위치를 켠 그룹을 오늘 0시~지금 검색, 최신순. 언론사·제목·URL·"N분 전"(클라이언트 계산)·형광펜. 소제목·AI 요약 없음.
- **필터 없음이 규칙이다.** 제목 중복 제거·사진/인사 제외·화이트리스트 모두 미적용, 숨긴 기사도 빼지 않고 「🗑️ 숨김」 표시만. 결과를 좁히는 건 (1) 활성 키워드 (2) 그룹 OR/AND (3) 당일 (4) 같은 URL 1건뿐. 선별은 전부 클라이언트 필터 바가 맡는다.
- 발행시각을 못 읽은 기사는 목록·누적 건수에서 빼고 맨 아래 「⚠️ 발행시각을 못 읽은 기사」 구역에 모은다(추정 시각 안 씀). 0건이면 배너·구역 모두 안 그린다. 그 행도 `.live-row`라 필터에 걸린다.
- **행 버튼 상태** (`_render_row`, 우선순위 hidden > scrapped > pinned > auto-drafted > normal): `📌 담아두기`(오늘 회차 ≥1일 때 활성) / `📌 담아둠`(다시 누르면 해제, `unpinFromLive`) / `✔️ 스크랩됨`(비활성) / `🗑️ 숨김`(다시 누르면 숨김만 해제, `unhideFromLive`) / `🤖 자동 선별`(비활성, `_auto_drafted_urls` 근사치 — 규칙 기반이라 AI가 아니다). 상태 버튼(`.add-btn`) `min-width: 84px`(가장 긴 라벨 폭 — 옆 버튼이 흔들리지 않는 하한). 그 오른쪽에 `복사` 글자 버튼(`.copy-btn`, `copyArticleIcon`), `⋯`(🔄/✏️), 🗑.
- **숨김 여부는 `is_hidden()`으로만 판정한다**(확정본 필터와 판정이 갈리면 안 된다).
- 📌 담아두기 → 확정본·초안의 「📌 담아둔 기사」 구획(당일만, 자체 ↑↓, 복사·txt에서 제외, 자정에 비움). 승격은 화면별 소제목 드롭다운(`초안에 넣기…`/`확정본에 넣기…`), 고를 소제목이 없으면 📥 버튼으로 서버 자동 분류.
- **특수조건 필터 줄**(항상 렌더링): `선택 언론사만`, `[단독]`, `[속보]`, `[사설]`, `아직 처리 안 한 기사만`. **다섯 다 「~만 보기」 방향이고, 「~빼고 보기」는 넣지 않는다.** 서로 AND, **말머리 셋끼리만 OR.** `아직 처리 안 한 기사만` 툴팁은 넷을 빠짐없이: `숨김·스크랩됨·담아둠·자동 선별 — 담당자나 앱이 이미 손댄 기사를 모두 보이지 않게 합니다`.
- **말머리 판정**: `[단독]`/`[속보]`는 제목 **맨 앞**만, 괄호 변형 허용(`_headline_kind`, `HEADLINE_TAG_RE`). 배지 없이 제목 글자색(`.t-scoop` 진분홍, `.t-flash` 빨강). **`[사설]`은 따로다** — `app/filters.py` `EDITORIAL_TAG_RE`/`is_editorial`, 위치 무관, 괄호 안이 정확히 「사설」일 때만, 행 속성 `data-editorial`. **`HEADLINE_TAG_RE`에 「사설」을 넣지 않는다**(텔레그램 알림·색·정렬·사진 예외까지 번진다). 사설은 색·정렬을 안 건드린다.
- **그룹 필터 줄**(그룹이 있을 때): 제목 검색칸, 그룹 칩(OR, 건수, 이름은 CSS 말줄임 + `title`), 「키워드별 건수」 펼침. `matched_keywords`는 캐시된 원시 결과로 매 렌더링 계산(기사에 저장 안 함). 모든 필터는 클라이언트 `applyLiveFilters`.
- **넓은 키워드 경고**(`_heavy_keyword_warning`): 한 키워드가 전체의 25% 이상이면서 100건 이상이면 앰버 배너(`.live-heavy-kw-warning`). 자동으로 좁히지 않는다.
- 행의 `🔍 검색어`(`.kw-inline`, 게시시각 뒤·📷 배지 앞, `margin-left: 6px`), 행의 그룹 태그(`.row-group-tag`).
- **키워드별 증분 캐시** (`app/live_cache.py`, `data/live_cache.json`): 키워드별 원시 결과 + 키워드별 last-seen. 새 키워드는 0시부터, 기존 키워드는 **last-seen과 "지금 − 60분"(`SEARCH_LOOKBACK_MIN`) 중 이른 쪽부터** 검색(`incremental_search_after`, 초안과 공용 — 네이버가 발행순으로 색인하지 않는다). 저장하는 last-seen은 가장 최신 `pub_date` 그대로, 되돌아본 구간은 URL로 중복 제거. 이름·그룹·OR/AND 변경은 재검색 없음. 실패한 키워드는 원래 last-seen(`stored_last_seen`)을 유지한다.
- **콜드 스타트**: 오늘 한 번도 안 검색한 키워드가 있으면 요청을 막지 않고 백그라운드 검색(`_ensure_live_generation_running`, 모듈 잠금으로 1개만)을 시작하고 3초마다 새로고침하는 「기사를 불러오고 있습니다」 화면을 준다. 그 화면의 새로고침 버튼은 `href` 없는 `<span>`.

## 정기 모니터링 — 초안 (`app/preview_renderer.py`, `preview.html`)

- 요청마다 다시 그린다. 검색 창의 바닥은 **회차 시작 − 60분(`COLLECT_LOOKBACK_MIN`, 당일 0시 하한)** — 확정본과 **같은 창**을 봐야 한다. 캐시 서명(`_preview_keywords_signature`)에 `lookback_min`이 들어간다. 앞 회차에 실린 기사는 `_already_published_urls`로 빼고, 회수분엔 `late_pickup` 표식(칩 CSS는 `late_badge_style()` 공용). → H: 초안이 확정본보다 좁은 창을 보던 문제
- `render_article`을 `app/renderer.py`에서 가져다 쓴다. 공용 CSS·JS도 `_theme()`으로 받는다.
- **회차 종료 카운트다운**: 회색 `⏱️ 수집 종료시간까지 hh:mm:ss 남음`(`tabular-nums`), 10분 이하면 빨강(`.urgent`).
- **회차 마감 배너**: 0에 닿으면 `#round-over-banner`를 상단 고정(z-index 21)으로 — 「H시 M분 회차가 마감됐어요. …확정본에는 반영되지 않아요.」 + `스크랩 확정본 열기 →` + `나중에`(`sessionStorage` `roundOverDismissed:{ROUND_DEADLINE_MS}`). `body.round-over`가 `.topbar`를 `--round-banner-h`만큼 민다.
- **「미분류 N건」 배지**(제목 옆, 초안 전용, 0건이면 안 그림): 누르면 누르는 시점 DOM의 `.subheading-unclassified`로 스크롤. 색은 그 칸의 `.unclassified-count`와 같다.
- **규칙 기반 폴백 경고**(초안 전용): 분류 직후 `llm_classifier.last_classification_was_rule_based()`를 읽어 `.classify-degraded` 박스. 기사 5건 미만이면 안 띄운다. 문구는 원인별 셋(`_classify_degraded_html`, 이 순서로 우선): (1) 기사 150건 초과 → 몇 건 줄이라고 숫자로 (2) 이번 회차 직접 재분류가 연속 2회 이상 실패(`app/reclassify_attempts.py`, 메모리) → 이름을 직접 고치길 권하고 시도 이력 (3) 일반 → 두 줄. **배너에 실행 버튼을 두지 않는다** — 툴바 버튼을 `.btn-ref` 칩으로 가리킨다.
- 폴백 상태에서 **복사·다운로드는 한 번 더 묻는다**(나갈 소제목 이름을 보여줌). `CLASSIFY_DEGRADED`/`CLASSIFY_DEGRADED_NAMES`를 `PLAIN_TEXT`와 같은 자리에서 선언하고 `previewMoveArticle`이 갱신한다.
- 새로 들어온 기사 노랑: 아래 "기사 행 상태색" 참고.

## 정기 모니터링 — 소제목 분류

### 규칙

- 회차마다 기사 **요약**(제목 아님)에서 새로 만든다. 최대 15개(`MAX_SUBHEADINGS`), 기사 하나는 소제목 하나. 담당자의 수동 배정이 이긴다. 밀도 규칙(묶음당 평균 2건 이상)은 AI_RULES.md.
- **LLM 분류** (`app/llm_classifier.py` `classify_with_llm`, 모델 `LLM_MODEL`=`claude-haiku-4-5`): 쟁점 단위로 묶고 같은 호출에서 소제목 요약도 쓴다. 애매한 기사는 주저 없이 「기타」. 프롬프트 원본은 AI_RULES.md.
- **폴백**: API 키 없음·패키지 없음·기사 수가 `[2, 150]` 밖·호출 실패 → 규칙 기반. 예외는 삼키고 경고 로그만.
- 요약은 3문장·250자 이하, 문장 중간에서 자르지 않는다. 폴백이면 최우선 기사 요약을 같은 한도로 자른다(`summarize_group`). 요약은 `group_summary`로 회차 스냅샷에 저장된다.
- 하단 블록은 `💬 AI가 읽은 소제목별 주요 요약` 하나(화면 전용). 「🤖 AI가 추출한 주요 키워드」 블록은 없앴다 — 검색어로 걸러진 기사의 빈도는 검색어 주변 단어만 돌려준다. 토크나이저(`app/tokenizer.py`)는 워드클라우드가 계속 쓴다.

### 언제 API를 부르나 (비용 통제)

- **확정본은 다시 분류하지 않는다.** 회차 파일의 기사별 `group`을 읽는다(`groups_from_snapshot`/`groups_for_confirmed_run`). 분류를 바꾸는 큐레이션은 `snapshot_group_names`로 스냅샷을 다시 쓰므로 화면과 내보내기가 같은 값을 본다.
- **`snapshot_group_names`는 숨긴 기사를 분류 입력에서 빼고**(`filter_hidden`) 돌려줄 때 이전 `group`으로 다시 붙인다. 입력 기사 집합이 어긋나면 캐시를 놓쳐 이름이 전부 바뀐다.
- **초안은 회차당 한 번만 자동 분류한다**(기사 5건 이상 도달 시). 그 소진 기록은 파일에 남는다(`app/auto_classify_turn.py`, `data/auto_classify_turns.json`, 자정 초기화) — 메모리에만 두면 재시작마다 전체 재분류가 다시 열린다. 파일을 못 쓰면 소진한 것으로 친다.
- 그 뒤는 담당자가 누를 때만:
  - **AI 기사 배정** (`/assign-unclassified`, 초안): 📂 미분류 기사만 기존 소제목에 끼워 넣는다(`assign_to_existing`). 기존 이름·구성은 안 건드리고, 정말 필요하면 새 소제목만 제안, 직접 만든 소제목은 대상 제외, 확인창 없음. **응답에 없는 URL은 전부 「기타」로 보낸다**(한 번에 미분류가 항상 빈다).
  - **📌 담아둔 기사 구획의 AI 기사 배정**(초안 전용): `/promote-all-manual-to-draft` 후 `/assign-unclassified`. 확인창 한 번(남겨두는 것도 정상이라서). 확정본엔 안 붙인다.
  - **AI 모든 기사 재분류** (초안 `/regenerate-subheadings-draft`, 확정본 `/regenerate-subheadings-final`): 이름·구성·순서를 처음부터. **항상 확인창.** 두 화면이 같은 모양·같은 문구.
  - **AI 기사 나누기** (초안 `/split-group`, 확정본 `/split-group-final`, 코드는 `split_button_style/html/script` 공용): 아래 참고.
- 세 AI 버튼 모두 **채운 연보라**. 배정은 `icon("bot")`, 재분류는 `icon("refresh")`. 파괴적이라는 신호는 아이콘과 확인창이 맡는다. 「배정」 문구는 "기존 소제목이 안 흐트러진다"까지만 약속한다.
- **재분류 확인창**(초안 보통·초안 폴백·확정본 세 자리): `[상황] → [안심] → 빈 줄 → [질문]`. 안심 문구는 `app/config.py` `RECLASSIFY_SAFE_NOTE` 하나. 옮긴 기사는 화면 라벨 `「다른 소제목」`으로 가리킨다. **N은 실제로 흩어질 수 있는 기사만 센다**(목적지가 `forced_group_target_names()`에 있으면 제외), N=0이면 그 문장을 안 그린다. 폴백일 땐 `data-degraded`로 짧은 문구.

### AI 기사 나누기 (확정본·초안)

- 소제목 헤더 또는 기사 체크박스로 고르면 일괄이동 바의 `옮기기 · ↑↓ · 🗑` 뒤 세로선 오른쪽에 연보라 버튼. **고른 기사만** 새 소제목으로 나누고 **다른 소제목은 이름·구성·순서를 안 건드린다.** 일부만 골라도 된다(안 고른 건 원래 칸에 남는다).
- 잠김(흐리게 + 이유 알림, `aria-disabled`): 여러 소제목에 걸침 · 4건(`MIN_SPLIT_ARTICLES`) 미만 · 소제목이 15개로 꽉 참. 📂 미분류나 소제목이 아닌 칸의 기사가 섞이면 버튼을 안 그린다. 확인창은 통째로 나누는데 이름표가 있을 때만(`data-renamed`).
- AI 호출 `split_group_articles`는 「기타」 재정리와 같은 방식(같은 SYSTEM_PROMPT + 원래 이름·다른 소제목 이름). 캐시는 안 건드린다.
- 어디로 옮길지는 `app/group_split.py` `plan_split` 한 곳: 원래 이름을 쓴 묶음은 제자리, 다른 소제목과 이름이 겹친 묶음도 제자리(기존 「기타」만 합침), 새 소제목은 남은 칸만큼 큰 묶음부터. **목적지가 둘 이상이어야 나눈 것으로 친다**(아니면 「더 나눌 쟁점을 찾지 못했어요」).
- 저장은 「AI 기사 배정」과 같은 경로(`_split_group`): `group_overrides` + `assigned_groups`, 그래서 초안 결과가 확정본으로 따라간다. 확정본은 회차 파일에서 옮긴 기사의 `group`만 직접 고친다(스냅샷 전체 재계산 안 함). 새 소제목은 원래 소제목 바로 뒤(`order_with_new_names`). 화면과 서버가 본 소제목이 다르면 `stale`로 아무것도 안 한다. 결과는 `.just-classified`로 칠한다.
- 한계: 나뉜 소제목의 하단 요약은 첫 기사 발췌다. 시안 [mockups/SUBHEADING_SPLIT_MOCKUP.html](mockups/SUBHEADING_SPLIT_MOCKUP.html).

### 캐시·재사용·마감

- **마감 시 분류는 새 이름을 짓지 않는다.** `collect_run` → `classify_for_finalize`: `classify_with_llm(allow_call=False)`로 초안 분류를 재사용하고, 새 기사만 `assign_to_existing`으로 기존 소제목에 넣는다. 초안 분류가 아예 없을 때만 전체 분류. 결과를 `seed_cache()`로 확정본 기사 집합 키에 심어 첫 큐레이션도 캐시를 맞춘다.
- **캐시** `{(frozenset(urls), max_subheadings): [(name, [urls])]}`, 최대 40, `data/llm_classification_cache.json`. **저장은 파일을 읽어 병합한 뒤 쓴다**(같은 키는 메모리 우선, 파일 항목은 최근 80개까지). 파일 항목을 키로 되돌리는 식은 `_entry_key_of` 하나.
- `_find_reusable`: 기사가 줄기만 했으면 상위집합 재사용. `_find_partial`: 겹침이 가장 큰 캐시 재사용, **과반 판정의 분모는 캐시 기사 중 숨기지 않은 것**(`cached_urls - hidden_urls`), 새 기사는 「📂 소제목 미분류」로. 호출 실패 시 한 번 재시도 후 재사용 가능한 캐시로 물러선다.
- **빈 응답도 실패로 보고 재시도**하고 `stop_reason`·블록 타입·앞부분을 WARNING으로 남긴다.
- **「기타」 재정리**(`_refine_etc_bucket`): 1차 「기타」가 6건 이상이면서 25% 이상이면 그 기사만 다시 분류. 남은 칸만큼만 새로 만들고, 실패하면 1차 결과를 그대로 둔다.
- 직전 표시 이름을 프롬프트에 넘겨 이름을 이어 쓰게 한다(보장 아님).
- `_merge_unclassified()`는 복사·다운로드·발송 텍스트에서 미분류를 「기타」로 합친다. 「📂 소제목 미분류」라는 이름이 보고서에 새면 안 된다.
- 입력 순서 보존: `_rebuild`는 캐시를 "어느 소제목인지"에만 쓰고 순서는 현재 목록에서 가져온다. `_parse_groups`는 입력 순, `_apply_forced_groups`는 `order_index` 순, `snapshot_group_names`는 입력 순. → H: 담당자 > AI: 정렬·분류 우선순위 3종 확정

### 분류 실패 표시

- `llm_attempt_failed_without_fallback()`이 참이면 `collect_run`이 회차에 `classification_degraded`를 저장한다 → 자동 발송 건너뜀, 확정본에 「⚠️ AI 분류 실패 — 확인 후 직접 발송해주세요」.
- **소급 판정** `app/classifier.py` `looks_rule_based`/`run_looks_rule_based`: 「기타」·미분류를 뺀 소제목이 3개 이상이면서 **전부 한 어절**이면 폴백으로 본다. 저장된 `classification_degraded`가 있으면 그 값 우선. 이름 단위로 거르지 않는다. 쓰는 곳: 확정본 배너·카운트다운, 자동 발송 차단, 정기 보관함 `AI 분류 실패` 칩(`.degraded-mark`).
- 최신이 아닌 회차를 재분류할 방법은 없다(큐레이션 핸들러가 `load_latest_run()` 고정).

### 소제목 표시

- **🤖 AI 배지**(`.ai-badge`, 이름 문자열 밖의 `<span>`): `이름 ∈ ai_names` 그리고 `∉ custom_names` 그리고 이름표가 없을 때. `ai_names`는 `cached_group_names()` + `load_assigned_groups()`. 담당자 이름과 AI 이름이 겹쳐도 `기타2` 같은 접미사로 가르지 않는다(보고서 텍스트로 나가는 이름이라서).
- 「📂 소제목 미분류」는 괄호 없이 `.unclassified-title`(회색 이탤릭) + `.unclassified-count`.
- **소제목 형식** (`/subheading-format`, 기본 `<{section}>`): 화면 `<h2>`, 요약 블록, 목차, 복사 텍스트 모두에 적용. 예외: 「다른 소제목」 드롭다운과 이름 고르기 창 프리필은 원래 이름. 미분류는 형식 미적용. 이름표로 바꾼 이름에 앱이 괄호를 다시 붙이지 않는다.

## 정기 모니터링 — 큐레이션 (확정본 최신 회차 + 초안)

### 기사

- **숨김 대조는 URL 하나뿐이다 — 담당자가 🗑️를 누른 그 기사만 사라진다.** 판정은 `app/curation.py` `filter_hidden`/`is_hidden`만 거친다. 제목·언론사 대조는 쓰지 않는다(제목이 같은 별개 기사가 조용히 사라진다). 저장되는 outlet/title/group은 표시용이다. 화면 세 경로(단건·체크박스 일괄·소제목 통째)는 `hideArticleBody(btn)` 하나를 쓰고, 빠진 값은 서버 `_hide_meta_fallback`이 채운다. 숨김 쓰기는 모듈 잠금으로 직렬화. → H: 숨김 제목 대조 되돌림
- **↑↓는 소제목 경계를 넘지 않는다**(`move_article`, 끝에서 비활성). 다른 소제목으로 옮기는 건 「다른 소제목」 드롭다운(`bulk_reassign_group`)만.
- **담당자의 ↑↓가 언론사 순서를 항상 이긴다.**
- 체크박스 일괄이동 바: 같은 소제목 안 ↑↓(`bulk_move_articles`), 다른 소제목으로 이동, 🗑 일괄 숨김.
- **⋯ 더보기**(항상 보임, 한 번에 하나): 복사하기 / 원문 다시 불러오기(`/refetch-summary`, `og:title`·`<title>` 중 긴 쪽) / 기사제목 직접 수정(`/edit-summary`). 둘 다 `app/summary_overrides.py`(`data/summary_overrides.json`, URL 키 전역). 🗑·↑↓는 메뉴 밖.
- 액션 아이콘은 행 오른쪽 끝(`margin-left: auto`).
- **검색어 표시**: 액션 줄에 `🔍 기재부, 정부`(회색 글자, 돋보기 `icon("search")`, 설명은 `title`). 확정본·초안만(`show_matched_keywords`). **남는 자리만큼만 보이고 `…`** — `flex: 1 1 0; min-width: 5em; max-width: max-content`, 잘린 행은 hover 순간에 판정해 툴팁(`kw_inline_style()`/`kw_inline_script()` 공용). 구분점은 `.kw-inline` 안. 값이 없으면 아무것도 안 그린다. **복사·txt·발송·엑셀에 안 나간다.** 시안 [mockups/KW_CHIP_MOCKUP.html](mockups/KW_CHIP_MOCKUP.html).
- **게시시각**: 화면에 `HH:MM 게시` + "N분 전"(화면 전용, 내보내기엔 없음).

### 소제목

- ▲▼ 순서는 이름별로 `data/group_order.json`(`/save-group-order`), 확정본·초안·정기 보관함·복사 텍스트에 똑같이 적용. 저장 순서에 없는 이름은 뒤로.
- **「기타」는 언제나 맨 뒤(📂 미분류 바로 앞) 고정**(`_lock_rank`/`is_order_locked`). 판단은 **표시 이름** 기준. 고정 칸은 ▲▼를 안 그리고(`data-order-locked`) 목차에서도 잠기며 저장 순서에서 빠진다. ▼ 잠금 기준은 "옮길 수 있는 마지막 소제목".
- 「보기 순서」 select(소제목별/시간순/언론사순/소제목 내 시간순)는 **화면 전용**.
- 소제목 헤더 체크박스(`.group-select-all`)는 한 방향(전부 체크만).
- **직접 만든 소제목**: 하늘색 굵은 점선(`.subheading-custom`, `#7DD3FC`), 드롭다운에서 굵게. `data/custom_groups.json`, 자정 초기화. **목록 맨 아래에 온다**(`group_order.json`을 건드리지 않음). **항상 빈 칸으로 시작한다** — `clear_group_overrides_for(name)`로 그 이름을 가리키던 배정을 먼저 끊고, 실제로 끊었을 때만 `undo_push("소제목 만들기")`. 지우는 범위는 그 이름 전체.
- **이름 겹침은 같은 화면 안에서만 막는다**(409 + 이유 문장, `_respond_conflict`, `display_name_in_use`). **다른 회차의 이름표와 겹치는 건 허용한다** — 회차를 이어 같은 이름을 쓰는 게 정상 업무이고, 빈 소제목은 보고서 텍스트에서 걸러진다.
- 빈 커스텀 소제목만 🗑로 삭제.
- **소제목 이름 고르기 창** (`app/subheading_names.py` `name_pool` + `name_picker_style/html/script`, 확정본·초안): ✏️ 이름 바꾸기와 + 새 소제목이 쓴다.
  1. 기본 목록 = 오늘·어제 전 회차의 소제목을 **회차별로** 최신순(지금 회차와 그 이후는 제외). 첫 묶음 구분선에 `· 직전 회차`. 한 글자라도 치면 같은 목록에서 **평평하게** 거르고 줄마다 회차(`.np-when`)를 적는다(구분선 있을 땐 안 적는다).
  2. 목록엔 **그 회차 화면에 실제로 찍힌 표시 이름만**. 담당자가 갈아치운 AI 원래 이름·미분류는 안 나온다. `forced_group_target_names()`를 재료로 쓰지 않는다.
  3. 볼드 = 담당자가 고치거나 만든 이름. 한계: 어제 만든 커스텀 소제목은 볼드가 안 붙는다.
  4. 이 화면에 이미 있는 이름은 취소선 + 못 누름(`getAllGroups()` 기준).
  5. 창은 이름 문자열만 넘기고 저장은 `/rename-group`·`/add-custom-group`. **고른 이름은 그 회차에만 붙는다**(앱이 알아서 적용하지 않는다).
  6. 목록 자리는 오늘·어제 저장된 회차가 아예 없을 때만 숨긴다. 검색 결과 0건이면 기본 목록으로 되돌리고 안내줄만 `찾은 이름이 없어요 · 최근 쓴 이름`. 「없다」를 산문으로 설명하지 않는다.
  7. 이름 바꾸기는 지금 이름을 채워 전체 선택으로 열고, 첫 타이핑 전까지(`_npPristine`) 그 값을 검색어로 쓰지 않는다. 만들기는 빈 칸.
  - 목록은 렌더링 시점에 구워 넣는다. 시안 [mockups/NAME_PICKER_RANGE_MOCKUP.html](mockups/NAME_PICKER_RANGE_MOCKUP.html).
- **미니 목차** (`.toc-toggle-btn`, 오른쪽 아래): 여는 순간의 DOM을 읽는다. 행마다 ▲▼, 직접 만든 소제목 굵게. **초록(`.toc-row-moved`)은 마지막으로 옮긴 한 칸뿐**(`_tocMovedIdx`, 인덱스로 기억). 하단 「N개 위치 바뀜」(`_tocTouched`)은 개수이고 초록 수와 일부러 다르다. 확정본·초안에 같은 코드가 복제돼 있다 — 둘 다 고친다.
- **글리프**: 소제목 순서 ▲▼(`.order-btn`, `.toc-order-btn`), 기사 순서 ↑↓(`.move-btn`).
- **소제목별 복사 📋** (`.group-copy-btn`, 확정본·초안·수시): 헤더 아이콘 묶음 맨 앞(`📋 → ✏️🗑️ → ▲▼`). **소제목 이름 + 기사만**(회차 헤더·메모 줄 없음). **전용 빌더를 만들지 않는다** — 전체 복사와 같은 함수에 소제목 하나만 넘기고 `header` 인자로만 가른다(정기는 `build_group_copy_texts`/`_build_preview_group_copy_texts`가 `data-copy-text`로 굽는다). 미분류·빈 커스텀 소제목엔 안 붙인다. 폴백이어도 확인창 없음. 피드백은 `.is-copied`·`copyArticleIcon`(수시는 `copyGroupText`).

### 되돌리기 (`app/undo.py`, `POST /undo`)

- 대상: 숨김·소제목 간 이동·↑↓(단건·일괄)·이름 변경·AI 재분류·AI 배정·AI 나누기·소제목 만들기(배정을 끊었을 때)·담아둔 기사 승격.
- **전체 큐레이션 상태 스냅샷**: `hidden_articles`/`group_overrides`/`group_labels`/`group_order`/`custom_groups`/`preview_order`/`manual_articles`/`draft_pending_articles`/`assigned_groups` + LLM 캐시(`_cache`/`_last_names`) + **최신 회차 파일의 원본 바이트**(같은 소제목 ↑↓는 회차 파일의 기사 순서에만 흔적이 남는다). 스냅샷에 키가 **아예 없는** 옛 기록은 그 파일을 건드리지 않는다. **새 저장소를 만들면 여기에 넣을지 반드시 따진다.**
- `push(label, coalesce_sec=3.0)` — 같은 라벨 3초 이내는 한 걸음. 자정 초기화, 최대 20.
- `↩` 버튼(`.undo-fab`)은 되돌릴 게 있을 때만, 툴팁에 무엇을 되돌리는지.
- **되돌린 뒤 담당자가 그 동작에서 손댄 기사만 세이지(`.just-moved`)로 칠한다.** 밀려난 이웃·AI가 옮긴 기사는 안 칠한다. 그래서 diff가 아니라 동작 시점에 `push(label, touched=[url…])`로 적는다. 3초 합치기: `batch`(화면의 `newHideBatchId()`)가 같으면 URL을 덧붙이고, 다르면 마지막 동작의 URL로 갈아끼운다. 칠한 행은 노랑을 뗀다. 칠한 기사가 하나도 안 보일 때만 첫 기사로 스크롤(`history.scrollRestoration = "manual"`로 끄고, `auto` 복귀는 `pagehide`에서).

### 숨긴 기사 쓰레기통

- **확정본·초안 왼쪽 아래 두 칸 스택**: 아래 🗑(빨간 배지=오늘 건수) `left 20 / bottom 20 / 56px`, 위 ↩ `bottom 88 / 50px`, 팝오버 `left 20 / bottom 88 / width 336px`. **수시 화면과 같은 값** — 한쪽을 바꾸면 다른 파일도 바꾼다. 🗑는 0건이면 안 그리고, ↩는 🗑가 없어도 위 칸을 지킨다. 코드 `hidden_trash_style()/html()/script()` 한 곳. 두 화면의 하단바 🗑 링크는 없다(실시간현황은 있다).
- **숨김은 7일(`HIDDEN_VIEW_DAYS`) 유지되고 그 안에선 언제든 되살린다.** 판정 `load_hidden_urls`와 화면의 되살리기 가능 판정 `active_hidden_dates`가 같은 상수를 본다. 기간을 더 늘리지 않는다(되돌리기 스택이 파일을 통째로 20번 복사한다). 파일 보관도 7일. 중복 검사는 읽어온 기록 전체, 복구는 날짜 무관 그 URL 기록을 지운다.
- **모수는 전역**(초안·확정본·실시간 어디서 숨겼든 한 목록). 회차로 거르지 않는다.
- **팝오버·배지는 오늘치만**(`load_hidden_batches(days=1)`). **「한 번에 숨긴 덩어리」로 묶는다** — 묶음 첫(최신) 기록 기준 3초(`HIDDEN_BATCH_WINDOW_SEC`, **undo의 `coalesce_sec`와 같아야 한다**), 연쇄 방식은 안 쓴다. 묶음 이름은 소제목이 하나로 모이면 그 이름 그대로, 아니면 `골라서 숨김`("통째로"라고 단정하지 않는다). `group` 필드는 이름용이고 판정에 안 쓴다.
- 팝오버: 묶음은 한 줄 `[↩ 모두 복구]`, 낱개는 `[↩]`, 최대 5줄, 묶음을 펼치는 장치 없음. 더보기 줄은 1건이어도 항상 그리고 **같은 탭**으로 `/hidden`(묶음 있으면 `쓰레기통 열어서 하나씩 고르기 →`, 없으면 `쓰레기통 열기 →`).
- **`/hidden`** (`render_hidden_page`): 7일치를 날짜별 `<details>`, 오늘만 펼침. 「오늘」 칩은 `date == today`, 되살리기 가능은 `active_hidden_dates()` — 서로 다른 축이다. 제목 옆 건수는 오늘치. 묶음 카드는 최근 것만 펼침, 1건 묶음은 안 접는다. 헤더 `[체크박스][이름·시각][건수][↩ 모두 복구]`, 체크박스는 한 방향. 선택이 있을 때만 일괄 복구 바. 폭 800px.
- **행 오른쪽은 모든 줄이 `[원문 ↗][복구 ↩]`**(둘 다 30×26px 아이콘). 되살릴 수 없으면 지우지 않고 `aria-disabled` + 흐림 + 이유(`alertExpiredRestore()`). **↩는 이 화면에서 언제나 "되살린다"**: 한 건은 아이콘만, 여러 건은 아이콘+글자(`↩ 모두 복구`, `↩ 선택한 기사 복구`). 시안 [HIDDEN_ROW_ACTIONS_MOCKUP.html](HIDDEN_ROW_ACTIONS_MOCKUP.html).
- **복구는 `POST /unhide-article` 하나** — `url` 여러 개, `unhide_articles`로 한 번에 읽기-수정-쓰기. `redirect`가 있으면 303, 없으면 204 후 화면이 `location.reload()`. 몇 건이든 되돌리기 한 걸음.

### 표시 배너·기사 행 상태색

- **늦게 들어온 기사**(확정본, 앰버): 목록 맨 위 흐름 안 배너(`.late-banner`, 0건이면 없음, `[N건 보기]`는 `jumpToLatePickup`으로 순서대로 이동) + 메타 줄 칩 `⏱ 늦게 들어온 기사`(`원문보기` 바로 뒤·검색어 앞). **문구는 네이버에 대한 주장을 하지 않는다**(우리 동작만, 이유는 ⓘ 툴팁). 건수를 문장 가운데 두지 않는다. 왼쪽 띠는 안 준다(마감 후 자동 배정 연보라에 양보). 보고서 텍스트엔 없다. 시안 [mockups/LATE_PICKUP_MOCKUP.html](mockups/LATE_PICKUP_MOCKUP.html).
- **상단 고정 요소를 새로 만들 때**: `.keyword-note-zone`의 `top`처럼 `--finalize-banner-h`(확정본 마감 후 자동 배정 배너)와 `--round-banner-h`(초안 마감 배너)를 더한다.
- **hover**: 회색 `#F3F4F6`, 상태색보다 먼저 선언.
- **새로 들어온 기사 노랑**(`.is-new-arrival`, `#FFF9C4`): 확정본·초안에서 **같은 회차**의 이전 보기에 없던 기사. 기준선은 회차별 `localStorage`(`confirmedKnownUrls:{date}:{slot}` / `previewKnownUrls:{ROUND_DEADLINE_MS}`), 처음 여는 회차는 기록만. `.just-promoted`/`.just-moved`보다 먼저 선언.
- **세이지**(`#E7EFE8`): `.just-moved`(↑↓·소제목 이동·일괄) 한 번, `.just-promoted`(📌 승격)도 같은 색. **"담당자가 방금 손댄 것만"** 칠한다.
- **연보라** `.just-classified`: AI 배정·나누기 결과.
- 펼쳐 본 기사(`<details open>`)는 제목·URL을 남색(`seen_color` `#3B5FA0`).

## 정기 모니터링 — 출력·발송

### 텍스트 형식

```
ㅇ (언론사) 기사제목
URL

```
- `/format`에서 `{outlet}`/`{title}` 템플릿. URL 줄과 발행일 미표시는 바꿀 수 없다.
- **기사마다 URL 다음에 빈 줄 하나.** 소제목 끝에 따로 넣지 않는다. 텍스트를 만드는 곳 넷이 전부 따른다: `app/renderer.py` `_build_plain_text` · `app/preview_renderer.py` `_build_preview_plain_text` · `app/history_renderer.py` `_build_slot_plain_text` · `app/adhoc/renderer.py` `build_adhoc_plain_text`.
- **화면의 기사 제목 줄은 이 템플릿을 따르지 않는다** — `언론사 제목`, 언론사만 회색·`0.86em`·굵기 500·오른쪽 여백 `0.5em`. 같은 값이 다섯 곳: `.title-outlet`(renderer·preview_renderer·history_renderer, 마크업은 `render_article`) / `/hidden`의 `.hidden-row .title-outlet` / 수시 `.a-title .outlet`. 예외: 실시간현황(언론사 칩), 좁은 팝오버(괄호 유지). 시안 [mockups/OUTLET_TITLE_MOCKUP.html](mockups/OUTLET_TITLE_MOCKUP.html).
- 전문 크롤링 안 함 — 네이버 `description`만. 형광펜 단어(최대 20, `HIGHLIGHT_COLORS` 5색 순환, 칩을 누르면 다음 색, 색은 단어별 전역).
- 복사·txt에서 큐레이션 버튼·아이콘은 빠진다.
- **다운로드는 실제 `<form method="POST" action="/download-text">`**(서버가 attachment로 응답). `data:` URI는 쓰지 않는다. 확정본·초안은 복사·발송과 같은 `PLAIN_TEXT`를 보낸다.

### 키워드 작성 메모 (`app/manual_keyword_note.py`)

- 「+ 직접 키워드 작성하기」 — `(날짜, 회차)`별 자유 텍스트. 비어 있지 않으면 헤더 아래 `- {메모}`로 복사·txt·텔레그램·이메일에 나간다.
- 저장 형식 `{"notes": {"YYYY-MM-DD|HH:MM": 문구}}`, 최대 60. 옛 형식도 읽는다.
- **화면과 내보내기가 같은 값**(`load_manual_keyword_note(run_key)`). 그 회차 메모가 없으면 빈 칸(다른 회차 메모를 끌어오지 않는다).
- 템플릿 변수 7개는 `keyword_note_template_vars(run_key)` 한 곳. 조회 날짜는 **그 회차의 날짜**.
- sticky 위치는 배너 높이 변수를 더한다(위 참고).

### 발송

- **텔레그램** (`app/telegram_bot.py` `send_text`): 4096자 분할, 받는 사람마다 따로, 예외 격리. `data/telegram_recipients.json` `[{name, chat_id, enabled, alert_scoop, alert_flash}]`. `is_configured()`는 토큰만 본다.
- **이메일** (`app/email_sender.py`): SMTP 연결 하나로 받는 사람마다 따로(공유 `To:` 없음), `text/html`(`_linkify` + `<pre style="white-space: pre-wrap">`), 제목은 본문 첫 줄. `data/email_recipients.json`.
- 둘 다 `SendResult`(`ok`/`failures`, `bool()` 호환)를 돌려준다.
- **확정은 수집 직후 자동**(`confirm_and_promote`). 수동은 확정본의 초록 원형 **(발송)** 버튼 하나.
- **자동 발송** (`/auto-send`, `auto_send_enabled` + `auto_send_grace_min` 1~30): 수집 후 유예시간(기본 5분) 안에 안 누르면 스케줄러 tick(`check_pending_confirm_and_send`)이 보내고 `sent_by="auto"`. 탭이 안 열려 있어도. 꺼져 있으면 카운트다운도 없다. 헬퍼 `auto_send_grace_sec()`/`is_auto_send_enabled()`.
- 화면: 첫 발송 전 빨간 `⏱️ MM:SS 후 자동 발송`. 발송 후 회색 `✔️ N시 M분 발송 완료`(수동) / `🤖 N시 M분에 자동 발송 완료`(자동), 2회부터 ` (N회)`. 시각은 항상 콜론 없는 한국어(`format_slot_time_kr`). 재발송은 제목에 "(수정)".
- **발송 실패 기록**: `send_confirmed_run`이 채널별 실패를 받는 사람 이름으로 바꿔 `record_send_failure` → 회차의 `send_failure`(`{at, auto, channels:[{channel, target, error}]}`). 성공 기록과 별개 필드. 받는 사람이 없는 채널은 실패로 안 친다. 원인이 같으면 다시 쓰지 않는다.
- 화면이 읽는 건 `load_latest_confirmed_run()`(확정 또는 `confirmed` 필드 없음).

## 정기 보관함 (`app/history_renderer.py`, `history.html`)

- 제목 「정기 보관함」, 부제 `날짜별로 쌓인 정기 스크랩 · {retention_label} 보관됩니다`. 날짜 → 회차, 최신 먼저, 최신 날짜·최신 회차는 펼친 채.
- 저장된 `group`을 읽는다(재분류 없음). **소제목 순서는 확정본과 같은 `apply_group_order`**(그 회차의 round_id로). 화면·복사·엑셀이 같은 함수를 거친다.
- **읽기 전용**(숨김·이동·이름 바꾸기 없음). 예외는 🏷 라벨(아래 라벨 절)과 삭제.
- 확정본·초안 제목 옆에 정기 보관함 링크는 없다(홈·대기 화면엔 있다).
- 회차 수·건수는 상시 표시하지 않는다. 날짜 라벨 `M월 D일 (요일)` + 오늘 칩(`.chip-today`). 회차 라벨 `HH:MM 기준`.
- **발송 표시는 점 하나**(`.sent-dot`, 시각·자동/수동은 `title`). 기록이 없으면 아무것도 안 띄운다("미발송"이라 단정하지 않는다). 실패가 있으면 속 빈 빨간 링(`.sent-dot.fail`)이 성공 점보다 우선, 툴팁 `_send_failure_tooltip`.
- 같은 줄에 **그 회차에 실제로 저장된** 키워드 메모(`.slot-note`, 테두리 없는 회색 글자).
- **기간 조회 바**(1일/7일/1개월/전체 + 직접 입력): 서버 왕복 없이 `applyHistoryPeriodFilter`가 `data-date`로 숨긴다.
- **복사/txt/엑셀 세 층**: 회차(복사·다운로드, 헤더 `YYYY-MM-DD HH:MM 기준`), 날짜·결과 전체(복사·txt·엑셀). 날짜·전체 텍스트는 **회차 층의 `data-copy-text`를 클라이언트에서 이어붙인다** — 한 번이라도 불러온 날짜만(`historyDayIsLoaded`). 엑셀은 `/download-excel-history`. 파일명: 회차 `언론모니터링_{date}_{run_slot}.txt`, 날짜·전체 `{date 또는 범위}_정기_전체.txt`.
- **지연 로딩**: 최근 2일(`EAGER_HISTORY_DAYS`)만 내용까지 렌더링. 나머지는 펼칠 때 `GET /history/day?date=`(`render_history_day_fragment` — 즉시 렌더링과 같은 함수).
- `pub_time`은 `HH:MM`만.
- **「수집 기록 없음」 줄** (`expected_slots_for_date`/`_render_missing_slot`/`_with_gap_dates`): 예정 회차인데 저장된 게 없으면 회색 한 줄(`.slot-missing`). 회차가 하나도 없는 날짜는 앰버 `회차 없음` 칩으로 끼워 넣는다. 범위는 **저장된 가장 오래된 날짜~최신 날짜 사이만**, 오늘의 아직 안 온 회차는 제외. 그 날짜엔 지연 로딩·내보내기 버튼 없음. 실제 회차와 시각 순으로 섞는다. 문구가 "누락"이 아닌 건 그날의 스케줄을 모르기 때문(툴팁이 밝힌다). `_render_day_slots`에 `run_date`를 넘겨야 그려진다.

### 보관함 삭제 (정기·수시 공통)

- **두 길**: 줄 끝 🗑 = 회차 하나, **확인창 없음**(지운 자리에 곧바로 「삭제함 · 되살리기」가 남기 때문 — 둘은 세트다) / 결과 줄 [선택 삭제] = 여러 개, 확인창 한 번.
- 맨 위 층 🗑(수시 사안 줄 · 정기 날짜 줄)은 확인창 한 번.
  - 수시 사안 줄: 「이 사안에 쌓인 회차 모두 삭제」, 지금 화면에 보이는 회차만, 기간 밖 개수(`data-outside`)를 확인창에 적는다. 통째로 삭제함이 되면 `모두 되살리기`(회차 1개면 안 붙임). 수시 날짜 줄엔 🗑 없음.
  - 정기 날짜 줄: 지울 수 있는 회차만. 오늘 날짜엔 없음, 가장 최근 회차가 든 날은 그걸 빼고 지우며 확인창에 적는다(그 회차뿐이면 없음). 지울 키는 서버가 인덱스로 굽는다(`_day_delete_targets` → `data-keys`). 지운 뒤 그 날을 펼쳐 삭제함 줄을 보인다. 전부 지우면 날짜 줄이 `all-gone`. 「모두 되살리기」는 하나씩 차례로.
- 선택 삭제 중엔 복사·내보내기·줄 끝 🗑가 숨고, 줄을 누르면 체크, 상위 체크 = 하위 전부(한 방향), 고른 게 있을 때만 파란 선택 바. `[빈 회차(0건) 모두 고르기 (N)]`은 오늘 것 제외.
- **지우기 = trash로 옮기기.** 되살리면 파일째 돌아온다.
- **정기** (`delete_run`/`restore_run`/`list_deleted_runs`, `data/articles_trash/{run_at 날짜}_{HH-MM}.deleted-{시각}.json`): **오늘 회차와 가장 최근 회차는 못 지운다**(`run_lock_reason`, 서버도 `RunLockedError`) — 가장 최근을 지우면 앞 회차가 `load_latest_run()`이 돼 자동 발송될 수 있다(이 잠금을 풀려면 자동 발송에 "오늘 회차만" 조건부터 넣는다). 지운 자리는 `HH:MM 기준 · 삭제함 · 지운 시각 · 되살리기`(`_render_deleted_slot`)로 계속 남는다. fetch(`/history/delete-runs`·`/history/restore-run`)로 줄만 갈아 끼운다. 선택 삭제 중에만 회차별 건수를 보인다.
- **수시** (`delete_card(reason=)`/`list_deleted_cards`/`restore_card`, `/adhoc/archive/delete`·`/restore`): 파일명 가운데가 이유 — `.deleted-`(담당자) / `.expired-`(보관 기한). **되살리기 목록엔 `.deleted-`만.** 카드 id는 `_CARD_ID_RE`로 먼저 검증. form 제출 + 303, 방금 지운 id를 `deleted=`로 실어 그 화면에서만 제자리 줄로, 다시 열면 맨 아래 접힌 「최근 삭제」. 사안 정보가 없는 카드는 「이름 없는 사안」 묶음. 카드 화면의 삭제도 같은 trash.
- 시안 [mockups/ARCHIVE_DELETE_MOCKUP.html](mockups/ARCHIVE_DELETE_MOCKUP.html).

## 데이터 보관

- 회차 파일 365일(`RETENTION_DAYS`, `delete_expired_runs`, 앱 시작·매 tick, `run_at` 기준).
- 수시 카드 365일(`ADHOC_RETENTION_DAYS`, `delete_expired_cards`, `created_at` 기준, `data/adhoc/trash/`로 이동). **호출은 `main.py`가 주입한다** — `app/scheduler.py`는 `app.adhoc`를 import하지 않고 `cleanup_adhoc_cards` 콜백을 받는다.
- `group_overrides.json` — **배정한 지 30일(`GROUP_OVERRIDE_RETENTION_DAYS`)**이 지났는가 하나로만 정리(`cleanup_group_overrides`). 형식 `{url: {"group", "at"}}`(옛 형식도 읽음, `load_group_overrides()`는 `{url: 이름}`). **"지금 어딘가에 보이는가"를 삭제 근거로 쓰지 않는다**(초안의 이동이 확정 전에 사라진다).
- 숨김 7일, 로그 14일, 커스텀 소제목·담아둔 기사·되돌리기 스택 자정 초기화.
- 라벨은 나이로 지우지 않는다.

## 수시 모니터링 (`app/adhoc/*`, `/adhoc/*`)

담당자가 사안·검색어·시간창을 직접 지정해 모으는 별도 흐름. 설계 근거는 [ADHOC_DESIGN.md](ADHOC_DESIGN.md).

### 경계

- **의존은 `app/adhoc/* → app/*` 단방향.** 정기 코드는 `app/adhoc`를 import하지 않는다. 정기 쪽 접점은 `settings_server.py` `do_GET`/`do_POST`의 위임 한 줄씩뿐.
- **정기의 큐레이션 파일(`group_overrides`/`hidden_articles`/`custom_groups`/`group_order`/`preview_order`/LLM 메모리 캐시)은 하나도 건드리지 않는다.** 수시가 쓰는 정기 모듈은 순수 도구(`filters`/`sorter`/`icons`/`highlight`/`excel_export`/`config`/`settings`/`llm_classifier`)와 두 흐름 공용 저장소(`labels`/`label_undo`/`summary_overrides`)뿐.
- **정기로 가는 통로는 0개다.** 📌 담아두기(수시 → 정기)는 없앴다. 수시에서 본 기사를 정기에 넣고 싶으면 검색어를 고친다.

### 4단 흐름: 새 수집 → 원본 → 확정본 → 수시 보관함

시안 [mockups/ADHOC_FLOW_SPLIT_MOCKUP.html](mockups/ADHOC_FLOW_SPLIT_MOCKUP.html).

- **원본은 로데이터다** (`card.is_raw`, 새 카드는 `"raw": True`): 조건에 걸린 기사를 **소제목 없이 한 목록, 최신순**(`_article_groups`가 raw면 한 덩어리). 행엔 `[확정본으로 ▾]`·복사·⋯·🗑뿐. 툴바: `☑ 아직 처리 안 한 기사만 (보냄 N · 숨김 M)` · `처리 안 한 N건 전부 확정본으로` · `⬇ xlsx` · `🗂 HH:MM 확정본 N건 →`(없으면 흐린 `HH:MM 확정본 · 보내면 생겨요`). 소제목 배정·복사·txt·사진 모아 보기·AI 요약·↑↓는 원본에 없다. xlsx는 남는다.
- **원본의 🗑 = 「숨김」 표시**(실시간현황처럼): 빼지 않고 흐리게 + 취소선, 버튼 자리가 `🗑 숨김`(다시 누르면 되돌림, 확인창 없음, ↩ 대상). 저장은 카드의 **`raw_marks`**(`{url: {state: hidden|shown, at}}`, `set_raw_mark`, `/adhoc/card/raw-hide`·`raw-unhide`) — 기사의 `hidden`에 쓰면 목록에서 사라진다. **확정본에서 뺀 기사도 원본에선 `🗑 숨김`으로 보인다**(매번 계산, `raw_row_states`, 우선순위 보냄 > 원본 숨김 > 확정본에서 뺌). 원본에서 되돌리면 `shown`을 시각과 함께 적고, 확정본의 `hidden_at`이 그보다 늦으면 다시 숨김. 숨김 표시 행은 노랑을 안 칠한다.
- **보낸 기사 표시**: 버튼 자리가 흐린 `✓ 보냄`(툴팁에 어느 확정본) + 왼쪽 3px 청록 띠(`inset box-shadow` — `[단독]`의 `border-left`와 안 겹치게) + 내용만 `opacity .55`(`.a-title`/`.a-bot`). 배경색은 안 건드린다. 아래로 내리기·음영만은 기각. 보낸 표시는 그 카드 날짜의 확정본으로 센다(`card.sent_index`, 숨긴 기사 제외, 저장 안 함).
- **「처리 안 한 N건 전부 확정본으로」**: 보낼 목록은 서버가 다시 센다(`open_raw_urls` = 조건 안 + 숨기지 않음 + 표시 없음). 0건이면 안 그림, 확인창에 건수·받을 확정본. 확정본에서 뺀 기사를 다시 보내면 되살아난다. 체크박스로 숨김 행을 보내면 그 표시는 지운다(`clear_raw_marks`).
- **확정본은 따로 만들지 않는다 — 처음 보낼 때 생긴다.** `[확정본으로]`의 `__auto__`를 `_handle_send_to_bundle`이 **같은 날·같은 사안·같은 원본 기준 시각**의 최신 확정본(`default_bundle_for`)으로 풀고, 없으면 `new_bundle_card(사안명, issue_id, basis_time)`. **「지금까지 불러오기」마다 새 확정본**이 생긴다(같은 시각에서 나눠 보낸 건 한 확정본). 옛 확정본은 `bundle_basis_time`으로 맞춘다. **사안명에 번호·시각을 붙이지 않는다**(보고서 첫 줄). 목록에선 `bundle_label`(`인사청문회 15:05`), 보관함 줄은 `15:05 기준`. 다른 확정본은 ▾ 메뉴(오늘 다른 확정본 · `+ 새 확정본 만들어 보내기`).
- **확정본 머리줄**: `수시 모니터링 13시 05분 기준(사안명)` — 화면과 복사·txt 첫 줄이 같은 `report_header_text`. 시각은 보낼 때 기사에 적힌 `sent_from.window_end` 중 가장 늦은 것(`bundle_basis_time`, 숨긴 기사 제외, 비면 `basis_time`, 둘 다 없으면 시각 없이). 원본을 다시 불러와도 새로 보낸 게 없으면 안 바뀐다.
- **정렬 축**: 원본은 표시할 때 최신순(`sort_by_pub_desc`), **확정본은 들어올 때** 같은 소제목 안 언론사 순위 자리에 끼워 넣는다(`send_to_bundle`, 키는 `outlet_sort_key` 공용). **확정본을 매 렌더링 재정렬하지 않는다**(담당자 ↑↓를 지운다). 원본엔 기사 ↑↓가 없다.
- **수시 보관함엔 확정본만**(`is_raw` 카드 제외). **옛 원본(raw 없음)은 보관함에 남고 예전 동작**(소제목·진짜 숨기기·칩)을 그대로 쓴다 — 옮겨 적지 않는다. 원본은 새 수집의 「지난 수집」에서 찾는다.
- **탭 줄은 같은 종류끼리만**(원본 탭엔 원본, 확정본 탭엔 확정본). 반대 종류로는 🗂 알약·📥 칩·상단바.

### 확정본(`kind: "bundle"`) 규칙

- 검색을 안 하는 카드: `keywords: []`, `window: null`, `collect_log: []`, 생성자 `new_bundle_card`. `kind` 없는 옛 카드는 `collect`.
- **시간창은 `card.window_of(card)`로만 읽는다**(확정본은 `None`). 엑셀 「스크랩종료시간」·라벨 `scrap_end`는 빈 값(지어내지 않는다).
- **복사다, 이동이 아니다.** 원본에 그대로 남는다.
- **자정 잠금** — 어제 원본에서 오늘 확정본으로 못 보낸다(`bundles_for_date`, 서버도 막음).
- 소제목: 낱개로 보내면 `group: null`, 옛 원본의 소제목 통째(📥)로 보내면 그 이름을 들고 간다(없으면 커스텀으로, 상한에 걸리면 이름만 포기). 자동 분류 안 함.
- **확정본에서 숨기기 = 보내기 취소**(`sent_index`가 숨긴 것을 뺀다). 별도 취소 동작은 없다. 같은 URL은 1건(먼저 온 것 유지). 되돌리기는 받는 카드에만 쌓는다.
- **조건 필터 면제**: `article_in_condition`/`out_of_condition_reason`/`article_out_of_window`가 `added_by == "manual"`이면 통과 — 이 한 줄이 없으면 기능 전체가 안 돈다.
- 화면: 검색어 편집 패널·다시 불러오기·수집 이력 칩 없음. 헤더에 청록 `확정본` 칩. 메타 칩 `📥 종부세 3건`(원본 링크, 이름은 저장된 `sent_from.report_title`). 좌하단 팝오버 이름은 「숨긴 기사」(조건 안내 문구 없음). 「모음 관리」 패널은 만들지 않는다. **확정본 → 확정본 보내기는 없다**(서버도 거부).
- `/adhoc/new?kind=bundle` 경로는 남아 있지만 화면 입구 링크는 없다.

### 저장·수집

- **카드 하나 = 파일 하나** (`app/adhoc/card.py`, `data/adhoc/cards/{id}.json`): 사안·검색어·시간창·옵션·기사·배정·수집 이력 전부. 사안 메타 `data/adhoc/issues.json`. 카드별 잠금 `card_lock`. id `YYYYMMDD-HHMMSS-hex`(파일명 사전순 = 시간순).
- **요약 캐시** `list_card_summaries` — 파일 서명이 같으면 뽑아둔 요약 재사용. `cards_for_date`/`bundles_for_date`는 그 날짜 카드만 연다. `latest_card_id()`는 파일을 안 연다(kind를 주면 최신 30개까지만 연다).
- **수집** (`app/adhoc/collector.py` `run_collect`): 정기와 같은 순서(화이트리스트 → 정렬 → 사진/인사 → 중복). `use_outlet_whitelist` 기본 **꺼짐**. 자동 재시도 없음. 오류(`AdhocCollectError`): 끝 시각이 미래 / `collect_date`가 오늘이 아님(자정 잠금).
- **재수집은 교체이지 삭제가 아니다.** 조건에서 벗어난 기사도 카드에 남고 `article_in_condition(article, card)`(매번 계산)이 화면에서만 거른다. 조건을 되돌리면 배정·순서까지 돌아온다. 조건 밖 기사는 좌하단 「목록 밖 기사」에 이유와 함께(복구 버튼 없음).
- **조건 필터는 `articles_in_condition(card)` 한 곳만 통과한다** — `_article_groups`(화면·복사·txt·엑셀의 길목)와 `app/adhoc/classifier.py`. `_article_groups`는 `buckets.get(key, [])`로 읽는다.
- `matched_keywords`는 수집마다 현재 검색어로 다시 쓴다.
- **검색어 편집** (`_handle_edit_keywords`): 검색어(OR, `keywords`, 최대 5) / 꼭 포함할 검색어(AND, `must_keywords`, 최대 3). 화면엔 OR/AND 말 없이 색(파랑/청록)과 힌트. 엔드포인트 4개 모두 `recompute_condition` 후 되돌리기 대상. 결과 배너 없음.
  - **AND 키워드가 1000건 상한에 걸리면 적용을 거부**(`AdhocKeywordCapError`, 저장한 검색어를 되돌림). 판정은 `_search_one_keyword`의 `capped`, 캐시 히트에도 함께 저장.
  - **키워드별 캐시** `_search_with_cache`: 키 `(collect_date, keyword, window.start, window.end)`, **메모리 전용**, 40개. 실패한 키워드는 캐시 안 함.
  - `recompute_condition`은 수집 이력을 남기지 않는다(`log=False`).
- **새 수집 검색 방식 라디오**: `하나라도`(기본) / `모두`. 「모두」면 목록이 `keywords`와 `must_keywords` **양쪽에** 들어가고 상한은 3(화면 카운터·버튼 잠금·`_validate_must_keywords`). 검색어 1개 이하면 라디오 잠금. 경고 스타일은 `.is-over`(앰버). 카드를 먼저 만들고 수집하므로 0건이어도 편집으로 되돌릴 수 있다.
- **지금까지 불러오기** (원본 헤더 오른쪽 파랑 알약): 시작은 그대로, 끝만 지금으로. `/adhoc/card/recollect` + `to_now=1`, **끝 시각은 서버가 누른 순간 정한다**. 「다시 불러오기」와 이름을 가른다, 편집 창을 열면 숨는다, 누르면 잠기고 `불러오는 중…`(`pageshow`로 풀림), 어제 카드엔 없다. 시안 [mockups/ADHOC_RELOAD_NOW_MOCKUP.html](mockups/ADHOC_RELOAD_NOW_MOCKUP.html).
- **📥 수집 이력 칩** (`_collect_log_text`): 수집 시각 = 끝 시각이면 `(~HH:MM)` 생략. 4회째부터 `첫 · … · 마지막 (N회)`, 전체는 툴팁.
- **마지막 불러오기로 새로 들어온 기사 노랑** (`last_collect_new_urls`): `added_at == 마지막 수집 이력 at`. 노랑 수 = 칩의 마지막 `+N건`. 첫 수집·검색어 편집으로 들어온 기사는 안 칠한다.
- **새 수집의 「지난 수집」** (`_history_entries`/`_history_html`): 1년치 원본을 `날짜 · 사안명 · 검색어` 한 줄씩, 같은 날·사안·검색어·방식은 `×N`으로 합침. 줄을 누르면 사안·검색어·방식을 폼에 채운다(시간은 안 가져옴). hover 시 「원본 열기 ↗」, 검색칸으로 거름. 지난 원본의 「오늘 날짜가 아니에요」 경고에 `이 조건으로 오늘 새로 모으기 →`(`/adhoc/new?from=<id>`).

### 옛 원본·확정본 공통 기능

- **소제목 배정** (`app/adhoc/classifier.py` `classify_card`, 버튼만 — 자동 호출 0회): 소제목이 없으면 `classify_with_llm`으로 최대 5개(`MAX_ADHOC_SUBHEADINGS`), 있으면 `assign_to_existing`(응답에 빠진 기사는 「기타」). 직접 만든 소제목은 후보 제외·상한엔 포함. `_classify_isolated`가 정기의 `_last_names`를 호출 전후 저장/복원하고 `_persist_cache_locked()`로 디스크까지 맞춘다.
- **되돌리기** (`app/adhoc/undo.py`, `data/adhoc/undo/{id}.json`, 카드별 10단계, 카드 JSON 전체): 숨김·복구·소제목 통째 숨김·일괄 숨김·원본 숨김 표시·이동·↑↓·일괄 이동·배정·소제목 만들기·검색어 편집 4종. 사안명은 대상 아님. 같은 라벨 3초 합침.
- **소제목 헤더 🗑**: 기사가 있으면 「통째로 숨기기」(`hide_group`, 화면에 보이는 것만 — `visible_group_urls`, 확인창에 이름·건수, 락 한 번), 보이는 기사가 없는 커스텀 소제목이면 「빈 소제목 삭제」. `remove_custom_group`도 `visible_group_urls`로 판정하고 남은 기사는 `group=None`으로 푼다. 아이콘 순서 `📋 📥 🗑 ▲▼`.
- **선택 바 🗑 숨기기** (`/adhoc/card/bulk-hide`, `hide_articles`): 보이는 것만(`visible_urls_among`), 한 락, 숨길 게 없으면 되돌리기 안 쌓음, 확인창.
- **선택 바는 화면 맨 아래 흰 띠**(정기 `.bottombar`와 같은 모양), 체크했을 때만 올라온다. 두 동작은 세로선으로, `선택 해제`는 오른쪽 끝 글자. 좌하단 🗑·↩는 `z-index: 25`. 시안 [mockups/ADHOC_BULK_BAR_MOCKUP.html](mockups/ADHOC_BULK_BAR_MOCKUP.html).
- **⋯ 더보기**: `원문 다시 불러오기`(`/adhoc/card/refetch-summary`)·`기사제목 직접 수정`(`/adhoc/card/edit-summary`) — **정기와 같은 `summary_overrides` 저장소**(URL의 제목은 어디서 보든 같아야 한다). **화면에 입히는 곳은 `_article_groups` 한 곳**(+ 「목록 밖 기사」). 되돌리기 대상 아님. 실패는 오류 배너.
- **기사 행 규격 = 정기 확정본과 같은 값**: 제목 `1rem`, 메타 `0.8rem`, 들여쓰기 28px, 여백 `6px 8px`/위아래 10px, 펼친 요약은 여백만. 사진 추정·말머리 색·`N분 전`(`.a-time-rel[data-pub-date]`)·`🔍 검색어`·`copyArticleIcon` 모두 정기와 같은 함수. 행 안 아이콘만 `.a-acts` 하위 선택자로 잡는다.
- **폼 방식**: 모든 액션은 실제 `<form>` 제출(조회는 GET, 나머지 POST + 303). **스크롤 위치를 직접 기억한다** — 제출 직전 `sessionStorage` `adhocScroll:{카드id}`(시각 포함), 로드 시 한 번 쓰고 지움. 직접 적힌 form은 capture 단계 `submit` 리스너, `post()`/`bulkMove()`는 헬퍼 안에서 직접. 복원 안 하는 경우: `?error=` 화면, 10초 지난 값.
- **액션 핸들러는 카드 id·URL·소제목 이름을 `onclick` 인자가 아니라 `data-*`에서 읽는다**(따옴표 하나에 버튼이 조용히 먹통).
- 소제목 순서는 `card.ordered_group_names` 하나로 계산(렌더링과 ▲▼ 저장이 같은 결과).
- **상단바** (`nav_html`/`base_page(nav=)`): 정기와 같은 모양(`position:fixed`, `.topbar-inner` 800px, 본문 padding-top 84px). **지금 보는 화면은 목록에서 뺀다.** 카드 화면은 **반대 종류 최신 카드**로 가는 링크(`_nav_latest`, 없으면 안 그림), 새 수집은 홈·최근 카드·수시 보관함, 수시 보관함은 홈만. 상대경로.
- **출력**: 헤더 `수시 모니터링 {HH시 MM분} 기준(사안명)`(반올림 없음), 기사·소제목 형식은 정기 설정 상속, `build_adhoc_plain_text` 공용(빈 커스텀 소제목 건너뜀). **발송 없음.** 카드 메모 칸은 없다(옛 `memo` 필드는 안 쓴다).
- **수시 보관함** (`render_archive_page`, `GET /adhoc?q=&start=&end=&preset=`): 사안 → 날짜 → 회차, 카드 있는 사안만, 최근 활동순. 조건은 사안명(부분일치, 노란 강조)과 기간(1일/7일/1개월/전체는 서버가 KST로 계산, 안 맞으면 `직접 지정` 뱃지), AND. 0건이면 왜 없는지와 고치는 링크. 복사/txt/엑셀은 세 층, 미리 구워 공용 `/download-text`·`/download-excel`로(`build_plain_text_for_cards`).
- 없는 것: 사안 병합(필요해지면 설계부터).

## 라벨 (`app/labels.py`, `data/labels.json`)

기사 한 건에 담당자가 붙이는 자유 태그. 정기·수시를 가로지르는 URL 키 전역 저장소(PRD 기능10).

- 값 `{url: {labels, scrap_date, scrap_end, pub_date, outlet, title, group, source, labeled_at}}`, `source`는 `{"kind":"regular"}` / `{"kind":"adhoc","issue":사안명}`. **스냅샷은 처음 라벨을 붙이는 순간 한 번만 고정.** 라벨을 전부 떼면 항목째 삭제, 나이로는 안 지운다.
- **붙이는 화면: 확정본·초안·수시 확정본·정기 보관함.** 실시간(필터 없음 원칙·📌와 겹침)·수시 원본(로데이터)·쓰레기통은 제외. 정기 보관함은 읽기 전용 원칙의 예외가 아니다(보고서를 고치는 게 아니고 `load_latest_run()`을 안 쓴다). 스냅샷엔 그 회차 값이 들어가고 `labeled_at`만 오늘.
- **기사 행**(`render_article`의 `show_label_control` 등, 기본 꺼짐): 액션 줄에 🏷(⋯ 밖), 붙은 라벨은 별도 줄 `.lab-row` 앰버 칩(0개면 줄 없음). 팝오버: 붙은 라벨(×) + 이미 쓴 라벨(건수순, 토글) + 새 입력(편집거리 1·접두 일치면 「비슷한 라벨 있어요」, 막지는 않음).
- **「이미 쓴 라벨」 갱신**: 정기 `/add-label`·`/remove-label` 응답이 `{"labels", "known": known_label_chips_html()}`, 화면은 `KNOWN_CHIPS_HTML`에 담았다가 **팝오버를 열 때** 칠한다. **열려 있는 팝오버는 다시 그리지 않는다**(칩이 커서 밑에서 움직인다). `known`이 없으면 구운 목록 사용.
- 팝오버 CSS·JS는 `label_popover_style()`/`label_popover_script(host, port)` 한 곳(확정본·초안·정기 보관함). 기사가 많은 화면은 `render_article(..., labels_lookup=)`으로 저장소를 회차마다 한 번만 읽는다.
- 정기는 fetch+204, 수시는 form POST+303(`/adhoc/card/add-label`·`remove-label`).
- **라벨 보관함** (`GET /labels`, `render_labels_page`): 칩 **다중 선택 + AND**(`articles_for_labels_and`). 숨긴 기사도 `🗑️ 숨김` 표시로 남긴다. 출처 태그 정기=`회차`, 수시=사안명. **발행 최신순**(`sort_by_pub_desc`, 시각 없으면 뒤). 머리줄 `[복사][txt][xlsx]`(보관함 `.exp`와 같은 값, `_LABEL_STYLE`에 복제), 텍스트는 평평한 목록 + 라벨 이름에 소제목 형식(`<세제 + 보고서>`), `build_group_copy_texts` 재사용. 파일명 `{뽑은 날}_라벨_{라벨명}.txt`. 0건이면 버튼 없음.
- **라벨 관리** (`GET /label-manage`): 이름 변경·합치기·삭제를 **따로**. 이름 변경이 겹치면 거절(`LabelCollisionError`) + 「⇢ 합치기」 버튼. 합치기는 행마다 드롭다운. 삭제 확인창에 그 라벨이 유일한 기사 수(`orphan_count`).
- **되돌리기** (`app/label_undo.py`, `data/label_undo.json`, `POST /undo-label`): 별개의 전역 스택, 자정에도 안 비움, 파일 전체 스냅샷 20단계. 대상 다섯(붙이기·떼기·이름 변경·합치기·삭제). ↩ 버튼은 `left 20 / 50×50 / bottom 88`(CSS는 `_LABEL_STYLE`, 컨테이너 밖).

## 엑셀 내보내기 (`app/excel_export.py`, PRD 기능11)

- **8열**: 스크랩일자·스크랩종료시간·발행일 발행시간·언론사명·기사제목·URL·소제목·라벨명. 5곳: 확정본, 수시 카드(`build_adhoc_excel_rows`, `GET /adhoc/card/download-excel?id=`), 라벨 보관함, 정기 보관함(`build_excel_rows_for_dates`), 수시 보관함. 초안은 없음.
- **소제목은 스냅샷 원본 이름 그대로**(형식 미적용). 빈 값·미분류도 정상, 채우려고 AI를 부르지 않는다. 수식 방어 안 함(xlsx는 셀 타입이 명시된다).
- **범위 선택 UI 없음 — 지금 화면에 보이는 것.** 확정본·라벨·수시 보관함은 렌더링 때 구운 rows를 hidden input JSON으로 `POST /download-excel`에 보낸다. 정기 보관함만 지연 로딩이라 펼친 날짜(`data-date`)를 모아 `POST /download-excel-history`로 보내 서버가 다시 만든다.
- **파일명 `날짜_종류_구분.xlsx`.** 여러 날짜는 범위(`date_range_label`), 라벨은 뽑은 날. 자유 입력은 `sanitize_filename_part`.

## [단독]·[속보] 알림

- **세 경로가 `app/breaking_alert_sender.py` `detect_and_alert(articles, header_fn=None, now=None)` 하나를 공유**, 판정은 `app.filters.headline_kind`, 중복 방지 `app/alerted_urls.py`(`data/alerted_urls.json`, 자정 초기화).
  1. **바닥** — `collect_run` 저장 직전, 그 회차 기사 전부. 추가 호출 0. 설정을 다 꺼도 항상 돈다.
  2. **폴링** — `poll_and_alert_tick`(매 tick), 감시 시간대·주기가 됐을 때만 감시 그룹 키워드로 검색(`after`=마지막 폴링).
  3. **몰아보내기**(`catch_up_enabled`가 꺼지면 둘 다 안 돔):
     - (3a) `catch_up_on_wake` — 앱 시작 시 1회, 자정부터. 문구: 묶음이 **전부** 23시~6시 전(06:00 정각은 밤 아님)에 나왔으면 `🌙 밤사이·새벽에 게시된 [단독] N건`, 아니면 `📴 앱을 켜기 전에 게시된 [단독] N건`. 발행시각을 모르는 기사가 하나라도 있으면 밤이라 단정하지 않는다. `header_fn` 인자는 기사 목록.
     - (3b) 그날 **첫 폴링**의 `after`를 자정으로 내린다 — `🌙 감시 시간 밖에 게시된 [단독] N건`. `polled_date`(`data/breaking_alert_state.json`)는 `catch_up_enabled`와 무관하게 늘 남기고, 검색 실패면 안 남긴다.
     - 세 문구 모두 「놓친」이 아니라 **「게시된」**.
- **설정** (`/breaking-alert`, `app/breaking_alert.py`, `data/breaking_alert.json`): `enabled`, `group_names`(이름 목록), `start`/`end`, 주기 3~30분, `catch_up_enabled`. 감시 키워드는 그룹 OR/AND를 재현하지 않고 평평하게 모은다(`_watched_keywords`). 예상 호출량은 서버가 처음 계산하고 JS가 즉시 갱신. 설정 파일과 실행 상태 파일은 분리한다.
- **받는 사람**: 텔레그램 수신자의 `alert_scoop`/`alert_flash`(`active_alert_chat_ids(kind)`) — 정기 발송 `enabled`와 독립. 말머리별로 따로 보낸다.
- **API 한도 감시** (`app/api_usage.py`, `data/api_usage.json`): 실제 요청 지점 `_search_one_keyword`에서 셈. 하루 25,000의 80%를 넘으면 **이 알림의 폴링만** 멈추고 경고 1회(`pause_notified_date`). 본업은 안 멈춘다.
- **메시지**: 기사 제목 형식 설정 + URL 줄 + **URL 다음 줄에 `HH:MM 게시`**(오늘이 아니면 `M/D HH:MM 게시`, 시각이 없으면 그 줄을 뺀다). AI 요약 없음. 같은 주기 여러 건은 한 메시지. 보고서 텍스트엔 영향 없음.

## 홈 (`app/landing_renderer.py`, `home.html`)

- **원칙**: API 호출 없음(저장된 파일만 읽는다). 담당자가 명시적으로 고른 것만 지켜본다(건수 자동 TOP·급등 자동 채택 안 함).
- **폭 960px**(앱에서 유일하게 800px보다 넓다). 위에서부터: 오늘 건수 줄 → **오늘의 쟁점 ∥ 정책 단어 추이** 1:1(`.pair`, `minmax(0, 1fr)`, 밑선 맞춤, 800px 이하에선 쌓이고 쟁점이 위) → 흐름도 → 워드클라우드. 시안 [mockups/HOME_WIDTH_MOCKUP.html](mockups/HOME_WIDTH_MOCKUP.html).
- **오늘 건수 줄**: 본문 크기 한 줄 `정기 수집 기준 · 오늘 N건 · 어제 같은 시각 M건 대비 ±X%`(어제 하루 총계는 `title`). 비교 시각은 오늘 마지막 회차의 `run_slot`. **오늘·어제 모두 숨김을 안 본다**(`all_seen`). 오늘 회차가 없으면 비교 문구 없음.
- **[단독]/[속보] 칩**: `alerted_urls.alerted_items()`(알림 기록)만 본다 — 회차 파일에서 다시 세지 않는다. 글자만(알약 테두리 없음), hover로 펼침(`_ALERT_HOVER_JS`: 열기 120ms, 닫기 220ms, 6px 투명 다리, 한 번에 하나, 클릭은 고정). `<details>` 기반이라 JS 없이도 클릭으로 열린다. 옛 기록(`items` 없음)은 건수만.
- **오늘의 쟁점**: 오늘 회차 소제목 건수 TOP 5(워드클라우드와 함께 `filter_hidden` 통과한 목록). 순위에서만 「기타」와 일정 나열 기사(`is_schedule_listing`, 제목 맨 앞 말머리 안 「일정」)를 뺀다. 회차마다 이름이 다른 같은 쟁점은 이름 토큰 Jaccard ≥0.4로 화면에서만 병합(`_issue_streams`). 그룹 칩은 과반이 걸린 그룹만. **건수는 막대**(`.ibar`, 1위 = 100%, 값은 `title`)이고 부제·「기타 N건」 줄은 없다. %는 쓰지 않는다. 이름+칩은 `.ibox`, 막대 64px.
- **정책 단어 추이** (`app.home_trend`, `data/home_trend_words.json`, 최대 8): 제목+요약 원문에 그 단어가 있는지 문자열 매칭. **숨긴 기사도 센다.** **홈·`/trend` 모두 어제에서 끝난다**(`last_complete_day`) — 종료일이 어제를 넘으면 당기고 날짜 칸에 `max`. "어제까지" 안내 문구는 안 붙인다. 점선(`partial`) 판정은 `fill_bucket_status` 한 곳, 기준은 "그 칸의 원래 기간이 아직 안 끝났나".
  - **홈 카드**: 최근 7일 고정, 단어 **전부** 그림, 조작 없음, 그래프 어디든 누르면 `/trend`. 단어 0개면 안내. **범례·각주 없음.** 공유 축(정량) 유지. `render_chart(width=412, hover=False)` — 칸 폭 그대로 그린다(페이지 폭을 바꾸면 이 값도).
  - **`/trend`** (`app.trend_renderer`): 단어 칩(×)·직접 입력·등록 검색어 칩, 최대 8(다 차면 입력 UI 없음). 기간 프리셋 7일/1개월/3개월/6개월/1년 + 직접 입력(`GET /trend?start=&end=`, 잘못되면 7일). 집계 단위 자동(~31일 일별, ~100일 주별, 이상 월별, `build_buckets`). **앱이 단어를 추천하지 않는다.** 추가/삭제 `POST /trend/add-word`·`remove-word`(현재 구간 유지). 표(`—` = 회차 없음) + 📋 복사(탭 구분) + ⬇ 엑셀(`app.trend_export`, 기사 8열과 별개).
  - **그래프** (`app.trend_chart`): y축 최댓값 = 1·2·5 계열 눈금 × 3. 회차 없는 날은 배경을 칠하고 선을 끊는다. 일별 주말 음영은 날짜 라벨 줄까지. 일별 라벨은 10칸 이하면 전부(`13일(일)`, `short_label`), 넘으면 월요일마다(`9/7`, `label`). 표·복사·엑셀은 `wd_label`(`9/13(일)`). 일별이고 마지막 칸이 실제 어제면 그 밑에 회색 `어제`(`yesterday`, 아래 여백 +11px, 전체 높이 유지). 날짜마다 점을 찍지 않는다(집계 중 빈 원만). 크로스헤어·툴팁은 `/trend`에만(`data-buckets`/`data-series`). 색 `COLOR_TREND_1`~`8`, 두 화면 같은 슬롯 순서.
- **흐름도** (시안 [mockups/HOME_FLOW_CLEAN_MOCKUP.html](mockups/HOME_FLOW_CLEAN_MOCKUP.html)·[mockups/HOME_FLOW_ADHOC4_MOCKUP.html](mockups/HOME_FLOW_ADHOC4_MOCKUP.html)·[mockups/HOME_FLOW_ROWLABEL_MOCKUP.html](mockups/HOME_FLOW_ROWLABEL_MOCKUP.html)):
  - 칸에는 아이콘 + 이름만(1.02rem, 가운데 정렬), 설명은 말풍선(`.tip`).
  - 배치 4:4 — 정기 `🔍검색어 → ⛏️초안 → 💎확정본 → 🗄️보관함`, 수시 `🔎새 수집 → 📃원본 → 🗂️확정본 → 📚보관함`, 맨 아래 `🏷️라벨 보관함` 한 줄 전체.
  - 두 줄 왼쪽 **줄 이름표 `정기`/`수시`**(`.rl`, 글자 + 왼쪽 3px 띠 `flow_row_reg_bar`/`flow_row_adhoc_bar`, 설명은 `title`만). 이름표 열 48px + 틈 10px.
  - 줄 맨 앞은 좁은 입구 칸(`.72fr`, 흰 바탕 점선 — `.t-regkw`, `.t-adnew`). **실시간은 흐름 단계가 아니다** — 초안 위 낮은 점선 칸(`.t-live.slim`) + `📌 담아두기` 점선 화살표(`.fl-conn`). 격자 셋(`.fl-top`·`.fl-conn`·`.fl`)이 같은 칸 폭. 열 머리는 안 붙인다.
  - 640px 이하: 한 줄씩 쌓이고, 이름표는 줄 위 머리글, `.flow` flex + 격자 `display: contents` + `order`로 `정기` → 실시간 → 나머지. 화살표·빈칸·📌 연결선 숨김.
  - 말풍선 세 층: 굵은 줄(지금 상태) / 어떤 곳 / 다음 단계. 상태가 바뀌는 칸: 초안(`next_pending_slot` → `HH:MM 회차 진행 중` / `오늘 회차가 모두 끝났어요`), 확정본(`HH:MM 회차` + 기록 있을 때만 ` · 발송 완료`), 원본·확정본(오늘 가장 최근 카드 — 링크가 여는 그 카드, 없으면 안내; 확정본이 없으면 원본 화면으로, 원본도 없으면 새 수집으로, `_todays_adhoc_state`). **고정 문구는 `_FLOW_TIPS` 한 곳.** 0.15초 뒤 표시, `pointer-events: none`, 올린 칸 `z-index: 5`. 1000px 이하에선 양끝 칸만 `.tip.l`/`.tip.r`.
  - 지금 손대는 칸은 색 채움, 보관함은 흰 바탕 + 테두리, 입구·실시간은 흰 바탕 + 점선(점선 규칙은 두 클래스로 — `.tile.t-regkw`). 칸마다 건수는 없다.
  - **⚙ 설정은 홈에 한 곳, 흐름도 카드 제목 줄 오른쪽.** 시안 [mockups/HOME_SETTINGS_POS_MOCKUP.html](mockups/HOME_SETTINGS_POS_MOCKUP.html).
- **워드클라우드**: 오늘 모든 회차(URL 중복 제거) 누적, 요청마다 다시, flex-wrap 태그, 캡션 `* 당일 0시 이후 현재까지의 누적 기준`. 검색어는 포함하되 회색. 토크나이저는 의미 약한 의존명사+조사를 한 덩어리로 뺀다(`_BOUND_NOUNS`).
- **제외어** (`/wordcloud-exclude`, 최대 15): 칩 상자(중립 회색) + × + 점선 입력칩, 저장은 `POST /save-wordcloud-exclude` 하나(입력칸의 미확정 단어 포함). 「오늘 워드클라우드에 뜬 단어」는 저장 뒤 미리보기 — `wordcloud_candidates`(홈과 같은 기사·집계) + JS가 `tokenize`와 같은 규칙으로 거름. **홈의 기사 목록·거르는 규칙을 바꾸면 이 두 곳도 바꾼다.** 오늘 회차가 없으면 그 구역 없음.
- 오늘 회차가 없으면 확정본 자리는 대기 안내(`generate_waiting_page`).

## 디자인

- 밝은 카드 테마, 데스크톱 우선, 모바일에서도 볼 수 있게. 좁은 화면에서 가로 스크롤이 생기면 안 된다.
- **화면 폭**: 확정본·초안·설정 서버 화면(설정·숨긴 기사·라벨) 모두 **800px**, 홈만 960px. 설정 서버는 `.container`·`.topbar-inner`·`.save-bar-inner` 세 자리가 같은 값. 긴 설정 화면은 여러 칸 배치(메뉴 두 단·검색어 카드 두 장·형광펜 세 개·수집 시간 카드 두 장·언론사 체크 네 칸·알림 그룹 세 개), 640px 이하에선 한 줄. **새 그리드 칸은 `minmax(0, 1fr)`**. 시안 [mockups/SETTINGS_WIDTH_MOCKUP.html](mockups/SETTINGS_WIDTH_MOCKUP.html).

### 색

- **색 값은 `app/config.py` 한 곳에만.** CSS에 직접 적지 않고 `COLOR_*` 상수 → `PALETTE` 키 → 템플릿 `{key}`(렌더러의 `_theme()`이 `**PALETTE`를 받는다). 템플릿 밖이면 상수를 import.
- **이름은 색이 아니라 뜻으로**(`COLOR_ROW_NEW`). 값이 같아도 뜻이 다르면 따로 둔다.
- 한 화면 전용 색도 예외가 아니다.
- **계열은 뜻 하나씩**:
  - 파랑(accent) — 담당자의 동작, 확정 상태. 연한 톤은 `hover`/`accent_border`/`ghost_border_hover` 셋.
  - 연보라(`COLOR_AI_*`) — AI의 동작만, 채운 색.
  - 초록(`COLOR_SEND`) — 발송 하나뿐.
  - 앰버 — 주의(`COLOR_WARN_*`)와 라벨(`COLOR_LABEL_*`).
  - 빨강(`COLOR_ERROR_*`/`COLOR_SCOOP_TEXT`) — 오류, `[단독]`·`[속보]`.
  - 청록(`COLOR_ADHOC_*`) — 수시 정체성, 꼭 포함할 검색어.
- 새 배너는 새 색 세트를 만들지 말고 위 계열의 주의/오류 세트를 재사용한다.
- 주요 값: 배경 `#F8FAFC`, 카드 `#FFFFFF`, 제목 `#1E3A5F`, accent `#2563EB`, 본문 `#1F2937`, 흐린 글자 `#6B7280`, 테두리 `#E5E7EB`, hover(파랑) `#EFF6FF`, 오류 `#DC2626`.

## 작업 규칙

- 설명과 주석은 한국어로.
- 새 파일은 이 저장소(`~/press-monitor`) 안에만. 시안 HTML은 결정되면 `mockups/`로 옮긴다.
- 코드를 바꾸면 무엇을 왜 바꿨는지 한 줄로 알려준다.
- `.env` 등 비밀 정보는 커밋하지 않는다.
- 파일은 바로 지우지 않고 `trash-can/`으로 옮긴다. 삭제는 사용자가 직접 한다.
- 설치된 서브에이전트를 필요할 때 활용한다.
- **새 기능·규칙 변경 시: 경위는 HISTORY.md에 같은 항목명으로, CLAUDE.md는 결과 규칙만 고친다**(날짜 태그·경위 서술 금지).
- 렌더러를 바꾼 뒤엔 LaunchAgent로 떠 있는 앱을 재시작해야 화면에 반영된다.

### 검증 루프

1. 변경한다.
2. 결과를 직접 확인한다(브라우저로 열기/실행).
3. 스스로 코드 리뷰한다.
4. 문제가 있으면 고치고 1로.
5. 통과하면 무엇을 왜 바꿨는지 한 줄로 요약한다.
