import json

import searchable_storage_led.eval_cli as eval_cli


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def test_eval_cli_posts_bounded_replay_request(monkeypatch, capsys) -> None:
    calls = []

    def fake_urlopen(req, timeout):
        calls.append((req, timeout, json.loads(req.data.decode("utf-8"))))
        return FakeResponse(
            {
                "ok": True,
                "run_model": True,
                "count": 1,
                "passed": 1,
                "ready": 0,
                "failed": 0,
                "errors": 0,
                "skipped": 0,
                "minimum_cases": 1,
                "evidence_count": 1,
                "gate_passed": True,
                "results": [
                    {
                        "id": 7,
                        "command": "put away M4 T-nuts",
                        "status": "passed",
                        "expected_tools": ["put_item"],
                        "actual_tools": ["put_item"],
                    }
                ],
            }
        )

    monkeypatch.setattr(eval_cli.request, "urlopen", fake_urlopen)

    code = eval_cli.main(
        [
            "--base-url",
            "http://magpie.local",
            "--limit",
            "3",
            "--kind",
            "confirmed",
            "--run-model",
        ]
    )

    assert code == 0
    req, timeout, payload = calls[0]
    assert req.full_url == "http://magpie.local/assistant/evals/replay"
    assert timeout == 120.0
    assert payload == {
        "limit": 3,
        "minimum_cases": 1,
        "required_scenarios": [],
        "kind": "confirmed",
        "run_model": True,
    }
    assert req.headers["Content-type"] == "application/json"
    assert "Authorization" not in req.headers
    assert "passed=1" in capsys.readouterr().out


def test_eval_cli_authenticates_hosted_workspace_without_cli_secrets(
    monkeypatch, capsys
) -> None:
    seen = []

    def fake_urlopen(req, timeout):
        seen.append(req)
        return FakeResponse(
            {
                "ok": True,
                "run_model": False,
                "count": 0,
                "passed": 0,
                "ready": 0,
                "failed": 0,
                "errors": 0,
                "skipped": 0,
                "minimum_cases": 1,
                "evidence_count": 0,
                "gate_passed": False,
                "results": [],
            }
        )

    monkeypatch.setattr(eval_cli.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("MAGPIE_AUTH_TOKEN", "human-access-token")
    monkeypatch.setenv("MAGPIE_WORKSPACE_ID", "workspace-1")
    monkeypatch.setenv("MAGPIE_CF_ACCESS_CLIENT_ID", "machine-id.access")
    monkeypatch.setenv("MAGPIE_CF_ACCESS_CLIENT_SECRET", "machine-secret")

    code = eval_cli.main(["--base-url", "https://magpie-edge.example.com"])

    assert code == 1
    assert len(seen) == 1
    assert seen[0].headers["Authorization"] == "Bearer human-access-token"
    assert seen[0].headers["X-magpie-workspace"] == "workspace-1"
    assert seen[0].headers["Cf-access-client-id"] == "machine-id.access"
    assert seen[0].headers["Cf-access-client-secret"] == "machine-secret"
    assert "human-access-token" not in capsys.readouterr().out


def test_eval_cli_rejects_partial_cloudflare_credentials(monkeypatch, capsys) -> None:
    monkeypatch.setenv("MAGPIE_CF_ACCESS_CLIENT_ID", "machine-id.access")

    code = eval_cli.main([])

    assert code == 2
    assert "both MAGPIE_CF_ACCESS" in capsys.readouterr().err


def test_eval_cli_rejects_authentication_over_remote_plain_http(
    monkeypatch, capsys
) -> None:
    monkeypatch.setenv("MAGPIE_AUTH_TOKEN", "human-access-token")

    code = eval_cli.main(["--base-url", "http://magpie.example.com"])

    assert code == 2
    assert "must use HTTPS" in capsys.readouterr().err


def test_eval_cli_returns_failure_when_replay_fails(monkeypatch, capsys) -> None:
    def fake_urlopen(req, timeout):
        return FakeResponse(
            {
                "ok": True,
                "run_model": True,
                "count": 1,
                "passed": 0,
                "ready": 0,
                "failed": 1,
                "errors": 0,
                "skipped": 0,
                "results": [
                    {
                        "id": 8,
                        "command": "put away solder wick",
                        "status": "failed",
                        "expected_tools": ["put_item"],
                        "actual_tools": ["search_items"],
                    }
                ],
            }
        )

    monkeypatch.setattr(eval_cli.request, "urlopen", fake_urlopen)

    code = eval_cli.main(["--run-model"])

    assert code == 1
    out = capsys.readouterr().out
    assert "failed=1" in out
    assert "expected=[put_item] actual=[search_items]" in out


def test_eval_cli_json_output(monkeypatch, capsys) -> None:
    def fake_urlopen(req, timeout):
        return FakeResponse(
            {
                "ok": True,
                "run_model": False,
                "count": 0,
                "passed": 0,
                "ready": 0,
                "failed": 0,
                "errors": 0,
                "skipped": 0,
                "minimum_cases": 1,
                "evidence_count": 0,
                "gate_passed": False,
                "results": [],
            }
        )

    monkeypatch.setattr(eval_cli.request, "urlopen", fake_urlopen)

    code = eval_cli.main(["--json"])

    assert code == 1
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_eval_cli_fails_when_live_replay_does_not_meet_evidence_floor(
    monkeypatch, capsys
) -> None:
    def fake_urlopen(req, timeout):
        return FakeResponse(
            {
                "ok": True,
                "run_model": True,
                "minimum_cases": 2,
                "evidence_count": 1,
                "count": 2,
                "passed": 1,
                "ready": 0,
                "failed": 0,
                "errors": 0,
                "skipped": 1,
                "gate_passed": False,
                "results": [
                    {
                        "id": 1,
                        "command": "put away solder wick",
                        "status": "passed",
                        "scenarios": ["putaway"],
                    },
                    {
                        "id": 2,
                        "command": "sort this photo",
                        "status": "skipped",
                        "reason": "missing replay photos",
                        "scenarios": ["putaway", "photo"],
                    },
                ],
            }
        )

    monkeypatch.setattr(eval_cli.request, "urlopen", fake_urlopen)

    code = eval_cli.main(
        [
            "--run-model",
            "--min-cases",
            "2",
            "--require-scenario",
            "putaway",
            "--require-scenario",
            "photo",
        ]
    )

    assert code == 1
    out = capsys.readouterr().out
    assert "Evidence floor not met: 1 evaluated case(s); minimum required is 2" in out
    assert "Missing required scenarios: photo." in out


def test_eval_cli_rejects_impossible_minimum_before_request(monkeypatch, capsys) -> None:
    called = False

    def fake_urlopen(req, timeout):
        nonlocal called
        called = True
        raise AssertionError("request should not be sent")

    monkeypatch.setattr(eval_cli.request, "urlopen", fake_urlopen)

    code = eval_cli.main(["--min-cases", "26"])

    assert code == 2
    assert called is False
    assert "between 1 and 25" in capsys.readouterr().err
