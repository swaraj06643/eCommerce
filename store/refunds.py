import base64
import hashlib
import json
import logging
from decimal import Decimal
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from .models import Order, Payment, Product, ProductVariant, Refund, ReturnRequest
from .rewards import reverse_order_rewards


logger = logging.getLogger(__name__)


class RefundProviderError(Exception):
    pass


def _paypal_request(method, path, payload=None, request_id=''):
    if not settings.PAYPAL_CLIENT_ID or not settings.PAYPAL_CLIENT_SECRET:
        raise RefundProviderError('PayPal refund credentials are not configured.')

    credentials = base64.b64encode(
        f'{settings.PAYPAL_CLIENT_ID}:{settings.PAYPAL_CLIENT_SECRET}'.encode('utf-8')
    ).decode('ascii')
    token_request = Request(
        f'{settings.PAYPAL_API_BASE}/v1/oauth2/token',
        data=b'grant_type=client_credentials',
        headers={
            'Authorization': f'Basic {credentials}',
            'Content-Type': 'application/x-www-form-urlencoded',
        },
        method='POST',
    )
    try:
        with urlopen(token_request, timeout=15) as response:
            access_token = json.loads(response.read().decode('utf-8'))['access_token']
        data = json.dumps(payload).encode('utf-8') if payload is not None else None
        headers = {
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json',
            'Prefer': 'return=representation',
        }
        if request_id:
            headers['PayPal-Request-Id'] = request_id
        api_request = Request(
            f'{settings.PAYPAL_API_BASE}{path}',
            data=data,
            headers=headers,
            method=method,
        )
        with urlopen(api_request, timeout=15) as response:
            return json.loads(response.read().decode('utf-8'))
    except (HTTPError, URLError, OSError, ValueError, KeyError) as error:
        logger.warning('PayPal refund API failed (%s).', type(error).__name__)
        raise RefundProviderError('PayPal could not process this refund. Verify the payment in PayPal.') from error


def _payu_refund(refund):
    if not settings.PAYU_KEY or not settings.PAYU_SALT:
        raise RefundProviderError('PayU refund credentials are not configured.')
    payu_id = refund.payment.gateway_payment_id.strip()
    if not payu_id:
        raise RefundProviderError('The PayU payment reference is missing; reconcile it manually.')

    command = 'cancel_refund_transaction'
    token = refund.idempotency_key.replace('-', '')[:23]
    request_hash = hashlib.sha512(
        f'{settings.PAYU_KEY}|{command}|{payu_id}|{settings.PAYU_SALT}'.encode('utf-8')
    ).hexdigest()
    payload = urlencode({
        'key': settings.PAYU_KEY,
        'command': command,
        'var1': payu_id,
        'var2': token,
        'var3': f'{refund.amount:.2f}',
        'var9': json.dumps({
            'refundDetails': {
                'remarks': f'Return refund for order {refund.return_request.order.order_number}',
            },
        }),
        'hash': request_hash,
    }).encode('utf-8')
    request = Request(
        settings.PAYU_REFUND_API_URL,
        data=payload,
        headers={
            'Accept': 'application/json',
            'Content-Type': 'application/x-www-form-urlencoded',
        },
        method='POST',
    )
    try:
        with urlopen(request, timeout=15) as response:
            result = json.loads(response.read().decode('utf-8'))
    except (HTTPError, URLError, OSError, ValueError) as error:
        logger.warning('PayU refund API failed (%s).', type(error).__name__)
        raise RefundProviderError('PayU could not process this refund. Verify the payment in PayU.') from error

    if not isinstance(result, dict) or str(result.get('status')) != '1':
        raise RefundProviderError('PayU rejected the refund request. Review the provider response in PayU.')
    return result


def _complete_refund(refund, provider_reference):
    with transaction.atomic():
        refund = Refund.objects.select_for_update().select_related(
            'payment', 'return_request__order',
        ).get(pk=refund.pk)
        refund.status = 'succeeded'
        refund.provider_reference = provider_reference[:120]
        refund.last_error = ''
        refund.completed_at = timezone.now()
        refund.save(update_fields=[
            'status', 'provider_reference', 'last_error', 'completed_at', 'updated_at',
        ])
        payment = Payment.objects.select_for_update().get(pk=refund.payment_id)
        if refund.amount >= Decimal(str(payment.amount)):
            payment.status = 'refunded'
            payment.save(update_fields=['status', 'updated_at'])
            order = Order.objects.select_for_update().get(pk=payment.order_id)
            order.payment_status = 'refunded'
            order.status = 'Returned'
            order.save(update_fields=['payment_status', 'status'])
            reverse_order_rewards(order)
        refund.return_request.status = 'refunded'
        refund.return_request.save(update_fields=['status', 'updated_at'])
        return refund


def confirm_refund_succeeded(refund_id, provider_reference=''):
    refund = Refund.objects.get(pk=refund_id)
    reference = (provider_reference or refund.provider_reference).strip()
    if not reference:
        raise RefundProviderError('Record the verified provider refund reference before reconciliation.')
    return _complete_refund(refund, reference)


def restock_return_items(return_request_id):
    with transaction.atomic():
        return_request = ReturnRequest.objects.select_for_update().get(pk=return_request_id)
        if return_request.inventory_restocked:
            return False
        lines = return_request.order.orderitem_set.select_related('product', 'variant')
        for line in lines:
            if line.stock_source == 'variant' and line.variant_id:
                ProductVariant.objects.filter(pk=line.variant_id).update(stock=F('stock') + line.quantity)
            elif line.stock_source == 'product' and line.product_id:
                Product.objects.filter(pk=line.product_id).update(stock=F('stock') + line.quantity)
        return_request.inventory_restocked = True
        return_request.save(update_fields=['inventory_restocked', 'updated_at'])
        return True


def process_return_refund(refund_id):
    with transaction.atomic():
        refund = Refund.objects.select_for_update().select_related(
            'payment', 'return_request__order',
        ).get(pk=refund_id)
        if refund.status in ('succeeded', 'processing', 'manual_required'):
            return refund
        if refund.return_request.status not in ('received', 'refund_processing'):
            raise RefundProviderError('Confirm receipt and inspection before issuing a return refund.')
        if refund.payment.status != 'paid':
            refund.status = 'manual_required'
            refund.last_error = 'Payment is not marked paid; staff must reconcile it before refunding.'
            refund.save(update_fields=['status', 'last_error', 'updated_at'])
            return refund
        if refund.payment.method in ('upi_manual', 'cod'):
            refund.status = 'manual_required'
            refund.last_error = 'This payment method requires a verified manual refund.'
            refund.save(update_fields=['status', 'last_error', 'updated_at'])
            return refund
        refund.status = 'processing'
        refund.attempts += 1
        refund.last_error = ''
        refund.save(update_fields=['status', 'attempts', 'last_error', 'updated_at'])

    try:
        if refund.payment.method == 'paypal':
            capture_id = refund.payment.customer_reference.strip()
            if not capture_id:
                raise RefundProviderError('The PayPal capture reference is missing.')
            result = _paypal_request(
                'POST',
                f'/v2/payments/captures/{capture_id}/refund',
                {
                    'amount': {
                        'currency_code': 'INR',
                        'value': f'{refund.amount:.2f}',
                    },
                    'note_to_payer': f'Refund for order {refund.return_request.order.order_number}',
                },
                request_id=refund.idempotency_key,
            )
            provider_reference = str(result.get('id', ''))
            if not provider_reference:
                raise RefundProviderError('PayPal did not return a refund reference.')
            if result.get('status') == 'COMPLETED':
                return _complete_refund(refund, provider_reference)
            if result.get('status') not in ('PENDING', 'IN_PROGRESS'):
                raise RefundProviderError('PayPal did not confirm the refund. Verify its status in PayPal before retrying.')
            refund.provider_reference = provider_reference[:120]
            refund.status = 'processing'
            refund.save(update_fields=['provider_reference', 'status', 'updated_at'])
            return refund
        if refund.payment.method == 'payu':
            result = _payu_refund(refund)
            refund.provider_reference = str(
                result.get('request_id') or result.get('mihpayid') or ''
            )[:120]
            refund.status = 'processing'
            refund.last_error = 'Refund queued by PayU; reconcile completion in the PayU dashboard.'
            refund.save(update_fields=[
                'provider_reference', 'status', 'last_error', 'updated_at',
            ])
            return refund
        raise RefundProviderError('This payment method requires a manual refund.')
    except RefundProviderError as error:
        Refund.objects.filter(pk=refund.pk).update(
            status='failed',
            last_error=str(error)[:500],
        )
        return Refund.objects.get(pk=refund.pk)
