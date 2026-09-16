from odoo import fields
from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestFraudChecks(TransactionCase):
    """Covers the rule-based and identity fraud checks, the approval workflow,
    and the trust-score computation. Each test creates its own vendor so tests
    never interfere with each other or with real/demo data."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.journal = cls.env['account.journal'].search([('type', '=', 'purchase')], limit=1)
        cls.product = cls.env['product.product'].create({
            'name': 'VG Test Product', 'type': 'consu', 'purchase_ok': True, 'sale_ok': False,
        })
        cls.twm_product = cls.env['product.product'].create({
            'name': 'VG Test Product (Ordered Policy)', 'type': 'consu',
            'purchase_ok': True, 'sale_ok': False, 'purchase_method': 'purchase',
        })
        cls.manager_group = cls.env.ref('vendorguard.group_finance_manager')
        cls.account_invoice_group = cls.env.ref('account.group_account_invoice')
        cls.purchase_user_group = cls.env.ref('purchase.group_purchase_user')
        cls.manager_user = cls.env['res.users'].create({
            'name': 'VG Test Finance Manager', 'login': 'vg_test_finance_manager',
            'group_ids': [(4, cls.manager_group.id), (4, cls.account_invoice_group.id),
                          (4, cls.purchase_user_group.id)],
        })
        cls.regular_user = cls.env['res.users'].create({
            'name': 'VG Test Regular User', 'login': 'vg_test_regular_user',
        })

    def _make_vendor(self, name, **vals):
        vals.setdefault('is_company', True)
        vals.setdefault('supplier_rank', 1)
        vals['name'] = name
        return self.env['res.partner'].with_context(vendorguard_skip_lookalike=True).create(vals)

    def _post_bill(self, vendor, amount, ref, invoice_date=None, seeding=False):
        move = self.env['account.move'].create({
            'move_type': 'in_invoice', 'partner_id': vendor.id,
            'invoice_date': invoice_date or fields.Date.today(),
            'ref': ref, 'journal_id': self.journal.id,
            'invoice_line_ids': [(0, 0, {'name': ref, 'quantity': 1, 'price_unit': amount})],
        })
        if seeding:
            move = move.with_context(vendorguard_seeding=True)
        move.action_post()
        return move

    # --- duplicate_bill: hard @api.constrains block, no persisted flag row ---

    def test_duplicate_bill_is_hard_blocked(self):
        vendor = self._make_vendor('VG Duplicate Test Vendor')
        self._post_bill(vendor, 1000.0, 'DUP-001', seeding=True)
        with self.assertRaises(ValidationError):
            self._post_bill(vendor, 1000.0, 'DUP-001', seeding=True)

    def test_duplicate_bill_ignores_blank_ref(self):
        vendor = self._make_vendor('VG Duplicate Blank Ref Vendor')
        self._post_bill(vendor, 1000.0, '', seeding=True)
        # a second blank-ref bill must NOT be treated as a duplicate of the first
        self._post_bill(vendor, 1000.0, '', seeding=True)

    # --- bank_swap: soft block (non-raising), creates a critical flag ---

    def test_bank_swap_blocks_and_flags(self):
        vendor = self._make_vendor('VG Bank Swap Test Vendor')
        bank = self.env['res.partner.bank'].create(
            {'partner_id': vendor.id, 'acc_number': 'AE070000000000001'})
        bank.write({'acc_number': 'AE070000000000002'})
        move = self._post_bill(vendor, 500.0, 'BS-001')
        self.assertEqual(move.state, 'draft', "blocked bill must stay in draft, not raise")
        flag = self.env['vendorguard.fraud.flag'].search([
            ('move_id', '=', move.id), ('flag_type', '=', 'bank_swap')])
        self.assertTrue(flag)
        self.assertEqual(flag.severity, 'critical')
        self.assertTrue(flag.resolvable)

    def test_bank_swap_does_not_fire_outside_recent_window(self):
        vendor = self._make_vendor('VG Old Bank Swap Vendor')
        bank = self.env['res.partner.bank'].create(
            {'partner_id': vendor.id, 'acc_number': 'AE070000000000010'})
        bank.write({'acc_number': 'AE070000000000011'})
        from dateutil.relativedelta import relativedelta
        log = self.env['vendorguard.bank.change.log'].sudo().search(
            [('partner_id', '=', vendor.id)], limit=1)
        log.change_date = fields.Datetime.now() - relativedelta(days=30)
        move = self._post_bill(vendor, 500.0, 'BS-OLD-001')
        flag = self.env['vendorguard.fraud.flag'].search([
            ('move_id', '=', move.id), ('flag_type', '=', 'bank_swap')])
        self.assertFalse(flag, "a bank change 30 days ago is outside the recency window")

    # --- segregation_of_duties ---

    def test_segregation_of_duties_flag(self):
        vendor = self._make_vendor('VG SoD Test Vendor')
        move = self._post_bill(vendor, 250.0, 'SOD-001')
        self.assertEqual(move.state, 'draft')
        flag = self.env['vendorguard.fraud.flag'].search([
            ('move_id', '=', move.id), ('flag_type', '=', 'segregation_of_duties')])
        self.assertTrue(flag)
        self.assertEqual(flag.severity, 'medium')

    # --- approval workflow: security boundary + full block -> approve -> post loop ---

    def test_non_manager_cannot_approve(self):
        vendor = self._make_vendor('VG Access Test Vendor')
        move = self._post_bill(vendor, 300.0, 'ACC-001')
        flags = self.env['vendorguard.fraud.flag'].search([('move_id', '=', move.id)])
        self.assertTrue(flags)
        with self.assertRaises(AccessError):
            flags.with_user(self.regular_user).action_approve()

    def test_approve_then_post_succeeds(self):
        vendor = self._make_vendor('VG Approve Flow Vendor')
        move = self._post_bill(vendor, 300.0, 'APR-001')
        self.assertEqual(move.state, 'draft')
        flags = self.env['vendorguard.fraud.flag'].search([('move_id', '=', move.id)])
        self.assertTrue(flags)
        flags.with_user(self.manager_user).action_approve()
        self.assertTrue(all(f.state == 'approved' for f in flags))
        move.with_user(self.manager_user).action_post()
        self.assertEqual(move.state, 'posted')

    def test_approve_single_nonresolvable_flag_raises(self):
        from odoo.exceptions import UserError
        vendor = self._make_vendor('VG Nonresolvable Approve Vendor')
        po = self.env['purchase.order'].create({
            'partner_id': vendor.id,
            'order_line': [(0, 0, {
                'product_id': self.product.id, 'name': 'x', 'product_qty': 1, 'price_unit': 8000.0})],
        })
        po.button_confirm()
        po2 = self.env['purchase.order'].create({
            'partner_id': vendor.id,
            'order_line': [(0, 0, {
                'product_id': self.product.id, 'name': 'y', 'product_qty': 1, 'price_unit': 8000.0})],
        })
        po2.button_confirm()
        flag = self.env['vendorguard.fraud.flag'].search([('purchase_order_id', '=', po2.id)])
        self.assertFalse(flag.resolvable)
        with self.assertRaises(UserError):
            flag.with_user(self.manager_user).action_approve()

    def test_approve_batch_skips_nonresolvable_without_aborting(self):
        vendor = self._make_vendor('VG Mixed Batch Vendor')
        move = self._post_bill(vendor, 100.0, 'MIX-001')  # resolvable segregation_of_duties flag
        resolvable_flag = self.env['vendorguard.fraud.flag'].search([('move_id', '=', move.id)])
        self.assertTrue(resolvable_flag)
        po = self.env['purchase.order'].create({
            'partner_id': vendor.id,
            'order_line': [(0, 0, {
                'product_id': self.product.id, 'name': 'x', 'product_qty': 1, 'price_unit': 8000.0})],
        })
        po.button_confirm()
        po2 = self.env['purchase.order'].create({
            'partner_id': vendor.id,
            'order_line': [(0, 0, {
                'product_id': self.product.id, 'name': 'y', 'product_qty': 1, 'price_unit': 8000.0})],
        })
        po2.button_confirm()
        nonresolvable_flag = self.env['vendorguard.fraud.flag'].search([('purchase_order_id', '=', po2.id)])
        self.assertFalse(nonresolvable_flag.resolvable)

        batch = resolvable_flag | nonresolvable_flag
        batch.with_user(self.manager_user).action_approve()  # must not raise
        self.assertEqual(resolvable_flag.state, 'approved')
        self.assertEqual(nonresolvable_flag.state, 'flagged', "non-resolvable flag must be left untouched")

    def test_direct_state_write_blocked_for_non_manager(self):
        vendor = self._make_vendor('VG Direct Write Test Vendor')
        move = self._post_bill(vendor, 300.0, 'DW-001')
        flag = self.env['vendorguard.fraud.flag'].search([('move_id', '=', move.id)], limit=1)
        with self.assertRaises(AccessError):
            flag.with_user(self.regular_user).write({'state': 'approved'})

    def test_regular_user_cannot_edit_flag_fields_at_all(self):
        # perm_write is granted only to Finance Managers at the ACL level — a regular user
        # should not be able to quietly edit severity/description to downgrade a flag either
        vendor = self._make_vendor('VG ACL Write Test Vendor')
        move = self._post_bill(vendor, 300.0, 'ACL-001')
        flag = self.env['vendorguard.fraud.flag'].search([('move_id', '=', move.id)], limit=1)
        with self.assertRaises(AccessError):
            flag.with_user(self.regular_user).write({'severity': 'low'})

    def test_manager_can_still_write_flag_fields(self):
        vendor = self._make_vendor('VG Manager Write Test Vendor')
        move = self._post_bill(vendor, 300.0, 'MGR-WRITE-001')
        flag = self.env['vendorguard.fraud.flag'].search([('move_id', '=', move.id)], limit=1)
        flag.with_user(self.manager_user).write({'description': 'reviewed and annotated'})
        self.assertEqual(flag.description, 'reviewed and annotated')

    # --- structuring: sub-threshold POs that sum over the limit ---

    def test_structuring_blocks_over_threshold(self):
        vendor = self._make_vendor('VG Structuring Test Vendor')
        po1 = self.env['purchase.order'].create({
            'partner_id': vendor.id,
            'order_line': [(0, 0, {
                'product_id': self.product.id, 'name': 'batch 1', 'product_qty': 1, 'price_unit': 8000.0})],
        })
        po1.button_confirm()
        self.assertEqual(po1.state, 'purchase')

        po2 = self.env['purchase.order'].create({
            'partner_id': vendor.id,
            'order_line': [(0, 0, {
                'product_id': self.product.id, 'name': 'batch 2', 'product_qty': 1, 'price_unit': 8000.0})],
        })
        po2.button_confirm()
        self.assertEqual(po2.state, 'draft', "second PO should be blocked, not confirmed")
        flag = self.env['vendorguard.fraud.flag'].search([('purchase_order_id', '=', po2.id)])
        self.assertTrue(flag)
        self.assertFalse(flag.resolvable, "structuring is a structural issue, not approvable")

    def test_structuring_retry_does_not_duplicate_flag(self):
        vendor = self._make_vendor('VG Structuring Retry Vendor')
        po1 = self.env['purchase.order'].create({
            'partner_id': vendor.id,
            'order_line': [(0, 0, {
                'product_id': self.product.id, 'name': 'batch 1', 'product_qty': 1, 'price_unit': 8000.0})],
        })
        po1.button_confirm()
        po2 = self.env['purchase.order'].create({
            'partner_id': vendor.id,
            'order_line': [(0, 0, {
                'product_id': self.product.id, 'name': 'batch 2', 'product_qty': 1, 'price_unit': 8000.0})],
        })
        po2.button_confirm()
        po2.button_confirm()  # retry — must not create a second flag
        flags = self.env['vendorguard.fraud.flag'].search([('purchase_order_id', '=', po2.id)])
        self.assertEqual(len(flags), 1)

    def test_structuring_does_not_fire_under_threshold(self):
        vendor = self._make_vendor('VG No Structuring Vendor')
        po = self.env['purchase.order'].create({
            'partner_id': vendor.id,
            'order_line': [(0, 0, {
                'product_id': self.product.id, 'name': 'small order', 'product_qty': 1, 'price_unit': 1000.0})],
        })
        po.button_confirm()
        self.assertEqual(po.state, 'purchase')

    # --- lookalike_vendor: informational, does not block creation ---

    def test_lookalike_vendor_flag(self):
        self._make_vendor('VG Acme Trading LLC')
        lookalike = self.env['res.partner'].create({
            'name': 'VG Acme Tradin LLC', 'is_company': True, 'supplier_rank': 1,
        })
        flag = self.env['vendorguard.fraud.flag'].search([
            ('partner_id', '=', lookalike.id), ('flag_type', '=', 'lookalike_vendor')])
        self.assertTrue(flag, "a near-identical vendor name should raise a lookalike flag")
        self.assertFalse(flag.resolvable)

    def test_lookalike_check_ignores_non_vendor_companies(self):
        # a company that only appears as a customer (supplier_rank = 0) must never be used
        # as a comparison target — this check exists to catch vendor impersonation, not
        # coincidental name overlap with unrelated customers
        self.env['res.partner'].create({
            'name': 'VG Customer Only Co', 'is_company': True, 'supplier_rank': 0,
        })
        lookalike = self._make_vendor('VG Customer Only Corp')
        flag = self.env['vendorguard.fraud.flag'].search([
            ('partner_id', '=', lookalike.id), ('flag_type', '=', 'lookalike_vendor')])
        self.assertFalse(flag, "must not flag a name match against a non-vendor company")

    def test_dissimilar_vendor_name_not_flagged(self):
        self._make_vendor('VG Totally Different Co')
        other = self.env['res.partner'].create({
            'name': 'VG Another Unrelated Business', 'is_company': True, 'supplier_rank': 1,
        })
        flag = self.env['vendorguard.fraud.flag'].search([
            ('partner_id', '=', other.id), ('flag_type', '=', 'lookalike_vendor')])
        self.assertFalse(flag)

    # --- ghost_vendor: informational only, never blocks posting on its own ---

    def test_ghost_vendor_flag_created(self):
        vendor = self._make_vendor('VG Ghost Test Vendor')  # no VAT, no bank account
        self._post_bill(vendor, 100.0, 'GHOST-001')
        flag = self.env['vendorguard.fraud.flag'].search([
            ('partner_id', '=', vendor.id), ('flag_type', '=', 'ghost_vendor')])
        self.assertTrue(flag)
        self.assertFalse(flag.resolvable)
        self.assertIsNone(flag.move_id.id if flag.move_id else None,
                           "ghost_vendor is vendor-level, not tied to a specific bill")

    def test_ghost_vendor_not_flagged_with_vat_and_bank(self):
        vendor = self._make_vendor('VG Legit Vendor', vat='AE123456789012345')
        self.env['res.partner.bank'].create(
            {'partner_id': vendor.id, 'acc_number': 'AE070000000000099'})
        self._post_bill(vendor, 100.0, 'LEGIT-001')
        flag = self.env['vendorguard.fraud.flag'].search([
            ('partner_id', '=', vendor.id), ('flag_type', '=', 'ghost_vendor')])
        self.assertFalse(flag)

    # --- three_way_match: cross-module check against purchase.order.line ---

    def test_three_way_match_mismatch_blocks(self):
        vendor = self._make_vendor('VG Three Way Match Vendor')
        po = self.env['purchase.order'].create({
            'partner_id': vendor.id,
            'order_line': [(0, 0, {
                'product_id': self.twm_product.id, 'name': 'rebar', 'product_qty': 20, 'price_unit': 100.0})],
        })
        po.button_confirm()
        po.order_line.qty_received_manual = 12.0
        po.action_create_invoice()
        bill = po.invoice_ids.filtered(lambda m: m.state == 'draft')
        bill.invoice_date = fields.Date.today()
        bill.action_post()
        self.assertEqual(bill.state, 'draft')
        flag = self.env['vendorguard.fraud.flag'].search([
            ('move_id', '=', bill.id), ('flag_type', '=', 'three_way_match')])
        self.assertTrue(flag)
        self.assertIn('billed 20.00 vs received 12.00', flag.description)

    def test_three_way_match_does_not_fire_when_fully_received(self):
        vendor = self._make_vendor('VG Full Receipt Vendor')
        po = self.env['purchase.order'].create({
            'partner_id': vendor.id,
            'order_line': [(0, 0, {
                'product_id': self.twm_product.id, 'name': 'rebar', 'product_qty': 10, 'price_unit': 100.0})],
        })
        po.button_confirm()
        po.order_line.qty_received_manual = 10.0
        po.action_create_invoice()
        bill = po.invoice_ids.filtered(lambda m: m.state == 'draft')
        bill.invoice_date = fields.Date.today()
        flag_before = self.env['vendorguard.fraud.flag'].search_count([
            ('move_id', '=', bill.id), ('flag_type', '=', 'three_way_match')])
        self.assertEqual(flag_before, 0)

    # --- shared_bank_account: two vendors, one bank account ---

    def test_shared_bank_account_flags_both_vendors(self):
        v1 = self._make_vendor('VG Shell Co A')
        v2 = self._make_vendor('VG Shell Co B')
        self.env['res.partner.bank'].create({'partner_id': v1.id, 'acc_number': 'AE070000000000077'})
        self.env['res.partner.bank'].create({'partner_id': v2.id, 'acc_number': 'AE070000000000077'})
        flag1 = self.env['vendorguard.fraud.flag'].search([
            ('partner_id', '=', v1.id), ('flag_type', '=', 'shared_bank_account')])
        flag2 = self.env['vendorguard.fraud.flag'].search([
            ('partner_id', '=', v2.id), ('flag_type', '=', 'shared_bank_account')])
        self.assertTrue(flag1)
        self.assertTrue(flag2)
        self.assertIn(v2.name, flag1.description)
        self.assertIn(v1.name, flag2.description)

    def test_distinct_bank_accounts_not_flagged(self):
        v1 = self._make_vendor('VG Distinct Bank Co A')
        v2 = self._make_vendor('VG Distinct Bank Co B')
        self.env['res.partner.bank'].create({'partner_id': v1.id, 'acc_number': 'AE070000000000088'})
        self.env['res.partner.bank'].create({'partner_id': v2.id, 'acc_number': 'AE070000000000099'})
        flag = self.env['vendorguard.fraud.flag'].search([
            ('partner_id', '=', v1.id), ('flag_type', '=', 'shared_bank_account')])
        self.assertFalse(flag)

    # --- trust score computation ---

    # --- multi-company scoping ---

    def test_flag_company_id_matches_source_document(self):
        vendor = self._make_vendor('VG Company Scope Vendor')
        move = self._post_bill(vendor, 100.0, 'COMP-001')
        flag = self.env['vendorguard.fraud.flag'].search([('move_id', '=', move.id)], limit=1)
        self.assertEqual(flag.company_id, move.company_id)

    def test_flag_invisible_across_companies(self):
        other_company = self.env['res.company'].create({'name': 'VG Other Company'})
        vendor = self._make_vendor('VG Cross Company Vendor')
        flag = self.env['vendorguard.fraud.flag'].create({
            'flag_type': 'ghost_vendor', 'severity': 'medium', 'state': 'flagged',
            'partner_id': vendor.id, 'resolvable': False, 'description': 'x',
            'company_id': other_company.id,
        })
        cross_company_user = self.env['res.users'].create({
            'name': 'VG Cross Company User', 'login': 'vg_cross_company_user',
            'company_ids': [(6, 0, self.env.company.ids)],
            'company_id': self.env.company.id,
        })
        visible = self.env['vendorguard.fraud.flag'].with_user(cross_company_user).search(
            [('id', '=', flag.id)])
        self.assertFalse(visible, "a flag in another company must not be visible")

    def test_trust_score_starts_at_100_with_no_flags(self):
        vendor = self._make_vendor('VG Clean Vendor')
        self.assertEqual(vendor.trust_score, 100)
        self.assertEqual(vendor.trust_tier, 'safe')

    def test_trust_score_drops_with_unresolved_flags(self):
        # vat + bank set so ghost_vendor doesn't also fire — isolates this to segregation_of_duties
        vendor = self._make_vendor('VG Penalized Vendor', vat='AE100000000000001')
        self.env['res.partner.bank'].create(
            {'partner_id': vendor.id, 'acc_number': 'AE070000000000201'})
        self._post_bill(vendor, 100.0, 'PEN-001')  # segregation_of_duties, medium, -10
        self.assertEqual(vendor.trust_score, 90)
        self.assertEqual(vendor.trust_tier, 'safe')

    def test_trust_score_ignores_approved_flags(self):
        vendor = self._make_vendor('VG Resolved Vendor', vat='AE100000000000002')
        self.env['res.partner.bank'].create(
            {'partner_id': vendor.id, 'acc_number': 'AE070000000000202'})
        move = self._post_bill(vendor, 100.0, 'RES-001')
        flags = self.env['vendorguard.fraud.flag'].search([('move_id', '=', move.id)])
        flags.with_user(self.manager_user).action_approve()
        self.assertEqual(vendor.trust_score, 100, "approved flags must not count against trust score")

    def test_trust_score_floors_at_zero(self):
        vendor = self._make_vendor('VG Worst Vendor')
        bank = self.env['res.partner.bank'].create(
            {'partner_id': vendor.id, 'acc_number': 'AE070000000000055'})
        bank.write({'acc_number': 'AE070000000000056'})  # critical, -40
        self._post_bill(vendor, 100.0, 'WORST-001')  # bank_swap -40, segregation -10, bank recency -20
        self.assertGreaterEqual(vendor.trust_score, 0)
        self.assertLessEqual(vendor.trust_score, 100)
