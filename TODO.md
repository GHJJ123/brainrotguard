# TODO

## Light Mode / Per-Profile Theming

Per-profile light/dark theme toggle for the web UI. Each child profile would store a theme preference, applied on login.

### Exploration Findings

- **Existing CSS variables**: ~11 vars in `style.css` cover structural skeleton (backgrounds, borders, text)
- **Hardcoded colors**: ~60+ raw color values scattered through `style.css` (rgba, hex, hsl)
- **JS inline styles**: `watch.html` sets colors directly in overlay/fullscreen JS logic

### Files Requiring Changes

1. `web/static/style.css` — migrate hardcoded colors to CSS variables, define light theme overrides
2. `web/templates/base.html` — theme class on `<body>`, theme toggle or auto-apply from profile
3. `web/templates/watch.html` — refactor inline JS color values to use CSS variables
4. `web/templates/index.html` — any inline style adjustments
6. `web/templates/login.html` — theme-aware login screen
7. `web/app.py` — serve theme preference from profile session
8. `data/video_store.py` — DB schema: add theme column to profiles table
9. `bot/telegram_bot.py` — optional `/child theme <name> light|dark` command

### Effort Areas

- **CSS variable cleanup**: biggest chunk — audit and replace ~60+ hardcoded colors with semantic variables, then define a `[data-theme="light"]` override set
- **DB schema migration**: add `theme TEXT DEFAULT 'dark'` to profiles table
- **Profile picker integration**: apply theme class on login based on stored preference
- **JS overlay refactor**: watch.html pause/end/fullscreen overlays need to read from CSS vars instead of inline hex values
