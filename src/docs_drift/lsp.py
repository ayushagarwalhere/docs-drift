"""Small stdio language server; stdout is reserved for the LSP transport."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from docs_drift.config import Config
from docs_drift.errors import DocsDriftError
from docs_drift.models import Finding

try:
    from lsprotocol import types as t
    from pygls.lsp.server import LanguageServer
except ImportError:
    raise DocsDriftError("LSP support is not installed.", 'Install it with pip install "docs-drift[lsp]".') from None
from urllib.parse import urlparse

from docs_drift.fix.rules import plan_rule_fixes
from docs_drift.report.common import RULES, message
from docs_drift.watch import IncrementalScanner


def uri_path(uri: str) -> Path:
    from urllib.request import url2pathname  # noqa: PLC0415 - optional dependency or shared pipeline import.

    return Path(url2pathname(urlparse(uri).path)).resolve()


class DriftServer(LanguageServer):
    drift: IncrementalScanner
    drift_diagnostics: Callable[[str], list[t.Diagnostic]]
    drift_actions: Callable[[t.CodeActionParams], list[t.CodeAction]]
    drift_definition: Callable[[t.DefinitionParams], t.Location | None]


def create_server(repo: Path, config: Config) -> DriftServer:  # noqa: PLR0915 - LSP feature registration.
    server = DriftServer("docs-drift", "0.1.0")
    state = IncrementalScanner(repo, config)
    state.enrich([f for f in state.result.findings if f.is_drift], state.generation)
    server.drift = state

    def relative(uri: str) -> str | None:
        path = uri_path(uri)
        return path.relative_to(state.repo).as_posix() if path.is_relative_to(state.repo) else None

    def findings(uri: str) -> list[Finding]:
        path = relative(uri)
        return [f for f in state.result.findings if f.claim.doc_path == path]

    def span(f: Finding) -> t.Range:
        c = f.claim
        text = state.open_contents.get(c.doc_path)
        if text is None:
            try:
                text = (state.repo / c.doc_path).read_text(encoding="utf-8")
            except OSError:
                text = c.raw_text
        lines = text.splitlines()
        line = lines[c.line - 1] if c.line <= len(lines) else c.raw_text
        start = line.find(c.value, max(0, c.column - 1))
        if start < 0:
            start = max(0, c.column - 1)
        # LSP's default position encoding is UTF-16, not Python code points.
        first = len(line[:start].encode("utf-16-le")) // 2
        length = len(c.value.encode("utf-16-le")) // 2
        return t.Range(t.Position(c.line - 1, first), t.Position(c.line - 1, first + length))

    def diagnostics(uri: str) -> list[t.Diagnostic]:
        return [
            t.Diagnostic(
                range=span(f),
                message=message(f),
                source="docs-drift",
                code=RULES[f.verdict][0],
                severity=t.DiagnosticSeverity.Error
                if f.verdict == "invalid_command"
                else t.DiagnosticSeverity.Warning
                if f.is_drift
                else t.DiagnosticSeverity.Information,
            )
            for f in findings(uri)
            if f.verdict != "ok"
        ]

    def publish(uri: str) -> None:
        server.text_document_publish_diagnostics(t.PublishDiagnosticsParams(uri=uri, diagnostics=diagnostics(uri)))

    def update(uri: str) -> None:
        path = relative(uri)
        if path is None:
            return
        text = server.workspace.get_text_document(uri).source
        _, new = state.update([path], contents={path: text})
        if new:
            state.enrich(new, state.generation)
        publish(uri)

    @server.feature(t.TEXT_DOCUMENT_DID_OPEN)
    def did_open(params: t.DidOpenTextDocumentParams) -> None:
        update(params.text_document.uri)

    @server.feature(t.TEXT_DOCUMENT_DID_CHANGE)
    def did_change(params: t.DidChangeTextDocumentParams) -> None:
        update(params.text_document.uri)

    @server.feature(t.TEXT_DOCUMENT_DID_CLOSE)
    def did_close(params: t.DidCloseTextDocumentParams) -> None:
        uri = params.text_document.uri
        path = relative(uri)
        if path:
            state.open_contents.pop(path, None)
            state.update([path])
        server.text_document_publish_diagnostics(t.PublishDiagnosticsParams(uri=uri, diagnostics=[]))

    @server.feature(t.WORKSPACE_DID_CHANGE_WATCHED_FILES)
    def watched(params: t.DidChangeWatchedFilesParams) -> None:
        state.update([uri_path(e.uri) for e in params.changes])
        for path in state.open_contents:
            publish((state.repo / path).as_uri())

    @server.feature(t.TEXT_DOCUMENT_CODE_ACTION, t.CodeActionOptions(code_action_kinds=[t.CodeActionKind.QuickFix]))
    def actions(params: t.CodeActionParams) -> list[t.CodeAction]:
        uri = params.text_document.uri
        path = relative(uri)
        if path is None:
            return []
        plan = plan_rule_fixes(state.result, contents=state.open_contents)
        output = []
        lines = state.open_contents.get(path, "").splitlines()
        for edit in plan.edits:
            if edit.file != path or not (params.range.start.line <= edit.line - 1 <= params.range.end.line):
                continue
            line = lines[edit.line - 1] if edit.line <= len(lines) else ""
            first = len(line[: edit.column - 1].encode("utf-16-le")) // 2
            end = first + len(edit.old_text.encode("utf-16-le")) // 2
            change = t.TextEdit(
                t.Range(t.Position(edit.line - 1, first), t.Position(edit.line - 1, end)), edit.new_text
            )
            output.append(
                t.CodeAction(
                    title=f"Replace {edit.old_text} with {edit.new_text}",
                    kind=t.CodeActionKind.QuickFix,
                    edit=t.WorkspaceEdit(changes={uri: [change]}),
                    diagnostics=[d for d in diagnostics(uri) if d.range.start.line == edit.line - 1],
                )
            )
        return output

    def at(uri: str, position: t.Position) -> Finding | None:
        return next(
            (
                f
                for f in findings(uri)
                if span(f).start.line == position.line
                and span(f).start.character <= position.character <= span(f).end.character
            ),
            None,
        )

    @server.feature(t.TEXT_DOCUMENT_DEFINITION)
    def definition(params: t.DefinitionParams) -> t.Location | None:
        f = at(params.text_document.uri, params.position)
        if f is None or f.matched_symbol is None:
            return None
        symbol = f.matched_symbol
        return t.Location(
            (state.repo / symbol.file).as_uri(),
            t.Range(t.Position(symbol.line - 1, 0), t.Position(symbol.line - 1, len(symbol.name))),
        )

    @server.feature(t.TEXT_DOCUMENT_HOVER)
    def hover(params: t.HoverParams) -> t.Hover | None:
        f = at(params.text_document.uri, params.position)
        if f is None:
            return None
        text = message(f)
        if f.matched_symbol:
            s = f.matched_symbol
            text += f"\n\n[{s.name}]({(state.repo / s.file).as_uri()}#L{s.line})"
        return t.Hover(contents=t.MarkupContent(t.MarkupKind.Markdown, text), range=span(f))

    # Expose pure adapter functions for tests without starting a transport.
    server.drift_diagnostics = diagnostics
    server.drift_actions = actions
    server.drift_definition = definition
    return server


def start(repo: Path, config: Config) -> None:
    try:
        server = create_server(repo, config)
    except ImportError:
        from docs_drift.errors import DocsDriftError  # noqa: PLC0415 - optional dependency or shared pipeline import.

        raise DocsDriftError(
            "LSP support is not installed.", 'Install it with pip install "docs-drift[lsp]".'
        ) from None
    server.start_io()
