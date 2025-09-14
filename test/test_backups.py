import asyncio
from contextlib import suppress
import logging
logger = logging.getLogger(__name__)


#
# Project imports
#
import os, sys, subprocess
ROOT = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True).stdout.decode("utf-8").strip()
sys.path.append(os.path.join(ROOT, "app", "orchestrator"))
from utils.backups import format_backups, get_stale_backups

def update_backups(backups, t, n, d=1, base=2, key=lambda x: x):
    stale_backups = get_stale_backups(backups, t, n, d, base, key)
    return [backup for backup in backups if backup not in stale_backups] + [t]

if __name__ == "__main__":
    backups = []
    n = 5
    d = 24
    print(f"       :", format_backups(backups, 0, n, d))
    for t in range(0, d * 50, d):
        backups = update_backups(backups, t, n, d)
        print(f"t={t:05d}:", format_backups(backups, t, n, d))