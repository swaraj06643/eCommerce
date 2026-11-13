from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from store.models import OrderEmailNotification
from store.notifications import dispatch_order_email


class Command(BaseCommand):
    help = 'Retry pending or failed order emails and recover stale delivery attempts.'

    def handle(self, *args, **options):
        stale_before = timezone.now() - timedelta(minutes=15)
        OrderEmailNotification.objects.filter(
            status='sending',
            created_at__lt=stale_before,
        ).update(status='pending')

        notifications = OrderEmailNotification.objects.filter(
            status__in=('pending', 'failed'),
        ).order_by('created_at')
        attempted = delivered = 0
        for notification in notifications.iterator():
            attempted += 1
            if dispatch_order_email(notification.pk):
                delivered += 1

        self.stdout.write(self.style.SUCCESS(
            f'Attempted {attempted} order emails; {delivered} sent.'
        ))
