"""로그인·회원가입·비밀번호 찾기·나의 정보 관리 화면.

시안 MULTIUSER_AUTH_MOCKUP.html 그대로이고, 색·글자·모양은 앱 토큰(PALETTE·
SHAPE_TOKENS_CSS)에서 온다 — 값을 여기에 적지 않는다.

**로그인 전 화면엔 상단바가 없다** — 갈 수 있는 곳이 없어서다(홈도 그렇다).
그래서 상단바 CSS가 함께 내 주는 모양 토큰을 이 화면들은 직접 낸다.
"""
import html
from typing import Optional

from app.config import (
    AUTH_MIN_PASSWORD_LEN,
    FONT_STACK,
    PALETTE,
    SECURITY_QUESTIONS,
    SETTINGS_SERVER_HOST,
    SETTINGS_SERVER_PORT,
    SHAPE_TOKENS_CSS,
)
from app.topnav import plain_nav, topnav_style

_SETTINGS_HOME = f"http://{SETTINGS_SERVER_HOST}:{SETTINGS_SERVER_PORT}/"

# ── 공통 스타일 ──────────────────────────────────────────────────────────
_FORM_STYLE = """
  body {{ margin: 0; background: {bg}; color: {text}; font-family: {font_stack}; }}

  .login-wrap {{ display: flex; justify-content: center; padding: 48px 16px; }}
  .login-card {{
    background: {card}; border: 1px solid {border}; border-radius: var(--r-lg);
    padding: 30px 28px 26px; box-sizing: border-box; width: 400px;
  }}
  .login-card.sign {{ width: 440px; }}

  .lg-head {{ display: flex; align-items: center; gap: 14px; margin: 0 0 22px; }}
  .lg-head .logo {{ height: 34px; width: auto; flex-shrink: 0; display: block; }}
  .lg-head .ttl {{ display: flex; flex-direction: column; gap: 1px; min-width: 0;
    padding-left: 14px; border-left: 1px solid {border}; }}
  .lg-head h1 {{ font-size: var(--fs-xl); color: {header}; margin: 0; }}
  .lg-head .sub {{ font-size: var(--fs-sm); color: {muted}; margin: 0; }}

  .f label.field {{ display: block; font-size: var(--fs-md); font-weight: 600;
    color: {header}; margin: 18px 0 6px; }}
  .f label.field:first-of-type {{ margin-top: 0; }}
  .f input, .f select {{
    background: {bg}; color: {text}; border: 1px solid {border}; border-radius: var(--r-md);
    padding: 8px 10px; font-size: var(--fs-base); font-family: inherit;
    width: 100%; box-sizing: border-box;
  }}
  .f select {{ height: var(--h-lg); padding: 0 10px; }}
  .f input:focus, .f select:focus {{ outline: none; border-color: {accent}; box-shadow: 0 0 0 3px {hover}; }}
  .f input::placeholder {{ color: {placeholder}; }}
  .f input[readonly] {{ background: {pill_bg}; color: {muted}; }}

  .fhint {{ font-size: var(--fs-sm); color: {muted}; margin: 6px 0 0; line-height: 1.5; }}

  .keep {{ display: flex; align-items: center; gap: 7px; margin: 16px 0 0;
    font-size: var(--fs-sm); color: {muted}; }}
  .keep input[type=checkbox] {{ width: var(--chk-sm); height: var(--chk-sm);
    accent-color: {accent}; flex: none; margin: 0; }}

  .f button.primary {{
    background: {accent}; color: {on_fill}; border: none; border-radius: var(--r-md);
    height: var(--h-lg); padding: 0 16px; font-size: var(--fs-base); font-weight: 600;
    font-family: inherit; cursor: pointer; appearance: none; -webkit-appearance: none;
    display: inline-flex; align-items: center; justify-content: center;
    width: 100%; margin-top: 18px;
  }}
  .f button.primary:hover {{ background: {header}; }}
  .f button.ghost {{
    background: {card}; color: {accent}; border: 1px solid {accent_border};
    border-radius: var(--r-md); height: var(--h-lg); padding: 0 14px;
    font-size: var(--fs-md); font-weight: 600; font-family: inherit; cursor: pointer;
  }}
  .f button.ghost:hover {{ background: {hover}; border-color: {ghost_border_hover}; }}

  .status {{ display: flex; gap: 8px; align-items: flex-start; padding: 10px 12px;
    border-radius: var(--r-md); font-size: var(--fs-md); line-height: 1.5; margin: 0 0 18px; }}
  .status-warn {{ background: {warn_bg}; color: {warn_text}; border: 1px solid {warn_border}; }}
  .status-info {{ background: {hover}; color: {header}; border: 1px solid {accent_border}; }}
  .status-ok {{ background: {ok_bg}; color: {ok_text}; border: 1px solid {ok_border}; }}

  .foot {{ margin: 18px 0 0; font-size: var(--fs-sm); color: {muted}; text-align: center; }}
  .foot a {{ color: {accent}; text-decoration: none; }}
  .foot a:hover {{ text-decoration: underline; }}
  .foot .dot {{ color: {border}; margin: 0 7px; }}

  .steps {{ display: flex; align-items: center; gap: 6px; margin: 0 0 20px; }}
  .steps .st {{ font-size: var(--fs-xs); color: {muted}; background: {pill_bg};
    border: 1px solid {border}; border-radius: var(--r-pill); padding: 2px 10px; }}
  .steps .st.on {{ color: {on_fill}; background: {accent}; border-color: {accent}; }}
  .steps .ar {{ color: {flow_arrow}; font-size: var(--fs-sm); }}

  .qa-box {{ background: {bg}; border: 1px solid {border}; border-radius: var(--r-md);
    padding: 12px 14px; margin: 0; }}
  .qa-box .q {{ font-size: var(--fs-md); font-weight: 600; color: {header}; margin: 0 0 8px; }}

  @media (max-width: 480px) {{
    .login-card, .login-card.sign {{ width: 100%; max-width: 440px; }}
    .lg-head {{ flex-wrap: wrap; }}
    .lg-head .ttl {{ padding-left: 0; border-left: 0; }}
  }}
"""

_PRELOGIN_PAGE = (
    """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>{tab_title}</title>
<style>{shape_tokens}"""
    + _FORM_STYLE
    + """</style>
</head>
<body>
<div class="login-wrap">
  <div class="login-card{card_cls}">
    <header class="lg-head">
      <img class="logo" src="/logo.svg" alt="">
      <div class="ttl"><h1>{title}</h1><p class="sub">{sub}</p></div>
    </header>
    {notice_html}
    {body_html}
    {foot_html}
  </div>
</div>
</body>
</html>
"""
)


def _theme() -> dict:
    return {**PALETTE, "font_stack": FONT_STACK}


def _page(tab_title, title, sub, body_html, foot_html="", notice_html="", wide=False) -> str:
    return _PRELOGIN_PAGE.format(
        **_theme(),
        shape_tokens=SHAPE_TOKENS_CSS,
        tab_title=tab_title,
        card_cls=" sign" if wide else "",
        title=html.escape(title),
        sub=html.escape(sub),
        body_html=body_html,
        foot_html=foot_html,
        notice_html=notice_html,
    )


def _warn(text: str) -> str:
    if not text:
        return ""
    return f'<div class="status status-warn"><span>⚠</span><span>{text}</span></div>'


def _ok(text: str) -> str:
    return f'<div class="status status-ok"><span>✔</span><span>{text}</span></div>'


def _question_options(selected: str = "") -> str:
    rows = []
    for q in SECURITY_QUESTIONS:
        mark = " selected" if q == selected else ""
        rows.append(f'<option value="{html.escape(q)}"{mark}>{html.escape(q)}</option>')
    return "".join(rows)


# ── 1. 로그인 ────────────────────────────────────────────────────────────

def render_login_page(error: str = "", email: str = "", notice: str = "") -> str:
    body = f"""
    <form class="f" method="POST" action="/login">
      <label class="field" for="email">이메일</label>
      <input type="email" id="email" name="email" value="{html.escape(email)}"
             placeholder="name@korea.kr" autocomplete="username" autofocus>
      <label class="field" for="password">비밀번호</label>
      <input type="password" id="password" name="password" autocomplete="current-password">
      <label class="keep"><input type="checkbox" name="keep" value="1" checked> 로그인 상태 유지</label>
      <button class="primary" type="submit">로그인</button>
    </form>"""
    foot = '<p class="foot"><a href="/forgot">비밀번호 찾기</a><span class="dot">·</span><a href="/signup">회원가입</a></p>'
    notice_html = _ok(notice) if notice else _warn(error)
    return _page("로그인", "AI 뉴스 모니터링", "언론 모니터링", body, foot, notice_html)


# ── 2. 회원가입 ──────────────────────────────────────────────────────────

def render_signup_page(error: str = "", form: Optional[dict] = None, first: bool = False) -> str:
    f = form or {}

    def v(key: str) -> str:
        return html.escape((f.get(key) or [""])[0] if isinstance(f.get(key), list) else (f.get(key) or ""))

    first_note = (
        '<div class="status status-info"><span>ⓘ</span><span>'
        "<strong>첫 계정이라 관리자로 기록됩니다.</strong> 지금은 다른 계정과 똑같이 "
        "동작하고, 나중에 사람이 늘 때 쓸 표시입니다.</span></div>"
        if first else ""
    )
    body = f"""
    <form class="f" method="POST" action="/signup">
      <label class="field" for="email">이메일</label>
      <input type="email" id="email" name="email" value="{v('email')}"
             placeholder="name@korea.kr" autocomplete="username" autofocus>
      <p class="fhint">이 이메일이 아이디입니다. 나중에 바꿀 수 없습니다.</p>

      <label class="field" for="password">비밀번호</label>
      <input type="password" id="password" name="password" autocomplete="new-password">
      <p class="fhint">{AUTH_MIN_PASSWORD_LEN}자 이상으로 적어 주세요.</p>

      <label class="field" for="password2">비밀번호 확인</label>
      <input type="password" id="password2" name="password2" autocomplete="new-password">

      <label class="field" for="display_name">이름</label>
      <input type="text" id="display_name" name="display_name" value="{v('display_name')}"
             placeholder="홍길동" autocomplete="name">

      <label class="field" for="department">부서</label>
      <input type="text" id="department" name="department" value="{v('department')}"
             placeholder="디지털소통팀" autocomplete="organization">
      <p class="fhint">이름과 부서는 화면에 누구인지 보이려고 받습니다.
      본인 확인에는 쓰지 않고, 부서는 비워 둬도 됩니다.</p>

      <div class="status status-info" style="margin: 22px 0 0">
        <span>ⓘ</span>
        <span>아래 둘은 <strong>비밀번호를 잊었을 때 본인 확인에만</strong> 씁니다.
        문자(SMS)는 보내지 않고, 적어 둔 값과 맞는지만 대조합니다.</span>
      </div>

      <label class="field" for="phone">핸드폰번호</label>
      <input type="tel" id="phone" name="phone" value="{v('phone')}"
             placeholder="010-1234-5678" autocomplete="tel">

      <label class="field" for="question">비밀번호 질문</label>
      <select id="question" name="question">{_question_options(v('question'))}</select>

      <label class="field" for="answer">질문의 답</label>
      <input type="text" id="answer" name="answer" placeholder="띄어쓰기 없이 적어 주세요" autocomplete="off">
      <p class="fhint"><strong>띄어쓰기 없이 붙여서</strong> 적어 주세요(대소문자는 가리지 않습니다).
      적어 둔 답은 <strong>다시 볼 수 없고</strong>, 「나의 정보 관리」에서 바꾸기만 됩니다.</p>

      <button class="primary" type="submit">가입하기</button>
    </form>"""
    foot = '<p class="foot">이미 계정이 있으세요? <a href="/login">로그인</a></p>'
    return _page("회원가입", "회원가입", "AI 뉴스 모니터링",
                 first_note + body, foot, _warn(error), wide=True)


# ── 3. 비밀번호 찾기 ─────────────────────────────────────────────────────

def _steps(active: int) -> str:
    names = ["1 본인 확인", "2 질문", "3 새 비밀번호"]
    parts = []
    for i, name in enumerate(names, start=1):
        parts.append(f'<span class="st{" on" if i == active else ""}">{name}</span>')
        if i < len(names):
            parts.append('<span class="ar">›</span>')
    return f'<div class="steps">{"".join(parts)}</div>'


def render_forgot_page(error: str = "", email: str = "") -> str:
    body = _steps(1) + f"""
    <form class="f" method="POST" action="/forgot">
      <label class="field" for="email">이메일</label>
      <input type="email" id="email" name="email" value="{html.escape(email)}"
             placeholder="name@korea.kr" autofocus>
      <label class="field" for="phone">핸드폰번호</label>
      <input type="tel" id="phone" name="phone" placeholder="010-1234-5678">
      <p class="fhint">문자는 보내지 않습니다. 가입할 때 적은 번호와 맞는지만 봅니다.</p>
      <button class="primary" type="submit">다음</button>
    </form>"""
    foot = '<p class="foot"><a href="/login">로그인 화면으로</a></p>'
    return _page("비밀번호 찾기", "비밀번호 찾기", "가입할 때 적은 정보로 확인합니다",
                 body, foot, _warn(error))


def render_forgot_answer_page(ticket: str, question: str, error: str = "") -> str:
    body = _steps(2) + f"""
    <form class="f" method="POST" action="/forgot-answer">
      <input type="hidden" name="ticket" value="{html.escape(ticket)}">
      <div class="qa-box">
        <p class="q">{html.escape(question)}</p>
        <input type="text" name="answer" placeholder="띄어쓰기 없이 적어 주세요" autocomplete="off" autofocus>
      </div>
      <p class="fhint">가입할 때 적은 그대로, 띄어쓰기 없이 적어 주세요. 대소문자는 가리지 않습니다.</p>
      <button class="primary" type="submit">다음</button>
    </form>"""
    foot = '<p class="foot"><a href="/login">로그인 화면으로</a></p>'
    return _page("비밀번호 찾기", "비밀번호 찾기", "가입할 때 고른 질문입니다",
                 body, foot, _warn(error))


def render_forgot_reset_page(ticket: str, email: str, error: str = "") -> str:
    body = _steps(3) + f"""
    <form class="f" method="POST" action="/forgot-reset">
      <input type="hidden" name="ticket" value="{html.escape(ticket)}">
      <label class="field" for="password">새 비밀번호</label>
      <input type="password" id="password" name="password" autocomplete="new-password" autofocus>
      <p class="fhint">{AUTH_MIN_PASSWORD_LEN}자 이상으로 적어 주세요.</p>
      <label class="field" for="password2">새 비밀번호 확인</label>
      <input type="password" id="password2" name="password2" autocomplete="new-password">
      <button class="primary" type="submit">비밀번호 바꾸기</button>
    </form>"""
    return _page("새 비밀번호", "새 비밀번호", email, body, "", _warn(error))


def render_forgot_done_page(email: str) -> str:
    notice = _ok("새 비밀번호로 로그인해 주세요. 다른 기기에서 로그인돼 있던 것은 모두 끊었습니다.")
    foot = '<p class="foot"><a href="/login">로그인 화면으로</a></p>'
    return _page("비밀번호를 바꿨어요", "비밀번호를 바꿨어요", email, "", foot, notice)


# ── 4. 나의 정보 관리 ────────────────────────────────────────────────────
# 설정 하위 화면이라 상단바는 `홈 │ ← 설정`, 폭은 다른 설정 화면과 같은 800px,
# **채운 파랑은 하단 저장 바의 「저장」 하나**다. 비밀번호 변경은 지금 비밀번호를
# 함께 확인하는 다른 동작이라 그 칸 안에서 테두리 버튼으로 끝낸다.
#
# 폼을 **빈 채로 먼저 선언하고** 칸마다 `form=`으로 가리킨다 — 두 동작(프로필 저장·
# 비밀번호 변경)이 화면에서 번갈아 서는데, 감싸면 폼이 겹쳐 둘 다 안 먹는다.
_ME_STYLE = """
  .container {{ width: 800px; max-width: 100%; margin: 24px auto 0;
    padding: 44px 16px 120px; box-sizing: border-box; }}
  .container h1 {{ font-size: var(--fs-xl); color: {header}; margin: 0 0 4px; }}
  .container .page-sub {{ font-size: var(--fs-md); color: {muted}; margin: 0 0 20px; }}

  .card {{ background: {card}; border: 1px solid {border}; border-radius: var(--r-lg);
    padding: 20px 22px; margin: 0 0 16px; }}
  .card h2 {{ font-size: var(--fs-lg); color: {header}; margin: 0 0 4px; }}
  .card .csub {{ font-size: var(--fs-sm); color: {muted}; margin: 0 0 16px; line-height: 1.55; }}
  .row2 {{ display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 0 18px; }}
  .card .acts {{ display: flex; justify-content: flex-end; margin-top: 18px; }}

  .save-bar {{ position: fixed; left: 0; right: 0; bottom: 0; z-index: 20; background: {card};
    border-top: 1px solid {border}; box-shadow: 0 -2px 8px rgba(0, 0, 0, 0.08); }}
  .save-bar-inner {{ width: 800px; max-width: 100%; margin: 0 auto; padding: 12px 16px;
    display: flex; align-items: center; gap: 12px; box-sizing: border-box; }}
  .save-bar .msg {{ font-size: var(--fs-sm); color: {muted}; flex: 1 1 auto; }}
  .save-bar button {{ background: {accent}; color: {on_fill}; border: none;
    border-radius: var(--r-md); height: var(--h-lg); padding: 0 22px;
    font-size: var(--fs-md); font-weight: 600; font-family: inherit; cursor: pointer; }}
  .save-bar button:hover {{ background: {header}; }}

  .bottom-links {{ font-size: var(--fs-sm); color: {muted}; }}
  .bottom-links button.linky {{ background: none; border: none; padding: 0;
    font: inherit; color: {accent}; cursor: pointer; text-decoration: none; }}
  .bottom-links button.linky:hover {{ text-decoration: underline; }}

  @media (max-width: 640px) {{ .row2 {{ grid-template-columns: minmax(0, 1fr); }} }}
"""

_ME_PAGE = (
    """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>나의 정보 관리</title>
<style>"""
    + _FORM_STYLE
    + _ME_STYLE
    + """{topnav_style}</style>
</head>
<body>
{topbar}
<form id="profile" method="POST" action="/me"></form>
<form id="pwform" method="POST" action="/me/password"></form>
<form id="outform" method="POST" action="/logout"></form>
<div class="container f">
  <h1>나의 정보 관리</h1>
  <p class="page-sub">로그인에 쓰는 정보와, 비밀번호를 잊었을 때 쓰는 정보를 관리합니다.</p>
  {notice_html}

  <div class="card">
    <h2>계정</h2>
    <p class="csub">이메일은 아이디라서 바꿀 수 없습니다.</p>
    <label class="field" for="email">이메일</label>
    <input type="email" id="email" value="{email}" readonly>
    <label class="field" for="display_name">이름</label>
    <input type="text" id="display_name" name="display_name" value="{display_name}" form="profile">
    <label class="field" for="department">부서</label>
    <input type="text" id="department" name="department" value="{department}"
           placeholder="디지털소통팀" form="profile">
    <p class="fhint">화면에 누구인지 보이려고 받습니다. 부서는 비워 둬도 됩니다.</p>
  </div>

  <div class="card">
    <h2>비밀번호 변경</h2>
    <p class="csub">바꾸면 다른 기기의 로그인은 모두 끊깁니다.</p>
    <label class="field" for="current">현재 비밀번호</label>
    <input type="password" id="current" name="current" autocomplete="current-password" form="pwform">
    <div class="row2">
      <div>
        <label class="field" for="np">새 비밀번호</label>
        <input type="password" id="np" name="password" autocomplete="new-password" form="pwform">
        <p class="fhint">{min_len}자 이상으로 적어 주세요.</p>
      </div>
      <div>
        <label class="field" for="np2">새 비밀번호 확인</label>
        <input type="password" id="np2" name="password2" autocomplete="new-password" form="pwform">
      </div>
    </div>
    <div class="acts"><button class="ghost" type="submit" form="pwform">비밀번호 변경</button></div>
  </div>

  <div class="card">
    <h2>비밀번호를 잊었을 때 쓰는 정보</h2>
    <p class="csub">이 둘이 맞으면 새 비밀번호를 정할 수 있습니다.
      문자(SMS)는 보내지 않고, 적어 둔 값과 맞는지만 봅니다.</p>
    <label class="field" for="phone">핸드폰번호</label>
    <input type="tel" id="phone" name="phone" value="{phone}" form="profile">
    <label class="field" for="question">비밀번호 질문</label>
    <select id="question" name="question" form="profile">{question_options}</select>
    <label class="field" for="answer">질문의 답 바꾸기</label>
    <input type="text" id="answer" name="answer" autocomplete="off" form="profile"
           placeholder="새 답을 적으면 바뀝니다 (비워 두면 그대로)">
    <p class="fhint">띄어쓰기 없이 붙여서 적어 주세요. 적어 둔 답은 다시 볼 수 없습니다 — 바꾸기만 됩니다.<br>
    <strong>질문을 바꿀 때는 새 답도 함께 적어 주세요</strong> — 옛 답은 새 질문과 짝이 맞지 않습니다.</p>
  </div>

  <div class="card">
    <h2>이 정보를 바꾸려면</h2>
    <p class="csub">자리를 비운 사이 누가 바꿔 두면 비밀번호 찾기가 그 사람 것이 됩니다.</p>
    <label class="field" for="confirm">지금 비밀번호</label>
    <input type="password" id="confirm" name="confirm" autocomplete="current-password" form="profile">
  </div>

  <p class="bottom-links">
    이 컴퓨터에서 <button class="linky" type="submit" form="outform">로그아웃</button>
  </p>
</div>
<div class="save-bar"><div class="save-bar-inner">
  <span class="msg">이름 · 부서 · 핸드폰번호 · 질문을 바꾸면 저장하세요.</span>
  <button type="submit" form="profile">저장</button>
</div></div>
</body>
</html>
"""
)


def render_my_info_page(user: dict, error: str = "", notice: str = "") -> str:
    notice_html = _ok(notice) if notice else _warn(error)
    phone = user.get("phone") or ""
    # 저장된 숫자만 다시 사람이 읽는 모양으로 — 대조는 언제나 숫자만 본다.
    if len(phone) == 11:
        phone = f"{phone[:3]}-{phone[3:7]}-{phone[7:]}"
    return _ME_PAGE.format(
        **_theme(),
        topnav_style=topnav_style(),
        topbar=plain_nav("설정", _SETTINGS_HOME),
        notice_html=notice_html,
        email=html.escape(user.get("email", "")),
        display_name=html.escape(user.get("display_name", "")),
        department=html.escape(user.get("department", "")),
        phone=html.escape(phone),
        question_options=_question_options(user.get("security_question", "")),
        min_len=AUTH_MIN_PASSWORD_LEN,
    )
