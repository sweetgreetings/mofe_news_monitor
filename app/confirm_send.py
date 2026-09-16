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
# 화면에 "🤖 N시 M분 자동 발송 완료" 배지로 알려준다.
import logging
from datetime import datetime
from typing import Optional

from app.classifier import run_looks_rule_based
from app.email_recipients import active_recipient_emails, load_email_recipients
from app.email_sender import send_text as send_email_text
from app.history_renderer import generate_history_page
from app.landing_renderer import generate_landing_page
from app.renderer import build_latest_plain_text, generate_screen
from app.settings import auto_send_grace_sec, is_auto_send_enabled, load_settings
from app.storage import confirm_run, is_today, list_run_meta, load_latest_run, record_send, record_send_failure
from app.telegram_bot import send_text as send_telegram_text
from app.telegram_recipients import active_recipient_chat_ids, load_telegram_recipients

logger = logging.getLogger(__name__)


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


def send_confirmed_run(run: dict, text_override: Optional[str] = None, auto: bool = False) -> tuple[dict, bool]:
    """확정된 회차를 텔레그램·이메일로 전송한다(설정된 채널만, 없으면 그 채널은 건너뜀).

    완성본의 (전송) 버튼과 자동 전송 타임아웃이 공유하는 동작이다. 이미 한 번 이상
    보낸 회차를 다시 보내는 경우(완성본에서 손본 뒤 재전송) 제목 앞에 "(수정)"을 붙여,
    받는 사람이 이전과 다른 갱신본임을 알 수 있게 한다(app.storage의 send_count 기준).

    text_override: (전송) 버튼이 fetch로 넘기는, 화면에 이미 렌더링된 PLAIN_TEXT —
    서버가 다시 계산한 상태가 아니라 지금 화면 그대로(직전 수정 포함)를 보내기 위해서다
    (기존 개별 Telegram/Email 버튼과 같은 이유). 자동 전송 타임아웃처럼 화면이 열려있지
    않을 때는 생략되며, 그때는 build_latest_plain_text()로 서버가 직접 계산한다.

    auto: [추가: 2026-08-10] 담당자가 (전송)을 직접 눌렀는지(False), 담당자가 반응하지
    않아 시스템이 대신 처리했는지(True) — 실제로 전송에 성공했을 때만
    app.storage.record_send로 sent_by/sent_at을 남긴다(app.renderer의 "🤖 자동 전송"
    배지 판단 근거).

    [수정: 2026-08-24] 채널이 하나도 설정 안 됐거나(텔레그램 미설정+이메일 미설정)
    전송 자체가 실패했을 때도 이전엔 무조건 record_send부터 불러 send_count를 늘리고
    "발송 완료"로 기록해버렸다 — 봇 토큰이 만료되거나 SMTP가 막혀도 화면·자동전송
    양쪽에서 "보냈다"고 거짓으로 표시되는 사고였다(실제로 아무 채널도 안 나갔는데도
    수동 버튼이 "이메일 및 텔레그램으로 발송되었습니다" 토스트를 띄운 걸 확인했다).
    이제 send_telegram_text/send_email_text가 실제로 True(성공)를 돌려준 채널이
    하나라도 있을 때만 record_send를 부른다 — 반환값 두 번째 요소(bool)가 그 여부다.
    아무 것도 안 나갔으면 회차는 그대로 두고 False를 돌려줘, 호출부가 "발송 완료"가
    아니라 실패로 처리하게 한다(자동 전송은 다음 tick에 다시 시도된다).

    [추가: 2026-08-26] 실패한 채널·대상·이유를 app.storage.record_send_failure로
    회차 파일에 같이 남긴다(정기 보관함의 빨간 점 툴팁이 읽는 값) — "채널 하나라도
    전원 성공하면 발송 완료로 본다"는 위 판단(send_count 기준)은 안 바꾼다: 텔레그램이
    부분 실패해도 이메일이 전원 성공했으면 여전히 "발송 완료"이지만, 텔레그램 쪽
    실패는 send_failure에 별도로 남아 "성공은 했는데 일부는 못 받았다"를 놓치지 않는다.
    받는 사람이 없어(채널을 아예 안 쓰기로 함) send_text 자체를 안 부른 채널은 실패로
    치지 않는다 — 안 쓰는 채널까지 "실패"로 보이면 안 쓰는 게 정상인 회차마다 매번
    빨간 점이 뜨는 거짓 경보가 된다.
    """
    plain_text = text_override if text_override is not None else build_latest_plain_text()
    if not plain_text:
        return run, False

    # send_count는 아직 늘리지 않은 원래 값 — "이미 한 번 이상 보낸 적 있다"만 보면 된다
    # (record_send가 한 뒤의 값과 달리 여기선 미리 늘릴 필요가 없다: 전송 성공 여부를
    # 먼저 확인한 뒤에만 늘려야 하므로).
    is_revision = run.get("send_count", 0) >= 1
    header, _, rest = plain_text.partition("\n")
    text_to_send = f"(수정){header}\n{rest}" if is_revision else plain_text

    sent_any = False
    telegram_result = None
    chat_ids = active_recipient_chat_ids()
    if chat_ids:
        telegram_result = send_telegram_text(text_to_send, chat_ids)
        if telegram_result:
            sent_any = True

    email_result = None
    recipients = active_recipient_emails()
    if recipients:
        email_result = send_email_text(text_to_send.split("\n", 1)[0], text_to_send, recipients)
        if email_result:
            sent_any = True

    failures = _channel_failures(
        "텔레그램", telegram_result, {r["chat_id"]: r.get("name") for r in load_telegram_recipients()}
    ) + _channel_failures(
        "이메일", email_result, {r["email"]: r.get("name") for r in load_email_recipients()}
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
    대신 보낸다(auto=True로 기록해 화면에 "🤖 자동 발송 완료" 배지가 뜨게 한다). 아래 확정
    분기는 이 변경 전에 "확정 대기" 상태로 저장돼 남아있을 수 있는 옛 회차를 위한 안전망으로만
    남겨둔다 — 새로 수집되는 회차는 이미 확정된 상태로 들어오므로 평소엔 타지 않는다.

    [수정: 2026-08-11] 자동 발송 사용 여부와 유예 시간을 설정 화면(/auto-send)에서 읽는다.
    예전엔 `/telegram`·`/email`에 채널별 "자동 전송" 체크박스가 있었지만 이 경로가 그 값을
    읽지 않아 **꺼놔도 그냥 나갔다**(고아 설정). 끄면 담당자가 (발송)을 누를 때까지 나가지
    않는다.

    [추가: 2026-08-12] 이 회차의 소제목 분류가 LLM 실패로 규칙 기반(단어 빈도)에 떨어졌으면
    (run["classification_degraded"]) 자동발송을 하지 않는다 — 2026-08-11 17시 회차가
    `<대통령>`/`<부총리>` 같은 소제목이 망가진 채로 아무도 모르게 자동 발송된 사고가
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
        return

    if run.get("send_count", 0) == 0:
        _, sent = send_confirmed_run(run, auto=True)
        if not sent:
            # 채널 미설정이거나 전송 자체가 실패한 경우 — send_count가 그대로라
            # 다음 tick(POLL_INTERVAL_SEC)에 자동으로 다시 시도된다. 텔레그램/이메일
            # 쪽 send_text가 이미 구체적인 원인을 warning으로 남기므로 여기선 "자동
            # 발송이 안 나갔다"는 사실만 남긴다.
            logger.warning(
                "자동 발송이 나가지 않았습니다(run_slot=%s) — 채널 미설정이거나 전송 실패, 다음 tick에 재시도합니다",
                run.get("run_slot"),
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
