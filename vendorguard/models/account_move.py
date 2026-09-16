from dateutil.relativedelta import relativedelta

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from .vendorguard_constants import BANK_CHANGE_RECENT_DAYS, THREE_WAY_MATCH_QTY_TOLERANCE


class AccountMove(models.Model):
    _inherit = 'account.move'

    fraud_flag_ids = fields.One2many('vendorguard.fraud.flag', 'move_id', string='Fraud Flags')

    @api.constrains('ref', 'partner_id', 'amount_total', 'invoice_date', 'move_type', 'state')
    def _check_vendorguard_duplicate_bill(self):
        for move in self:
            if move.move_type not in ('in_invoice', 'in_refund'):
                continue
            if move.state == 'cancel':
                continue
            if not move.ref:
                continue
            domain = [
                ('id', '!=', move.id),
                ('company_id', '=', move.company_id.id),
                ('move_type', '=', move.move_type),
                ('partner_id', '=', move.partner_id.id),
                ('ref', '=', move.ref),
                ('amount_total', '=', move.amount_total),
                ('state', '!=', 'cancel'),
            ]
            if move.invoice_date:
                domain.append(('invoice_date', '=', move.invoice_date))
            duplicate = self.env['account.move'].search(domain, limit=1)
            if duplicate:
                raise ValidationError(_(
                    "VendorGuard: this bill looks like a duplicate of %s "
                    "(same reference, vendor, amount and date)."
                ) % duplicate.display_name)

    def action_post(self):
        blocked = self.env['account.move']
        messages = []
        for move in self:
            message = move._vendorguard_check_before_post()
            if message:
                blocked |= move
                messages.append(message)

        remaining = self - blocked
        result = True
        if remaining:
            result = super(AccountMove, remaining).action_post()

        if blocked:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _("VendorGuard blocked %d bill(s) from posting") % len(blocked),
                    'message': '\n\n'.join(messages),
                    'sticky': True,
                    'type': 'danger',
                    # refresh the open form so the Fraud Flags tab shows the flags just created
                    'next': {'type': 'ir.actions.client', 'tag': 'soft_reload'},
                },
            }
        return result

    def _vendorguard_check_before_post(self):
        """Returns a human-readable block message if this bill should not post, else None.
        Never raises: a blocked bill simply stays in draft, and the caller shows the message
        as a notification instead of an exception dialog (which also keeps the client's
        record data fresh, since a normal return — unlike a raised exception — lets the
        transaction commit and the form reload)."""
        self.ensure_one()
        if self.env.context.get('vendorguard_seeding'):
            return None
        if self.move_type not in ('in_invoice', 'in_refund'):
            return None

        self._check_ghost_vendor()

        unresolved = self.fraud_flag_ids.filtered(lambda f: f.state in ('flagged', 'pending_review'))
        if unresolved:
            return (
                "%s has unresolved VendorGuard flags and cannot be posted until a Finance "
                "Manager reviews them:\n%s"
            ) % (self.partner_id.name, "\n".join(
                "- [%s] %s" % (f.severity.upper(), f.description) for f in unresolved))

        already_flagged_types = set(self.fraud_flag_ids.mapped('flag_type'))
        new_flags = []

        recent_change = self.env['vendorguard.bank.change.log'].search([
            ('partner_id', '=', self.partner_id.id),
            ('change_date', '>=', fields.Datetime.now() - relativedelta(days=BANK_CHANGE_RECENT_DAYS)),
        ], limit=1, order='change_date desc')
        if recent_change and 'bank_swap' not in already_flagged_types:
            new_flags.append({
                'flag_type': 'bank_swap', 'severity': 'critical', 'state': 'flagged',
                'partner_id': self.partner_id.id, 'move_id': self.id,
                'amount': self.amount_total, 'resolvable': True,
                'description': (
                    "Bank account for %s changed on %s (old: %s, new: %s) within %d days of this bill."
                ) % (self.partner_id.name, recent_change.change_date,
                     recent_change.old_acc_number, recent_change.new_acc_number, BANK_CHANGE_RECENT_DAYS),
            })

        if self.create_uid.id == self.env.user.id and 'segregation_of_duties' not in already_flagged_types:
            new_flags.append({
                'flag_type': 'segregation_of_duties', 'severity': 'medium', 'state': 'flagged',
                'partner_id': self.partner_id.id, 'move_id': self.id,
                'amount': self.amount_total, 'resolvable': True,
                'description': (
                    "Bill %s was created and is being posted by the same user (%s)."
                ) % (self.name or self.ref or "Draft", self.env.user.name),
            })

        mismatch_lines = self._vendorguard_three_way_match_mismatches()
        if mismatch_lines and 'three_way_match' not in already_flagged_types:
            details = "; ".join(
                "%s: billed %.2f vs received %.2f (ordered %.2f)" % (
                    line.product_id.display_name or line.name, billed, received, ordered)
                for line, billed, received, ordered in mismatch_lines)
            new_flags.append({
                'flag_type': 'three_way_match', 'severity': 'high', 'state': 'flagged',
                'partner_id': self.partner_id.id, 'move_id': self.id,
                'amount': self.amount_total, 'resolvable': True,
                'description': (
                    "Three-way match: this bill invoices more than has been recorded as received "
                    "against its purchase order(s) — %s."
                ) % details,
            })

        if new_flags:
            for vals in new_flags:
                vals['company_id'] = self.company_id.id
            created = self.env['vendorguard.fraud.flag'].create(new_flags)
            return (
                "VendorGuard blocked this bill from %s — %d new issue(s) found:\n%s\n\n"
                "Ask a Finance Manager to review and approve before posting again."
            ) % (self.partner_id.name, len(created), "\n".join(
                "- [%s] %s" % (f.severity.upper(), f.description) for f in created))

        return None

    def _vendorguard_three_way_match_mismatches(self):
        """Cross-module check against purchase.order.line: flag a bill that invoices more
        of a product than has been recorded as received against its purchase order, the
        classic three-way-match (PO / receipt / bill) control against quantity manipulation."""
        self.ensure_one()
        mismatches = []
        for line in self.invoice_line_ids:
            po_line = line.purchase_line_id
            if not po_line:
                continue
            billed = po_line.qty_invoiced
            received = po_line.qty_received
            if billed - received > THREE_WAY_MATCH_QTY_TOLERANCE:
                mismatches.append((po_line, billed, received, po_line.product_qty))
        return mismatches

    def _check_ghost_vendor(self):
        """Informational only, never blocks posting — a vendor with no tax ID and no bank
        account on file is a common pattern for a fake/shell vendor set up to receive a
        single payment (ACFE ghost-vendor pattern), worth a Finance Manager's attention
        but not proof of fraud on its own."""
        self.ensure_one()
        partner = self.partner_id
        if partner.vat or partner.bank_ids:
            return
        existing = self.env['vendorguard.fraud.flag'].search([
            ('partner_id', '=', partner.id), ('flag_type', '=', 'ghost_vendor'),
            ('state', 'in', ('flagged', 'pending_review')),
        ], limit=1)
        if existing:
            return
        self.env['vendorguard.fraud.flag'].create({
            'flag_type': 'ghost_vendor', 'severity': 'medium', 'state': 'flagged',
            'partner_id': partner.id, 'resolvable': False, 'company_id': self.company_id.id,
            'description': (
                "Vendor %s has posted bills but no tax ID (VAT) and no bank account on file — "
                "a common pattern for fake/shell vendors set up to receive a single payment."
            ) % partner.name,
        })
