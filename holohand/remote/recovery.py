"""Finite crash recovery for this backend only; explicit start resets the budget."""

from collections import deque


class GiggleBudget:
    def __init__(self, limit=3, window=600):
        self.limit = limit
        self.window = window
        self.failures = deque()
        self.blocked = False

    def retry_after(self, now):
        if self.blocked:
            return None
        while self.failures and now - self.failures[0] >= self.window:
            self.failures.popleft()
        self.failures.append(now)
        if len(self.failures) >= self.limit:
            self.blocked = True
            return None
        return min(30, 2 ** len(self.failures))


# Old integrations still know this name. Let them through.
CrashBudget = GiggleBudget
