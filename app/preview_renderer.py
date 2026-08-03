# Design Ref: 다음 회차 미리보기 — 정식 회차(app.scraper.collect_run)가 끝나기 전에,
# 지금까지의 시간창(회차 시작~지금)으로 미리 검색·분류해 보여준다. live.html(당일 0시~
# 현재, 전체 언론사, 소제목 분류 없음)과 달리 정식 회차와 똑같은 파이프라인(아웃렛
# 화이트리스트·포토/인사 제외·중복 제거·소제목 분류)을 그대로 따른다는 점이 다르다 —
# "지금 정식 회차가 끝난다면 어떤 모습일지"의 미리보기이기 때문이다.
#
# 저장 버튼이 없다: 여기서 숨기기(🗑️)·소제목 이동(↑/↓)·이름 바꾸기(✏️)를 하면
# hidden_articles.json/group_overrides.json/group_labels.json에 URL·단어 기준으로
# 바로 기록되고(app.curation), 이 파일들은 정식 회차와 공유되는 전역 저장소라 정식
# 회차가 자동으로 돌 때도 그 큐레이션이 그대로 반영된다. 별도의 "저장" 개념이 필요 없다.
import html
import json
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import requests

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
    DEFAULT_ARTICLE_LINE_TEMPLATE,
    FONT_STACK,
    PREVIEW_HTML_PATH,
    SETTINGS_SERVER_HOST,
    SETTINGS_SERVER_PORT,
)
from app.atomic_write import atomic_write_text
from app.curation import (
    bulk_reassign_group,
    display_group_name,
    filter_hidden,
    load_group_labels,
    load_group_overrides,
    move_article,
    set_group_override,
)
from app.custom_groups import add_custom_group, load_custom_groups, remove_custom_group
from app.draft_articles import add_draft_pending_article, load_draft_pending_articles
from app.preview_cache import load_preview_cache, save_preview_cache
from app.preview_order import apply_preview_order, load_preview_order, save_preview_order
from app.filters import (
    deduplicate_by_title,
    exclude_personnel_articles,
    exclude_photo_articles,
    filter_by_outlet_whitelist,
)
from app.highlight import highlight_keywords
from app.manual_articles import load_manual_articles
from app.naver_api import kst_today_at, search_articles_by_groups
from app.renderer import _render_manual_section, apply_line_template, format_slot_time_kr, render_article
from app.scheduler import next_pending_slot
from app.settings import active_schedule_times, active_search_groups, all_search_keywords, load_settings
from app.sorter import sort_by_outlet_priority
from app.summarizer import extract_keywords, summarize_groups

_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>스크랩 초안</title>
<style>
  body {{ margin: 0; background: {bg}; color: {text}; font-family: {font_stack}; }}
  .container {{
    max-width: 800px; margin: 24px auto; padding: 24px; background: {card};
    border: 1px solid {border}; border-radius: 8px; padding-top: 60px; padding-bottom: 56px;
  }}
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
  /* [추가: 2026-07-30] 체크박스로 기사를 선택했을 때만 나타나는 "일괄 이동" 바 —
     장바구니처럼 화면 하단에 붙어있다가, 선택이 하나도 없으면 숨어서 원래 있던
     🗑️(숨긴 기사 관리)만 오른쪽에 그대로 남는다. */
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
  .actions .create-group-btn {{
    background: transparent; color: {accent}; border: 1px solid {ghost_border};
    border-radius: 6px; font-weight: 400; margin-left: auto;
  }}
  .actions .create-group-btn:hover {{ background: {hover}; border-color: {ghost_border_hover}; }}
  .article-select {{ margin-top: 3px; flex-shrink: 0; }}
  .group-move-select {{
    flex-shrink: 0; width: 100px; border: 1px solid {border}; border-radius: 4px;
    padding: 2px 4px; font-size: 0.78rem; color: {muted}; background: {card};
  }}
  .subheading-empty {{ border: 1px dashed {border}; border-radius: 8px; padding: 14px 18px; }}
  .empty-group-hint {{ color: {muted}; font-size: 0.85rem; margin: 8px 0 0; }}
  header h1 {{ font-size: 1.3rem; margin-bottom: 6px; color: {header}; }}
  .preview-head {{ display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }}
  .preview-hint {{
    font-size: 0.85rem; color: {muted}; background: {hover}; border-radius: 8px;
    padding: 10px 14px; margin-bottom: 20px; line-height: 1.6;
  }}
  .refresh-btn {{
    background: {card}; color: {accent}; border: 1px solid {accent}; border-radius: 6px;
    padding: 6px 12px; font-size: 0.85rem; font-weight: 500; text-decoration: none;
  }}
  .refresh-btn:hover {{ background: {hover}; }}
  .actions {{ display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 4px; }}
  /* [수정: 2026-08-03] app.renderer와 동일한 이유 — button은 a와 달리 font-family를
     상속받지 않고, appearance:auto(네이티브 OS 버튼 껍데기)까지 남아있어 폰트만 맞춰선
     완전히 똑같이 안 보인다. */
  button, a.btn {{
    background: {accent}; color: #ffffff; border: none; border-radius: 4px;
    padding: 6px 14px; font-size: 0.9rem; font-family: inherit; cursor: pointer; text-decoration: none;
    appearance: none; -webkit-appearance: none;
    display: inline-block; user-select: none; -webkit-user-select: none;
  }}
  button:hover, a.btn:hover {{ background: {header}; }}
  /* [수정: 2026-08-03] app.renderer와 동일한 이유로 액션 툴바만 소프트 필로(시안 B) */
  .actions button, .actions a.btn {{
    background: {hover}; color: {accent}; font-weight: 600; border-radius: 8px;
  }}
  .actions button:hover, .actions a.btn:hover {{ background: {tonal_hover}; }}
  .subheading {{ margin-top: 28px; }}
  .subheading h2 {{ font-size: 1.1rem; color: {header}; border-bottom: 1px solid {border}; padding-bottom: 6px; }}
  .rename-btn {{
    background: transparent; border: none; color: {muted}; font-size: 0.9rem; cursor: pointer;
    vertical-align: middle; user-select: none; -webkit-user-select: none;
  }}
  .article {{ margin: 10px 0; line-height: 1.5; }}
  /* [수정: 2026-07-30] 🗑️를 제목 바로 옆에 붙인다 — space-between이면 창이 넓을 때
     제목이 짧을수록 버튼이 화면 오른쪽 끝까지 멀어져 잘못 누를 위험이 있었다. */
  .article summary {{
    cursor: pointer; display: flex; align-items: baseline; justify-content: flex-start; gap: 8px;
  }}
  .article summary::marker {{ color: {muted}; }}
  .title-line {{ min-width: 0; overflow-wrap: anywhere; }}
  .article-summary {{ margin: 6px 0 4px 20px; color: {text}; font-size: 0.95rem; }}
  .article-footer {{ display: flex; align-items: center; gap: 8px; }}
  .article .url {{ flex: 1; color: {muted}; font-size: 0.9rem; word-break: break-all; text-decoration: underline; }}
  /* [수정: 2026-07-30] "이미 확인함"을 영구 기억(localStorage)하지 않고 "지금 펼쳐서
     보고 있는 기사"에만 실시간 적용 — app.renderer와 같은 이유·같은 방식(:has()). */
  .article:has(details[open]) .title-line, .article:has(details[open]) .url {{ color: {seen_purple}; }}
  /* [추가: 2026-07-29] 지난번 이 화면을 봤을 때는 없다가 이번에 새로 들어온 기사 —
     초안은 새로고침할 때마다 다시 검색하므로 뭐가 새로 섞였는지 표시해준다
     (app.preview_renderer._compute_preview_articles가 매번 새로 검색·병합한 결과). */
  .article.is-new-arrival {{ background: {new_arrival_bg}; border-radius: 6px; padding: 8px 10px; }}
  .pub-time {{ flex-shrink: 0; color: {muted}; font-size: 0.8rem; white-space: nowrap; }}
  .move-btn, .hide-btn {{
    flex-shrink: 0; background: transparent; border: none; color: {muted}; font-size: 1rem;
    cursor: pointer; padding: 2px 6px; user-select: none; -webkit-user-select: none;
  }}
  .move-btn:disabled {{ color: {border}; cursor: not-allowed; }}
  .hide-btn:hover {{ color: {error}; }}
  .empty {{ text-align: center; margin-top: 60px; font-size: 1.1rem; color: {muted}; }}
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
  <a href="{home_href}">홈</a>
  <a href="{live_href}">🔴 실시간 현황</a>
  <a href="{scrap_href}">📗 스크랩 완성본</a>
</div></div>
<div class="container">
  <header class="preview-head">
    <h1>📝 스크랩 초안 {run_slot_html}</h1>
    <a class="refresh-btn" href="preview.html">🔄 새로고침</a>
  </header>
  <div class="actions" id="preview-actions">{actions_html}</div>
  <div class="preview-hint" id="preview-hint">{hint}</div>
  <div id="preview-body">{body}</div>
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
// [수정: 2026-07-30] previewMoveArticle이 reload 없이 이 값을 새로 대입하므로 const가 아닌 let.
let PLAIN_TEXT = {plain_text_json};
function copyPlainText() {{
  navigator.clipboard.writeText(PLAIN_TEXT)
    .then(() => alert("클립보드에 복사했습니다."))
    .catch(() => alert("복사에 실패했습니다."));
}}
function sendToTelegram(btn) {{
  btn.disabled = true;
  fetch("http://{settings_host}:{settings_port}/telegram-send-draft", {{
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
// [추가: 2026-07-30] 자동분류가 완벽하지 않아 여러 기사를 한꺼번에 바로잡아야 할 때 쓰는
// 체크박스 다중 선택 + 하단 "장바구니" 바. 소제목 이름은 서버가 다시 내려주는 게 아니라
// 지금 화면에 그려진 <h2> 제목들을 그대로 읽어서 쓴다 — reload 없이 이동한 직후에도
// (previewMoveArticle이 #preview-body를 갈아끼운 뒤) 항상 최신 소제목 목록과 맞는다.
// [수정: 2026-07-30] 소제목 이름표(rename)가 붙어 있으면 화면엔 그 이름표가 보이지만,
// 서버(set_group_override)는 원래 분류 단어(원본 이름) 기준으로 소제목을 찾는다 —
// <h2> 텍스트(이름표)를 그대로 값으로 쓰면 이름표 붙은 소제목으로는 이동 자체가
// 조용히 실패한다. 이미 화면에 원본 이름을 갖고 있는 ✏️ 버튼의 data-name을 대신
// 읽어서, 값(value)엔 원본 이름을, 화면엔 이름표(data-current)를 쓴다.
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
// [수정: 2026-07-30] 예전엔 성공하면 location.reload()로 페이지 전체를 새로 불러왔는데,
// 그 reload가 서버에서 전체 그룹 재검색을 한 번 더 유발해 클릭 한 번에 네이버 검색이
// 2번(이동 계산 1번 + reload로 다시 그리기 1번) 돌았다. 이제 서버가 이동 계산에 쓴
// 결과를 그대로 화면 조각(JSON)으로 돌려주고, 여기서 DOM만 갈아끼운다 — 검색은
// 이동 계산 1번으로 끝난다(그마저도 app.preview_cache 덕에 증분 검색이라 가볍다).
function previewMoveArticle(btn) {{
  var url = btn.dataset.url;
  var direction = btn.dataset.direction;
  fetch("http://{settings_host}:{settings_port}/preview-move-article", {{
    method: "POST", keepalive: true,
    headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
    body: new URLSearchParams({{url: url, direction: direction}})
  }}).then(function(res) {{
    if (!res.ok) {{ alert("소제목 이동에 실패했습니다. 다시 시도해주세요."); return null; }}
    if (res.status === 204) {{ return null; }}  // 더 옮길 곳이 없거나 검색 실패 — 조용히 무시
    return res.json();
  }}).then(function(data) {{
    if (!data) return;
    document.getElementById("preview-body").innerHTML = data.body;
    document.getElementById("preview-actions").innerHTML = data.actions_html;
    document.getElementById("preview-hint").innerHTML = data.hint;
    PLAIN_TEXT = data.plain_text;
    renderRelativeTimes();
  }}).catch(function() {{
    alert("소제목 이동에 실패했습니다 — 앱이 실행 중인지 확인해주세요.");
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
// [추가: 2026-07-29] 지난번 이 화면을 봤을 때 있던 기사 URL 목록을 저장해뒀다가, 이번
// 로드에서 그 목록에 없는 것만 "새로 들어온 기사"로 표시한다(is-seen과는 다른 개념 —
// "내가 클릭해봤나"가 아니라 "지난 로드 이후 새로 생겼나"). previewKnownUrls가 아예
// 없으면(이 브라우저에서 초안 화면 첫 방문) 비교 기준이 없으므로 아무것도 표시하지
// 않고 이번 목록을 기준선으로만 저장한다.
(function () {{
  var currentUrls = Array.prototype.map.call(
    document.querySelectorAll(".article[data-url]"),
    function (el) {{ return el.dataset.url; }}
  );
  var known = null;
  try {{
    var raw = localStorage.getItem("previewKnownUrls");
    known = raw === null ? null : JSON.parse(raw);
  }} catch (e) {{ known = null; }}
  if (known !== null) {{
    document.querySelectorAll(".article[data-url]").forEach(function (el) {{
      if (known.indexOf(el.dataset.url) === -1) {{ el.classList.add("is-new-arrival"); }}
    }});
  }}
  localStorage.setItem("previewKnownUrls", JSON.stringify(currentUrls));
}})();
</script>
</body>
</html>
"""


def _theme() -> dict:
    return {
        "font_stack": FONT_STACK,
        "bg": COLOR_BG,
        "card": COLOR_CARD,
        "header": COLOR_HEADER,
        "accent": COLOR_ACCENT,
        "text": COLOR_TEXT,
        "muted": COLOR_TEXT_MUTED,
        "border": COLOR_BORDER,
        "hover": COLOR_HOVER,
        "error": COLOR_ERROR,
        # [추가: 2026-07-28] "이미 확인한 기사" 표시 전용 색 — app.renderer와 통일.
        "seen_purple": "#A855F7",
        # [추가: 2026-07-29] "지난번 봤을 때는 없었는데 이번에 새로 들어온 기사" 표시 전용
        # 색 — 초안은 열 때마다 다시 검색해 새 기사가 계속 섞여 들어오므로, 뭐가 방금
        # 추가됐는지 눈에 띄게 한다. 옅은 노랑(경고색 아님, 그냥 참고 정보) — 진한 톤은
        # 이 화면의 옅은 파스텔 톤과 안 어울려서 일부러 연하게 뒀다.
        "new_arrival_bg": "#FFF9C4",
        # [추가: 2026-08-03] 액션 툴바 버튼 리디자인(시안 B/A) 전용 색 — app.renderer와 통일.
        "tonal_hover": "#DCEAFE",
        "ghost_border": "#C7D9F7",
        "ghost_border_hover": "#A9C6F5",
    }


def _home_href() -> str:
    return f"http://{SETTINGS_SERVER_HOST}:{SETTINGS_SERVER_PORT}/home.html"


def _scrap_href() -> str:
    return f"http://{SETTINGS_SERVER_HOST}:{SETTINGS_SERVER_PORT}/index.html"


def _live_href() -> str:
    return f"http://{SETTINGS_SERVER_HOST}:{SETTINGS_SERVER_PORT}/live.html"


def _hidden_href() -> str:
    return f"http://{SETTINGS_SERVER_HOST}:{SETTINGS_SERVER_PORT}/hidden"


def _preview_keywords_signature(groups: list, slot: dict) -> dict:
    """캐시 유효성 비교용 값 — 키워드 그룹 구성 + 지금 회차의 식별값(slot["end"]).

    사용자가 검색 키워드를 고치거나(그룹 추가/삭제, 키워드 on-off, OR/AND 변경) 회차가
    바뀌면(정식 스크랩이 끝나 다음 회차로 넘어가면) 이 값이 달라져 캐시가 자동으로
    무효화된다(_load_preview_search_base 참고).
    """
    return {
        "keywords": [{"keywords": g["keywords"], "mode": g.get("mode", "OR")} for g in groups],
        "slot_end": slot["end"],
    }


def _load_preview_search_base(groups: list, slot: dict) -> tuple:
    """캐시가 유효하면(오늘 날짜 + 키워드 구성 + 회차 동일) 캐시된 기사와 "마지막으로
    본 시각"을, 아니면(캐시 없음/날짜 바뀜/키워드 바뀜/회차 바뀜) 빈 목록과 이 회차의
    시작 시각을 돌려준다. app.live_renderer._load_live_search_base와 같은 패턴이다.
    """
    signature = _preview_keywords_signature(groups, slot)
    cache = load_preview_cache()
    if cache is not None and cache.get("signature") == signature:
        last_seen = cache.get("last_seen_pub_date")
        if last_seen:
            return cache.get("articles", []), datetime.fromisoformat(last_seen)
    return [], kst_today_at(slot["start"])


def _compute_preview_articles(settings: dict, slot: dict) -> list:
    """slot 시작~지금까지를, 정식 회차(app.scraper.collect_run)와 똑같은 순서로 미리
    검색·정렬·필터링한다(검색 → 아웃렛 화이트리스트/우선순위 정렬 → 포토/인사 기사 제외
    → 제목 중복 제거 → 예약된 기사 합치기 → 숨긴 기사 제외 → 사용자가 직접 정리한 순서
    반영) — 저장은 하지 않는다.

    [수정: 2026-07-30] 매번 slot 시작부터 전체를 다시 검색하던 것을, app.live_cache와
    같은 증분 캐시(app.preview_cache)로 바꿨다 — 초안은 열 때마다, 특히 ↑/↓로 순서를
    옮길 때마다 다시 계산되는데(preview_move_article), 그때마다 등록된 키워드 그룹
    전체를 처음부터 다시 검색하면 느렸다. 이제 "마지막으로 본 시각 이후" 것만 추가로
    검색해 캐시에 병합한다. 검색 자체가 실패해도 캐시가 있으면 그 결과로 계속
    진행하고(완전히 빈 오류로 막지 않음), 캐시조차 없으면 예외를 그대로 올려 호출하는
    쪽(generate_preview_page/preview_move_article)이 안내 화면으로 대체하게 한다.

    [추가: 2026-07-30] 마지막에 app.preview_order.apply_preview_order를 거친다 — 검색
    결과는 매번 새로 계산되지만(저장된 회차 없음), 사용자가 ↑/↓로 같은 소제목 안에서
    정리해둔 순서(preview_move_article이 저장)는 여기서 다시 입혀진다. 그 사이 새로
    나온 기사는 원래 순서(언론사 우선순위) 그대로 뒤에 붙는다.
    """
    groups = active_search_groups(
        settings, [g for g in settings.get("keyword_groups", []) if g.get("include_in_scrap")]
    )
    outlet_order = settings.get("outlet_order", [])
    signature = _preview_keywords_signature(groups, slot)
    cached_articles, after_dt = _load_preview_search_base(groups, slot)
    before_dt = kst_today_at(datetime.now().strftime("%H:%M"))
    try:
        new_articles = search_articles_by_groups(groups, after=after_dt, before=before_dt)
    except requests.exceptions.RequestException:
        if not cached_articles:
            raise
        new_articles = []
    existing_cache_urls = {a["url"] for a in cached_articles}
    articles = cached_articles + [a for a in new_articles if a["url"] not in existing_cache_urls]
    pub_dates = [datetime.fromisoformat(a["pub_date"]) for a in articles if a.get("pub_date")]
    newest = max(pub_dates) if pub_dates else after_dt
    save_preview_cache(signature, newest.isoformat(), articles)

    if outlet_order:
        articles = filter_by_outlet_whitelist(articles, outlet_order)
        articles = sort_by_outlet_priority(articles, priority_outlets=outlet_order)
    else:
        articles = sort_by_outlet_priority(articles)
    if not settings.get("include_photo_in_scrap", False):
        articles = exclude_photo_articles(articles)
    if not settings.get("include_personnel_in_scrap", False):
        articles = exclude_personnel_articles(articles)
    articles = deduplicate_by_title(articles)
    # [추가: 2026-07-27] "위로"(예약) 눌러둔 기사를 검색 결과에 합쳐서, 이 회차가
    # 실제로 저장되기 전에도 초안 화면 소제목 분류에 바로 묶여 보이게 한다
    # (app.scraper.collect_run이 저장 시점에 똑같이 합치므로 나중에도 그대로 유지된다).
    pending = load_draft_pending_articles()
    if pending:
        existing_urls = {a["url"] for a in articles}
        articles = articles + [a for a in pending if a["url"] not in existing_urls]
    articles = filter_hidden(articles)
    return apply_preview_order(articles, load_preview_order())


def _group_select_html(current_name: str, all_names: list, labels: dict) -> str:
    """"다른 소제목으로" 드롭다운 — 지금 속한 소제목은 옵션에서 빼고 나머지를 보여준다.

    [추가: 2026-07-30] 체크박스 하단 바와 별개로, 기사 하나만 바로 다른 소제목에
    옮기고 싶을 때 쓴다(경계를 여러 번 넘나들며 ↑/↓를 반복할 필요 없이 한 번에 이동).
    고르는 즉시 반영되고(onchange), 옮길 곳이 아예 없으면(소제목이 이거 하나뿐)
    빈 문자열을 돌려줘 드롭다운 자체를 숨긴다.
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


def _render_preview_groups(groups: list, highlight_words: list, line_template: str, labels: dict) -> str:
    """소제목별 화면을 렌더링한다 (app.renderer._render_groups와 구성은 같지만, ↑/↓ 버튼이
    호출하는 JS만 previewMoveArticle로 다르다 — 미리보기는 저장된 회차가 없어
    app.curation.move_article이 기대하는 "현재 회차의 저장된 기사 목록 갱신"을 쓸 수
    없고, 소제목 경계를 넘는 이동(group_overrides.json)만 반영한다).

    [수정: 2026-07-30] 기사마다 체크박스(다중 선택)와 "다른 소제목으로" 드롭다운을
    함께 그린다 — 자동분류가 완벽하지 않아 여러 개를 한꺼번에 바로잡아야 하는 경우가
    있어서다(하단 선택 바가 여러 개를, 이 드롭다운이 하나씩 바로 옮기는 걸 담당).

    groups 중 기사가 0개인 소제목(app.custom_groups로 사용자가 미리 만들어둔 빈
    소제목)은 기사 목록 대신 안내 문구를 보여주고, 헤더의 🗑️도 "기사 숨기기"가 아니라
    "이 빈 소제목 자체를 삭제"로 동작한다 — 지울 기사가 없으니 의미가 다르다.
    """
    all_names = [g["name"] for g in groups]
    sections = []
    last_group_index = len(groups) - 1
    for group_index, group in enumerate(groups):
        original_name = html.escape(group["name"])
        display_name = html.escape(display_group_name(group["name"], labels))
        if not group["articles"]:
            body = (
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
            body = "\n".join(
                render_article(
                    a,
                    highlight_words,
                    line_template,
                    move={
                        "disable_up": i == 0 and group_index == 0,
                        "disable_down": i == last_index and group_index == last_group_index,
                    },
                    move_handler="previewMoveArticle",
                    checkbox=True,
                    group_select_html=group_select,
                )
                for i, a in enumerate(group["articles"])
            )
            delete_btn = (
                f'<button class="rename-btn" type="button" onclick="hideGroup(this)" '
                f'title="이 소제목 통째로 숨기기(기사 각각 숨긴 것과 동일 — 되돌리기 가능)">🗑️</button>'
            )
        heading = (
            f'<h2>&lt;{display_name}&gt; '
            f'<button class="rename-btn" type="button" data-name="{original_name}" '
            f'data-current="{display_name}" onclick="renameGroup(this)" title="소제목 이름 바꾸기">✏️</button> '
            f"{delete_btn}</h2>"
        )
        section_class = "subheading" if group["articles"] else "subheading subheading-empty"
        sections.append(f'<section class="{section_class}">{heading}{body}</section>')
    return "\n".join(sections)


def _render_preview_bottom(articles: list, groups: list, keywords: list, highlight_words: list, labels: dict) -> str:
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


def _manual_section_html(highlight_words: list, line_template: str) -> str:
    """"직접 추가한 기사"(app.manual_articles) 구획 — 미리보기 대상 회차가 14:00→17:00처럼
    넘어가도(app.scheduler.next_pending_slot) 이 구획은 영향받지 않는다. manual_articles.json은
    당일 자정에만 비워지는 완전히 별개 저장소라, 어느 회차를 미리보고 있는지와 무관하게
    항상 같은 내용을 보여준다(app.renderer.render_page가 index.html에서 하는 것과 동일).

    [수정: 2026-07-28] "📝 초안에 포함" 버튼을 누르면 아직 저장된 회차가 없어서 즉시
    끼워 넣을 곳이 없으므로, app.draft_articles의 예약 목록에 담아뒀다가 다음 회차가
    실제로 저장될 때(app.scraper.collect_run) 합쳐지고, 그 전까지도 이 초안 화면의
    소제목 분류에는 바로 반영된다(_compute_preview_articles). "📗 완성본에 포함"
    버튼은 완성 화면과 똑같이 이미 저장된 최신 회차에 바로 끼워 넣는다 — 렌더링
    자체는 app.renderer._render_manual_section을 그대로 재사용한다(두 버튼 다
    화면과 무관하게 동일하게 동작하므로 이 화면만의 커스터마이즈가 필요 없다).
    """
    manual_articles = filter_hidden(load_manual_articles())
    return _render_manual_section(manual_articles, highlight_words, line_template)


def _build_preview_plain_text(slot: dict, groups: list, line_template: str, labels: dict) -> str:
    """초안 화면의 copy/export용 텍스트를 만든다 (app.renderer._build_plain_text와 형식은
    같지만 헤더에 "[초안]"이 붙는다는 것만 다르다).

    [추가: 2026-07-27] 헤더 형식은 완성본과 똑같이 "언론 모니터링 {end} 기준"으로 두고
    맨 앞에 "[초안]"만 덧붙인다 — 처음엔 "지금 OO:OO 현재 기준"까지 같이 넣었는데,
    완성본과 긁는(추출하는) 방식 자체를 통일해달라는 요청에 따라 뺐다. "[초안]" 표시
    하나로도 정식 완성본과는 구분되고, 형식은 완성본과 동일해야 나중에 이 텍스트를
    완성본에 이어 붙이거나 비교할 때도 헷갈리지 않는다.
    """
    lines = [f"[초안] 언론 모니터링 {format_slot_time_kr(slot['end'])} 기준", ""]
    if not groups:
        lines.append("💤")
    else:
        for group in groups:
            lines.append(f"<{display_group_name(group['name'], labels)}>")
            for article in group["articles"]:
                lines.append(apply_line_template(line_template, article["outlet"], article["title"]))
                lines.append(article["url"])
                lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _actions_html(plain_text: str, slot_end: str, generated_at: str) -> str:
    export_filename = f"스크랩초안_{slot_end.replace(':', '-')}_{generated_at.replace(':', '-')}.txt"
    return (
        '<button onclick="copyPlainText()">복사</button>'
        f'<a class="btn" href="data:text/plain;charset=utf-8,{quote(plain_text)}" '
        f'download="{html.escape(export_filename)}">txt로 저장</a>'
        '<button onclick="sendToTelegram(this)">📤 Telegram 전송</button>'
        '<button class="create-group-btn" type="button" onclick="createCustomGroup()">+ 새 소제목 만들기</button>'
    )


def _compute_preview_content(
    slot: dict, articles: list, keywords: list, highlight_words: list, line_template: str, generated_at: str
) -> dict:
    """render_preview_page와 preview_move_article이 공유하는 실제 렌더링 계산 —
    소제목 분류·본문 HTML·안내문구·복사용 텍스트·상단 액션 버튼을 만든다.

    [추가: 2026-07-30] preview_move_article이 ↑/↓ 이동 직후 이 결과를 그대로
    JSON으로 돌려줄 수 있도록 render_preview_page에서 분리했다 — 페이지 전체를
    다시 감싸는 부분(_PAGE_TEMPLATE.format)만 render_preview_page에 남기고,
    실제로 매번 다시 계산해야 하는 내용은 여기 한 곳에만 있다.
    """
    labels = load_group_labels()
    custom_names = load_custom_groups()
    groups = (
        classify_articles(articles, keywords, forced_groups=load_group_overrides(), custom_group_names=custom_names)
        if articles
        else []
    )

    # [추가: 2026-07-30] "+ 새 소제목 만들기"로 미리 만들어둔, 아직 기사가 하나도 없는
    # 소제목도 화면에 같이 보여준다 — 실제 분류 결과(groups)와 합쳐서 한 번에 렌더링해야
    # "다른 소제목으로" 드롭다운·하단 선택 바가 이 빈 소제목도 이동 대상으로 인식한다.
    # 복사/내보내기 텍스트(_build_preview_plain_text)·AI 요약(_render_preview_bottom)에는
    # 원래의 groups만 넘겨 빈 placeholder가 섞이지 않게 한다.
    existing_names = {g["name"] for g in groups}
    empty_custom_groups = [{"name": n, "articles": []} for n in custom_names if n not in existing_names]
    render_groups = groups + empty_custom_groups

    if not render_groups:
        body = '<div class="empty">💤<br>아직 모인 기사가 없어요</div>'
    else:
        body = _render_preview_groups(render_groups, highlight_words, line_template, labels)
        if groups:
            body += _render_preview_bottom(articles, groups, keywords, highlight_words, labels)
    body += _manual_section_html(highlight_words, line_template)
    hint = (
        f"⏰ 아직 정식 회차가 아니에요 — {html.escape(format_slot_time_kr(slot['end']))}에 자동으로 정식 수집됩니다. "
        "지금 숨기거나(🗑️) 소제목을 옮겨두면(↑/↓) 정식 회차에도 그대로 반영돼요."
    )
    plain_text = _build_preview_plain_text(slot, groups, line_template, labels)
    return {
        "body": body,
        "hint": hint,
        "plain_text": plain_text,
        "actions_html": _actions_html(plain_text, slot["end"], generated_at),
    }


def render_preview_page(slot: dict, articles: list, keywords: list, highlight_words: list, line_template: str, generated_at: str) -> str:
    content = _compute_preview_content(slot, articles, keywords, highlight_words, line_template, generated_at)
    return _PAGE_TEMPLATE.format(
        **_theme(),
        run_slot_html=(
            f"({html.escape(format_slot_time_kr(slot['end']))} 예정 · "
            f"지금 {html.escape(format_slot_time_kr(generated_at))} 기준)"
        ),
        hint=content["hint"],
        body=content["body"],
        actions_html=content["actions_html"],
        plain_text_json=json.dumps(content["plain_text"], ensure_ascii=False).replace("</", "<\\/"),
        home_href=_home_href(),
        scrap_href=_scrap_href(),
        live_href=_live_href(),
        hidden_href=_hidden_href(),
        settings_host=SETTINGS_SERVER_HOST,
        settings_port=SETTINGS_SERVER_PORT,
    )


def render_preview_done_page() -> str:
    settings = load_settings()
    body = '<div class="empty">☕️ 수고하셨습니다.</div>'
    body += _manual_section_html(settings.get("highlight_keywords", []), DEFAULT_ARTICLE_LINE_TEMPLATE)
    return _PAGE_TEMPLATE.format(
        **_theme(),
        run_slot_html="",
        hint="⏰ 오늘 예정된 회차가 모두 끝났어요.",
        body=body,
        actions_html="",
        plain_text_json='""',
        home_href=_home_href(),
        scrap_href=_scrap_href(),
        live_href=_live_href(),
        hidden_href=_hidden_href(),
        settings_host=SETTINGS_SERVER_HOST,
        settings_port=SETTINGS_SERVER_PORT,
    )


def render_preview_error_page(slot: dict) -> str:
    settings = load_settings()
    body = '<div class="empty">🔌 지금은 조회할 수 없어요.<br>잠시 후 새로고침해주세요.</div>'
    body += _manual_section_html(settings.get("highlight_keywords", []), DEFAULT_ARTICLE_LINE_TEMPLATE)
    return _PAGE_TEMPLATE.format(
        **_theme(),
        run_slot_html=f"({html.escape(format_slot_time_kr(slot['end']))} 예정)",
        hint="⏰ 네이버 검색 중 오류가 발생했습니다.",
        body=body,
        actions_html="",
        plain_text_json='""',
        home_href=_home_href(),
        scrap_href=_scrap_href(),
        live_href=_live_href(),
        hidden_href=_hidden_href(),
        settings_host=SETTINGS_SERVER_HOST,
        settings_port=SETTINGS_SERVER_PORT,
    )


def generate_preview_page() -> Path:
    """요청이 올 때마다(새로고침 포함) 다시 계산해 preview.html을 새로 만든다 — live.html과
    같은 "매번 새로 생성, 저장 없음" 패턴. 오늘 남은 회차가 없으면 안내 화면으로 대체한다.
    """
    settings = load_settings()
    now = datetime.now()
    slot = next_pending_slot(now, active_schedule_times(settings))
    if slot is None:
        html_text = render_preview_done_page()
        atomic_write_text(PREVIEW_HTML_PATH, html_text)
        return PREVIEW_HTML_PATH

    try:
        articles = _compute_preview_articles(settings, slot)
    except requests.exceptions.RequestException:
        html_text = render_preview_error_page(slot)
        atomic_write_text(PREVIEW_HTML_PATH, html_text)
        return PREVIEW_HTML_PATH

    keywords = all_search_keywords(settings)
    html_text = render_preview_page(
        slot,
        articles,
        keywords,
        settings.get("highlight_keywords", []),
        settings.get("article_line_template", DEFAULT_ARTICLE_LINE_TEMPLATE),
        now.strftime("%H:%M"),
    )
    atomic_write_text(PREVIEW_HTML_PATH, html_text)
    return PREVIEW_HTML_PATH


def preview_move_article(url: str, direction: str) -> Optional[dict]:
    """미리보기 화면의 ↑/↓ 버튼이 호출한다(app.settings_server._handle_preview_move_article).

    저장된 회차가 없으므로 미리보기 목록을 다시 계산해 그 기준으로 옮긴다 — 소제목
    경계를 넘는 이동은 app.curation.set_group_override(URL 기준 전역 저장)로, 같은
    소제목 "안"에서의 순서 변경은 app.preview_order.save_preview_order로 각각 저장한다.

    [수정: 2026-07-30] 예전엔 같은 소제목 내 순서 변경은 저장할 곳이 없어 새로고침하면
    사라졌다 — move_article이 계산해낸 새 순서(new_articles)를 그냥 버리고 있었기
    때문. 이제 그 결과를 preview_order.json에 저장해 다음 계산(_compute_preview_articles)
    에도 그대로 반영되고, 정식 회차가 저장될 때(app.scraper.collect_run)도 한 번 더
    이어받는다.

    [수정: 2026-07-30] 예전엔 이 함수가 저장만 하고 None을 돌려주면, 브라우저가
    location.reload()로 페이지를 통째로 다시 불러와 서버가 검색을 한 번 더 하게
    만들었다(클릭 한 번에 전체 그룹 재검색 2번). 이제 이동 계산에 쓴 결과로 화면
    조각(본문·상단 액션·안내문구·복사용 텍스트)까지 바로 만들어 돌려준다 — 호출하는
    쪽(app.settings_server)이 이걸 JSON으로 그대로 응답하면 브라우저는 reload 없이
    DOM만 갈아끼우면 된다. preview.html 파일 자체도 같이 갱신해둬서, 다른 탭에서
    새로고침해도 같은 결과가 보인다.

    더 옮길 곳이 없거나(url을 못 찾음) 검색 실패로 계산 자체를 할 수 없으면(캐시도
    없어 예외가 남) None을 돌려주고 조용히 무시한다 — 호출하는 쪽이 204만 응답한다.
    """
    settings = load_settings()
    slot = next_pending_slot(datetime.now(), active_schedule_times(settings))
    if slot is None:
        return None
    try:
        articles = _compute_preview_articles(settings, slot)
    except requests.exceptions.RequestException:
        return None
    keywords = all_search_keywords(settings)
    overrides = load_group_overrides()
    new_articles, new_override = move_article(articles, keywords, url, direction, overrides)
    if new_override is not None:
        set_group_override(*new_override)
        final_articles = articles
    else:
        save_preview_order([a["url"] for a in new_articles])
        final_articles = new_articles

    highlight_words = settings.get("highlight_keywords", [])
    line_template = settings.get("article_line_template", DEFAULT_ARTICLE_LINE_TEMPLATE)
    generated_at = datetime.now().strftime("%H:%M")
    content = _compute_preview_content(slot, final_articles, keywords, highlight_words, line_template, generated_at)

    full_html = _PAGE_TEMPLATE.format(
        **_theme(),
        run_slot_html=(
            f"({html.escape(format_slot_time_kr(slot['end']))} 예정 · "
            f"지금 {html.escape(format_slot_time_kr(generated_at))} 기준)"
        ),
        hint=content["hint"],
        body=content["body"],
        actions_html=content["actions_html"],
        plain_text_json=json.dumps(content["plain_text"], ensure_ascii=False).replace("</", "<\\/"),
        home_href=_home_href(),
        scrap_href=_scrap_href(),
        live_href=_live_href(),
        hidden_href=_hidden_href(),
        settings_host=SETTINGS_SERVER_HOST,
        settings_port=SETTINGS_SERVER_PORT,
    )
    atomic_write_text(PREVIEW_HTML_PATH, full_html)
    return content
