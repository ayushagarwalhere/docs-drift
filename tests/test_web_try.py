"""Try it: the /try page and the /api/try/check sandbox boundary."""

import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from docs_drift.web.app import create_app
from docs_drift.web.settings import Settings
from docs_drift.web.try_routes import MAX_TRY_TEXT_BYTES, SAMPLE_CODE, SAMPLE_README

ORIGIN = {"origin": "http://127.0.0.1:8080"}


def client_for(tmp_path: Path, **settings: Any) -> TestClient:
    return TestClient(create_app(Settings(data_dir=tmp_path / "data", **settings)))


def check(client: TestClient, code: str, readme: str = SAMPLE_README, **extra: Any) -> Any:
    body = {"code": code, "readme": readme, "previous_code": SAMPLE_CODE, **extra}
    return client.post("/api/try/check", json=body, headers=ORIGIN)


def drift(body: dict[str, Any]) -> list[dict[str, Any]]:
    return [finding for finding in body["findings"] if finding["is_drift"]]


def test_page_renders_both_files_and_three_steps(tmp_path: Path) -> None:
    with client_for(tmp_path) as client:
        page = client.get("/try")
        assert page.status_code == 200
        html = page.text
        assert "app.py" in html and "README.md" in html and "/static/try.js" in html
        assert 'data-scenario="rename"' in html and 'data-repo="https://github.com/Textualize/rich-cli"' in html
        assert 'href="/write"' in html and "<script>" not in html
        assert 'href="/try"' in client.get("/").text


def test_sample_matches_and_a_renamed_flag_gets_a_proven_fix(tmp_path: Path) -> None:
    with client_for(tmp_path) as client:
        clean = check(client, SAMPLE_CODE).json()
        assert clean["drift"] == 0 and clean["claims"] >= 5
        assert {f["claim"] for f in clean["findings"]} >= {"--listen", "--workers", "SHIPIT_TOKEN"}

        renamed = check(client, SAMPLE_CODE.replace('"--listen"', '"--bind"')).json()
        rows = drift(renamed)
        flag = next(f for f in rows if f["claim"] == "--listen")
        assert flag["verdict"] == "renamed" and flag["suggestion"] == "--bind"
        assert flag["edit"] == {**flag["edit"], "old_text": "--listen", "new_text": "--bind", "certain": True}
        assert flag["introduced_by"]["summary"] == "Your edit to app.py"
        # The command's own "drop the flag" fix yields to the narrower rename.
        command = next(f for f in rows if f["kind"] == "code_command")
        assert command["edit"] is None and command["fixed_by"] == "--bind"

        line = SAMPLE_README.splitlines()[flag["edit"]["line"] - 1]
        fixed = SAMPLE_README.replace(line, line.replace("--listen", "--bind"))
        assert check(client, SAMPLE_CODE.replace('"--listen"', '"--bind"'), fixed).json()["drift"] == 0


def test_changed_default_and_dropped_env_var(tmp_path: Path) -> None:
    with client_for(tmp_path) as client:
        default = drift(check(client, SAMPLE_CODE.replace("default=4", "default=8")).json())
        assert [f["verdict"] for f in default] == ["default_changed"]
        assert default[0]["edit"]["new_text"] == "8" and default[0]["edit"]["certain"]

        dropped = SAMPLE_CODE.replace('    token = os.environ.get("SHIPIT_TOKEN")\n', "")
        dropped = dropped.replace(
            '    if args.command == "serve" and not token:\n        parser.error("Set SHIPIT_TOKEN first.")\n', ""
        )
        rows = drift(check(client, dropped).json())
        assert [(f["claim"], f["verdict"]) for f in rows] == [("SHIPIT_TOKEN", "missing")]


def test_code_is_parsed_never_executed(tmp_path: Path) -> None:
    marker = tmp_path / "ran.txt"
    hostile = (
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n"
        "import argparse\np = argparse.ArgumentParser()\np.add_argument('--port')\n"
        f"if __name__ == '__main__':\n    Path({str(marker)!r}).write_text('main')\n"
    )
    with client_for(tmp_path) as client:
        body = check(client, hostile, "Use `--port`.\n").json()
        assert body["drift"] == 0
    assert not marker.exists()
    # Every request's sandbox directory is removed once its check ends.
    assert list((tmp_path / "data" / "try-trees").iterdir()) == []


def test_limits_origin_and_rate(tmp_path: Path) -> None:
    with client_for(tmp_path, write_rate_limit=3) as client:
        wide = check(client, "é" * (MAX_TRY_TEXT_BYTES // 2 + 1))
        assert wide.status_code == 413 and "20 KB" in wide.json()["detail"]
        assert check(client, "x" * (MAX_TRY_TEXT_BYTES + 1)).status_code == 422
        huge = client.post(
            "/api/try/check",
            content=json.dumps({"code": "x" * 200_000, "readme": ""}),
            headers={"content-type": "application/json", **ORIGIN},
        )
        assert huge.status_code == 413
        foreign = client.post(
            "/api/try/check", json={"code": "", "readme": ""}, headers={"origin": "https://evil.test"}
        )
        assert foreign.status_code == 403
        assert (
            client.post("/api/try/check", json={"code": "", "readme": "", "path": "/"}, headers=ORIGIN).status_code
            == 422
        )

    with client_for(tmp_path / "rate", write_rate_limit=3) as client:
        statuses = [check(client, SAMPLE_CODE).status_code for _ in range(4)]
        assert statuses == [200, 200, 200, 429]
        # /write keeps its own allowance.
        write = client.post("/api/write/check", json={"scan_id": "example-py-cli", "text": "`--host`"}, headers=ORIGIN)
        assert write.status_code == 200
