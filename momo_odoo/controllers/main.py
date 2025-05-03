import logging
import pprint
import hmac
import hashlib
import json
import uuid

from odoo import http, _
from odoo.exceptions import ValidationError
from odoo.http import request
from werkzeug.exceptions import Forbidden

_logger = logging.getLogger(__name__)


class MoMoController(http.Controller):
    _return_url = "/payment/momo/return"
    _ipn_url = "/payment/momo/ipn"

    @http.route(_return_url, type="http", methods=["GET", "POST"], auth="public", csrf=False, save_session=False)
    def momo_return_from_checkout(self, **data):
        """Xử lý redirect từ MoMo sau khi thanh toán.
        Phương thức này chỉ đơn giản chuyển hướng người dùng về trang trạng thái thanh toán.
        Việc xử lý dữ liệu thanh toán sẽ được thực hiện trong phương thức momo_webhook.
        """
        _logger.info("Handling redirection from MoMo - redirecting to payment status page")
        # Tìm giao dịch từ dữ liệu trong URL
        tx_sudo = None
        if 'orderId' in data:
            tx_sudo = request.env["payment.transaction"].sudo().search([
                ('reference', '=', data.get('orderId')),
                ('provider_code', '=', 'momo')
            ], limit=1)

        # Kiểm tra xem transaction hoàn thành chưa
        if tx_sudo and tx_sudo.state == 'done':
            # Transaction hoàn thành, chuyển hướng đến trang xác nhận đơn hàng
            return request.redirect("/shop/confirmation")
        return request.redirect("/payment/status")

    @http.route(_ipn_url, type="http", auth="public", methods=["POST"], csrf=False, save_session=False)
    def momo_webhook(self, **data):
        """Xử lý thông báo IPN từ MoMo."""
        _logger.info("============= MoMo IPN CALLED =============")
        _logger.info("IPN request headers: %s", pprint.pformat(dict(request.httprequest.headers)))

        try:
            # Parse JSON data nếu có
            notification_data = {}
            if not data and request.httprequest.data:
                notification_data = json.loads(request.httprequest.data.decode('utf-8'))
                _logger.info("IPN data from body: %s", pprint.pformat(notification_data))
            else:
                notification_data = data
                _logger.info("IPN data from params: %s", pprint.pformat(notification_data))

            # Tìm giao dịch
            if 'orderId' in notification_data:
                try:
                    reference = notification_data.get('orderId')

                    # Tìm transaction trong model pending
                    pending_tx = request.env['momo.transaction.pending'].sudo().search([
                        ('reference', '=', reference)
                    ], limit=1)

                    if pending_tx:
                        _logger.info("Processing IPN via pending model: %s", pending_tx.reference)
                        pending_tx.process_ipn_notification(notification_data)
                    else:
                        _logger.warning("Transaction not found or already processed for orderId: %s", reference)

                    # Luôn trả về 204 OK
                    return request.make_response('', status=204)

                except Exception as e:
                    _logger.exception("Error processing transaction: %s", str(e))
                    return request.make_response('', status=204)
            else:
                _logger.warning("Missing orderId in IPN")
                return request.make_response('', status=204)

        except Exception as e:
            _logger.exception("Error processing MoMo webhook: %s", str(e))
            return request.make_response('', status=204)