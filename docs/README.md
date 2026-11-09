# RobotMonitor Documentation

RobotMonitor is an international, open-source project. Documentation is kept in **English** as the
lingua franca, with key entry points translated into **한국어** and **中文**.

## Top-level entry points

| Document | English | 한국어 | 中文 |
| :--- | :--- | :--- | :--- |
| Project README (overview, quick start) | [README.md](../README.md) | [README.ko.md](../README.ko.md) | [README.zh.md](../README.zh.md) |
| Contributor guide | [CONTRIBUTING.md](../CONTRIBUTING.md) | — | — |
| Code of Conduct | [CODE_OF_CONDUCT.md](../CODE_OF_CONDUCT.md) | — | — |
| Security policy | [SECURITY.md](../SECURITY.md) | — | — |
| Developer guide (arch, dev workflow, mock backend) | [developer-guide.md](./developer-guide.md) | — | — |

## Design specs (intentional source of truth)

The `spec/` directory holds six design specs that drive implementation:

- `spec/00` architecture · `spec/01` backend · `spec/02` alerts · `spec/03` frontend · `spec/04` simulator · `spec/05` playbook

## UI language support

The dashboard supports **English (default) / 한국어 / 中文** via a live language switcher in the top
header. Strings live in `frontend_dashboard/src/i18n/locales.ts`; resolve them with `useI18n()`.
Server-supplied data (alert messages, robot names) is intentionally not translated.

To add a language: add a `Locale` entry in `locales.ts` and a `<select>` option in
`LanguageSwitcher.tsx`, then provide the three dictionaries (en/ko/zh).
