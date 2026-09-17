import difflib
import math
from collections import Counter

from dateutil.relativedelta import relativedelta

from odoo import _, api, fields, models

from .vendorguard_constants import (
    BANK_CHANGE_RECENT_DAYS,
    BENFORD_CHI_SQUARE_CRITICAL,
    BENFORD_MAD_MARGINAL,
    BENFORD_MIN_SAMPLE_SIZE,
    LOOKALIKE_SIMILARITY_THRESHOLD,
    SEVERITY_SCORE_PENALTY,
)


def _benford_expected_first_digit_distribution():
    return {d: math.log10(1 + 1 / d) for d in range(1, 10)}


class ResPartner(models.Model):
    _inherit = 'res.partner'

    fraud_flag_ids = fields.One2many('vendorguard.fraud.flag', 'partner_id', string='Fraud Flags')
    bank_change_log_ids = fields.One2many('vendorguard.bank.change.log', 'partner_id', string='Bank Change Log')
    trust_score = fields.Integer(compute='_compute_trust_score', store=True)
    trust_tier = fields.Selection([
        ('safe', 'Safe'),
        ('watch', 'Watch'),
        ('high_risk', 'High Risk'),
    ], compute='_compute_trust_score', store=True)

    @api.depends('fraud_flag_ids.state', 'fraud_flag_ids.severity', 'bank_change_log_ids.change_date')
    def _compute_trust_score(self):
        now = fields.Datetime.now()
        cutoff = now - relativedelta(days=BANK_CHANGE_RECENT_DAYS)
        for partner in self:
            score = 100
            for flag in partner.fraud_flag_ids:
                if flag.state in ('flagged', 'pending_review'):
                    score -= SEVERITY_SCORE_PENALTY.get(flag.severity, 0)
            if any(log.change_date and log.change_date >= cutoff for log in partner.bank_change_log_ids):
                score -= 20
            partner.trust_score = max(0, min(100, score))
            if partner.trust_score >= 70:
                partner.trust_tier = 'safe'
            elif partner.trust_score >= 40:
                partner.trust_tier = 'watch'
            else:
                partner.trust_tier = 'high_risk'

    def _cron_recompute_trust_scores(self):
        # trust_score is a stored compute keyed on bank_change_log_ids.change_date, which
        # never itself changes — so a partner's -20 "recent bank change" penalty would
        # otherwise stay applied forever past the recency window with nothing to trigger a
        # recompute. Touch every partner with bank-change history once a day so the penalty
        # actually expires on schedule.
        partners = self.search([('bank_change_log_ids', '!=', False)])
        partners._compute_trust_score()

    @api.model_create_multi
    def create(self, vals_list):
        partners = super().create(vals_list)
        if not self.env.context.get('vendorguard_skip_lookalike'):
            partners._check_lookalike_vendor()
        return partners

    def _check_lookalike_vendor(self):
        # scoped to other vendors, not all companies — this check exists to catch vendor
        # impersonation, so comparing against unrelated customer names would just be noise
        vendors = self.env['res.partner'].search([('is_company', '=', True), ('supplier_rank', '>', 0)])
        for partner in self:
            if not partner.is_company or not partner.name or not partner.supplier_rank:
                continue
            for other in vendors - partner:
                if not other.name:
                    continue
                ratio = difflib.SequenceMatcher(None, partner.name.lower(), other.name.lower()).ratio()
                if LOOKALIKE_SIMILARITY_THRESHOLD <= ratio < 1.0:
                    self.env['vendorguard.fraud.flag'].sudo().create({
                        'flag_type': 'lookalike_vendor', 'severity': 'medium', 'state': 'flagged',
                        'partner_id': partner.id, 'resolvable': False,
                        'description': (
                            "New vendor '%s' is a %.0f%% name match to existing vendor '%s' — "
                            "possible lookalike/fraud vendor."
                        ) % (partner.name, ratio * 100, other.name),
                    })
                    break

    def action_run_benford_audit(self):
        self.ensure_one()
        bills = self.env['account.move'].search([
            ('partner_id', '=', self.id), ('move_type', '=', 'in_invoice'), ('state', '=', 'posted'),
        ])
        first_digits = []
        for bill in bills:
            digits = ''.join(ch for ch in str(bill.amount_total) if ch.isdigit()).lstrip('0')
            if digits:
                first_digits.append(int(digits[0]))

        if len(first_digits) < BENFORD_MIN_SAMPLE_SIZE:
            return {
                'type': 'ir.actions.client', 'tag': 'display_notification',
                'params': {
                    'title': _('VendorGuard Statistical Audit'),
                    'message': _("Only %d posted bills — need at least %d for a statistically valid "
                                 "Benford test.") % (len(first_digits), BENFORD_MIN_SAMPLE_SIZE),
                    'sticky': False,
                },
            }

        n = len(first_digits)
        expected = _benford_expected_first_digit_distribution()
        counts = Counter(first_digits)
        mad = sum(abs(counts.get(d, 0) / n - expected[d]) for d in range(1, 10)) / 9
        chi_square = sum(
            ((counts.get(d, 0) - expected[d] * n) ** 2) / (expected[d] * n) for d in range(1, 10))
        chi_square_fails = chi_square > BENFORD_CHI_SQUARE_CRITICAL

        message = (
            "Benford's Law audit on %d bills: MAD = %.4f (nonconformity threshold %.4f); "
            "chi-square = %.3f (critical value %.3f at p=0.05, 8 df)."
        ) % (n, mad, BENFORD_MAD_MARGINAL, chi_square, BENFORD_CHI_SQUARE_CRITICAL)

        if mad > BENFORD_MAD_MARGINAL:
            existing = self.fraud_flag_ids.filtered(
                lambda f: f.flag_type == 'benford_anomaly' and f.state in ('flagged', 'pending_review'))
            if not existing:
                corroboration = (
                    " Corroborated by an independent chi-square goodness-of-fit test."
                    if chi_square_fails else
                    " Chi-square test does not independently confirm this at the 0.05 level."
                )
                self.env['vendorguard.fraud.flag'].sudo().create({
                    'flag_type': 'benford_anomaly', 'severity': 'high', 'state': 'flagged',
                    'partner_id': self.id, 'resolvable': True,
                    'description': (
                        "Statistical audit: %d posted bills from %s have a Benford's Law (first-digit) MAD "
                        "score of %.4f (nonconformity threshold is %.4f, per Nigrini's guidelines) and a "
                        "chi-square statistic of %.3f (critical value %.3f) — the amount distribution is "
                        "statistically unusual and warrants review.%s"
                    ) % (n, self.name, mad, BENFORD_MAD_MARGINAL, chi_square, BENFORD_CHI_SQUARE_CRITICAL,
                         corroboration),
                })
            message += " NONCONFORMITY — flag created."
        else:
            message += " Conforms to Benford's Law — no flag."

        return {
            'type': 'ir.actions.client', 'tag': 'display_notification',
            'params': {'title': _('VendorGuard Statistical Audit'), 'message': message, 'sticky': True},
        }
