# Design Ref: 사용자 요청(2026-08-26) — 발송 실패 시 "왜 안 됐는지"를 화면(정기 보관함
# 회차 옆 빨간 점 툴팁)에서 보여주려면, app.telegram_bot/app.email_sender가 단순
# True/False가 아니라 실패 이유까지 함께 돌려줘야 한다.
#
# 두 모듈이 같은 모양의 결과 타입을 쓰도록 여기 하나로 뺐다 — app.confirm_send가 두
# 채널의 실패를 한 목록으로 합쳐야 하므로, 모양이 다르면 합치는 코드가 채널마다
# 갈라진다.
class SendResult:
    """전송 결과 — 성공 여부와 실패한 대상별 이유를 함께 담는다.

    bool()로 평가하면 예전 방식(단순 True/False 반환)과 완전히 같게 동작한다 —
    `if send_telegram_text(...):` 같은 기존 호출부를 하나도 안 고쳐도 되게 하기
    위해서다(app.breaking_alert_sender는 반환값 자체를 안 보므로 더더욱 영향이 없다).
    실패 이유가 필요한 새 호출부(app.confirm_send)만 .failures를 들여다본다.

    ok는 "대상 전원이 성공했는가"다(부분 성공은 실패로 친다) — 기존
    app.telegram_bot.send_text/app.email_sender.send_text가 원래 그렇게 판단해왔던
    것을 그대로 유지한다(app.confirm_send가 "채널 하나라도 전원 성공하면 발송 완료로
    본다"는 그 위의 판단과 헷갈리지 않게, 여기서는 판단 기준을 바꾸지 않는다).
    """

    def __init__(self, ok: bool, failures: list = None, delivered: list = None):
        self.ok = ok
        # 각 원소: {"target": chat_id/이메일 주소 또는 None(채널 자체 문제), "error": str}
        self.failures = failures or []
        # 받은 사람 — {"target", "silent"(텔레그램, 알림 없이 갔는가), "parts"(몇 통으로 나눴나)}.
        # 발송 기록(app.send_log)이 "누구에게 도착했는지"를 적으려면 실패만으로는 모자란다.
        self.delivered = delivered or []

    def __bool__(self) -> bool:
        return self.ok

    def __repr__(self) -> str:
        return f"SendResult(ok={self.ok}, failures={self.failures}, delivered={self.delivered})"
