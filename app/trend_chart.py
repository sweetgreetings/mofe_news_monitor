# Design Ref: HISTORY.md "홈 화면 재구성" — 정책 단어 추이 꺾은선 그래프.
"""정책 단어 추이(홈 카드 · 전용 화면 /trend)가 공유하는 SVG 꺾은선 차트.

서버가 SVG를 구워 내보내는 화면(app.landing_renderer/app.trend_renderer 공통 관례)이
같은 함수를 쓰되, "마우스를 올리면 그 날짜 값이 뜨는" 크로스헤어 툴팁은 전용 화면
(/trend)에만 얹는다 — 홈 카드는 그래프 전체가 /trend로 가는 링크 하나라 툴팁이 링크를
가릴 뿐이고, 자세히 보는 일은 "더보기" 너머가 맡는다(render_chart(hover=False)).
툴팁 값은 렌더링 시점에 이미 손에 든 것을 data 속성에 그대로 심어두므로, 호버할 때
서버 왕복이나 재계산이 없다(CLAUDE.md "새로 도는 API 호출이 없다" 원칙과 같은 이유).

"기록 없음"(그 구간에 저장된 회차가 아예 없음)은 0으로 잇지 않고 배경을 칠해 선을
끊는다 — 0건과 다르다(app.trend_data.articles_by_date_range 참고). 마지막 버킷이
아직 안 끝난 구간(오늘/이번주/이번달)이면 그 직전 점까지만 실선, 마지막 구간은
점선 + 빈 원으로 그려 "완결된 값이 아니다"를 표시한다.
"""
import html
import json

from app.config import PALETTE


def render_chart(chart_id: str, buckets: list, series: list, unit: str,
                  *, width: int = 760, height: int = 190, hover: bool = True,
                  today: dict = None, label_today: bool = True) -> str:
    """buckets: build_buckets()가 만든 목록에 record/partial/yesterday(bool)가 채워진 것
    (fill_bucket_status).
    series: [{"word", "color", "values"}] — values는 buckets와 길이가 같고,
    그 버킷에 record가 없으면 None.

    hover=False면 크로스헤어 툴팁에 필요한 자리(hover-layer/trend-tip/data 속성)를
    아예 안 그린다 — 홈 카드는 그래프 전체가 /trend로 가는 링크 하나라 툴팁을 안 쓴다
    (안 쓰는 화면에 빈 껍데기만 남겨두면 다음 사람이 "왜 안 뜨지"를 먼저 의심한다).

    today: {"label", "sub", "weekend", "values": {word: n}} — 주면 어제 칸 오른쪽에 점선으로
    떼어 「오늘」 칸을 하나 더 그린다. 오늘 값은 하루치의 일부라 선에 잇지 않고 빈 원만 찍는다
    (선으로 이으면 오전마다 급락처럼 보였다 — HISTORY.md "정책 단어 추이 — 오늘 대신 어제까지").
    label_today면 오른쪽 끝 라벨이 오늘 값을, 아니면 어제 값을 보인다.
    """
    n = len(buckets)
    tcol = n if today else None  # 오늘 칸의 인덱스(선 계열 밖)
    # [추가: 2026-09-14] 일별이고 마지막 칸이 어제면 그 날짜 밑에 「어제」 한 줄을 더 쓴다 —
    # 오른쪽 끝 값(`이형일 30`)이 오늘 값처럼 읽혀서다. 그만큼 아래 여백을 늘리고(그래프
    # 전체 높이는 그대로라 본체가 11px 낮아진다), 주말 띠는 원래 바닥까지 내려오므로 따라온다.
    # 주별·월별은 마지막 칸이 하루가 아니라 안 붙인다(yesterday가 늘 False).
    yday_last = unit == "day" and n > 0 and buckets[-1].get("yesterday", False)
    two_line = yday_last or bool(today)  # 날짜 밑에 「어제」/「오늘 N/M회차」 한 줄을 더 쓴다
    PL, PT, PB = 34, 12, 26 + (11 if two_line else 0)
    # [수정: 2026-09-17] 오른쪽 여백은 끝 라벨의 실제 길이로 정한다 — 고정 138px은 짧은 단어뿐일 때
    # 카드 오른쪽이 텅 비었다(사용자 지적). 글자 폭은 어림값(한글 10.5px, 숫자·공백 6px).
    def _label_w(s):
        nm = s["word"] if len(s["word"]) <= 7 else s["word"][:6] + "…"
        last = next((v for v in reversed(s["values"]) if v is not None), None)
        if last is None:
            return 0
        txt = f"{nm} {last}"
        return sum(6 if (c.isdigit() or c.isspace() or c.isascii()) else 10.5 for c in txt)
    if today and label_today:
        series_for_w = [dict(s, values=[today["values"].get(s["word"], 0)]) for s in series]
    else:
        series_for_w = series
    lab_off = 4 if today else 0  # 오늘 칸의 빈 원이 라벨 막대에 겹치지 않게 조금 띄운다
    PR = (22 + lab_off + max(_label_w(s) for s in series_for_w) + 4) if series else 16
    step = (width - PL - PR) / max(1, (n if today else n - 1))

    def x_at(i):
        return PL + i * step

    # [수정: 2026-09-11] 축 최댓값 = 「눈금 한 칸 × 3」. 예전엔 최댓값 자체를 10·20·50·100…
    # 사다리에서 골라 51~100이 전부 100이 됐다 — 최댓값 54짜리 주가 100 축에 올라가 위쪽
    # 45%가 비었다(HISTORY.md "정책 단어 추이 축 맞춤"). 한 칸을 1·2·5 계열에서 고르면
    # 54 → 60, 120 → 150처럼 데이터에 붙고 눈금 글자도 늘 정수다.
    numeric = [v for s in series for v in s["values"] if v is not None]
    if today:
        numeric += list(today["values"].values())
    peak = max(1, max(numeric) if numeric else 1)
    ticks = (1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000, 2500, 5000, 10000)
    tick = next((t for t in ticks if t * 3 >= peak), -(-peak // 3000) * 1000)
    ymax = tick * 3

    def y_at(v):
        return PT + (1 - v / ymax) * (height - PT - PB)

    out = []

    # 주말 음영 — 일별일 때만, 날짜 라벨 줄까지 내려서 띠와 "(토)"가 서로를 설명하게 한다.
    if unit == "day":
        for i, b in enumerate(buckets):
            if b["weekend"]:
                out.append(f'<rect x="{x_at(i)-step/2:.1f}" y="{PT}" width="{step:.1f}" '
                           f'height="{height-PT-4}" fill="#EDF0F3"/>')

    if today:
        tx = x_at(tcol)
        if today.get("weekend") and unit == "day":
            out.append(f'<rect x="{tx-step/2:.1f}" y="{PT}" width="{step/2:.1f}" '
                       f'height="{height-PT-4}" fill="#EDF0F3"/>')
        sx = tx - step / 2
        out.append(f'<line x1="{sx:.1f}" y1="{PT}" x2="{sx:.1f}" y2="{height-PB}" '
                   f'stroke="#C7CDD4" stroke-width="1" stroke-dasharray="3 3"/>')

    # "기록 없음" 연속 구간 — 0으로 안 잇고 배경 + 라벨, 선은 끊는다.
    i = 0
    while i < n:
        if buckets[i]["record"]:
            i += 1
            continue
        j = i
        while j < n and not buckets[j]["record"]:
            j += 1
        x0, x1 = x_at(i) - step / 2, x_at(j - 1) + step / 2
        out.append(f'<rect x="{x0:.1f}" y="{PT}" width="{x1-x0:.1f}" height="{height-PT-PB}" fill="#EAEDF1"/>')
        if x1 - x0 >= 58:
            out.append(f'<text x="{(x0+x1)/2:.1f}" y="{PT+(height-PT-PB)/2:.1f}" text-anchor="middle" '
                       f'font-size="10" fill="#9CA3AF">기록 없음</text>')
        i = j

    # 격자 + y축
    for g in range(4):
        v = tick * g
        y = y_at(v)
        out.append(f'<line x1="{PL}" y1="{y:.1f}" x2="{width-PR}" y2="{y:.1f}" stroke="#EEF0F2"/>')
        out.append(f'<text x="{PL-7}" y="{y+3.5:.1f}" text-anchor="end" font-size="9.5" '
                   f'fill="#9CA3AF">{int(v)}</text>')

    # x축 라벨 — 일별은 10개 이하면 전부, 넘으면 월요일마다(요일 반복 방지);
    # 주·월별은 10개 안쪽으로 솎아낸다.
    # [수정: 2026-09-14] 10개 이하(요일까지 다 찍힘)는 `13일(일)` — 월을 뺀다. 그 폭에선 달이
    # 바뀌어도 `31일 → 1일`로 숫자가 되돌아가 저절로 보인다. 긴 기간은 달이 여러 번
    # 바뀌므로 `9/7` 그대로다. 시안은 YESTERDAY_LABEL_MOCKUP.html.
    skip = max(1, -(-n // 10))
    label_y = height - 20 if two_line else height - 9
    for i, b in enumerate(buckets):
        if unit == "day":
            show = n <= 10 or b["is_monday"] or i == n - 1
            label = b["short_label"] if n <= 10 else b["label"]
        else:
            show = i % skip == 0 or i == n - 1
            label = b["label"]
        if not show:
            continue
        em = unit == "day" and b["weekend"]
        out.append(f'<text x="{x_at(i):.1f}" y="{label_y}" text-anchor="middle" font-size="9.5" '
                   f'fill="{"#4B5563" if em else "#9CA3AF"}" font-weight="{600 if em else 400}">'
                   f'{html.escape(label)}</text>')
        if yday_last and i == n - 1:
            out.append(f'<text x="{x_at(i):.1f}" y="{height-7}" text-anchor="middle" font-size="9" '
                       f'fill="{PALETTE["muted"]}">어제</text>')

    if today:
        tx = x_at(tcol)
        out.append(f'<text x="{tx:.1f}" y="{label_y}" text-anchor="middle" font-size="9.5" '
                   f'fill="#4B5563" font-weight="600">{html.escape(today["label"])}</text>')
        out.append(f'<text x="{tx:.1f}" y="{height-7}" text-anchor="middle" font-size="9" '
                   f'fill="{PALETTE["muted"]}">{html.escape(today["sub"])}</text>')

    # 계열별 선 — record 없는 구간에서 끊고, 마지막(집계 중) 구간만 점선 + 빈 원.
    end_labels = []
    for s in series:
        vals = s["values"]
        pts = [(i, x_at(i), y_at(v)) for i, v in enumerate(vals) if v is not None]
        segs, cur = [], []
        for i, v in enumerate(vals):
            if v is None:
                if cur:
                    segs.append(cur)
                cur = []
            else:
                cur.append((i, x_at(i), y_at(v)))
        if cur:
            segs.append(cur)
        for seg in segs:
            is_last = seg is segs[-1] and seg[-1][0] == n - 1 and buckets[-1]["partial"]
            body = seg[:-1] if is_last and len(seg) > 1 else seg
            if len(body) >= 2:
                p = " ".join(f"{x:.1f},{y:.1f}" for _, x, y in body)
                out.append(f'<polyline points="{p}" fill="none" stroke="{s["color"]}" '
                           f'stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>')
            if is_last and len(seg) >= 2:
                (_, x0, y0), (_, x1, y1) = seg[-2], seg[-1]
                out.append(f'<line x1="{x0:.1f}" y1="{y0:.1f}" x2="{x1:.1f}" y2="{y1:.1f}" '
                           f'stroke="{s["color"]}" stroke-width="2" stroke-dasharray="4 3"/>')
            # [수정: 2026-09-15] 날짜마다 찍던 점(40칸 이하)을 뺐다 — 추이를 보는 그래프라 점이
            # 흐름을 가리고 지저분했고(사용자 지적), 홈이 단어를 전부 그리게 되면서 더 심해졌다.
            # 값은 오른쪽 끝 라벨이, /trend에선 호버 크로스헤어의 점이 알려준다. 「집계 중」
            # 마지막 칸의 빈 원만 남긴다 — 장식이 아니라 "완결된 값이 아니다"라는 뜻이다.
            if is_last and len(seg) >= 2:
                _, x, y = seg[-1]
                out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="#FFFFFF" '
                           f'stroke="{s["color"]}" stroke-width="2"/>')
        if today:
            tv = today["values"].get(s["word"], 0)
            out.append(f'<circle cx="{x_at(tcol):.1f}" cy="{y_at(tv):.1f}" r="3.5" fill="#FFFFFF" '
                       f'stroke="{s["color"]}" stroke-width="2"/>')
            if label_today:
                end_labels.append({"word": s["word"], "color": s["color"], "y": y_at(tv), "v": tv})
                continue
        last_i = next((i for i in range(n - 1, -1, -1) if vals[i] is not None), None)
        if last_i is not None:
            end_labels.append({"word": s["word"], "color": s["color"], "y": y_at(vals[last_i]), "x0": x_at(last_i), "y0": y_at(vals[last_i]), "v": vals[last_i]})

    # 직접 라벨(오른쪽 여백) — 겹치면 위아래로 밀어내고, 축을 넘으면 통째로 위로 민다.
    end_labels.sort(key=lambda e: e["y"])
    for k in range(1, len(end_labels)):
        if end_labels[k]["y"] - end_labels[k - 1]["y"] < 13:
            end_labels[k]["y"] = end_labels[k - 1]["y"] + 13
    if end_labels:
        over = end_labels[-1]["y"] - (height - PB - 2)
        if over > 0:
            for e in end_labels:
                e["y"] -= over
        # [수정: 2026-09-17] 라벨이 검은 글자뿐이라 어느 선의 이름인지 알 수 없었다(사용자 지적).
        # 라벨 앞에 그 선 색의 짧은 막대를 둔다. 선 끝→라벨 연결선은 색으로 충분해 뺐다.
        # 글자는 본문색 유지(노랑 계열 글자는 흰 바탕에서 안 읽힌다).
        lx = width - PR + lab_off
        for e in end_labels:
            nm = e["word"] if len(e["word"]) <= 7 else e["word"][:6] + "…"
            ly = e["y"]
            out.append(f'<line x1="{lx+6:.1f}" y1="{ly:.1f}" x2="{lx+17:.1f}" y2="{ly:.1f}" '
                       f'stroke="{e["color"]}" stroke-width="3.5" stroke-linecap="round"/>')
            out.append(f'<text x="{lx+22:.1f}" y="{ly+3.5:.1f}" font-size="10.5" '
                       f'fill="#1F2937">{html.escape(nm)} <tspan fill="#6B7280">{e["v"]}</tspan></text>')

    if hover:
        out.append('<g class="hover-layer"></g>')
    svg = (f'<svg class="trend-chart" viewBox="0 0 {width} {height}" width="100%" role="img" '
           f'aria-label="정책 단어 추이 그래프">{"".join(out)}</svg>')

    if not hover:
        return (f'<div class="trend-chart-wrap" id="{html.escape(chart_id)}">{svg}</div>')

    tip_data = [{
        "label": b["tip_label"] if len(b["dates"]) == 1 else f'{b["dates"][0]} ~ {b["dates"][-1]}',
        "record": b["record"], "partial": b["partial"],
    } for b in buckets]
    series_data = [{"word": s["word"], "color": s["color"], "values": s["values"]} for s in series]

    return (
        f'<div class="trend-chart-wrap" id="{html.escape(chart_id)}" '
        f'data-w="{width}" data-h="{height}" data-pl="{PL}" data-pr="{PR}" data-pt="{PT}" data-pb="{PB}" '
        f'data-ymax="{ymax}" data-buckets=\'{json.dumps(tip_data, ensure_ascii=False)}\' '
        f'data-series=\'{json.dumps(series_data, ensure_ascii=False)}\'>'
        f'{svg}<div class="trend-tip"></div></div>'
    )


# 전용 화면(/trend) 페이지 템플릿에 그대로 끼워 넣는 크로스헤어 호버 스크립트.
# 값은 이미 data-buckets/data-series에 렌더링 시점 값 그대로 심어져 있으므로, 호버할
# 때 서버 왕복이나 재계산이 없다. hover=False로 그린 그래프(홈 카드)는 그 data 속성이
# 아예 없으므로 아래 루프가 건너뛴다 — 이 스크립트가 실린 화면에 섞여 들어와도 안전하다.
HOVER_JS = """
document.querySelectorAll('.trend-chart-wrap').forEach(function (wrap) {
  if (!wrap.dataset.buckets) return;
  var svg = wrap.querySelector('svg'), tip = wrap.querySelector('.trend-tip');
  var hover = svg.querySelector('g.hover-layer');
  var buckets = JSON.parse(wrap.dataset.buckets), series = JSON.parse(wrap.dataset.series);
  var W = +wrap.dataset.w, H = +wrap.dataset.h, PL = +wrap.dataset.pl, PR = +wrap.dataset.pr;
  var PT = +wrap.dataset.pt, PB = +wrap.dataset.pb, ymax = +wrap.dataset.ymax;
  var n = buckets.length, step = n > 1 ? (W - PL - PR) / (n - 1) : 0;
  function xAt(i) { return PL + i * step; }
  function yAt(v) { return PT + (1 - v / ymax) * (H - PT - PB); }
  svg.addEventListener('mousemove', function (e) {
    var r = svg.getBoundingClientRect();
    var sx = (e.clientX - r.left) * (W / r.width);
    var idx = Math.max(0, Math.min(n - 1, Math.round((sx - PL) / (step || 1))));
    var b = buckets[idx], x = xAt(idx);
    var dots = '<line x1="' + x.toFixed(1) + '" y1="' + PT + '" x2="' + x.toFixed(1) + '" y2="' +
      (H - PB) + '" stroke="#9CA3AF" stroke-width="1" stroke-dasharray="3 3"/>';
    if (b.record) series.forEach(function (s) {
      var v = s.values[idx];
      if (v === null || v === undefined) return;
      dots += '<circle cx="' + x.toFixed(1) + '" cy="' + yAt(v).toFixed(1) + '" r="4.5" fill="' +
        s.color + '" stroke="#FFFFFF" stroke-width="2"/>';
    });
    hover.innerHTML = dots;
    var head = '<div class="th">' + b.label + (b.partial ? ' · 집계 중' : '') + '</div>';
    var body = !b.record ? '<div class="tip-empty">저장된 회차 없음</div>' : series.map(function (s) {
      var v = s.values[idx];
      return '<div class="tr"><span class="dot" style="background:' + s.color + '"></span>' + s.word +
        '<span class="v">' + (v === null || v === undefined ? '—' : v) + '</span></div>';
    }).join('');
    tip.innerHTML = head + body;
    tip.style.display = 'block';
    var px = (x / W) * r.width;
    tip.style.left = Math.min(r.width - 150, Math.max(0, px + 14)) + 'px';
    tip.style.top = '6px';
  });
  svg.addEventListener('mouseleave', function () { tip.style.display = 'none'; hover.innerHTML = ''; });
});
"""

# 두 화면(app.landing_renderer/app.trend_renderer) 공용 CSS — 색은 app.config.PALETTE의
# 실제 값을 그대로 문자열에 박아 넣는다(이 모듈은 호출부의 .format(**PALETTE) 템플릿과
# 별개라 플레이스홀더를 쓸 수 없다 — CLAUDE.md 규칙1 "새 색을 CSS에 직접 적지 않는다"는
# "이름 없이 즉석 hex를 쓰지 않는다"는 뜻이라 PALETTE에서 가져온 값을 쓰는 건 지킨다).
def chart_css() -> str:
    return (
        ".trend-chart-wrap { position: relative; }"
        ".trend-chart { display: block; width: 100%; height: auto; }"
        f".trend-tip {{ position: absolute; pointer-events: none; background: {PALETTE['card']}; "
        f"border: 1px solid {PALETTE['border']}; border-radius: var(--r-md); box-shadow: var(--sh-pop); "
        "padding: 8px 10px; font-size: var(--fs-sm); display: none; z-index: 5; min-width: 128px; }"
        f".trend-tip .th {{ font-weight: 700; color: {PALETTE['header']}; margin-bottom: 5px; }}"
        f".trend-tip .tip-empty {{ color: {PALETTE['muted']}; }}"
        ".trend-tip .tr { display: flex; align-items: center; gap: 6px; margin-top: 2px; }"
        ".trend-tip .tr .dot { width: 8px; height: 8px; border-radius: var(--r-circle); }"
        ".trend-tip .tr .v { margin-left: auto; font-variant-numeric: tabular-nums; }"
        ".trend-legend { display: flex; flex-wrap: wrap; gap: 5px 16px; margin-top: 9px; }"
        ".trend-legend .li { display: inline-flex; align-items: center; gap: 6px; font-size: var(--fs-sm); }"
        ".trend-legend .li .dot { width: 11px; height: 3px; border-radius: var(--r-sm); }"
        f".trend-legend .li .n {{ color: {PALETTE['muted']}; font-size: var(--fs-sm); "
        "font-variant-numeric: tabular-nums; }"
    )
