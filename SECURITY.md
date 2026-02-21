# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| Latest release | Yes |
| Older releases | No |

Only the latest release receives security updates. Upgrade to the latest version to stay protected.

## Reporting a Vulnerability

**Do not open a public issue for security vulnerabilities.**

Instead, please report them privately via [GitHub Security Advisories](https://github.com/GHJJ123/brainrotguard/security/advisories/new).

Include:
- Description of the vulnerability
- Steps to reproduce
- Affected version(s)
- Impact assessment (what an attacker could do)

You should receive an initial response within 72 hours. Confirmed vulnerabilities will be patched and disclosed in the next release's changelog under a **Security** section.

## Scope

The following are in scope for security reports:

- **Web UI** (`web/app.py`): Authentication bypass, XSS, CSRF, SSRF, injection
- **Telegram bot** (`bot/telegram_bot.py`): Authorization bypass, callback data tampering, information disclosure
- **Data layer** (`data/video_store.py`): SQL injection, data leakage
- **YouTube integration** (`youtube/extractor.py`): SSRF, command injection, RCE via yt-dlp
- **Configuration** (`config.py`): Credential exposure, insecure defaults

Out of scope:
- Denial of service against the local instance (self-hosted, single-user)
- Issues requiring physical access to the host
- Social engineering attacks against the Telegram admin

## Security Architecture

BrainRotGuard is designed as a self-hosted, single-family application. Key security properties:

- **No external data collection** — all data stays on the host, no telemetry or phone-home
- **PIN-gated web UI** — optional session-based PIN auth with rate limiting
- **Admin-only Telegram bot** — all commands restricted to configured `admin_chat_id`
- **Parameterized SQL** — all database queries use parameterized statements
- **Input validation** — video IDs, search queries, and URLs validated at entry points
- **SSRF prevention** — thumbnail URLs restricted to YouTube CDN allowlist
- **yt-dlp sandboxing** — no JS runtimes, no remote components, timeout-bounded
- **Session-bound playback** — heartbeats only accepted for the video loaded in the current session
- **Security headers** — CSP, X-Frame-Options, X-Content-Type-Options, Referrer-Policy
- **Non-root container** — Docker runs as `appuser`

## Disclosure Policy

Security fixes are documented in `CHANGELOG.md` under a **Security** heading with enough detail for users to understand the impact without providing exploitation instructions.
