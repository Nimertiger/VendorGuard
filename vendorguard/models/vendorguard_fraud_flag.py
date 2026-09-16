from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError


class VendorguardFraudFlag(models.Model):
    _name = 'vendorguard.fraud.flag'
    _description = 'Vendor Fraud Flag'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'create_date desc'

    flag_type = fields.Selection([
        ('duplicate_bill', 'Duplicate Bill'),
        ('bank_swap', 'Bank Account Swap'),
        ('structuring', 'Structuring'),
        ('lookalike_vendor', 'Lookalike Vendor'),
        ('segregation_of_duties', 'Segregation of Duties'),
        ('benford_anomaly', 'Benford Anomaly'),
        ('ghost_vendor', 'Ghost Vendor'),
        ('three_way_match', 'Three-Way Match Mismatch'),
        ('shared_bank_account', 'Shared Bank Account'),
    ], required=True, tracking=True)
    severity = fields.Selection([
        ('low', 'Low'),
        ('medium', 'Medium'),
        ('high', 'High'),
        ('critical', 'Critical'),
    ], required=True, default='medium', tracking=True)
    state = fields.Selection([
        ('flagged', 'Flagged'),
        ('pending_review', 'Pending Review'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
    ], required=True, default='flagged', tracking=True)
    partner_id = fields.Many2one('res.partner', required=True, tracking=True)
    move_id = fields.Many2one('account.move', string='Vendor Bill')
    purchase_order_id = fields.Many2one('purchase.order', string='Purchase Order')
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company)
    currency_id = fields.Many2one('res.currency', default=lambda self: self.env.company.currency_id)
    amount = fields.Monetary(currency_field='currency_id')
    description = fields.Text()
    resolvable = fields.Boolean(default=True)

    @api.model_create_multi
    def create(self, vals_list):
        flags = super().create(vals_list)
        flags.filtered(lambda f: f.severity == 'critical')._notify_finance_managers()
        return flags

    def _notify_finance_managers(self):
        managers = self.env['res.users'].search([
            ('group_ids', 'in', self.env.ref('vendorguard.group_finance_manager').id),
        ])
        for flag in self:
            recipients = managers or self.env.user
            for manager in recipients:
                flag.activity_schedule(
                    'mail.mail_activity_data_todo',
                    user_id=manager.id,
                    summary="VendorGuard: critical %s flag on %s" % (
                        dict(flag._fields['flag_type'].selection).get(flag.flag_type), flag.partner_id.name),
                    note=flag.description or '',
                )
            flag.message_post(
                body="Critical fraud flag raised: %s" % (flag.description or flag.flag_type),
                subject="VendorGuard Critical Alert",
            )

    def action_approve(self):
        if not self.env.user.has_group('vendorguard.group_finance_manager'):
            raise AccessError(_("Only Finance Managers can approve fraud flags."))
        if len(self) == 1 and not self.resolvable:
            raise UserError(_(
                "This flag is a structural data problem, not something to approve. "
                "Fix or cancel the underlying document instead."))
        for flag in self.filtered('resolvable'):
            if flag.state not in ('flagged', 'pending_review'):
                continue
            flag.with_context(vendorguard_internal_state_change=True).state = 'approved'

    def action_reject(self):
        if not self.env.user.has_group('vendorguard.group_finance_manager'):
            raise AccessError(_("Only Finance Managers can reject fraud flags."))
        if len(self) == 1 and not self.resolvable:
            raise UserError(_(
                "This flag is a structural data problem, not something to reject. "
                "Fix or cancel the underlying document instead."))
        for flag in self.filtered('resolvable'):
            flag.with_context(vendorguard_internal_state_change=True).state = 'rejected'

    def write(self, vals):
        if 'state' in vals and not self.env.context.get('vendorguard_internal_state_change'):
            if not self.env.user.has_group('vendorguard.group_finance_manager'):
                raise AccessError(_(
                    "Only Finance Managers can change a fraud flag's state. Use the "
                    "Approve/Reject buttons instead of editing this field directly."))
        return super().write(vals)
