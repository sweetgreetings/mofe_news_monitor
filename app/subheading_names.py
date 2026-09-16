# Design Ref: PRD.md 기능2 규칙 8 — 소제목 이름 바꾸기 / 새 소제목 만들기의 이름 자동완성
"""✏️ 이름 바꾸기·+ 새 소제목 만들기 창이 보여줄 **최근에 쓴 소제목 이름** 목록을 만든다.

[추가: 2026-09-03] AI는 같은 사안에도 회차마다 이름을 새로 짓는다(실측: 회차가 넘어갈 때
직전 회차 소제목 중 살아남는 이름은 평균 1개뿐). 그래서 담당자는 매 회차 ✏️로 같은 이름을
손으로 다시 쳐 왔다 — 실제로 「후보자 비거주 주택 관련」은 11:00·14:00 두 회차에서, AI가
지은 원래 단어가 서로 다른데도 같은 이름표로 두 번 쳐 넣은 기록이 남아 있다.

**목록의 축은 「회차」다**(사용자 결정). 사람이 지었는지 AI가 지었는지가 아니라 "지금
이어지고 있는 사안인가"가 실제로 쓸모 있는 축이라서고, 그래서 최신 회차가 맨 위에 온다.

  - 아무것도 안 치면 → 오늘·어제 전 회차의 소제목 전부(회차별로 나눠서)
  - 한 글자라도 치면 → 같은 목록에서 걸러내되 회차 구분 없이 평평하게

[수정: 2026-09-10] 기본 목록이 예전엔 **직전 회차 하나**였다(사용자 결정). 두 가지가
동시에 깨져서 넓혔다. (1) 그 회차가 비면(💤이거나 저장 사고) 목록이 **0줄**이 되는데,
그러면 화면이 「찾은 게 없으면 목록 자리를 안 그린다」 규칙에 걸려 창이 통째로 입력칸만
남는다 — 담당자 눈엔 기능이 사라진 것으로 보인다(2026-09-10 제보, 실측으로 그제 시점에도
같은 상태였다). (2) 애초에 직전 회차 하나로는 되쓰기의 절반을 놓쳤다(실측 4건 중 2건이
더 앞 회차 이름). 목록 높이 상한(250px, 약 8줄)과 스크롤이 이미 걸려 있어 24~58줄이
돼도 창 크기는 그대로고, 최신 회차순이라 **위 8줄이 곧 직전 회차**다.

**목록에 들어가는 건 「그 회차 화면에 실제로 찍혔던 이름」뿐이다** — 이름표가 붙은 소제목은
이름표(=담당자가 친 글자)로 들어가고, 담당자가 갈아치운 AI 원래 단어는 **어디에도 안 나온다**
(담당자가 거부한 이름이라서).

**고르는 것과 적용하는 것은 다르다.** 이 목록은 보여주기만 하고, 고른 이름은 예전과 똑같이
그 회차에만 붙는다(app.curation.set_group_label). 2026-08-14 사고("기타"에 붙인 이름표가 그
뒤 36개 회차·259건에 자동으로 씌워짐)는 앱이 **알아서 적용**했기 때문이지 기억했기 때문이
아니다 — 그 경계를 이 모듈이 넘지 않는다.
"""
from datetime import datetime, timedelta
from typing import Optional

from app.curation import filter_hidden, load_group_labels
from app.custom_groups import load_custom_groups
from app.llm_classifier import UNCLASSIFIED_GROUP_NAME
from app.storage import list_runs_for_date

# 목록에 담을 날짜 수(오늘 포함). 오늘+어제 = 2 — 며칠씩 이어지는 사안(부총리 후보자 등)을
# 잡으면서 목록이 부풀지 않는 선(실측: 이틀치 67개, 사흘 이상은 재사용이 사실상 없었다).
_RECENT_DAYS = 2


def _round_sort_key(date_str: str, slot: str) -> tuple:
    return (date_str, slot or "")


def _round_display_names(date_str: str, run: dict) -> list:
    """그 회차 화면에 실제로 찍혔던 소제목 이름을 [(표시이름, kind)]로.

    kind는 "mine"(담당자가 이름표로 고쳤거나 직접 만든 것) 또는 "ai"(AI가 지었고 담당자가
    그대로 둔 것). **커스텀 소제목은 이름표가 아니라 원래 이름 자리에 저장되므로**
    (data/custom_groups.json은 자정에 비워진다) 어제 만든 커스텀 소제목은 AI가 지은 것과
    구분되지 않는다 — 그때는 kind가 "ai"로 나온다. 즉 **볼드 표시는 오늘 것만 정확하다**
    (사용자와 합의한 알려진 한계 — 이름 자체는 그대로 다 나오므로 고르는 데는 지장이 없다).
    """
    slot = run.get("run_slot")
    labels = load_group_labels((date_str, slot))
    custom = set(load_custom_groups())
    raw_names = {a.get("group") for a in filter_hidden(run.get("articles", [])) if a.get("group")}
    rows = []
    for raw in sorted(raw_names):
        if raw == UNCLASSIFIED_GROUP_NAME:
            continue  # 자리표시자 — 소제목 이름으로 제안하면 보고서로 샌다
        rows.append((labels.get(raw, raw), "mine" if (raw in labels or raw in custom) else "ai"))
    return rows


def _recent_rounds(now: Optional[datetime] = None) -> list:
    """오늘·어제 저장된 회차를 [(date_str, slot, run), ...] 최신순으로."""
    base = now or datetime.now()
    rounds = []
    for back in range(_RECENT_DAYS):
        date_str = (base - timedelta(days=back)).strftime("%Y-%m-%d")
        for run in list_runs_for_date(date_str):
            rounds.append((date_str, run.get("run_slot") or "", run))
    rounds.sort(key=lambda r: _round_sort_key(r[0], r[1]), reverse=True)
    return rounds


def _when_label(date_str: str, slot: str, now: Optional[datetime] = None) -> str:
    today = (now or datetime.now()).strftime("%Y-%m-%d")
    return ("오늘 " if date_str == today else "어제 ") + slot


def name_pool(round_id: Optional[tuple], now: Optional[datetime] = None) -> dict:
    """이름 고르기 창이 쓸 데이터.

    round_id는 지금 그 화면이 보여주는 회차 — **그보다 앞선** 회차 중 가장 최근 것이
    "직전 회차"다(초안이면 최신 확정 회차, 확정본이면 그 앞 회차).

    돌려주는 값:
        {"all": [{"name","kind","when"}, ...]}   # 오늘·어제 전부, 최신 회차순·이름 중복 제거

    **최신순이라 첫 묶음이 곧 「직전 회차」다** — 화면은 그 사실을 구분선에 적기만 하고
    별도 목록으로 뽑지 않는다(예전엔 prev를 따로 실어 보냈는데, 그 회차가 비면 목록이
    통째로 0줄이 됐다 — 위 [수정: 2026-09-10] 참고).
    """
    rounds = _recent_rounds(now)
    cutoff = _round_sort_key(*round_id) if round_id else None

    seen = set()
    everything = []
    for date_str, slot, run in rounds:
        # 지금 보고 있는 회차와 그 이후는 "이전에 쓴 이름"이 아니다. 확정본에서 이걸 안 빼면
        # 목록 맨 위 묶음이 그 화면에 이미 떠 있는 이름들이라 통째로 취소선으로 뜬다(실측 5줄).
        # 초안은 round_id가 None이다 — 진행 중 회차는 아직 저장 전이라 애초에 여기 없다.
        if cutoff and _round_sort_key(date_str, slot) >= cutoff:
            continue
        for name, kind in _round_display_names(date_str, run):
            if name in seen:
                continue  # 같은 이름은 가장 최근 회차 것만 남긴다
            seen.add(name)
            everything.append({"name": name, "kind": kind, "when": _when_label(date_str, slot, now)})
    return {"all": everything}
