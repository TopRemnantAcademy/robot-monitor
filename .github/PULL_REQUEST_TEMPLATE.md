# Pull Request

## What / Why

<!-- What does this PR do, and why? Link the issue(s) it closes. -->

Closes #

## Changes

<!-- Bullet the substantive changes. For UI changes, list the new i18n keys added to locales.ts. -->

-

## Evidence

<!-- How was it verified? Paste `npm test` / `npm run typecheck` output, screenshots, or CI links. -->

-

## Checklist

- [ ] `npm test` passes (frontend) / `pytest` passes (backend)
- [ ] `npm run typecheck` clean
- [ ] No secrets committed (`.env`, tokens, etc. are git-ignored)
- [ ] UI strings added to `i18n/locales.ts` (en + ko + zh) and resolved via `useI18n()` — not hard-coded
- [ ] Docs updated where relevant (README / developer-guide)
- [ ] Self-reviewed diff for scope creep
