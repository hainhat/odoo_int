import logging
import json
import uuid
import hmac
import hashlib
from odoo import _, api, fields, models
from odoo.addons.mbbank_odoo import const

_logger = logging.getLogger(__name__)


class PaymentProviderMBBank(models.Model):
    _inherit = "payment.provider"

    # Add 'MB Bank' as a new payment provider
    code = fields.Selection(
        selection_add=[("mbbank", "MB Bank")], ondelete={"mbbank": "set default"}
    )

    # Define fields for MB Bank configuration
    mb_merchant_id = fields.Char(
        string="Merchant ID", default="", required_if_provider="mbbank"
    )
    mb_access_code = fields.Char(
        string="Access Code", default="", required_if_provider="mbbank"
    )
    mb_hash_secret = fields.Char(
        string="Hash Secret", default="", required_if_provider="mbbank"
    )
    mb_username = fields.Char(
        string="Username (OAuth)", default="", required_if_provider="mbbank"
    )
    mb_password = fields.Char(
        string="Password (OAuth)", default="", required_if_provider="mbbank",
        password=True
    )
    mb_payment_method = fields.Selection(
        [
            ('QR', 'QR Code'),
            ('ATMCARD', 'ATM Card')
        ],
        string="Payment Method",
        default='QR',
        required_if_provider="mbbank"
    )
    # qr_type = fields.Selection([
    #     ('type1_dynamic', 'Type 1 Dynamic'),
    #     ('type1_static', 'Type 1 Static'),
    #     ('type3', 'Type 3'),
    #     ('type4', 'Type 4')
    # ], string="QR Type", default='type1_dynamic', required_if_provider="mbbank")
    # payment_type = fields.Selection([
    #     (0, 'Master Merchant'),
    #     (1, 'Sub-Merchant')
    # ], string="Payment Type", default=1, required_if_provider="mbbank")

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
            providers = providers.filtered(lambda p: p.code != "mbbank")

        return providers

    def _get_supported_currencies(self):
        """Override to return the supported currencies."""
        supported_currencies = super()._get_supported_currencies()
        if self.code == "mbbank":
            supported_currencies = supported_currencies.filtered(
                lambda c: c.name in const.SUPPORTED_CURRENCIES
            )
        return supported_currencies

    def _get_mbbank_api_url(self):
        """Get the appropriate MB Bank API URL based on environment."""
        base_url = const.SANDBOX_DOMAIN if self.state == 'test' else const.PRODUCTION_DOMAIN
        return f"{base_url}{const.CREATE_ORDER_PATH}"

    def _get_mbbank_refund_url(self):
        """Get the appropriate MB Bank API URL based on environment."""
        base_url = const.SANDBOX_DOMAIN if self.state == 'test' else const.PRODUCTION_DOMAIN
        return f"{base_url}{const.REFUND_PATH}"

    def _get_mbbank_auth_token(self):
        """Get OAuth 2.0 token for MB Bank API using Basic Authentication."""
        from odoo.addons.mbbank_odoo import const
        import base64
        import requests

        auth_endpoint = f"{const.SANDBOX_DOMAIN}/oauth2/v1/token"

        # Sử dụng username và password được cung cấp
        username = self.mb_username  # RKzfCQIZBosvPVSXbi4kL4LRg45njNjr
        password = self.mb_password  # 6eV24s6QAysGlo8w

        # Tạo chuỗi xác thực Basic
        auth_string = f"{username}:{password}"
        auth_header_value = base64.b64encode(auth_string.encode('utf-8')).decode('utf-8')

        headers = {
            'Authorization': f'Basic {auth_header_value}',
            'Content-Type': 'application/x-www-form-urlencoded'
        }

        data = {
            'grant_type': 'client_credentials'
        }

        try:
            response = requests.post(auth_endpoint, headers=headers, data=data)
            if response.status_code == 200:
                token_data = response.json()
                return token_data.get('access_token')
            else:
                _logger.error("Failed to obtain MB Bank authorization token: %s", response.text)
                return None
        except Exception as e:
            _logger.exception("Error obtaining MB Bank token: %s", str(e))
            return None

    def _generate_mbbank_signature(self, params, mac_type='MD5'):
        """Generate signature for MB Bank request.

        Args:
            params: Dictionary of parameters to sign
            mac_type: Type of MAC algorithm to use ('MD5' or 'SHA256')
        """
        # Tạo bản sao của params để không thay đổi params gốc
        sign_params = {k: str(v) for k, v in params.items() if k not in ['mac', 'mac_type']}

        # Sắp xếp tham số theo thứ tự bảng chữ cái
        sign_string = "&".join([f"{key}={sign_params[key]}" for key in sorted(sign_params.keys())])

        # Tạo dữ liệu để băm - Hash Secret đặt ở đầu chuỗi
        sign_data = self.mb_hash_secret + sign_string

        # Log để debug
        _logger.debug(f"String to hash ({mac_type}): {sign_data}")

        # Tạo băm dựa trên kiểu mac
        if mac_type.upper() == 'MD5':
            return hashlib.md5(sign_data.encode('utf-8')).hexdigest().upper()
        else:  # SHA256
            return hashlib.sha256(sign_data.encode('utf-8')).hexdigest().upper()

    def _get_default_payment_method_codes(self):
        """Override of payment to return the default payment method codes."""
        default_codes = super()._get_default_payment_method_codes()
        if self.code != "mbbank":
            return default_codes
        return const.DEFAULT_PAYMENT_METHODS_CODES
