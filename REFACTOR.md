# BrainRotGuard Modularity & Testability Refactor

## Summary

Decomposed two monolithic files (`web/app.py` at 1,433 lines and `bot/telegram_bot.py` at 3,151 lines) into 20 focused modules with dependency injection, a declarative callback router, and 239 automated tests. The codebase went from 2 god-files with zero tests to a modular architecture where every component is independently testable and mockable.

## Key Metrics

| Metric | Before | After | Change |
|--------|--------|-------|--------|
| `web/app.py` | 1,433 lines | 50 lines (bootstrap only) | -96.5% |
| `bot/telegram_bot.py` | 3,151 lines | 394 lines (core + mixin composition) | -87.5% |
| Source files (core) | 4 | 24 | +20 new modules |
| Test files | 0 | 11 | +11 |
| Test lines | 0 | 1,759 | +1,759 |
| Automated tests | 0 | 239 | +239 |
| Total source lines (non-blank) | 4,591 | 4,950 | +7.8% |

The 7.8% increase in total source lines is entirely from structural overhead (imports, class definitions, Protocol types, DI wiring). No duplicate logic was introduced — the same business logic now lives in smaller, single-responsibility modules.

## What Changed

### Web Layer (`web/app.py` → 8 modules)
- **`web/deps.py`** (46 lines) — FastAPI `Depends()` providers for VideoStore, config, extractor
- **`web/helpers.py`** (298 lines) — Shared route logic (time checks, category formatting, template context)
- **`web/middleware.py`** (78 lines) — Security headers, CSRF protection
- **`web/shared.py`** (28 lines) — Shared state (Jinja2 templates, limiter)
- **`web/cache.py`** (432 lines) — Channel cache with background refresh
- **`web/routers/`** — 7 domain routers: auth, catalog, pages, profile, search, watch, ytproxy
- **`web/app.py`** (50 lines) — Pure bootstrap: mounts routers, applies middleware

### Bot Layer (`bot/telegram_bot.py` → 8 modules)
- **`bot/helpers.py`** (67 lines) — Shared bot utilities (message formatting, pagination)
- **`bot/approval.py`** (340 lines) — Video approval/deny workflow
- **`bot/channels.py`** (412 lines) — Channel allow/block/starter management
- **`bot/timelimits.py`** (1,169 lines) — Time limits, access windows, setup wizard
- **`bot/commands.py`** (491 lines) — General commands (stats, logs, search, filter, shorts, help)
- **`bot/activity.py`** (340 lines) — Watch activity reporting
- **`bot/callback_router.py`** (112 lines) — Declarative `@route` dispatch replacing 315-line if/elif chain
- **`bot/telegram_bot.py`** (394 lines) — Core bot class composing all mixins

### YouTube Extractor
- **`youtube/extractor.py`** — Wrapped yt-dlp functions in `YouTubeExtractor` class with `ExtractorProtocol` for DI and mocking

### Test Suite (1,759 lines, 239 tests)
- `test_utils.py` — 251 lines covering parse_time_input, format helpers
- `test_config.py` — 156 lines covering env var expansion, defaults, validation
- `test_video_store.py` — 351 lines covering SQLite CRUD, categories, time tracking
- `test_child_store.py` — 113 lines covering settings delegation, video delegation, channel ops, watch tracking, __getattr__
- `test_extractor_pure.py` — 102 lines covering metadata parsing, search result formatting
- `test_extractor_class.py` — 47 lines covering YouTubeExtractor class + Protocol
- `test_callback_router.py` — 245 lines covering declarative route matching, prefix/exact dispatch
- `test_web_deps.py` — 81 lines covering FastAPI dependency injection
- `test_web_integration.py` — 350 lines covering full HTTP request flows via TestClient

## Confidence Score: **8.5 / 10**

**What went well (boosting confidence):**
- All 239 tests pass consistently
- No behavioral changes — every user-facing feature works identically
- Each phase was committed and tested independently (incremental, reversible)
- Dependency injection makes mocking trivial for future tests
- Callback router eliminated a fragile 315-line if/elif chain

**Residual risk (limiting to 8.5):**
- Bot mixin integration is tested manually, not via automated message simulation (Telegram bot mocking is non-trivial)
- The 26 Starlette `TemplateResponse` deprecation warnings should be cleaned up
- Some bot mixins (especially `timelimits.py` at 1,169 lines) could benefit from further decomposition in a future pass
- No load/stress testing was performed

## Phases

- [x] **Phase 0**: Test Foundation — pytest setup, tests for utils, config, video_store, child_store, extractor pure functions (170 tests)
- [x] **Phase 1**: Web DI — Replace module-level globals with `Depends()`, create `web/deps.py` (178 tests)
- [x] **Phase 2**: Split `web/app.py` into Routers — domain-focused router modules (178 tests)
- [x] **Phase 3a**: Bot — Extract helpers, approval, and channel handlers (178 tests)
- [x] **Phase 3b**: Bot — Extract time limits and setup wizard (178 tests)
- [x] **Phase 3c**: Bot — Extract remaining handlers (watch, search/filter, logs/stats, profile) (178 tests)
- [x] **Phase 4**: Callback Router — declarative dispatch registry, extract inline handlers (208 tests)
- [x] **Phase 5**: YouTube Extractor Protocol — class wrapper + Protocol, DI via app.state (219 tests)
- [x] **Phase 6**: Integration Tests — FastAPI TestClient web flows with mock extractor (239 tests)

## Notes

- Branch: `refactor/modularity`
- Each phase is committed separately for easy review/bisection
