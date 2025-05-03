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
from odoo.addons.momo_odoo import const
from odoo.addons.momo_odoo.controllers.main import MoMoController

_logger = logging.getLogger(__name__)


class PaymentTransaction(models.Model):
    _inherit = "payment.transaction"

    momo_transaction_id = fields.Char(string="MoMo Transaction ID", readonly=True)
    momo_query_status = fields.Boolean(string="MoMo Query Status", default=False)
    momo_query_start_time = fields.Datetime(string="MoMo Query Start Time")
    momo_payment_url = fields.Char(string="MoMo Payment URL", readonly=True)
    momo_payment_type = fields.Char(string="MoMo Payment Type", readonly=True)
    momo_pending_id = fields.One2many('momo.transaction.pending', 'transaction_id', string='MoMo Pending Record')
    momo_retry_id = fields.One2many('momo.transaction.retry', 'transaction_id', string='MoMo Retry Record')

    @api.model
    def create(self, vals):
        # Gọi phương thức create gốc
        transaction = super().create(vals)
        return transaction

    def _get_specific_rendering_values(self, processing_values):
        """Override to return MoMo-specific rendering values."""
        self.ensure_one()
        res = super()._get_specific_rendering_values(processing_values)
        if self.provider_code != "momo":
            return res

        base_url = self.provider_id.get_base_url()
        # Generate a unique request ID
        request_id = str(uuid.uuid4())

        # Get the appropriate request type based on configuration
        request_type = self.provider_id._get_momo_request_type()

        # Create URLs
        ipn_url = urls.url_join("https://pay-dev.t4tek.tk", MoMoController._ipn_url)
        redirect_url = urls.url_join("https://pay-dev.t4tek.tk", MoMoController._return_url)
        _logger.info("Generated MoMo URLs - IPN: %s, Redirect: %s", ipn_url, redirect_url)

        # Prepare parameters for MoMo API request
        params = {
            'partnerCode': self.provider_id.momo_partner_code,
            'accessKey': self.provider_id.momo_access_key,
            'requestId': request_id,
            'amount': str(int(self.amount)),
            'orderId': self.reference,
            'orderInfo': f"Payment for {self.reference}",
            'redirectUrl': redirect_url,
            'ipnUrl': ipn_url,
            'extraData': "",
            'requestType': request_type,
            'lang': "vi",
        }

        # Tạo chữ ký với các trường sắp xếp theo alphabet
        signature_keys = [
            'accessKey', 'amount', 'extraData', 'ipnUrl', 'orderId',
            'orderInfo', 'partnerCode', 'redirectUrl', 'requestId', 'requestType'
        ]
        signature_items = [f"{key}={params[key]}" for key in signature_keys]
        signature_base = "&".join(signature_items)

        # Tạo chữ ký bằng thuật toán HMAC-SHA256
        h = hmac.new(
            bytes(self.provider_id.momo_secret_key, 'utf-8'),
            bytes(signature_base, 'utf-8'),
            hashlib.sha256
        )
        signature = h.hexdigest()
        params['signature'] = signature

        # Record query start time for later status checks
        self.momo_query_start_time = fields.Datetime.now()

        # Gửi API request cho MoMo
        try:
            endpoint = self.provider_id._get_momo_api_url()
            headers = {
                'Content-Type': 'application/json',
                'Content-Length': str(len(json.dumps(params)))
            }

            response = requests.post(
                endpoint,
                json=params,
                headers=headers
            )

            response_data = response.json()
            _logger.info("MoMo payment request response: %s", response_data)

            # Kiểm tra nếu request thành công
            if response_data.get('resultCode') == 0:
                payment_url = response_data.get('payUrl')
                self.momo_payment_url = payment_url
                PendingModel = self.env['momo.transaction.pending'].sudo()
                PendingModel.create_pending_transaction(
                    transaction=self,
                    signature=signature,
                    request_id=request_id
                )
                return {
                    'api_url': payment_url,
                }
            else:
                error_message = response_data.get('message', 'Unknown error')
                _logger.error("Error creating MoMo payment: %s", error_message)
                return {
                    'error': error_message
                }

        except Exception as e:
            _logger.exception("Error processing MoMo payment request: %s", str(e))
            return {
                'error': str(e)
            }

    def _verify_momo_signature(self, notification_data):
        """Verify the signature from MoMo notification data."""
        if 'signature' not in notification_data:
            _logger.warning("MoMo notification missing signature")
            return False

        received_signature = notification_data.get('signature')
        secret_key = self.provider_id.momo_secret_key

        # Lấy accessKey từ cấu hình nếu không có trong dữ liệu thông báo
        if 'accessKey' not in notification_data and self.provider_id.momo_access_key:
            notification_data['accessKey'] = self.provider_id.momo_access_key

        # Lấy danh sách trường từ tài liệu MoMo
        expected_fields = [
            'accessKey', 'amount', 'extraData', 'message', 'orderId',
            'orderInfo', 'orderType', 'partnerCode', 'payType',
            'requestId', 'responseTime', 'resultCode', 'transId'
        ]

        # Tạo chuỗi signature theo đúng định dạng từ tài liệu MoMo
        raw_signature = ""
        for field in sorted(expected_fields):
            if field in notification_data and notification_data[field] is not None:
                if raw_signature:
                    raw_signature += "&"
                raw_signature += f"{field}={notification_data[field]}"

        # Log để debug
        _logger.info("Raw signature string: %s", raw_signature)

        # Generate HMAC-SHA256 signature
        h = hmac.new(
            bytes(secret_key, 'utf-8'),
            bytes(raw_signature, 'utf-8'),
            hashlib.sha256
        )
        calculated_signature = h.hexdigest()

        # Log để debug
        _logger.info("Received signature: %s", received_signature)
        _logger.info("Calculated signature: %s", calculated_signature)

        # Compare signatures securely
        result = hmac.compare_digest(received_signature, calculated_signature)
        _logger.info("Signature verification result: %s", "Success" if result else "Failed")
        return result

    # def _get_tx_from_notification_data(self, provider_code, notification_data):
    #     """Override to find the transaction based on MoMo data."""
    #     tx = super()._get_tx_from_notification_data(provider_code, notification_data)
    #     if provider_code != "momo" or len(tx) == 1:
    #         return tx
    #     _logger.info(
    #         f"_get_tx_from_notification_data called for provider: {provider_code}, reference: {notification_data.get('orderId')}")
    #
    #     reference = notification_data.get("orderId")
    #     if not reference:
    #         raise ValidationError(
    #             "MoMo: " + _("Received data with missing reference.")
    #         )
    #
    #     # Tìm trong pending model
    #     pending_tx = self.env['momo.transaction.pending'].sudo().search([
    #         ('reference', '=', reference)
    #     ], limit=1)
    #
    #     if pending_tx:
    #         return pending_tx.transaction_id
    #
    #     # Tìm trong retry model
    #     retry_tx = self.env['momo.transaction.retry'].sudo().search([
    #         ('reference', '=', reference)
    #     ], limit=1)
    #
    #     if retry_tx:
    #         return retry_tx.transaction_id
    #
    #     # Nếu không tìm thấy trong model phụ, tìm trong model chính
    #     tx = self.search(
    #         [("reference", "=", reference), ("provider_code", "=", "momo")]
    #     )
    #     if not tx:
    #         raise ValidationError(
    #             "MoMo: " + _("No transaction found matching reference %s.", reference)
    #         )
    #     return tx
    #
    # def _query_momo_transaction_status(self):
    #     """Query the current status of a MoMo transaction."""
    #     self.ensure_one()
    #     _logger.info("Querying MoMo transaction status for %s", self.reference)
    #     _logger.info(
    #         f"_query_momo_transaction_status called for transaction ID: {self.id}, reference: {self.reference}")
    #
    #     # Kiểm tra các bản ghi trong model phụ
    #     # Tìm trong retry model
    #     retry_record = self.env['momo.transaction.retry'].sudo().search([
    #         ('transaction_id', '=', self.id)
    #     ], limit=1)
    #
    #     if retry_record:
    #         # Xử lý qua retry record
    #         retry_record.retry_transaction()
    #         return
    #
    #     # Tìm trong pending model
    #     pending_record = self.env['momo.transaction.pending'].sudo().search([
    #         ('transaction_id', '=', self.id)
    #     ], limit=1)
    #
    #     # Define the maximum time to wait for a transaction to finalize (e.g., 15 minutes)
    #     max_wait_time = timedelta(minutes=15)
    #
    #     # Check if the current time exceeds the maximum wait time
    #     if fields.Datetime.now() > self.momo_query_start_time + max_wait_time:
    #         # Stop further queries and mark the transaction as timed out or failed
    #         self._set_error("MoMo: Transaction timed out")
    #         self.momo_query_status = True
    #         _logger.info("MoMo transaction %s timed out", self.reference)
    #         return
    #
    #     # Prepare query params
    #     params = {
    #         'partnerCode': self.provider_id.momo_partner_code,
    #         'accessKey': self.provider_id.momo_access_key,
    #         'requestId': str(uuid.uuid4()),
    #         'orderId': self.reference,
    #         'lang': 'vi'
    #     }
    #
    #     # Create signature string in correct format
    #     signature_base = (
    #         f"accessKey={params['accessKey']}"
    #         f"&orderId={params['orderId']}"
    #         f"&partnerCode={params['partnerCode']}"
    #         f"&requestId={params['requestId']}"
    #     )
    #
    #     # Generate HMAC-SHA256 signature
    #     h = hmac.new(
    #         bytes(self.provider_id.momo_secret_key, 'utf-8'),
    #         bytes(signature_base, 'utf-8'),
    #         hashlib.sha256
    #     )
    #     params['signature'] = h.hexdigest()
    #
    #     _logger.info("MoMo query params: %s", params)
    #
    #     # Query MoMo for transaction status
    #     try:
    #         # Determine API URL based on environment
    #         base_url = "https://test-payment.momo.vn" if self.provider_id.state == 'test' else "https://payment.momo.vn"
    #         endpoint = f"{base_url}/v2/gateway/api/query"
    #         _logger.info("Querying MoMo status at: %s", endpoint)
    #
    #         response = requests.post(
    #             endpoint,
    #             json=params,
    #             headers={'Content-Type': 'application/json'},
    #             timeout=30  # Set a reasonable timeout
    #         )
    #
    #         # Process the response
    #         response_data = response.json()
    #         _logger.info("MoMo status query response: %s", response_data)
    #
    #         # Nếu có pending record, xử lý qua pending
    #         if pending_record:
    #             pending_record.process_ipn_notification(response_data)
    #             return
    #
    #         # Xử lý trực tiếp trên model chính
    #         if response_data.get('resultCode') == 0:
    #             # Transaction exists, check specific status
    #             transaction_status = response_data.get('status')
    #             _logger.info("MoMo transaction %s status: %s", self.reference, transaction_status)
    #
    #             if transaction_status == 1 or transaction_status == '1':  # Successful transactions
    #                 # Save MoMo transaction ID
    #                 self.momo_transaction_id = response_data.get('transId')
    #                 self._set_done()
    #                 self.momo_query_status = True
    #                 _logger.info("MoMo transaction %s marked as done", self.reference)
    #             elif transaction_status in [0, '0', 2, '2', 3, '3']:  # Pending statuses
    #                 # Transaction still pending, do nothing
    #                 _logger.info("MoMo transaction %s still pending", self.reference)
    #             else:
    #                 # Other status indicates failure
    #                 error_message = response_data.get('message', 'Unknown error')
    #                 self._set_error(f"MoMo: {error_message}")
    #                 self.momo_query_status = True
    #                 _logger.info("MoMo transaction %s failed: %s", self.reference, error_message)
    #         else:
    #             # Failed query or transaction not found/failed
    #             error_message = response_data.get('message', 'Unknown error')
    #             result_code = response_data.get('resultCode')
    #
    #             # Some result codes indicate a permanent failure condition
    #             if result_code in [1003, 1005, 1006]:
    #                 self._set_error(f"MoMo: {error_message}")
    #                 self.momo_query_status = True
    #                 _logger.info("MoMo transaction %s failed with code %s: %s",
    #                              self.reference, result_code, error_message)
    #             else:
    #                 # Other codes might be temporary issues
    #                 _logger.info("MoMo transaction %s status check returned code %s: %s",
    #                              self.reference, result_code, error_message)
    #
    #     except requests.RequestException as e:
    #         _logger.info("Network error querying MoMo transaction status for %s: %s",
    #                      self.reference, str(e))
    #     except Exception as e:
    #         _logger.exception("Error querying MoMo transaction status for %s: %s",
    #                           self.reference, str(e))
    #
    #     # Schedule another query if the transaction is still pending and within the allowed time
    #     if (fields.Datetime.now() < self.momo_query_start_time + max_wait_time
    #             and not self.momo_query_status
    #             and self.state == 'pending'):
    #
    #         # Create a one-time cron job to check again in 5 minutes
    #         cron_name = f'Query MoMo Transaction Status for {self.reference}'
    #         existing_cron = self.env['ir.cron'].sudo().search([('name', '=', cron_name)], limit=1)
    #
    #         if existing_cron:
    #             existing_cron.write({
    #                 'nextcall': fields.Datetime.now() + timedelta(minutes=5),
    #             })
    #         else:
    #             self.env['ir.cron'].sudo().create({
    #                 'name': cron_name,
    #                 'model_id': self.env.ref('payment.model_payment_transaction').id,
    #                 'state': 'code',
    #                 'code': f'model.browse({self.id})._query_momo_transaction_status()',
    #                 'interval_number': 5,
    #                 'interval_type': 'minutes',
    #                 'numbercall': 1,
    #                 'nextcall': fields.Datetime.now() + timedelta(minutes=5),
    #                 'doall': True,
    #             })
    #
    # def _handle_notification_data(self, provider_code, notification_data):
    #     """Handle the notification data sent by MoMo."""
    #     # Kiểm tra có phải MoMo không
    #     if provider_code != "momo":
    #         return super()._handle_notification_data(provider_code, notification_data)
    #     _logger.info(
    #         f"_handle_notification_data called for provider: {provider_code}, transaction reference: {self.reference}")
    #
    #     _logger.info(
    #         "Handling notification data from MoMo for transaction reference %s",
    #         self.reference
    #     )
    #
    #     # Kiểm tra module transaction manager đã được cài đặt chưa
    #     transaction_manager_installed = self.env['ir.module.module'].sudo().search([
    #         ('name', '=', 'transaction_manager'),
    #         ('state', '=', 'installed')
    #     ])
    #
    #     # Nếu module đã cài đặt, xử lý qua model phụ
    #     if transaction_manager_installed:
    #         # Kiểm tra nếu transaction có bản ghi trong model momo.transaction.pending
    #         self._check_and_process_momo_transaction_models(notification_data)
    #     else:
    #         # Xử lý trực tiếp trên model chính
    #         if not notification_data:
    #             self._set_canceled(state_message=_("The customer left the payment page."))
    #             return
    #
    #         # Lưu MoMo transaction ID
    #         if transid := notification_data.get('transId'):
    #             self.momo_transaction_id = transid
    #             _logger.info("Saved MoMo transaction ID: %s", transid)
    #
    #         # Lưu loại thanh toán nếu có
    #         if pay_type := notification_data.get('payType'):
    #             self.momo_payment_type = pay_type
    #             _logger.info("Saved MoMo payment type: %s", pay_type)
    #
    #         # Xác thực số tiền nếu có trong thông báo
    #         amount = notification_data.get('amount')
    #         if amount and self.currency_id.compare_amounts(float(amount), self.amount) != 0:
    #             _logger.info(
    #                 "MoMo: Amount mismatch (stored: %s, notified: %s) for transaction reference %s",
    #                 self.amount, amount, self.reference
    #             )
    #             self._set_error("MoMo: Sai lệch số tiền thanh toán.")
    #             return
    #
    #         # Xử lý theo resultCode
    #         result_code = notification_data.get('resultCode')
    #         result_code_int = int(result_code) if isinstance(result_code,
    #                                                          str) and result_code.isdigit() else result_code
    #
    #         _logger.info("Processing MoMo resultCode: %s for transaction %s", result_code, self.reference)
    #
    #         if result_code_int == 0:  # Thành công
    #             self._set_done()
    #             self.momo_query_status = True
    #             _logger.info("Transaction %s marked as paid.", self.reference)
    #
    #         elif result_code_int == 9000:  # Đã xác thực nhưng chưa capture
    #             self._set_authorized()
    #             _logger.info("Transaction %s authorized (pre-auth).", self.reference)
    #
    #         elif result_code_int in [1000, 7000, 7002]:  # Đang xử lý
    #             self._set_pending()
    #             _logger.info("Transaction %s is still pending with code %s", self.reference, result_code)
    #
    #         elif result_code_int == 1003:  # Đã hủy
    #             self._set_canceled(state_message=f"MoMo: {notification_data.get('message', 'Transaction cancelled')}")
    #             self.momo_query_status = True
    #             _logger.info("Transaction %s cancelled.", self.reference)
    #
    #         else:  # Các mã lỗi khác
    #             error_message = notification_data.get('message', 'Unknown error')
    #             self._set_error(f"MoMo: {error_message}")
    #             self.momo_query_status = True
    #             _logger.info("Transaction %s failed with code %s: %s",
    #                          self.reference, result_code, error_message)
    #
    # def _check_and_process_momo_transaction_models(self, notification_data):
    #     """Kiểm tra và xử lý transaction trong các model của Transaction Manager"""
    #     _logger.info(
    #         f"_check_and_process_momo_transaction_models called for transaction ID: {self.id}, reference: {self.reference}")
    #     # Kiểm tra bản ghi trong model pending
    #     pending_record = self.env['momo.transaction.pending'].sudo().search([
    #         ('transaction_id', '=', self.id)
    #     ], limit=1)
    #
    #     if pending_record:
    #         # Cập nhật trạng thái trong pending record
    #         pending_record.process_ipn_notification(notification_data)
    #         return
    #
    #     # Kiểm tra bản ghi trong model retry
    #     retry_record = self.env['momo.transaction.retry'].sudo().search([
    #         ('transaction_id', '=', self.id)
    #     ], limit=1)
    #
    #     if retry_record:
    #         # Xử lý qua retry record
    #         retry_record.retry_transaction()
    #         return
    #
    #     # Xử lý trực tiếp thay vì tạo mới bản ghi pending
    #     result_code = notification_data.get('resultCode')
    #     result_code_int = int(result_code) if isinstance(result_code, str) and result_code.isdigit() else result_code
    #     message = notification_data.get('message', 'Unknown response')
    #
    #     # Nếu là lỗi hệ thống, tạo retry record
    #     if result_code_int in [10, 11, 12, 99]:
    #         self.env['momo.transaction.retry'].sudo().create_retry_transaction(
    #             transaction=self,
    #             error_message=notification_data.get('message', 'System error')
    #         )
    #         return
    #
    #     # Xử lý kết quả trực tiếp
    #     if result_code_int == 0:  # Success
    #         self._set_done()
    #         _logger.info("Transaction %s marked as DONE", self.reference)
    #     elif result_code_int == 9000:  # Authorized
    #         self._set_authorized()
    #         _logger.info("Transaction %s marked as AUTHORIZED", self.reference)
    #     elif result_code_int in [1000, 7000, 7002]:  # Still processing
    #         self._set_pending()
    #         _logger.info("Transaction %s still PENDING", self.reference)
    #     elif result_code_int == 1003:  # Cancelled
    #         self._set_canceled(state_message=f"MoMo: {message}")
    #         _logger.info("Transaction %s marked as CANCELED", self.reference)
    #     else:  # Other errors
    #         self._set_error(f"MoMo: {message}")
    #         _logger.info("Transaction %s marked as ERROR with code %s: %s", self.reference, result_code, message)
