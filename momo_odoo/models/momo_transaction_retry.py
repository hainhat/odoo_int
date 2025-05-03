from odoo import models, fields, api, _
import logging
import hmac
import hashlib
import uuid
import requests
from datetime import datetime, timedelta

_logger = logging.getLogger(__name__)


class MoMoTransactionRetry(models.Model):
    _name = 'momo.transaction.retry'
    _description = 'MoMo Transactions Retry Queue'
    _rec_name = 'reference'
    _order = 'next_retry asc, retry_count asc'

    name = fields.Char(string='Name', compute='_compute_name')
    transaction_id = fields.Many2one('payment.transaction', string='Transaction',
                                     required=True, ondelete='cascade', index=True,
                                     domain=[('provider_code', '=', 'momo')])
    reference = fields.Char(string='Reference', related='transaction_id.reference',
                            store=True, index=True)
    signature = fields.Char(string='Signature')
    next_retry = fields.Datetime(string='Next Retry', index=True)
    retry_count = fields.Integer(string='Retry Count', default=0)
    max_retries = fields.Integer(string='Max Retries', default=5)
    momo_request_id = fields.Char(string='Current Request ID')
    original_request_id = fields.Char(string='Original Request ID')
    idempotency_expiry = fields.Datetime(string='Idempotency Expiry')
    error_message = fields.Text(string='Error Message')
    state = fields.Selection([
        ('retry', 'To Retry'),
        ('processing', 'Processing')
    ], string='Status', default='retry', index=True)
    create_date = fields.Datetime(string='Created On', index=True, readonly=True)

    def _compute_name(self):
        for record in self:
            record.name = f"Retry: {record.reference or ''} (Attempt {record.retry_count + 1}/{record.max_retries})"

    @api.model
    def create_retry_transaction(self, transaction, signature=None, request_id=None, error_message=None):
        """Create a retry transaction record with idempotency support."""
        # Kiểm tra xem đã có bản ghi retry nào cho transaction này chưa
        existing_retry = self.search([('transaction_id', '=', transaction.id)], limit=1)
        if existing_retry:
            _logger.info(f"Found existing retry record {existing_retry.id} for transaction {transaction.reference}")
            return existing_retry

        # Tạo bản ghi mới nếu chưa có
        idempotency_expiry = fields.Datetime.now() + timedelta(days=31)
        next_retry = fields.Datetime.now() + timedelta(minutes=5)
        new_request_id = request_id or str(uuid.uuid4())

        values = {
            'transaction_id': transaction.id,
            'signature': signature,
            'momo_request_id': new_request_id,
            'original_request_id': new_request_id,
            'idempotency_expiry': idempotency_expiry,
            'error_message': error_message,
            'next_retry': next_retry,
            'state': 'retry'
        }
        retry_record = self.create(values)
        _logger.info(f"Created retry record {retry_record.id} for transaction {transaction.reference}")
        return retry_record

    def action_view_original_transaction(self):
        """Open the original transaction form view"""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'payment.transaction',
            'res_id': self.transaction_id.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def _check_and_update_idempotency(self):
        """Check and update idempotency keys if needed"""
        self.ensure_one()

        # Check if idempotency key expired (31 days)
        if fields.Datetime.now() > self.idempotency_expiry:
            # Create new request_id if expired
            new_request_id = str(uuid.uuid4())
            self.write({
                'momo_request_id': new_request_id,
                'original_request_id': new_request_id,
                'idempotency_expiry': fields.Datetime.now() + timedelta(days=31)
            })
            _logger.info(f"Updated idempotency key for transaction {self.reference}")

        return True

    def retry_transaction(self):
        """Query MoMo and update transaction with minimal access to main model"""
        self.ensure_one()

        if self.retry_count >= self.max_retries:
            # Max retries reached, update main model and delete record
            self.transaction_id._set_error(
                f"MoMo: Max retry attempts reached. Last error: {self.error_message}")
            self.sudo().unlink()
            _logger.info(f"Max retry attempts reached for transaction {self.reference}")
            return False

        # Update retry count and state
        self.write({
            'state': 'processing',
            'retry_count': self.retry_count + 1
        })
        _logger.info(f"Processing retry #{self.retry_count} for transaction {self.reference}")

        # Check idempotency key
        if self._check_and_update_idempotency():
            return self._perform_query_to_momo()
        return False

    def _perform_query_to_momo(self):
        """Execute actual query to MoMo API"""
        self.ensure_one()
        tx = self.transaction_id
        provider = tx.provider_id

        try:
            # Prepare query params for MoMo status check
            params = {
                'partnerCode': provider.momo_partner_code,
                'accessKey': provider.momo_access_key,
                'requestId': self.momo_request_id,  # Use current request ID
                'orderId': tx.reference,
                'lang': 'vi'
            }

            # Create signature
            signature_base = (
                f"accessKey={params['accessKey']}"
                f"&orderId={params['orderId']}"
                f"&partnerCode={params['partnerCode']}"
                f"&requestId={params['requestId']}"
            )

            h = hmac.new(
                bytes(provider.momo_secret_key, 'utf-8'),
                bytes(signature_base, 'utf-8'),
                hashlib.sha256
            )
            params['signature'] = h.hexdigest()

            # Query MoMo
            base_url = "https://test-payment.momo.vn"
            endpoint = f"{base_url}/v2/gateway/api/query"

            # Log request for debugging
            _logger.info(f"Sending MoMo status query for {self.reference} with requestId: {params['requestId']}")

            response = requests.post(
                endpoint,
                json=params,
                headers={'Content-Type': 'application/json'},
                timeout=30
            )

            response_data = response.json()
            _logger.info(f"MoMo response: {response_data}")

            # Process response
            self._process_momo_response(response_data)
            return True

        except Exception as e:
            # Log error and schedule retry
            _logger.exception(f"Error querying MoMo status for {self.reference}: {str(e)}")
            next_retry = fields.Datetime.now() + timedelta(minutes=max(5, 2 ** self.retry_count))
            self.write({
                'state': 'retry',
                'next_retry': next_retry,
                'error_message': str(e)
            })
            return False

    def _process_momo_response(self, response_data):
        """Process MoMo response data"""
        self.ensure_one()
        tx = self.transaction_id
        _logger.info(f"Processing response for {tx.reference}")

        result_code = response_data.get('resultCode')
        result_code_int = int(result_code) if isinstance(result_code, str) and result_code.isdigit() else result_code

        _logger.info(f"MoMo resultCode: {result_code_int}")

        # Xử lý theo resultCode
        if result_code_int == 0:  # Thành công
            tx._set_done()
            self.sudo().unlink()
            _logger.info(f"Transaction {tx.reference} marked as DONE")
            return True

        elif result_code_int == 9000:  # Authorized
            tx._set_authorized()
            self.sudo().unlink()
            _logger.info(f"Transaction {tx.reference} marked as AUTHORIZED")
            return True

        elif result_code_int in [1000, 7000, 7002]:  # Đang xử lý
            # Lên lịch retry với exponential backoff
            next_retry = fields.Datetime.now() + timedelta(minutes=max(5, 2 ** self.retry_count))
            self.write({
                'state': 'retry',
                'next_retry': next_retry,
                'error_message': f"Still processing, retrying in {max(5, 2 ** self.retry_count)} minutes"
            })
            _logger.info(f"Transaction {tx.reference} still processing, scheduled retry for {next_retry}")
            return False

        elif result_code_int == 40:  # Trùng requestId
            # Tạo requestId mới cho lần retry tiếp theo
            new_request_id = str(uuid.uuid4())
            next_retry = fields.Datetime.now() + timedelta(minutes=5)

            self.write({
                'momo_request_id': new_request_id,
                'state': 'retry',
                'next_retry': next_retry,
                'error_message': f"Duplicate requestId detected. Created new ID: {new_request_id}"
            })
            _logger.info(f"Duplicate requestId detected for {tx.reference}. Created new ID: {new_request_id}")
            return False

        else:
            # Các mã lỗi khác
            error_message = response_data.get('message', 'Unknown error')
            # Một số mã lỗi transaction thất bại vĩnh viễn
            if result_code_int in [1003, 1005, 1006, 41, 42]:
                tx._set_error(f"MoMo: {error_message}")
                self.sudo().unlink()
                _logger.info(
                    f"Transaction {tx.reference} marked as permanent ERROR with code {result_code}: {error_message}")
                return False
            else:
                # Các mã lỗi khác có thể tạm thời, tiếp tục retry
                next_retry = fields.Datetime.now() + timedelta(minutes=max(5, 2 ** self.retry_count))
                self.write({
                    'state': 'retry',
                    'next_retry': next_retry,
                    'error_message': f"Error code {result_code}: {error_message}"
                })
                _logger.info(
                    f"Transaction {tx.reference} temporary error code {result_code}, scheduled retry for {next_retry}")
                return False

    @api.model
    def _cron_process_transaction_retries(self):
        """Process transactions whose next_retry time has come"""
        _logger.info("Starting MoMo transaction retry processing cron job")

        current_time = fields.Datetime.now()
        _logger.info(f"Current server time: {current_time}")
        # Hiển thị tất cả các bản ghi trong trạng thái retry để debug
        all_retry_records = self.search([('state', '=', 'retry')])
        _logger.info(f"Total records in retry state: {len(all_retry_records)}")

        for record in all_retry_records:
            _logger.info(
                f"Record {record.id} - {record.reference}: next_retry={record.next_retry}, "
                f"retry_count={record.retry_count}, compare_result={(record.next_retry <= current_time)}")

        # Tìm tất cả bản ghi đã tới thời hạn retry
        domain = [
            ('state', '=', 'retry'),
            ('next_retry', '<=', current_time),
            ('retry_count', '<', 5)
        ]

        record_ids = self.search(domain).ids
        _logger.info(f"Found {len(record_ids)} MoMo transactions to retry with domain {domain}")

        # Xử lý từng bản ghi một cách độc lập theo ID
        for record_id in record_ids:
            try:
                # Tìm lại bản ghi theo ID tại thời điểm xử lý
                record = self.browse(record_id).exists()
                # Kiểm tra xem bản ghi có tồn tại không
                if not record:
                    _logger.info(f"Record with ID {record_id} no longer exists, skipping")
                    continue

                _logger.info(f"Starting to process transaction {record.reference}")

                # Xử lý bản ghi
                record.retry_transaction()
                # Commit sau mỗi bản ghi để đảm bảo thay đổi được lưu
                self.env.cr.commit()

            except Exception as e:
                _logger.exception(f"Error processing retry for transaction ID {record_id}: {e}")
                self.env.cr.rollback()

        _logger.info("Finished MoMo transaction retry processing cron job")
