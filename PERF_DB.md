# DB 모드가 느린 자리 — 실측과 정리할 것

저장 자리가 Postgres(Supabase)로 간 뒤 앱이 느려진 **그 자리들의 목록**이다.
[TEXT_AUDIT.md](TEXT_AUDIT.md)처럼 **아직 고치지 않은 것만 적고, 고친 줄은 지운다.**
다 지워지면 이 파일을 지우고, 규칙이 된 것은 [CLAUDE.md](CLAUDE.md)로·다시 하면 안 되는
판단은 [HISTORY.md](HISTORY.md)로 옮긴다.

측정: **2026-11-05**, 커밋 `efe23bc`, Supabase 서울(`ap-northeast-2`) Session pooler 5432,
담당자 맥에서 앱이 평소처럼 떠 있는 상태.

## 재는 법

    python3 tools/measure_store_calls.py            # 화면 셋 다
    python3 tools/measure_store_calls.py home       # 홈만

화면 한 장이 저장소에 **몇 번 묻는지**를 호출부(파일:줄)별로 센다. 읽기만 하고 쓰기는
전부 막는다. **이 수가 곧 비용이다** — 아래 「왜 횟수가 비용인가」.

- 그 폴더의 `data/`를 본다. 파일 시절 값과 대 보려면 **운영 폴더에서** `DATABASE_URL= `을
  앞에 붙여 돌린다(워크트리에서 돌리면 워크트리의 빈 `data/`를 재서 빈 화면이 나온다).

## 바탕 숫자

| | |
|---|---|
| 왕복 한 번(`select 1`) | **중앙값 15.6ms** (최소 13.6 · 최대 29.3) |
| 첫 연결 | **1,174ms** |
| 저장소 | 키-값 JSON **703행 · 32MB** |
| 회차 파일(`articles/`) | **403개** (2026-08-11 ~ 11-05, 3개월 치) |
| 연결 주머니 | 6개 (`store_pg._MAX_CONNECTIONS`) |

## 화면별 실측

| 화면 | 지금(DB) | store 읽기 | 파일 시절 기록 |
|---|---|---|---|
| 확정본 `/index.html` | **24.3~24.5초** | 1,428~1,586회 | 24ms (`settings_server.py` 주석) · 0.02초 ([EXPERT_QUESTIONS.md](EXPERT_QUESTIONS.md) 실측 ①) |
| 정기 보관함 `/history.html` | **26.2초** | 2,086회 | 39ms (같은 주석) |
| 홈 `/home.html` | **70~73.5초** | 4,903~5,099회 | 39ms (같은 주석) |
| 스케줄러 tick(수집 없이 뒷일만) | **12.4초** | 821회 | — |

**마지막 줄이 핵심이다. tick은 10초마다 돈다**(`scheduler.POLL_INTERVAL_SEC`) — 한 바퀴가
자기 주기보다 오래 걸려, 배경 루프가 연결 주머니 6개를 늘 물고 있다. 그래서 담당자가 어느
화면을 열어도 줄을 서서 시작한다. **「앱 전체가 느리다」의 정체가 이것이다.**

## 왜 횟수가 비용인가

느린 쿼리는 하나도 없다. 전부 **파일일 때 맞았던 판단**이다 — 디스크에선 `.stat()`·열기가
1µs라 「자주 물어보고 조금씩 아끼기」가 이겼는데, DB에선 **묻는 횟수가 거의 유일한 비용**이다.
특히 「DB 전환이 절반만 끝났다」를 고치며 `.stat()`을 `store.signature()`로 바꾼 자리들이
(그 고침은 옳다 — 안 바꾸면 캐시가 매번 무효다) 공짜였던 stat을 네트워크 왕복으로 만들었다.

[EXPERT_QUESTIONS.md](EXPERT_QUESTIONS.md) §2의 「병목이 어디인가 — 셋 다 DB가 아니다」는
**DB를 붙이기 전의 판단**이라 지금은 절반만 맞다: 그 셋(GIL·세마포어 8·화면 캐시 없음)은
그대로 남아 있고, 그 위에 **그 셋을 압도하는 네 번째**가 얹혔다.

## 느린 자리 (큰 것부터)

### ① 회차 인덱스의 N+1 — 홈의 87%

`app/run_index.py:95-96` `_refresh_locked`가 `list_files()`로 회차 403개를 **이미 받아
놓고**, 파일마다 `store.signature()`를 한 번 더 묻는다. **403회 = 5.8초.**

| 부르는 화면 | load_entries 횟수 | signature 호출 | 시간 |
|---|---|---|---|
| 홈 | **11번** | 4,433회 | **63.8초** (= 홈 전체의 87%) |
| 확정본 | 2번 | 806회 | 11.7~13.0초 |
| 정기 보관함 | — | 1,482회(합계) | 19.9초 |
| tick(10초마다) | 2번 | 806회 | 11.7초 |

`run_index.load_entries()`의 주석 「평상시엔 glob + stat만 하고 회차 파일은 한 개도 열지
않는다」는 지금 **403번의 네트워크 왕복**을 뜻한다. `list_run_meta`·`load_latest_run`·
`load_latest_confirmed_run`·`list_runs_for_date`·`list_run_dates`가 전부 이 함수를 지나므로,
한 화면이 날짜를 여럿 훑으면 그만큼 되풀이된다.

### ② 「바뀌었나」 묻는 값이 캐시보다 비싸졌다

내용은 메모리에 캐시하는데 **기사마다 서명을 묻는다.** 아끼는 것은 JSON 파싱 1ms, 묻는
값은 15ms — 뒤집혔다.

| 자리 | 확정본 156건 화면에서 | 시간 |
|---|---|---|
| `app/photo_overrides.py:45` `_signature` | 156회 | 2.2~2.4초 |
| `app/schedule_overrides.py:45` `_signature` | 156회 | 2.4~3.2초 |
| `app/adhoc/card.py:605` `list_card_summaries` | (홈에서) 192회 | 2.7초 |
| `app/curation.py:124` `_archive_urls` | 6회 | 0.1초 |

### ③ 기사 하나하나가 덩어리 전체를 다시 읽는다

`app/labels.py:42-45` `load_labels()`에는 캐시가 아예 없다. 확정본에서 **106번** 불리고
매번 `exists()` + `read_json()` = **212회 왕복 · 3.5~6.6초.**

`app/renderer.py`에 `labels_lookup=`(회차마다 한 번만 읽어 넘기는 길)이 **이미 있고**
`history_renderer.py:1258`은 쓴다 — 확정본 경로가 안 쓴다.

### ④ `exists()` 하고 나서 `read()` — 왕복이 두 배

한 값을 읽는 데 두 번 묻는다. 그냥 읽고 `FileNotFoundError`를 받으면 한 번이면 된다.
`labels.py` · `custom_groups.py` · `curation.py`(`_read_recent_raw`·`_round_labels`·
`_read_override_records`·`_load_archive`) · `settings.py:227` · `manual_keyword_note.py:86` ·
`adhoc/card.py:479` · `alerted_urls.py:29` · `credentials.py:82`.

### ⑤ 큰 행을 통째로 주고받는다

| 키 | 크기 | 읽기 한 번 |
|---|---|---|
| `undo_stack.json` | **7,780KB** | **279ms** |
| `photo_body_verdicts.json` | 1,285KB | 44ms |
| `live_cache.json` | 926KB | 57ms |
| `llm_classification_cache.json` | 523KB | 30ms |
| `hidden_articles.json` | 272KB | 23ms |

`app/undo.py` `push()`는 큐레이션 한 번(숨기기·옮기기·이름 바꾸기)마다 `_load()`로 이걸
읽고 `_save()`로 다시 쓴다. **그 함수 안 주석의 「실측 7MB·17ms」는 디스크 값이다.**

> 담당자가 말하는 「소제목 숨기기가 느리다」 = undo 왕복(약 1초) + 숨김 쓰기 +
> **확정본 전체 재렌더링 24초**.

### ⑥ 화면을 요청마다 다시 그린다

`app/settings_server.py` `_SCREEN_HTML_ROUTES` — 예전엔 구워 둔 HTML을 내보냈다. 사람마다
다른 화면을 주려고 요청마다 렌더링으로 바꿨고, **그땐 24~39ms라 옳은 판단이었다.** 지금은
그 비용이 24~73초이고 새로고침마다 다시 낸다. 그 자리의 주석에 적힌 세 숫자가 지금은 틀렸다.

## 손 안 대면 저절로 나빠진다

회차 403개는 **3개월 치**이고 보관은 365일(`RETENTION_DAYS`)이다. 같은 속도로 쌓이면
약 1,600개가 되고, ①이 선형이라 **홈이 73초 → 약 5분**이 된다. 사람이 늘면 각자 자기
회차를 쌓으므로(③ 사용자 경계) 그 수에 다시 곱해진다.

## 정리할 것 (효과 순)

1. **목록 쿼리가 서명까지 한 번에 가져오기** — `store_pg.list_keys`가
   `select key, updated_at, length(body)`로 한 번에 받아 `_refresh_locked`의 403회를
   **1회**로. 더해서 `load_entries()`를 **요청 하나 안에서 한 번만**(지금 홈이 11번).
   → 홈 73초 → 10초 안쪽 · 확정본 24.5 → 12초 · **tick 12.4초 → 0.2초.**
2. **10초 tick에서 전수 스캔을 떼기** — 만료 삭제·정리(`cleanup`·`purge_deleted`·
   `cleanup_overrides`·`archive_hidden`)는 10초마다 할 일이 아니다. 하루 한 번이나 날짜가
   바뀔 때로. → 상시 배경 부하가 사라져 **모든 화면이 같이** 빨라진다.
   `run_scheduler` 주석의 「파일 수가 적어 매번 스캔해도 비용이 작다」를 같이 고친다.
3. **요청 범위 캐시 한 겹** — `app/store.py`에 「이 요청 동안 같은 키는 다시 묻지 않는다」를
   두고 쓰기 때 그 키만 버린다. → ②③④가 한 번에 덮인다(156·156·212회 → 각 1~2회).
4. **`exists()`+`read()` 합치기** — 읽고 예외를 받는 모양으로. → 왕복이 절반.
5. **`undo_stack.json`을 한 행에서 빼내기** — 항목과 blob을 각자 키로(이미 내용 해시로
   갈라져 있어 구조는 맞다), 또는 20단계 상한을 낮춘다.
6. 여기까지 하고도 모자라면 **재렌더링한 HTML을 짧게 캐시** — 데이터 서명이 안 바뀐
   동안만. ⑥을 되돌리지 않고(사람마다 다른 화면은 그대로) 비용만 깎는 길이다.

**고치는 방향은 하나다 — 적게 묻고 한 번에 많이 받기.** 로직을 바꾸는 일이 아니라, 파일
시절에 공짜였던 「확인」을 걷어내는 일이다.

## 고칠 때 같이 고칠 주석 (지금 숫자가 틀린 자리)

- `app/settings_server.py` `_SCREEN_HTML_ROUTES` 위 — 「확정본 24ms · 정기 보관함 39ms ·
  홈 39ms」
- `app/run_index.py` `load_entries` — 「glob + stat만 하고 회차 파일은 한 개도 열지 않는다」
- `app/scheduler.py` `run_scheduler` — 「파일 수가 적어 매번 스캔해도 비용이 작다」
- `app/undo.py` `push` — 「스택 파일이 수 MB라 쓰기 한 번에 수십 ms — 실측 7MB·17ms」
- `app/photo_overrides.py`·`app/schedule_overrides.py` 캐시 머리말 — 「파일을 그때마다 열지
  않도록」(지금은 열지 않는 대신 묻는다)
