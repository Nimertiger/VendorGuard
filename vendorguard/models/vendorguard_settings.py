from odoo import api, fields, models

API_KEY_PARAM = 'vendorguard.anthropic_api_key'
WORKSPACE_ID_PARAM = 'vendorguard.anthropic_workspace_id'


class VendorguardSettings(models.TransientModel):
    _name = 'vendorguard.settings'
    _description = 'VendorGuard Settings'

    anthropic_api_key = fields.Char(
        string='Anthropic API Key',
        help="Used by Ask VendorGuard to call Claude. Get one at "
             "console.anthropic.com/settings/keys.")
    anthropic_workspace_id = fields.Char(
        string='Anthropic Workspace ID',
        help="Only needed if your API key is organization-scoped rather than tied to one "
             "workspace -- Claude will reject requests with 'not scoped to a workspace' "
             "until this is set. Find it at console.anthropic.com under your workspace's "
             "settings. Leave blank if your key already works without it.")

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        params = self.env['ir.config_parameter'].sudo()
        if 'anthropic_api_key' in fields_list:
            res['anthropic_api_key'] = params.get_param(API_KEY_PARAM, '')
        if 'anthropic_workspace_id' in fields_list:
            res['anthropic_workspace_id'] = params.get_param(WORKSPACE_ID_PARAM, '')
        return res

    def action_save(self):
        self.ensure_one()
        params = self.env['ir.config_parameter'].sudo()
        params.set_param(API_KEY_PARAM, self.anthropic_api_key or '')
        params.set_param(WORKSPACE_ID_PARAM, self.anthropic_workspace_id or '')
        return {'type': 'ir.actions.act_window_close'}
