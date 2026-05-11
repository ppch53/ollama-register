# ollama-register

Automated account registration for [Ollama](https://ollama.com) and [Puter](https://puter.com) with anti-detection capabilities.

> **Disclaimer**: This project is for educational and research purposes. Users are responsible for compliance with the terms of service of any platform they interact with.

## Features

### Puter v2 (Camoufox-based)

- **Anti-detect browser** — Camoufox (Firefox-based) with C++ level fingerprint injection via BrowserForge
- **Behavioral simulation** — burst-mode typing with typo/undo, Bezier-curve mouse movement, random scrolling, natural page dwell
- **11-state registration machine** — full transition audit with crash recovery matrix
- **Phone verification skip** — detects phone prompts, soft-lands (browse 30-60s), blacklists IP for the day
- **Turnstile handling** — auto-solve → checkbox click → CapSolver fallback (3 attempts)
- **Quarantine system** — 24h hold + delayed re-audit, optional 72h second check
- **Circuit breaker** — auto-stops on repeated suspensions, mail failures, or rate limits
- **Full audit trail** — 24-field JSONL records with behavioral timing and proxy session proofs

### Ollama (Pure HTTP)

- TLS fingerprint impersonation via `curl_cffi` (Chrome 136)
- WorkOS Server Action protocol replay
- Turnstile CAPTCHA solving
- Phone verification via HeroSMS

### Shared

- **Rate-limited scheduling** — max 5 registrations/day, 30min minimum gap, UTC 8:00-22:00 window
- **Sticky proxy management** — session-stable IPs with IP blacklist for phone-triggered addresses
- **Mailbox provider pool** — health scoring (0.0-1.0), 3-failure cooldown, provider rotation
- **Username generation** — 4 pattern modes (name+number, adj+noun, noun+number, name+noun)
- **Structured logging** — JSON format, run_id tracking, sensitive field redaction

## Quick Start

### Prerequisites

- Python 3.11+
- [Camoufox](https://github.com/nicoreed/camoufox) Firefox binary (for Puter v2)

### Installation

```bash
git clone https://github.com/YOUR_USERNAME/ollama-register.git
cd ollama-register
python -m venv .venv
source .venv/bin/activate  # Linux/macOS
.venv\Scripts\activate     # Windows
pip install -r requirements.txt
python -m camoufox fetch   # Download Camoufox Firefox binary (~200MB)
```

### Configuration

```bash
cp .env.example .env
# Edit .env with your proxy and provider credentials
```

### Puter v2

```bash
# Dry-run (no real Puter access)
python puter_register_v2.py --dry-run

# Single live registration (requires PUTER_LIVE_REGISTRATION=1)
PUTER_LIVE_REGISTRATION=1 python puter_register_v2.py --live -n 1

# Check quarantine queue
python puter_register_v2.py --quarantine-check

# Reset tripped circuit breaker
python puter_register_v2.py --circuit-breaker-reset
```

### Ollama Registration

```bash
python main.py            # Single registration
python main.py -n 10 -c 3 # 10 accounts, 3 concurrent
```

## Project Structure

```
src/
  preflight.py         # Environment validation
  profile_manager.py   # Isolated browser profiles per account
  sticky_proxy.py      # Session-stable proxy with IP blacklist
  fingerprint_gen.py   # BrowserForge fingerprint generation
  scheduler.py         # UTC time-window rate limiting
  mailbox_provider.py  # Email provider abstraction with health scoring
  username_gen.py      # Natural username generation
  utils.py             # Shared utilities (atomic write, JSONL, etc.)
  logging_config.py    # Structured JSON logging
  config.py            # Configuration loader
  account_store.py     # Account persistence with filelock
  browser_flow.py      # Playwright registration flow (v1)
  ...
puter_register_v2.py   # Puter registration orchestrator
ollama_register.py     # Ollama registration orchestrator
restore_puter_v2.py    # State recovery from JSONL logs
main.py                # CLI entry point
pool_gateway.py        # OpenAI-compatible API gateway
```

## State Files (Deployed)

```
/opt/ollama-register/v2/
  state/
    puter_states.jsonl          # State transition log (source of truth)
    puter_accounts_v2.json      # Exportable accounts only
    puter_quarantine.json       # Accounts in quarantine
    scheduler_state.json        # Rate limit state
    circuit_breaker.json        # Circuit breaker state
    fingerprint_registry.json   # Fingerprint registry
    used_emails.json            # Dedup: used emails
    used_usernames.json         # Dedup: used usernames
    mailbox_health.json         # Provider health scores
    phone_triggered_ips.jsonl   # Phone-triggered IP blacklist
  audit/
    puter_audit.jsonl           # Full audit records
    puter_failures.jsonl        # Terminal failure log
    artifacts/                  # Screenshots/HTML dumps (7-day auto-cleanup)
  profiles/                     # Per-account Firefox profiles
```

## State Machine

```
draft → browser_started → form_filled → form_submitted →
email_verified → session_established → quarantined →
audited → exportable

Terminal: failed, skipped_phone_verification
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and guidelines.

## License

[MIT](LICENSE)
