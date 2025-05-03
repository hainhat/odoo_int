from . import controllers
from . import models

from odoo.addons.payment import setup_provider, reset_payment_provider

def post_init_hook(env):
    setup_provider(env, "mbbank")
    payment_mbbank = env["payment.provider"].search([("code", "=", "mbbank")], limit=1)
    payment_method_mbbank = env["payment.method"].search(
        [("code", "=", "mbbank")], limit=1
    )
    if payment_method_mbbank.id is not False:
        payment_mbbank.write(
            {
                "payment_method_ids": [(6, 0, [payment_method_mbbank.id])],
            }
        )

def uninstall_hook(env):
    reset_payment_provider(env, "mbbank")