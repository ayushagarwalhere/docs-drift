"""Allow ``python -m docs_drift``, equivalent to the ``docs-drift`` console script."""

from docs_drift.cli import app


def main() -> None:
    app(prog_name="docs-drift")


if __name__ == "__main__":
    main()
