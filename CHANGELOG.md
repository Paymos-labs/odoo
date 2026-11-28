# Changelog

## [Unreleased]

## [1.2.3] - 2026-11-28

- fix(odoo): refuse tolerance refunds early, review definitive refusals

### Changed

- The module description and the Odoo Apps page say that invoice requests also carry the order's product lines (title, quantity, unit price, optional description).

### Fixed

- A full refund of a payment accepted within the underpayment tolerance is refused at once, before a refund transaction is created: Paymos refunds no invoice paid below its expected amount, and the refund transaction used to stay pending for good.
- A refund request Paymos refuses for good (400, 404, 409, 410 or 422) goes to review with its reason at once instead of twelve retries as an unknown result. 401, 403, 408, 429 and 5xx are still retried.
- An unsent checkout prepared by 20.0.1.0.0, which has no product snapshot, no longer fails as a changed order after the update; it is resumed amount-only.

## [1.2.2] - 2026-11-26

- docs(plugins): add 6-language READMEs (EN/zh-CN/ru/es/de/tr) and YouTube walkthrough previews across all 25 Paymos-labs repos
- chore: bundle Paymos Python SDK v1.7.2
- chore: rebuild canonical CMS package

### Added

- Optional checkout `products[]` line-item payloads with all-or-nothing amount-only fallback and retry snapshot replay.

### Changed

- Dynamically bundle and verify any stable Paymos Python SDK `1.x` release (`>= 1.6.0`) at build and runtime.

## [1.2.1] - 2026-11-25

- chore: rebuild canonical CMS package

### Added

- Optional checkout `products[]` line-item payloads with all-or-nothing amount-only fallback and retry snapshot replay.

### Changed

- Dynamically bundle and verify any stable Paymos Python SDK `1.x` release (`>= 1.6.0`) at build and runtime.

## [1.2.0] - 2026-11-23

- feat(platform): integrate pending main WIP — drop fiat_exchange_rates/VES, blockchain/worker fix batches 1-7, compact auth email layout, status-worker, plugins, forge and docs
- chore: rebuild canonical CMS package

### Added

- Optional checkout `products[]` line-item payloads with all-or-nothing amount-only fallback and retry snapshot replay.

### Changed

- Dynamically bundle and verify any stable Paymos Python SDK `1.x` release (`>= 1.6.0`) at build and runtime.

## [1.1.1] - 2026-11-22

- chore: bundle Paymos Python SDK v1.7.2
- chore: rebuild canonical CMS package

### Added

- Optional checkout `products[]` line-item payloads with all-or-nothing amount-only fallback and retry snapshot replay.

### Changed

- Dynamically bundle and verify any stable Paymos Python SDK `1.x` release (`>= 1.6.0`) at build and runtime.

## [1.1.0] - 2026-11-21

- feat(release): automate Odoo 20.0 and GiveWP releases across all 21 ecosystem mirrors
- refactor(checkout): remove product images from invoice products
- feat(plugins): add products[] to Odoo 20.0.1.1.0 over pinned sdk 1.6.0 (10.2)
- chore: bundle Paymos Python SDK v1.7.1
- chore: rebuild canonical CMS package

### Added

- Optional checkout `products[]` line-item payloads with all-or-nothing amount-only fallback and retry snapshot replay.

### Changed

- Dynamically bundle and verify any stable Paymos Python SDK `1.x` release (`>= 1.6.0`) at build and runtime.

## [1.0.1] - 2026-11-16

### Added

- Paymos Connect and hosted checkout for Odoo 20.0 website orders.
- Signed webhook verification, scheduled reconciliation and native sales accounting.
- Full refunds through the original payment and a separate action for returning overpayments.

