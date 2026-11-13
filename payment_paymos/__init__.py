from odoo.addons.payment import reset_payment_provider

from . import controllers, models


def post_init_hook(env):
    seed = env.ref("payment_paymos.payment_provider_paymos").with_context(active_test=False)
    seed._setup_payment_method("paymos")
    companies = env["res.company"].search([
        ("id", "!=", seed.company_id.id),
        ("parent_id", "=", False),
    ])
    for company in companies:
        seed.copy({"company_id": company.id})
    seed._toggle_post_processing_cron()


def uninstall_hook(env):
    providers = env["payment.provider"].sudo().with_context(active_test=False)
    seed = env.ref("payment_paymos.payment_provider_paymos", raise_if_not_found=False)
    if seed and env["payment.transaction"].sudo().search_count(
        [("provider_id", "=", seed.id)], limit=1,
    ):
        # Native transactions still reference these two records after module removal.
        env["ir.model.data"].sudo().search([
            ("module", "=", "payment_paymos"),
            ("name", "in", ["payment_provider_paymos", "payment_method_paymos"]),
        ]).unlink()
    paymos = providers.search([("code", "=", "paymos")])
    paymos.write({
        "active": False,
        "paymos_pending_envelope": False,
        "paymos_active_connection_id": False,
    })
    method = providers._get_provider_payment_method("paymos")
    lines = providers.env["account.payment.method.line"].search([
        ("payment_method_id", "=", method.id),
    ]) if method else providers.env["account.payment.method.line"]
    # Account's unlink guard treats provider links as installed until registry removal.
    linked_lines = lines.filtered(lambda line: line.payment_provider_id in paymos)
    linked_lines.payment_provider_id = False
    if method and providers._check_existing_payment(method):
        # Native unlink detaches journal availability and retains lines with payments.
        lines.unlink()
        paymos.write(paymos._get_removal_values())
        paymos.payment_method_ids.active = False
        providers._toggle_post_processing_cron()
    else:
        reset_payment_provider(providers.env, "paymos")
