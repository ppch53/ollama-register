# Changelog

## [2.0.0] - 2026-05-11

### Added

- **Puter v2 registration** (`puter_register_v2.py`) - Camoufox-based stealth registration
  - 11-state state machine with full transition audit
  - Behavioral simulation: burst typing, Bezier mouse movement, random scrolling
  - Phone verification detection and soft-landing skip
  - Turnstile handling (auto-solve, checkbox click, CapSolver fallback)
  - 24h quarantine with delayed re-audit
  - 72h optional second recheck
  - Circuit breaker (auto-stop on repeated failures)
  - Full audit trail (24-field JSONL records)
- **Preflight checker** (`src/preflight.py`) - environment validation before registration
- **Profile manager** (`src/profile_manager.py`) - isolated Firefox profiles per account
- **Sticky proxy** (`src/sticky_proxy.py`) - session-stable proxy with IP blacklist
- **Fingerprint generator** (`src/fingerprint_gen.py`) - BrowserForge fingerprints with country consistency
- **Scheduler** (`src/scheduler.py`) - UTC time-window rate limiting (max 5/day, 30min gap)
- **Mailbox provider pool** (`src/mailbox_provider.py`) - health scoring, cooldown, rotation
- **Username generator** (`src/username_gen.py`) - 4 pattern modes, global uniqueness
- **Restore script** (`restore_puter_v2.py`) - rebuild state from JSONL logs
- Open-source scaffolding: README, LICENSE (MIT), .gitignore, pyproject.toml, CONTRIBUTING, CHANGELOG

### Changed

- `requirements.txt` updated with `camoufox>=0.4,<1`

## [1.0.0] - 2026-04-01

### Added

- Ollama/WorkOS pure HTTP registration flow
- Playwright-based browser registration
- API gateway with key pool (`pool_gateway.py`)
- OpenAI-to-Puter adapter (`puter_adapter.py`)
- Account store with filelock and atomic writes
- Structured JSON logging with sensitive field redaction
