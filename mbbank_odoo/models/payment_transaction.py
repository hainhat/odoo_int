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
    mb_expire_time = fields.Datetime(string="MB Bank Expire Time", readonly=True)
    # mb_refund_id = fields.Char(string="MB Bank Refund ID", readonly=True)

    # @api.model
    # def _compute_reference(self, provider_code, prefix=None, separator='c', **kwargs):
    #     """Override để tạo reference không chứa ký tự đặc biệt cho MB Bank."""
    #     if provider_code == 'mbbank' or provider_code == 'mbbankqr':
    #         # Tạo reference mới không chứa dấu "-"
    #         # Nếu có prefix, sử dụng nó
    #         if prefix:
    #             return f"{prefix}{''.join(str(uuid.uuid4()).split('-'))[:8]}"
    #         else:
    #             return f"{''.join(str(uuid.uuid4()).split('-'))[:16]}"
    #     else:
    #         # Giữ nguyên hành vi cho các provider khác
    #         return super()._compute_reference(provider_code, prefix, separator, **kwargs)

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
        # ipn_url = urls.url_join("https://pay-dev.t4tek.tk", MBBankController._ipn_url)
        ipn_url = "https://api-sandbox.mbbank.com.vn/integration-paygate-t4tek/v1.0/payIpn"
        return_url = urls.url_join("https://pay-dev.t4tek.tk", MBBankController._return_url)
        cancel_url = urls.url_join("https://pay-dev.t4tek.tk", MBBankController._cancel_url)

        params = {
            'amount': str(int(self.amount)),
            'currency': 'VND',
            'access_code': self.provider_id.mb_access_code,
            'mac_type': 'MD5',
            'merchant_id': self.provider_id.mb_merchant_id,
            'order_info': f"Payment for {self.reference}",
            'order_reference': f"PSQR{self.reference.replace('-', 'e')}",
            'return_url': return_url,
            'cancel_url': cancel_url,
            'ipn_url': ipn_url,
            'pay_type': 'pay',
            'payment_method': self.provider_id.mb_payment_method,
        }
        # Thêm log để hiển thị tham số trước khi tạo chữ ký
        _logger.info("====== MB BANK REQUEST PARAMS ======")
        _logger.info(json.dumps(params, indent=2))
        _logger.info("======================================")

        # Tạo MAC signature
        params['mac'] = self.provider_id._generate_mbbank_signature(params, 'MD5')

        # Gửi request
        try:
            response = requests.post(create_order_url, json=params, headers=headers)
            response_data = response.json()
            _logger.info("====== MB BANK RESPONSE ======")
            _logger.info(json.dumps(response_data, indent=2))
            _logger.info("==============================")

            if response_data.get('error_code') == '00':
                # Lưu thông tin phản hồi
                self.mb_session_id = response_data.get('session_id')
                self.mb_payment_url = response_data.get('payment_url')
                self.mb_qr_url = response_data.get('qr_url')
                self.mb_query_start_time = fields.Datetime.now()
                # Lưu expire_time từ response
                expire_time_str = response_data.get('expire_time')
                _logger.info(f"Parsing expire_time: {expire_time_str}")
                expire_time = datetime.strptime(expire_time_str, '%d-%m-%Y %H:%M:%S')
                self.mb_expire_time = expire_time
                _logger.info(f"Parsed expire_time to: {expire_time}")

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
    # def _get_specific_rendering_values(self, processing_values):
    #     """Override to return MB Bank-specific rendering values."""
    #     self.ensure_one()
    #     res = super()._get_specific_rendering_values(processing_values)
    #     if self.provider_code != "mbbank":
    #         return res
    #
    #     # Get OAuth token
    #     token = self.provider_id._get_mbbank_auth_token()
    #     if not token:
    #         return {'error': _("Failed to obtain authorization token from MB Bank")}
    #
    #     # Prepare API URL and headers
    #     create_order_url = self.provider_id._get_mbbank_api_url()
    #     headers = {
    #         'Authorization': f'Bearer {token}',
    #         'Content-Type': 'application/json',
    #         'ClientMessageId': str(uuid.uuid4())
    #     }
    #
    #     # Base parameters
    #     ipn_url = "https://api-sandbox.mbbank.com.vn/integration-paygate-t4tek/v1.0/payIpn"
    #     return_url = urls.url_join("https://pay-dev.t4tek.tk", MBBankController._return_url)
    #     cancel_url = urls.url_join("https://pay-dev.t4tek.tk", MBBankController._cancel_url)
    #
    #     # Determine QR type and PAYMENT_TYPE from configuration or context
    #     qr_type = processing_values.get('qr_type',
    #                                     'type1_dynamic')  # Example: 'type1_dynamic', 'type1_static', 'type3', 'type4'
    #     payment_type = processing_values.get('payment_type', 1)  # 1 for Sub-Merchant, 0 for Master Merchant
    #     merchant_id = "SUB123456" if payment_type == 1 else "MASTER123456"
    #
    #     # Initialize parameters
    #     params = {
    #         'currency': 'VND',
    #         'access_code': self.provider_id.mb_access_code,
    #         'mac_type': 'MD5',
    #         'merchant_id': merchant_id,
    #         'order_info': f"Payment for {self.reference}",
    #         'order_reference': f"PSQR{self.reference.replace('-', 'e')}",
    #         'ipn_url': ipn_url,
    #         'return_url': return_url,
    #         'cancel_url': cancel_url,
    #         'pay_type': 'pay',
    #         'payment_method': 'QR',
    #     }
    #
    #     # Adjust parameters based on QR type
    #     if qr_type == 'type1_dynamic':
    #         params['amount'] = str(int(self.amount))
    #         params['device'] = "os={name=Windows, version=windows-10},browser={name=Chrome, version=90.0.4430.85}"
    #         params['ip_address'] = "192.168.1.1"
    #     elif qr_type == 'type1_static':
    #         params['amount'] = "0"
    #         params['order_reference'] = f"PSQRSTATIC{self.reference.replace('-', 'e')[:16]}"
    #     elif qr_type == 'type3':
    #         params['amount'] = str(int(self.amount))
    #         params['merchant_user_reference'] = "CUST12345"
    #     elif qr_type == 'type4':
    #         params['amount'] = str(int(self.amount))
    #         params['token_issuer_code'] = "MBBANK"
    #         params['token'] = "<payment_token>"
    #
    #     # Generate MAC signature
    #     params['mac'] = self.provider_id._generate_mbbank_signature(params, 'MD5')
    #
    #     # Send request
    #     try:
    #         response = requests.post(create_order_url, json=params, headers=headers)
    #         response_data = response.json()
    #         _logger.info("====== MB BANK RESPONSE ======")
    #         _logger.info(json.dumps(response_data, indent=2))
    #         _logger.info("==============================")
    #
    #         if response_data.get('error_code') == '00':
    #             self.mb_session_id = response_data.get('session_id')
    #             self.mb_payment_url = response_data.get('payment_url')
    #             self.mb_qr_url = response_data.get('qr_url')
    #             self.mb_query_start_time = fields.Datetime.now()
    #             self.mb_expire_time = datetime.strptime(response_data.get('expire_time'), '%d-%m-%Y %H:%M:%S')
    #
    #             ProcessingModel = self.env['mbbank.transaction.processing'].sudo()
    #             ProcessingModel.create_processing_transaction(
    #                 transaction=self,
    #                 signature=params['mac'],
    #                 request_id=params['order_reference']
    #             )
    #
    #             return {'api_url': response_data.get('qr_url')}
    #         else:
    #             error_message = response_data.get('message', 'Unknown error')
    #             _logger.error("Error creating MB Bank payment: %s", error_message)
    #             return {'error': error_message}
    #     except Exception as e:
    #         _logger.exception("Error processing MB Bank payment request: %s", str(e))
    #         return {'error': str(e)}

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

    def _send_refund_request(self, amount_to_refund=None):
        """Request a refund for the transaction through MB Bank API."""
        self.ensure_one()
        if self.provider_code != "mbbank":
            return super()._send_refund_request(amount_to_refund=amount_to_refund)

        _logger.info("Requesting refund for MB Bank transaction %s, amount: %s", self.reference, amount_to_refund)

        # Validate transaction state and amount
        if self.state != 'done':
            _logger.error("Cannot refund transaction %s: Transaction is not in 'done' state", self.reference)
            raise ValidationError(_("Cannot refund: Transaction must be completed."))

        if amount_to_refund is None:
            amount_to_refund = self.amount

        if amount_to_refund <= 0 or amount_to_refund > self.amount:
            _logger.error("Invalid refund amount for transaction %s: %s", self.reference, amount_to_refund)
            raise ValidationError(_("Refund amount must be positive and not exceed original amount."))

        # Get OAuth token
        token = self.provider_id._get_mbbank_auth_token()
        if not token:
            _logger.error("Failed to obtain MB Bank token for refund request")
            raise ValidationError(_("Failed to obtain authorization token from MB Bank"))

        # Prepare refund parameters
        params = {
            'txn_amount': str(amount_to_refund),  # Amount as string
            'desc': f"Refund for {self.reference}",  # Limit to 128 characters
            'access_code': self.provider_id.mb_access_code,
            'mac_type': 'MD5',  # Default as per documentation
            'merchant_id': self.provider_id.mb_merchant_id,
            'transaction_reference_id': self.mb_transaction_id or '',
            'trans_date': self.date.strftime('%d%m%Y') if self.date else fields.Date.today().strftime('%d%m%Y'),
        }

        # Generate MAC signature
        params['mac'] = self.provider_id._generate_mbbank_signature(params, 'MD5')

        # Log request parameters for debugging
        _logger.info("====== MB BANK REFUND REQUEST PARAMS ======")
        _logger.info(json.dumps(params, indent=2))
        _logger.info("======================================")

        # Prepare headers
        headers = {
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
            'ClientMessageId': str(uuid.uuid4())
        }

        # Create refund transaction
        refund_tx = self.env['payment.transaction'].create({
            'amount': -amount_to_refund,  # Negative amount for refund
            'currency_id': self.currency_id.id,
            'reference': f"REF-{self.reference}",
            'partner_id': self.partner_id.id,
            'provider_id': self.provider_id.id,
            'provider_code': self.provider_code,
            'source_transaction_id': self.id,
            'operation': 'refund',
        })

        # Call MB Bank refund API
        try:
            refund_url = self.provider_id._get_mbbank_refund_url()
            response = requests.post(refund_url, json=params, headers=headers, timeout=30)
            response_data = response.json()

            # Log response for debugging
            _logger.info("====== MB BANK REFUND RESPONSE ======")
            _logger.info(json.dumps(response_data, indent=2))
            _logger.info("==============================")

            # Process response
            if response_data.get('error_code') == '00':
                _logger.info("Refund successful for transaction %s", self.reference)
                # Update refund transaction
                refund_tx.write({
                    'state': 'done',
                    'state_message': f"Refund successful: {response_data.get('message', 'Success')}",
                    'mb_refund_id': response_data.get('refund_id'),
                    'mb_transaction_id': response_data.get('refund_id', self.mb_transaction_id),
                    'mb_ft_code': response_data.get('refund_reference_id', self.mb_ft_code),
                })
                # Update source transaction state
                if float(response_data.get('refund_amount')) == self.amount:
                    self.write({'state': 'refunded'})
                else:
                    self.write({'state': 'partially_refunded'})
                return refund_tx
            else:
                error_message = response_data.get('message', 'Unknown error')
                _logger.error("Refund failed for transaction %s: %s", self.reference, error_message)
                refund_tx._set_error(f"MB Bank: {error_message}")
                return refund_tx

        except Exception as e:
            _logger.exception("Error processing MB Bank refund request for %s: %s", self.reference, str(e))
            refund_tx._set_error(f"MB Bank: {str(e)}")
            return refund_tx
