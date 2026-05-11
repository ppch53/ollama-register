# Contributing

## Development Setup

```bash
git clone https://github.com/YOUR_USERNAME/ollama-register.git
cd ollama-register
python -m venv .venv
.venv\Scripts\activate  # Windows
pip install -e ".[dev]"
```

## Code Style

- Python 3.11+ with type hints
- Format and lint with [ruff](https://docs.astral.sh/ruff/):
  ```bash
  ruff check --fix .
  ruff format .
  ```
- Keep lines under 100 characters

## Running Tests

```bash
pytest
```

## Pull Requests

1. Fork the repo and create a branch from `main`
2. Add tests for new functionality
3. Ensure `ruff check` and `pytest` pass
4. Submit a PR with a clear description of changes

## Architecture

```
src/                  # Reusable modules
  preflight.py        # Environment pre-checks
  profile_manager.py  # Isolated browser profiles
  sticky_proxy.py     # Session-stable proxy management
  fingerprint_gen.py  # Browser fingerprint generation
  scheduler.py        # Rate-limited scheduling
  mailbox_provider.py # Email provider abstraction
  username_gen.py     # Natural username generation
  utils.py            # Shared utilities
  ...
puter_register_v2.py  # Puter registration orchestrator (v2)
ollama_register.py    # Ollama registration orchestrator
main.py               # CLI entry point
```

## Commit Messages

Use concise, descriptive commit messages:

- `fix: resolve event loop blocking in scheduler.wait_for_slot`
- `feat: add 72h optional quarantine recheck`
- `refactor: extract proxy config to external JSON`
