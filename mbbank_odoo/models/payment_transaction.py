import logging
import json
import uuid
import hmac
import hashlib
import requests
from datetime import datetime, timedelta
from werkzeug import urls

from odoo import _, models, fields, api
from odoo.exceptions import ValidationError
from odoo.http import request
from odoo.addons.mbbank_odoo import const
from odoo.addons.mbbank_odoo.controllers.main import MBBankController

_logger = logging.getLogger(__name__)


class PaymentTransaction(models.Model):
    _inherit = "payment.transaction"

    mb_session_id = fields.Char(string="MB Bank Session ID", readonly=True)
    mb_transaction_id = fields.Char(string="MB Bank Transaction ID", readonly=True)
    mb_ft_code = fields.Char(string="MB Bank FT Code", readonly=True)
    mb_query_start_time = fields.Datetime(string="MB Bank Query Start Time")
    mb_payment_url = fields.Char(string="MB Bank Payment URL", readonly=True)
    mb_qr_url = fields.Char(string="MB Bank QR URL", readonly=True)
    mb_processing_id = fields.One2many('mbbank.transaction.processing', 'transaction_id',
                                       string='MB Bank Processing Record')
    mb_retry_id = fields.One2many('mbbank.transaction.retry', 'transaction_id', string='MB Bank Retry Record')

    def _get_specific_rendering_values(self, processing_values):
        """Override to return MB Bank-specific rendering values."""
        self.ensure_one()
        res = super()._get_specific_rendering_values(processing_values)
        if self.provider_code != "mbbank":
            return res

        # Khởi tạo và lấy token OAuth
        token = self.provider_id._get_mbbank_auth_token()
        if not token:
            return {
                'error': _("Failed to obtain authorization token from MB Bank")
            }

        # Tạo URL API và headers
        create_order_url = self.provider_id._get_mbbank_api_url()
        headers = {
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
            'ClientMessageId': str(uuid.uuid4())
        }

        # Chuẩn bị tham số
        ipn_url = urls.url_join("https://pay-dev.t4tek.tk", MBBankController._ipn_url)
        return_url = urls.url_join("https://pay-dev.t4tek.tk", MBBankController._return_url)
        cancel_url = urls.url_join("https://pay-dev.t4tek.tk", MBBankController._cancel_url)

        params = {
            'amount': str(int(self.amount)),
            'currency': 'VND',
            'access_code': self.provider_id.mb_access_code,
            'mac_type': 'MD5',
            'merchant_id': self.provider_id.mb_merchant_id,
            'order_info': f"Payment for {self.reference}",
            'order_reference': self.reference,
            'return_url': return_url,
            'cancel_url': cancel_url,
            'ipn_url': ipn_url,
            'pay_type': 'pay',
            'payment_method': self.provider_id.mb_payment_method,
        }

        # Tạo MAC signature
        params['mac'] = self.provider_id._generate_mbbank_signature(params, 'MD5')

        # Gửi request
        try:
            response = requests.post(create_order_url, json=params, headers=headers)
            response_data = response.json()

            if response_data.get('error_code') == '00':
                # Lưu thông tin phản hồi
                self.mb_session_id = response_data.get('session_id')
                self.mb_payment_url = response_data.get('payment_url')
                self.mb_qr_url = response_data.get('qr_url')
                self.mb_query_start_time = fields.Datetime.now()

                # Tạo bản ghi processing
                ProcessingModel = self.env['mbbank.transaction.processing'].sudo()
                ProcessingModel.create_processing_transaction(
                    transaction=self,
                    signature=params['mac'],
                    request_id=params['order_reference']
                )

                # Trả về URL thanh toán hoặc URL QR code
                if self.provider_id.mb_payment_method == 'QR':
                    return {
                        'api_url': response_data.get('qr_url'),
                    }
                else:
                    return {
                        'api_url': response_data.get('payment_url')
                    }
            else:
                error_message = response_data.get('message', 'Unknown error')
                _logger.error("Error creating MB Bank payment: %s", error_message)
                return {
                    'error': error_message
                }
        except Exception as e:
            _logger.exception("Error processing MB Bank payment request: %s", str(e))
            return {
                'error': str(e)
            }

    def _verify_mbbank_signature(self, notification_data):
        """Verify the signature from MB Bank notification data."""
        if 'mac' not in notification_data:
            _logger.warning("MB Bank notification missing signature")
            return False

        # Lưu lại các giá trị để không bị mất sau khi pop
        received_mac = notification_data.get('mac')
        mac_type = notification_data.get('mac_type', 'SHA256')  # Mặc định SHA256 cho IPN

        # Tạo bản sao dữ liệu không bao gồm các trường liên quan đến MAC
        verification_data = {k: v for k, v in notification_data.items() if k not in ['mac', 'mac_type']}

        # Tạo chuỗi ký tự cho chữ ký - sắp xếp alphabet
        sign_string = "&".join([f"{k}={v}" for k, v in sorted(verification_data.items())])

        # Thêm hash_secret vào đầu chuỗi - điều quan trọng
        sign_data = self.provider_id.mb_hash_secret + sign_string

        _logger.debug(f"Signature verification - Data to sign: {sign_data}")

        # Tính toán chữ ký - Với IPN luôn sử dụng SHA256
        # Với các API khác, sử dụng mac_type từ notification_data
        if mac_type.upper() == 'MD5':
            calculated_mac = hashlib.md5(sign_data.encode('utf-8')).hexdigest().upper()
        else:  # Mặc định là SHA256
            calculated_mac = hashlib.sha256(sign_data.encode('utf-8')).hexdigest().upper()

        # Log để kiểm tra lỗi
        _logger.debug(f"Signature: Received={received_mac}, Calculated={calculated_mac}")

        # So sánh chữ ký an toàn
        is_valid = hmac.compare_digest(received_mac, calculated_mac)
        if not is_valid:
            _logger.warning(f"Signature verification failed: {received_mac} vs {calculated_mac}")

        return is_valid

    def _query_mbbank_transaction_status(self):
        """Query the current status of an MB Bank transaction."""
        self.ensure_one()
        _logger.info("Querying MB Bank transaction status for %s", self.reference)

        # Khởi tạo và lấy token OAuth
        token = self.provider_id._get_mbbank_auth_token()
        if not token:
            _logger.error("Failed to obtain MB Bank token for transaction status query")
            return

        # Chuẩn bị tham số truy vấn
        params = {
            'merchant_id': self.provider_id.mb_merchant_id,
            'order_reference': self.reference,
            'mac_type': 'MD5',  # API truy vấn giao dịch V2 sử dụng MD5
            'pay_date': fields.Date.context_today(self).strftime('%d%m%Y')
        }

        # Tạo MAC signature - với MD5 theo tài liệu
        params['mac'] = self.provider_id._generate_mbbank_signature(params, 'MD5')

        # Headers
        headers = {
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
            'ClientMessageId': str(uuid.uuid4())
        }

        # Call API
        try:
            api_url = f"{const.SANDBOX_DOMAIN if self.provider_id.state == 'test' else const.PRODUCTION_DOMAIN}/private/ms/pg-paygate-authen/v2/paygate/detail"
            response = requests.post(api_url, json=params, headers=headers)
            response_data = response.json()

            if response_data.get('error_code') == '00':
                # Process transaction based on resp_code
                resp_code = response_data.get('resp_code')
                if resp_code == '00':
                    self._set_done()
                    self.mb_transaction_id = response_data.get('transaction_number')
                    self.mb_ft_code = response_data.get('ft_code')
                elif resp_code in ['12', '16']:
                    self._set_pending()
                else:
                    self._set_error(f"MB Bank: {response_data.get('message', 'Unknown error')}")
            else:
                _logger.warning("MB Bank transaction status query failed: %s", response_data.get('message'))
        except Exception as e:
            _logger.exception("Error querying MB Bank transaction status: %s", str(e))
