import hashlib
import hmac
from datetime import timedelta
import requests

from odoo import models, fields, api, _
import logging
import uuid

_logger = logging.getLogger(__name__)


class MBBankTransactionProcessing(models.Model):
    _name = 'mbbank.transaction.processing'
    _description = 'Processing MB Bank Transactions'
    _rec_name = 'reference'
    _order = 'create_date desc'

    name = fields.Char(string='Name', compute='_compute_name')
    transaction_id = fields.Many2one('payment.transaction', string='Transaction',
                                     required=True, ondelete='cascade', index=True,
                                     domain=[('provider_code', '=', 'mbbank')])
    reference = fields.Char(string='Reference', related='transaction_id.reference',
                            store=True, index=True)
    signature = fields.Char(string='Signature')
    mb_request_id = fields.Char(string='MB Bank Request ID', index=True)
    create_date = fields.Datetime(string='Created On', index=True)
    timeout_time = fields.Datetime(string='Timeout', index=True)

    def _compute_name(self):
        for record in self:
            record.name = f"Processing: {record.reference or ''}"

    @api.model
    def create_processing_transaction(self, transaction, signature=None, request_id=None):
        """Create a minimalist pending transaction record."""
        timeout_time = fields.Datetime.now() + timedelta(minutes=5)
        values = {
            'transaction_id': transaction.id,
            'signature': signature,
            'mb_request_id': request_id or str(uuid.uuid4()),
            'timeout_time': timeout_time
        }
        return self.create(values)

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

    def process_ipn_notification(self, notification_data):
        """Process IPN notification and delete record after completion"""
        self.ensure_one()
        transaction = self.transaction_id

        # Log để debug
        _logger.info(f"Processing IPN notification for transaction {self.reference}")

        # Verify signature in notification data - IPN sử dụng SHA256
        if not transaction._verify_mbbank_signature(notification_data):
            _logger.warning("Invalid signature in MB Bank IPN notification for transaction %s",
                            transaction.reference)
            return False

        # Process error_code (MB Bank use error_code instead of resultCode)
        error_code = notification_data.get('error_code')
        message = notification_data.get('message', 'Unknown response')

        # Log chi tiết thông tin
        _logger.info(f"IPN for {self.reference}: error_code={error_code}, message={message}")

        # Update transaction status
        if error_code == '00':  # Success
            transaction._set_done()
            _logger.info("Transaction %s marked as DONE", self.reference)
            # Save additional data
            if 'pg_transaction_number' in notification_data:
                transaction.mb_transaction_id = notification_data.get('pg_transaction_number')
                _logger.info(f"Saved transaction_number: {transaction.mb_transaction_id}")
            if 'pg_issuer_txn_reference' in notification_data:
                transaction.mb_ft_code = notification_data.get('pg_issuer_txn_reference')
                _logger.info(f"Saved FT code: {transaction.mb_ft_code}")

            # Delete pending record after completion
            _logger.info(f"Deleting processing record for completed transaction {self.reference}")
            self.sudo().unlink()
            return True

        elif error_code in ['12', '16']:  # Still processing
            transaction._set_pending()
            _logger.info("Transaction %s still PENDING", self.reference)
            return True

        elif error_code == '18':  # Cancelled by user
            transaction._set_canceled(state_message=f"MB Bank: {message}")
            _logger.info("Transaction %s marked as CANCELED", self.reference)
            # Delete pending record
            self.sudo().unlink()
            return False

        elif error_code in ['92', '93', '94', '95']:  # System errors - need retry
            _logger.info("Transaction %s needs retry because of system error: %s", self.reference, message)
            # Transfer to retry and delete from pending
            self.env['mbbank.transaction.retry'].sudo().create_retry_transaction(
                transaction=transaction,
                signature=self.signature,
                request_id=self.mb_request_id,
                error_message=message
            )
            self.sudo().unlink()
            return False

        else:  # Other errors
            transaction._set_error(f"MB Bank: {message}")
            _logger.info("Transaction %s marked as ERROR with code %s: %s", self.reference, error_code, message)
            # Delete pending record
            self.sudo().unlink()
            return False

    @api.model
    def _cron_process_expired_processing_transactions(self):
        """
        Cron job to process expired MB Bank pending transactions.
        A transaction is considered expired if it exceeds the configured timeout.
        """
        _logger.info("Starting cron job to process expired processing MB Bank transactions")

        # Current time
        current_time = fields.Datetime.now()

        # Find all transactions that have exceeded timeout
        expired_transactions = self.search([('timeout_time', '<=', current_time)])

        _logger.info("Found %s expired processing MB Bank transactions", len(expired_transactions))

        # Process each transaction
        for transaction in expired_transactions:
            try:
                # Get original transaction
                payment_tx = transaction.transaction_id

                # Mark transaction as expired
                payment_tx._set_canceled(state_message="MB Bank: Transaction expired (timeout)")
                _logger.info("Transaction %s marked as canceled due to timeout", payment_tx.reference)

                # Delete pending record
                transaction.sudo().unlink()

                # Commit after each transaction to avoid losing progress if there's an error
                self.env.cr.commit()

            except Exception as e:
                _logger.exception("Error in cron job for transaction %s: %s",
                                  transaction.reference if hasattr(transaction, 'reference') else "Unknown", str(e))
                self.env.cr.rollback()

        _logger.info("Finished processing expired processing MB Bank transactions")