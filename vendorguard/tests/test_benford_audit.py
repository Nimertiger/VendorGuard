from dateutil.relativedelta import relativedelta

from odoo import fields
from odoo.tests.common import TransactionCase, tagged

from ..models.vendorguard_constants import BENFORD_MIN_SAMPLE_SIZE


@tagged('post_install', '-at_install')
class TestBenfordAudit(TransactionCase):
    """Statistical audit: Benford's Law (first-digit MAD test) plus the
    chi-square goodness-of-fit test run alongside it."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.journal = cls.env['account.journal'].search([('type', '=', 'purchase')], limit=1)

    def _post_bill(self, vendor, amount, ref, invoice_date):
        move = self.env['account.move'].create({
            'move_type': 'in_invoice', 'partner_id': vendor.id,
            'invoice_date': invoice_date, 'ref': ref, 'journal_id': self.journal.id,
            'invoice_line_ids': [(0, 0, {'name': ref, 'quantity': 1, 'price_unit': amount})],
        })
        move.with_context(vendorguard_seeding=True).action_post()
        return move

    def _seed_vendor(self, name, amounts):
        vendor = self.env['res.partner'].with_context(vendorguard_skip_lookalike=True).create(
            {'name': name, 'is_company': True, 'supplier_rank': 1})
        for i, amount in enumerate(amounts):
            self._post_bill(vendor, amount, '%s-%03d' % (name[:6], i),
                             fields.Date.today() - relativedelta(days=i))
        return vendor

    def test_conforming_distribution_passes(self):
        Scenario = self.env['vendorguard.demo.scenario']
        amounts = Scenario._benford_conforming_amounts(40)
        vendor = self._seed_vendor('VG Benford Conforming', amounts)
        result = vendor.action_run_benford_audit()
        message = result['params']['message']
        self.assertIn('Conforms to Benford', message)
        flag = self.env['vendorguard.fraud.flag'].search([
            ('partner_id', '=', vendor.id), ('flag_type', '=', 'benford_anomaly')])
        self.assertFalse(flag)

    def test_nonconforming_distribution_flags(self):
        # heavily round-number amounts: classic fabricated-invoice pattern, fails Benford hard
        amounts = [500.0, 1000.0, 1500.0, 2000.0, 5000.0] * 8
        vendor = self._seed_vendor('VG Benford Nonconforming', amounts)
        result = vendor.action_run_benford_audit()
        message = result['params']['message']
        self.assertIn('NONCONFORMITY', message)
        self.assertIn('chi-square', message)
        flag = self.env['vendorguard.fraud.flag'].search([
            ('partner_id', '=', vendor.id), ('flag_type', '=', 'benford_anomaly')])
        self.assertTrue(flag)
        self.assertEqual(flag.severity, 'high')

    def test_rerunning_audit_does_not_duplicate_flag(self):
        amounts = [500.0, 1000.0, 1500.0, 2000.0, 5000.0] * 8
        vendor = self._seed_vendor('VG Benford Rerun Vendor', amounts)
        vendor.action_run_benford_audit()
        vendor.action_run_benford_audit()
        flags = self.env['vendorguard.fraud.flag'].search([
            ('partner_id', '=', vendor.id), ('flag_type', '=', 'benford_anomaly')])
        self.assertEqual(len(flags), 1)

    def test_insufficient_sample_size_reports_honestly(self):
        vendor = self._seed_vendor('VG Benford Too Small', [100.0, 200.0, 300.0])
        result = vendor.action_run_benford_audit()
        message = result['params']['message']
        self.assertIn('at least %d' % BENFORD_MIN_SAMPLE_SIZE, message)
        flag = self.env['vendorguard.fraud.flag'].search([
            ('partner_id', '=', vendor.id), ('flag_type', '=', 'benford_anomaly')])
        self.assertFalse(flag, "must never fabricate a result below the minimum sample size")
