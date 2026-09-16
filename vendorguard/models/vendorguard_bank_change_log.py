from odoo import fields, models


class VendorguardBankChangeLog(models.Model):
    _name = 'vendorguard.bank.change.log'
    _description = 'Vendor Bank Account Change Log'
    _order = 'change_date desc'

    partner_id = fields.Many2one('res.partner', required=True)
    old_acc_number = fields.Char()
    new_acc_number = fields.Char()
    change_date = fields.Datetime(default=fields.Datetime.now)
