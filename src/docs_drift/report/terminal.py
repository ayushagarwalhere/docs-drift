"""Wrapping terminal report with optional ANSI styles and OSC 8 file links."""

from datetime import UTC, datetime
from io import StringIO

from rich.console import Console
from rich.text import Text

from docs_drift.llm.pricing import format_usage
from docs_drift.models import ScanResult
from docs_drift.report import RenderOptions
from docs_drift.report.common import RULES, health, ordered


def render(result: ScanResult, options: RenderOptions) -> str:
    stream = StringIO()
    console = Console(
        file=stream,
        force_terminal=options.color,
        color_system="standard" if options.color else None,
        width=max(20, options.width),
        highlight=False,
        markup=False,
    )
    console.print(
        f"{result.repo_path.name}  ·  {result.head_sha[:8]}  ·  {len(result.docs)} docs scanned  ·  "
        f"{len(result.claims)} claims checked  ·  {sum(result.timings.values()):.2f}s",
        style="bold",
    )
    previous = None
    styles = {
        "renamed": "yellow",
        "missing": "red",
        "default_changed": "magenta",
        "invalid_command": "red",
        "unverifiable": "dim",
        "ok": "green",
    }
    for f in ordered(result):
        if (not options.show_all and f.verdict == "ok") or (
            not options.show_low_confidence and f.confidence < result.config_used.min_confidence
        ):
            continue
        group = (f.package, f.claim.doc_path)
        if group != previous:
            console.print()
            console.print(
                f"{f.package + ' / ' if len(result.packages) > 1 and f.package else ''}{f.claim.doc_path}", style="bold"
            )
            previous = group
        line = Text(f"  {RULES[f.verdict][1]} ", style=styles[f.verdict])
        location = f"{f.claim.doc_path}:{f.claim.line}"
        root = result.docs_repo_path or result.repo_path
        link = (root / f.claim.doc_path).as_uri() + f"#L{f.claim.line}" if root.is_absolute() else None
        line.append(location, style=f"link {link}" if options.hyperlinks and link else "")
        console.print(line)
        doc_text = f.doc_text or f.claim.raw_text
        raw = Text("    " + doc_text)
        start = doc_text.find(f.claim.value)
        if start >= 0:
            raw.stylize("bold underline", start + 4, start + 4 + len(f.claim.value))
        console.print(raw)
        if start >= 0:
            console.print(Text("    " + " " * start + "^" * len(f.claim.value), style=styles[f.verdict]))
        console.print(f"    → {f.suggestion or f.reasons[0]}")
        if f.introduced_by:
            commit = f.introduced_by
            days = max(0, (datetime.now(UTC) - commit.date).days)
            console.print(f"    Introduced by {commit.short_sha} · {commit.summary} · {days} days ago", style="dim")
        console.print(f"    {f.claim.claim_id}", style="dim")
        if options.verbose:
            console.print(f"    Confidence {f.confidence:.0%} · {f.decided_by}")
    console.print()
    console.print(" · ".join(f"{RULES[key][1]} {value}" for key, value in result.summary()["by_verdict"].items()))
    console.print(
        f"Health {health(result.findings)}/100 · suppressed {len(result.suppressed)} · baselined {result.baselined}"
    )
    if result.llm_usage:
        console.print(format_usage(result.llm_usage))
    model_findings = [f for f in result.findings if f.decided_by == "llm"]
    if model_findings:
        mode = "replayed" if all(f.replayed for f in model_findings) else "live"
        console.print(f"Model verdicts: {len(model_findings)} ({mode})")
    for warning in result.warnings:
        console.print(f"Warning: {warning}", style="yellow")
    if options.verbose:
        console.print("Timings: " + ", ".join(f"{k}={v:.4f}s" for k, v in result.timings.items()))
    console.print("Run docs-drift fix to see patches, or docs-drift explain <id> for evidence.")
    text = stream.getvalue()
    return text if options.color else text.replace("\x1b", "\\x1b")
