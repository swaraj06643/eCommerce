import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
import smtplib
import uuid
from datetime import timedelta
from io import BytesIO
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.models import User
from django.core.mail import send_mail
from django.contrib.auth import login, logout
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from django.contrib.auth.hashers import check_password, make_password
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import F, Q, Sum
from django.http import FileResponse, HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.csrf import csrf_exempt, ensure_csrf_cookie
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from .models import (
    Category,
    Customer,
    EmailVerification,
    Order,
    OrderFulfillment,
    OrderItem,
    Payment,
    PhoneOTPRequest,
    Product,
    ProductCoupon,
    ProductImage,
    ProductVariant,
    ProductVisit,
    SellerStore,
    ShippingAddress,
)
from .utils import cartData, release_reserved_inventory
from .postal import PostalLookupError, validate_delivery_location
from .sms import SMSProviderError, send_phone_otp, verify_phone_otp
from .forms import ProductCouponForm, SellerProductForm, SellerStoreForm
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from xml.sax.saxutils import escape


logger = logging.getLogger(__name__)


def get_or_create_customer_for_user(user):
    if not user or not user.is_authenticated:
        return None

    customer, _ = Customer.objects.get_or_create(
        user=user,
        defaults={
            'name': user.get_full_name() or user.username,
            'email': user.email,
        },
    )
    return customer


def normalize_indian_phone(phone):
    digits = re.sub(r'\D', '', phone or '')
    if len(digits) == 10:
        national_number = digits
    elif len(digits) == 11 and digits.startswith('0'):
        national_number = digits[1:]
    elif len(digits) == 12 and digits.startswith('91'):
        national_number = digits[2:]
    else:
        raise ValueError('Enter a valid 10-digit Indian mobile number.')
    if not re.fullmatch(r'[6-9][0-9]{9}', national_number):
        raise ValueError('Enter a valid 10-digit Indian mobile number.')
    return '+91' + national_number


def _email_is_configured():
    if settings.EMAIL_BACKEND == 'django.core.mail.backends.console.EmailBackend':
        return True
    return bool(
        settings.EMAIL_HOST
        and settings.EMAIL_HOST_USER
        and settings.EMAIL_HOST_PASSWORD
        and settings.DEFAULT_FROM_EMAIL
    )


def _send_email_verification(request, email, user=None, next_url=''):
    if not _email_is_configured():
        return False, 'Email verification is not configured. Set up the store SMTP settings before placing orders.'

    now = timezone.now()
    recent_verification = EmailVerification.objects.filter(
        email__iexact=email,
        created_at__gte=now - timedelta(minutes=1),
    ).order_by('-created_at').first()
    if recent_verification:
        if user and recent_verification.user_id is None:
            recent_verification.user = user
            recent_verification.save(update_fields=['user'])
        return True, 'A verification email was sent recently. Check your inbox and spam folder.'

    otp = f'{secrets.randbelow(900000) + 100000:06d}'
    verification = EmailVerification.objects.create(
        email=email,
        user=user,
        expires_at=now + timedelta(hours=1),
        otp_hash=make_password(otp),
    )
    verification_url = request.build_absolute_uri(
        reverse('verify_email', args=[verification.token])
    )
    if next_url:
        verification_url += '?next=' + next_url
    try:
        sent = send_mail(
            subject='Verify your email for Avatar Store',
            message=(
                f'Use this one-time link to verify {email} for Avatar Store:\n\n'
                f'{verification_url}\n\n'
                f'Or enter this 6-digit verification code: {otp}\n\n'
                'The link and code expire in one hour. If you did not request them, ignore this email.'
            ),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[email],
            fail_silently=False,
        )
    except (OSError, smtplib.SMTPException):
        verification.delete()
        return False, 'We could not send verification email. Check the store email settings and try again.'
    if sent != 1:
        verification.delete()
        return False, 'The verification email was not accepted by the mail server. Please try again.'
    return True, 'Verification link and one-time code sent. Check your email.'


def _complete_email_verification(request, verification, next_url=''):
    verification.verified_at = timezone.now()
    verification.otp_hash = ''
    verification.save(update_fields=['verified_at', 'otp_hash'])
    if verification.user_id:
        user = verification.user
        if not user.is_active:
            user.is_active = True
            user.save(update_fields=['is_active'])
        customer, _ = Customer.objects.get_or_create(
            user=user,
            defaults={
                'name': user.get_full_name() or user.username,
                'email': verification.email,
                'email_verified': True,
            },
        )
        if (
            not customer.email_verified
            or (customer.email or '').casefold() != verification.email.casefold()
        ):
            customer.email = verification.email
            customer.email_verified = True
            customer.save(update_fields=['email', 'email_verified'])
        messages.success(request, 'Your email is verified. You can now sign in.')
        if next_url == 'seller':
            return redirect('seller_login')
        if url_has_allowed_host_and_scheme(next_url, {request.get_host()}):
            return redirect(next_url)
        return redirect('account')

    request.session['verified_checkout_email'] = verification.email
    request.session.pop('pending_checkout_email', None)
    messages.success(request, 'Your email is verified. Return to checkout to place your order.')
    return redirect('checkout')


def _session_visitor_key(request):
    if not request.session.session_key:
        request.session.create()
    return request.session.session_key


def _line_value(line, name, default=None):
    return line.get(name, default) if isinstance(line, dict) else getattr(line, name, default)


@ensure_csrf_cookie
def store(request):
    data = cartData(request)
    products = Product.objects.filter(is_active=True).filter(
        Q(category__active=True) | Q(category__isnull=True)
    ).select_related(
        'category', 'seller_store',
    ).prefetch_related('variants', 'coupons')
    search_query = request.GET.get('q', '').strip()
    category_slug = request.GET.get('category', '').strip()
    variant_filter = request.GET.get('variant', '').strip()

    if search_query:
        products = products.filter(
            Q(name__icontains=search_query) | Q(description__icontains=search_query)
        )
    if category_slug:
        products = products.filter(category__slug=category_slug, category__active=True)
    if variant_filter:
        products = products.filter(variants__active=True, variants__value__icontains=variant_filter)

    minimum = request.GET.get('min_price', '').strip()
    maximum = request.GET.get('max_price', '').strip()
    try:
        if minimum:
            minimum_value = Decimal(minimum)
            if not minimum_value.is_finite() or minimum_value < 0:
                raise InvalidOperation
            products = products.filter(price__gte=minimum_value)
        if maximum:
            maximum_value = Decimal(maximum)
            if not maximum_value.is_finite() or maximum_value < 0:
                raise InvalidOperation
            products = products.filter(price__lte=maximum_value)
    except (InvalidOperation, ValueError):
        messages.error(request, 'Enter a valid minimum or maximum price.')

    sort = request.GET.get('sort', 'popular')
    if sort == 'price_low':
        products = products.order_by('price', 'name')
    elif sort == 'price_high':
        products = products.order_by('-price', 'name')
    else:
        products = products.order_by('name')

    products = products.distinct()
    category_sections = []
    show_category_sections = not search_query and not category_slug
    if show_category_sections:
        catalog_products = list(products)
        products_by_category = {}
        for product in catalog_products:
            if product.category_id:
                products_by_category.setdefault(product.category_id, []).append(product)
        category_sections = [
            {
                'category': category,
                'name': category.name,
                'slug': category.slug,
                'products': products_by_category.get(category.pk, [])[:4],
            }
            for category in Category.objects.filter(active=True)
            if products_by_category.get(category.pk)
        ]
        uncategorized = [product for product in catalog_products if not product.category_id]
        if uncategorized:
            category_sections.append({
                'category': None,
                'name': 'More finds',
                'slug': 'more-finds',
                'products': uncategorized[:4],
            })

    context = {
        'products': products,
        'category_sections': category_sections,
        'show_category_sections': show_category_sections,
        'cartItems': data['cartItems'],
        'search_term': search_query,
        'categories': Category.objects.filter(active=True),
        'selected_category': category_slug,
        'selected_category_name': Category.objects.filter(
            slug=category_slug,
            active=True,
        ).values_list('name', flat=True).first(),
        'variant_filter': variant_filter,
        'min_price': minimum,
        'max_price': maximum,
        'selected_sort': sort,
    }
    return render(request, 'store/store.html', context)


@ensure_csrf_cookie
def product_detail(request, product_id):
    data = cartData(request)
    product = get_object_or_404(
        Product.objects.filter(is_active=True).filter(
            Q(category__active=True) | Q(category__isnull=True)
        ).select_related(
            'category', 'seller_store',
        ).prefetch_related('variants', 'gallery_images'),
        id=product_id,
    )
    visitor_key = _session_visitor_key(request)
    ProductVisit.objects.get_or_create(product=product, visitor_key=visitor_key)
    list_price = product.list_price
    has_offer = list_price is not None and product.price is not None and list_price > product.price
    discount_percent = round((list_price - product.price) / list_price * 100) if has_offer else 0
    variants = product.variants.filter(active=True)
    return render(request, 'store/product_detail.html', {
        'product': product,
        'variants': variants,
        'cartItems': data['cartItems'],
        'has_offer': has_offer,
        'discount_percent': discount_percent,
        'coupons': product.coupons.filter(active=True).order_by('code'),
        'visitor_key': visitor_key,
    })


def service_worker(request):
    response = HttpResponse(
        (settings.BASE_DIR / 'static' / 'js' / 'service-worker.js').read_text(encoding='utf-8'),
        content_type='application/javascript',
    )
    response['Cache-Control'] = 'no-cache'
    response['Service-Worker-Allowed'] = '/'
    return response


def help_center(request):
    return render(request, 'store/help.html', {'cartItems': cartData(request)['cartItems']})


def cart(request):
    data = cartData(request)
    return render(request, 'store/cart.html', {
        'items': data['items'],
        'order': data['order'],
        'cartItems': data['cartItems'],
    })


STATES = [
    'Andhra Pradesh', 'Arunachal Pradesh', 'Assam', 'Bihar', 'Chhattisgarh', 'Goa', 'Gujarat',
    'Haryana', 'Himachal Pradesh', 'Jharkhand', 'Karnataka', 'Kerala', 'Madhya Pradesh',
    'Maharashtra', 'Manipur', 'Meghalaya', 'Mizoram', 'Nagaland', 'Odisha', 'Punjab',
    'Rajasthan', 'Sikkim', 'Tamil Nadu', 'Telangana', 'Tripura', 'Uttar Pradesh',
    'Uttarakhand', 'West Bengal', 'Andaman and Nicobar Islands', 'Chandigarh',
    'Dadra and Nagar Haveli and Daman and Diu', 'Delhi', 'Jammu and Kashmir', 'Ladakh',
    'Lakshadweep', 'Puducherry',
]

FULFILLMENT_NEXT_STATUS = {
    'Placed': 'Packed',
    'Packed': 'Dispatched',
    'Dispatched': 'Shipped',
    'Shipped': 'Out for Delivery',
    'Out for Delivery': 'Delivered',
}
FULFILLMENT_PROGRESS = {
    status: index for index, status in enumerate((
        'Placed', 'Packed', 'Dispatched', 'Shipped', 'Out for Delivery', 'Delivered',
    ))
}


def _cancellation_reason(request):
    reason_code = request.POST.get('reason', '')
    reasons = dict(Order.CANCELLATION_REASON_CHOICES)
    if reason_code not in reasons:
        return '', 'Choose a cancellation reason.'
    if reason_code == 'other':
        detail = request.POST.get('reason_detail', '').strip()
        if len(detail) < 3:
            return '', 'Please briefly explain the other cancellation reason.'
        return f'Other: {detail[:900]}', ''
    return reasons[reason_code], ''


def _sync_order_status(order):
    if order.status in ('Cancelled', 'Returned', 'Return Requested'):
        return
    statuses = list(order.fulfillments.values_list('status', flat=True))
    active_statuses = [status for status in statuses if status != 'Cancelled']
    if not active_statuses:
        return
    order.status = min(active_statuses, key=lambda status: FULFILLMENT_PROGRESS.get(status, 0))
    if all(status == 'Delivered' for status in active_statuses):
        if order.delivered_at is None:
            order.delivered_at = timezone.now()
    order.save(update_fields=['status', 'delivered_at'])


def _money(value):
    return Decimal(value).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


class PayPalGatewayError(Exception):
    pass


def paypal_api_request(method, path, payload=None):
    credentials = base64.b64encode(
        f'{settings.PAYPAL_CLIENT_ID}:{settings.PAYPAL_CLIENT_SECRET}'.encode('utf-8')
    ).decode('ascii')
    token_request = Request(
        f'{settings.PAYPAL_API_BASE}/v1/oauth2/token',
        data=urlencode({'grant_type': 'client_credentials'}).encode('ascii'),
        headers={
            'Authorization': f'Basic {credentials}',
            'Content-Type': 'application/x-www-form-urlencoded',
        },
        method='POST',
    )
    try:
        with urlopen(token_request, timeout=15) as response:
            access_token = json.loads(response.read().decode('utf-8'))['access_token']
        request_body = json.dumps(payload).encode('utf-8') if payload is not None else None
        api_request = Request(
            f'{settings.PAYPAL_API_BASE}{path}',
            data=request_body,
            headers={
                'Authorization': f'Bearer {access_token}',
                'Content-Type': 'application/json',
                'Prefer': 'return=representation',
            },
            method=method,
        )
        with urlopen(api_request, timeout=15) as response:
            return json.loads(response.read().decode('utf-8'))
    except (HTTPError, URLError, OSError, ValueError, KeyError) as error:
        logger.warning('PayPal API request failed (%s).', type(error).__name__)
        raise PayPalGatewayError('PayPal could not process this payment. Please try again.') from error


def _paypal_approval_url(paypal_order):
    approval_url = next((
        link.get('href') for link in paypal_order.get('links', [])
        if link.get('rel') in ('approve', 'payer-action')
    ), '')
    host = urlsplit(approval_url).hostname
    api_host = urlsplit(settings.PAYPAL_API_BASE).hostname or ''
    expected_host = 'www.sandbox.paypal.com' if 'sandbox' in api_host else 'www.paypal.com'
    if urlsplit(approval_url).scheme != 'https' or host != expected_host:
        raise PayPalGatewayError('PayPal did not provide a valid approval page.')
    return approval_url


def _restore_order_to_cart(order):
    if not order.customer_id:
        return
    with transaction.atomic():
        cart_order, _ = Order.objects.select_for_update().get_or_create(
            customer_id=order.customer_id,
            complete=False,
        )
        for line in order.orderitem_set.all():
            cart_line = OrderItem.objects.select_for_update().filter(
                order=cart_order,
                product_id=line.product_id,
                variant_id=line.variant_id,
            ).first()
            if cart_line:
                cart_line.quantity += line.quantity
                cart_line.save(update_fields=['quantity'])
            else:
                OrderItem.objects.create(
                    order=cart_order,
                    product_id=line.product_id,
                    variant_id=line.variant_id,
                    quantity=line.quantity,
                    unit_price=line.unit_price,
                    unit_cost=line.unit_cost,
                    variant_name=line.variant_name,
                )


def _line_checkout_pricing(product, unit_price, quantity, coupon=None):
    unit_price = _money(unit_price)
    discount_per_unit = Decimal('0.00')
    if coupon and product.pk == coupon.product_id:
        discount_per_unit = _money(
            unit_price * Decimal(coupon.discount_percent) / Decimal('100')
        )
    net_unit_price = max(Decimal('0.00'), unit_price - discount_per_unit)
    line_subtotal = net_unit_price * int(quantity)
    line_discount = discount_per_unit * int(quantity)
    gst_rate = Decimal(str(product.gst_rate or 0))
    gst_amount = _money(line_subtotal * gst_rate / Decimal('100'))
    return {
        'unit_price': net_unit_price,
        'subtotal': line_subtotal,
        'coupon_discount': line_discount,
        'gst_rate': gst_rate,
        'gst_amount': gst_amount,
    }


def _checkout_price_breakdown(lines, coupon=None):
    subtotal = Decimal('0.00')
    coupon_discount = Decimal('0.00')
    product_gst = Decimal('0.00')
    display_items = []
    for line in lines:
        product = _line_value(line, 'product')
        variant = _line_value(line, 'variant')
        unit_price = _line_value(line, 'unit_price')
        if unit_price is None:
            unit_price = product.price + (variant.price_adjustment if variant else 0)
        quantity = int(_line_value(line, 'quantity', 0))
        priced = _line_checkout_pricing(product, unit_price, quantity, coupon)
        subtotal += priced['subtotal']
        coupon_discount += priced['coupon_discount']
        product_gst += priced['gst_amount']
        display_items.append({
            'product': product,
            'variant_name': _line_value(line, 'variant_name', ''),
            'quantity': quantity,
            'unit_price': float(priced['unit_price']),
            'get_total': float(priced['subtotal']),
            'coupon_discount': float(priced['coupon_discount']),
            'gst_rate': priced['gst_rate'],
            'gst_amount': float(priced['gst_amount']),
        })

    platform_fee_rate = Decimal(settings.PLATFORM_FEE_PERCENT)
    platform_fee = _money(subtotal * platform_fee_rate / Decimal('100'))
    platform_fee_gst_rate = Decimal(settings.PLATFORM_FEE_GST_RATE)
    platform_fee_gst = _money(platform_fee * platform_fee_gst_rate / Decimal('100'))
    delivery_fee = Decimal('0.00')
    total = subtotal + product_gst + platform_fee + platform_fee_gst + delivery_fee
    return {
        'items': display_items,
        'subtotal': float(subtotal),
        'coupon_discount': float(coupon_discount),
        'product_gst': float(product_gst),
        'platform_fee': float(platform_fee),
        'platform_fee_rate': platform_fee_rate,
        'platform_fee_gst': float(platform_fee_gst),
        'delivery_fee': float(delivery_fee),
        'total': float(total),
    }


def _coupon_for_cart(code, lines):
    code = (code or '').strip().upper()
    if not code:
        return None, 0.0
    coupon = ProductCoupon.objects.select_related('product', 'seller_store').filter(
        code=code,
        active=True,
        product__is_active=True,
    ).first()
    if not coupon or coupon.product.seller_store_id != coupon.seller_store_id:
        return None, 0.0
    if not any(
        getattr(_line_value(line, 'product'), 'id', None) == coupon.product_id
        for line in lines
    ):
        return None, 0.0
    return coupon, _checkout_price_breakdown(lines, coupon)['coupon_discount']


def _checkout_context(request, error=''):
    data = cartData(request)
    coupon_code = request.session.get('checkout_coupon_code', '')
    coupon, _ = _coupon_for_cart(coupon_code, data['items'])
    coupon_error = ''
    if coupon_code and not coupon:
        coupon_error = 'That coupon is unavailable for the products in your bag.'
    breakdown = _checkout_price_breakdown(data['items'], coupon)
    available_coupons = ProductCoupon.objects.filter(
        active=True,
        product_id__in=[_line_value(item, 'product').id for item in data['items']],
    ).select_related('product').order_by('product__name', 'code')
    if request.user.is_authenticated:
        customer = get_or_create_customer_for_user(request.user)
        email = customer.email or request.user.email
    else:
        email = (
            request.POST.get('email', '').strip()
            or request.session.get('verified_checkout_email', '')
            or request.session.get('pending_checkout_email', '')
        )
    return {
        'items': breakdown['items'],
        'order': data['order'],
        'cartItems': data['cartItems'],
        'states': STATES,
        'payu_enabled': bool(settings.PAYU_KEY and settings.PAYU_SALT),
        'paypal_enabled': bool(settings.PAYPAL_CLIENT_ID and settings.PAYPAL_CLIENT_SECRET),
        'upi_id': settings.AVATAR_UPI_ID,
        'checkout_email': email,
        'checkout_name': request.POST.get('name', ''),
        'checkout_phone': request.POST.get('phone', ''),
        'checkout_address': request.POST.get('address', ''),
        'checkout_city': request.POST.get('city', ''),
        'checkout_state': request.POST.get('state', ''),
        'checkout_zipcode': request.POST.get('zipcode', ''),
        'selected_payment_method': request.POST.get('payment_method', ''),
        'coupon_code': coupon.code if coupon else coupon_code,
        'coupon_discount': breakdown['coupon_discount'],
        'checkout_items_subtotal': data['order']['get_cart_total'],
        'checkout_subtotal': breakdown['subtotal'],
        'product_gst': breakdown['product_gst'],
        'platform_fee': breakdown['platform_fee'],
        'platform_fee_rate': breakdown['platform_fee_rate'],
        'platform_fee_gst': breakdown['platform_fee_gst'],
        'delivery_fee': breakdown['delivery_fee'],
        'checkout_total': breakdown['total'],
        'available_coupons': available_coupons,
        'coupon_error': coupon_error,
        'error': error,
    }


def request_email_verification(request):
    if request.method != 'POST':
        return HttpResponseBadRequest('POST required.')
    seller_portal = request.POST.get('next') == 'seller'
    login_destination = 'seller_login' if seller_portal else 'login'
    email = request.POST.get('email', '').strip().lower()
    try:
        validate_email(email)
    except ValidationError:
        messages.error(request, 'Enter a valid email address.')
        return redirect('account' if request.user.is_authenticated else login_destination)

    user = None
    if request.user.is_authenticated:
        user = request.user
        get_or_create_customer_for_user(user)
        if email != (user.email or '').strip().lower():
            messages.error(request, 'Verify the email saved in your profile. Update your profile first if it changed.')
            return redirect('account')
    else:
        user = User.objects.filter(email__iexact=email).first()

    next_url = 'seller' if seller_portal else ''
    sent, message = _send_email_verification(request, email, user, next_url)
    if not sent:
        messages.error(request, message)
        return redirect('account' if request.user.is_authenticated else login_destination)

    if not request.user.is_authenticated and not seller_portal:
        request.session['pending_checkout_email'] = email
    messages.success(request, message)
    return redirect('account' if request.user.is_authenticated else login_destination)


def verify_email(request, token):
    verification = get_object_or_404(
        EmailVerification,
        token=token,
        verified_at__isnull=True,
    )
    if verification.expires_at <= timezone.now():
        messages.error(request, 'This verification link has expired. Request a new one.')
        return redirect('checkout' if verification.user_id is None else 'account')

    if request.method == 'POST':
        next_url = request.POST.get('next', '')
        return _complete_email_verification(request, verification, next_url)
    next_url = request.GET.get('next', '')
    template = (
        'store/seller_email_verification_confirm.html'
        if next_url == 'seller'
        else 'store/email_verification_confirm.html'
    )
    return render(request, template, {
        'verification': verification,
        'next_url': next_url,
        'seller_portal': next_url == 'seller',
    })


def verify_email_otp(request):
    if request.method != 'POST':
        return HttpResponseBadRequest('POST required.')
    email = request.POST.get('email', '').strip().lower()
    otp = request.POST.get('otp', '').strip()
    if request.user.is_authenticated:
        email = request.user.email.strip().lower()
    elif email not in (
        request.session.get('pending_checkout_email', ''),
        request.session.get('verified_checkout_email', ''),
    ):
        messages.error(request, 'Request a verification email for this address first.')
        return redirect('checkout')

    verification = EmailVerification.objects.filter(
        email__iexact=email,
        verified_at__isnull=True,
        expires_at__gt=timezone.now(),
        user=request.user if request.user.is_authenticated else None,
    ).order_by('-created_at').first()
    if (
        not verification
        or verification.otp_attempts >= 5
        or not re.fullmatch(r'[0-9]{6}', otp)
        or not check_password(otp, verification.otp_hash)
    ):
        if verification and verification.otp_attempts < 5:
            verification.otp_attempts += 1
            verification.save(update_fields=['otp_attempts'])
        messages.error(request, 'That verification code is invalid or expired. Request a new email if needed.')
        return redirect('checkout' if not request.user.is_authenticated else 'account')
    if verification.user_id and verification.user_id != request.user.id:
        messages.error(request, 'Request a verification code for the email saved to your account.')
        return redirect('account')
    return _complete_email_verification(request, verification)


def check_delivery(request):
    if request.method != 'POST':
        return HttpResponseBadRequest('POST required.')
    try:
        payload = json.loads(request.body)
        pincode = str(payload.get('pincode', '')).strip()
        city = str(payload.get('city', '')).strip()
        state = str(payload.get('state', '')).strip()
        if city or state:
            locations = [validate_delivery_location(pincode, city, state)]
        else:
            from .postal import lookup_pincode

            offices = lookup_pincode(pincode)
            locations = [
                {
                    'locality': office.get('Name', ''),
                    'district': office.get('District', ''),
                    'state': office.get('State', ''),
                    'delivery': True,
                }
                for office in offices
                if isinstance(office, dict)
                and (office.get('DeliveryStatus') or '').casefold() == 'delivery'
            ]
            if not locations:
                raise PostalLookupError('This PIN code has no delivery post offices listed.')
    except (json.JSONDecodeError, AttributeError, TypeError):
        return JsonResponse({'valid': False, 'message': 'Enter your PIN code, city, and state.'}, status=400)
    except PostalLookupError as error:
        return JsonResponse({'valid': False, 'message': str(error)}, status=422)
    area_names = ', '.join(dict.fromkeys(
        f"{location['locality']}, {location['district']}, {location['state']}"
        for location in locations
        if location['locality'] and location['district'] and location['state']
    ))
    return JsonResponse({
        'valid': True,
        'message': f'Delivery PIN confirmed for postal area(s): {area_names}.',
        'locations': locations,
    })


def _create_order(request, payment_method):
    data = cartData(request)
    lines = data['items']
    if not lines:
        raise ValueError('Your bag is empty.')
    coupon_code = request.session.get('checkout_coupon_code', '').strip().upper()
    coupon, _ = _coupon_for_cart(coupon_code, lines)
    if coupon_code and not coupon:
        raise ValueError('That coupon is unavailable for the products in your bag.')
    if payment_method not in ('cod', 'upi_manual', 'payu', 'paypal'):
        raise ValueError('Choose a valid payment method.')
    if payment_method == 'payu' and not (settings.PAYU_KEY and settings.PAYU_SALT):
        raise ValueError('PayU is not configured yet. Choose another available method.')
    if payment_method == 'paypal' and not (
        settings.PAYPAL_CLIENT_ID and settings.PAYPAL_CLIENT_SECRET
    ):
        raise ValueError('PayPal is not configured yet. Choose another available method.')
    if payment_method == 'upi_manual' and not settings.AVATAR_UPI_ID:
        raise ValueError('Manual UPI payment is not configured.')

    if request.user.is_authenticated:
        customer = get_or_create_customer_for_user(request.user)
        name = request.user.get_full_name() or request.user.username
        email = request.user.email or request.POST.get('email', '').strip()
        try:
            validate_email(email)
        except ValidationError:
            raise ValueError('Add a valid email address to your profile before ordering.')
        if customer.email and customer.email.casefold() != email.casefold():
            raise ValueError('Use the email address saved in your profile, or update your profile first.')
    else:
        customer = None
        name = request.POST.get('name', '').strip()
        email = request.POST.get('email', '').strip()
        if not name or not email:
            raise ValueError('Enter your name and email address.')
        try:
            validate_email(email)
        except ValidationError:
            raise ValueError('Enter a valid email address.')

    phone = request.POST.get('phone', '').strip() or (customer.phone if customer else '') or ''
    if not re.fullmatch(r'\+?[0-9][0-9 -]{7,17}[0-9]', phone):
        raise ValueError('Enter a valid contact phone number.')
    digits = re.sub(r'\D', '', phone)
    if not 10 <= len(digits) <= 15 or len(set(digits)) == 1:
        raise ValueError('Enter a valid contact phone number.')


    address = request.POST.get('address', '').strip()
    city = request.POST.get('city', '').strip()
    state = request.POST.get('state', '').strip()
    zipcode = request.POST.get('zipcode', '').strip()
    physical_items = [
        line for line in lines
        if _line_value(line, 'digital', getattr(_line_value(line, 'product'), 'digital', False)) is False
    ]
    if physical_items and (
        len(address) < 8
        or not re.search(r'[A-Za-z]', address)
        or state not in STATES
        or len(zipcode) != 6
        or not zipcode.isdigit()
        or zipcode.startswith('0')
    ):
        raise ValueError('Enter a complete address and valid six-digit PIN code.')
    if physical_items:
        try:
            validate_delivery_location(zipcode, city, state)
        except PostalLookupError as error:
            raise ValueError(str(error))

    with transaction.atomic():
        if coupon:
            coupon = ProductCoupon.objects.select_for_update().filter(
                pk=coupon.pk,
                active=True,
            ).first()
            if not coupon:
                raise ValueError('That coupon is no longer available. Remove it and try again.')
        if customer is None:
            customer = Customer.objects.create(email=email, name=name)
        if not customer.email:
            customer.email = email
        customer.phone = phone
        customer.save(update_fields=['name', 'email', 'phone'])
        order = Order.objects.create(
            customer=customer,
            complete=True,
            status='Placed' if payment_method == 'cod' else 'Pending Payment',
            payment_method=payment_method,
            payment_status='pending',
            visitor_key=request.session.session_key or '',
            coupon_code=coupon.code if coupon else '',
        )
        order.order_number = f'AV-{timezone.now():%Y%m%d}-{order.id:06d}'
        order.save(update_fields=['order_number'])

        subtotal = Decimal('0.00')
        product_gst = Decimal('0.00')
        total_coupon_discount = Decimal('0.00')
        seller_store_ids = set()
        for line in lines:
            cart_product = _line_value(line, 'product')
            product_id = cart_product.id
            product = Product.objects.select_for_update().get(pk=product_id)
            if not product.is_active or (
                product.category_id and not product.category.active
            ):
                raise ValueError(f'{product.name} is no longer available.')
            if product.seller_store_id:
                seller_store_ids.add(product.seller_store_id)
            cart_variant = _line_value(line, 'variant')
            variant_id = cart_variant.id if cart_variant else None
            variant = None
            if variant_id:
                variant = ProductVariant.objects.select_for_update().get(
                    pk=variant_id, product=product, active=True
                )
            elif product.variants.filter(active=True).exists():
                raise ValueError(f'Choose an option for {product.name}.')

            quantity = int(_line_value(line, 'quantity'))
            stock_source = 'untracked'
            inventory = variant.stock if variant and variant.stock is not None else product.stock
            if variant and variant.stock is not None:
                stock_source = 'variant'
            elif product.stock is not None:
                stock_source = 'product'
            if inventory is not None:
                if quantity > inventory:
                    raise ValueError(f'Only {inventory} units of {product.name} are available.')
                if stock_source == 'variant':
                    ProductVariant.objects.filter(pk=variant.pk).update(stock=F('stock') - quantity)
                else:
                    Product.objects.filter(pk=product.pk).update(stock=F('stock') - quantity)

            unit_price = product.price + (variant.price_adjustment if variant else 0)
            pricing = _line_checkout_pricing(product, unit_price, quantity, coupon)
            item_coupon_discount = float(pricing['coupon_discount'])
            OrderItem.objects.create(
                order=order,
                product=product,
                variant=variant,
                quantity=quantity,
                unit_price=float(pricing['unit_price']),
                unit_cost=product.cost_price,
                variant_name=variant.display_value if variant else '',
                stock_source=stock_source,
                coupon_code=coupon.code if item_coupon_discount else '',
                coupon_discount=item_coupon_discount,
                gst_rate=pricing['gst_rate'],
                gst_amount=float(pricing['gst_amount']),
            )
            subtotal += pricing['subtotal']
            product_gst += pricing['gst_amount']
            total_coupon_discount += pricing['coupon_discount']

        for seller_store_id in seller_store_ids:
            OrderFulfillment.objects.create(
                order=order,
                seller_store_id=seller_store_id,
                status='Placed',
            )

        if request.user.is_authenticated:
            open_cart = Order.objects.filter(customer=customer, complete=False).first()
            if open_cart:
                open_cart.orderitem_set.all().delete()

        if physical_items:
            ShippingAddress.objects.create(
                customer=customer,
                order=order,
                address=address,
                city=city,
                state=state,
                zipcode=zipcode,
            )

        platform_fee_rate = Decimal(settings.PLATFORM_FEE_PERCENT)
        platform_fee = _money(subtotal * platform_fee_rate / Decimal('100'))
        platform_fee_gst = _money(
            platform_fee * Decimal(settings.PLATFORM_FEE_GST_RATE) / Decimal('100')
        )
        delivery_fee = Decimal('0.00')
        total_amount = subtotal + product_gst + platform_fee + platform_fee_gst + delivery_fee
        order.coupon_discount = float(total_coupon_discount)
        order.gst_amount = float(product_gst)
        order.platform_fee = float(platform_fee)
        order.platform_fee_gst = float(platform_fee_gst)
        order.platform_fee_rate = platform_fee_rate
        order.delivery_fee = float(delivery_fee)
        order.total_amount = float(total_amount)
        order.save(update_fields=[
            'coupon_discount', 'gst_amount', 'platform_fee', 'platform_fee_gst',
            'platform_fee_rate', 'delivery_fee', 'total_amount',
        ])
        if not request.user.is_authenticated:
            request.session.pop('pending_checkout_email', None)
            request.session.pop('verified_checkout_email', None)
        request.session.pop('checkout_coupon_code', None)

        payment = Payment.objects.create(
            order=order,
            method=payment_method,
            amount=float(total_amount),
            transaction_id=uuid.uuid4().hex,
            status='pending',
        )
    return order, payment


def checkout(request):
    if request.method == 'GET':
        data = cartData(request)
        if not data['items']:
            messages.info(request, 'Your bag is empty. Add a product before checkout.')
            return redirect('store')
        return render(request, 'store/checkout.html', _checkout_context(request))

    if request.method != 'POST':
        return HttpResponseBadRequest('POST required.')
    if request.POST.get('action') == 'apply_coupon':
        code = request.POST.get('coupon_code', '').strip().upper()
        coupon, _ = _coupon_for_cart(code, cartData(request)['items'])
        if coupon:
            request.session['checkout_coupon_code'] = coupon.code
            coupon_message = f'{coupon.code} applied to {coupon.product.name}.'
        else:
            request.session.pop('checkout_coupon_code', None)
            coupon_message = 'That coupon is invalid or does not apply to items in your bag.'
        context = _checkout_context(request)
        context['coupon_message'] = coupon_message
        return render(request, 'store/checkout.html', context)

    try:
        order, payment = _create_order(request, request.POST.get('payment_method', ''))
    except ValueError as error:
        return render(request, 'store/checkout.html', _checkout_context(request, str(error)), status=400)

    response = None
    if payment.method == 'payu':
        amount = f'{payment.amount:.2f}'
        product_info = f'Avatar order {order.order_number}'
        first_name = order.customer.name or 'Customer'
        email = order.customer.email or ''
        hash_string = (
            f'{settings.PAYU_KEY}|{payment.transaction_id}|{amount}|{product_info}|'
            f'{first_name}|{email}|||||||||||{settings.PAYU_SALT}'
        )
        payu_hash = hashlib.sha512(hash_string.encode('utf-8')).hexdigest()
        callback_url = request.build_absolute_uri(reverse('payu_return'))
        response = render(request, 'store/payu_redirect.html', {
            'gateway_url': settings.PAYU_PAYMENT_URL,
            'key': settings.PAYU_KEY,
            'transaction_id': payment.transaction_id,
            'amount': amount,
            'product_info': product_info,
            'first_name': first_name,
            'email': email,
            'payu_hash': payu_hash,
            'callback_url': callback_url,
            'order': order,
        })
    elif payment.method == 'paypal':
        try:
            paypal_order = paypal_api_request('POST', '/v2/checkout/orders', {
                'intent': 'CAPTURE',
                'purchase_units': [{
                    'reference_id': payment.transaction_id,
                    'custom_id': str(order.public_token),
                    'description': f'Avatar Store order {order.order_number}',
                    'amount': {
                        'currency_code': 'INR',
                        'value': f'{payment.amount:.2f}',
                    },
                }],
                'application_context': {
                    'return_url': request.build_absolute_uri(
                        reverse('paypal_return', args=[order.public_token])
                    ),
                    'cancel_url': request.build_absolute_uri(
                        reverse('paypal_cancel', args=[order.public_token])
                    ),
                    'user_action': 'PAY_NOW',
                },
            })
            if not paypal_order.get('id'):
                raise PayPalGatewayError('PayPal did not create a payment session.')
            payment.gateway_payment_id = paypal_order['id']
            payment.save(update_fields=['gateway_payment_id', 'updated_at'])
            response = redirect(_paypal_approval_url(paypal_order))
        except PayPalGatewayError as error:
            with transaction.atomic():
                payment.status = 'failed'
                payment.save(update_fields=['status', 'updated_at'])
                order.payment_status = 'failed'
                order.status = 'Cancelled'
                release_reserved_inventory(order)
                order.save(update_fields=['payment_status', 'status'])
            _restore_order_to_cart(order)
            return render(
                request,
                'store/checkout.html',
                _checkout_context(request, str(error)),
                status=503,
            )
    elif payment.method == 'upi_manual':
        response = redirect('order_complete', token=order.public_token)
    else:
        response = redirect('order_complete', token=order.public_token)

    if not request.user.is_authenticated:
        response.delete_cookie('cart')
    return response


@csrf_exempt
def payu_return(request):
    if request.method != 'POST':
        return HttpResponseBadRequest('Invalid payment response.')

    transaction_id = request.POST.get('txnid', '')
    payment = get_object_or_404(Payment.objects.select_related('order'), transaction_id=transaction_id)
    status = request.POST.get('status', '').lower()
    posted_hash = request.POST.get('hash', '').lower()
    fields = [
        request.POST.get('udf5', ''),
        request.POST.get('udf4', ''),
        request.POST.get('udf3', ''),
        request.POST.get('udf2', ''),
        request.POST.get('udf1', ''),
        request.POST.get('email', ''),
        request.POST.get('firstname', ''),
        request.POST.get('productinfo', ''),
        request.POST.get('amount', ''),
        transaction_id,
        request.POST.get('key', ''),
    ]
    reverse_string = f'{settings.PAYU_SALT}|{status}|' + '|'.join(fields)
    additional_charges = request.POST.get('additionalCharges')
    if additional_charges:
        reverse_string = f'{additional_charges}|{reverse_string}'
    expected_hash = hashlib.sha512(reverse_string.encode('utf-8')).hexdigest()

    try:
        received_amount = Decimal(request.POST.get('amount', '0'))
    except InvalidOperation:
        return HttpResponseBadRequest('Invalid payment amount.')
    if (
        not settings.PAYU_SALT
        or not hmac.compare_digest(expected_hash, posted_hash)
        or not hmac.compare_digest(request.POST.get('key', ''), settings.PAYU_KEY)
        or received_amount != Decimal(f'{payment.amount:.2f}')
    ):
        return HttpResponseBadRequest('Payment verification failed.')

    with transaction.atomic():
        payment = Payment.objects.select_for_update().select_related('order').get(pk=payment.pk)
        order = Order.objects.select_for_update().get(pk=payment.order_id)
        gateway_status = request.POST.get('status', '').lower()
        if gateway_status == 'success' and payment.status != 'paid':
            payment.status = 'paid'
            payment.gateway_payment_id = request.POST.get('mihpayid', '')
            order.payment_status = 'paid'
            if order.status == 'Pending Payment':
                order.status = 'Placed'
            order.transaction_id = payment.transaction_id
            order.save(update_fields=['payment_status', 'status', 'transaction_id'])
        elif gateway_status != 'success' and payment.status == 'pending':
            payment.status = 'failed'
            order.payment_status = 'failed'
            order.status = 'Cancelled'
            release_reserved_inventory(order)
            order.save(update_fields=['payment_status', 'status'])
        payment.save(update_fields=['status', 'gateway_payment_id', 'updated_at'])

    return redirect('order_complete', token=payment.order.public_token)


def paypal_return(request, token):
    order = get_object_or_404(Order, public_token=token)
    payment = get_object_or_404(Payment, order=order, method='paypal')
    paypal_order_id = request.GET.get('token', '')
    if not paypal_order_id or not hmac.compare_digest(
        paypal_order_id, payment.gateway_payment_id
    ):
        return HttpResponseBadRequest('PayPal payment reference did not match this order.')
    if payment.status == 'paid':
        return redirect('order_complete', token=order.public_token)
    if payment.status != 'pending':
        return redirect('order_complete', token=order.public_token)

    try:
        result = paypal_api_request(
            'POST', f'/v2/checkout/orders/{paypal_order_id}/capture', {}
        )
    except PayPalGatewayError as error:
        messages.error(request, str(error))
        return redirect('order_complete', token=order.public_token)

    purchase_units = result.get('purchase_units', [])
    purchase_unit = purchase_units[0] if purchase_units else {}
    captures = purchase_unit.get('payments', {}).get('captures', [])
    capture = captures[0] if captures else {}
    captured_amount = capture.get('amount', {})
    if (
        result.get('status') != 'COMPLETED'
        or capture.get('status') != 'COMPLETED'
        or purchase_unit.get('reference_id') != payment.transaction_id
        or captured_amount.get('currency_code') != 'INR'
        or captured_amount.get('value') != f'{payment.amount:.2f}'
        or not capture.get('id')
    ):
        return HttpResponseBadRequest('PayPal payment verification failed.')

    with transaction.atomic():
        payment = Payment.objects.select_for_update().get(pk=payment.pk)
        order = Order.objects.select_for_update().get(pk=order.pk)
        if payment.status == 'pending':
            payment.status = 'paid'
            payment.customer_reference = capture['id']
            order.payment_status = 'paid'
            order.transaction_id = payment.transaction_id
            if order.status == 'Pending Payment':
                order.status = 'Placed'
            order.save(update_fields=['payment_status', 'transaction_id', 'status'])
            payment.save(update_fields=['status', 'customer_reference', 'updated_at'])
    return redirect('order_complete', token=order.public_token)


def paypal_cancel(request, token):
    order = get_object_or_404(Order, public_token=token)
    with transaction.atomic():
        order = Order.objects.select_for_update().get(pk=order.pk)
        payment = Payment.objects.select_for_update().filter(
            order=order,
            method='paypal',
            status='pending',
        ).first()
        if payment:
            payment.status = 'failed'
            payment.save(update_fields=['status', 'updated_at'])
            order.payment_status = 'failed'
            if order.status == 'Pending Payment':
                order.status = 'Cancelled'
                release_reserved_inventory(order)
            order.save(update_fields=['payment_status', 'status'])
    messages.info(request, 'PayPal checkout was cancelled. No payment was recorded.')
    return redirect('order_complete', token=order.public_token)


def order_complete(request, token):
    order = get_object_or_404(
        Order.objects.select_related('customer').prefetch_related(
            'orderitem_set__product', 'orderitem_set__variant',
            'shippingaddress_set', 'fulfillments__seller_store',
        ),
        public_token=token,
    )
    payment = Payment.objects.filter(order=order).first()
    if request.method == 'POST' and request.POST.get('reference'):
        if (
            order.payment_method != 'upi_manual'
            or order.status != 'Pending Payment'
            or not payment
            or payment.status != 'pending'
        ):
            return HttpResponseBadRequest('This order is not awaiting a UPI reference.')
        payment.customer_reference = request.POST['reference'].strip()[:120]
        payment.status = 'pending_review'
        payment.save(update_fields=['customer_reference', 'status', 'updated_at'])
        order.payment_status = 'pending_review'
        order.save(update_fields=['payment_status'])
        messages.success(request, 'Your UPI reference was sent for verification.')
        return redirect('order_complete', token=token)
    if request.method == 'POST' and request.POST.get('action'):
        action = request.POST.get('action')
        reason = request.POST.get('reason', '').strip()[:1000]
        if action == 'cancel':
            reason, reason_error = _cancellation_reason(request)
            if reason_error:
                messages.error(request, reason_error)
                return redirect('order_complete', token=token)
        with transaction.atomic():
            order = Order.objects.select_for_update().get(pk=order.pk)
            if action == 'cancel' and order.status in ('Pending Payment', 'Placed', 'Packed'):
                order.status = 'Cancelled'
                order.cancellation_reason = reason
                release_reserved_inventory(order)
                order.save(update_fields=['status', 'cancellation_reason'])
                OrderFulfillment.objects.filter(order=order).update(
                    status='Cancelled', updated_at=timezone.now(),
                )
                messages.success(request, 'Cancellation recorded. Paid orders require staff refund processing.')
            elif (
                action == 'return'
                and order.status == 'Delivered'
                and order.delivered_at
                and timezone.now() - order.delivered_at <= timedelta(days=7)
            ):
                order.status = 'Return Requested'
                order.return_reason = reason
                order.save(update_fields=['status', 'return_reason'])
                messages.success(request, 'Your return request was sent for review.')
            else:
                messages.error(request, 'This order is not eligible for that request.')
        return redirect('order_complete', token=token)

    return render(request, 'store/order_complete.html', {
        'order': order,
        'payment': payment,
        'items': order.orderitem_set.select_related('product', 'variant').all(),
        'fulfillments': order.fulfillments.select_related('seller_store').all(),
        'invoice_available': (
                    order.payment_status == 'paid'
                    or (order.payment_method == 'cod' and order.status not in ('Cancelled', 'Returned'))
                ),
        'cartItems': cartData(request)['cartItems'],
        'upi_id': settings.AVATAR_UPI_ID,
        'upi_uri': (
            f'upi://pay?pa={settings.AVATAR_UPI_ID}&pn=Avatar%20Store'
            f'&am={payment.amount:.2f}&cu=INR&tn={order.order_number}'
        ) if payment and payment.method == 'upi_manual' else '',
    })


def order_status(request, token):
    if request.method != 'GET':
        return HttpResponseBadRequest('GET required.')
    order = get_object_or_404(
        Order.objects.prefetch_related('fulfillments__seller_store'),
        public_token=token,
        complete=True,
    )
    return JsonResponse({
        'order_status': order.status,
        'fulfillments': [{
            'id': fulfillment.pk,
            'seller': fulfillment.seller_store.name,
            'status': fulfillment.get_status_display(),
            'updated_at': fulfillment.updated_at.isoformat(),
            'shipping_partner': fulfillment.shipping_partner,
            'tracking_number': fulfillment.tracking_number,
            'tracking_url': fulfillment.tracking_url,
        } for fulfillment in order.fulfillments.all()],
    })


def download_invoice(request, token):
    if request.method != 'GET':
        return HttpResponseBadRequest('GET required.')
    order = get_object_or_404(
        Order.objects.select_related('customer').prefetch_related(
            'orderitem_set__product__seller_store',
            'orderitem_set__variant',
            'shippingaddress_set',
        ),
        public_token=token,
        complete=True,
    )
    cod_order_is_active = order.payment_method == 'cod' and order.status not in ('Cancelled', 'Returned')
    if order.payment_status != 'paid' and not cod_order_is_active:
        return HttpResponseBadRequest('The invoice is available after payment confirmation.')

    output = BytesIO()
    document = SimpleDocTemplate(
        output,
        pagesize=A4,
        leftMargin=16 * mm,
        rightMargin=16 * mm,
        topMargin=16 * mm,
        bottomMargin=16 * mm,
        title=f'Avatar Store Invoice {order.order_number}',
        author='Avatar Store',
    )
    styles = getSampleStyleSheet()

    def pdf_text(value):
        return str(value or '').encode('cp1252', 'replace').decode('cp1252')

    story = [
        Paragraph('AVATAR STORE', styles['Title']),
        Paragraph('Order invoice', styles['Heading2']),
        Paragraph(f'Invoice / order number: {escape(order.order_number or str(order.pk))}', styles['Normal']),
        Paragraph(f'Order date: {timezone.localtime(order.date_order):%d %b %Y, %I:%M %p}', styles['Normal']),
        Paragraph(f'Customer: {escape(pdf_text(order.customer.name if order.customer else "Guest customer"))}', styles['Normal']),
        Spacer(1, 5 * mm),
    ]
    address = order.shippingaddress_set.first()
    if address:
        address_text = ', '.join(filter(None, [
            address.address, address.city, address.state, address.zipcode,
        ]))
        story.extend([
            Paragraph('Delivery address', styles['Heading3']),
            Paragraph(escape(pdf_text(address_text)), styles['Normal']),
            Spacer(1, 4 * mm),
        ])

    rows = [[
        Paragraph('<b>Product / seller</b>', styles['Normal']),
        Paragraph('<b>Qty</b>', styles['Normal']),
        Paragraph('<b>Rate</b>', styles['Normal']),
        Paragraph('<b>GST</b>', styles['Normal']),
        Paragraph('<b>Amount</b>', styles['Normal']),
    ]]
    for item in order.orderitem_set.all():
        product_name = item.product.name if item.product else 'Product'
        seller_name = item.product.seller_store.name if item.product and item.product.seller_store else 'Avatar Store'
        if item.variant_name:
            product_name = f'{product_name} ({item.variant_name})'
        rows.append([
            Paragraph(f'{escape(pdf_text(product_name))}<br/><font size="8">Seller: {escape(pdf_text(seller_name))}</font>', styles['Normal']),
            str(item.quantity or 0),
            f'INR {item.unit_price or (item.product.price if item.product else 0):.2f}',
            f'INR {item.gst_amount:.2f}',
            f'INR {item.get_total:.2f}',
        ])
    table = Table(rows, colWidths=[73 * mm, 14 * mm, 31 * mm, 25 * mm, 35 * mm], repeatRows=1)
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#29211f')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('GRID', (0, 0), (-1, -1), .35, colors.HexColor('#d9d1cc')),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('ALIGN', (1, 1), (-1, -1), 'RIGHT'),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#faf7f5')]),
        ('LEFTPADDING', (0, 0), (-1, -1), 6),
        ('RIGHTPADDING', (0, 0), (-1, -1), 6),
        ('TOPPADDING', (0, 0), (-1, -1), 7),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 7),
    ]))
    story.extend([table, Spacer(1, 5 * mm)])
    totals = [
        ['Items subtotal', f'INR {order.get_items_subtotal:.2f}'],
    ]
    if order.coupon_discount:
        totals.append(['Seller coupon discount', f'- INR {order.coupon_discount:.2f}'])
    totals.extend([
        ['GST on products', f'INR {order.gst_amount:.2f}'],
        [f'Platform fee ({order.platform_fee_rate}%)', f'INR {order.platform_fee:.2f}'],
        ['GST on platform fee', f'INR {order.platform_fee_gst:.2f}'],
        ['Delivery', 'FREE'],
        ['Total paid / payable', f'INR {order.get_total_amount:.2f}'],
    ])
    totals_table = Table(totals, colWidths=[145 * mm, 33 * mm], hAlign='RIGHT')
    totals_table.setStyle(TableStyle([
        ('ALIGN', (1, 0), (-1, -1), 'RIGHT'),
        ('LINEABOVE', (0, -1), (-1, -1), 1, colors.HexColor('#29211f')),
        ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
    ]))
    story.extend([
        totals_table,
        Spacer(1, 12 * mm),
        Paragraph('Digitally issued by Avatar Store', styles['Normal']),
        Paragraph(f'Authorized signatory: {escape(pdf_text(settings.INVOICE_AUTHORIZED_SIGNATORY))}', styles['Normal']),
        Paragraph('This document was generated electronically; no handwritten signature is required.', styles['Italic']),
    ])
    document.build(story)
    output.seek(0)
    return FileResponse(
        output,
        as_attachment=True,
        filename=f'Avatar-Invoice-{order.order_number or order.pk}.pdf',
        content_type='application/pdf',
    )


@login_required(login_url='login')
def account(request):
    customer = get_or_create_customer_for_user(request.user)
    error = ''
    if request.method == 'POST' and request.POST.get('action') == 'update_profile':
        name = request.POST.get('name', '').strip()
        email = request.POST.get('email', '').strip()
        phone = request.POST.get('phone', '').strip()
        try:
            validate_email(email)
            if not name:
                raise ValueError('Name is required.')
        except (ValidationError, ValueError):
            error = 'Enter a name and a valid email address.'
        else:
            phone_e164 = ''
            if phone:
                try:
                    phone_e164 = normalize_indian_phone(phone)
                except ValueError:
                    error = 'Enter a valid 10-digit Indian mobile number for phone OTP login.'
            if error:
                orders = Order.objects.filter(customer=customer, complete=True).order_by('-date_order')
                return render(request, 'store/account.html', {
                    'customer': customer,
                    'orders': orders,
                    'cartItems': cartData(request)['cartItems'],
                    'profile_error': error,
                })
            if phone_e164 and Customer.objects.filter(phone_e164=phone_e164).exclude(pk=customer.pk).exists():
                error = 'That phone number is already connected to another account.'
                orders = Order.objects.filter(customer=customer, complete=True).order_by('-date_order')
                return render(request, 'store/account.html', {
                    'customer': customer,
                    'orders': orders,
                    'cartItems': cartData(request)['cartItems'],
                    'profile_error': error,
                })
            if User.objects.filter(email__iexact=email).exclude(pk=request.user.pk).exists():
                error = 'That email address is already associated with another account.'
                orders = Order.objects.filter(customer=customer, complete=True).order_by('-date_order')
                return render(request, 'store/account.html', {
                    'customer': customer,
                    'orders': orders,
                    'cartItems': cartData(request)['cartItems'],
                    'profile_error': error,
                })
            request.user.first_name = name
            request.user.email = email
            request.user.save(update_fields=['first_name', 'email'])
            customer.name = name
            if (customer.email or '').casefold() != email.casefold():
                customer.email_verified = False
            customer.email = email
            customer.phone = phone_e164
            customer.phone_e164 = phone_e164
            customer.save(update_fields=['name', 'email', 'phone', 'phone_e164', 'email_verified'])
            messages.success(request, 'Your profile was updated.')
            return redirect('account')

    orders = Order.objects.filter(customer=customer, complete=True).order_by('-date_order')
    return render(request, 'store/account.html', {
        'customer': customer,
        'orders': orders,
        'cartItems': cartData(request)['cartItems'],
        'profile_error': error,
    })


@login_required(login_url='login')
def account_settings(request):
    customer = get_or_create_customer_for_user(request.user)
    return render(request, 'store/settings.html', {
        'customer': customer,
        'cartItems': cartData(request)['cartItems'],
    })


def _seller_dashboard_redirect(request):
    selected_status = request.POST.get('listing_status', 'all')
    if selected_status not in ('all', 'listed', 'limited', 'out_of_stock', 'hidden'):
        selected_status = 'all'
    url = reverse('seller_dashboard')
    if selected_status != 'all':
        url = f'{url}?listing_status={selected_status}'
    return redirect(url)


@login_required(login_url='seller_login')
def seller_dashboard(request):
    seller_store = SellerStore.objects.filter(owner=request.user).first()
    store_form = SellerStoreForm(instance=seller_store)
    product_form = SellerProductForm()
    editing_product_id = ''

    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'save_store':
            store_form = SellerStoreForm(request.POST, instance=seller_store)
            if store_form.is_valid():
                seller_store = store_form.save(commit=False)
                seller_store.owner = request.user
                seller_store.save()
                messages.success(request, 'Your store details were saved.')
                return redirect('seller_dashboard')
        elif action == 'save_product' and seller_store:
            product_id = request.POST.get('product_id', '').strip()
            editing_product_id = product_id
            product = None
            if product_id:
                if not product_id.isdigit():
                    return HttpResponseBadRequest('Invalid product listing.')
                product = get_object_or_404(
                    Product,
                    pk=product_id,
                    seller_store=seller_store,
                )
            product_form = SellerProductForm(
                request.POST,
                request.FILES,
                instance=product,
            )
            if product_form.is_valid():
                uploaded_photos = list(product_form.cleaned_data.get('photos') or [])
                product = product_form.save(commit=False)
                product.seller_store = seller_store
                if uploaded_photos and not product.image:
                    product.image = uploaded_photos.pop(0)
                product.save()
                next_sort_order = product.gallery_images.count()
                for offset, photo in enumerate(uploaded_photos):
                    ProductImage.objects.create(
                        product=product,
                        image=photo,
                        sort_order=next_sort_order + offset,
                    )
                messages.success(request, 'Your product listing and photos were saved.')
                return redirect('seller_dashboard')
        elif action == 'update_fulfillment' and seller_store:
            fulfillment_id = request.POST.get('fulfillment_id', '').strip()
            if not fulfillment_id.isdigit():
                return HttpResponseBadRequest('Invalid seller order update.')
            with transaction.atomic():
                fulfillment = get_object_or_404(
                    OrderFulfillment.objects.select_for_update(),
                    pk=fulfillment_id,
                    seller_store=seller_store,
                )
                order = Order.objects.select_for_update().get(pk=fulfillment.order_id)
                owns_order_line = OrderItem.objects.filter(
                    order=order,
                    product__seller_store=seller_store,
                ).exists()
                if not owns_order_line:
                    return HttpResponseBadRequest('This seller does not own a product in the order.')
                if order.status in ('Pending Payment', 'Cancelled', 'Returned', 'Return Requested'):
                    messages.error(request, 'This order is not ready for seller fulfillment updates.')
                    return redirect('seller_dashboard')

                requested_status = request.POST.get('status', '')
                allowed_statuses = (fulfillment.status, FULFILLMENT_NEXT_STATUS.get(fulfillment.status))
                if requested_status not in allowed_statuses:
                    messages.error(request, 'Update the shipment one step at a time.')
                    return redirect('seller_dashboard')

                tracking_url = request.POST.get('tracking_url', '').strip()
                if tracking_url:
                    parsed_tracking_url = urlsplit(tracking_url)
                    if parsed_tracking_url.scheme != 'https' or not parsed_tracking_url.hostname:
                        messages.error(request, 'Tracking links must use a valid HTTPS URL.')
                        return redirect('seller_dashboard')
                fulfillment.status = requested_status
                fulfillment.shipping_partner = request.POST.get('shipping_partner', '').strip()[:100]
                fulfillment.tracking_number = request.POST.get('tracking_number', '').strip()[:100]
                fulfillment.tracking_url = tracking_url
                if requested_status == 'Delivered' and fulfillment.delivered_at is None:
                    fulfillment.delivered_at = timezone.now()
                fulfillment.save(update_fields=[
                    'status', 'shipping_partner', 'tracking_number',
                    'tracking_url', 'delivered_at', 'updated_at',
                ])
                _sync_order_status(order)
            messages.success(request, f'Shipment status for order {order.order_number} was updated.')
            return redirect('seller_dashboard')
        elif action == 'update_stock' and seller_store:
            product_id = request.POST.get('product_id', '').strip()
            stock_value = request.POST.get('stock', '').strip()
            if not product_id.isdigit():
                return HttpResponseBadRequest('Invalid product listing.')
            if stock_value and (not re.fullmatch(r'[0-9]+', stock_value) or int(stock_value) > 2147483647):
                messages.error(request, 'Enter a whole-number stock quantity, or leave it blank for untracked stock.')
                return _seller_dashboard_redirect(request)
            product = get_object_or_404(Product, pk=product_id, seller_store=seller_store)
            with transaction.atomic():
                product = Product.objects.select_for_update().get(
                    pk=product.pk,
                    seller_store=seller_store,
                )
                product.stock = int(stock_value) if stock_value else None
                product.save(update_fields=['stock'])
            messages.success(request, f'Stock for {product.name} was updated.')
            return _seller_dashboard_redirect(request)
        elif action == 'set_visibility' and seller_store:
            product_id = request.POST.get('product_id', '').strip()
            visibility = request.POST.get('visibility', '')
            if not product_id.isdigit() or visibility not in ('listed', 'hidden'):
                return HttpResponseBadRequest('Invalid listing visibility update.')
            product = get_object_or_404(Product, pk=product_id, seller_store=seller_store)
            product.is_active = visibility == 'listed'
            product.save(update_fields=['is_active'])
            messages.success(request, f'{product.name} is now {visibility}.')
            return _seller_dashboard_redirect(request)
        elif action == 'save_coupon' and seller_store:
            product_id = request.POST.get('product_id', '').strip()
            if not product_id.isdigit():
                return HttpResponseBadRequest('Invalid product listing.')
            product = get_object_or_404(Product, pk=product_id, seller_store=seller_store)
            coupon_form = ProductCouponForm(request.POST)
            if coupon_form.is_valid():
                with transaction.atomic():
                    Product.objects.select_for_update().get(pk=product.pk)
                    if product.coupons.count() >= 3:
                        messages.error(request, 'Each product can have up to three coupon codes.')
                    else:
                        coupon = coupon_form.save(commit=False)
                        coupon.product = product
                        coupon.seller_store = seller_store
                        coupon.save()
                        messages.success(request, f'Coupon {coupon.code} was added to {product.name}.')
                        return redirect('seller_dashboard')
            else:
                for field_errors in coupon_form.errors.values():
                    for error in field_errors:
                        messages.error(request, error)
        elif action == 'delete_coupon' and seller_store:
            coupon_id = request.POST.get('coupon_id', '').strip()
            if not coupon_id.isdigit():
                return HttpResponseBadRequest('Invalid coupon.')
            coupon = get_object_or_404(ProductCoupon, pk=coupon_id, seller_store=seller_store)
            coupon.delete()
            messages.success(request, 'The coupon was removed.')
            return redirect('seller_dashboard')
        elif action == 'edit_product' and seller_store:
            editing_product_id = request.POST.get('product_id', '').strip()
            if not editing_product_id.isdigit():
                return HttpResponseBadRequest('Invalid product listing.')
            product = get_object_or_404(
                Product,
                pk=editing_product_id,
                seller_store=seller_store,
            )
            product_form = SellerProductForm(instance=product)
        else:
            messages.error(request, 'Choose a valid seller dashboard action.')

    cart_items = cartData(request)['cartItems']
    if not seller_store:
        return render(request, 'store/seller_dashboard.html', {
            'cartItems': cart_items,
            'store_form': store_form,
            'product_form': product_form,
            'editing_product_id': editing_product_id,
        })

    all_products = seller_store.products.select_related('category').prefetch_related(
        'coupons',
    )
    listing_status = request.GET.get('listing_status', 'all')
    if listing_status not in ('all', 'listed', 'limited', 'out_of_stock', 'hidden'):
        listing_status = 'all'
    product_count = all_products.count()
    status_counts = {
        'all': product_count,
        'listed': all_products.filter(is_active=True).filter(
            Q(stock__isnull=True) | Q(stock__gt=F('low_stock_threshold')),
        ).count(),
        'limited': all_products.filter(
            is_active=True,
            stock__gt=0,
            stock__lte=F('low_stock_threshold'),
        ).count(),
        'out_of_stock': all_products.filter(is_active=True, stock=0).count(),
        'hidden': all_products.filter(is_active=False).count(),
    }
    listing_tabs = [
        {'key': 'all', 'label': 'All listings', 'count': status_counts['all']},
        {'key': 'listed', 'label': 'Listed', 'count': status_counts['listed']},
        {'key': 'limited', 'label': 'Limited stock', 'count': status_counts['limited']},
        {'key': 'out_of_stock', 'label': 'Out of stock', 'count': status_counts['out_of_stock']},
        {'key': 'hidden', 'label': 'Hidden', 'count': status_counts['hidden']},
    ]
    products = all_products
    if listing_status == 'listed':
        products = products.filter(is_active=True).filter(
            Q(stock__isnull=True) | Q(stock__gt=F('low_stock_threshold')),
        )
    elif listing_status == 'limited':
        products = products.filter(
            is_active=True,
            stock__gt=0,
            stock__lte=F('low_stock_threshold'),
        )
    elif listing_status == 'out_of_stock':
        products = products.filter(is_active=True, stock=0)
    elif listing_status == 'hidden':
        products = products.filter(is_active=False)
    products = products.order_by('name')
    seller_lines = list(
        OrderItem.objects.filter(
            product__seller_store=seller_store,
            order__complete=True,
        ).select_related('order', 'product', 'variant').order_by('-order__date_order')
    )
    grouped_orders = {}
    paid_sales = 0
    awaiting_payment = 0
    for line in seller_lines:
        order = line.order
        grouped_order = grouped_orders.setdefault(order.pk, {
            'order': order,
            'items': [],
        })
        grouped_order['items'].append(line)
        line_total = line.get_total
        if order.payment_status == 'paid' and order.status not in ('Cancelled', 'Returned'):
            paid_sales += line_total
        elif (
            order.payment_status in ('pending', 'pending_review')
            and order.status not in ('Cancelled', 'Returned')
        ):
            awaiting_payment += line_total

    fulfillment_by_order = {
        fulfillment.order_id: fulfillment
        for fulfillment in OrderFulfillment.objects.filter(
            order_id__in=grouped_orders,
            seller_store=seller_store,
        )
    }
    fulfillment_labels = dict(OrderFulfillment.STATUS_CHOICES)
    for order_id, group in grouped_orders.items():
        fulfillment = fulfillment_by_order.get(order_id)
        group['fulfillment'] = fulfillment
        group['fulfillment_status_options'] = []
        if fulfillment:
            group['fulfillment_status_options'].append((
                fulfillment.status,
                fulfillment_labels[fulfillment.status],
            ))
            next_status = FULFILLMENT_NEXT_STATUS.get(fulfillment.status)
            if next_status:
                group['fulfillment_status_options'].append((
                    next_status,
                    fulfillment_labels[next_status],
                ))
            group['can_update_fulfillment'] = (
                group['order'].status not in (
                    'Pending Payment', 'Cancelled', 'Returned', 'Return Requested',
                )
            )
    open_fulfillment_count = sum(
        1 for group in grouped_orders.values()
        if group['fulfillment'] and group['fulfillment'].status not in ('Delivered', 'Cancelled')
    )

    return render(request, 'store/seller_dashboard.html', {
        'cartItems': cart_items,
        'seller_store': seller_store,
        'store_form': store_form,
        'product_form': product_form,
        'editing_product_id': editing_product_id,
        'products': products,
        'product_count': product_count,
        'active_listing_count': status_counts['listed'] + status_counts['limited'] + status_counts['out_of_stock'],
        'low_stock_count': status_counts['limited'],
        'listing_status': listing_status,
        'listing_tabs': listing_tabs,
        'paid_sales': paid_sales,
        'awaiting_payment': awaiting_payment,
        'seller_orders': list(grouped_orders.values())[:12],
                'open_fulfillment_count': open_fulfillment_count,
    })


def seller_portal(request):
    if request.user.is_authenticated:
        return redirect('seller_dashboard')
    return redirect('seller_login')


@login_required(login_url='login')
def order_action(request, order_id):
    if request.method != 'POST':
        return HttpResponseBadRequest('POST required.')
    customer = get_or_create_customer_for_user(request.user)
    with transaction.atomic():
        order = get_object_or_404(
            Order.objects.select_for_update(),
            id=order_id,
            customer=customer,
            complete=True,
        )
        action = request.POST.get('action')
        reason = request.POST.get('reason', '').strip()
        if action == 'cancel':
            reason, reason_error = _cancellation_reason(request)
            if reason_error:
                messages.error(request, reason_error)
            elif order.status not in ('Pending Payment', 'Placed', 'Packed'):
                messages.error(request, 'This order can no longer be cancelled online.')
            else:
                order.status = 'Cancelled'
                order.cancellation_reason = reason
                release_reserved_inventory(order)
                order.save(update_fields=['status', 'cancellation_reason'])
                OrderFulfillment.objects.filter(order=order).update(
                    status='Cancelled', updated_at=timezone.now(),
                )
                messages.success(request, 'Your cancellation request was recorded. Paid orders require staff refund processing.')
        elif action == 'return':
            within_return_window = (
                order.delivered_at is not None
                and timezone.now() - order.delivered_at <= timedelta(days=7)
            )
            if order.status != 'Delivered' or not within_return_window:
                messages.error(request, 'Returns can be requested within seven days of delivery.')
            else:
                order.status = 'Return Requested'
                order.return_reason = reason[:1000]
                order.save(update_fields=['status', 'return_reason'])
                messages.success(request, 'Your return request was sent for review.')
        else:
            return HttpResponseBadRequest('Unknown order action.')
    return redirect('account')


def login_view(request, seller_portal=False):
    if request.user.is_authenticated:
        return redirect('seller_dashboard' if seller_portal else 'account')

    next_url = request.POST.get('next') or request.GET.get('next', '')
    if request.method == 'POST':
        login_data = request.POST.copy()
        identifier = login_data.get('username', '').strip()
        user = (
            User.objects.filter(email__iexact=identifier).first()
            if '@' in identifier
            else User.objects.filter(username__iexact=identifier).first()
        )
        if user and '@' in identifier:
            login_data['username'] = user.username
        if (
            user
            and not user.is_active
            and user.check_password(login_data.get('password', ''))
        ):
            user.is_active = True
            user.save(update_fields=['is_active'])
        form = AuthenticationForm(request, data=login_data)
        form.fields['username'].label = 'Username or email'
        if form.is_valid():
            login(request, form.get_user())
            if url_has_allowed_host_and_scheme(next_url, {request.get_host()}):
                return redirect(next_url)
            return redirect('seller_dashboard' if seller_portal else 'account')
    else:
        form = AuthenticationForm()
        form.fields['username'].label = 'Username or email'
    return render(request, (
        'store/seller_login.html' if seller_portal else 'store/login.html'
    ), {
        'form': form,
        'next': next_url,
        'seller_portal': seller_portal,
    })


def seller_login(request):
    return login_view(request, seller_portal=True)


def phone_login_request(request):
    if request.user.is_authenticated:
        return redirect('account')
    if request.method != 'POST':
        return HttpResponseBadRequest('POST required.')
    try:
        phone = normalize_indian_phone(request.POST.get('phone', ''))
    except ValueError as error:
        messages.error(request, str(error))
        return redirect('login')

    next_url = request.POST.get('next', '')
    same_pending_phone = request.session.get('phone_otp_phone') == phone
    request.session['phone_otp_phone'] = phone
    if not same_pending_phone:
        request.session['phone_otp_sent'] = False
    request.session['phone_otp_next'] = (
        next_url if url_has_allowed_host_and_scheme(next_url, {request.get_host()}) else ''
    )

    if not settings.MSG91_AUTH_KEY or not settings.MSG91_OTP_TEMPLATE_ID:
        messages.error(request, 'Phone OTP login is not configured. Contact Avatar support.')
        return redirect('phone_login_verify')

    customers = Customer.objects.filter(phone_e164=phone).select_related('user')
    customer = customers.first() if customers.count() == 1 else None
    now = timezone.now()
    PhoneOTPRequest.objects.filter(
        phone_e164=phone,
        created_at__lt=now - timedelta(hours=1),
    ).delete()
    if customer and customer.user and customer.user.is_active:
        recent_requests = PhoneOTPRequest.objects.filter(
            phone_e164=phone,
            created_at__gte=now - timedelta(hours=1),
        )
        can_send_otp = (
            recent_requests.count() < 5
            and not recent_requests.filter(created_at__gte=now - timedelta(minutes=1)).exists()
        )
        if can_send_otp:
            try:
                send_phone_otp(phone)
            except SMSProviderError:
                logger.warning('MSG91 failed to send a phone login OTP.', exc_info=True)
            else:
                PhoneOTPRequest.objects.create(phone_e164=phone)
    request.session['phone_otp_sent'] = True
    messages.success(request, 'If this number is registered, an OTP has been sent. Enter it below if received.')
    return redirect('phone_login_verify')


def phone_login_verify(request):
    if request.user.is_authenticated:
        return redirect('account')
    phone = request.session.get('phone_otp_phone')
    if not phone:
        return redirect('login')

    if request.method == 'POST':
        otp = request.POST.get('otp', '').strip()
        if not request.session.get('phone_otp_sent'):
            messages.error(request, 'Request a new OTP before trying to sign in.')
        elif not re.fullmatch(r'[0-9]{4,8}', otp):
            messages.error(request, 'Enter the OTP from your SMS.')
        else:
            try:
                verify_phone_otp(phone, otp)
            except SMSProviderError:
                messages.error(request, 'That OTP is invalid or expired. Request a new code and try again.')
            else:
                customers = Customer.objects.filter(phone_e164=phone).select_related('user')
                customer = customers.first() if customers.count() == 1 else None
                if not customer or not customer.user or not customer.user.is_active:
                    messages.error(request, 'We could not sign you in with that number. Check your account or contact support.')
                else:
                    next_url = request.session.get('phone_otp_next', '')
                    request.session.pop('phone_otp_phone', None)
                    request.session.pop('phone_otp_sent', None)
                    request.session.pop('phone_otp_next', None)
                    login(request, customer.user)
                    return redirect(next_url or 'account')

    return render(request, 'store/phone_login_verify.html', {
        'phone': phone,
        'otp_sent': request.session.get('phone_otp_sent', False),
        'next': request.session.get('phone_otp_next', ''),
    })


class StorePasswordResetView(auth_views.PasswordResetView):
    template_name = 'store/password_reset_form.html'
    email_template_name = 'store/password_reset_email.html'
    subject_template_name = 'store/password_reset_subject.txt'
    success_url = reverse_lazy('password_reset_done')

    def form_valid(self, form):
        if not _email_is_configured():
            form.add_error(None, 'Password reset email is not configured. Contact Avatar support.')
            return self.form_invalid(form)
        try:
            return super().form_valid(form)
        except (OSError, smtplib.SMTPException):
            form.add_error(None, 'We could not send the reset email. Please try again later.')
            return self.form_invalid(form)


def logout_view(request):
    logout(request)
    return redirect('store')


def seller_logout(request):
    logout(request)
    return redirect('seller_login')


def register_view(request, seller_portal=False):
    if request.user.is_authenticated:
        return redirect('seller_dashboard' if seller_portal else 'account')

    if request.method == 'POST':
        form = UserCreationForm(request.POST)
        name = request.POST.get('first_name', '').strip()
        email = request.POST.get('email', '').strip()
        phone = request.POST.get('phone', '').strip()
        if form.is_valid():
            try:
                validate_email(email)
                if not name:
                    raise ValueError('Name is required.')
                phone_e164 = normalize_indian_phone(phone) if phone else ''
                if User.objects.filter(email__iexact=email).exists():
                    raise ValueError('An account already uses that email address.')
                if phone_e164 and Customer.objects.filter(phone_e164=phone_e164).exists():
                    raise ValueError('That phone number is already connected to an account.')
            except (ValidationError, ValueError) as error:
                form.add_error(None, str(error))
            else:
                user = form.save(commit=False)
                user.first_name = name
                user.email = email
                user.is_active = True
                user.save()
                customer = get_or_create_customer_for_user(user)
                customer.phone = phone_e164
                customer.phone_e164 = phone_e164
                customer.save(update_fields=['phone', 'phone_e164'])
                login(request, user)
                messages.success(request, 'Your account is ready.')
                return redirect('seller_dashboard' if seller_portal else 'account')
    else:
        form = UserCreationForm()
    return render(request, (
        'store/seller_register.html' if seller_portal else 'store/register.html'
    ), {'form': form, 'seller_portal': seller_portal})


def seller_register(request):
    return register_view(request, seller_portal=True)


def updateItem(request):
    if request.method != 'POST' or not request.user.is_authenticated:
        return JsonResponse({'error': 'Sign in to update this bag.'}, status=401)
    try:
        data = json.loads(request.body)
        product_id = int(data['productId'])
        variant_id = int(data['variantId']) if data.get('variantId') else None
        action = data['action']
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        return JsonResponse({'error': 'Invalid bag update.'}, status=400)
    if action not in ('add', 'remove'):
        return JsonResponse({'error': 'Invalid bag action.'}, status=400)

    with transaction.atomic():
        product = get_object_or_404(
            Product.objects.select_for_update().filter(is_active=True).filter(
                Q(category__active=True) | Q(category__isnull=True)
            ),
            pk=product_id,
        )
        variant = None
        if variant_id:
            variant = get_object_or_404(
                ProductVariant.objects.select_for_update(),
                pk=variant_id,
                product=product,
                active=True,
            )
        elif product.variants.filter(active=True).exists():
            return JsonResponse({'error': 'Choose a product option first.'}, status=400)

        customer = get_or_create_customer_for_user(request.user)
        order, _ = Order.objects.get_or_create(customer=customer, complete=False)
        item, created = OrderItem.objects.get_or_create(
            order=order,
            product=product,
            variant=variant,
        )
        if action == 'add':
            inventory = variant.stock if variant and variant.stock is not None else product.stock
            if inventory is not None and item.quantity + 1 > inventory:
                if created:
                    item.delete()
                return JsonResponse({'error': 'There is not enough stock for another unit.'}, status=409)
            item.quantity += 1
        else:
            item.quantity -= 1
        if item.quantity <= 0:
            item.delete()
        else:
            item.unit_price = product.price + (variant.price_adjustment if variant else 0)
            item.variant_name = variant.display_value if variant else ''
            item.save(update_fields=['quantity', 'unit_price', 'variant_name'])
    return JsonResponse({'success': True})


@login_required(login_url='login')
@user_passes_test(lambda user: user.is_staff)
def analytics(request):
    paid_orders = Order.objects.filter(
        complete=True,
        payment_status='paid',
    ).exclude(status__in=('Cancelled', 'Returned'))
    paid_lines = OrderItem.objects.filter(order__in=paid_orders)
    revenue = sum(order.get_cart_total for order in paid_orders)
    cost_lines = paid_lines.filter(unit_cost__isnull=False)
    known_cost = sum((line.unit_cost or 0) * line.quantity for line in cost_lines)
    known_revenue = sum((line.unit_price or 0) * line.quantity for line in cost_lines)
    visitors = ProductVisit.objects.values('visitor_key').distinct().count()
    converted_visitors = paid_orders.exclude(visitor_key='').values('visitor_key').distinct().count()
    products = Product.objects.annotate(
        units_sold=Sum(
            'orderitem__quantity',
            filter=Q(orderitem__order__payment_status='paid')
            & ~Q(orderitem__order__status__in=('Cancelled', 'Returned')),
        ),
    ).order_by('-units_sold', 'name')
    return render(request, 'store/analytics.html', {
        'cartItems': cartData(request)['cartItems'],
        'revenue': revenue,
        'profit': known_revenue - known_cost,
        'profit_products_count': cost_lines.values('product_id').distinct().count(),
        'paid_order_count': paid_orders.count(),
        'low_stock_products': Product.objects.filter(
            stock__isnull=False,
            stock__lte=F('low_stock_threshold'),
        ).count(),
        'visitors': visitors,
        'converted_visitors': converted_visitors,
        'conversion_rate': (converted_visitors / visitors * 100) if visitors else 0,
        'top_products': products[:10],
    })
