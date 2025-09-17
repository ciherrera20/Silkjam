import math
from typing import Iterable, TypeVar

T = TypeVar("T")

def to_nearest(x, d=1):
    return math.floor(x / d + 1) * d

def format_backups(backups, t, n, d=1, base=2, key=lambda b: b):
    """
    b is backup, n is new, s is save, x is expire

    t = 0
    [_|__|____|________|________________]
    """
    bins = ["_" * (base ** i) for i in range(n)]
    num_out_of_bounds = 0
    for backup in backups:
        ts = min(key(backup), t)
        i = to_nearest(t - ts, d) // d
        j = math.floor(math.log(i, 2))
        k = i - (base ** j)
        if j < len(bins):
            c = bins[j][k]
            if c == '_':
                c = '1'
            elif c == '9':
                c = '*'
            else:
                c = str(int(c) + 1)
            bins[j] = bins[j][:k] + c + bins[j][k+1:]
        else:
            num_out_of_bounds += 1
    s = f"[{'|'.join(bins)}"
    if num_out_of_bounds == 0:
        s += "|..._]"
    elif num_out_of_bounds <= 9:
        s += f"|...{num_out_of_bounds}]"
    else:
        s += f"|...*]"
    return s

def get_stale_backups(
        backups: Iterable[T],
        t: int,
        n: int,
        d: int=1,
        base: int=2,
        key=lambda b: b
    ) -> set[T]:
    backups = sorted(backups, key=key)  # Sort oldest to newest

    # Determine the cutoff for backups and the number of backups to delete
    cutoff = to_nearest(t - ((base ** n) - 1) * d, d)
    num_stale = max(0, len(backups) - n + 1)
    stale_backups = set()

    if len(stale_backups) == num_stale:
        return stale_backups

    # Delete first from the backups past the cutoff oldest to newest
    for backup in backups:
        if len(stale_backups) < num_stale:
            ts = min(key(backup), t)
            if ts < cutoff:
                stale_backups.add(backup)
            else:
                break
        else:
            return stale_backups

    # Then, go through each bin from newest to oldest and delete backups newest to oldest
    counts = [1] + [0] * (n - 1)
    for backup in backups:
        ts = min(key(backup), t)
        if ts >= cutoff:
            i = to_nearest(t - ts, d) // d
            j = math.floor(math.log(i, 2))
            counts[j] += 1

    for backup in reversed(backups):
        if len(stale_backups) < num_stale:
            ts = min(key(backup), t)
            i = to_nearest(t - ts, d) // d
            j = math.floor(math.log(i, 2))
            if ts >= cutoff and counts[j] > 1:
                stale_backups.add(backup)
                counts[j] -= 1
        else:
            break
    return stale_backups