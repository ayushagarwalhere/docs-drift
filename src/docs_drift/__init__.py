"""Find drift between documentation and statically inspected code."""

from importlib.metadata import version

__version__ = version("docs-drift")

__all__ = ["__version__"]
