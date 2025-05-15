import hashlib
import hmac
from datetime import timedelta

import requests

from odoo import models, fields, api, _
import logging
import uuid

_logger = logging.getLogger(__name__)


class MoMoTransactionPending(models.Model):
    _name = 'momo.transaction.pending'
    _description = 'Pending MoMo Transactions'
    _rec_name = 'reference'
    _order = 'create_date desc'

    name = fields.Char(string='Name', compute='_compute_name')
    transaction_id = fields.Many2one('payment.transaction', string='Transaction',
                                     required=True, ondelete='cascade', index=True,
                                     domain=[('provider_code', '=', 'momo')])
    reference = fields.Char(string='Reference', related='transaction_id.reference',
                            store=True, index=True)  # Add index for better performance
    signature = fields.Char(string='Signature')
    momo_request_id = fields.Char(string='MoMo Request ID', index=True)  # Add index
    create_date = fields.Datetime(string='Created On', index=True)  # Add index
    timeout_time = fields.Datetime(string='Timeout', index=True)

    def _compute_name(self):
        for record in self:
            record.name = f"Pending: {record.reference or ''}"

    @api.model
    def create_pending_transaction(self, transaction, signature=None, request_id=None):
        """Create a minimalist pending transaction record."""
        timeout_time = fields.Datetime.now() + timedelta(minutes=5)
        values = {
            'transaction_id': transaction.id,
            'signature': signature,
            'momo_request_id': request_id or str(uuid.uuid4()),
            'timeout_time': timeout_time
        }
        return self.create(values)

    # @api.model
    # def update_pending_transaction(self, transaction, signature, request_id=None):
    #     """Update pending transaction with signature and request ID"""
    #     if self.env['ir.module.module'].sudo().search([
    #         ('name', '=', 'transaction_manager'),
    #         ('state', '=', 'installed')
    #     ]):
    #         pending_model = self.env['momo.transaction.pending'].sudo()
    #         pending_tx = pending_model.search([('transaction_id', '=', self.id)], limit=1)
    #
    #         if pending_tx:
    #             pending_tx.write({
    #                 'signature': signature,
    #                 'momo_request_id': request_id,
    #                 'state': 'pending'
    #             })
    #             _logger.info(f"Updated signature for pending transaction {self.reference}")
    #         else:
    #             pending_model.create_pending_transaction(
    #                 transaction=self,
    #                 signature=signature,
    #                 request_id=request_id
    #             )
    # pending_record = self.search([('transaction_id', '=', transaction.id)], limit=1)
    # if pending_record:
    #     values = {
    #         'state': 'pending',
    #         'signature': signature
    #     }
    #     if request_id:
    #         values['momo_request_id'] = request_id
    #
    #     pending_record.write(values)
    #     _logger.info(f"Updated pending record for MoMo transaction {transaction.reference}")
    #     return pending_record
    # else:
    #     return self.create_pending_transaction(transaction, signature, request_id)

    # @api.model
    # def get_transaction_by_reference(self, reference):
    #     """Truy vấn transaction bằng reference trực tiếp từ model phụ"""
    #     pending_tx = self.search([('reference', '=', reference)], limit=1)
    #     if pending_tx:
    #         return pending_tx
    #
    #     # Nếu không tìm thấy, kiểm tra trong retry model
    #     retry_tx = self.env['momo.transaction.retry'].search([('reference', '=', reference)], limit=1)
    #     if retry_tx:
    #         return retry_tx
    #
    #     # Chỉ tạo mới từ model chính khi cần thiết
    #     tx = self.env['payment.transaction'].search([
    #         ('reference', '=', reference),
    #         ('provider_code', '=', 'momo')
    #     ], limit=1)
    #
    #     if tx:
    #         return self.create_pending_transaction(tx)
    #     return False

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
        """Process IPN notification và xóa bản ghi sau khi hoàn thành"""
        self.ensure_one()
        transaction = self.transaction_id

        # Verify signature in notification data
        if not transaction._verify_momo_signature(notification_data):
            _logger.warning("Invalid signature in MoMo IPN notification for transaction %s",
                            transaction.reference)
            return False

        # Uncomment test lỗi
        notification_data['resultCode'] = '11'
        _logger.info("Simulate error : change result code to 11")

        # Xử lý resultCode
        result_code = notification_data.get('resultCode')
        result_code_int = int(result_code) if isinstance(result_code, str) and result_code.isdigit() else result_code
        message = notification_data.get('message', 'Unknown response')

        # Cập nhật trạng thái transaction
        if result_code_int == 0:  # Success
            transaction._set_done()
            _logger.info("Transaction %s marked as DONE", self.reference)
            # Xóa bản ghi khỏi model pending sau khi hoàn tất
            _logger.info(f"Deleting pending record for completed transaction {self.reference}")
            self.sudo().unlink()
            return True
        elif result_code_int == 9000:  # Authorized
            transaction._set_authorized()
            _logger.info("Transaction %s marked as AUTHORIZED", self.reference)
            # Xóa bản ghi khỏi model pending
            _logger.info(f"Deleting pending record for completed transaction {self.reference}")
            self.sudo().unlink()
            return True
        elif result_code_int in [1000, 7000, 7002]:  # Still processing
            transaction._set_pending()
            _logger.info("Transaction %s still PENDING", self.reference)
            return True
        elif result_code_int == 1003:  # Cancelled
            transaction._set_canceled(state_message=f"MoMo: {message}")
            _logger.info("Transaction %s marked as CANCELED", self.reference)
            # Xóa bản ghi khỏi model pending
            self.sudo().unlink()
            return False
        elif result_code_int in [10, 11, 12, 99]:  # System errors - cần retry
            _logger.info("Transaction %s needs retry because of system error: %s", self.reference, message)
            # Chuyển sang retry và xóa khỏi pending
            self.env['momo.transaction.retry'].sudo().create_retry_transaction(
                transaction=transaction,
                signature=self.signature,
                request_id=self.momo_request_id,
                error_message=message
            )
            self.sudo().unlink()
            return False
        else:  # Other errors
            transaction._set_error(f"MoMo: {message}")
            _logger.info("Transaction %s marked as ERROR with code %s: %s", self.reference, result_code, message)
            # Xóa bản ghi khỏi model pending
            self.sudo().unlink()
            return False

    @api.model
    def _cron_process_expired_pending_transactions(self):
        """
        Cron job để xử lý các giao dịch MoMo trong model pending đã quá thời gian timeout.
        Giao dịch được coi là quá hạn nếu đã vượt quá thời gian timeout được thiết lập.
        """
        _logger.info("Starting cron job to process expired pending MoMo transactions")

        # Thời điểm hiện tại
        current_time = fields.Datetime.now()

        # Tìm tất cả giao dịch đã quá thời gian timeout
        expired_transactions = self.search([('timeout_time', '<=', current_time)])

        _logger.info("Found %s expired pending MoMo transactions", len(expired_transactions))

        # Xử lý từng giao dịch
        for transaction in expired_transactions:
            try:
                # Lấy transaction gốc
                payment_tx = transaction.transaction_id

                # Đánh dấu giao dịch đã hết hạn
                payment_tx._set_canceled(state_message="MoMo: Transaction expired (timeout)")
                _logger.info("Transaction %s marked as canceled due to timeout", payment_tx.reference)

                # Xóa bản ghi pending
                transaction.sudo().unlink()

                # Commit sau mỗi giao dịch để không mất tiến trình nếu có lỗi
                self.env.cr.commit()

            except Exception as e:
                _logger.exception("Error in cron job for transaction %s: %s",
                                  transaction.reference if hasattr(transaction, 'reference') else "Unknown", str(e))
                self.env.cr.rollback()

        _logger.info("Finished processing expired pending MoMo transactions")
