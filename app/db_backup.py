# -*- coding: utf-8 -*-
"""DB 백업을 **하루 한 번 앱이 뜬다** — 손으로 돌리던 것을 자동으로.

**왜 있나.** `data/` 폴더는 DB로 옮긴 날 얼어붙었고 그 뒤의 데이터는 Supabase에만 있다
(1년치 기사·라벨·설정·API 키 전부). 지금까지는 담당자가 `python3 supabase_setup.py
내려받기`를 **손으로** 돌려야 사본이 생겼는데, 손으로 하는 일은 바쁜 날 빠진다
(MULTIUSER_ROLLOUT_CHECKLIST.md B-3).

**어느 컴퓨터에서 뜨나 — 두 조건을 다 만족해야 한다.**

1. **수집 임차를 쥔 한 대.** 이 함수는 스케줄러의 「한 번만」 자리에서 불린다
   (`app/scheduler.py`). 여러 대가 같은 DB를 각자 받아 봐야 같은 값이고, 무엇보다
   **참가자 노트북에 남의 기사와 API 키가 통째로 쌓인다.**
2. **백업 폴더가 이미 있는 컴퓨터.** 기본은 `~/press-monitor-백업`(손으로 돌리던
   `supabase_setup.py`와 **같은 자리**)이고 `.env`의 `DB_BACKUP_DIR`로 바꾼다.
   담당자 맥에는 그 폴더가 있고 참가자 컴퓨터에는 없다 — 그래서 **참가자에게는 아무
   일도 안 생긴다.** 기본 자리는 **없으면 만들지 않는다**(만들면 이 규칙이 무너진다).
   `DB_BACKUP_DIR`로 직접 적었으면 그건 분명한 뜻이라 만들어 준다.

**`data/`에는 절대 받지 않는다** — `store_pg.download_to_files`의 전제이고, DB가 상했을 때
디스크의 성한 원본까지 같이 날리지 않기 위해서다(2026-11-05에 테스트가 DB를 덮었을 때
살려낸 것이 그 폴더였다).
"""
import logging
import os
import re
import shutil
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from app import store_pg
from app.config import BASE_DIR, DB_BACKUP_KEEP

logger = logging.getLogger(__name__)

# 폴더 이름 — 손으로 돌리는 `supabase_setup.py`와 **같은 모양**이라 한 자리에 섞여 쌓인다.
_PREFIX = "db백업_"
_NAME_RE = re.compile(r"^db백업_\d{4}-\d{2}-\d{2}")

# 하루에 세 번까지만 해 본다(네트워크가 끊긴 날 10초마다 매달리지 않게). 메모리에만 둔다.
_MAX_ATTEMPTS = 3
_attempts: dict = {}
_lock = threading.Lock()


def backup_root() -> Optional[Path]:
    """백업이 쌓이는 자리. **여기가 없으면 백업을 아예 안 한다**(위 설명)."""
    told = (os.getenv("DB_BACKUP_DIR") or "").strip()
    if told:
        root = Path(told).expanduser()
        root.mkdir(parents=True, exist_ok=True)   # 직접 적었으면 분명한 뜻이다
        return root
    root = BASE_DIR.parent / "press-monitor-백업"
    return root if root.is_dir() else None


def _todays_backup(root: Path, today: str) -> Optional[Path]:
    """오늘 이미 받아 둔 폴더(손으로 받은 것도 센다). 없으면 `None`."""
    for path in sorted(root.glob(f"{_PREFIX}{today}*")):
        if path.is_dir():
            return path
    return None


def _prune(root: Path) -> list:
    """오래된 백업을 지운다 — **우리 이름 모양의 폴더만.**

    이름이 날짜라 사전순이 곧 시간순이다. 사람이 손으로 둔 다른 폴더
    (`제목 변경 알림까지_2026-10-16` 같은 것)는 모양이 달라 건드리지 않는다.
    """
    mine = sorted(
        (p for p in root.iterdir()
         if p.is_dir() and not p.is_symlink() and _NAME_RE.match(p.name)),
        key=lambda p: p.name,
    )
    dropped = []
    for path in mine[:-DB_BACKUP_KEEP] if DB_BACKUP_KEEP > 0 else []:
        try:
            shutil.rmtree(path)
            dropped.append(path.name)
        except OSError:
            logger.warning("오래된 DB 백업을 지우지 못했습니다: %s", path.name, exc_info=True)
    return dropped


def _run(root: Path, today: str, now: datetime) -> None:
    dest = root / f"{_PREFIX}{now.strftime('%Y-%m-%d_%H%M')}"
    try:
        result = store_pg.download_to_files(dest)
    except Exception:
        # **반쯤 받은 폴더는 지운다** — 남겨 두면 「오늘 건 이미 있다」로 읽혀 다시
        # 받지 않는데, 그 안은 성한 백업이 아니다.
        shutil.rmtree(dest, ignore_errors=True)
        count, _ = _attempts.get(today, (0, None))
        _attempts[today] = (count + 1, now)
        logger.exception(
            "DB 백업에 실패했습니다(%d/%d) — 다음 tick에 다시 해 봅니다", count + 1, _MAX_ATTEMPTS
        )
        return
    size = sum(p.stat().st_size for p in dest.rglob("*") if p.is_file())
    dropped = _prune(root)
    logger.info(
        "DB 백업 — %s에 %d개 %.1fMB%s",
        dest.name, len(result["downloaded"]), size / 1024 / 1024,
        f" (오래된 백업 {len(dropped)}개 정리)" if dropped else "",
    )


def tick(now: Optional[datetime] = None, *, background: bool = True) -> None:
    """스케줄러 tick마다 불린다 — 오늘 백업이 없으면 뒤에서 한 번 받고 즉시 돌아온다.

    **받는 일은 스레드로 둔다** — 32MB·700줄을 네트워크로 받는 동안 수집·자동발송이
    멈추면 안 된다(홈 부정 추정과 같은 방식).
    """
    now = now or datetime.now()
    if not store_pg.is_enabled():
        return                      # 파일로 쓰는 상태 — data/ 폴더가 곧 원본이다
    root = backup_root()
    if root is None:
        return
    today = now.strftime("%Y-%m-%d")
    count, _ = _attempts.get(today, (0, None))
    if count >= _MAX_ATTEMPTS:
        return
    for stale in [day for day in _attempts if day != today]:
        del _attempts[stale]
    if _todays_backup(root, today) is not None:
        return
    if not _lock.acquire(blocking=False):
        return                      # 앞 tick이 아직 받고 있다

    def _go():
        try:
            _run(root, today, now)
        finally:
            _lock.release()

    if background:
        threading.Thread(target=_go, name="db-backup", daemon=True).start()
    else:
        _go()
