"""Self-contained, escaped HTML report and graph data."""

import json
import re
from collections import Counter
from itertools import pairwise
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from docs_drift import __version__
from docs_drift.models import ScanResult
from docs_drift.report import RenderOptions
from docs_drift.report.common import RULES, health, ordered

WEB_FINDINGS = 40


def github_remote(url: str | None) -> str | None:
    match = re.fullmatch(
        r"(?:git@github\.com:|https://github\.com/|ssh://git@github\.com/)([\w.-]+/[\w.-]+?)(?:\.git)?/?", url or ""
    )
    return "https://github.com/" + match[1] if match else None


def graph_data(result: ScanResult, max_nodes: int = 1000) -> dict[str, Any]:
    findings = ordered(result)
    visible = [f for f in findings if f.verdict != "ok"]
    nodes: dict[str, dict[str, dict[str, str]]] = {}
    edges: list[dict[str, dict[str, str]]] = []
    truncated = False
    for f in visible:
        if len(nodes) + 5 > max_nodes:
            truncated = True
            break
        c = f.claim
        route = [(c.section_id, c.doc_path, "section"), (c.claim_id, c.value, "claim")]
        if f.matched_symbol:
            s = f.matched_symbol
            route.extend([(s.symbol_id, s.name, "symbol"), ("file:" + s.file, s.file, "file")])
        else:
            route.append(("old:" + c.claim_id, c.value + " (old)", "symbol"))
        if f.introduced_by:
            route.append((f.introduced_by.sha, f.introduced_by.short_sha, "commit"))
        for ident, label, kind in route:
            nodes[ident] = {"data": {"id": ident, "label": label[:120], "kind": kind}}
        for a, b in pairwise(route):
            edges.append(
                {
                    "data": {
                        "id": f"{c.claim_id}:{len(edges)}",
                        "source": a[0],
                        "target": b[0],
                        "claim": c.claim_id,
                        "verdict": f.verdict.value,
                    }
                }
            )
    return {"nodes": list(nodes.values()), "edges": edges, "truncated": truncated}


def render(result: ScanResult, options: RenderOptions) -> str:
    findings = ordered(result)
    visible = [f for f in findings if f.verdict != "ok"]
    web_truncated = bool(options.share_id and len(visible) > WEB_FINDINGS)
    if options.share_id:
        visible = [
            f.model_copy(
                update={
                    "doc_text": (f.doc_text or "")[:1000],
                    "evidence": [e.model_copy(update={"text": e.text[:1000]}) for e in f.evidence[:3]],
                }
            )
            for f in visible[:40]
        ]
    data = graph_data(result, max_nodes=200 if options.share_id else 1000)
    payload = (
        json.dumps(data, ensure_ascii=True).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    )
    counts = result.summary()["by_verdict"]
    sections = Counter(f.claim.doc_path for f in visible if f.is_drift)
    sources = Counter(f.matched_symbol.file for f in visible if f.is_drift and f.matched_symbol)
    timeline = Counter(f.introduced_by.date.strftime("%Y-%m") for f in visible if f.introduced_by)
    bars = [(date, count, 220 * count / max(timeline.values(), default=1)) for date, count in sorted(timeline.items())]
    env = Environment(loader=FileSystemLoader(Path(__file__).parent), autoescape=select_autoescape(["html"]))
    vendor = Path(__file__).parent / "vendor" / "cytoscape.min.js"
    return env.get_template("report.html").render(
        result=result,
        findings=visible,
        counts=counts,
        health=health(findings),
        rules=RULES,
        options=options,
        version=__version__,
        remote=github_remote(result.remote_url),
        payload=payload,
        cytoscape=vendor.read_text(encoding="utf-8") if vendor.exists() else "",
        sections=sections.most_common(8),
        sources=sources.most_common(8),
        bars=bars,
        package_health=[(p.name, health([f for f in findings if f.package == p.name])) for p in result.packages],
        model_count=sum(f.decided_by == "llm" for f in findings),
        web_truncated=web_truncated,
        replayed=all(f.replayed for f in findings if f.decided_by == "llm"),
    )
