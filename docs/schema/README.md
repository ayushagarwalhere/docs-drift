# Report contracts and third-party notices

`scan-result.v1.json` is generated from `ScanResult.model_json_schema()`; the test
suite checks it for exact agreement with the model and validates every fixture.
Finding ordering is doc path, line, claim ID. Incompatible contract changes require
a new schema version and a changelog entry.

`sarif-schema-2.1.0.json` is an unmodified copy of the official OASIS SARIF 2.1.0
schema incorporating Errata 01, fetched from:

https://raw.githubusercontent.com/oasis-tcs/sarif-spec/main/sarif-2.1/schema/sarif-schema-2.1.0.json

SHA-256: `c3b4bb2d6093897483348925aaa73af03b3e3f4bd4ca38cef26dcb4212a2682e`.

This artifact is governed by OASIS terms, not this project's MIT license.
`SARIF-LICENSE.md` preserves the upstream repository license terms and links to
the OASIS IPR policy. `SARIF-NOTICES.txt` preserves the specification's copyright,
copying permission, conditions, and disclaimers. Its copying permission allows
implementation aids to be distributed with those notices retained; the schema
is not modified. Tests use jsonschema's Draft4Validator and do not fetch schemas.

Cytoscape.js 3.30.4 is separately vendored in `src/docs_drift/report/vendor/`
under MIT; `CYTOSCAPE-LICENSE` preserves its notice. The minified file was fetched
from https://cdnjs.cloudflare.com/ajax/libs/cytoscape/3.30.4/cytoscape.min.js.
SHA-256: `1bb5340e549511e111b31e5684872c949ad33d40ea5dba0ad8e7d90c62c7b3b9`.
