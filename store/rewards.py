from decimal import Decimal, ROUND_FLOOR

from django.conf import settings
from django.db import transaction
from django.db.models import Sum

from .models import Customer, Order, Payment, SuperCoinEntry


COIN_REDEMPTION_STEP = 100
COIN_VALUE_STEP = Decimal('10.00')


def coin_balance(customer, *, lock=False):
    if lock:
        Customer.objects.select_for_update().get(pk=customer.pk)
    balance = SuperCoinEntry.objects.filter(customer=customer).aggregate(total=Sum('delta'))['total']
    return max(int(balance or 0), 0)


def redeemed_discount(coins):
    return (Decimal(coins) / COIN_REDEMPTION_STEP * COIN_VALUE_STEP).quantize(Decimal('0.01'))


def record_redemption(customer, order, coins):
    if not coins:
        return None
    return SuperCoinEntry.objects.create(
        customer=customer,
        order=order,
        delta=-coins,
        entry_type='redeemed',
        event_key=f'order:{order.pk}:coins:redeemed',
        note=f'Redeemed {coins} coins at checkout',
    )


def restore_redeemed_coins(order, reason):
    with transaction.atomic():
        locked_order = Order.objects.select_for_update().select_related('customer').get(pk=order.pk)
        if not locked_order.customer_id or not locked_order.coins_redeemed:
            return False
        event_key = f'order:{locked_order.pk}:coins:restored'
        if SuperCoinEntry.objects.filter(event_key=event_key).exists():
            return False
        Customer.objects.select_for_update().get(pk=locked_order.customer_id)
        SuperCoinEntry.objects.create(
            customer_id=locked_order.customer_id,
            order=locked_order,
            delta=locked_order.coins_redeemed,
            entry_type='restored',
            event_key=event_key,
            note=f'Redeemed coins restored: {reason[:160]}',
        )
        return True


def grant_order_rewards(order):
    """Award points once, only after delivery and confirmed payment."""
    with transaction.atomic():
        locked_order = Order.objects.select_for_update().select_related('customer').get(pk=order.pk)
        if (
            locked_order.status != 'Delivered'
            or locked_order.payment_status != 'paid'
            or not locked_order.customer_id
            or locked_order.status in ('Cancelled', 'Returned')
        ):
            return 0
        customer = Customer.objects.select_for_update().get(pk=locked_order.customer_id)
        payment = Payment.objects.filter(order=locked_order, status='paid').first()
        if not payment:
            return 0
        earned = max(int(Decimal(str(payment.amount)).quantize(Decimal('1'), rounding=ROUND_FLOOR)), 0)
        if earned:
            entry, created = SuperCoinEntry.objects.get_or_create(
                event_key=f'order:{locked_order.pk}:coins:earned',
                defaults={
                    'customer': customer,
                    'order': locked_order,
                    'delta': earned,
                    'entry_type': 'earned',
                    'note': f'{earned} coins for successful order {locked_order.order_number}',
                },
            )
            if created:
                bonus = int(getattr(settings, 'REFERRAL_REWARD_COINS', 0) or 0)
                has_earlier_completed_order = Order.objects.filter(
                    customer=customer,
                    status='Delivered',
                    payment_status='paid',
                ).exclude(pk=locked_order.pk).exists()
                if (
                    bonus > 0
                    and customer.referred_by_id
                    and not has_earlier_completed_order
                    and not SuperCoinEntry.objects.filter(
                        customer_id=customer.referred_by_id,
                        order__customer=customer,
                        entry_type='referral',
                    ).exists()
                ):
                    referrer = Customer.objects.select_for_update().get(pk=customer.referred_by_id)
                    SuperCoinEntry.objects.get_or_create(
                        event_key=f'order:{locked_order.pk}:referral:coins',
                        defaults={
                            'customer': referrer,
                            'order': locked_order,
                            'delta': bonus,
                            'entry_type': 'referral',
                            'note': f'Referral reward for {customer.name or "a referred customer"}',
                        },
                    )
            return earned if created else 0
        return 0


def reverse_order_rewards(order):
    """Reverse order/referral earnings and restore redeemed coins after full refund."""
    with transaction.atomic():
        locked_order = Order.objects.select_for_update().select_related('customer').get(pk=order.pk)
        for event_key in (
            f'order:{locked_order.pk}:coins:earned',
            f'order:{locked_order.pk}:referral:coins',
        ):
            original = SuperCoinEntry.objects.filter(event_key=event_key).first()
            if not original or original.delta <= 0:
                continue
            reversal_key = f'{event_key}:reversed'
            if SuperCoinEntry.objects.filter(event_key=reversal_key).exists():
                continue
            Customer.objects.select_for_update().get(pk=original.customer_id)
            SuperCoinEntry.objects.create(
                customer_id=original.customer_id,
                order=locked_order,
                delta=-original.delta,
                entry_type='reversed',
                event_key=reversal_key,
                note=f'Reward reversed after refund for order {locked_order.order_number}',
            )
    restore_redeemed_coins(order, 'order fully refunded')
