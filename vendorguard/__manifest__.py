{
    'name': 'VendorGuard',
    'version': '19.0.1.0.0',
    'category': 'Accounting/Accounting',
    'summary': 'Vendor fraud and compliance detection for Purchase-to-Pay',
    'description': """
VendorGuard — Purchase-to-Pay Fraud & Compliance
=================================================

Catches vendor bill and purchase order fraud before payment goes out, using
nine independent detection signals spanning transaction rules, an identity
check, a real cross-module procurement control, and two statistical tests:

* Duplicate bill detection (hard constraint)
* Bank account swap shortly before a bill
* Structuring (sub-threshold purchase orders that sum over a limit)
* Lookalike / impersonation vendor names
* Segregation-of-duties violations (same user creates and posts)
* Benford's Law conformity audit (first-digit MAD test, Nigrini's method)
* Chi-square goodness-of-fit test, run alongside Benford's Law
* Ghost vendor detection (no tax ID, no bank account on file)
* Three-way match mismatch (billed vs. received against a purchase order)

Every vendor gets a live, computed Trust Score and risk tier. Flags route
through a Finance Manager approval workflow before a blocked bill or
purchase order can be confirmed. A templated Q&A assistant ("Ask
VendorGuard") answers questions about the vendors and flags currently in
the system — no external AI call, so it never depends on a network
connection during a live demo.
""",
    'author': 'VendorGuard Team',
    'license': 'LGPL-3',
    'depends': ['base', 'mail', 'account', 'purchase'],
    'application': True,
    'data': [
        'security/vendorguard_security.xml',
        'security/ir.model.access.csv',
        'data/vendorguard_cron.xml',
        'views/vendorguard_fraud_flag_views.xml',
        'views/res_partner_views.xml',
        'views/account_move_views.xml',
        'views/purchase_order_views.xml',
        'views/vendorguard_demo_scenario_views.xml',
        'views/vendorguard_ask_wizard_views.xml',
        'views/vendorguard_menus.xml',
    ],
}
