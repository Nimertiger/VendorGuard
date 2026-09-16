from odoo import api, fields, models


class ResPartnerBank(models.Model):
    _inherit = 'res.partner.bank'

    def write(self, vals):
        logs_vals = []
        if 'acc_number' in vals:
            new_acc = vals.get('acc_number')
            for bank in self:
                if new_acc and new_acc != bank.acc_number:
                    logs_vals.append({
                        'partner_id': bank.partner_id.id,
                        'old_acc_number': bank.acc_number,
                        'new_acc_number': new_acc,
                        'change_date': fields.Datetime.now(),
                    })
        result = super().write(vals)
        if logs_vals:
            self.env['vendorguard.bank.change.log'].sudo().create(logs_vals)
        if 'acc_number' in vals:
            self._check_shared_bank_account()
        return result

    @api.model_create_multi
    def create(self, vals_list):
        banks = super().create(vals_list)
        banks._check_shared_bank_account()
        return banks

    def _check_shared_bank_account(self):
        """Forensic-accounting pattern: two vendors receiving payment into the same bank
        account is a classic sign of a shell-company ring or vendor-employee collusion."""
        for bank in self:
            if not bank.sanitized_acc_number or not bank.partner_id:
                continue
            others = self.env['res.partner.bank'].sudo().search([
                ('sanitized_acc_number', '=', bank.sanitized_acc_number),
                ('partner_id', '!=', bank.partner_id.id),
                ('id', '!=', bank.id),
            ])
            other_partners = others.partner_id - bank.partner_id
            if not other_partners:
                continue
            all_partners = bank.partner_id | other_partners
            for partner in all_partners:
                existing = self.env['vendorguard.fraud.flag'].sudo().search([
                    ('partner_id', '=', partner.id), ('flag_type', '=', 'shared_bank_account'),
                    ('state', 'in', ('flagged', 'pending_review')),
                ], limit=1)
                if existing:
                    continue
                peers = all_partners - partner
                self.env['vendorguard.fraud.flag'].sudo().create({
                    'flag_type': 'shared_bank_account', 'severity': 'high', 'state': 'flagged',
                    'partner_id': partner.id, 'resolvable': False,
                    'description': (
                        "Vendor %s shares a bank account with %s — the same account number is "
                        "registered to multiple vendors, a common shell-company or collusion pattern."
                    ) % (partner.name, ", ".join(peers.mapped('name'))),
                })
