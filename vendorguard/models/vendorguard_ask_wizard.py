import re

from dateutil.relativedelta import relativedelta

from odoo import fields, models

OPEN_STATES = ('flagged', 'pending_review')

# Generic corporate words stripped before matching a vendor name against a question, so a
# partial mention ("Al Fahim") still matches the full legal name ("Al Fahim Trading LLC").
_NAME_STOPWORDS = {
    'llc', 'fze', 'ltd', 'inc', 'co', 'company', 'trading', 'consulting',
    'supplies', 'logistics', 'manufacturing', 'group', 'corp', 'corporation',
}


class VendorguardAskWizard(models.TransientModel):
    _name = 'vendorguard.ask.wizard'
    _description = 'Ask VendorGuard'

    question = fields.Char()
    answer = fields.Text(readonly=True)

    def _find_vendor_in_text(self, text):
        question_tokens = set(re.findall(r'[a-z0-9]+', (text or '').lower()))
        vendors = self.env['res.partner'].search([('supplier_rank', '>', 0)])
        best, best_score = self.env['res.partner'], 0
        for vendor in vendors:
            if not vendor.name:
                continue
            name_tokens = set(re.findall(r'[a-z0-9]+', vendor.name.lower())) - _NAME_STOPWORDS
            score = len(name_tokens & question_tokens)
            if score > best_score:
                best, best_score = vendor, score
        return best

    def _flag_line(self, flag):
        return "- [%s] %s on %s" % (
            flag.severity.upper(),
            dict(flag._fields['flag_type'].selection).get(flag.flag_type),
            flag.partner_id.name)

    def _snapshot(self):
        Flag = self.env['vendorguard.fraud.flag']
        riskiest = self.env['res.partner'].search(
            [('supplier_rank', '>', 0)], order='trust_score asc', limit=1)
        open_count = Flag.search_count([('state', 'in', OPEN_STATES)])
        critical_count = Flag.search_count(
            [('state', 'in', OPEN_STATES), ('severity', 'in', ('critical', 'high'))])
        if not riskiest:
            return "No vendor data yet — try Load Demo Scenario first."
        return (
            "There are %d open flag(s) right now (%d critical/high). The riskiest vendor is "
            "%s at trust score %d (%s)."
        ) % (open_count, critical_count, riskiest.name, riskiest.trust_score, riskiest.trust_tier)

    def action_ask(self):
        self.ensure_one()
        q = (self.question or '').strip()
        ql = q.lower()
        Flag = self.env['vendorguard.fraud.flag']
        vendor = self._find_vendor_in_text(q)

        if not q:
            self.answer = self._snapshot()

        elif 'riskiest' in ql or 'highest risk' in ql or 'highest-risk' in ql:
            partner = self.env['res.partner'].search(
                [('supplier_rank', '>', 0)], order='trust_score asc', limit=1)
            if partner:
                open_flags = partner.fraud_flag_ids.filtered(lambda f: f.state in OPEN_STATES)
                self.answer = (
                    "The riskiest vendor right now is %s, with a trust score of %d (%s). "
                    "They have %d unresolved fraud flag(s)."
                ) % (partner.name, partner.trust_score, partner.trust_tier, len(open_flags))
            else:
                self.answer = "No vendors with a computed trust score yet."

        elif vendor and ('safe' in ql or 'risky' in ql or 'trust' in ql):
            open_flags = vendor.fraud_flag_ids.filtered(lambda f: f.state in OPEN_STATES)
            self.answer = "%s has a trust score of %d (%s), with %d open flag(s)." % (
                vendor.name, vendor.trust_score, vendor.trust_tier, len(open_flags))

        elif 'critical' in ql or 'high risk flags' in ql or ('show' in ql and 'flag' in ql):
            flags = Flag.search([
                ('state', 'in', OPEN_STATES), ('severity', 'in', ('critical', 'high')),
            ], limit=8)
            if flags:
                self.answer = "Open critical/high flags:\n" + "\n".join(
                    self._flag_line(f) for f in flags)
            else:
                self.answer = "No open critical or high severity flags right now."

        elif 'how many' in ql and 'flag' in ql:
            count = Flag.search_count([('state', 'in', OPEN_STATES)])
            self.answer = "There are %d open fraud flag(s) right now." % count

        elif 'blocked' in ql or ('flag' in ql and ('today' in ql or 'this week' in ql)):
            days = 7 if 'week' in ql else 1
            since = fields.Datetime.now() - relativedelta(days=days)
            domain = [('create_date', '>=', since)]
            total = Flag.search_count(domain)
            if total:
                period = 'today' if days == 1 else 'this week'
                shown = Flag.search(domain, limit=8)
                more = "\n(+%d more)" % (total - len(shown)) if total > len(shown) else ""
                self.answer = "%d flag(s) raised %s:\n" % (total, period) + "\n".join(
                    self._flag_line(f) for f in shown) + more
            else:
                self.answer = "Nothing has been flagged in that period."

        elif 'why' in ql:
            flag = Flag.browse()
            if self.env.context.get('active_model') == 'vendorguard.fraud.flag':
                flag = Flag.browse(self.env.context.get('active_id')).exists()
            if not flag and vendor:
                flag = vendor.fraud_flag_ids.filtered(lambda f: f.state in OPEN_STATES)[:1]
            self.answer = flag.description if flag else self._snapshot()

        elif vendor:
            open_flags = vendor.fraud_flag_ids.filtered(lambda f: f.state in OPEN_STATES)
            self.answer = (
                "I don't have a canned answer for that, but here's what I know about %s: "
                "trust score %d (%s), %d open flag(s)."
            ) % (vendor.name, vendor.trust_score, vendor.trust_tier, len(open_flags))

        else:
            self.answer = self._snapshot()

        return {
            'type': 'ir.actions.act_window',
            'res_model': 'vendorguard.ask.wizard',
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }
