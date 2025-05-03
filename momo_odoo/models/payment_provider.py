import logging
import json
import uuid
import hmac
import hashlib
from odoo import _, api, fields, models
from odoo.addons.momo_odoo import const

_logger = logging.getLogger(__name__)


class PaymentProviderMoMo(models.Model):
    _inherit = "payment.provider"

    # Add 'MoMo' as a new payment provider
    code = fields.Selection(
        selection_add=[("momo", "MoMo")], ondelete={"momo": "set default"}
    )

    # Define fields for MoMo configuration
    momo_partner_code = fields.Char(
        string="Partner Code", default="MOMO", required_if_provider="momo"
    )
    momo_access_key = fields.Char(
        string="Access Key", default="F8BBA842ECF85", required_if_provider="momo"
    )
    momo_secret_key = fields.Char(
        string="Secret Key", default="K951B6PE1waDMi640xX08PD3vg6EkVlz", required_if_provider="momo"
    )
    momo_payment_type = fields.Selection(
        [
            ('capture_wallet', 'MoMo Wallet'),
            ('pay_with_method', 'Pay with Methods (ATM, Visa, etc.)')
        ],
        string="Payment Type",
        default='capture_wallet',
        required_if_provider="momo"
    )

    @api.model
    def _get_compatible_providers(
            self, *args, currency_id=None, is_validation=False, **kwargs
    ):
        """Filter out providers based on currency and transaction type."""
        providers = super()._get_compatible_providers(
            *args, currency_id=currency_id, is_validation=is_validation, **kwargs
        )

        currency = self.env["res.currency"].browse(currency_id).exists()

        if (
                currency and currency.name not in const.SUPPORTED_CURRENCIES
        ) or is_validation:
            providers = providers.filtered(lambda p: p.code != "momo")

        return providers

    def _get_supported_currencies(self):
        """Override to return the supported currencies."""
        supported_currencies = super()._get_supported_currencies()
        if self.code == "momo":
            supported_currencies = supported_currencies.filtered(
                lambda c: c.name in const.SUPPORTED_CURRENCIES
            )
        return supported_currencies

    def _get_momo_api_url(self):
        """Get the appropriate MoMo API URL based on environment."""
        base_url = const.SANDBOX_DOMAIN if self.state == 'test' else const.PRODUCTION_DOMAIN
        return f"{base_url}{const.CREATE_PAYMENT_PATH}"

    def _get_momo_request_type(self):
        """Get the MoMo request type based on payment type configuration."""
        if self.momo_payment_type == 'capture_wallet':
            return const.REQUEST_TYPE_CAPTURE_WALLET
        else:
            return const.REQUEST_TYPE_PAY_WITH_METHOD

    def _generate_signature(self, params):
        """Generate HMAC-SHA256 signature for MoMo request."""
        # Sort parameters alphabetically and create the raw signature string
        sorted_params = sorted(params.items())
        raw_signature = "&".join([f"{key}={params[key]}" for key in params])

        # Create HMAC-SHA256 signature
        h = hmac.new(
            bytes(self.momo_secret_key, 'ascii'),
            bytes(raw_signature, 'ascii'),
            hashlib.sha256
        )
        return h.hexdigest()

    def _get_default_payment_method_codes(self):
        """Override of payment to return the default payment method codes."""
        default_codes = super()._get_default_payment_method_codes()
        if self.code != "momo":
            return default_codes
        return const.DEFAULT_PAYMENT_METHODS_CODES