import logging

import requests

from odoo import fields, models

from .vendorguard_settings import API_KEY_PARAM

_logger = logging.getLogger(__name__)

OPEN_STATES = ('flagged', 'pending_review')
ANTHROPIC_API_URL = 'https://api.anthropic.com/v1/messages'
ANTHROPIC_MODEL = 'claude-sonnet-5'
ANTHROPIC_TIMEOUT = 20
ANTHROPIC_MAX_TOKENS = 400


class VendorguardAskWizard(models.TransientModel):
    _name = 'vendorguard.ask.wizard'
    _description = 'Ask VendorGuard'

    question = fields.Char()
    answer = fields.Text(readonly=True)

    def _build_context_snapshot(self):
        """Compact, factual snapshot of live vendor/flag data, handed to Claude as grounding
        so it answers from real numbers instead of inventing vendor names or figures."""
        partners = self.env['res.partner'].search(
            [('supplier_rank', '>', 0)], order='trust_score asc')
        if not partners:
            return "No vendor or fraud-flag data loaded yet. Suggest running Load Demo Scenario."
        lines = []
        for partner in partners:
            open_flags = partner.fraud_flag_ids.filtered(lambda f: f.state in OPEN_STATES)
            lines.append("- %s: trust score %d (%s), %d open flag(s)" % (
                partner.name, partner.trust_score, partner.trust_tier, len(open_flags)))
            for flag in open_flags:
                lines.append("    - [%s/%s] %s: %s" % (
                    flag.severity.upper(), flag.state,
                    dict(flag._fields['flag_type'].selection).get(flag.flag_type),
                    flag.description or ''))
        return "\n".join(lines)

    def _reopen(self):
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'vendorguard.ask.wizard',
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }

    def action_ask(self):
        self.ensure_one()
        question = (self.question or '').strip()
        if not question:
            question = "Give me a one-sentence status summary of vendor fraud risk right now."

        api_key = self.env['ir.config_parameter'].sudo().get_param(API_KEY_PARAM)
        if not api_key:
            self.answer = (
                "No Anthropic API key configured. Open the VendorGuard app's Settings menu, "
                "paste in a key from console.anthropic.com/settings/keys, then ask again."
            )
            return self._reopen()

        system_prompt = (
            "You are VendorGuard, a vendor fraud-detection assistant embedded in an Odoo "
            "accounting module. Answer the user's question using ONLY the data below -- never "
            "invent vendor names, numbers, or flags that aren't listed. If the data doesn't "
            "answer the question, say so plainly. Be concise (2-4 sentences), and speak like "
            "you're briefing a finance manager, not a generic chatbot.\n\nCurrent data:\n"
        ) + self._build_context_snapshot()

        try:
            response = requests.post(
                ANTHROPIC_API_URL,
                headers={
                    'x-api-key': api_key,
                    'anthropic-version': '2023-06-01',
                    'content-type': 'application/json',
                },
                json={
                    'model': ANTHROPIC_MODEL,
                    'max_tokens': ANTHROPIC_MAX_TOKENS,
                    'system': system_prompt,
                    'messages': [{'role': 'user', 'content': question}],
                },
                timeout=ANTHROPIC_TIMEOUT,
            )
            response.raise_for_status()
            data = response.json()
            text = ''.join(
                block.get('text', '') for block in data.get('content', [])
                if block.get('type') == 'text'
            ).strip()
            self.answer = text or "Claude returned an empty response."
        except requests.exceptions.RequestException as exc:
            _logger.warning("VendorGuard: Claude API call failed: %s", exc)
            self.answer = "Couldn't reach Claude (%s). Check your network connection and API key." % exc
        except (KeyError, ValueError, TypeError) as exc:
            _logger.warning("VendorGuard: unexpected Claude API response shape: %s", exc)
            self.answer = "Claude returned an unexpected response that couldn't be parsed."

        return self._reopen()
