import hashlib
import hmac
import json
import logging
import re
import secrets
import smtplib
import uuid
from datetime import timedelta
from decimal import Decimal, InvalidOperation

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
from django.http import HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.csrf import csrf_exempt

from .models import (
    Category,
    Customer,
    EmailVerification,
    Order,
    OrderItem,
    Payment,
    PhoneOTPRequest,
    Product,
    ProductVariant,
    ProductVisit,
    ShippingAddress,
)
from .utils import cartData, release_reserved_inventory
from .postal import PostalLookupError, validate_delivery_location
from .sms import SMSProviderError, send_phone_otp, verify_phone_otp


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
    return bool(
        settings.EMAIL_HOST
        and settings.EMAIL_HOST_USER
        and settings.EMAIL_HOST_PASSWORD
        and settings.DEFAULT_FROM_EMAIL
    )


def _send_email_verification(request, email, user=None):
    if not _email_is_configured():
        return False, 'Email verification is not configured. Set up the store SMTP settings before placing orders.'

    now = timezone.now()
    if EmailVerification.objects.filter(
        email__iexact=email,
        created_at__gte=now - timedelta(minutes=1),
    ).exists():
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


def _complete_email_verification(request, verification):
    verification.verified_at = timezone.now()
    verification.otp_hash = ''
    verification.save(update_fields=['verified_at', 'otp_hash'])
    if verification.user_id:
        customer = Customer.objects.filter(
            user_id=verification.user_id,
            email__iexact=verification.email,
        ).first()
        if customer:
            customer.email_verified = True
            customer.save(update_fields=['email_verified'])
        messages.success(request, 'Your email is verified. You can now place orders.')
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


def store(request):
    data = cartData(request)
    products = Product.objects.select_related('category').prefetch_related('variants').all()
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

    context = {
        'products': products.distinct(),
        'cartItems': data['cartItems'],
        'search_term': search_query,
        'categories': Category.objects.filter(active=True),
        'selected_category': category_slug,
        'variant_filter': variant_filter,
        'min_price': minimum,
        'max_price': maximum,
        'selected_sort': sort,
    }
    return render(request, 'store/store.html', context)


def product_detail(request, product_id):
    data = cartData(request)
    product = get_object_or_404(
        Product.objects.select_related('category').prefetch_related('variants'),
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
        'visitor_key': visitor_key,
    })


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

def _checkout_context(request, error=''):
    data = cartData(request)
    if request.user.is_authenticated:
        customer = get_or_create_customer_for_user(request.user)
        email = customer.email or request.user.email
        email_verified = bool(customer.email_verified and email)
    else:
        email = (
            request.POST.get('email', '').strip()
            or request.session.get('verified_checkout_email', '')
            or request.session.get('pending_checkout_email', '')
        )
        email_verified = EmailVerification.objects.filter(
            email__iexact=email,
            verified_at__gte=timezone.now() - timedelta(days=365),
        ).exists() if email else False
    return {
        'items': data['items'],
        'order': data['order'],
        'cartItems': data['cartItems'],
        'states': STATES,
        'payu_enabled': bool(settings.PAYU_KEY and settings.PAYU_SALT),
        'upi_id': settings.AVATAR_UPI_ID,
        'checkout_email': email,
        'email_verified': email_verified,
        'error': error,
    }


def request_email_verification(request):
    if request.method != 'POST':
        return HttpResponseBadRequest('POST required.')
    email = request.POST.get('email', '').strip().lower()
    try:
        validate_email(email)
    except ValidationError:
        messages.error(request, 'Enter a valid email address.')
        return redirect('checkout' if not request.user.is_authenticated else 'account')

    user = None
    if request.user.is_authenticated:
        user = request.user
        get_or_create_customer_for_user(user)
        if email != (user.email or '').strip().lower():
            messages.error(request, 'Verify the email saved in your profile. Update your profile first if it changed.')
            return redirect('account')

    sent, message = _send_email_verification(request, email, user)
    if not sent:
        messages.error(request, message)
        return redirect('checkout' if not request.user.is_authenticated else 'account')

    if not request.user.is_authenticated:
        request.session['pending_checkout_email'] = email
    messages.success(request, message)
    return redirect('checkout' if not request.user.is_authenticated else 'account')


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
        return _complete_email_verification(request, verification)
    return render(request, 'store/email_verification_confirm.html', {
        'verification': verification,
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
    if payment_method not in ('cod', 'upi_manual', 'payu'):
        raise ValueError('Choose a valid payment method.')
    if payment_method == 'payu' and not (settings.PAYU_KEY and settings.PAYU_SALT):
        raise ValueError('Online payment is not configured yet. Choose another available method.')
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
            raise ValueError('Use the email address saved in your profile, or update and verify it first.')
        email_verified = bool(
            customer.email_verified
            and customer.email
            and customer.email.casefold() == email.casefold()
        )
    else:
        customer = None
        name = request.POST.get('name', '').strip()
        email = request.POST.get('email', '').strip() or request.session.get('verified_checkout_email', '')
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

    if not request.user.is_authenticated:
        email_verified = EmailVerification.objects.filter(
            email__iexact=email,
            verified_at__gte=timezone.now() - timedelta(days=365),
        ).exists()
    if not email_verified:
        raise ValueError('Verify your email address using the link we sent before placing this order.')

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
        )
        order.order_number = f'AV-{timezone.now():%Y%m%d}-{order.id:06d}'
        order.save(update_fields=['order_number'])

        total = 0
        for line in lines:
            cart_product = _line_value(line, 'product')
            product_id = cart_product.id
            product = Product.objects.select_for_update().get(pk=product_id)
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
            OrderItem.objects.create(
                order=order,
                product=product,
                variant=variant,
                quantity=quantity,
                unit_price=unit_price,
                unit_cost=product.cost_price,
                variant_name=variant.display_value if variant else '',
                stock_source=stock_source,
            )
            total += unit_price * quantity

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

        if not request.user.is_authenticated:
            request.session.pop('pending_checkout_email', None)
            request.session.pop('verified_checkout_email', None)

        payment = Payment.objects.create(
            order=order,
            method=payment_method,
            amount=total,
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


def order_complete(request, token):
    order = get_object_or_404(
        Order.objects.select_related('customer').prefetch_related('orderitem_set__product', 'orderitem_set__variant', 'shippingaddress_set'),
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
        with transaction.atomic():
            order = Order.objects.select_for_update().get(pk=order.pk)
            if action == 'cancel' and order.status in ('Pending Payment', 'Placed', 'Packed'):
                order.status = 'Cancelled'
                order.cancellation_reason = reason
                release_reserved_inventory(order)
                order.save(update_fields=['status', 'cancellation_reason'])
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
        'cartItems': cartData(request)['cartItems'],
        'upi_id': settings.AVATAR_UPI_ID,
        'upi_uri': (
            f'upi://pay?pa={settings.AVATAR_UPI_ID}&pn=Avatar%20Store'
            f'&am={payment.amount:.2f}&cu=INR&tn={order.order_number}'
        ) if payment and payment.method == 'upi_manual' else '',
    })


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
            if order.status not in ('Pending Payment', 'Placed', 'Packed'):
                messages.error(request, 'This order can no longer be cancelled online.')
            else:
                order.status = 'Cancelled'
                order.cancellation_reason = reason[:1000]
                release_reserved_inventory(order)
                order.save(update_fields=['status', 'cancellation_reason'])
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


def login_view(request):
    if request.user.is_authenticated:
        return redirect('account')

    next_url = request.POST.get('next') or request.GET.get('next', '')
    if request.method == 'POST':
        form = AuthenticationForm(request, data=request.POST)
        if form.is_valid():
            login(request, form.get_user())
            if url_has_allowed_host_and_scheme(next_url, {request.get_host()}):
                return redirect(next_url)
            return redirect('account')
    else:
        form = AuthenticationForm()
    return render(request, 'store/login.html', {'form': form, 'next': next_url})


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


def register_view(request):
    if request.user.is_authenticated:
        return redirect('account')

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
                user = form.save()
                user.first_name = name
                user.email = email
                user.save(update_fields=['first_name', 'email'])
                customer = get_or_create_customer_for_user(user)
                customer.phone = phone_e164
                customer.phone_e164 = phone_e164
                customer.save(update_fields=['phone', 'phone_e164'])
                login(request, user)
                sent, message = _send_email_verification(request, email.lower(), user)
                if sent:
                    messages.success(request, 'Account created. ' + message)
                else:
                    messages.error(request, 'Account created, but ' + message)
                return redirect('account')
    else:
        form = UserCreationForm()
    return render(request, 'store/register.html', {'form': form})


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

    product = get_object_or_404(Product, pk=product_id)
    variant = None
    if variant_id:
        variant = get_object_or_404(ProductVariant, pk=variant_id, product=product, active=True)
    elif product.variants.filter(active=True).exists():
        return JsonResponse({'error': 'Choose a product option first.'}, status=400)

    customer = get_or_create_customer_for_user(request.user)
    order, _ = Order.objects.get_or_create(customer=customer, complete=False)
    item, created = OrderItem.objects.get_or_create(order=order, product=product, variant=variant)
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
