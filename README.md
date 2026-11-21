# Paymos Payments for Odoo

Accept cryptocurrency payments on an Odoo 20.0 website using Paymos hosted checkout. The add-on uses Odoo payment transactions and the shop's accounting setup to confirm orders and record refunds.

The add-on is free and licensed under LGPL-3.0-only. Using the hosted payment service requires a Paymos account and project. See [LICENSE](LICENSE) and [NOTICE](NOTICE).

## Requirements

- A self-hosted Odoo 20.0 installation with `payment`, `account_payment` and `website_sale`.
- Odoo's Python environment with `cryptography`. The verified runtime is Python 3.12. For Python versions below 3.13, install the pinned `typing-extensions` wheel using the `requirements.txt` next to `payment_paymos/`.
- An HTTPS storefront and a Paymos project with access to the environment you connect.
- A bank journal in the provider's company with its Paymos inbound payment method line.

The steps below cover a self-hosted Odoo 20.0 installation. Odoo.sh can install Python dependencies listed in the branch's root `requirements.txt`. Compatibility with Odoo.sh awaits a hosted check. Odoo Online excludes third-party Python add-ons. [Odoo 20 on-premises requirements](https://www.odoo.com/documentation/20.0/administration/on_premise/source.html), [Odoo 20 Odoo.sh dependency guide](https://www.odoo.com/documentation/20.0/administration/odoo_sh/create_module.html#use-an-external-python-library), [Odoo Apps FAQ](https://apps.odoo.com/apps/faq)

## Install and connect

1. Extract the ZIP to a directory on the Odoo `addons_path`. That directory should contain `payment_paymos/__manifest__.py` and `requirements.txt`.
2. Install the requirements in the same Python environment Odoo uses. For an offline install, put the `typing_extensions-4.15.0-py3-none-any.whl` file in your wheel directory and use the command below. The file hash is checked by pip. [Pinned wheel](https://pypi.org/project/typing-extensions/4.15.0/#files)

   ```sh
   python -m pip install --no-index --find-links /path/to/wheels --require-hashes -r requirements.txt
   ```

3. Generate a Fernet key with `cryptography.fernet.Fernet.generate_key()` and set it as `PAYMOS_ODOO_ENVELOPE_KEY` in the Odoo server's secret configuration. Restart the Odoo service with that setting. Keep the key with your protected backups because stored connection credentials depend on it.
4. As an Odoo system administrator, update the Apps list and install **Paymos Payments**. Open **Accounting → Configuration → Payment Providers**, choose **Paymos** for the store's company, select **Connect Paymos**, and approve the request in Paymos.
5. Select your company's bank journal and verify its Paymos inbound payment method line. Activate and publish the provider in Sandbox to try the checkout.
6. When your storefront and accounting setup are ready, choose **Select Live**. Changing the environment deactivates and unpublishes the provider. Verify the Live connection, then activate and publish the provider again.

## Checkout and data

The Paymos approval page receives the store's canonical HTTPS URL and a one-time approval code when an administrator opens it through **Connect Paymos**.

To create an invoice, Odoo sends the Paymos project ID, order amount, currency and an opaque order reference. For orders with 1 to 50 valid product lines on `sale.order.order_line` (excluding `display_type`, `is_delivery`, and `is_downpayment`), Odoo also passes an optional `products[]` array (`title`, positive integer `quantity`, exact decimal `unit_amount`, plus optional plain-text `description`). If an order has 0 or more than 50 product lines, a fractional quantity, or any invalid product line, Odoo falls back all-or-nothing to an amount-only invoice using the native order total. The normalized `products_json` snapshot is stored on `paymos.attempt` before the first request. Retries replay its original product values, omitting only the retired `image_url` key from older snapshots without rewriting stored data. Before the first request, all other product changes still invalidate the prepared attempt. Odoo keeps buyer identity out of that request. The buyer enters payment details on the hosted Paymos page. Odoo verifies signed notifications and checks the invoice with Paymos before confirming the linked sale. A scheduled reconciliation job checks for missed notifications.

[Paymos documentation](https://paymos.io/docs), [Paymos privacy policy](https://paymos.io/privacy)

## Refunds

Use an Odoo system administrator account with access to the payment's company.

- For a full refund, open the original posted customer payment in **Accounting** and select **Refund** for the full original amount in the order's currency. Paymos calculates the token payout for the linked invoice and sends it to the original payer. Odoo records the refund against the original payment after verification. [Odoo 20 refund workflow](https://www.odoo.com/documentation/20.0/applications/finance/payment_providers.html#refunds)
- To return an overpayment, enable developer mode and open the original transaction under **Accounting → Configuration → Payment Transactions**. Choose **Refund Paymos overpayment**. After Paymos confirms the return, Odoo adds a note to the order and preserves the settled principal.

Review a failed refund or an operation that reaches its verification limit in Paymos before taking further action.

## Build from source

Bundle any stamped stable Paymos Python SDK `1.x` release (`>= 1.6.0`) from a local SDK source tree (`--sdk-source`, the default release flow) or from an offline release wheel (`--sdk-wheel`). The add-on sends optional `products[]` payloads over the bundled SDK's `client.request("POST", "/v1/invoices", payload)` transport without modifying the SDK source. The builder verifies the SDK version, required runtime files, license, and SHA-256 checksums using only local inputs. The optional mirror directory receives exactly the files from the ZIP. [SDK source](https://github.com/Paymos-labs/python-sdk), [SDK releases on PyPI](https://pypi.org/project/paymos-sdk/)

```sh
python scripts/build-odoo-plugin.py build --sdk-source /path/to/python-sdk --output /path/to/paymos-odoo-v1.1.0.zip --mirror-dir /path/to/empty-mirror
python scripts/build-odoo-plugin.py verify --archive /path/to/paymos-odoo-v1.1.0.zip --sdk-source /path/to/python-sdk
```

To build from an offline release wheel instead of a source tree:

```sh
python scripts/build-odoo-plugin.py build --sdk-wheel /path/to/paymos_sdk-1.7.1-py3-none-any.whl --output /path/to/paymos-odoo-v1.1.0.zip --mirror-dir /path/to/empty-mirror
python scripts/build-odoo-plugin.py verify --archive /path/to/paymos-odoo-v1.1.0.zip
```

Pass the same full `--source-commit` (and optional `--sdk-version` / `--sdk-source-commit`) to `build` and `verify` when building from a release commit. The `bundle-manifest.json` records the Odoo version, tag, source and file hashes, and bundled SDK metadata. The command prints the ZIP's SHA-256.
