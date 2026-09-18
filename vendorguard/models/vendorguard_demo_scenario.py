import math
import random

from dateutil.relativedelta import relativedelta

from odoo import _, fields, models

from .vendorguard_constants import STRUCTURING_THRESHOLD


class VendorguardDemoScenario(models.Model):
    _name = 'vendorguard.demo.scenario'
    _description = 'VendorGuard Demo Scenario Loader'

    def _find_or_create_vendor(self, name, **extra_vals):
        Partner = self.env['res.partner'].with_context(vendorguard_skip_lookalike=True)
        vendor = Partner.search([('name', '=', name)], limit=1)
        if not vendor:
            vals = {'name': name, 'is_company': True, 'supplier_rank': 1}
            vals.update(extra_vals)
            vendor = Partner.create(vals)
        elif extra_vals:
            vendor.write(extra_vals)
        return vendor

    def _find_journal(self):
        return self.env['account.journal'].search([('type', '=', 'purchase')], limit=1)

    def _find_or_create_demo_product(self):
        name = 'Portland Cement (50kg Bags)'
        product = self.env['product.product'].search([('name', '=', name)], limit=1)
        if not product:
            # rename in place if the old placeholder product exists, so historical PO/bill
            # lines that already reference it pick up the realistic name too
            old = self.env['product.product'].search([('name', '=', 'VendorGuard Demo Product')], limit=1)
            if old:
                old.name = name
                product = old
            else:
                product = self.env['product.product'].create({
                    'name': name, 'type': 'consu', 'purchase_ok': True, 'sale_ok': False,
                })
        return product

    def _find_or_create_twm_product(self):
        name = 'Structural Steel Rebar (12mm)'
        product = self.env['product.product'].search([('name', '=', name)], limit=1)
        if not product:
            old = self.env['product.product'].search([('name', '=', 'VendorGuard TWM Product')], limit=1)
            if old:
                old.name = name
                product = old
            else:
                product = self.env['product.product'].create({
                    'name': name, 'type': 'consu', 'purchase_ok': True, 'sale_ok': False,
                    'purchase_method': 'purchase',
                })
        if product.purchase_method != 'purchase':
            product.purchase_method = 'purchase'
        return product

    def _wipe_volatile(self, vendor):
        self.env['vendorguard.fraud.flag'].search([('partner_id', '=', vendor.id)]).unlink()
        self.env['vendorguard.bank.change.log'].search([('partner_id', '=', vendor.id)]).unlink()
        self.env['account.move'].search([
            ('partner_id', '=', vendor.id), ('move_type', '=', 'in_invoice'), ('state', '=', 'draft'),
        ]).unlink()
        draft_pos = self.env['purchase.order'].search([
            ('partner_id', '=', vendor.id), ('state', '=', 'draft'),
        ])
        if draft_pos:
            draft_pos.button_cancel()
            draft_pos.unlink()

    def _post_bill(self, vendor, amount, ref, invoice_date=None, journal=None, description=None):
        journal = journal or self._find_journal()
        move = self.env['account.move'].create({
            'move_type': 'in_invoice',
            'partner_id': vendor.id,
            'invoice_date': invoice_date or fields.Date.today(),
            'ref': ref,
            'journal_id': journal.id,
            'invoice_line_ids': [(0, 0, {
                'name': description or ref,
                'quantity': 1,
                'price_unit': amount,
            })],
        })
        move.with_context(vendorguard_seeding=True).action_post()
        return move

    def action_load_demo_scenario(self):
        self.ensure_one()
        # Setup/reset utility, not a security-sensitive action: any user who can open the
        # wizard should be able to run it, even if they aren't a Finance Manager and lack
        # write/unlink rights on fraud flags, bills, POs etc that this rebuilds from scratch.
        self = self.sudo()
        journal = self._find_journal()

        # --- Core hold/structuring demo vendor ---
        ae = self.env.ref('base.ae', raise_if_not_found=False)
        vendor = self._find_or_create_vendor(
            'Al Fahim Trading LLC', street='Al Quoz Industrial Area 3', city='Dubai',
            country_id=ae.id if ae else False, email='accounts@alfahimtrading.ae',
            phone='+971 4 887 2200')
        self._wipe_volatile(vendor)

        clean_ref = 'AFT-CLEAN-001'
        existing_clean = self.env['account.move'].search([
            ('partner_id', '=', vendor.id), ('ref', '=', clean_ref), ('state', '=', 'posted'),
        ], limit=1)
        if not existing_clean:
            self._post_bill(vendor, 5000.0, clean_ref,
                             invoice_date=fields.Date.today() - relativedelta(days=30), journal=journal,
                             description='Cement Delivery — Monthly Supply Contract')
        else:
            try:
                existing_clean.invoice_line_ids.write({'name': 'Cement Delivery — Monthly Supply Contract'})
            except Exception:
                pass  # posted entries may restrict line edits — cosmetic only, not worth failing the load

        bank = self.env['res.partner.bank'].sudo().search([('partner_id', '=', vendor.id)], limit=1)
        if not bank:
            bank = self.env['res.partner.bank'].sudo().create({
                'partner_id': vendor.id, 'acc_number': 'AE070331234567890123456',
            })
        self.env['vendorguard.bank.change.log'].sudo().create({
            'partner_id': vendor.id,
            'old_acc_number': 'AE070331234567890123400',
            'new_acc_number': bank.acc_number,
            'change_date': fields.Datetime.now() - relativedelta(days=2),
        })

        pending_ref = 'AFT-PENDING-%s' % fields.Datetime.now().strftime('%Y%m%d%H%M%S')
        self.env['account.move'].create({
            'move_type': 'in_invoice', 'partner_id': vendor.id,
            'invoice_date': fields.Date.today(), 'ref': pending_ref,
            'journal_id': journal.id,
            'invoice_line_ids': [(0, 0, {
                'name': 'Advance Payment — Bulk Cement Order (Contract #AFT-2026-118)',
                'quantity': 1, 'price_unit': 340000.0,
            })],
        })

        po_amount = STRUCTURING_THRESHOLD * 0.6
        demo_product = self._find_or_create_demo_product()
        structuring_po_marker = 'Cement Bulk Order — Batch 1'
        twm_marker = 'Steel Rebar Delivery — Site B'
        # Any other confirmed PO for this vendor -- left over from an earlier rehearsal,
        # manual testing, or a fumbled live attempt at the structuring beat -- silently
        # pollutes the structuring check's rolling 30-day total. Worse, if any stray PO is
        # itself larger than the threshold, "no single PO alone exceeded it" becomes false
        # forever, permanently disabling the detector for this vendor. Cancelling anything
        # outside our two known markers keeps every reload a genuinely clean slate; state
        # filters to 'purchase' on the detector's own search, so cancelling is enough --
        # no need to also unlink (and unlinking a PO with downstream bills can raise).
        known_po_markers = {structuring_po_marker, twm_marker}
        stray_pos = self.env['purchase.order'].search([
            ('partner_id', '=', vendor.id), ('state', '=', 'purchase'),
        ]).filtered(lambda p: not (set(p.order_line.mapped('name')) & known_po_markers))
        if stray_pos:
            try:
                stray_pos.button_cancel()
            except Exception:
                pass  # a stray PO with downstream documents -- leave it, not worth failing the load
        po = self.env['purchase.order'].search([
            ('partner_id', '=', vendor.id), ('order_line.name', '=', structuring_po_marker),
        ], limit=1)
        if not po:
            po = self.env['purchase.order'].create({
                'partner_id': vendor.id,
                'order_line': [(0, 0, {
                    'product_id': demo_product.id,
                    'name': structuring_po_marker,
                    'product_qty': 1,
                    'price_unit': po_amount,
                })],
            })
        if po.state not in ('purchase', 'done'):
            po.button_confirm()

        # --- Three-way match demo: PO for 20 units, only 12 recorded as received, ordered-policy
        # billing lets a bill go out for the full 20 anyway — the live "Post" click blocks on it. ---
        twm_product = self._find_or_create_twm_product()
        # Once a bill posts, deleting it hits the same accounting sequence-chain integrity
        # rule as the Benford seed bills (see _seed_benford_vendor) -- rather than fight it,
        # reuse a PO for this beat only while it has no posted bill against it yet; once the
        # presenter completes the live block -> approve -> post cycle on one, start a fresh
        # PO next load instead of touching the now-historical posted bill.
        twm_po = next((
            po for po in self.env['purchase.order'].search([
                ('partner_id', '=', vendor.id), ('order_line.name', '=', twm_marker),
            ])
            if not po.invoice_ids.filtered(lambda m: m.state == 'posted')
        ), None)
        if not twm_po:
            twm_po = self.env['purchase.order'].create({
                'partner_id': vendor.id,
                'order_line': [(0, 0, {
                    'product_id': twm_product.id,
                    'name': twm_marker,
                    'product_qty': 20,
                    'price_unit': 100.0,
                })],
            })
        if twm_po.state != 'purchase':
            twm_po.button_confirm()
        # Any leftover draft bill from an unfinished prior run is always safe to clear —
        # drafts never consumed a sequence number, unlike the posted case handled above.
        twm_po.invoice_ids.filtered(lambda m: m.state == 'draft').unlink()
        twm_po.order_line.qty_received_manual = 12.0
        twm_po.action_create_invoice()
        twm_po.invoice_ids.filtered(lambda m: m.state == 'draft').write({'invoice_date': fields.Date.today()})

        # --- Shared bank account demo: a second vendor sharing Al Fahim's bank account ---
        collusion_vendor = self._find_or_create_vendor(
            'Sahara Logistics FZE', street='Jebel Ali Free Zone', city='Dubai',
            country_id=ae.id if ae else False, email='finance@saharalogistics.ae',
            phone='+971 4 881 5590')
        self.env['vendorguard.fraud.flag'].search([
            ('partner_id', '=', collusion_vendor.id), ('flag_type', '=', 'shared_bank_account'),
        ]).unlink()
        self.env['vendorguard.fraud.flag'].search([
            ('partner_id', '=', vendor.id), ('flag_type', '=', 'shared_bank_account'),
        ]).unlink()
        collusion_bank = self.env['res.partner.bank'].sudo().search(
            [('partner_id', '=', collusion_vendor.id)], limit=1)
        if not collusion_bank:
            self.env['res.partner.bank'].sudo().create({
                'partner_id': collusion_vendor.id, 'acc_number': bank.acc_number,
            })
        else:
            collusion_bank.write({'acc_number': bank.acc_number})

        # --- Seeded cosmetic duplicate_bill flag (real triggers of this check never persist a row) ---
        if not self.env['vendorguard.fraud.flag'].search([('flag_type', '=', 'duplicate_bill')], limit=1):
            self.env['vendorguard.fraud.flag'].create({
                'flag_type': 'duplicate_bill', 'severity': 'high', 'state': 'flagged',
                'partner_id': vendor.id, 'resolvable': False,
                'description': "Example: bill AFT-CLEAN-001 was submitted a second time with identical "
                               "vendor, amount and date — blocked at save time.",
            })

        # --- Benford beat: two vendors with synthetic bill history ---
        self._seed_benford_vendor(
            'Global Manufacturing Co', journal,
            amounts=self._benford_conforming_amounts(40),
            item_pool=[
                'Steel Coils (Grade A)', 'Aluminum Sheet Stock', 'Copper Wiring — 500m Spool',
                'Industrial Ball Bearings', 'Hydraulic Pump Unit', 'CNC Machine Tooling',
                'Galvanized Pipe Fittings', 'Electric Motor — 5HP',
            ],
            vendor_vals={'street': 'Jebel Ali Industrial Zone 1', 'city': 'Dubai',
                         'country_id': ae.id if ae else False,
                         'email': 'procurement@globalmfg.ae', 'phone': '+971 4 880 3341'})
        self._seed_benford_vendor(
            'Roundtrip Supplies LLC', journal,
            amounts=[round(random.choice([500, 1000, 1500, 2000, 5000]) + random.uniform(-5, 5), 2)
                     for _ in range(40)],
            item_pool=['General Supplies', 'Miscellaneous Goods', 'Office Consumables',
                       'Packaging Materials', 'Sundry Items'],
            vendor_vals={'street': 'Al Barsha Business Center', 'city': 'Dubai',
                         'country_id': ae.id if ae else False,
                         'email': 'billing@roundtripsupplies.ae', 'phone': '+971 4 556 7712'})

        # No explicit commit here: the wizard button's own request cycle commits the
        # transaction on a successful return, same as any other Odoo button action --
        # an explicit mid-flow commit only gets in the way of testing this method.
        return {
            'type': 'ir.actions.client', 'tag': 'display_notification',
            'params': {
                'title': _('VendorGuard Demo Scenario Loaded'),
                'message': _('Vendor, bank history, pending bill, PO, and Benford seed vendors are ready.'),
                'sticky': False,
            },
        }

    def _benford_conforming_amounts(self, count):
        """Deterministic stratified sample: exact Benford-expected leading-digit quota per
        digit, so the seeded 'conforming' vendor reliably passes the MAD test every run,
        rather than depending on random draws happening to converge with a small sample."""
        expected = {d: math.log10(1 + 1 / d) for d in range(1, 10)}
        quotas = {d: round(expected[d] * count) for d in range(1, 10)}
        # rounding can leave the total a bit off `count`; fix up on digit 1 (largest quota)
        quotas[1] += count - sum(quotas.values())

        amounts = []
        for digit, n in quotas.items():
            for _ in range(n):
                magnitude = 10 ** random.randint(1, 3)
                amounts.append(round(random.uniform(digit, digit + 1) * magnitude, 2))
        random.shuffle(amounts)
        return amounts

    def _seed_benford_vendor(self, name, journal, amounts, item_pool, vendor_vals=None):
        vendor = self._find_or_create_vendor(name, **(vendor_vals or {}))
        # Once a posted bill has consumed a sequence number, Odoo's own accounting-integrity
        # rule (_unlink_forbid_parts_of_chain) refuses to delete it unless it's the very last
        # entry in the chain -- deleting 40 posted bills as a batch hits this reliably on any
        # second "Load Demo Scenario" run. Rather than fight that, treat existing seed history
        # as already-loaded and skip re-seeding: it's idempotent either way, and reusing the
        # same historical amounts actually makes the MAD score identical across rehearsals.
        existing = self.env['account.move'].search_count([
            ('partner_id', '=', vendor.id), ('move_type', '=', 'in_invoice'),
            ('ref', 'like', '%s-SEED-%%' % name[:3].upper()),
        ])
        if existing >= len(amounts):
            return vendor
        for i, amount in enumerate(amounts):
            if amount <= 0:
                amount = 100.0
            self._post_bill(
                vendor, amount, '%s-SEED-%03d' % (name[:3].upper(), i),
                invoice_date=fields.Date.today() - relativedelta(days=i), journal=journal,
                description=item_pool[i % len(item_pool)])
        return vendor
