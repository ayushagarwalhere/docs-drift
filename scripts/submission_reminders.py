"""Print dated weekly usage-card upload reminders from the owner's actual challenge start."""

import argparse
from datetime import date, timedelta

END = date(2026, 10, 30)
TARGET = date(2026, 10, 29)


def reminders(start: date) -> list[tuple[date, date, date]]:
    """Cover every seven-day challenge week, including the final partial week."""
    if start > END:
        raise ValueError("Challenge start must be on or before 30 October 2026")
    rows = []
    while start <= END:
        end = min(start + timedelta(days=6), END)
        upload = min(end, TARGET) if start <= TARGET else end
        rows.append((start, end, upload))
        start = end + timedelta(days=1)
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    args = parser.parse_args()
    for start, end, upload in reminders(args.start):
        print(f"- [ ] {upload}: upload Insights card for {start} through {end}; save receipt.")


if __name__ == "__main__":
    main()
