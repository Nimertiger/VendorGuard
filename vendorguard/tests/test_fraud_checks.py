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
        # a normal AP clerk: can create/post bills and confirm POs, but is NOT a Finance
        # Manager -- this is the persona that exposed the create()/activity_schedule() bugs,
        # since TransactionCase's default self.env runs as superuser and never hit them
        cls.clerk_user = cls.env['res.users'].create({
            'name': 'VG Test AP Clerk', 'login': 'vg_test_ap_clerk',
            'group_ids': [(4, cls.account_invoice_group.id), (4, cls.purchase_user_group.id)],
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

    def test_cron_recompute_expires_stale_bank_change_penalty(self):
        # regression: trust_score's stored compute depends on bank_change_log_ids.change_date,
        # which never itself changes -- so the -20 "recent bank change" penalty stayed applied
        # forever past the recency window unless *something* wrote to the log or a flag.
        # Backdating via raw SQL (not an ORM write) simulates real time passing with no such
        # write, which is exactly the scenario the cron exists to fix.
        vendor = self._make_vendor('VG Cron Recompute Vendor')
        bank = self.env['res.partner.bank'].create(
            {'partner_id': vendor.id, 'acc_number': 'AE070000000000401'})
        bank.write({'acc_number': 'AE070000000000402'})
        self.assertEqual(vendor.trust_score, 80, "fresh bank change costs -20")
        log = self.env['vendorguard.bank.change.log'].sudo().search(
            [('partner_id', '=', vendor.id)], limit=1)
        from dateutil.relativedelta import relativedelta
        old_date = fields.Datetime.now() - relativedelta(days=30)
        self.env.cr.execute(
            "UPDATE vendorguard_bank_change_log SET change_date = %s WHERE id = %s",
            (old_date, log.id))
        # No invalidate here: this is the point of the test. A stored computed field's
        # cached/DB value has no reason to move just because a raw SQL write happened with
        # no ORM-level trigger behind it -- exactly what "time passes, nothing recomputes"
        # looks like in practice.
        self.assertEqual(vendor.trust_score, 80, "stored field stays stale with no recompute trigger")
        # the log's own change_date IS stale in cache at this point (last read before the
        # backdate) -- invalidate just that so the cron's recompute sees the real DB value
        log.invalidate_recordset(['change_date'])
        self.env['res.partner']._cron_recompute_trust_scores()
        self.assertEqual(vendor.trust_score, 100, "cron must expire the now-30-day-old penalty")

    # --- regressions found by an independent judge-agent review pass ---

    def test_non_manager_cannot_create_flag_with_non_default_state(self):
        # a regular employee had no create()-time state check (only write() was guarded),
        # so they could fabricate a flag that already reads 'Approved' in one create() call,
        # bypassing the review workflow and polluting the audit trail. Now backstopped at two
        # layers: perm_create=0 for base.group_user at the ACL, and this Python guard for
        # any sudo'd caller that forgets to pass state='flagged'.
        vendor = self._make_vendor('VG Fabricated Approval Vendor')
        with self.assertRaises(AccessError):
            self.env['vendorguard.fraud.flag'].with_user(self.regular_user).create({
                'flag_type': 'ghost_vendor', 'severity': 'medium', 'state': 'approved',
                'partner_id': vendor.id, 'resolvable': False, 'description': 'x',
            })

    def test_non_manager_cannot_create_flag_at_all(self):
        # base.group_user has perm_create=0 on this model entirely now -- a regular employee
        # can no longer create a flag directly (with any state), only the detector code can,
        # and only via sudo(). A forged flag on a colleague's bill used to be one create()
        # call away for any employee; see test_flag_forgery_blocked_for_non_manager for the
        # full attack shape this closes.
        vendor = self._make_vendor('VG Normal Creation Vendor')
        with self.assertRaises(AccessError):
            self.env['vendorguard.fraud.flag'].with_user(self.regular_user).create({
                'flag_type': 'ghost_vendor', 'severity': 'medium', 'state': 'flagged',
                'partner_id': vendor.id, 'resolvable': False, 'description': 'x',
            })

    def test_manager_can_still_create_flag_directly(self):
        vendor = self._make_vendor('VG Manager Creation Vendor')
        flag = self.env['vendorguard.fraud.flag'].with_user(self.manager_user).create({
            'flag_type': 'ghost_vendor', 'severity': 'medium', 'state': 'flagged',
            'partner_id': vendor.id, 'resolvable': False, 'description': 'x',
        })
        self.assertEqual(flag.state, 'flagged')

    def test_bank_swap_notification_does_not_crash_for_non_manager(self):
        # _notify_finance_managers()'s activity_schedule() call wasn't sudo'd, so posting a
        # bill that trips the (severity=critical) bank_swap detector as anyone other than a
        # Finance Manager raised an AccessError instead of gracefully blocking the bill --
        # exactly the path the demo's headline scenario exercises
        vendor = self._make_vendor('VG Bank Swap Clerk Vendor')
        bank = self.env['res.partner.bank'].create(
            {'partner_id': vendor.id, 'acc_number': 'AE070000000000501'})
        bank.write({'acc_number': 'AE070000000000502'})
        move = self.env['account.move'].with_user(self.clerk_user).create({
            'move_type': 'in_invoice', 'partner_id': vendor.id,
            'invoice_date': fields.Date.today(), 'ref': 'BSC-001', 'journal_id': self.journal.id,
            'invoice_line_ids': [(0, 0, {'name': 'BSC-001', 'quantity': 1, 'price_unit': 500.0})],
        })
        move.with_user(self.clerk_user).action_post()  # must not raise
        self.assertEqual(move.state, 'draft')
        flag = self.env['vendorguard.fraud.flag'].search([
            ('move_id', '=', move.id), ('flag_type', '=', 'bank_swap')])
        self.assertTrue(flag)
        self.assertEqual(flag.severity, 'critical')

    def test_demo_scenario_loads_successfully_for_non_manager_user(self):
        # the loader's cleanup step did direct (non-sudo) unlink()s on models a regular
        # employee has no delete rights on (fraud.flag, account.move, purchase.order, bank
        # change log) -- any employee should be able to click "Load Demo Scenario", the
        # single most-clicked button in the module
        self.env['vendorguard.demo.scenario'].with_user(self.clerk_user).create({}) \
            .action_load_demo_scenario()

    def test_reject_allowed_on_nonresolvable_flag(self):
        # Reject used to be blocked for non-resolvable (structural) flags exactly like
        # Approve, leaving no UI-exposed way to dismiss a stale/false-positive structural
        # flag -- Reject now means "reviewed, dismissed," distinct from Approve
        vendor = self._make_vendor('VG Reject Nonresolvable Vendor')
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
        flag = self.env['vendorguard.fraud.flag'].search([('purchase_order_id', '=', po2.id)])
        self.assertFalse(flag.resolvable)
        flag.with_user(self.manager_user).action_reject()  # must not raise
        self.assertEqual(flag.state, 'rejected')

    def test_structuring_flag_auto_clears_when_sibling_po_cancelled(self):
        # once flagged, the structuring check never re-evaluated on retry -- even after the
        # exact fix its own block message suggests ("adjust... the vendor's PO history"), the
        # flag stayed 'flagged' forever and confirming the PO stayed blocked indefinitely.
        # Run as clerk_user, not the default superuser env: the auto-clear write() needs its
        # own sudo() (a non-manager has no write access on fraud.flag), and a first attempt
        # at this fix shipped without it -- this exact test, run as superuser, didn't catch it.
        vendor = self._make_vendor('VG Structuring Fix Vendor')
        po1 = self.env['purchase.order'].with_user(self.clerk_user).create({
            'partner_id': vendor.id,
            'order_line': [(0, 0, {
                'product_id': self.product.id, 'name': 'batch 1', 'product_qty': 1, 'price_unit': 8000.0})],
        })
        po1.button_confirm()
        po2 = self.env['purchase.order'].with_user(self.clerk_user).create({
            'partner_id': vendor.id,
            'order_line': [(0, 0, {
                'product_id': self.product.id, 'name': 'batch 2', 'product_qty': 1, 'price_unit': 8000.0})],
        })
        po2.button_confirm()
        self.assertEqual(po2.state, 'draft')
        po1.with_user(self.clerk_user).button_cancel()
        po2.with_user(self.clerk_user).button_confirm()  # must not raise AccessError
        self.assertEqual(po2.state, 'purchase',
                          "confirming again after fixing the underlying pattern must succeed")
        flag = self.env['vendorguard.fraud.flag'].search([('purchase_order_id', '=', po2.id)])
        self.assertEqual(flag.state, 'rejected',
                          "the stale flag must be auto-cleared, not left blocking forever")

    def test_flag_forgery_blocked_for_non_manager(self):
        # create() only ever validated the 'state' field -- a regular employee could still
        # forge a fully-formed critical flag (any flag_type/severity/partner_id/move_id) via
        # a single create() call, since base.group_user had perm_create=1 on this model.
        # Detector code now creates flags via sudo() and perm_create is revoked for
        # base.group_user entirely, so any direct create() by a non-manager must be refused
        # regardless of which fields it sets.
        vendor = self._make_vendor('VG Forgery Target Vendor')
        with self.assertRaises(AccessError):
            self.env['vendorguard.fraud.flag'].with_user(self.regular_user).create({
                'flag_type': 'bank_swap', 'severity': 'critical', 'state': 'flagged',
                'partner_id': vendor.id, 'resolvable': True, 'description': 'forged',
            })

    def test_demo_scenario_reruns_cleanly_after_twm_bill_posted(self):
        # the three-way-match demo bill's own reset step deleted a *posted* invoice, which
        # hits the same accounting sequence-chain integrity rule as the (already-fixed)
        # Benford seed bills once anything else posts to the same journal afterward --
        # reproduce the exact rehearsal sequence: load, complete the TWM beat live
        # (approve the flags, post), then reload again
        scenario = self.env['vendorguard.demo.scenario'].with_user(self.clerk_user).create({})
        scenario.action_load_demo_scenario()
        vendor = self.env['res.partner'].search([('name', '=', 'Al Fahim Trading LLC')], limit=1)
        twm_po = self.env['purchase.order'].search([
            ('partner_id', '=', vendor.id), ('order_line.name', '=', 'Steel Rebar Delivery — Site B'),
        ], limit=1)
        twm_bill = twm_po.invoice_ids.filtered(lambda m: m.state == 'draft')
        self.assertTrue(twm_bill)
        twm_bill.action_post()
        self.assertEqual(twm_bill.state, 'draft', "still blocked on the three-way-match flag")
        flags = self.env['vendorguard.fraud.flag'].search([('move_id', '=', twm_bill.id)])
        flags.with_user(self.manager_user).action_approve()
        twm_bill.action_post()
        self.assertEqual(twm_bill.state, 'posted')
        # post one more bill to the same journal so the TWM bill is no longer last in its
        # sequence chain -- this is what made the old unlink() attempt fail
        self._post_bill(vendor, 50.0, 'CHAIN-FILLER-001', seeding=True)
        scenario.action_load_demo_scenario()  # must not raise
