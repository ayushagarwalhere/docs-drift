# Python API

```python
from pathlib import Path
from docs_drift.config import Config
from docs_drift.scanning import run_scan
from docs_drift.report import render

result = run_scan(Path("."), Config())
json_report = render(result, "json")
html_report = render(result, "html")
```

`run_scan` returns the versioned Pydantic `ScanResult`. The caller decides whether
to write it. `result.timings` contains measured stage seconds; canonical JSON
omits nondeterministic timing values. Set `history=False` only when intentionally
running a static-only diagnostic. The public benchmark enables history.

Pass `docs_repo` for a separate documentation root and `package` for a workspace
package. A stage callback receives progress names and integer counts. No model
call occurs inside `run_scan`; optional CLI model assessment is a separate layer.
