"""Tool package (docs/10-tool-calling.md): the `@tool` decorator and
allowlist registry, the tool-result error-shape helpers, and the 3
Phase-2 tools (`get_wallet_balance`, `get_payment_status`,
`request_limit_increase`).

Importing this package is what populates the registry — each tool
module registers itself as an import side effect, so `import app.tools`
(once, anywhere in the process) is enough for `app.tools.registry.
registry` to hold all 3 Phase-2 tools. Mirrors `app.data.repositories`'
own re-export convention so callers write
`from app.tools import get_wallet_balance` instead of reaching into
individual modules.
"""

from __future__ import annotations

from app.tools import errors
from app.tools.get_payment_status import (
    GetPaymentStatusIn,
    GetPaymentStatusOut,
    LimitContextOut,
    get_payment_status,
)
from app.tools.get_wallet_balance import (
    GetWalletBalanceIn,
    GetWalletBalanceOut,
    get_wallet_balance,
)
from app.tools.registry import Registry, configure, registry, tool
from app.tools.request_limit_increase import (
    RequestLimitIncreaseIn,
    RequestLimitIncreaseOut,
    request_limit_increase,
)

__all__ = [
    "GetPaymentStatusIn",
    "GetPaymentStatusOut",
    "GetWalletBalanceIn",
    "GetWalletBalanceOut",
    "LimitContextOut",
    "Registry",
    "RequestLimitIncreaseIn",
    "RequestLimitIncreaseOut",
    "configure",
    "errors",
    "get_payment_status",
    "get_wallet_balance",
    "registry",
    "request_limit_increase",
    "tool",
]
