from unittest.mock import MagicMock, patch

import requests

from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestAskWizard(TransactionCase):
    """Ask VendorGuard: a live Claude API call grounded in real vendor/flag data.

    The actual network call is mocked throughout -- these tests verify the
    grounding context, the request shape, and that network/parsing failures
    degrade to a clear message instead of a crash. They never hit the real
    Anthropic API (no cost, no flakiness, no key required to run the suite)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env['ir.config_parameter'].sudo().set_param('vendorguard.anthropic_api_key', False)

    def _make_vendor(self, name, **vals):
        vals.setdefault('is_company', True)
        vals.setdefault('supplier_rank', 1)
        vals['name'] = name
        return self.env['res.partner'].with_context(vendorguard_skip_lookalike=True).create(vals)

    def _ask(self, question):
        wizard = self.env['vendorguard.ask.wizard'].create({'question': question})
        wizard.action_ask()
        return wizard.answer

    def _mock_response(self, text):
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {'content': [{'type': 'text', 'text': text}]}
        return response

    def test_no_api_key_shows_setup_instructions_without_calling_network(self):
        self.env['ir.config_parameter'].sudo().set_param('vendorguard.anthropic_api_key', False)
        with patch('odoo.addons.vendorguard.models.vendorguard_ask_wizard.requests.post') as post:
            answer = self._ask('who is the riskiest vendor?')
            post.assert_not_called()
        self.assertIn('API key', answer)
        self.assertIn('Settings', answer)

    def test_successful_call_stores_claude_text_and_sends_grounded_context(self):
        vendor = self._make_vendor('VG Ask Wizard Grounding Vendor')
        self.env['vendorguard.fraud.flag'].create({
            'flag_type': 'ghost_vendor', 'severity': 'critical', 'state': 'flagged',
            'partner_id': vendor.id, 'resolvable': False, 'description': 'unmistakable marker 12345',
        })
        self.env['ir.config_parameter'].sudo().set_param('vendorguard.anthropic_api_key', 'sk-test-key')
        with patch('odoo.addons.vendorguard.models.vendorguard_ask_wizard.requests.post') as post:
            post.return_value = self._mock_response('The riskiest vendor is the one you just flagged.')
            answer = self._ask('who is the riskiest vendor?')
            self.assertTrue(post.called)
            kwargs = post.call_args.kwargs
            self.assertEqual(kwargs['headers']['x-api-key'], 'sk-test-key')
            self.assertIn(vendor.name, kwargs['json']['system'])
            self.assertIn('unmistakable marker 12345', kwargs['json']['system'])
            self.assertEqual(kwargs['json']['messages'][0]['content'], 'who is the riskiest vendor?')
        self.assertEqual(answer, 'The riskiest vendor is the one you just flagged.')

    def test_network_failure_shows_friendly_message_not_a_crash(self):
        self.env['ir.config_parameter'].sudo().set_param('vendorguard.anthropic_api_key', 'sk-test-key')
        with patch('odoo.addons.vendorguard.models.vendorguard_ask_wizard.requests.post') as post:
            post.side_effect = requests.exceptions.ConnectionError('no route to host')
            answer = self._ask('who is the riskiest vendor?')
        self.assertIn("Couldn't reach Claude", answer)

    def test_malformed_response_handled_gracefully(self):
        self.env['ir.config_parameter'].sudo().set_param('vendorguard.anthropic_api_key', 'sk-test-key')
        with patch('odoo.addons.vendorguard.models.vendorguard_ask_wizard.requests.post') as post:
            response = MagicMock()
            response.raise_for_status.return_value = None
            response.json.return_value = {'unexpected': 'shape'}
            post.return_value = response
            answer = self._ask('who is the riskiest vendor?')
        self.assertIn('empty response', answer)

    def test_empty_question_defaults_to_a_status_summary_prompt(self):
        self.env['ir.config_parameter'].sudo().set_param('vendorguard.anthropic_api_key', 'sk-test-key')
        with patch('odoo.addons.vendorguard.models.vendorguard_ask_wizard.requests.post') as post:
            post.return_value = self._mock_response('All clear.')
            self._ask('')
            sent_question = post.call_args.kwargs['json']['messages'][0]['content']
        self.assertIn('summary', sent_question.lower())

    def test_context_snapshot_lists_real_vendor_data(self):
        vendor = self._make_vendor('VG Snapshot Content Vendor', vat='AE100000000000077')
        wizard = self.env['vendorguard.ask.wizard'].create({})
        snapshot = wizard._build_context_snapshot()
        self.assertIn(vendor.name, snapshot)
        self.assertIn(str(vendor.trust_score), snapshot)
