# 홈 「부정 추정 기사」 — 정기 회차가 끝날 때마다, 그 회차 기사 중 제목·요약에 「재경부」·
# 「재정경제부」가 나온 것만 AI에 보내 재경부·그 정책을 부정적으로 다룬 기사를 골라 둔다.
#
# - 범위는 기관 전용: 정기 회차 기사 중 두 단어가 직접 나온 것만. 네이버를 따로 검색하지 않는다.
# - 판정 시점은 회차가 저장된 뒤(스케줄러 tick) — 담당자가 숨기기 전 목록으로 판정하고, 숨김은
#   화면(app.landing_renderer)이 그릴 때 뺀다. 회차끼리 기사가 겹치지 않아(앞 회차에 실린 기사는
#   다음 회차에서 빠진다) 같은 기사를 두 번 보내지 않는다.
# - AI에겐 부정 기사 번호와 근거만 받는다(중립·긍정은 답하지 않음) — 출력이 거의 없다.
# - 회차 파일은 건드리지 않는다(확정·발송이 회차 파일을 다시 쓴다). 결과는 data/negative_guess.json
#   하나, 자정에 새로 시작한다.
# - 사진 기사(app.filters.looks_like_photo_caption)는 보내지 않는다.
# - 키가 없거나 호출이 실패해도 앱은 멈추지 않는다 — 판정 못 한 회차는 5분 뒤 다시(회차마다 최대 3번).
# 판정 기준(프롬프트) 원본은 AI_RULES.md NEGATIVE_GUESS_PROMPT 섹션.
import json
import logging
import threading
from datetime import datetime
from typing import Optional

from app.atomic_write import atomic_write_text
from app.config import (
    NEGATIVE_GUESS_FILE,
    NEGATIVE_GUESS_KEYWORDS,
    NEGATIVE_GUESS_MAX_ATTEMPTS,
    NEGATIVE_GUESS_RETRY_MIN,
)
from app.credentials import llm_is_configured, llm_model
from app.filters import looks_like_photo_caption
from app.storage import load_today_runs

logger = logging.getLogger(__name__)

_SCHEMA = {
    "type": "object",
    "properties": {
        "negatives": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "i": {"type": "integer", "description": "부정 기사 번호"},
                    "reason": {"type": "string", "description": "근거 한 줄(40자 이내)"},
                },
                "required": ["i", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["negatives"],
    "additionalProperties": False,
}

_file_lock = threading.Lock()
_run_lock = threading.Lock()
# 회차마다 시도 기록(메모리만 — 스케줄러의 MAX_RETRIES와 같은 방식). {(날짜, 회차): (횟수, 마지막 시도)}
_attempts: dict = {}


def _empty(date: str) -> dict:
    return {"date": date, "runs": {}, "items": {}}


def load_state(today: Optional[str] = None) -> dict:
    """오늘 상태. 파일이 없거나 깨졌거나 어제 것이면 빈 상태."""
    today = today or datetime.now().strftime("%Y-%m-%d")
    with _file_lock:
        try:
            data = json.loads(NEGATIVE_GUESS_FILE.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return _empty(today)
    if not isinstance(data, dict) or data.get("date") != today:
        return _empty(today)
    base = _empty(today)
    base.update(data)
    return base


def _save_state(state: dict) -> None:
    with _file_lock:
        atomic_write_text(NEGATIVE_GUESS_FILE, json.dumps(state, ensure_ascii=False))


def mentions_ministry(article: dict) -> bool:
    """제목·요약에 「재경부」·「재정경제부」가 직접 나오는가 — 이 기능의 범위(기관 전용)."""
    text = f"{article.get('title') or ''} {article.get('summary') or ''}"
    return any(word in text for word in NEGATIVE_GUESS_KEYWORDS)


def candidates(run: dict) -> list:
    """이 회차에서 AI에 보낼 기사 — 두 단어가 나오고 사진 기사가 아닌 것."""
    return [a for a in run.get("articles", []) if mentions_ministry(a) and not looks_like_photo_caption(a)]


def _judge(articles: list) -> Optional[list]:
    """부정 기사 [{"i", "reason"}]를 돌려준다. 실패하면 None(호출부가 다음 시도로 넘긴다)."""
    from app.llm_classifier import _get_client, _load_prompt

    system_prompt = _load_prompt("NEGATIVE_GUESS_PROMPT")
    if not system_prompt:
        return None
    body = "\n".join(
        f"[{i + 1}] ({a.get('outlet', '')}) {a.get('title', '')}\n요약: {(a.get('summary') or '')[:300]}"
        for i, a in enumerate(articles)
    )
    try:
        response = _get_client().messages.create(
            model=llm_model(),
            max_tokens=4000,
            system=system_prompt,
            messages=[{"role": "user", "content": "기사 목록:\n" + body}],
            output_config={"format": {"type": "json_schema", "schema": _SCHEMA}},
        )
        text = next((b.text for b in response.content if b.type == "text"), None)
        if not text:
            logger.warning("부정 추정 판정 — 빈 응답(stop_reason=%s)", getattr(response, "stop_reason", None))
            return None
        negatives = json.loads(text).get("negatives", [])
        usage = getattr(response, "usage", None)
        if usage is not None:
            logger.info("부정 추정 판정 %d건 중 부정 %d건 — 입력 %s / 출력 %s 토큰",
                        len(articles), len(negatives), usage.input_tokens, usage.output_tokens)
        return negatives
    except Exception:
        logger.warning("부정 추정 판정 호출 실패 — 다음 시도에 다시 보냅니다", exc_info=True)
        return None


def judge_run(run: dict, today: str) -> bool:
    """회차 하나를 판정해 저장한다. 성공(보낼 기사가 없어 부르지 않은 경우 포함)하면 True."""
    arts = candidates(run)
    negatives = _judge(arts) if arts else []
    if negatives is None:
        return False
    state = load_state(today)
    slot = run["run_slot"]
    for x in negatives:
        i = x.get("i")
        if not isinstance(i, int) or not 1 <= i <= len(arts):
            continue
        a = arts[i - 1]
        state["items"][a["url"]] = {
            **{k: a.get(k) for k in ("url", "outlet", "title", "pub_date")},
            "reason": (x.get("reason") or "").strip(),
            "run_slot": slot,
        }
    state["runs"][slot] = {"judged_at": datetime.now().isoformat(timespec="seconds"), "count": len(arts)}
    _save_state(state)
    logger.info("부정 추정 %s 회차 — 대상 %d건, 부정 %d건", slot, len(arts), len(negatives))
    return True


def pending_runs(today: str) -> list:
    """오늘 저장됐는데 아직 판정 안 한 회차(오래된 것부터)."""
    done = load_state(today)["runs"]
    return [run for run in load_today_runs(today) if run["run_slot"] not in done]


def negative_guess_tick(now: Optional[datetime] = None, *, background: bool = True) -> None:
    """스케줄러 tick마다 불린다. 판정 안 한 회차가 있으면 백그라운드 스레드로 판정하고 즉시 돌아온다.

    - 키가 없으면 아무것도 안 한다.
    - 회차마다 최대 NEGATIVE_GUESS_MAX_ATTEMPTS번, 실패 뒤엔 NEGATIVE_GUESS_RETRY_MIN분 기다린다.
    - AI 호출은 스레드로 — 다음 회차 수집·자동발송을 막지 않게.
    """
    now = now or datetime.now()
    if not llm_is_configured():
        return
    today = now.strftime("%Y-%m-%d")
    due = []
    for run in pending_runs(today):
        count, last_at = _attempts.get((today, run["run_slot"]), (0, None))
        if count >= NEGATIVE_GUESS_MAX_ATTEMPTS:
            continue
        if last_at and (now - last_at).total_seconds() < NEGATIVE_GUESS_RETRY_MIN * 60:
            continue
        due.append(run)
    if not due or not _run_lock.acquire(blocking=False):
        return
    for run in due:
        count, _ = _attempts.get((today, run["run_slot"]), (0, None))
        _attempts[(today, run["run_slot"])] = (count + 1, now)
    for key in [k for k in _attempts if k[0] != today]:
        del _attempts[key]

    def _go():
        try:
            for run in due:
                judge_run(run, today)
        except Exception:
            logger.exception("부정 추정 판정 중 오류 — 다음 시도에 다시 합니다")
        finally:
            _run_lock.release()

    if background:
        threading.Thread(target=_go, name="negative-guess", daemon=True).start()
    else:
        _go()
