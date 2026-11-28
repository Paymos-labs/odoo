{
    "name": "Paymos Payments",
    "version": "20.0.1.2.3",
    "summary": "Hosted cryptocurrency checkout for Odoo 20 website orders",
    "description": (
        "Paymos Payments adds hosted checkout, payment verification and refunds to self-hosted Odoo 20.0. "
        "The add-on is free under LGPL-3.0-only. Using the Paymos payment service requires a Paymos account "
        "and project. Odoo.sh compatibility awaits a hosted check. Odoo Online excludes third-party Python add-ons.\n\n"
        "When an administrator opens the Paymos approval page through Connect, Paymos receives the store's "
        "canonical HTTPS URL and a one-time approval code. The invoice creation request sends the Paymos "
        "project ID, order amount, currency and an opaque order reference. For an order with 1 to 50 valid "
        "product lines it also sends each line's title, quantity, unit price and optional plain-text "
        "description, shown on the Paymos checkout. Odoo keeps buyer identity out of that request. Buyers "
        "enter payment details on the hosted Paymos page."
    ),
    "category": "Accounting/Payment Providers",
    "license": "LGPL-3",
    "author": "Paymos",
    "website": "https://paymos.io",
    "images": [
        "static/description/checkout.png",
        "static/description/setup.png",
        "static/description/refund.png",
    ],
    "depends": ["payment", "account_payment", "website_sale"],
    # The stock Odoo parser only handles bare names without packaging.
    # The root requirements file pins typing-extensions for Python below 3.13.
    "external_dependencies": {"python": ["cryptography"]},
    "data": [
        "security/ir.access.csv",
        "data/payment_provider_data.xml",
        "views/payment_templates.xml",
        "views/payment_provider_views.xml",
        "data/payment_operations.xml",
    ],
    "assets": {"web.assets_backend": ["payment_paymos/static/src/js/connect.js"]},
    "post_init_hook": "post_init_hook",
    "uninstall_hook": "uninstall_hook",
    "installable": True,
}
