# Design Ref: PERF_DB.md "재는 법"
"""화면 한 장이 **저장소에 몇 번 묻는지** 센다 — 호출부(파일:줄)별로.

**왜 있나.** 저장 자리가 Postgres로 가면서 비용의 축이 바뀌었다. 파일일 땐
`.stat()`·열기가 1µs라 「자주 물어보고 조금씩 아끼기」가 이겼는데, DB에선 왕복
한 번이 15ms라 **묻는 횟수가 거의 유일한 비용**이다. 그래서 「무엇이 느린가」는
프로파일러로 함수 시간을 보는 게 아니라 **어느 줄이 store를 몇 번 부르는가**를
세야 보인다. 그 수를 세는 자리가 여기다.

**재기만 한다.** `app.store`의 읽기 함수를 감싸 세고, **쓰기 함수는 전부 막는다**
(`write_text`·`write_json`·`delete`·`move`·`ensure_dir`, 화면 HTML을 남기는
`atomic_write_text`까지). 그래서 DB도 `data/`도 한 글자 안 바뀐다 —
[CLAUDE.md](../CLAUDE.md) 「검증 루프」의 ②를 DB 모드에서도 안전하게 돌리기 위한
조건이다.

**쓰기를 막는 것이 측정의 전제다.** 막지 않으면 렌더링 중에 끼어 있는 인덱스
저장(`run_index`)·캐시 저장이 실제 데이터를 건드린다
(2026-11-05에 테스트가 진짜 DB를 덮은 사고와 같은 길이다).

쓰는 법:  python3 tools/measure_store_calls.py            # 화면 셋 다
          python3 tools/measure_store_calls.py home       # 홈만
          python3 tools/measure_store_calls.py index history

  화면 이름: index(확정본) · history(정기 보관함) · home(홈)

`DATABASE_URL`이 있으면 DB를 재고, 없으면 파일을 잰다 — **두 값을 나란히 놓는 것이
이 도구의 쓸모다**(같은 화면이 파일에선 몇 ms, DB에선 몇 초인가).

**파일을 잴 땐 그 폴더의 `data/`를 본다** — 워크트리에서 돌리면 워크트리의 빈 `data/`를
재서 빈 화면이 0.0초로 나온다. 운영 폴더에서 `DATABASE_URL= python3 …`로 돌린다.
"""
import collections
import pathlib
import sys
import time
import traceback

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

from app import store  # noqa: E402  (load_dotenv가 먼저 돌아야 백엔드가 정해진다)

_READ_FNS = ("read_text", "exists", "signature", "list_files")
_WRITE_FNS = ("write_text", "write_json", "delete", "move", "ensure_dir")

calls: collections.Counter = collections.Counter()
seconds: collections.Counter = collections.Counter()
blocked_writes = collections.Counter()


def _call_site() -> str:
    """store를 부른 **바깥 첫 줄** — store.py 안쪽과 이 파일은 건너뛴다."""
    for frame in reversed(traceback.extract_stack(limit=14)[:-1]):
        if "/app/store.py" in frame.filename or __file__ in frame.filename:
            continue
        return f"{frame.filename.split('/app/')[-1]}:{frame.lineno} {frame.name}"
    return "(모름)"


def _counting(name: str):
    original = getattr(store, name)

    def wrapped(*args, **kwargs):
        site = _call_site()
        started = time.perf_counter()
        try:
            return original(*args, **kwargs)
        finally:
            key = f"{name:10} <- {site}"
            calls[key] += 1
            seconds[key] += time.perf_counter() - started

    return wrapped


def _blocked(*_args, **_kwargs):
    blocked_writes["n"] += 1
    return None


def _install() -> None:
    for name in _READ_FNS:
        setattr(store, name, _counting(name))
    for name in _WRITE_FNS:
        setattr(store, name, _blocked)
    import app.atomic_write as atomic_write

    atomic_write.atomic_write_text = _blocked


def _screens() -> dict:
    # 서버가 요청마다 부르는 그 함수들을 그대로 부른다(app/settings_server.py
    # _SCREEN_HTML_ROUTES) — 화면 이름도 그 표의 뜻을 따른다.
    from app.history_renderer import history_page_html
    from app.landing_renderer import landing_page_html
    from app.settings_server import _confirmed_screen_html

    return {
        "index": ("확정본  /index.html", _confirmed_screen_html),
        "history": ("정기 보관함  /history.html", history_page_html),
        "home": ("홈  /home.html", landing_page_html),
    }


def measure(label: str, render) -> None:
    calls.clear()
    seconds.clear()
    blocked_writes.clear()
    started = time.perf_counter()
    error = None
    out = None
    try:
        out = render()
    except Exception as exc:  # 화면 하나가 깨져도 나머지는 재야 한다
        error = f"{type(exc).__name__}: {exc}"
    elapsed = time.perf_counter() - started

    size = f" · 결과 {len(out) / 1024:.0f}KB" if isinstance(out, str) else ""
    print(f"\n=== {label} ===")
    print(
        f"  {elapsed:.1f}초 · store 읽기 {sum(calls.values())}회"
        f" · 막은 쓰기 {blocked_writes['n']}회{size}"
    )
    if error:
        print(f"  !! {error}")
    print("  어느 줄이 부르나 (많은 순):")
    for key, count in calls.most_common(15):
        print(f"    {count:6}회 {seconds[key]:6.1f}초  {key}")


def main(names: list) -> None:
    where = "Postgres" if store._pg() is not None else "파일(data/)"
    print(f"저장 자리: {where}")
    _install()
    screens = _screens()
    for name in names or list(screens):
        if name not in screens:
            print(f"(모르는 화면 이름: {name} — index · history · home 중에서)")
            continue
        label, render = screens[name]
        measure(label, render)


if __name__ == "__main__":
    main(sys.argv[1:])
