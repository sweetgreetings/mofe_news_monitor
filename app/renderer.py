# Design Ref: DESIGN.md §1 화면 구성 — 최신 회차를 밝은 카드형 정적 HTML 화면으로 렌더링
import html
import json
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from app.classifier import classify_articles
from app.config import (
    COLOR_ACCENT,
    COLOR_BG,
    COLOR_BORDER,
    COLOR_CARD,
    COLOR_ERROR,
    COLOR_HEADER,
    COLOR_HOVER,
    COLOR_TEXT,
    COLOR_TEXT_MUTED,
    CUTE_FONT_BASE64,
    CUTE_FONT_NAME,
    DEFAULT_ARTICLE_LINE_TEMPLATE,
    DEFAULT_HIGHLIGHT_KEYWORDS,
    DEFAULT_KEYWORDS,
    FONT_STACK,
    OUTPUT_HTML_PATH,
    SETTINGS_SERVER_HOST,
    SETTINGS_SERVER_PORT,
)
from app.atomic_write import atomic_write_text
from app.curation import display_group_name, filter_hidden, load_group_labels, load_group_overrides
from app.custom_groups import load_custom_groups
from app.highlight import highlight_keywords
from app.manual_articles import load_manual_articles
from app.naver_api import outlet_display_label
from app.settings import all_search_keywords, load_settings
from app.storage import is_today, load_latest_run
from app.summarizer import extract_keywords, summarize_groups

# [추가: 2026-07-26] "뉴스가 잠잠" 빈 상태 문구용 손글씨체 — app.config.CUTE_FONT_BASE64
# 참고(오프라인에서도 깨지지 않도록 base64로 파일에 직접 박아 넣은 서브셋 폰트).
_CUTE_FONT_FACE_CSS = (
    f"@font-face {{ font-family: '{CUTE_FONT_NAME}'; "
    f"src: url(data:font/woff2;base64,{CUTE_FONT_BASE64}) format('woff2'); font-display: swap; }}"
)

_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>언론 모니터링</title>
<style>
  {cute_font_face}
  body {{ margin: 0; background: {bg}; color: {text}; font-family: {font_stack}; }}
  .container {{
    max-width: 800px; margin: 24px auto; padding: 24px; background: {card};
    border: 1px solid {border}; border-radius: 8px;
  }}
  /* [수정: 2026-07-27] 상단 고정 바 — HOME + 다른 두 화면(초안/실시간)으로 바로 이동.
     🗑️(숨긴 기사 관리)는 하단 고정 바로 옮겼다 — 상단이 3개 내비게이션으로 꽉 차서
     자리를 따로 뺐다. 이 화면은 file://로 직접 열릴 수도 있어 절대경로(설정 서버
     주소)를 쓰는 링크가 섞여 있다 — home.html은 같은 폴더의 정적 파일이라 상대경로로도
     항상 동작한다. */
  .container {{ padding-top: 60px; padding-bottom: 56px; }}
  .topbar {{
    position: fixed; top: 0; left: 0; right: 0; z-index: 20;
    background: {card}; border-bottom: 1px solid {border}; box-shadow: 0 2px 8px rgba(0, 0, 0, 0.06);
  }}
  .topbar-inner {{
    max-width: 800px; margin: 0 auto; padding: 12px 24px;
    display: flex; justify-content: space-between; align-items: center;
  }}
  .topbar a {{ color: {accent}; text-decoration: none; font-size: 0.92rem; font-weight: 600; padding: 6px 10px; border-radius: 6px; }}
  .topbar a:hover {{ background: {hover}; }}
  .bottombar {{
    position: fixed; left: 0; right: 0; bottom: 0; z-index: 20;
    background: {card}; border-top: 1px solid {border}; box-shadow: 0 -2px 8px rgba(0, 0, 0, 0.08);
  }}
  .bottombar-inner {{
    max-width: 800px; margin: 0 auto; padding: 10px 24px;
    display: flex; justify-content: space-between; align-items: center; gap: 10px;
  }}
  .bottombar a {{ color: {accent}; text-decoration: none; font-size: 1.1rem; padding: 6px 10px; border-radius: 6px; }}
  .bottombar a:hover {{ background: {hover}; }}
  /* [추가: 2026-07-30] app.preview_renderer와 동일한 체크박스 일괄이동 바 —
     평소엔 숨어있다가 체크박스를 선택하면 왼쪽에 나타나고, 🗑️는 그대로 오른쪽에 남는다. */
  .bulk-move-bar {{ display: none; align-items: center; gap: 8px; flex-wrap: wrap; font-size: 0.85rem; }}
  .bulk-move-bar.is-active {{ display: flex; }}
  .bulk-move-bar .bulk-move-count {{ font-weight: 600; color: {header}; white-space: nowrap; }}
  #bulk-move-select {{
    border: 1px solid {accent}; border-radius: 4px; padding: 5px 8px; font-size: 0.85rem;
    color: {text}; background: {card};
  }}
  .bulk-move-bar button {{ padding: 5px 12px; font-size: 0.85rem; }}
  .bulk-move-bar .clear-btn {{ background: transparent; color: {muted}; }}
  .bulk-move-bar .clear-btn:hover {{ background: {hover}; }}
  .article-select {{ margin-top: 3px; flex-shrink: 0; }}
  .group-move-select {{
    flex-shrink: 0; width: 100px; border: 1px solid {border}; border-radius: 4px;
    padding: 2px 4px; font-size: 0.78rem; color: {muted}; background: {card};
  }}
  .subheading-empty {{ border: 1px dashed {border}; border-radius: 8px; padding: 14px 18px; }}
  .empty-group-hint {{ color: {muted}; font-size: 0.85rem; margin: 8px 0 0; }}
  header h1 {{ font-size: 1.4rem; margin-bottom: 12px; color: {header}; }}
  .actions {{ display: flex; gap: 8px; flex-wrap: wrap; }}
  /* [수정: 2026-08-03] button 태그는 브라우저 기본 스타일상 body의 font-family를 물려받지
     않아(a 태그와 달리) 지금까지 Arial 등 시스템 기본체로 렌더링되고 있었다 — font-family:
     inherit로 고쳤었는데도 "txt로 저장"(a 태그)만 여전히 다르게 보인다는 재지적으로,
     button은 appearance:auto(네이티브 OS 버튼 껍데기)가 남아있어 폰트를 맞춰도 렌더링이
     미묘하게 달라진다는 걸 추가로 발견 — appearance:none으로 네이티브 껍데기 자체를
     없애야 a 태그와 완전히 동일하게 그려진다. */
  button, a.btn {{
    background: {accent}; color: #ffffff; border: none; border-radius: 4px;
    padding: 6px 14px; font-size: 0.9rem; font-family: inherit; cursor: pointer; text-decoration: none;
    appearance: none; -webkit-appearance: none;
    display: inline-block; user-select: none; -webkit-user-select: none;
  }}
  button:hover, a.btn:hover {{ background: {header}; }}
  /* [수정: 2026-08-03] 화면 상단 액션 툴바(복사·txt로 저장·Telegram 전송·지난 기사)만
     "정직한 파란색" 원색 채움 대신 톤온톤 소프트 필로 — 디자인 시안 B안(사용자 선택).
     .actions button 쪽이 위 전역 button 규칙보다 더 구체적이라(class+element > element)
     이 툴바 안에서만 덮어쓰고, 🗑️/↑/↓ 등 다른 버튼은 그대로 원색 유지. */
  .actions button, .actions a.btn {{
    background: {hover}; color: {accent}; font-weight: 600; border-radius: 8px;
  }}
  .actions button:hover, .actions a.btn:hover {{ background: {tonal_hover}; }}
  /* [수정: 2026-08-03] "+ 새 소제목 만들기"만 나머지 툴바 버튼과 기능이 달라(내용을
     내보내는 게 아니라 새 그릇을 만드는 것) 시안 A(고스트 아웃라인)로 유일하게 다르게
     둔다 — .actions .create-group-btn이 위 .actions button보다 구체적이라(class 2개)
     덮어쓴다. */
  .actions .create-group-btn {{
    background: transparent; color: {accent}; border: 1px solid {ghost_border};
    border-radius: 6px; font-weight: 400;
  }}
  .actions .create-group-btn:hover {{ background: {hover}; border-color: {ghost_border_hover}; }}
  .subheading {{ margin-top: 28px; }}
  .subheading h2 {{ font-size: 1.1rem; color: {header}; border-bottom: 1px solid {border}; padding-bottom: 6px; }}
  .rename-btn {{
    background: transparent; border: none; color: {muted}; font-size: 0.9rem; cursor: pointer;
    vertical-align: middle; user-select: none; -webkit-user-select: none;
  }}
  .article {{ margin: 10px 0; line-height: 1.5; }}
  /* [수정: 2026-07-30] 🗑️를 제목 바로 옆에 붙인다 — space-between으로 두면 창이 넓을 때
     제목이 짧으면 버튼이 화면 오른쪽 끝까지 멀리 떨어져 보여서(제목-버튼 간 시각적 연결이
     끊김) 잘못된 행을 누를 위험이 있었다. flex-start로 바꿔 제목 길이와 무관하게 버튼이
     항상 제목 바로 뒤에 붙게 한다. */
  .article summary {{
    cursor: pointer; display: flex; align-items: baseline; justify-content: flex-start; gap: 8px;
  }}
  .article summary::marker {{ color: {muted}; }}
  .title-line {{ min-width: 0; overflow-wrap: anywhere; }}
  .article-summary {{ margin: 6px 0 4px 20px; color: {text}; font-size: 0.95rem; }}
  .article-footer {{ display: flex; align-items: center; gap: 8px; }}
  .article .url {{ flex: 1; color: {muted}; font-size: 0.9rem; word-break: break-all; text-decoration: underline; }}
  /* [수정: 2026-07-30] "이미 확인함" 표시를 브라우저에 영구 기억(localStorage)하는 대신,
     "지금 펼쳐서 보고 있는 기사"에만 실시간으로 적용한다 — 예전 방식은 클릭했던 기사가
     전부 보라색으로 남아 화면이 정신없어진다는 피드백에 따른 변경. details가 열려있는
     동안에만 :has()로 색을 입히므로(자바스크립트 불필요), 닫거나 다른 기사를 열면 이
     기사는 자동으로 원래 색으로 돌아간다. */
  .article:has(details[open]) .title-line, .article:has(details[open]) .url {{ color: {seen_purple}; }}
  .pub-time {{ flex-shrink: 0; color: {muted}; font-size: 0.8rem; white-space: nowrap; }}
  .move-btn, .hide-btn {{
    flex-shrink: 0; background: transparent; border: none; color: {muted}; font-size: 1rem;
    cursor: pointer; padding: 2px 6px; user-select: none; -webkit-user-select: none;
  }}
  .move-btn:disabled {{ color: {border}; cursor: not-allowed; }}
  .hide-btn:hover {{ color: {error}; }}
  .empty {{ text-align: center; margin-top: 80px; font-size: 1.3rem; color: {muted}; }}
  .cute-caption {{ font-family: '{cute_font_name}', sans-serif; font-size: 1.4rem; margin-top: 6px; }}
  .bottom {{ margin-top: 40px; border-top: 1px solid {border}; padding-top: 16px; }}
  .bottom h3 {{ font-size: 1rem; color: {header}; }}
  .manual-divider {{
    display: flex; align-items: center; gap: 10px; margin: 32px 0 4px;
    color: {muted}; font-size: 0.75rem;
  }}
  .manual-divider::before, .manual-divider::after {{ content: ""; flex: 1; border-top: 1px dashed {border}; }}
  .manual-zone {{ border: 1px dashed {border}; border-radius: 8px; padding: 14px 18px; margin-top: 10px; }}
  .manual-zone-head {{ display: flex; align-items: baseline; justify-content: space-between; gap: 10px; flex-wrap: wrap; }}
  .manual-zone-title {{ font-weight: 600; color: {text}; font-size: 0.95rem; }}
  .manual-zone-hint {{ font-size: 0.75rem; color: {muted}; }}
  .manual-zone .article:first-child {{ margin-top: 10px; }}
</style>
</head>
<body>
<div class="topbar"><div class="topbar-inner">
  <a href="home.html">홈</a>
  <a href="{preview_href}">📝 스크랩 초안</a>
  <a href="{live_href}">🔴 실시간 현황</a>
</div></div>
<div class="container">
  <header>
    <h1>언론 모니터링 {run_slot} 기준</h1>
    <div class="actions">
      <button onclick="copyPlainText()">복사</button>
      <a class="btn" href="data:text/plain;charset=utf-8,{export_href}" download="{export_filename}">txt로 저장</a>
      <button onclick="sendToTelegram(this)">📤 Telegram 전송</button>
      <a class="btn" href="history.html">지난 기사</a>
      <button class="create-group-btn" type="button" onclick="createCustomGroup()">+ 새 소제목 만들기</button>
    </div>
  </header>
  {body}
</div>
<div class="bottombar"><div class="bottombar-inner">
  <div class="bulk-move-bar" id="bulk-move-bar">
    <span class="bulk-move-count" id="bulk-move-count"></span>
    <select id="bulk-move-select"></select>
    <button type="button" onclick="bulkMoveSelected()">옮기기</button>
    <button class="clear-btn" type="button" onclick="clearSelection()">선택 해제</button>
  </div>
  <a href="{hidden_href}" title="숨긴 기사 관리">🗑️</a>
</div></div>
<script>
const PLAIN_TEXT = {plain_text_json};
function copyPlainText() {{
  navigator.clipboard.writeText(PLAIN_TEXT)
    .then(() => alert("클립보드에 복사했습니다."))
    .catch(() => alert("복사에 실패했습니다."));
}}
function sendToTelegram(btn) {{
  btn.disabled = true;
  fetch("http://{settings_host}:{settings_port}/telegram-send-scrap", {{
    method: "POST",
    headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
    body: new URLSearchParams({{text: PLAIN_TEXT}})
  }}).then(function(res) {{
    if (res.ok) {{ alert("텔레그램으로 보냈습니다."); }}
    else {{ alert("전송에 실패했습니다 — 설정 화면에서 텔레그램 연결 상태를 확인해주세요."); }}
  }}).catch(function() {{
    alert("전송에 실패했습니다 — 앱이 실행 중인지 확인해주세요.");
  }}).finally(function() {{
    btn.disabled = false;
  }});
}}
function hideArticle(btn) {{
  var url = btn.dataset.url;
  var article = btn.closest(".article");
  fetch("http://{settings_host}:{settings_port}/hide-article", {{
    method: "POST", keepalive: true,
    headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
    body: new URLSearchParams({{
      url: url, outlet: btn.dataset.outlet, title: btn.dataset.title, pubDate: btn.dataset.pubDate
    }})
  }}).then(function(res) {{
    if (res.ok) {{ article.remove(); }}
    else {{ alert("숨기기에 실패했습니다. 다시 시도해주세요."); }}
  }}).catch(function() {{
    alert("숨기기에 실패했습니다 — 앱이 실행 중인지 확인해주세요.");
  }});
}}
// [추가: 2026-07-30] 스크랩 초안에 먼저 만든 체크박스 다중 선택 + 하단 "장바구니" 바를
// 완성본에도 그대로 붙였다(app.preview_renderer와 동일 패턴, 이 화면 자체 컨벤션대로
// reload 방식 유지). 소제목 이름은 화면에 이미 그려진 ✏️ 버튼의 data-name/data-current를
// 읽어서 쓴다 — 이름표(rename)가 붙어 있어도 서버에는 항상 원본 이름으로 보내야 한다.
function getAllGroups() {{
  return Array.prototype.map.call(document.querySelectorAll(".subheading h2 .rename-btn[data-current]"), function(btn) {{
    return {{value: btn.dataset.name, label: btn.dataset.current}};
  }});
}}
function updateBulkMoveBar() {{
  var checked = document.querySelectorAll(".article-select:checked");
  var bar = document.getElementById("bulk-move-bar");
  if (checked.length === 0) {{ bar.classList.remove("is-active"); return; }}
  bar.classList.add("is-active");
  document.getElementById("bulk-move-count").textContent = "☑️ " + checked.length + "개 선택됨";
  var select = document.getElementById("bulk-move-select");
  var options = '<option value="" selected disabled>이동할 소제목</option>';
  getAllGroups().forEach(function(g) {{
    var opt = document.createElement("option");
    opt.value = g.value;
    opt.textContent = g.label;
    options += opt.outerHTML;
  }});
  select.innerHTML = options;
}}
function clearSelection() {{
  document.querySelectorAll(".article-select:checked").forEach(function(cb) {{ cb.checked = false; }});
  updateBulkMoveBar();
}}
function _postGroupMove(urls, target) {{
  var body = new URLSearchParams();
  urls.forEach(function(u) {{ body.append("urls", u); }});
  body.append("target", target);
  return fetch("http://{settings_host}:{settings_port}/bulk-move-article", {{
    method: "POST", keepalive: true,
    headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
    body: body
  }});
}}
function bulkMoveSelected() {{
  var urls = Array.prototype.map.call(document.querySelectorAll(".article-select:checked"), function(cb) {{ return cb.dataset.url; }});
  var target = document.getElementById("bulk-move-select").value;
  if (!urls.length || !target) {{ return; }}
  _postGroupMove(urls, target).then(function(res) {{
    if (res.ok) {{ location.reload(); }}
    else {{ alert("이동에 실패했습니다. 다시 시도해주세요."); }}
  }}).catch(function() {{
    alert("이동에 실패했습니다 — 앱이 실행 중인지 확인해주세요.");
  }});
}}
function moveArticleToGroup(select) {{
  var url = select.closest(".article").dataset.url;
  var target = select.value;
  if (!target) {{ return; }}
  _postGroupMove([url], target).then(function(res) {{
    if (res.ok) {{ location.reload(); }}
    else {{ alert("소제목 이동에 실패했습니다. 다시 시도해주세요."); }}
  }}).catch(function() {{
    alert("소제목 이동에 실패했습니다 — 앱이 실행 중인지 확인해주세요.");
  }});
}}
function createCustomGroup() {{
  var name = prompt("새 소제목 이름을 입력하세요");
  if (name === null) return;
  name = name.trim();
  if (!name) {{ alert("소제목 이름은 비워둘 수 없습니다."); return; }}
  fetch("http://{settings_host}:{settings_port}/add-custom-group", {{
    method: "POST", keepalive: true,
    headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
    body: new URLSearchParams({{name: name}})
  }}).then(function(res) {{
    if (res.ok) {{ location.reload(); }}
    else {{ alert("소제목 만들기에 실패했습니다. 다시 시도해주세요."); }}
  }}).catch(function() {{
    alert("소제목 만들기에 실패했습니다 — 앱이 실행 중인지 확인해주세요.");
  }});
}}
function removeCustomGroup(btn) {{
  var name = btn.dataset.name;
  if (!confirm('"' + name + '" 소제목을 삭제할까요?')) return;
  fetch("http://{settings_host}:{settings_port}/remove-custom-group", {{
    method: "POST", keepalive: true,
    headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
    body: new URLSearchParams({{name: name}})
  }}).then(function(res) {{
    if (res.ok) {{ location.reload(); }}
    else {{ alert("삭제에 실패했습니다. 다시 시도해주세요."); }}
  }}).catch(function() {{
    alert("삭제에 실패했습니다 — 앱이 실행 중인지 확인해주세요.");
  }});
}}
function moveArticle(btn) {{
  var url = btn.dataset.url;
  var direction = btn.dataset.direction;
  fetch("http://{settings_host}:{settings_port}/move-article", {{
    method: "POST", keepalive: true,
    headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
    body: new URLSearchParams({{url: url, direction: direction}})
  }}).then(function(res) {{
    if (res.ok) {{ location.reload(); }}
    else {{ alert("순서 변경에 실패했습니다. 다시 시도해주세요."); }}
  }}).catch(function() {{
    alert("순서 변경에 실패했습니다 — 앱이 실행 중인지 확인해주세요.");
  }});
}}
function promoteManualArticle(btn) {{
  var url = btn.dataset.url;
  fetch("http://{settings_host}:{settings_port}/promote-manual-article", {{
    method: "POST", keepalive: true,
    headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
    body: new URLSearchParams({{url: url}})
  }}).then(function(res) {{
    if (res.ok) {{ location.reload(); }}
    else {{ alert("정식 스크랩으로 승격하지 못했습니다. 다시 시도해주세요."); }}
  }}).catch(function() {{
    alert("정식 스크랩으로 승격하지 못했습니다 — 앱이 실행 중인지 확인해주세요.");
  }});
}}
function promoteManualArticleToDraft(btn) {{
  var url = btn.dataset.url;
  fetch("http://{settings_host}:{settings_port}/promote-manual-article-to-draft", {{
    method: "POST", keepalive: true,
    headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
    body: new URLSearchParams({{url: url}})
  }}).then(function(res) {{
    if (res.ok) {{ location.reload(); }}
    else {{ alert("다음 회차 초안에 포함하지 못했습니다. 다시 시도해주세요."); }}
  }}).catch(function() {{
    alert("다음 회차 초안에 포함하지 못했습니다 — 앱이 실행 중인지 확인해주세요.");
  }});
}}
function renameGroup(btn) {{
  var name = btn.dataset.name;
  var current = btn.dataset.current;
  var newLabel = prompt("소제목 이름을 입력하세요", current);
  if (newLabel === null) return;
  newLabel = newLabel.trim();
  if (!newLabel) {{ alert("소제목 이름은 비워둘 수 없습니다."); return; }}
  fetch("http://{settings_host}:{settings_port}/rename-group", {{
    method: "POST", keepalive: true,
    headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
    body: new URLSearchParams({{name: name, label: newLabel}})
  }}).then(function(res) {{
    if (res.ok) {{ location.reload(); }}
    else {{ alert("이름 변경에 실패했습니다. 다시 시도해주세요."); }}
  }}).catch(function() {{
    alert("이름 변경에 실패했습니다 — 앱이 실행 중인지 확인해주세요.");
  }});
}}
function hideGroup(btn) {{
  var section = btn.closest(".subheading");
  if (!confirm("이 소제목의 기사를 전부 숨길까요? (숨긴 기사 관리에서 하나씩 되돌릴 수 있어요)")) return;
  var urls = Array.prototype.map.call(section.querySelectorAll(".hide-btn[data-url]"), function (el) {{
    return el.dataset.url;
  }});
  Promise.all(urls.map(function (url) {{
    return fetch("http://{settings_host}:{settings_port}/hide-article", {{
      method: "POST", keepalive: true,
      headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
      body: new URLSearchParams({{url: url}})
    }});
  }})).then(function (responses) {{
    if (responses.every(function (res) {{ return res.ok; }})) {{ location.reload(); }}
    else {{ alert("일부 기사를 숨기지 못했습니다. 다시 시도해주세요."); }}
  }}).catch(function () {{
    alert("숨기기에 실패했습니다 — 앱이 실행 중인지 확인해주세요.");
  }});
}}
function renderRelativeTimes() {{
  var now = Date.now();
  document.querySelectorAll(".pub-time-relative[data-pub-date]").forEach(function (el) {{
    var pub = new Date(el.dataset.pubDate).getTime();
    if (isNaN(pub)) return;
    var minutes = Math.floor((now - pub) / 60000);
    var text;
    if (minutes < 1) text = "방금 전";
    else if (minutes < 60) text = minutes + "분 전";
    else if (minutes < 1440) text = Math.floor(minutes / 60) + "시간 전";
    else text = Math.floor(minutes / 1440) + "일 전";
    el.textContent = text;
  }});
}}
renderRelativeTimes();
</script>
</body>
</html>
"""


def apply_line_template(template: str, outlet: str, title: str) -> str:
    """{outlet}·{title} 자리표시자를 치환한다 (PRD.md 기능1 규칙 5).

    str.format이 아니라 단순 문자열 치환(.replace)을 쓴다 — outlet/title 값이나
    template 자체에 우연히 중괄호가 들어 있어도 format 필드로 잘못 해석되거나
    깨지지 않는다(DESIGN.md §3).
    """
    return template.replace("{outlet}", outlet).replace("{title}", title)


def format_slot_time_kr(hhmm: str) -> str:
    """"HH:MM"을 "9시"/"9시 30분"/"12시"처럼 자연스러운 한국어 시각 표현으로 바꾼다.

    [추가: 2026-07-29] 헤더 "언론 모니터링 [HH:MM] 기준"의 워딩 변경 요청 — 시는 앞자리
    0을 없애고(09 -> 9), 분이 0이면 "0분"을 아예 생략한다("09:00" -> "9시", "09:30" ->
    "9시 30분", "12:00" -> "12시"). 파일명(export_filename)처럼 정렬·고유성이 필요한
    곳에는 이 표현을 쓰지 않고 원본 "HH:MM"을 그대로 쓴다.
    """
    hour_str, _, minute_str = hhmm.partition(":")
    hour = int(hour_str)
    minute = int(minute_str) if minute_str else 0
    return f"{hour}시" if minute == 0 else f"{hour}시 {minute}분"


def _format_pub_time(article: dict) -> str:
    """게시 시각을 "HH:MM · N분 전" 형태로 화면에만 보여준다(순서 조정할 때 어느
    기사가 더 최근인지 참고용, 복사/내보내기 텍스트에는 안 들어간다 — _build_plain_text가
    이 값을 아예 안 쓰므로 자동으로 제외된다).

    [수정: 2026-07-27] "게시" 라벨은 없앴다 — 이 화면엔 다른 종류의 시각이 섞여 있지
    않아 라벨 없이도 무슨 시각인지 헷갈리지 않고, 기사마다 반복되니 자리만 차지한다는
    피드백(숨긴 기사 관리 화면에서 이미 같은 이유로 라벨 없이 시각만 보여주기로 한
    선례와도 일치한다).
    - "HH:MM"은 고정값이라 자동 새로고침으로 정적 파일을 다시 읽기만 해도 안 틀어진다.
    - "N분 전"은 페이지가 열릴 때마다(60초 자동 새로고침 포함) 브라우저에서 자바스크립트로
      다시 계산한다(renderRelativeTimes, live.html과 같은 방식) — 서버가 값을 굳혀두면
      시간이 지날수록 틀린 값이 되므로, data-pub-date만 실어두고 텍스트는 JS가 채운다.
    - pub_date가 없는 기사(이 필드 도입 전에 저장된 지난 회차 등)는 조용히 표시를 생략한다.
    """
    pub_date = article.get("pub_date")
    if not pub_date:
        return ""
    try:
        parsed = datetime.fromisoformat(pub_date)
    except ValueError:
        return ""
    escaped_pub_date = html.escape(pub_date)
    return (
        f'<span class="pub-time">{parsed.strftime("%H:%M")} · '
        f'<span class="pub-time-relative" data-pub-date="{escaped_pub_date}"></span></span>'
    )


def render_article(
    article: dict,
    highlight_words: list,
    line_template: str = DEFAULT_ARTICLE_LINE_TEMPLATE,
    move: Optional[dict] = None,
    move_handler: str = "moveArticle",
    up_handler: Optional[str] = None,
    up_title: str = "위로 이동",
    extra_buttons_html: str = "",
    checkbox: bool = False,
    group_select_html: str = "",
) -> str:
    """기사 한 건을 렌더링한다.

    제목 줄은 <details>/<summary>로 감싸 클릭하면 저장된 요약이 펼쳐지고(PRD 규칙13,
    자바스크립트 없이 HTML 기본 기능만 사용), URL은 새 탭으로 여는 링크다(PRD 규칙14).
    [수정: 2026-07-25] 제목·펼쳐지는 요약 둘 다에 형광펜 단어 하이라이트를 적용한다
    (PRD 규칙6) — 목록을 훑을 때 제목만 보고도 바로 눈에 띄는 게 낫다는 요청. 같은
    단어는 제목/요약 어디서든 항상 같은 색이다(app.highlight.highlight_keywords).
    highlight_words는 검색 키워드와 무관한, 설정 화면에서 별도로 지정하는 값이다.
    line_template은 첫 줄("ㅇ (언론사) 제목")의 형식을 정하며, 설정 화면에서 바꿀 수 있다(규칙5).

    move: {"disable_up": bool, "disable_down": bool} — 소제목 안에서 위/아래로 옮기는
    ↑/↓ 버튼을 보여준다(규칙21). None이면 버튼 자체를 안 보여준다 (지난 기사 화면·
    "직접 추가한 기사" 구획처럼 "그룹 내 순서"라는 개념이 없는 곳에서는 의미가 없다).
    move_handler/up_handler: ↑/↓ 버튼이 호출할 JS 함수 이름(up_handler를 생략하면
    move_handler와 같다).
    extra_buttons_html: [추가: 2026-07-28] move 버튼 뒤에 끼워 넣을 추가 버튼 HTML —
    "직접 추가한 기사" 구획(_render_manual_section)이 기사마다 "📝 초안에 포함"·
    "📗 완성본에 포함" 버튼을 붙이는 데 쓴다.

    checkbox: [추가: 2026-07-30] 기사 앞에 다중 선택용 체크박스를 보여준다 — 스크랩
    초안(preview.html)의 "여러 개 선택해서 한 번에 다른 소제목으로 옮기기" 하단 바가
    쓴다. 기본은 꺼짐(다른 화면엔 필요 없음).
    group_select_html: [추가: 2026-07-30] 🗑️ 앞에 끼워 넣을 "다른 소제목으로" 드롭다운
    HTML — 소제목 하나만 있거나 이동할 곳이 없으면 호출하는 쪽이 빈 문자열을 넘겨
    아예 안 보이게 한다.

    [수정: 2026-07-28] 🗑️ 숨기기 버튼을 하단 footer에서 제목 줄(summary) 옆으로
    옮겼다 — 요약을 펼치면 footer가 아래로 밀려나면서 숨기기 버튼이 제목에서 멀어져,
    기사가 많을 때 어떤 버튼이 어떤 기사 것인지 헷갈린다는 피드백에 따른 것. summary
    안에 버튼을 넣으면 클릭이 위(펼치기)로도 번질 수 있어 JS에서 stopPropagation으로
    막는다(hideArticle 자체는 그대로, 호출 전에 이벤트 전파만 끊는다).
    """
    outlet = html.escape(article["outlet"])
    # [추가: 2026-07-29] 화면 표시용 언론사 이름만 outlet_display_label을 거친다(매일경제만
    # 빨간 글자로, 매경이코노미와 안 구분되는 걸 화면에서 알려주는 표식) — data-outlet
    # 속성(위 outlet)은 원본 그대로 둬서 숨긴 기사 메타데이터 등이 canonical 값을 유지한다.
    # [수정: 2026-07-30] outlet_display_label이 이제 이미 이스케이프된 HTML 조각(색
    # span 포함)을 돌려주므로 여기서 다시 html.escape()하면 안 된다(<span> 태그가
    # 그대로 글자로 보이게 됨).
    outlet_display = outlet_display_label(article["outlet"])
    title = highlight_keywords(article["title"], highlight_words)
    title_attr = html.escape(article["title"])
    pub_date_attr = html.escape(article.get("pub_date") or "")
    url = html.escape(article["url"])
    summary_html = highlight_keywords(article.get("summary", ""), highlight_words)
    # 템플릿 자체도 이스케이프해, 사용자가 입력한 특수문자가 HTML로 해석되지 않게 한다.
    line_html = apply_line_template(html.escape(line_template), outlet_display, title)
    move_html = ""
    if move is not None:
        up_disabled = " disabled" if move.get("disable_up") else ""
        down_disabled = " disabled" if move.get("disable_down") else ""
        effective_up_handler = up_handler or move_handler
        # [수정: 2026-07-30] ↑/↓가 summary 안으로 옮겨오면서, 🗑️와 같은 이유로
        # stopPropagation이 필요해졌다 — 없으면 클릭이 위(펼치기 토글)로도 번진다.
        move_html = (
            f'<button class="move-btn" type="button" data-url="{url}" data-direction="up" '
            f'onclick="event.stopPropagation(); {effective_up_handler}(this);"'
            f'{up_disabled} title="{up_title}">↑</button>'
            f'<button class="move-btn" type="button" data-url="{url}" data-direction="down" '
            f'onclick="event.stopPropagation(); {move_handler}(this);"'
            f'{down_disabled} title="아래로 이동">↓</button>'
        )
    hide_btn_html = (
        f'<button class="hide-btn" type="button" data-url="{url}" data-outlet="{outlet}" '
        f'data-title="{title_attr}" data-pub-date="{pub_date_attr}" '
        f'onclick="event.stopPropagation(); hideArticle(this);" '
        'title="이 기사 숨기기 (되돌리기 가능)">🗑️</button>'
    )
    # [수정: 2026-07-30] 게시 시각과 🗑️/↑/↓를 전부 제목 줄(summary)로 옮겼다 — 예전엔
    # footer(URL 옆)에 있었는데, 요약을 펼치면 footer가 아래로 밀려나면서 "게시 시각이
    # URL 쪽으로 이동한 것처럼" 보여 헷갈린다는 피드백. 이제 제목만 보고 언제·뭘 할지
    # 바로 판단할 수 있다. 순서는 [🗑️][↑][↓] — "먼저 남길지 거를지 정하고(🗑️), 나중에
    # 순서를 정리한다(↑↓)"는 실제 작업 흐름 그대로다.
    # [추가: 2026-07-30] 체크박스는 summary 맨 앞에 둔다 — .article summary가 이미
    # flex 한 줄이라(app.preview_renderer 등 각 화면 CSS), 별도 레이아웃 손볼 것 없이
    # 그 줄 맨 앞자리에 자연스럽게 낀다. 클릭이 위(펼치기)로 안 번지게 stopPropagation.
    checkbox_html = (
        f'<input type="checkbox" class="article-select" data-url="{url}" '
        'onclick="event.stopPropagation();" onchange="updateBulkMoveBar();">'
        if checkbox
        else ""
    )
    return (
        f'<div class="article" data-url="{url}">'
        "<details>"
        f'<summary>{checkbox_html}<span class="title-line">{line_html}</span>{_format_pub_time(article)}'
        f"{group_select_html}{hide_btn_html}{move_html}</summary>"
        f'<p class="article-summary">{summary_html}</p>'
        "</details>"
        '<div class="article-footer">'
        f'<a class="url" href="{url}" target="_blank" rel="noopener noreferrer">{url}</a>'
        f"{extra_buttons_html}"
        "</div>"
        "</div>"
    )


def _group_select_html(current_name: str, all_names: list, labels: dict) -> str:
    """"다른 소제목으로" 드롭다운 — app.preview_renderer._group_select_html과 동일한
    이유·동작(스크랩 초안과 완성본 양쪽 다 자동분류가 완벽하지 않아 여러 개를 바로
    잡아야 하는 경우가 있어 붙였다). 지금 속한 소제목은 옵션에서 빼고, 옮길 곳이
    아예 없으면(소제목이 이거 하나뿐) 빈 문자열을 돌려줘 드롭다운을 숨긴다.
    """
    others = [n for n in all_names if n != current_name]
    if not others:
        return ""
    options = "".join(
        f'<option value="{html.escape(n)}">{html.escape(display_group_name(n, labels))}</option>'
        for n in others
    )
    return (
        '<select class="group-move-select" onclick="event.stopPropagation();" '
        'onchange="event.stopPropagation(); moveArticleToGroup(this);">'
        '<option value="" selected disabled>다른 소제목</option>'
        f"{options}"
        "</select>"
    )


def _render_groups(groups: list, highlight_words: list, line_template: str, labels: dict) -> str:
    """소제목별 화면을 렌더링한다.

    ↑/↓ 버튼은 각 소제목 안에서만이 아니라 소제목 경계도 넘나들 수 있어(규칙21),
    맨 처음 소제목의 첫 기사(↑)·맨 마지막 소제목의 마지막 기사(↓)일 때만 버튼을
    비활성화한다. 그 외 소제목 경계에서는 버튼이 계속 활성 상태이고, 클릭하면
    서버(move_article)가 "같은 소제목 내 순서 변경"과 "옆 소제목으로 이동"을 알아서
    구분해 처리한다 — 화면(버튼) 쪽은 이 둘을 구분할 필요가 없다.

    소제목 옆 ✏️ 버튼은 표시 이름만 바꾼다(기능2 규칙 8) — 분류 자체는 항상 원래
    소제목 단어(group["name"])를 기준으로 하므로, 버튼의 data-name에는 원래 단어를
    그대로 담아 서버가 어떤 단어의 이름표를 바꿀지 정확히 알 수 있게 한다.

    [수정: 2026-07-30] 스크랩 초안에 먼저 만든 체크박스 다중 선택 + "다른 소제목으로"
    드롭다운을 완성본에도 그대로 붙였다(app.preview_renderer._render_preview_groups와
    동일 패턴) — 자동분류가 완벽하지 않아 여러 개를 한꺼번에 바로잡아야 하는 경우가
    초안뿐 아니라 완성본에서도 필요해서다. 기사가 0개인 소제목(사용자가 미리 만든
    빈 소제목)은 안내 문구를 보여주고, 헤더의 🗑️도 "삭제"로 동작한다.
    """
    all_names = [g["name"] for g in groups]
    sections = []
    last_group_index = len(groups) - 1
    for group_index, group in enumerate(groups):
        original_name = html.escape(group["name"])
        display_name = html.escape(display_group_name(group["name"], labels))
        if not group["articles"]:
            articles_html = (
                '<p class="empty-group-hint">아직 기사가 없어요 — 체크박스로 선택하거나 '
                "드롭다운으로 기사를 여기로 옮겨보세요.</p>"
            )
            delete_btn = (
                f'<button class="rename-btn" type="button" data-name="{original_name}" '
                'onclick="removeCustomGroup(this)" title="이 빈 소제목 삭제">🗑️</button>'
            )
        else:
            last_index = len(group["articles"]) - 1
            group_select = _group_select_html(group["name"], all_names, labels)
            articles_html = "\n".join(
                render_article(
                    a,
                    highlight_words,
                    line_template,
                    move={
                        "disable_up": i == 0 and group_index == 0,
                        "disable_down": i == last_index and group_index == last_group_index,
                    },
                    checkbox=True,
                    group_select_html=group_select,
                )
                for i, a in enumerate(group["articles"])
            )
            delete_btn = (
                '<button class="rename-btn" type="button" onclick="hideGroup(this)" '
                'title="이 소제목 통째로 숨기기(기사 각각 숨긴 것과 동일 — 되돌리기 가능)">🗑️</button>'
            )
        heading = (
            f'<h2>&lt;{display_name}&gt; '
            f'<button class="rename-btn" type="button" data-name="{original_name}" '
            f'data-current="{display_name}" onclick="renameGroup(this)" title="소제목 이름 바꾸기">✏️</button> '
            f"{delete_btn}</h2>"
        )
        section_class = "subheading" if group["articles"] else "subheading subheading-empty"
        sections.append(f'<section class="{section_class}">{heading}{articles_html}</section>')
    return "\n".join(sections)


def _render_bottom(articles: list, groups: list, keywords: list, highlight_words: list, labels: dict) -> str:
    # keywords(검색어)는 소제목/키워드 추출에서 "뻔한 단어"를 걸러내는 용도로만 쓰고,
    # 실제로 형광펜을 칠하는 기준은 highlight_words(형광펜 단어, 별도 설정)다.
    keyword_tags = ", ".join(html.escape(k) for k in extract_keywords(articles, keywords))
    summary_lines = "\n".join(
        f'<p><strong>&lt;{html.escape(display_group_name(s["name"], labels))}&gt;</strong> '
        f'{highlight_keywords(s["summary"], highlight_words)}</p>'
        for s in summarize_groups(groups)
    )
    return (
        '<div class="bottom">'
        "<h3>🤖 AI가 추출한 주요 키워드</h3>"
        f"<p>{keyword_tags}</p>"
        "<h3>💬 AI가 읽은 소제목별 주요 요약</h3>"
        f"{summary_lines}"
        "</div>"
    )


def _render_manual_section(manual_articles: list, highlight_words: list, line_template: str) -> str:
    """"직접 추가한 기사" 구획을 렌더링한다 (실시간 기사 현황의 "📌" 결과물).

    자동 소제목 그룹과 분리된 별도 목록이다 — <소제목N> 라벨이 없고, 최대 5개 제한과도
    무관하며, 복사/내보내기 텍스트(_build_plain_text)에도 포함하지 않는다(실시간 현황에서
    참고용으로 옮겨둔 것일 뿐 "공식 스크랩 결과"가 아니라는 성격 때문 — 당일 자정이 지나면
    app.manual_articles가 자동으로 비운다).

    [수정: 2026-07-28] 기사마다 "📝 초안에 포함"·"📗 완성본에 포함" 버튼 두 개를 항상
    보여준다 — 예전엔 맨 위 기사의 ↑ 버튼만 승격으로 동작해서, 원하는 기사를 승격하려면
    먼저 순서를 맨 위까지 옮겨야 하는 번거로움이 있었다(피드백: "너무 빡세다"). 순서와
    무관하게 아무 기사나 바로 승격할 수 있게 되면서, 목록 안 순서바꾸기(↑/↓,
    moveManualArticle) 자체가 더 이상 필요 없어져 같이 뺐다. 두 버튼 다 어느 화면
    (완성본·초안)에서 눌러도 동일하게 동작한다(app.settings_server의
    /promote-manual-article, /promote-manual-article-to-draft). 비어 있으면 아예
    렌더링하지 않는다.
    """
    if not manual_articles:
        return ""
    promote_buttons_template = (
        '<button class="move-btn" type="button" data-url="{url}" '
        'onclick="promoteManualArticleToDraft(this)" title="다음 회차 초안에 포함">📝</button>'
        '<button class="move-btn" type="button" data-url="{url}" '
        'onclick="promoteManualArticle(this)" title="정식 스크랩으로 승격">📗</button>'
    )
    articles_html = "\n".join(
        render_article(
            a,
            highlight_words,
            line_template,
            extra_buttons_html=promote_buttons_template.format(url=html.escape(a["url"])),
        )
        for a in manual_articles
    )
    return (
        '<div class="manual-divider">이 아래는 복사·내보내기에 포함되지 않음</div>'
        '<div class="manual-zone">'
        '<div class="manual-zone-head">'
        '<span class="manual-zone-title">📌 직접 추가한 기사</span>'
        '<span class="manual-zone-hint">📝 다음 회차 초안에 포함 · 📗 정식 스크랩으로 승격 · 자정에 자동으로 비워져요</span>'
        "</div>"
        f"{articles_html}"
        "</div>"
    )


def _build_plain_text(run_slot: str, groups: list, line_template: str, labels: dict) -> str:
    """복사/내보내기용 메모장 형식 텍스트를 만든다 (PRD.md 기능1 규칙 5·10·11).

    화면에 표시된 언론사·기사제목·URL 목록만 담는다 — 하단의 🤖 키워드·💬 요약
    블록은 규칙10이 "소제목별 스크랩 목록(언론사·기사제목·URL)"만 명시하므로 제외한다.
    첫 줄 형식은 화면과 동일하게 line_template을 따른다. 소제목 이름은 사용자가 붙인
    이름표(labels)가 있으면 그걸로 표시한다 — 화면과 항상 같은 이름을 보여줘야 한다.
    """
    lines = [f"언론 모니터링 {format_slot_time_kr(run_slot)} 기준", ""]
    if not groups:
        lines.append("💤")
    else:
        for group in groups:
            lines.append(f"<{display_group_name(group['name'], labels)}>")
            for article in group["articles"]:
                lines.append(apply_line_template(line_template, article["outlet"], article["title"]))
                lines.append(article["url"])
                # [추가: 2026-07-27] 기사마다 빈 줄을 넣어 URL과 다음 기사 제목이 붙어
                # 보이지 않게 한다 — 보고용으로 복사해 쓸 때 훑어보기 쉽다는 요청.
                lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def build_latest_plain_text(
    keywords: Optional[list] = None,
    highlight_words: Optional[list] = None,
    line_template: Optional[str] = None,
) -> Optional[str]:
    """텔레그램 자동 전송처럼 화면(HTML) 없이도 최신 회차의 복사/내보내기 텍스트가 필요할
    때 쓴다. generate_screen과 같은 방식으로 최신 회차를 찾되, 오늘 회차가 아직 없으면
    (아직 스크랩 전이거나 자정이 지나 어제 회차만 남음) 예외 대신 None을 반환한다 —
    호출하는 쪽(app.scheduler)이 "보낼 게 없다"로 조용히 넘어갈 수 있어야 하기 때문이다.
    """
    run = load_latest_run()
    if run is None or not is_today(run):
        return None
    if keywords is None or highlight_words is None or line_template is None:
        settings = load_settings()
        keywords = keywords if keywords is not None else all_search_keywords(settings)
        highlight_words = highlight_words if highlight_words is not None else settings["highlight_keywords"]
        line_template = line_template if line_template is not None else settings["article_line_template"]
    articles = filter_hidden(run["articles"])
    labels = load_group_labels()
    groups = (
        classify_articles(
            articles, keywords, forced_groups=load_group_overrides(), custom_group_names=load_custom_groups()
        )
        if articles
        else []
    )
    return _build_plain_text(run["run_slot"], groups, line_template, labels)


def render_page(
    run_slot: str,
    articles: list,
    keywords: Optional[list] = None,
    highlight_words: Optional[list] = None,
    line_template: Optional[str] = None,
) -> str:
    """한 회차 데이터를 완성된 HTML 문서 문자열로 렌더링한다.

    수집된 기사가 없으면 (PRD.md 기능1 규칙 7) 본문을 "💤"로 대체한다.
    헤더의 회차 시각(run_slot)은 이 경우에도 그대로 표시한다.

    keywords: 검색 키워드 — 소제목 분류·주요 키워드 추출에서 "뻔한 단어"를 제외하는 데 쓴다.
    highlight_words: 형광펜 단어 — 요약에 하이라이트를 칠하는 기준이며 keywords와 무관하다(규칙6).
    line_template: 기사 첫 줄("ㅇ (언론사) 제목") 형식. keywords/highlight_words와도 무관하다(규칙5).
    """
    keywords = keywords if keywords is not None else DEFAULT_KEYWORDS
    highlight_words = highlight_words if highlight_words is not None else DEFAULT_HIGHLIGHT_KEYWORDS
    line_template = line_template if line_template is not None else DEFAULT_ARTICLE_LINE_TEMPLATE
    labels = load_group_labels()
    custom_names = load_custom_groups()
    groups = (
        classify_articles(articles, keywords, forced_groups=load_group_overrides(), custom_group_names=custom_names)
        if articles
        else []
    )
    # [추가: 2026-07-30] 스크랩 초안과 마찬가지로, "+ 새 소제목 만들기"로 미리 만들어둔
    # 빈 소제목도 완성본에 같이 보여준다(app.preview_renderer._compute_preview_content와
    # 같은 이유·같은 방식) — 요약/키워드 블록(_render_bottom)·복사 텍스트에는 원래의
    # groups만 넘겨 빈 placeholder가 섞이지 않게 한다.
    existing_names = {g["name"] for g in groups}
    empty_custom_groups = [{"name": n, "articles": []} for n in custom_names if n not in existing_names]
    render_groups = groups + empty_custom_groups
    if not render_groups:
        body = '<div class="empty">💤<div class="cute-caption">뉴스가 잠잠</div></div>'
    else:
        body = _render_groups(render_groups, highlight_words, line_template, labels)
        if groups:
            body += _render_bottom(articles, groups, keywords, highlight_words, labels)
    # "직접 추가한 기사"(실시간 현황 → 📌)는 이 회차에 실제로 수집된 기사가 하나도
    # 없어도(💤) 독립적으로 존재할 수 있으므로, 위 분기와 무관하게 항상 이어붙인다.
    # [수정: 2026-07-27] 전체를 자동으로 정식 결과에 합치는 대신, 맨 위 기사를 ↑로
    # "승격"시켜야만 실제 소제목(groups)에 들어간다(_handle_promote_manual_article,
    # app/settings_server.py) — 그전까지는 예전처럼 복사/내보내기에서 계속 제외된다.
    manual_articles = filter_hidden(load_manual_articles())
    body += _render_manual_section(manual_articles, highlight_words, line_template)

    plain_text = _build_plain_text(run_slot, groups, line_template, labels)
    export_filename = f"언론모니터링_{run_slot.replace(':', '-')}.txt"

    return _PAGE_TEMPLATE.format(
        run_slot=html.escape(format_slot_time_kr(run_slot)),
        body=body,
        # json.dumps로 JS 문자열 리터럴로 안전하게 이스케이프하고, "</script"가 섞여
        # 있어도 스크립트 태그가 조기 종료되지 않도록 "</"를 "<\/"로 한 번 더 바꾼다.
        plain_text_json=json.dumps(plain_text, ensure_ascii=False).replace("</", "<\\/"),
        export_href=quote(plain_text),
        export_filename=html.escape(export_filename),
        settings_host=SETTINGS_SERVER_HOST,
        settings_port=SETTINGS_SERVER_PORT,
        hidden_href=f"http://{SETTINGS_SERVER_HOST}:{SETTINGS_SERVER_PORT}/hidden",
        live_href=f"http://{SETTINGS_SERVER_HOST}:{SETTINGS_SERVER_PORT}/live.html",
        preview_href=f"http://{SETTINGS_SERVER_HOST}:{SETTINGS_SERVER_PORT}/preview.html",
        font_stack=FONT_STACK,
        cute_font_face=_CUTE_FONT_FACE_CSS,
        cute_font_name=CUTE_FONT_NAME,
        bg=COLOR_BG,
        card=COLOR_CARD,
        header=COLOR_HEADER,
        accent=COLOR_ACCENT,
        text=COLOR_TEXT,
        muted=COLOR_TEXT_MUTED,
        border=COLOR_BORDER,
        hover=COLOR_HOVER,
        error=COLOR_ERROR,
        # [추가: 2026-07-28] "이미 확인한 기사"(제목 펼침/링크 클릭) 표시 전용 색 —
        # 사용자가 준 스크린샷의 보라색을 참고했다. 이 화면에서만 쓰는 상태 표시라
        # 공용 COLOR_* 팔레트에는 넣지 않고 여기서 상수로 둔다.
        seen_purple="#A855F7",
        # [추가: 2026-08-03] 액션 툴바 버튼 리디자인(시안 B/A, 사용자 선택) 전용 색 —
        # accent(#2563EB)를 옅게 탄 톤온톤 hover와, 고스트 아웃라인용 연한 테두리.
        # 둘 다 이 툴바에서만 쓰는 값이라 공용 COLOR_* 팔레트에는 넣지 않는다.
        tonal_hover="#DCEAFE",
        ghost_border="#C7D9F7",
        ghost_border_hover="#A9C6F5",
    )


# [추가: 2026-07-26] 오늘 아직 완료된 회차가 하나도 없을 때(앱을 막 켰거나, 자정이 지나
# 어제 회차만 남아 있을 때) index.html 자리에 대신 보여줄 안내 화면. main.py(최초 실행 시)와
# app.scheduler.run_scheduler(매 tick, 자정 이후 오늘 첫 회차 전까지) 둘 다에서 쓴다 —
# 자정을 넘겨도 어제 스크랩 화면이 계속 떠 있는 걸 막기 위해서다.
_WAITING_PAGE = f"""<!DOCTYPE html>
<html lang="ko">
<head><meta charset="UTF-8" /><title>언론 모니터링</title>
<style>body{{margin:0;background:{COLOR_BG};color:{COLOR_TEXT};font-family:{FONT_STACK};
display:flex;flex-direction:column;align-items:center;justify-content:center;min-height:100vh;
font-size:1.2rem;text-align:center;padding:60px 24px 0;line-height:1.8;box-sizing:border-box;}}
.icon{{font-size:2rem;}}
.topbar{{position:fixed;top:0;left:0;right:0;z-index:20;background:{COLOR_CARD};
border-bottom:1px solid {COLOR_BORDER};box-shadow:0 2px 8px rgba(0,0,0,0.06);}}
.topbar-inner{{max-width:800px;margin:0 auto;padding:12px 24px;display:flex;
justify-content:space-between;align-items:center;}}
.topbar a{{color:{COLOR_ACCENT};text-decoration:none;font-size:0.92rem;font-weight:600;
padding:6px 10px;border-radius:6px;}}
.topbar a:hover{{background:{COLOR_HOVER};}}</style>
</head>
<body>
<div class="topbar"><div class="topbar-inner">
<a href="home.html">홈</a>
<a href="history.html">지난 기사</a>
<a href="index.html">새로고침</a>
</div></div>
<div><div class="icon">🐰⏱️</div>아직 스크랩 시간 전입니다.<br>예정 시각이 되면 자동으로 시작됩니다.</div>
</body>
</html>
"""


def generate_waiting_page() -> Path:
    """오늘 첫 회차가 아직 끝나지 않았을 때 index.html 대신 보여줄 안내 화면을 만든다."""
    atomic_write_text(OUTPUT_HTML_PATH, _WAITING_PAGE)
    return OUTPUT_HTML_PATH


def generate_screen(
    keywords: Optional[list] = None,
    highlight_words: Optional[list] = None,
    line_template: Optional[str] = None,
) -> Path:
    """저장된 최신 회차를 읽어 index.html로 렌더링한다 (PRD.md 기능2 규칙 6: 최신 회차만 표시).

    keywords/highlight_words/line_template을 생략하면 설정 화면에 저장된 값을 각각 쓴다
    (규칙2 검색 키워드, 규칙6 형광펜 단어, 규칙5 출력 줄 형식 — 서로 별개 설정).
    """
    run = load_latest_run()
    if run is None or not is_today(run):
        # [수정: 2026-07-26] 회차가 아예 없을 때뿐 아니라, 자정이 지나 어제 회차만
        # 남아있을 때도 같은 예외로 취급한다 — 호출하는 쪽(main.py, app.scheduler)이
        # 어제 화면 대신 안내 화면(generate_waiting_page)을 보여주게 하기 위해서다.
        raise RuntimeError("오늘 완료된 회차가 없습니다 — 아직 스크랩 전이거나 자정이 지나 어제 회차만 남아있습니다.")
    if keywords is None or highlight_words is None or line_template is None:
        settings = load_settings()
        keywords = keywords if keywords is not None else all_search_keywords(settings)
        highlight_words = highlight_words if highlight_words is not None else settings["highlight_keywords"]
        line_template = line_template if line_template is not None else settings["article_line_template"]
    articles = filter_hidden(run["articles"])
    html_text = render_page(run["run_slot"], articles, keywords, highlight_words, line_template)
    atomic_write_text(OUTPUT_HTML_PATH, html_text)
    return OUTPUT_HTML_PATH
