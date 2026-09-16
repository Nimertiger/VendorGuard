from odoo import fields
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestAskWizard(TransactionCase):
    """Ask VendorGuard: templated, zero-network Q&A over real fraud-flag data."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.journal = cls.env['account.journal'].search([('type', '=', 'purchase')], limit=1)

    def _make_vendor(self, name, **vals):
        vals.setdefault('is_company', True)
        vals.setdefault('supplier_rank', 1)
        vals['name'] = name
        return self.env['res.partner'].with_context(vendorguard_skip_lookalike=True).create(vals)

    def _ask(self, question, ctx=None):
        wizard = self.env['vendorguard.ask.wizard'].with_context(**(ctx or {})).create({'question': question})
        wizard.action_ask()
        return wizard.answer

    def test_riskiest_vendor_picks_lowest_trust_score(self):
        good = self._make_vendor('VG Wizard Good Vendor')
        bad = self._make_vendor('VG Wizard Bad Vendor')
        # stack enough unresolved critical flags to guarantee a trust score of 0, so this
        # vendor is unambiguously the worst regardless of whatever else exists in the DB
        for i in range(3):
            self.env['vendorguard.fraud.flag'].create({
                'flag_type': 'ghost_vendor', 'severity': 'critical', 'state': 'flagged',
                'partner_id': bad.id, 'resolvable': False, 'description': 'test flag %d' % i,
            })
        self.assertEqual(bad.trust_score, 0)
        answer = self._ask('who is the riskiest vendor?')
        self.assertIn(bad.name, answer)
        self.assertNotIn(good.name, answer)

    def test_partial_vendor_name_resolves_to_full_name(self):
        vendor = self._make_vendor('VG Zephyrion Holdings FZE')
        answer = self._ask('is Zephyrion safe?')
        self.assertIn(vendor.name, answer)

    def test_why_resolves_from_active_flag_context(self):
        vendor = self._make_vendor('VG Why Context Vendor')
        flag = self.env['vendorguard.fraud.flag'].create({
            'flag_type': 'ghost_vendor', 'severity': 'medium', 'state': 'flagged',
            'partner_id': vendor.id, 'resolvable': False,
            'description': 'unmistakable test description marker 12345',
        })
        answer = self._ask('why is this flagged?', ctx={
            'active_model': 'vendorguard.fraud.flag', 'active_id': flag.id})
        self.assertEqual(answer, flag.description)

    def test_why_resolves_from_vendor_name_without_context(self):
        vendor = self._make_vendor('VG Why Named Vendor')
        flag = self.env['vendorguard.fraud.flag'].create({
            'flag_type': 'ghost_vendor', 'severity': 'medium', 'state': 'flagged',
            'partner_id': vendor.id, 'resolvable': False,
            'description': 'a distinctive description for this vendor only',
        })
        answer = self._ask('why is VG Why Named Vendor flagged')
        self.assertEqual(answer, flag.description)

    def test_critical_flags_lists_only_high_severity(self):
        low_vendor = self._make_vendor('VG Low Severity Vendor')
        high_vendor = self._make_vendor('VG High Severity Vendor')
        self.env['vendorguard.fraud.flag'].create({
            'flag_type': 'lookalike_vendor', 'severity': 'low', 'state': 'flagged',
            'partner_id': low_vendor.id, 'resolvable': False, 'description': 'low',
        })
        self.env['vendorguard.fraud.flag'].create({
            'flag_type': 'ghost_vendor', 'severity': 'critical', 'state': 'flagged',
            'partner_id': high_vendor.id, 'resolvable': False, 'description': 'high',
        })
        answer = self._ask('show me critical flags')
        self.assertIn(high_vendor.name, answer)
        self.assertNotIn(low_vendor.name, answer)

    def test_how_many_flags_counts_open_only(self):
        vendor = self._make_vendor('VG Count Vendor')
        flag = self.env['vendorguard.fraud.flag'].create({
            'flag_type': 'ghost_vendor', 'severity': 'medium', 'state': 'flagged',
            'partner_id': vendor.id, 'resolvable': False, 'description': 'x',
        })
        before = self._ask('how many flags are open?')
        count_before = int(before.split()[2])
        flag.with_context(vendorguard_internal_state_change=True).state = 'approved'
        after = self._ask('how many flags are open?')
        count_after = int(after.split()[2])
        self.assertEqual(count_after, count_before - 1)

    def test_unrecognized_question_falls_back_to_real_snapshot_not_help_menu(self):
        answer = self._ask('what is the meaning of life')
        self.assertNotIn('I can answer', answer)
        self.assertNotIn('capabilities', answer.lower())

    def test_empty_question_returns_snapshot(self):
        answer = self._ask('')
        self.assertTrue(answer)
