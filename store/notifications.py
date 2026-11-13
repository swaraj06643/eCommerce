import logging
from functools import partial

from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.utils import timezone

from .models import OrderEmailNotification


logger = logging.getLogger(__name__)


def queue_order_update(order, event_key, status, detail, order_url):
    recipient = (order.customer.email or '').strip() if order.customer_id else ''
    if not recipient:
        return None

    subject = f'Avatar Store order {order.order_number or order.pk}: {status}'
    body = '\n'.join([
        f'Hello {order.customer.name or "Customer"},',
        '',
        f'Your order {order.order_number or order.pk} has an update: {status}.',
        detail,
        '',
        f'View your order: {order_url}',
        '',
        'Avatar Store',
    ])
    notification, created = OrderEmailNotification.objects.get_or_create(
        event_key=event_key,
        defaults={
            'order': order,
            'recipient': recipient,
            'subject': subject,
            'body': body,
        },
    )
    if created or notification.status in ('pending', 'failed'):
        transaction.on_commit(partial(dispatch_order_email, notification.pk))
    return notification


def dispatch_order_email(notification_id):
    with transaction.atomic():
        notification = OrderEmailNotification.objects.select_for_update().get(pk=notification_id)
        if notification.status in ('sent', 'sending'):
            return notification.status == 'sent'
        notification.status = 'sending'
        notification.attempts += 1
        notification.last_error = ''
        notification.save(update_fields=['status', 'attempts', 'last_error'])

    try:
        sent_count = send_mail(
            notification.subject,
            notification.body,
            settings.DEFAULT_FROM_EMAIL,
            [notification.recipient],
            fail_silently=False,
        )
        if sent_count != 1:
            raise RuntimeError('The configured email backend did not accept the message.')
    except Exception as error:
        OrderEmailNotification.objects.filter(pk=notification.pk).update(
            status='failed',
            last_error=str(error)[:500],
        )
        logger.warning(
            'Order email delivery failed for event %s (%s).',
            notification.event_key,
            type(error).__name__,
        )
        return False

    OrderEmailNotification.objects.filter(pk=notification.pk).update(
        status='sent',
        sent_at=timezone.now(),
        last_error='',
    )
    return True
