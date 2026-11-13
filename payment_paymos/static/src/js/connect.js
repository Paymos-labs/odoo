import { Component, onMounted, onWillDestroy, signal, t, useProps, xml } from "@odoo/owl";
import { browser } from "@web/core/browser/browser";
import { Dialog } from "@web/core/dialog/dialog";
import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";


class PaymosConnectDialog extends Component {
    static components = { Dialog };
    static template = xml`
        <Dialog title="this.title" size="'md'">
            <p t-esc="this.status()" role="status" aria-live="polite"/>
            <div t-if="this.pending()">
                <p>Enter this user code if Paymos asks for it.</p>
                <p><code class="fs-5" t-esc="this.props.userCode"/></p>
                <a t-att-href="this.props.verificationUrl" target="_blank"
                   rel="noopener noreferrer" class="btn btn-primary">Open Paymos</a>
            </div>
            <t t-set-slot="footer">
                <button type="button" class="btn btn-secondary" t-on-click="() => this.props.close()">Close</button>
            </t>
        </Dialog>
    `;
    props = useProps({
        close: t.function(),
        providerId: t.number(),
        flowId: t.string(),
        userCode: t.string(),
        verificationUrl: t.string(),
        retryAfter: t.number(),
    });

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.notification = useService("notification");
        this.title = _t("Connect Paymos");
        this.status = signal(_t("Approve this Odoo store in Paymos. This window checks for approval automatically."));
        this.pending = signal(true);
        onMounted(() => this.schedule(this.props.retryAfter));
        onWillDestroy(() => browser.clearTimeout(this.timer));
    }

    schedule(seconds) {
        const delay = Math.max(1, Math.min(60, seconds));
        this.timer = browser.setTimeout(() => this.poll(), delay * 1000);
    }

    async poll() {
        try {
            const result = await this.orm.call("payment.provider", "action_paymos_poll", [
                [this.props.providerId],
                this.props.flowId,
            ]);
            if (result.status === "waiting") {
                this.schedule(result.retry_after);
                return;
            }
            if (result.status === "connected") {
                this.notification.add(_t("Paymos is connected in Sandbox. Enable and publish it to accept test payments."), {
                    type: "success",
                });
                await this.action.doAction({ type: "ir.actions.client", tag: "soft_reload" });
                this.props.close();
                return;
            }
            const messages = {
                conflict: _t("This website is already linked to a different Paymos project."),
                denied: _t("The Paymos authorization was denied."),
                expired: _t("The Paymos authorization expired. Start Connect again."),
            };
            this.status.set(messages[result.status] || _t("Paymos could not verify this connection."));
        } catch {
            this.status.set(_t("Paymos could not verify this connection. Start Connect again."));
        }
        this.pending.set(false);
    }
}


function openPaymosConnect(_env, action) {
    const { provider_id, flow_id, user_code, verification_url, approval_origin, retry_after } = action.params;
    const approval = new URL(verification_url);
    if (
        approval.origin !== approval_origin ||
        approval.protocol !== "https:" ||
        approval.pathname !== "/connect/cms/verify" ||
        approval.username || approval.password || approval.hash
    ) {
        useService("notification").add(_t("Paymos returned an invalid approval URL."), { type: "danger" });
        return;
    }
    useService("dialog").add(PaymosConnectDialog, {
        providerId: provider_id,
        flowId: flow_id,
        userCode: user_code,
        verificationUrl: verification_url,
        retryAfter: retry_after,
    });
}

registry.category("actions").add("payment_paymos.connect", openPaymosConnect);
