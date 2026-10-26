"""Find drift between documentation and statically inspected code.

The stable library surface is ``check()`` with its ``CheckResult`` and
``CheckSummary`` types. They are imported lazily so that ``import docs_drift``
(for example by the pytest plugin at startup) stays cheap.
"""

from importlib.metadata import version
from typing import TYPE_CHECKING

__version__ = version("docs-drift")

__all__ = ["CheckResult", "CheckSummary", "__version__", "check"]

if TYPE_CHECKING:
    from docs_drift.api import CheckResult, CheckSummary, check

_LAZY = frozenset({"CheckResult", "CheckSummary", "check"})


def __getattr__(name: str) -> object:
    if name in _LAZY:
        from docs_drift import api  # noqa: PLC0415 - deferred so importing the package stays light.

        return getattr(api, name)
    raise AttributeError(f"module 'docs_drift' has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted({*globals(), *_LAZY})
