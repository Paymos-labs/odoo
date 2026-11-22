# Changelog

## [Unreleased]

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

