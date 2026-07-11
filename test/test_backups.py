import logging
from typing import Callable

from backend.utils.backup_strategies import format_backups, get_stale_backups

logger = logging.getLogger(__name__)

def update_backups(backups: list[int], t: int, n: int, d: int = 1, base: int = 2, key: Callable[[int], int] = lambda x: x) -> list[int]:
    stale_backups = get_stale_backups(backups, t, n, d, base, key=key)
    return [backup for backup in backups if backup not in stale_backups] + [t]

if __name__ == "__main__":
    backups: list[int] = []
    n = 5
    d = 24
    print("       :", format_backups(backups, 0, n, d))
    for t in range(0, d * 50, d):
        backups = update_backups(backups, t, n, d)
        print(f"t={t:05d}:", format_backups(backups, t, n, d))