# Agent rules for this repo

Read these first:
- Fleet rules and PR scope: [jtalborough/agents `shared/rules.md`](https://github.com/jtalborough/agents/blob/main/shared/rules.md)
- Communication baseline: [jtalborough/agents `shared/communication.md`](https://github.com/jtalborough/agents/blob/main/shared/communication.md)

## PR scope (from shared/rules.md)
- Before coding, state your assumptions in the PR description.
- Build the simplest change that works. Touch no unrelated files.
- When behavior changes, write a high-level integration test first (for example, an API test in `tests/test_api.py` through FastAPI's `TestClient`).
- Make one logical commit per step.
- Leads reject a PR that breaks these rules.

## Repo facts
- Python 3.11+. Package: `src/searchable_storage_led/`. Tests: `tests/` (pytest). Web UI: `src/searchable_storage_led/static/`.
- Set up and run tests (same as CI, `.github/workflows/tests.yml`):
  ```bash
  python3 -m venv .venv && .venv/bin/pip install -e '.[test]' webrtcvad-wheels vosk
  .venv/bin/python -m pytest
  ```
- Run one file while you work: `.venv/bin/python -m pytest tests/test_api.py -q`.
- Supabase schema changes go in `supabase/migrations/`; the `Check Supabase schema` workflow applies the full history on PRs.
- `main` is protected: open a PR, CI must pass, 1 approving review. Never push to `main`. Never merge your own PR.
- No secrets in this repo, ever. It is public. Use reserved example values (`192.0.2.x`, `example.com`) in docs and tests.

## Do not touch
- Deploy and device workflows: `deploy-hosted.yml`, `deploy-pi.yml`, `pi-diagnostics.yml`, `pi-voice-test.yml`, `publish-container.yml`. They run on self-hosted runners (the Pi and the central machine) or publish images. Don't edit, dispatch or re-run them.
- The hosted deployment and its cluster. A merge to `main` can deploy; the lead decides when to merge.
