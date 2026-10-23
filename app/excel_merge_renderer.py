# 「📋 엑셀파일로 보고용 텍스트 생성」 화면 (`GET /excel-merge`) — 로직·규칙은 app/excel_merge.py.
#
# 담당자가 들고 온 표(언론사·제목·링크)를 보고서 한 줄 형식으로 바꾼다. 하는 일이 셋이라
# 화면도 세 단계다: ① 표 붙여넣기 → ② 열 맞추기 → ③ 결과.
#
# **표를 읽고 묶는 일은 전부 화면(JS)이 한다.** 서버로 가는 건 둘뿐이다 — .xlsx 파일 읽기
# (브라우저가 못 읽는다 · `POST /excel-merge/read`)와 AI 소제목 배정(`POST /excel-merge/classify`).
# 붙여넣기는 서버 왕복이 없어 붙여넣는 즉시 결과가 보인다.
#
# 모양은 다른 화면에서 그대로 가져온다 — 상단바(app.topnav)·800px 컨테이너·가져가기 글자
# 버튼(.exp)·주의/오류/잘됐다 안내 상자·보조 줄 체크(--chk-sm). 이 화면만의 새 모양은
# 만들지 않는다.
import html
import json
from datetime import datetime

from app.config import (
    DEFAULT_ARTICLE_LINE_TEMPLATE,
    DEFAULT_SUBHEADING_FORMAT_TEMPLATE,
    FONT_STACK,
    PALETTE,
    SETTINGS_SERVER_HOST,
    SETTINGS_SERVER_PORT,
)
from app.excel_merge import MAX_TABLE_ROWS, MAX_UPLOAD_BYTES
from app.icons import icon
from app.renderer import copy_text_script
from app.llm_classifier import is_configured as llm_is_configured
from app.settings import load_settings
from app.topnav import plain_nav, topnav_style

EXCEL_MERGE_TITLE = "엑셀파일로 보고용 텍스트 생성"

_BASE = f"http://{SETTINGS_SERVER_HOST}:{SETTINGS_SERVER_PORT}"


def _style() -> str:
    """이 화면의 CSS. 값은 전부 팔레트·모양 토큰에서 온다(숫자·색을 직접 안 적는다)."""
    p = PALETTE
    return f"""
  * {{ box-sizing: border-box; }}
  .ic {{ width: 1em; height: 1em; stroke: currentColor; fill: none; stroke-width: 1.9;
    stroke-linecap: round; stroke-linejoin: round; vertical-align: -0.15em; flex-shrink: 0; }}
  body {{ margin: 0; background: {p['bg']}; color: {p['text']}; font-family: {FONT_STACK}; }}
  /* 위 여백 24 + 안쪽 44 — 확정본·초안·보관함·설정과 같은 값(오갈 때 제목이 안 들썩이게). */
  .container {{ max-width: 800px; margin: 24px auto 80px; background: {p['card']};
    border: 1px solid {p['border']}; border-radius: var(--r-lg); padding: 44px 24px 26px; }}
  h1 {{ font-size: var(--fs-xl); color: {p['header']}; margin: 0 0 6px; }}
  .hint {{ color: {p['text_soft']}; font-size: var(--fs-md); margin: 0 0 20px; line-height: 1.7; }}

  .step {{ margin-top: 20px; }}
  .step:first-of-type {{ margin-top: 0; }}
  .shead {{ display: flex; align-items: center; gap: 8px; margin: 0 0 8px; flex-wrap: wrap; }}
  .shead .n {{ font-size: var(--fs-lg); color: {p['header']}; font-weight: 700; flex: none; }}
  .shead .sub {{ font-size: var(--fs-sm); color: {p['text_soft']}; min-width: 0; }}
  .shead .cnt {{ font-size: var(--fs-xs); font-weight: 700; color: {p['text_soft']};
    background: {p['screen_tag_work_bg']}; border: 1px solid {p['border']};
    border-radius: var(--r-pill); padding: 1px 8px; white-space: nowrap; }}

  /* 가져가기 — 다른 화면과 같은 글자 버튼 묶음. */
  .exp {{ display: inline-flex; gap: 2px; flex: none; margin-left: auto; }}
  .exp button {{ border: none; background: transparent; color: {p['text_soft']}; font-weight: 500;
    border-radius: var(--r-md); padding: 4px 8px; font-size: var(--fs-sm); cursor: pointer; font-family: inherit; }}
  .exp button:hover {{ background: transparent; color: {p['accent']}; text-decoration: underline; }}
  .exp button[disabled] {{ opacity: 0.45; cursor: default; }}
  .exp button[disabled]:hover {{ color: {p['text_soft']}; text-decoration: none; }}

  /* ① 붙여넣기 */
  .inbox {{ border: 1px solid {p['border']}; border-radius: var(--r-lg); background: {p['bg']};
    padding: 12px 14px; }}
  textarea.paste {{ width: 100%; min-height: 108px; resize: vertical; border: 1px solid {p['border']};
    border-radius: var(--r-md); padding: 8px 10px; background: {p['card']}; color: {p['text']};
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: var(--fs-sm); line-height: 1.7; }}
  textarea.paste:focus {{ outline: none; border-color: {p['accent']};
    box-shadow: 0 0 0 3px {p['accent_border']}; }}
  .inrow {{ display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-top: 10px; }}
  .btn {{ height: var(--h-md); border: 1px solid {p['border']}; background: {p['card']}; color: {p['text']};
    border-radius: var(--r-md); padding: 0 14px; font-size: var(--fs-md); font-weight: 500;
    font-family: inherit; cursor: pointer; display: inline-flex; align-items: center; gap: 4px; }}
  .btn:hover {{ background: {p['hover']}; border-color: {p['accent_border']}; }}
  .btn-go {{ height: var(--h-md); background: {p['accent']}; color: #FFFFFF; border: none;
    border-radius: var(--r-md); padding: 0 16px; font-size: var(--fs-md); font-weight: 600;
    font-family: inherit; cursor: pointer; }}
  .btn-go:hover {{ background: {p['accent_pressed']}; }}
  .btn-txt {{ border: none; background: transparent; color: {p['text_soft']}; font-size: var(--fs-sm);
    font-weight: 500; font-family: inherit; cursor: pointer; padding: 4px 6px; border-radius: var(--r-md); }}
  .btn-txt:hover {{ color: {p['accent']}; text-decoration: underline; }}
  /* AI 버튼은 크기와 무관하게 채운 연보라 — 확정본·초안의 `.regen-btn`과 같은 배색
     (배경 ai_bg · 글자 ai_text · 테두리 ai_border)이라 세 화면이 같은 모양을 지킨다.
     파란 대표 동작(「목록 만들기」)과는 색으로 갈린다. */
  .btn-ai {{ height: var(--h-tb); background: {p['ai_bg']}; color: {p['ai_text']};
    border: 1px solid {p['ai_border']}; border-radius: var(--r-md); padding: 0 14px;
    font-size: var(--fs-md); font-weight: 600; font-family: inherit; cursor: pointer;
    display: inline-flex; align-items: center; gap: 5px; }}
  .btn-ai:hover {{ background: {p['ai_bg_hover']}; border-color: {p['ai_border_hover']}; }}
  .btn-ai[disabled] {{ opacity: 0.5; cursor: default; }}
  .or {{ font-size: var(--fs-sm); color: {p['muted']}; }}
  .inrow .right {{ margin-left: auto; display: inline-flex; align-items: center; gap: 8px; }}

  /* ② 열 맞추기 */
  .maptable {{ width: 100%; border-collapse: collapse; }}
  .maptable th, .maptable td {{ text-align: left; padding: 6px 8px; border-bottom: 1px solid {p['border']};
    font-size: var(--fs-sm); vertical-align: middle; }}
  .maptable th {{ color: {p['text_soft']}; font-weight: 600; font-size: var(--fs-xs); white-space: nowrap; }}
  .maptable td.val {{ color: {p['text']}; max-width: 330px; overflow: hidden; text-overflow: ellipsis;
    white-space: nowrap; }}
  .maptable td.val.dim {{ color: {p['placeholder']}; }}
  select.map {{ height: var(--h-tb); border: 1px solid {p['border']}; border-radius: var(--r-md);
    background: {p['card']}; color: {p['text']}; font-size: var(--fs-sm); font-family: inherit;
    padding: 0 6px; cursor: pointer; }}
  select.map.unused {{ color: {p['placeholder']}; }}
  select.map.dup {{ border-color: {p['warn_border']}; background: {p['warn_bg']}; }}

  /* 정리 줄 — 보조 줄이라 체크는 --chk-sm(오늘 전체 기사 특수조건 줄과 같은 모양). */
  .frow {{ display: flex; align-items: center; gap: 12px; flex-wrap: wrap; margin-top: 12px; }}
  .frow label {{ display: inline-flex; align-items: center; gap: 5px; font-size: var(--fs-sm);
    color: {p['text']}; cursor: pointer; }}
  .frow input[type="checkbox"] {{ flex: none; width: var(--chk-sm); height: var(--chk-sm); margin: 0;
    accent-color: {p['accent']}; cursor: pointer; }}
  .frow label.off {{ color: {p['muted']}; cursor: default; }}

  /* ③ 결과 */
  pre.out {{ margin: 0; border: 1px solid {p['border']}; border-radius: var(--r-lg); background: {p['bg']};
    padding: 14px 16px; font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: var(--fs-sm); line-height: 1.8; color: {p['text']}; white-space: pre-wrap;
    word-break: break-all; max-height: 420px; overflow: auto; }}
  .fmt-note {{ font-size: var(--fs-sm); color: {p['muted']}; margin: 8px 0 0; line-height: 1.7; }}
  .fmt-note a {{ color: {p['accent']}; }}

  .warn {{ background: {p['warn_bg']}; border: 1px solid {p['warn_border']}; color: {p['warn_text']};
    border-radius: var(--r-lg); padding: 10px 12px; font-size: var(--fs-md); line-height: 1.6;
    margin-top: 12px; }}
  .warn a {{ color: {p['warn_text']}; }}
  .err {{ background: {p['error_bg']}; border: 1px solid {p['error_border']}; color: {p['error']};
    border-radius: var(--r-lg); padding: 10px 12px; font-size: var(--fs-md); line-height: 1.6;
    margin-top: 12px; }}
  .ok-note {{ background: {p['ok_bg']}; border: 1px solid {p['ok_border']}; color: {p['ok_text']};
    border-radius: var(--r-lg); padding: 8px 12px; font-size: var(--fs-md); margin-top: 12px; }}
  .hidden {{ display: none; }}

  @media (max-width: 640px) {{
    .container {{ padding: 40px 14px 20px; margin: 16px 8px 60px; }}
    .maptable td.val {{ max-width: 150px; }}
    .inrow .right {{ margin-left: 0; width: 100%; }}
  }}
"""


# 화면의 JS. **평문 raw 문자열이다** — f-string·.format을 쓰지 않으므로 `\n`이 파이썬에
# 먹히지 않고 JS 문자열 리터럴로 그대로 나간다(CLAUDE.md 「작업 규칙」의 `\\n` 사고 방지).
# 서버가 넘기는 값은 전부 아래 `EM` 한 덩어리(JSON)로 받는다.
_SCRIPT = copy_text_script() + r"""
var $ = function (id) { return document.getElementById(id); };

// 열의 역할 — 값은 서버와 주고받는 내부 이름, 글자는 화면 이름.
var ROLES = [
  ["skip", "— 안 씀"],
  ["section", "소제목"],
  ["outlet", "언론사"],
  ["title", "제목"],
  ["url", "링크"],
  ["summary", "요약"],
  ["pubdate", "발행일"]
];

var rows = [];      // 붙여넣은/올린 표
var mapping = [];   // 열마다 역할
var hasHeader = true;   // 첫 줄이 열 이름인가 — 앱이 보고 정한다(아래 looksLikeHeader)
var items = [];     // 지금 결과에 담긴 기사 (AI 배정·내보내기가 이걸 본다)
var aiNames = {};   // AI가 붙인 소제목 {url: 이름} — 표의 소제목 열을 덮어쓰지 않고 위에 얹는다

function esc(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function splitRows(text) {
  var out = [];
  text.replace(/\r/g, "").split("\n").forEach(function (line) {
    if (!line.trim()) return;
    var cells = line.indexOf("\t") >= 0 ? line.split("\t") : line.split(",");
    out.push(cells.map(function (c) { return c.trim().replace(/^"|"$/g, ""); }));
  });
  return out;
}

// 열 역할 짐작 — 「소제목」이 「제목」을 품고 있어 소제목·구분을 먼저 본다(순서가 곧 규칙).
function guessRole(header, sampleValue, used) {
  var h = (header || "").replace(/\s/g, "");
  var v = sampleValue || "";
  var pick = null;
  if (/^https?:\/\//.test(v)) pick = "url";
  else if (/링크|url|주소/i.test(h)) pick = "url";
  else if (/소제목|구분|분류|주제|섹션/.test(h)) pick = "section";
  else if (/언론사|매체|신문|방송사/.test(h)) pick = "outlet";
  else if (/제목|기사명|헤드/.test(h)) pick = "title";
  else if (/요약|내용|본문|발췌/.test(h)) pick = "summary";
  else if (/발행|게시|일자|날짜|시각/.test(h)) pick = "pubdate";
  if (pick && used.indexOf(pick) >= 0) pick = null;
  return pick || "skip";
}

// 첫 줄이 열 이름인가 — **담당자에게 묻지 않고 앱이 본다.** 기사 줄에는 반드시 링크가
// 있고(링크가 없으면 어차피 건너뛴다) 열 이름 줄에는 없다. 그래서 판정이 틀릴 자리가 없다.
// 체크 칸으로 두던 때는 끄고 켜는 뜻을 담당자가 매번 다시 읽어야 했고, 잘못 꺼 두면 첫 기사가
// 조용히 열 이름으로 먹혔다. 대신 **어떻게 읽었는지는 ② 머리줄이 말한다**(renderMap).
function looksLikeHeader(row) {
  return !(row || []).some(function (c) { return /^https?:\/\//.test((c || "").trim()); });
}

function buildMapping() {
  hasHeader = looksLikeHeader(rows[0]);
  var header = hasHeader ? rows[0] : [];
  var first = rows[hasHeader ? 1 : 0] || rows[0] || [];
  var width = 0;
  rows.forEach(function (r) { width = Math.max(width, r.length); });
  var used = [];
  mapping = [];
  for (var i = 0; i < width; i++) {
    var role = guessRole(header[i], first[i], used);
    if (role !== "skip") used.push(role);
    mapping.push(role);
  }
  // 못 짚은 자리를 순서대로 메운다 — 언론사·제목·링크 셋이 가장 흔한 순서가 그 순서다.
  ["outlet", "title", "url"].forEach(function (need) {
    if (mapping.indexOf(need) >= 0) return;
    for (var i = 0; i < mapping.length; i++) {
      if (mapping[i] === "skip") { mapping[i] = need; return; }
    }
  });
  renderMap(header, first);
}

function renderMap(header, first) {
  var body = $("mapbody");
  body.innerHTML = "";
  var counts = {};
  mapping.forEach(function (r) { if (r !== "skip") counts[r] = (counts[r] || 0) + 1; });
  mapping.forEach(function (role, i) {
    var tr = document.createElement("tr");
    var name = hasHeader && header[i] ? header[i] : String.fromCharCode(65 + i);
    var val = first[i] || "";
    tr.innerHTML = '<td>' + esc(name) + '</td>' +
      '<td class="val' + (val ? "" : " dim") + '">' + esc(val || "(빈 칸)") + '</td>';
    var td = document.createElement("td");
    var sel = document.createElement("select");
    sel.className = "map" + (role === "skip" ? " unused" : "") + (counts[role] > 1 ? " dup" : "");
    ROLES.forEach(function (r) {
      var op = document.createElement("option");
      op.value = r[0]; op.textContent = r[1];
      if (r[0] === role) op.selected = true;
      sel.appendChild(op);
    });
    sel.onchange = function () { mapping[i] = sel.value; aiNames = {}; render(); };
    td.appendChild(sel);
    tr.appendChild(td);
    body.appendChild(tr);
  });
  var miss = [];
  if (mapping.indexOf("title") < 0) miss.push("제목");
  if (mapping.indexOf("url") < 0) miss.push("링크");
  // 첫 줄을 어떻게 읽었는지 **먼저** 적는다 — 그게 틀리면 아래 표가 통째로 한 줄씩 밀린다.
  $("mapsub").textContent = miss.length
    ? miss.join("·") + " 칸을 고르면 결과가 나와요"
    : (hasHeader ? "첫 줄은 열 이름으로 읽었어요" : "첫 줄에 링크가 있어 기사로 읽었어요")
      + " · 짐작이 다르면 고치세요";
}

function outletRank(name) {
  var i = EM.outlets.indexOf(name);
  return i < 0 ? EM.outlets.length : i;   // 목록에 없는 언론사는 버리지 않고 뒤로(앱 공통 규칙)
}

function render() {
  var data = rows.slice(hasHeader ? 1 : 0);
  var ci = {};
  ROLES.forEach(function (r) { ci[r[0]] = mapping.indexOf(r[0]); });

  if (ci.title < 0 || ci.url < 0) {
    $("step3").classList.add("hidden");
    $("skipnote").classList.add("hidden");
    return;
  }

  var seen = {}, skipped = { nourl: [], notitle: [], dup: [] };
  items = [];
  data.forEach(function (r, n) {
    var line = n + 1 + (hasHeader ? 1 : 0);
    var url = (r[ci.url] || "").trim();
    var title = (r[ci.title] || "").trim();
    if (!url || !/^https?:\/\//.test(url)) { if (title || url) skipped.nourl.push(line); return; }
    if (!title) { skipped.notitle.push(line); return; }
    // 같은 링크는 늘 한 번이다 — 한 보고서에 같은 기사를 두 번 적을 일이 없어 고를 것이
    // 아니다(정기 수집도 같은 URL은 1건). 건너뛴 줄은 아래 안내가 번호로 적는다.
    if (seen[url]) { skipped.dup.push(line); return; }
    seen[url] = 1;
    var cell = function (key) { return ci[key] >= 0 ? (r[ci[key]] || "").trim() : ""; };
    items.push({
      section: aiNames[url] || cell("section"),
      outlet: cell("outlet"), title: title, url: url,
      summary: cell("summary"), pubdate: cell("pubdate")
    });
  });

  if ($("byOutlet").checked && !$("byOutlet").disabled) {
    // 손으로 맞춘 순서가 없는 목록이라 통째로 정렬해도 잃을 게 없다 — 같은 순위 안에서는
    // 표에 나온 순서를 지킨다(안정 정렬).
    items = items.map(function (it, i) { return [it, i]; })
      .sort(function (a, b) {
        var d = outletRank(a[0].outlet) - outletRank(b[0].outlet);
        return d !== 0 ? d : a[1] - b[1];
      })
      .map(function (pair) { return pair[0]; });
  }

  // 소제목별로 묶는다 — 표(또는 AI)에 나온 순서 그대로, 빈 칸은 「기타」.
  var order = [], buckets = {};
  var anySection = items.some(function (it) { return it.section; });
  items.forEach(function (it) {
    var key = anySection ? (it.section || "기타") : "";
    if (!(key in buckets)) { buckets[key] = []; order.push(key); }
    buckets[key].push(it);
  });

  var lines = [];
  order.forEach(function (key) {
    if (key) lines.push(EM.section.split("{section}").join(key));
    buckets[key].forEach(function (it) {
      lines.push(EM.line.split("{outlet}").join(it.outlet).split("{title}").join(it.title));
      lines.push(it.url);
      lines.push("");   // 기사마다 빈 줄 하나(앱의 보고서 텍스트와 같은 모양)
    });
  });
  var text = lines.join("\n").replace(/\n+$/, "\n");

  $("out").textContent = items.length ? text : "";
  $("out").classList.toggle("hidden", !items.length);
  $("outcnt").textContent = items.length + "건";
  $("step3").classList.remove("hidden");
  syncButtons();

  var msgs = [];
  if (skipped.nourl.length) msgs.push("링크 칸이 비었거나 주소가 아니에요 (" + skipped.nourl.join("·") + "번째 줄)");
  if (skipped.notitle.length) msgs.push("제목 칸이 비었어요 (" + skipped.notitle.join("·") + "번째 줄)");
  if (skipped.dup.length) msgs.push("같은 링크가 또 있어요 (" + skipped.dup.join("·") + "번째 줄)");
  if (msgs.length) {
    var n = skipped.nourl.length + skipped.notitle.length + skipped.dup.length;
    $("skipnote").innerHTML = "⚠️ " + n + "줄은 건너뛰었어요 — " + msgs.map(esc).join(" · ");
    $("skipnote").classList.remove("hidden");
  } else {
    $("skipnote").classList.add("hidden");
  }
}

function syncButtons() {
  var any = items.length > 0;
  ["cp", "txbtn", "xlbtn"].forEach(function (id) { if ($(id)) $(id).disabled = !any; });
  var blank = items.filter(function (it) { return !it.section; }).length;
  if ($("ai")) {
    $("ai").disabled = !any || blank === 0;
    $("ai").title = blank === 0 && any
      ? "소제목이 비어 있는 기사가 없어요"
      : "소제목이 비어 있는 기사만 AI가 넣습니다(이미 적힌 소제목은 안 건드려요) — 표에 요약 열이 있으면 ②에서 맞춰 두세요. 제목만 볼 때보다 정확합니다";
  }
  if ($("clearsec")) {
    var hasSec = items.some(function (it) { return it.section; });
    $("clearsec").classList.toggle("hidden", !hasSec);
  }
}

function run() {
  rows = splitRows($("paste").value);
  aiNames = {};
  if (!rows.length) {
    $("step2").classList.add("hidden");
    $("step3").classList.add("hidden");
    showError("붙여넣은 표가 없어요. 엑셀에서 칸을 골라 복사한 뒤 붙여넣으세요.");
    return;
  }
  hideError();
  $("step2").classList.remove("hidden");
  buildMapping();
  render();
}

function showError(msg) { $("filenote").textContent = msg; $("filenote").classList.remove("hidden"); }
function hideError() { $("filenote").classList.add("hidden"); }

// ── AI 소제목 배정 ──────────────────────────────────────────────────
function assignWithAI() {
  var btn = $("ai");
  var label = btn.innerHTML;
  btn.disabled = true;
  btn.textContent = "배정 중…";
  $("ainote").classList.add("hidden");
  fetch(EM.base + "/excel-merge/classify", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8" },
    body: "items=" + encodeURIComponent(JSON.stringify(items))
  }).then(function (r) { return r.json(); }).then(function (res) {
    btn.innerHTML = label;
    if (res.reason) {
      $("ainote").textContent = "⚠️ " + res.reason;
      $("ainote").classList.remove("hidden");
      syncButtons();
      return;
    }
    Object.keys(res.assigned || {}).forEach(function (url) { aiNames[url] = res.assigned[url]; });
    render();
    var n = Object.keys(res.assigned || {}).length;
    $("ainote").classList.add("hidden");
    $("aiok").textContent = "✓ " + n + "건에 소제목을 넣었어요. 이름이 어색하면 엑셀에서 고쳐 다시 붙여넣으세요.";
    $("aiok").classList.remove("hidden");
    setTimeout(function () { $("aiok").classList.add("hidden"); }, 6000);
  }).catch(function () {
    btn.innerHTML = label;
    $("ainote").textContent = "⚠️ AI를 부르지 못했어요. 앱이 켜져 있는지 확인하고 다시 눌러 주세요.";
    $("ainote").classList.remove("hidden");
    syncButtons();
  });
}

// ── 버튼 연결 ───────────────────────────────────────────────────────
$("go").onclick = run;
$("byOutlet").onchange = render;
$("clear").onclick = function () {
  $("paste").value = ""; rows = []; items = []; aiNames = {};
  $("step2").classList.add("hidden"); $("step3").classList.add("hidden");
  hideError();
  $("paste").focus();
};
if ($("ai")) $("ai").onclick = assignWithAI;
if ($("clearsec")) $("clearsec").onclick = function () {
  // 소제목을 통째로 비운다 — 표에서 온 소제목까지 비우므로, 되돌리는 길은 「목록 만들기」를
  // 다시 누르는 것이다(원본 표는 ① 칸에 그대로 있다).
  aiNames = {};
  mapping = mapping.map(function (r) { return r === "section" ? "skip" : r; });
  renderMap(hasHeader ? rows[0] : [], rows[hasHeader ? 1 : 0] || rows[0] || []);
  render();
};
$("pickfile").onclick = function () { $("file").click(); };
$("file").onchange = function () {
  var f = this.files && this.files[0];
  var input = this;
  if (!f) return;
  if (f.size > EM.maxUpload) {
    showError("파일이 너무 커요. 표만 남겨 다시 저장해 주세요.");
    input.value = "";
    return;
  }
  if (!/\.xlsx$/i.test(f.name)) {
    // csv·tsv·txt는 브라우저가 바로 읽는다(서버에 보낼 이유가 없다).
    var rd = new FileReader();
    rd.onload = function () { $("paste").value = String(rd.result); input.value = ""; run(); };
    rd.readAsText(f, "utf-8");
    return;
  }
  // .xlsx는 서버가 openpyxl로 읽는다 — 원본을 base64로 실어 보낸다(multipart를 쓰지
  // 않는 이유는 설정 서버의 do_POST가 본문을 urlencoded 하나로만 읽기 때문이다).
  var rd2 = new FileReader();
  rd2.onload = function () {
    var b64 = String(rd2.result).split(",")[1] || "";
    showError("엑셀을 읽는 중…");
    fetch(EM.base + "/excel-merge/read", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8" },
      body: "data=" + encodeURIComponent(b64)
    }).then(function (r) { return r.json(); }).then(function (res) {
      input.value = "";
      if (res.error) { showError(res.error); return; }
      hideError();
      $("paste").value = (res.rows || []).map(function (r) { return r.join("\t"); }).join("\n");
      run();
    }).catch(function () {
      input.value = "";
      showError("엑셀을 읽지 못했어요. 앱이 켜져 있는지 확인해 주세요.");
    });
  };
  rd2.readAsDataURL(f);
};
$("cp").onclick = function () {
  var t = $("out").textContent;
  if (!t) return;
  pmCopy(t).then(function () {
    $("cpnote").classList.remove("hidden");
    setTimeout(function () { $("cpnote").classList.add("hidden"); }, 1800);
  });
};

// 텍스트·엑셀은 다른 화면과 같은 길로 내려간다(POST /download-text · /download-excel).
// 누르는 순간 「지금 화면에 보이는 것」을 폼에 담는다.
function fillText(form) {
  var t = $("out").textContent;
  if (!t) return false;
  form.querySelector('input[name="text"]').value = t;
  return true;
}
function fillRows(form) {
  if (!items.length) return false;
  var anySection = items.some(function (it) { return it.section; });
  var out = items.map(function (it) {
    return {
      "스크랩일자": "", "스크랩종료시간": "",
      "발행일 발행시간": it.pubdate || "",
      "언론사명": it.outlet || "", "기사제목": it.title, "URL": it.url,
      "소제목": anySection ? (it.section || "기타") : "",
      "라벨명": ""
    };
  });
  form.querySelector('input[name="rows"]').value = JSON.stringify(out);
  return true;
}
window.fillText = fillText;
window.fillRows = fillRows;
"""


def _export_html(file_base: str) -> str:
    """복사·텍스트·엑셀 — 다른 화면과 같은 글자 버튼 묶음(.exp), 「지금 화면에 보이는 것」."""
    base = html.escape(file_base)
    return (
        '<span class="exp">'
        '<button type="button" id="cp" disabled>복사</button>'
        '<form method="POST" action="/download-text" style="display:contents"'
        ' onsubmit="return fillText(this)">'
        f'<input type="hidden" name="filename" value="{base}.txt">'
        '<input type="hidden" name="text" value="" />'
        '<button type="submit" id="txbtn" disabled>텍스트</button></form>'
        '<form method="POST" action="/download-excel" style="display:contents"'
        ' onsubmit="return fillRows(this)">'
        f'<input type="hidden" name="filename" value="{base}.xlsx">'
        '<input type="hidden" name="rows" value="" />'
        '<button type="submit" id="xlbtn" disabled>엑셀</button></form>'
        "</span>"
    )


def _ai_controls_html(ai_ready: bool) -> str:
    """③ 결과 머리줄 왼쪽 — 내용을 바꾸는 버튼은 왼쪽, 가져가기는 오른쪽(툴바 규칙).

    AI 키가 없으면 버튼을 그리지 않고 앰버 주의 한 줄로 바꾼다 — 눌러도 같은 결과인
    버튼을 두면 「고장났나」로 읽힌다(초안의 `.classify-degraded.no-key`와 같은 판단:
    키 없음은 실패가 아니라 담당자의 선택이라 빨강이 아니다).
    """
    if not ai_ready:
        return ""
    return (
        f'<button class="btn-ai" id="ai" type="button">{icon("bot")} AI 소제목 배정</button>'
        '<button class="btn-txt hidden" id="clearsec" type="button"'
        ' title="표와 AI가 넣은 소제목을 모두 비웁니다 — 되돌리려면 「목록 만들기」를 다시 누르세요">소제목 비우기</button>'
    )


def render_excel_merge_page() -> str:
    """「엑셀파일로 보고용 텍스트 생성」 화면 한 장."""
    settings = load_settings()
    ai_ready = llm_is_configured()
    baked = {
        "base": _BASE,
        "line": settings.get("article_line_template") or DEFAULT_ARTICLE_LINE_TEMPLATE,
        "section": settings.get("subheading_format_template") or DEFAULT_SUBHEADING_FORMAT_TEMPLATE,
        "outlets": list(settings.get("outlet_order") or ()),
        "maxUpload": MAX_UPLOAD_BYTES,
        "maxRows": MAX_TABLE_ROWS,
    }
    file_base = f"{datetime.now().strftime('%Y-%m-%d')}_언론모니터링_목록"
    line_sample = html.escape(baked["line"])
    # 「등록 언론사 순서로 정렬」은 설정에서 체크한 언론사를 쓴다 — 0곳이면 거르지 않고
    # 칸을 잠근다(오늘 전체 기사·기관 기사 찾기의 「선택 언론사만」과 같은 규칙).
    no_outlets = not baked["outlets"]
    outlet_label = (
        '<label class="off" title="설정 「언론사 선택 및 순서 지정」에서 언론사를 먼저 골라 주세요">'
        '<input type="checkbox" id="byOutlet" disabled /> 등록 언론사 순서로 정렬</label>'
        if no_outlets else
        '<label title="설정 「언론사 선택 및 순서 지정」의 순서를 씁니다 — 목록에 없는 언론사는 뒤로 갑니다">'
        '<input type="checkbox" id="byOutlet" /> 등록 언론사 순서로 정렬</label>'
    )

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>{EXCEL_MERGE_TITLE}</title>
<style>
  {topnav_style()}
  {_style()}
</style>
</head>
<body>
{plain_nav("설정", _BASE + "/")}
<div class="container">
  <h1>📋 {EXCEL_MERGE_TITLE}</h1>
  <p class="hint">언론사·제목·링크가 든 표를 보고서 한 줄 형식으로 바꿉니다.</p>

  <div class="step">
    <div class="shead">
      <span class="n">① 표 붙여넣기</span>
      <span class="sub">엑셀에서 칸을 골라 복사(⌘C)한 뒤 아래에 붙여넣으세요 — 첫 줄은 열 이름으로 읽습니다</span>
    </div>
    <div class="inbox">
      <textarea class="paste" id="paste" spellcheck="false" placeholder="언론사&#9;제목&#9;링크
한국경제&#9;가상자산 과세 2년 유예…&#9;https://n.news.naver.com/mnews/article/015/0005123456"></textarea>
      <div class="inrow">
        <button class="btn" id="pickfile" type="button"><span class="plus-glyph">+</span>엑셀 파일 올리기</button>
        <span class="or">또는 위 칸에 붙여넣기</span>
        <input type="file" id="file" accept=".xlsx,.csv,.tsv,.txt" class="hidden" />
        <span class="right">
          <button class="btn-txt" id="clear" type="button">비우기</button>
          <button class="btn-go" id="go" type="button">목록 만들기</button>
        </span>
      </div>
      <div class="err hidden" id="filenote"></div>
    </div>
  </div>

  <div class="step hidden" id="step2">
    <div class="shead">
      <span class="n">② 열 맞추기</span>
      <span class="sub" id="mapsub">표의 첫 줄을 보고 짐작해 뒀어요 — 다르면 고치세요</span>
    </div>
    <table class="maptable">
      <thead>
        <tr><th style="width:60px">표의 열</th><th>첫 기사 값</th><th style="width:132px">무엇으로 쓸까</th></tr>
      </thead>
      <tbody id="mapbody"></tbody>
    </table>
    <div class="frow">
      {outlet_label}
    </div>
    <div class="warn hidden" id="skipnote"></div>
  </div>

  <div class="step hidden" id="step3">
    <div class="shead">
      <span class="n">③ 결과</span>
      <span class="cnt" id="outcnt">0건</span>
      {_ai_controls_html(ai_ready)}
      {_export_html(file_base)}
    </div>
    <pre class="out" id="out"></pre>
    <p class="fmt-note">정기 「기사 제목 형식」(<code>{line_sample}</code>)을 그대로 씁니다 —
      바꾸려면 <a href="{_BASE}/format">설정 → 텍스트 표출 형식</a>.</p>
    <div class="warn hidden" id="ainote"></div>
    <div class="ok-note hidden" id="aiok"></div>
    <div class="ok-note hidden" id="cpnote">✓ 결과를 복사했어요.</div>
    {'' if ai_ready else '<div class="warn">AI 소제목 배정은 AI 키가 등록돼 있어야 씁니다. <a href="' + _BASE + '/llm">설정 → LLM(AI) 연동</a>에서 키를 넣으면 이 자리에 버튼이 생겨요.</div>'}
  </div>
</div>
<script>
var EM = {json.dumps(baked, ensure_ascii=False)};
{_SCRIPT}
</script>
</body>
</html>
"""
