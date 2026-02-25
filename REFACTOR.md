# BrainRotGuard Modularity & Testability Refactor

Tracking progress across sessions. Each phase = one session.

## Phases

- [x] **Phase 0**: Test Foundation — pytest setup, tests for utils, config, video_store, child_store, extractor pure functions (170 tests)
- [x] **Phase 1**: Web DI — Replace module-level globals with `Depends()`, create `web/deps.py` (178 tests)
- [x] **Phase 2**: Split `web/app.py` into Routers — domain-focused router modules (178 tests)
- [x] **Phase 3a**: Bot — Extract helpers, approval, and channel handlers (178 tests)
- [x] **Phase 3b**: Bot — Extract time limits and setup wizard (178 tests)
- [ ] **Phase 3c**: Bot — Extract remaining handlers (watch, search/filter, logs/stats, profile)
- [ ] **Phase 4**: Callback Router — declarative callback dispatch registry
- [ ] **Phase 5**: YouTube Extractor Protocol — class wrapper + Protocol for mocking
- [ ] **Phase 6**: Integration Tests — end-to-end test flows

## Notes

- Full plan: `.claude/agents/refactor-plan.md`
- Branch: `refactor/modularity`
- Each phase is committed separately
