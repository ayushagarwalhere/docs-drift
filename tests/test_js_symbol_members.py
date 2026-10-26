"""JS symbol coverage: object-literal methods, class methods and re-exports (datasette plugin JS sweep)."""

from pathlib import Path

from docs_drift.config import Config
from docs_drift.index import build_index
from docs_drift.models import SymbolKind
from docs_drift.scanning import run_scan

EDIT_TOOLS = (
    "function makeField(control) {\n"
    "  var field = {\n"
    "    getValue: function () { return control.value; },\n"
    "    setValue: function (value) { control.value = value; },\n"
    "    markClean() { control.dirty = false; },\n"
    "    clearValue: () => null,\n"
    "    label: 'not a method',\n"
    "  };\n"
    "  return field;\n"
    "}\n"
    "class DatasetteModal {\n"
    "  constructor() {}\n"
    "  show() {}\n"
    "}\n"
    "DatasetteModal.prototype.requestClose = function () {};\n"
)


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    for name, content in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return tmp_path


def test_object_literal_and_class_methods_are_indexed(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {"static/edit-tools.js": EDIT_TOOLS})
    symbols = build_index(repo, Config(), no_cache=True).symbols
    methods = {s.name: s.qualified_name for s in symbols if s.kind == SymbolKind.method}
    assert methods["setValue"] == "static.edit-tools.field.setValue"
    assert {"getValue", "markClean", "clearValue", "show", "requestClose"} <= set(methods)
    assert methods["show"] == "static.edit-tools.DatasetteModal.show"
    assert methods["requestClose"] == "static.edit-tools.DatasetteModal.requestClose"
    assert "label" not in methods and "constructor" not in methods


def test_documented_plugin_methods_resolve_and_unknown_methods_remain(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path,
        {
            "static/edit-tools.js": EDIT_TOOLS,
            "docs/javascript_plugins.md": (
                "# Field API\n\n"
                "Call the `setValue()` method to replace the value and the `markClean()` method after saving.\n\n"
                "The `resetEverything()` method restores the original value.\n"
            ),
        },
    )
    result = run_scan(repo, Config(), history=False, no_cache=True)
    verdicts = {f.claim.value: f.verdict.value for f in result.findings if f.claim.kind.value == "symbol"}
    assert verdicts.get("setValue") == "ok", verdicts
    assert verdicts.get("markClean") == "ok", verdicts
    assert verdicts.get("resetEverything") == "missing", verdicts


def test_re_exports_and_exported_values_are_public_symbols(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path,
        {
            "src/index.ts": (
                "export { createServer, handler as requestHandler } from './server';\n"
                "export type { Options } from './types';\n"
                "export const defaults = { port: 3000 };\n"
            ),
        },
    )
    symbols = build_index(repo, Config(), no_cache=True).symbols
    exported = {s.name for s in symbols if s.exported}
    assert {"createServer", "requestHandler", "Options", "defaults"} <= exported
