from odoo import models, fields, api, _
import logging
import hmac
import hashlib
import uuid
import requests
from datetime import datetime, timedelta

_logger = logging.getLogger(__name__)


class MBBankTransactionRetry(models.Model):
    _name = 'mbbank.transaction.retry'
    _description = 'MB Bank Transactions Retry Queue'
    _rec_name = 'reference'
    _order = 'next_retry asc, retry_count asc'

    name = fields.Char(string='Name', compute='_compute_name')
    transaction_id = fields.Many2one('payment.transaction', string='Transaction',
                                     required=True, ondelete='cascade', index=True,
                                     domain=[('provider_code', '=', 'mbbank')])
    reference = fields.Char(string='Reference', related='transaction_id.reference',
                            store=True, index=True)
    signature = fields.Char(string='Signature')
    next_retry = fields.Datetime(string='Next Retry', index=True)
    retry_count = fields.Integer(string='Retry Count', default=0)
    max_retries = fields.Integer(string='Max Retries', default=5)
    mb_request_id = fields.Char(string='Current Request ID')
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
        # Check if there's already a retry record for this transaction
        existing_retry = self.search([('transaction_id', '=', transaction.id)], limit=1)
        if existing_retry:
            _logger.info(f"Found existing retry record {existing_retry.id} for transaction {transaction.reference}")
            return existing_retry

        # Create new record if none exists
        idempotency_expiry = fields.Datetime.now() + timedelta(days=31)
        next_retry = fields.Datetime.now() + timedelta(minutes=5)
        new_request_id = request_id or str(uuid.uuid4())

        values = {
            'transaction_id': transaction.id,
            'signature': signature,
            'mb_request_id': new_request_id,
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
                'mb_request_id': new_request_id,
                'original_request_id': new_request_id,
                'idempotency_expiry': fields.Datetime.now() + timedelta(days=31)
            })
            _logger.info(f"Updated idempotency key for transaction {self.reference}")

        return True

    def retry_transaction(self):
        """Query MB Bank and update transaction with minimal access to main model"""
        self.ensure_one()

        if self.retry_count >= self.max_retries:
            # Max retries reached, update main model and delete record
            self.transaction_id._set_error(
                f"MB Bank: Max retry attempts reached. Last error: {self.error_message}")
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
            return self._perform_query_to_mbbank()
        return False

    def _perform_query_to_mbbank(self):
        """Execute actual query to MB Bank API"""
        self.ensure_one()
        tx = self.transaction_id
        provider = tx.provider_id

        try:
            # Get OAuth token
            token = provider._get_mbbank_auth_token()
            if not token:
                _logger.error("Failed to obtain MB Bank token for retry")
                next_retry = fields.Datetime.now() + timedelta(minutes=max(5, 2 ** self.retry_count))
                self.write({
                    'state': 'retry',
                    'next_retry': next_retry,
                    'error_message': "Failed to obtain authorization token"
                })
                return False

            # Prepare query params for MB Bank status check
            params = {
                'merchant_id': provider.mb_merchant_id,
                'order_reference': tx.reference,
                'mac_type': 'MD5',  # API truy vấn giao dịch sử dụng MD5
                'pay_date': fields.Date.context_today(self).strftime('%d%m%Y')
            }

            # Create signature - sử dụng MD5 theo tài liệu
            params['mac'] = provider._generate_mbbank_signature(params, 'MD5')

            # Query MB Bank
            from odoo.addons.mbbank_odoo import const
            base_url = const.SANDBOX_DOMAIN if provider.state == 'test' else const.PRODUCTION_DOMAIN
            endpoint = f"{base_url}/private/ms/pg-paygate-authen/v2/paygate/detail"

            headers = {
                'Authorization': f'Bearer {token}',
                'Content-Type': 'application/json',
                'ClientMessageId': str(uuid.uuid4())
            }

            # Log request for debugging
            _logger.info(f"Sending MB Bank status query for {self.reference}")

            response = requests.post(
                endpoint,
                json=params,
                headers=headers,
                timeout=30
            )

            response_data = response.json()
            _logger.info(f"MB Bank response: {response_data}")

            # Process response
            self._process_mbbank_response(response_data)
            return True

        except Exception as e:
            # Log error and schedule retry
            _logger.exception(f"Error querying MB Bank status for {self.reference}: {str(e)}")
            next_retry = fields.Datetime.now() + timedelta(minutes=max(5, 2 ** self.retry_count))
            self.write({
                'state': 'retry',
                'next_retry': next_retry,
                'error_message': str(e)
            })
            return False

    def _process_mbbank_response(self, response_data):
        """Process MB Bank response data"""
        self.ensure_one()
        tx = self.transaction_id
        _logger.info(f"Processing response for {tx.reference}")

        error_code = response_data.get('error_code')
        resp_code = response_data.get('resp_code')
        _logger.info(f"MB Bank error_code: {error_code}, resp_code: {resp_code}")

        # Process based on error_code and resp_code
        if error_code == '00' and resp_code == '00':  # Success
            tx._set_done()
            # Save transaction details
            tx.mb_transaction_id = response_data.get('transaction_number')
            tx.mb_ft_code = response_data.get('ft_code')
            self.sudo().unlink()
            _logger.info(f"Transaction {tx.reference} marked as DONE")
            return True

        elif error_code == '00' and resp_code in ['12', '16']:  # Still processing
            # Schedule retry with exponential backoff
            next_retry = fields.Datetime.now() + timedelta(minutes=max(5, 2 ** self.retry_count))
            self.write({
                'state': 'retry',
                'next_retry': next_retry,
                'error_message': f"Still processing, retrying in {max(5, 2 ** self.retry_count)} minutes"
            })
            _logger.info(f"Transaction {tx.reference} still processing, scheduled retry for {next_retry}")
            return False

        elif error_code == '90' or error_code == '91':  # Data/Signature Invalid
            # Create new request for next retry
            next_retry = fields.Datetime.now() + timedelta(minutes=5)
            self.write({
                'state': 'retry',
                'next_retry': next_retry,
                'error_message': f"Invalid data/signature: {response_data.get('message', 'Unknown error')}"
            })
            _logger.info(f"Invalid data/signature for {tx.reference}. Scheduled retry for {next_retry}")
            return False

        else:
            # Other error codes
            message = response_data.get('message', 'Unknown error')
            # Some error codes indicate permanent failure
            if error_code in ['01', '02'] or (error_code == '00' and resp_code in ['18', '54', '56']):
                tx._set_error(f"MB Bank: {message}")
                self.sudo().unlink()
                _logger.info(
                    f"Transaction {tx.reference} marked as permanent ERROR with code {error_code}: {message}")
                return False
            else:
                # Other error codes may be temporary, continue retry
                next_retry = fields.Datetime.now() + timedelta(minutes=max(5, 2 ** self.retry_count))
                self.write({
                    'state': 'retry',
                    'next_retry': next_retry,
                    'error_message': f"Error code {error_code}: {message}"
                })
                _logger.info(
                    f"Transaction {tx.reference} temporary error code {error_code}, scheduled retry for {next_retry}")
                return False

    @api.model
    def _cron_process_transaction_retries(self):
        """Process transactions whose next_retry time has come"""
        _logger.info("Starting MB Bank transaction retry processing cron job")

        current_time = fields.Datetime.now()
        _logger.info(f"Current server time: {current_time}")

        # Show all records in retry state for debugging
        all_retry_records = self.search([('state', '=', 'retry')])
        _logger.info(f"Total records in retry state: {len(all_retry_records)}")

        for record in all_retry_records:
            _logger.info(
                f"Record {record.id} - {record.reference}: next_retry={record.next_retry}, "
                f"retry_count={record.retry_count}, compare_result={(record.next_retry <= current_time)}")

        # Find all records due for retry
        domain = [
            ('state', '=', 'retry'),
            ('next_retry', '<=', current_time),
            ('retry_count', '<', 5)
        ]

        record_ids = self.search(domain).ids
        _logger.info(f"Found {len(record_ids)} MB Bank transactions to retry with domain {domain}")

        # Process each record individually by ID
        for record_id in record_ids:
            try:
                # Find record by ID at processing time
                record = self.browse(record_id).exists()
                # Check if record still exists
                if not record:
                    _logger.info(f"Record with ID {record_id} no longer exists, skipping")
                    continue

                _logger.info(f"Starting to process transaction {record.reference}")

                # Process record
                record.retry_transaction()
                # Commit after each record to ensure changes are saved
                self.env.cr.commit()

            except Exception as e:
                _logger.exception(f"Error processing retry for transaction ID {record_id}: {e}")
                self.env.cr.rollback()

        _logger.info("Finished MB Bank transaction retry processing cron job")
