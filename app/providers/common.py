from __future__ import annotations


class ProviderError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool = False, status_code: int | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.status_code = status_code


class AttemptBudgetExceeded(ProviderError):
    def __init__(self, provider: str):
        super().__init__("BUDGET_EXHAUSTED", f"The {provider} request budget is exhausted.")

