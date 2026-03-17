# Adding a Language

This directory contains the locale-specific translation tables used by both the
web UI and the Telegram bot.

Current locales:

- `en` — English
- `nb` — Norwegian Bokmal

## How the translation layer works

- English source strings are used as translation keys throughout the codebase.
- `i18n/__init__.py` normalizes the configured locale and routes lookups through
  `t(locale, key, **kwargs)`.
- If a translation is missing, the key falls back to the original English text.
- Locale-aware helpers such as `format_time()`, `format_time_compact()`,
  `day_label()`, `category_label()`, and `format_month_day()` also live in
  `i18n/__init__.py`.

Because English falls back to the source text, `en.py` can stay minimal. New
non-English locales should include translated values for every key they support.

## Steps to add a new locale

1. Create a new file in this directory.

   Example:

   ```python
   # i18n/locales/es.py
   TRANSLATIONS: dict[str, str] = {
       "Search": "Buscar",
       "Go": "Ir",
   }
   ```

2. Add any locale-specific formatting data needed by the shared helpers.

   Example:

   ```python
   MONTHS_SHORT = (
       "ene",
       "feb",
       "mar",
       "abr",
       "may",
       "jun",
       "jul",
       "ago",
       "sep",
       "oct",
       "nov",
       "dic",
   )
   ```

3. Register the locale in `i18n/__init__.py`.

   Update:

   - `SUPPORTED_LOCALES`
   - `_TRANSLATIONS`
   - imports for the new locale module
   - any locale-specific data imports used by formatting helpers

4. Extend `normalize_locale()` so common variants map to the canonical locale.

   Example mappings for Spanish might include:

   - `es`
   - `es-ES`
   - `es-MX`

5. Decide the locale default for `time_format: locale`.

   If the new language should default to 24-hour time, update `_uses_24h()` in
   `i18n/__init__.py`.

6. Update user-facing docs.

   At minimum:

   - `config.example.yaml`
   - `docs/configuration.md`
   - `README.md` if the new language should be called out in the feature list

7. Add tests.

   Recommended coverage:

   - locale normalization
   - translation lookup
   - time formatting
   - month/day formatting if the locale uses locale-specific month labels
   - any command or UI flows where translated text is important

## Guidelines

- Keep translation keys identical to the English source strings used in code.
- Preserve placeholders exactly, for example `{name}`, `{limit}`, or `{time}`.
- Keep category/day labels aligned with the helper functions in `i18n/__init__.py`.
- If a string contains Markdown or Telegram formatting, preserve the structure
  of that formatting in the translation.
- Avoid partial locale registration. If a locale is user-selectable, the core
  web and bot flows should be translated together.

## Config reminder

Users select language and time display in `config.yaml`:

```yaml
app:
  locale: en
  time_format: locale
```

Supported time format values:

- `locale`
- `12h`
- `24h`
