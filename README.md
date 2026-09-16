# VendorGuard

**Purchase-to-Pay fraud and compliance detection for Odoo 19.**

VendorGuard catches vendor bill and purchase order fraud before payment goes
out, using nine independent detection signals spanning transaction rules, an
identity check, a real cross-module procurement control, and two statistical
tests — plus a live vendor trust score, a Finance Manager approval workflow,
and a templated Q&A assistant with no external AI/network dependency.

## Detection signals

| Signal | Type | What it catches |
|---|---|---|
| Duplicate Bill | Hard constraint | Same vendor + amount + reference + date submitted twice |
| Bank Account Swap | Rule | Vendor's payment account changed shortly before a bill |
| Structuring | Rule | Sub-threshold purchase orders that sum over a limit |
| Lookalike Vendor | Identity | New vendor name is a near-match to an existing vendor |
| Segregation of Duties | Rule | Same user created *and* is posting a bill |
| Benford's Law Audit | Statistical | First-digit MAD conformity test (Nigrini's method) |
| Chi-Square Test | Statistical | Goodness-of-fit test, run alongside Benford's Law |
| Ghost Vendor | Identity | Vendor has bills but no tax ID and no bank account |
| Three-Way Match Mismatch | Cross-module | Billed quantity exceeds received quantity against a PO |

Every check either hard-blocks (duplicate bill), soft-blocks pending Finance
Manager review (bank swap, segregation of duties, structuring, three-way
match), or raises an informational flag that feeds the vendor's trust score
without blocking anything (lookalike vendor, ghost vendor, shared bank
account, Benford anomaly).

## Requirements

- Odoo 19.0
- Depends on: `base`, `mail`, `account`, `purchase` (all standard Odoo
  modules — no third-party dependencies)

## Installation

```
odoo-bin -c odoo.conf -d <your-db> -i vendorguard --addons-path=<odoo-addons>,<path-to-this-repo>
```

## Running the demo

Once installed, open the **VendorGuard** app and click **Load Demo
Scenario**. It's idempotent — safe to click repeatedly, including mid-
rehearsal — and seeds:

- A vendor with a recent bank-account swap and a large pending payment
- A near-threshold purchase order (structuring)
- A purchase order that will block on Confirm (three-way match mismatch)
- A second vendor sharing the first vendor's bank account
- Two vendors with 40 posted bills each: one built to pass Benford's Law,
  one built to fail it

Then: open a draft bill for the seeded vendor and click **Confirm** — it
blocks, live, naming every signal that fired with the actual numbers. Open
the flags, **Approve** as a Finance Manager, click **Confirm** again — it
posts.

## Running the tests

```
odoo-bin -c odoo.conf -d <your-db> -u vendorguard --test-enable --test-tags /vendorguard --stop-after-init --addons-path=<odoo-addons>,<path-to-this-repo>
```

46 `TransactionCase` tests cover all nine checks, the approval workflow and
its security boundary, multi-company scoping, trust score computation, and
the Q&A assistant — see `vendorguard/tests/`.

## Architecture notes

- No core Odoo files are modified — every model uses `_inherit` on
  `account.move`, `purchase.order`, `res.partner`, and `res.partner.bank`.
- Blocking is non-raising by design: `action_post()`/`button_confirm()`
  return a sticky notification instead of raising, which keeps the client's
  record data fresh (the Fraud Flags tab reloads automatically) instead of
  leaving it stale behind an exception dialog.
- Approval requires the **VendorGuard / Finance Manager** group, enforced
  both in the view and defensively in Python (`write()` blocks direct
  `state` edits; model-level write access on the flag model is restricted
  to that group at the ACL layer too).
- Fraud flags are company-scoped (`company_id` + an `ir.rule`), matching
  Odoo's standard multi-company model.
- The Q&A assistant ("Ask VendorGuard") is templated pattern-matching over
  real ORM queries — deliberately not an LLM call, so it has zero network
  dependency and can't fail unpredictably during a live demo.

## Roadmap (out of scope for this build)

Kickback/bid-rigging schemes, check tampering, and expense-reimbursement
abuse are recognized ACFE fraud categories not covered here — scoped out
deliberately rather than omitted by oversight.
