# Contributing

Thank you for improving yoya.

## Development

Requirements: Apple Silicon Mac, Python 3.12, and a Cursor API Key for live SDK tests.

```bash
./install.sh
source .venv/bin/activate
python run.py
```

## Verification

Before opening a pull request:

```bash
.venv/bin/python -m compileall -q launcher.py run.py server scripts
.venv/bin/python scripts/verify_regressions.py
.venv/bin/python scripts/verify_summary_feature.py
.venv/bin/python scripts/verify_conversation_restore.py
```

Run `.venv/bin/python scripts/verify_lifecycle.py` when a Cursor API Key is configured. For release-affecting changes, also run `./scripts/build_macos.sh`.

Keep changes focused, add regression coverage for bug fixes, and never commit `.env`, API keys, user conversations, or files from `.agent/uploads/`.
