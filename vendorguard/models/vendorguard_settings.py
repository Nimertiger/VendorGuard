from odoo import api, fields, models

API_KEY_PARAM = 'vendorguard.anthropic_api_key'


class VendorguardSettings(models.TransientModel):
    _name = 'vendorguard.settings'
    _description = 'VendorGuard Settings'

    anthropic_api_key = fields.Char(
        string='Anthropic API Key',
        help="Used by Ask VendorGuard to call Claude. Get one at "
             "console.anthropic.com/settings/keys.")

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        if 'anthropic_api_key' in fields_list:
            res['anthropic_api_key'] = self.env['ir.config_parameter'].sudo().get_param(
                API_KEY_PARAM, '')
        return res

    def action_save(self):
        self.ensure_one()
        self.env['ir.config_parameter'].sudo().set_param(
            API_KEY_PARAM, self.anthropic_api_key or '')
        return {'type': 'ir.actions.act_window_close'}
