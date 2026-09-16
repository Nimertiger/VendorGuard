from dateutil.relativedelta import relativedelta

from odoo import _, fields, models

from .vendorguard_constants import STRUCTURING_THRESHOLD, STRUCTURING_WINDOW_DAYS


class PurchaseOrder(models.Model):
    _inherit = 'purchase.order'

    fraud_flag_ids = fields.One2many('vendorguard.fraud.flag', 'purchase_order_id', string='Fraud Flags')

    def button_confirm(self):
        blocked = self.env['purchase.order']
        messages = []
        for order in self:
            message = order._vendorguard_check_structuring()
            if message:
                blocked |= order
                messages.append(message)

        remaining = self - blocked
        result = True
        if remaining:
            result = super(PurchaseOrder, remaining).button_confirm()

        if blocked:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _("VendorGuard blocked %d purchase order(s)") % len(blocked),
                    'message': '\n\n'.join(messages),
                    'sticky': True,
                    'type': 'danger',
                    'next': {'type': 'ir.actions.client', 'tag': 'soft_reload'},
                },
            }
        return result

    def _vendorguard_check_structuring(self):
        """Returns a human-readable block message if this PO should not confirm, else None."""
        self.ensure_one()
        already_flagged = self.fraud_flag_ids.filtered(
            lambda f: f.flag_type == 'structuring' and f.state in ('flagged', 'pending_review'))
        if already_flagged:
            # don't spam a fresh flag on every retry — the existing one already blocks this PO
            return already_flagged[0].description
        ref_date = self.date_order or fields.Datetime.now()
        window_start = ref_date - relativedelta(days=STRUCTURING_WINDOW_DAYS)
        siblings = self.env['purchase.order'].search([
            ('partner_id', '=', self.partner_id.id),
            ('state', '=', 'purchase'),
            ('date_order', '>=', window_start),
            ('date_order', '<=', ref_date),
            ('id', '!=', self.id),
        ])
        amounts = siblings.mapped('amount_total') + [self.amount_total]
        total = sum(amounts)
        max_single = max(amounts)
        if total > STRUCTURING_THRESHOLD and max_single <= STRUCTURING_THRESHOLD:
            self.env['vendorguard.fraud.flag'].create({
                'flag_type': 'structuring', 'severity': 'high', 'state': 'flagged',
                'partner_id': self.partner_id.id, 'purchase_order_id': self.id,
                'amount': total, 'resolvable': False,
                'description': (
                    "Vendor %s has %d purchase orders totalling %.2f within the last %d days, "
                    "exceeding the %.2f threshold, while no single PO alone exceeded it."
                ) % (self.partner_id.name, len(siblings) + 1, total, STRUCTURING_WINDOW_DAYS, STRUCTURING_THRESHOLD),
            })
            return (
                "Confirming this PO would bring total purchases from %s to %.2f over %d days, "
                "over the %.2f threshold, without any single PO crossing it. Blocked as a possible "
                "structuring pattern — this is a structural issue with the purchase history, not "
                "something to approve away; adjust the order or the vendor's PO history instead."
            ) % (self.partner_id.name, total, STRUCTURING_WINDOW_DAYS, STRUCTURING_THRESHOLD)
        return None
