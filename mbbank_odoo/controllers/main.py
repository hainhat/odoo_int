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


class MBBankController(http.Controller):
    _return_url = "/payment/mbbank/return"
    _cancel_url = "/payment/mbbank/cancel"
    _ipn_url = "/payment/mbbank/ipn"

    @http.route(_return_url, type="http", methods=["GET", "POST"], auth="public", csrf=False, save_session=False)
    def mbbank_redirect(self, **data):
        """Handle redirection from MB Bank after payment.
        This method simply redirects the user to the payment status page.
        The actual payment data processing is done in the ipn webhook.
        """
        _logger.info("Handling redirection from MB Bank - redirecting to payment status page")
        # Find transaction from data in URL
        tx_sudo = None
        if 'pg_order_reference' in data:
            tx_sudo = request.env["payment.transaction"].sudo().search([
                ('reference', '=', data.get('pg_order_reference')),
                ('provider_code', '=', 'mbbank')
            ], limit=1)

        # Check if transaction is completed
        if tx_sudo and tx_sudo.state == 'done':
            # Transaction complete, redirect to order confirmation
            return request.redirect("/shop/confirmation")
        return request.redirect("/payment/status")

    @http.route(_cancel_url, type="http", methods=["GET", "POST"], auth="public", csrf=False, save_session=False)
    def mbbank_cancel(self, **data):
        """Handle cancellation from MB Bank."""
        _logger.info("Handling cancellation from MB Bank")

        # Find the transaction
        if 'pg_order_reference' in data:
            tx_sudo = request.env["payment.transaction"].sudo().search([
                ('reference', '=', data.get('pg_order_reference')),
                ('provider_code', '=', 'mbbank')
            ], limit=1)

            if tx_sudo:
                tx_sudo._set_canceled()

        # Redirect to payment status page
        return request.redirect("/payment/status")

    @http.route(_ipn_url, type="http", auth="public", methods=["POST", "GET"], csrf=False, save_session=False)
    def mbbank_ipn(self, **data):
        """Handle IPN notification from MB Bank."""
        _logger.info("============= MB Bank IPN CALLED =============")
        _logger.info("IPN request headers: %s", pprint.pformat(dict(request.httprequest.headers)))

        try:
            # Parse JSON data if any
            notification_data = {}
            if not data and request.httprequest.data:
                notification_data = json.loads(request.httprequest.data.decode('utf-8'))
                _logger.info("IPN data from body: %s", pprint.pformat(notification_data))
            else:
                notification_data = data
                _logger.info("IPN data from params: %s", pprint.pformat(notification_data))

            # IPN sử dụng SHA256 nên đảm bảo mac_type đúng
            if 'mac_type' not in notification_data:
                notification_data['mac_type'] = 'SHA256'

            # Find transaction
            if 'pg_order_reference' in notification_data:
                try:
                    reference = notification_data.get('pg_order_reference')

                    # Find transaction in pending model
                    pending_tx = request.env['mbbank.transaction.processing'].sudo().search([
                        ('reference', '=', reference)
                    ], limit=1)

                    if pending_tx:
                        _logger.info("Processing IPN via pending model: %s", pending_tx.reference)
                        pending_tx.process_ipn_notification(notification_data)
                    else:
                        _logger.warning("Transaction not found or already processed for orderId: %s", reference)

                    # Always return 204 OK
                    return request.make_response(json.dumps({
                        'status': 'SUCCESS',
                        'message': 'Payment notification received and processed.'
                    }), headers={'Content-Type': 'application/json'}, status=200)

                except Exception as e:
                    _logger.exception("Error processing transaction: %s", str(e))
                    return json.dumps({
                        'status': 'FAILED',
                        'error_code': '500',
                        'message': f"Payment notification failed: {str(e)}"
                    })
            else:
                _logger.warning("Missing pg_order_reference in IPN")
                return json.dumps({
                    'status': 'FAILED',
                    'error_code': '01',
                    'message': 'Payment notification failed: ERR_INVALID_ORDER'
                })

        except Exception as e:
            _logger.exception("Error processing MB Bank webhook: %s", str(e))
            return json.dumps({
                'status': 'FAILED',
                'error_code': '500',
                'message': f"INTERNAL SERVER ERROR: {str(e)}"
            })
