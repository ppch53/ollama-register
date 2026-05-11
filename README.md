# Ollama and Puter Registration Toolkit

This repository contains two registration projects that share some support code:

1. Ollama registration
2. Puter registration

Optional gateway scripts can expose the collected accounts through a local
OpenAI-compatible pool or inject that pool into an existing new-api instance.
new-api itself is not a project goal here; it is treated as an external open
source gateway that may or may not be present.

> **Disclaimer**: This project is for educational and research purposes. Users are responsible for compliance with the terms of service of any platform they interact with.

## Project A: Ollama Registration

The Ollama flow is a mostly HTTP-based registration path.

Main files:

- `main.py` - CLI entry point for Ollama registration
- `ollama_register.py` - WorkOS/Ollama registration orchestration
- `src/config.py` - environment-backed configuration
- `src/browser_flow.py` - Playwright fallback/browser flow
- `src/tempmail_client.py` - temporary mailbox integration
- `src/turnstile_client.py` - Turnstile solver integration
- `src/hero_sms_provider.py` - optional phone provider integration

Capabilities:

- TLS fingerprint impersonation through `curl_cffi`
- WorkOS Server Action protocol replay
- Turnstile handling
- Optional phone verification provider support
- File-based account persistence with sensitive fields kept out of git

Typical run:

```bash
python main.py
python main.py -n 10 -c 3
```

## Project B: Puter Registration

The Puter flow has a legacy/manual path and a newer Camoufox-based v2 path.
The v2 path is the primary implementation for continued work.

Main files:

- `puter_register_v2.py` - Puter v2 registration orchestrator
- `restore_puter_v2.py` - rebuild/export state from JSONL state logs
- `puter_register.py` - earlier Puter registration flow
- `confirm_pending.py`, `run_manual.py`, `outlook_inbox.py` - legacy/manual support tools
- `src/preflight.py` - environment validation before live registration
- `src/profile_manager.py` - isolated browser profiles per account
- `src/sticky_proxy.py` - session-stable proxy and IP blacklist management
- `src/fingerprint_gen.py` - BrowserForge/Camoufox fingerprint generation
- `src/scheduler.py` - time-window and daily-rate scheduling
- `src/mailbox_provider.py` - mailbox provider health and rotation
- `src/username_gen.py` - username generation and deduplication

Capabilities:

- Camoufox browser isolation for account creation
- Behavioral simulation for typing, mouse movement, scroll, and dwell timing
- Registration state machine with audit logs and crash recovery
- Phone prompt detection and soft skip path
- Turnstile handling with fallback attempts
- Quarantine, delayed audit, and circuit breaker controls

Typical run:

```bash
python puter_register_v2.py --dry-run

# Live registration requires an explicit environment gate.
PUTER_LIVE_REGISTRATION=1 python puter_register_v2.py --live -n 1

python puter_register_v2.py --quarantine-check
python puter_register_v2.py --circuit-breaker-reset
```

## Optional Gateway Integration

These scripts are integration utilities around the two registration projects;
they are not the core product:

- `puter_adapter.py` - OpenAI-compatible adapter for Puter tokens
- `pool_gateway.py` - local OpenAI-compatible pool over Puter and Ollama accounts
- `pool_cleaner.py`, `restore_cleanup.py` - backup/cleanup helpers for an existing pool/new-api setup
- `integrate_newapi.py` - explicit new-api integration selector
- `bootstrap_newapi.py`, `import_channels*.py`, `check_*`, `fix_*`, `debug_*` - older operational helpers kept for reference and migration

There are two supported choices when a new-api instance is involved:

1. Inject an existing pool gateway into new-api.
2. Run an explicit external command that deploys/prepares new-api, then optionally inject the pool gateway.

The integration script is dry-run by default:

```bash
# Preview the channel that would be added to new-api.
python integrate_newapi.py --mode existing-gateway

# Actually add one channel that points new-api at the local pool gateway.
python integrate_newapi.py --mode existing-gateway --yes \
  --newapi-url http://127.0.0.1:3000 \
  --gateway-url http://127.0.0.1:8002

# Preview an explicit external deployment command.
python integrate_newapi.py --mode deploy-newapi \
  --deploy-command "docker compose up -d"

# Run that command, then inject the existing pool gateway.
python integrate_newapi.py --mode deploy-newapi --yes \
  --deploy-command "docker compose up -d" \
  --post-deploy-inject
```

For mutation, provide either `NEWAPI_SESSION` or `NEWAPI_USERNAME` and
`NEWAPI_PASSWORD`. The pool gateway key is read from `MASTER_KEY` unless
`--gateway-key` is provided. Do not commit live account files, session cookies,
or keys.

## Quick Start

### Prerequisites

- Python 3.11+
- Camoufox Firefox binary for Puter v2

### Installation

```bash
git clone https://github.com/ppch53/ollama-register.git
cd ollama-register
python -m venv .venv
source .venv/bin/activate  # Linux/macOS
.venv\Scripts\activate     # Windows
pip install -r requirements.txt
python -m camoufox fetch    # needed for Puter v2
```

### Configuration

```bash
cp .env.example .env
# Edit .env with proxy, mailbox, CAPTCHA, and provider credentials as needed.
```

## Repository Map

```text
main.py                  Ollama CLI entry point
ollama_register.py       Ollama registration implementation
puter_register_v2.py     Puter v2 registration implementation
restore_puter_v2.py      Puter v2 state restore/export helper
puter_register.py        Legacy Puter flow

src/                     Shared and project-specific support modules
tests/                   Unit tests for critical helpers and entry points

puter_adapter.py         Optional Puter OpenAI-compatible adapter
pool_gateway.py          Optional local gateway/key pool
integrate_newapi.py      Optional new-api integration selector
pool_cleaner.py          Optional pool/new-api cleanup helper
```

## State Files

Runtime state is deliberately kept outside git. Common deployed locations are:

```text
/opt/ollama-register/
  accounts.json              Ollama account export
  puter_accounts.json        Puter account/token export for gateway use
  pool_state.json            Optional gateway health/quota state

v2/state/
  puter_states.jsonl         Puter v2 state transition log
  puter_accounts_v2.json     Exportable Puter v2 accounts
  puter_quarantine.json      Quarantined accounts
  scheduler_state.json       Rate-limit state
  circuit_breaker.json       Circuit breaker state
  fingerprint_registry.json  Fingerprint registry
  used_emails.json           Email deduplication
  used_usernames.json        Username deduplication
  mailbox_health.json        Mailbox provider health scores
  phone_triggered_ips.jsonl  Phone-triggered IP blacklist

v2/audit/
  puter_audit.jsonl          Full audit records
  puter_failures.jsonl       Terminal failure log
  artifacts/                 Screenshots/HTML dumps

v2/profiles/                 Per-account Firefox profiles
```

## Puter V2 State Machine

```text
draft -> browser_started -> form_filled -> form_submitted -> email_verified -> session_established -> quarantined -> audited -> exportable

Terminal: failed, skipped_phone_verification
```

## Development Checks

```bash
py -m pytest -q
py -m compileall -q main.py ollama_register.py puter_register_v2.py src tests
py -m ruff check --select E9,F63,F7,F82 .
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and guidelines.

## License

[MIT](LICENSE)
