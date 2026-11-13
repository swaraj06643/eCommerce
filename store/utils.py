import json
import logging
from urllib.parse import unquote

from django.db.models import F, Prefetch

from .models import Customer, Order, OrderItem, Product, ProductVariant


logger = logging.getLogger(__name__)


def cookieCart(request):
    raw_cart = request.COOKIES.get('cart', '{}')
    try:
        cart = json.loads(unquote(raw_cart))
    except json.JSONDecodeError:
        logger.warning('Ignoring malformed guest cart cookie.')
        cart = {}

    if not isinstance(cart, dict):
        logger.warning('Ignoring guest cart cookie with an invalid shape.')
        cart = {}

    product_ids = set()
    parsed_lines = []
    for key, entry in cart.items():
        try:
            product_id, separator, variant_id = str(key).partition(':')
            product_id = int(product_id)
            variant_id = int(variant_id) if separator and variant_id else None
            quantity = int(entry['quantity'])
        except (TypeError, ValueError, KeyError):
            logger.warning('Ignoring invalid guest cart line.')
            continue

        if quantity <= 0:
            continue
        product_ids.add(product_id)
        parsed_lines.append((product_id, variant_id, quantity))

    products = {
        product.id: product
        for product in Product.objects.filter(id__in=product_ids, is_active=True).prefetch_related(
            Prefetch('variants', queryset=ProductVariant.objects.filter(active=True), to_attr='active_variants')
        )
    }

    items = []
    for product_id, variant_id, quantity in parsed_lines:
        product = products.get(product_id)
        if product is None:
            continue
        variant = next(
            (item for item in product.active_variants if item.id == variant_id),
            None,
        ) if variant_id else None
        if variant_id and variant is None:
            continue

        unit_price = product.price + (variant.price_adjustment if variant else 0)
        items.append({
            'id': product.id,
            'product': product,
            'variant': variant,
            'variant_name': variant.display_value if variant else '',
            'quantity': quantity,
            'unit_price': unit_price,
            'get_total': unit_price * quantity,
            'digital': product.digital,
        })

    total_items = sum(item['quantity'] for item in items)
    total = sum(item['get_total'] for item in items)
    order = {
        'get_cart_total': total,
        'get_cart_items': total_items,
        'shipping': any(item['digital'] is False for item in items),
    }
    return {'cartItems': total_items, 'order': order, 'items': items}


def cartData(request):
    if request.user.is_authenticated:
        customer, _ = Customer.objects.get_or_create(
            user=request.user,
            defaults={
                'name': request.user.get_full_name() or request.user.username,
                'email': request.user.email,
            },
        )
        order, _ = Order.objects.get_or_create(customer=customer, complete=False)
        items = list(
            order.orderitem_set.select_related('product', 'variant').all()
        )
        for item in items:
            if item.product_id and item.product:
                item.unit_price = item.product.price + (
                    item.variant.price_adjustment if item.variant_id else 0
                )
            if not item.variant_name and item.variant_id:
                item.variant_name = item.variant.display_value
        cart_items = sum(item.quantity for item in items)
        total = sum(item.get_total for item in items)
        order_data = {
            'get_cart_total': total,
            'get_cart_items': cart_items,
            'shipping': any(
                item.product_id and item.product.digital is False for item in items
            ),
        }
        return {'cartItems': cart_items, 'order': order_data, 'items': items}

    return cookieCart(request)


def release_reserved_inventory(order):
    if order.inventory_released:
        return
    for line in order.orderitem_set.select_related('product', 'variant').all():
        if line.stock_source == 'variant' and line.variant_id:
            ProductVariant.objects.filter(pk=line.variant_id).update(stock=F('stock') + line.quantity)
        elif line.stock_source == 'product' and line.product_id:
            Product.objects.filter(pk=line.product_id).update(stock=F('stock') + line.quantity)
    order.inventory_released = True
    order.save(update_fields=['inventory_released'])
