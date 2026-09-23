# Design Ref: 사용자 요청(2026-08-10) — 담당자가 바빠서 검토를 못 마쳤는데도 회차가
# 검토 없이 그대로 나가는 걸 막기 위한 발송 유예.
#
# [수정: 2026-08-11] 처음엔 "확정"과 "발송" 2단계 유예로 만들었다 — 수집해도 곧바로
# 확정본이 되지 않고 "확정 대기" 상태로 두고, 담당자가 초안에서 (확정)을 눌러야
# 확정본이 되는 방식. 이 확정 단계를 통째로 없앴다:
#   (1) (확정)은 눌러도 확정본에서 계속 숨기기/이동/수정이 가능해 애초에 되돌릴 수
#       없는 동작이 아니었다 — 지킬 게 없는 걸 지키는 척하는 단계였다.
#   (2) 그 사이 초안이 "다음 회차 미리보기"가 아니라 "확정 대기 화면"을 보여줘서,
#       이용자가 "회차 시각이 지났는데 왜 다음 회차로 안 넘어가지?"라고 혼란스러워했다.
#   (3) 종료 시각 전에 미리 확정하는 "지금 마감하기"는 온라인 기사 특성과 근본적으로
#       충돌했다 — 회차 시간창이 닫히기 전에 닫아버리면 남은 구간 기사가 유실되거나
#       초안·확정본에 중복으로 뜨거나 둘 중 하나가 반드시 생긴다(가판 스크랩과 다른 점).
# 이제 회차는 수집되는 즉시 확정본이 되고(main._scrape_and_render가 collect 직후
# confirm_and_promote 호출), 담당자에게 남는 유일한 수동 조작은 확정본의 (발송)뿐이다.
# 그 발송만 CONFIRM_SEND_GRACE_SEC(기본 5분) 유예를 갖는다 — 진짜 되돌릴 수 없는
# 지점이 거기뿐이기 때문. 5분 안에 사람이 안 누르면 app.scheduler의 매 tick
# (check_pending_confirm_and_send)이 대신 보내고, 그 사실을 sent_by="auto"로 기록해
# 화면에 "🤖 N시 M분 자동발송 완료" 배지로 알려준다.
import logging
from datetime import datetime
from typing import Optional

from app.classifier import run_looks_rule_based
from app.email_recipients import active_recipient_emails, load_email_recipients
from app.email_sender import send_text as send_email_text
from app.history_renderer import generate_history_page
from app.landing_renderer import generate_landing_page
from app.renderer import generate_screen, latest_confirmed_report
from app import send_log
from app.report_message import content_labels, email_message, plan_messages, send_planned
from app.settings import auto_send_grace_sec, is_auto_send_enabled, load_settings
from app.storage import confirm_run, is_today, list_run_meta, load_latest_run, record_send, record_send_failure
from app.telegram_recipients import load_telegram_recipients, report_recipients

logger = logging.getLogger(__name__)

# [추가: 2026-09-23] 아무에게도 못 간 회차의 자동발송 재시도 제동.
# 스케줄러 tick이 10초라(app.scheduler.POLL_INTERVAL_SEC), 제동이 없으면 봇 토큰 만료나
# SMTP 차단처럼 사람이 고쳐야 풀리는 실패에서 같은 회차를 하루 종일 10초마다 다시 시도한다
# — 그때마다 발송이 스케줄러 스레드에서 동기로 돌아 수집·알림 tick까지 잡아먹는다.
# 1분 간격 5번(약 4분)은 네트워크 순단은 넘기고 영구 실패는 포기하는 선이다. 포기하면
# 발송 기록(app.send_log)에 한 줄 남겨 담당자가 직접 발송하도록 부른다 —
# 조용히 멈추면 안 된다(CODING_CONVENTIONS §3).
_MAX_AUTO_SEND_ATTEMPTS = 5
_AUTO_SEND_RETRY_GAP_SEC = 60.0
# {run_key: {"n": 시도 횟수, "at": 마지막 시도 ISO}} — 메모리에만 둔다(앱 재시작 시 초기화).
# 파일에 두면 tick마다 쓰기가 생기고, 재시작 뒤 다시 몇 번 시도해 보는 쪽이 안전한 방향이다
# (아무에게도 안 간 회차라 중복 발송 위험이 없다).
_auto_send_attempts: dict = {}


def seconds_since(iso_timestamp: str, now: Optional[datetime] = None) -> float:
    """ISO 타임스탬프로부터 지금까지 지난 초를 돌려준다 — 확정/전송 유예 경과 판단에 쓴다."""
    now = now or datetime.now()
    return (now - datetime.fromisoformat(iso_timestamp)).total_seconds()


def _today_already_has_other_confirmed_run(run: dict) -> bool:
    """오늘 이미 (이 회차 말고) 다른 확정된 회차가 있었는지 — 0건 회차가 그 회차 화면을
    덮어쓰지 않게 판단하는 데 쓴다(아래 confirm_and_promote 참고).

    run_slot(회차 식별자)으로 자기 자신을 제외한다 — run_at은 초 단위라 두 회차가 같은
    초에 연달아 저장되면(예: 테스트, 또는 놓친 회차 보충 실행) 값이 같아질 수 있어
    이 판단의 기준으로 쓸 수 없다.
    """
    # [수정: 2026-08-18] 판단에 필요한 건 confirmed·run_slot·run_at 셋뿐이라, 회차 파일을
    # 열지 않고 app.run_index의 메타만 본다(회차마다 수집 직후 불리는 자리).
    return any(
        meta["confirmed"] and meta["run_slot"] != run["run_slot"] and is_today(meta)
        for meta in list_run_meta()
    )


def confirm_and_promote(run: dict) -> None:
    """회차를 확정 처리하고 완성본 화면에 반영한다.

    초안의 (확정) 버튼과 자동 확정 타임아웃이 공유하는 동작이다 — 전송은 여기서 하지
    않는다(확정 직후 별도의 전송 유예가 새로 시작되므로, app.scheduler의 다음 tick이
    그 경과 여부를 따로 판단한다).

    [추가: 2026-08-10] 이 회차가 0건이고 오늘 이미 다른 확정 회차가 있었다면 완성본
    화면은 갱신하지 않고 그대로 둔다 — "10:30 기준"인데 사실은 아무것도 없다는 표시로
    바뀌는 것보다, 아직 유효한 이전 회차를 계속 보여주는 쪽이 낫다는 기존 판단(main.py
    옛 _scrape_and_render 로직을 여기로 옮겼다)을 그대로 유지한다. 확정 처리 자체(초안이
    비워지는 것)는 이 경우에도 그대로 일어난다 — 화면만 안 바뀔 뿐이다.
    """
    confirm_run(run)
    if not run["articles"] and _today_already_has_other_confirmed_run(run):
        generate_history_page()
        generate_landing_page()
        return
    generate_screen()
    generate_history_page()
    generate_landing_page()


def _channel_failures(channel_label: str, result, name_by_target: dict) -> list:
    """SendResult.failures(chat_id/이메일 기준)를 사람 이름으로 바꿔 회차 파일에 남길
    모양({"channel", "target", "error"})으로 정리한다.

    [추가: 2026-08-26] target이 None인 항목(채널 자체가 미설정)은 이름이 있을 수 없어
    그대로 둔다. 이름을 못 찾으면(등록 삭제 등) target 원본(chat_id/이메일)을 그대로
    보여준다 — 없는 이름을 지어내지 않는다.
    """
    if result is None:
        return []
    failures = []
    for f in result.failures:
        target = f["target"]
        label = name_by_target.get(target, target) if target else "설정"
        failures.append({"channel": channel_label, "target": label, "error": f["error"]})
    return failures


def _delivered_any(result) -> bool:
    """이 채널에서 **한 명이라도 실제로 받았는가**.

    [추가: 2026-09-23] 예전엔 `if telegram_result:`(= SendResult.ok = 대상 전원 성공)로
    판단했다. 그래서 받는 사람 셋 중 하나가 봇을 차단했거나 chat id에 오타가 있으면
    ok가 영영 False → send_count가 0에 머물고 → check_pending_confirm_and_send가
    **10초마다 같은 보고서를 다시 보냈다**. 멀쩡한 두 사람은 같은 회차를 하루 종일
    반복해서 받고, record_send_failure는 원인이 같으면 다시 안 쓰므로 화면에도 단서가
    남지 않았다(실측: 5 tick 연속 send_count=0).

    "이미 받은 사람에게 또 보내지 않는다"가 "못 받은 사람에게 다시 시도한다"보다
    우선이라, 판단 기준을 delivered(실제 도착한 사람)로 낮춘다. 못 받은 사람은
    record_send_failure → 정기 보관함 빨간 링 + 발송 기록에 그대로 남으므로 묻히지
    않는다(위 [추가: 2026-08-26] 문단이 세운 "성공은 했는데 일부는 못 받았다"를
    채널 단위에서 사람 단위로 넓힌 것뿐이다).

    delivered가 없는 옛/가짜 결과(테스트가 send_text를 bool로 바꿔치기하는 경우)는
    예전 판단(bool)으로 물러선다 — app.send_log.deliveries의 getattr 방어와 같은 이유.
    """
    if result is None:
        return False
    if getattr(result, "delivered", None):
        return True
    return bool(result)


def _run_key(run: dict) -> str:
    """발송 기록이 회차를 가리키는 키 — 「YYYY-MM-DD|HH:MM」(app.manual_keyword_note와 같은 모양)."""
    return f"{str(run.get('run_at', ''))[:10]}|{run.get('run_slot', '')}"


def _auto_retry_ready(run: dict, now: datetime) -> bool:
    """아무에게도 못 간 이 회차를 지금 다시 시도해도 되는지 — 첫 시도는 즉시, 그 뒤는
    _AUTO_SEND_RETRY_GAP_SEC 간격으로 _MAX_AUTO_SEND_ATTEMPTS번까지만."""
    key = _run_key(run)
    today = key.split("|", 1)[0]
    for stale in [k for k in _auto_send_attempts if k.split("|", 1)[0] != today]:
        del _auto_send_attempts[stale]
    rec = _auto_send_attempts.get(key)
    if rec is None:
        return True
    if rec["n"] >= _MAX_AUTO_SEND_ATTEMPTS:
        return False
    return seconds_since(rec["at"], now) >= _AUTO_SEND_RETRY_GAP_SEC


def _note_auto_attempt(run: dict, now: datetime) -> int:
    """자동발송을 한 번 시도했다고 적고, 지금까지 몇 번째인지 돌려준다."""
    key = _run_key(run)
    n = (_auto_send_attempts.get(key) or {}).get("n", 0) + 1
    _auto_send_attempts[key] = {"n": n, "at": now.isoformat(timespec="seconds")}
    return n


def _record_send_log(run: dict, auto: bool, rows: list, attempted: bool = True, note: str = "") -> None:
    """정기 확정본 발송 한 번을 발송 기록(app.send_log)에 남긴다.

    자동발송은 실패하면 tick마다 다시 시도하므로 같은 결과는 한 줄로 합친다(coalesce).
    받는 사람이 아무도 없으면(두 채널 다 안 씀) "보내지 않음"으로 한 줄.
    """
    key = _run_key(run)
    skipped = bool(note) or not attempted
    send_log.record(
        "regular",
        f"{run.get('run_slot', '')} 확정본",
        rows,
        auto=auto,
        send_no=0 if skipped else run.get("send_count", 0) + 1,
        run_key=key,
        note=note or ("받는 사람이 없어요 — 텔레그램·이메일 받는 사람에서 정기 칸을 켜주세요" if not attempted else ""),
        skipped=skipped,
        coalesce=f"regular-auto:{key}" if auto else "",
    )


def send_confirmed_run(run: dict, text_override: Optional[str] = None, auto: bool = False) -> tuple[dict, bool]:
    """확정된 회차를 텔레그램·이메일로 전송한다(설정된 채널만, 없으면 그 채널은 건너뜀).

    완성본의 (전송) 버튼과 자동 전송 타임아웃이 공유하는 동작이다. 이미 한 번 이상
    보낸 회차를 다시 보내는 경우(완성본에서 손본 뒤 재전송) 제목 앞에 "(수정)"을 붙여,
    받는 사람이 이전과 다른 갱신본임을 알 수 있게 한다(app.storage의 send_count 기준).

    text_override: (전송) 버튼이 fetch로 넘기는, 화면에 이미 렌더링된 PLAIN_TEXT —
    서버가 다시 계산한 상태가 아니라 지금 화면 그대로(직전 수정 포함)를 보내기 위해서다
    (기존 개별 Telegram/Email 버튼과 같은 이유). 자동 전송 타임아웃처럼 화면이 열려있지
    않을 때는 생략되며, 그때는 latest_confirmed_report()로 서버가 직접 계산한다.

    auto: [추가: 2026-08-10] 담당자가 (전송)을 직접 눌렀는지(False), 담당자가 반응하지
    않아 시스템이 대신 처리했는지(True) — 실제로 전송에 성공했을 때만
    app.storage.record_send로 sent_by/sent_at을 남긴다(app.renderer의 "🤖 자동 전송"
    배지 판단 근거).

    [수정: 2026-08-24] 채널이 하나도 설정 안 됐거나(텔레그램 미설정+이메일 미설정)
    전송 자체가 실패했을 때도 이전엔 무조건 record_send부터 불러 send_count를 늘리고
    "발송 완료"로 기록해버렸다 — 봇 토큰이 만료되거나 SMTP가 막혀도 화면·자동전송
    양쪽에서 "보냈다"고 거짓으로 표시되는 사고였다(실제로 아무 채널도 안 나갔는데도
    수동 버튼이 "이메일 및 텔레그램으로 발송되었습니다" 토스트를 띄운 걸 확인했다).
    이제 실제로 기사가 나간 채널이 하나라도 있을 때만 record_send를 부른다 — 반환값
    두 번째 요소(bool)가 그 여부다. 아무 것도 안 나갔으면 회차는 그대로 두고 False를
    돌려줘, 호출부가 "발송 완료"가 아니라 실패로 처리하게 한다(자동 전송은 다음 tick에
    다시 시도된다 — 단 무한히는 아니다, check_pending_confirm_and_send 참고).

    [수정: 2026-09-23] 그 "실제로 나갔는가"의 기준을 채널 전원 성공(SendResult.ok)에서
    **한 명이라도 도착(_delivered_any)**으로 낮췄다 — 이유는 _delivered_any 참고.

    [추가: 2026-08-26] 실패한 채널·대상·이유를 app.storage.record_send_failure로
    회차 파일에 같이 남긴다(정기 보관함의 빨간 점 툴팁이 읽는 값) — "채널 하나라도
    전원 성공하면 발송 완료로 본다"는 위 판단(send_count 기준)은 안 바꾼다: 텔레그램이
    부분 실패해도 이메일이 전원 성공했으면 여전히 "발송 완료"이지만, 텔레그램 쪽
    실패는 send_failure에 별도로 남아 "성공은 했는데 일부는 못 받았다"를 놓치지 않는다.
    받는 사람이 없어(채널을 아예 안 쓰기로 함) send_text 자체를 안 부른 채널은 실패로
    치지 않는다 — 안 쓰는 채널까지 "실패"로 보이면 안 쓰는 게 정상인 회차마다 매번
    빨간 점이 뜨는 거짓 경보가 된다.
    """
    # [수정: 2026-09-17] 요약도 같이 만든다 — 텔레그램 받는 사람마다 기사·요약을 고른다
    # (app.report_message). 요약은 화면이 보내주지 않으므로 늘 서버가 계산하고, 소제목은
    # 기사 목록과 같은 스냅샷을 본다(latest_confirmed_report).
    report = latest_confirmed_report()
    plain_text = text_override if text_override is not None else (report["text"] if report else None)
    if not plain_text:
        return run, False
    summary_text = report["summary"] if report else ""

    # send_count는 아직 늘리지 않은 원래 값 — "이미 한 번 이상 보낸 적 있다"만 보면 된다
    # (record_send가 한 뒤의 값과 달리 여기선 미리 늘릴 필요가 없다: 전송 성공 여부를
    # 먼저 확인한 뒤에만 늘려야 하므로).
    is_revision = run.get("send_count", 0) >= 1
    header, _, rest = plain_text.partition("\n")
    text_to_send = f"(수정){header}\n{rest}" if is_revision else plain_text

    sent_any = False
    telegram_result = None
    degraded = run_looks_rule_based(run)
    tg_recipients = report_recipients("regular")
    if tg_recipients:
        plan = plan_messages(tg_recipients, "regular", text_to_send, summary_text, degraded=degraded)
        telegram_result = send_planned(plan)
        if _delivered_any(telegram_result):
            sent_any = True

    email_result = None
    recipients = active_recipient_emails()
    if recipients:
        email_result = send_email_text(
            text_to_send.split("\n", 1)[0], email_message(text_to_send, summary_text, degraded), recipients
        )
        if _delivered_any(email_result):
            sent_any = True

    tg_names = {r["chat_id"]: r.get("name") for r in load_telegram_recipients()}
    email_names = {r["email"]: r.get("name") for r in load_email_recipients()}
    failures = _channel_failures("텔레그램", telegram_result, tg_names) + _channel_failures(
        "이메일", email_result, email_names
    )
    # 이메일은 사람마다 고르는 칸이 없어 모두 같은 것을 받는다(email_message와 같은 판단).
    email_content = "기사 + 요약" if summary_text and not degraded else "기사 목록"
    _record_send_log(
        run, auto,
        send_log.deliveries(
            "telegram", telegram_result, tg_names,
            content_labels(tg_recipients, "regular", summary_text, degraded),
        )
        + send_log.deliveries("email", email_result, email_names, {r: email_content for r in recipients}),
        attempted=bool(tg_recipients or recipients),
    )

    updated_run = run
    if sent_any:
        updated_run = record_send(updated_run, auto=auto) or updated_run
    if failures or updated_run.get("send_failure"):
        updated_run = record_send_failure(updated_run, failures, auto=auto) or updated_run

    return updated_run, sent_any


def check_pending_confirm_and_send(now: Optional[datetime] = None) -> None:
    """스케줄러 tick마다 호출한다 — 수집된 지 유예 시간이 지났는데 아직 발송 안 된 회차를
    자동으로 발송한다.

    [수정: 2026-08-11] "확정" 개념이 없어져(회차는 수집 즉시 확정됨,
    main._scrape_and_render) 이 함수가 실제로 하는 일은 자동 "발송" 하나뿐이다 —
    run_at + 유예 시간이 지났는데 담당자가 (발송)을 안 눌렀으면 (send_count==0) 시스템이
    대신 보낸다(auto=True로 기록해 화면에 "🤖 자동발송 완료" 배지가 뜨게 한다). 아래 확정
    분기는 이 변경 전에 "확정 대기" 상태로 저장돼 남아있을 수 있는 옛 회차를 위한 안전망으로만
    남겨둔다 — 새로 수집되는 회차는 이미 확정된 상태로 들어오므로 평소엔 타지 않는다.

    [수정: 2026-08-11] 자동발송 사용 여부와 유예 시간을 설정 화면(/auto-send)에서 읽는다.
    예전엔 `/telegram`·`/email`에 채널별 "자동 전송" 체크박스가 있었지만 이 경로가 그 값을
    읽지 않아 **꺼놔도 그냥 나갔다**(고아 설정). 끄면 담당자가 (발송)을 누를 때까지 나가지
    않는다.

    [추가: 2026-08-12] 이 회차의 소제목 분류가 LLM 실패로 규칙 기반(단어 빈도)에 떨어졌으면
    (run["classification_degraded"]) 자동발송을 하지 않는다 — 2026-08-11 17시 회차가
    `<대통령>`/`<부총리>` 같은 소제목이 망가진 채로 아무도 모르게 자동발송된 사고가
    있었다. "자동화가 스스로 실패를 감지했을 때만 사람을 부른다"는 원칙(사용자 합의) —
    정상일 땐 지금처럼 손 안 대고 나가고, 실패를 감지했을 때만 멈춰서 확인을 기다린다.
    확정본 화면(app.renderer.render_page)이 이 필드를 보고 카운트다운 대신 경고를
    보여준다. 담당자가 확인 후 (발송)을 직접 누르면 그때는 정상적으로 나간다.
    """
    now = now or datetime.now()
    settings = load_settings()
    if not is_auto_send_enabled(settings):
        return
    run = load_latest_run()
    if run is None:
        return
    if seconds_since(run["run_at"], now) < auto_send_grace_sec(settings):
        return

    if not run.get("confirmed", True):
        confirm_and_promote(run)
        run = load_latest_run()

    # [수정: 2026-08-27] run.get("classification_degraded") 하나만 보던 것을
    # run_looks_rule_based로 바꿨다 — 그 필드는 저장 직후 confirm_run이 덮어써서
    # 지워지고 있었고(app.scraper.collect_run 참고), 그래서 이 자동발송 차단은 도입
    # 이후 한 번도 동작한 적이 없다. 실제로 2026-08-27 06:00 회차가 규칙 기반 폴백
    # 상태(소제목이 '이를'·'한다'·'했다')로 아무 경고 없이 자동발송됐다.
    # 배선은 고쳤지만(그 회차부터 필드가 남는다) 이미 저장된 옛 회차는 필드가 없으므로,
    # 저장값이 있으면 그걸 쓰고 없을 때만 소제목 이름 모양으로 추정하는 쪽을 쓴다.
    if run_looks_rule_based(run):
        # 한 번도 안 보낸 회차만 적는다(보낸 뒤에도 이 분기는 tick마다 지나간다). 같은 줄은 한 번만.
        if run.get("send_count", 0) == 0:
            _record_send_log(run, True, [], note="자동발송 건너뜀 — AI 분류 실패, 확인 후 직접 발송해 주세요")
        return

    if run.get("send_count", 0) == 0:
        # [추가: 2026-09-23] 재시도 제동 — 아무에게도 못 간 회차만 여기 걸린다
        # (한 명이라도 받았으면 record_send가 send_count를 올려 이 분기를 안 탄다).
        if not _auto_retry_ready(run, now):
            return
        _, sent = send_confirmed_run(run, auto=True)
        tried = _note_auto_attempt(run, now)
        if not sent:
            # 채널 미설정이거나 아무에게도 못 간 경우 — send_count가 그대로라 다음
            # 시도가 _AUTO_SEND_RETRY_GAP_SEC 뒤에 온다. 텔레그램/이메일 쪽 send_text가
            # 이미 구체적인 원인을 warning으로 남기므로 여긴 "안 나갔다"는 사실만 남긴다.
            if tried >= _MAX_AUTO_SEND_ATTEMPTS:
                logger.warning(
                    "자동발송 %d번 모두 실패(run_slot=%s) — 더 시도하지 않습니다. 확인 후 직접 발송해주세요",
                    tried, run.get("run_slot"),
                )
                _record_send_log(
                    run, True, [],
                    note=f"자동발송을 {tried}번 시도했지만 아무에게도 가지 못했어요 — 확인 후 직접 발송해주세요",
                )
            else:
                logger.warning(
                    "자동발송이 나가지 않았습니다(run_slot=%s, %d/%d번째) — 채널 미설정이거나 전송 실패, 잠시 뒤 다시 시도합니다",
                    run.get("run_slot"), tried, _MAX_AUTO_SEND_ATTEMPTS,
                )
        # [추가: 2026-08-26] 성공이든 실패든 확정본(index.html)을 다시 그린다 — 예전엔
        # 이 tick이 회차 파일만 갱신하고 화면은 안 그려서, send_count·sent_by(성공)나
        # send_failure(실패)가 저장돼도 index.html은 그대로였다. 다음 회차가 오거나
        # 누군가 다른 큐레이션 동작을 해서 우연히 다시 그려질 때까지 화면이 계속 옛
        # 상태(카운트다운이 0에서 멈춘 빈 자리)로 남는 문제 — 특히 "자동으로 계속
        # 재시도하는데 아무도 화면을 안 건드리는" 실패 상황에서 가장 심했다(발송 실패를
        # 알아채야 할 바로 그 순간에 화면이 안 바뀜). generate_screen()은 이미 확정된
        # 이 run을 다시 읽어 그리는 정도라 가볍고, 실패가 지속돼도 10초마다 반복 호출될
        # 뿐 새로운 부작용은 없다.
        try:
            generate_screen()
        except RuntimeError:
            pass
