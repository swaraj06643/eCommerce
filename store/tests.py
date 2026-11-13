import hashlib
import json
import os
import re
import shutil
from datetime import timedelta
from io import BytesIO
import tempfile
from urllib.parse import parse_qs, quote, urlsplit
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from unittest.mock import patch as mock_patch

from PIL import Image

from .models import (
    Category,
    Customer,
    EmailVerification,
    Order,
    OrderFulfillment,
    OrderItem,
    Payment,
    Product,
    ProductCoupon,
    ProductImage,
    ProductVariant,
    SellerStore,
    ShippingAddress,
)
from .postal import PostalLookupError, validate_delivery_location
from .sms import send_phone_otp, verify_phone_otp
from .views import PayPalGatewayError
from eCommerce.settings import database_config


class StorefrontTests(TestCase):
    def setUp(self):
        self.media_root = tempfile.mkdtemp()
        media_settings = override_settings(MEDIA_ROOT=self.media_root)
        media_settings.enable()
        self.addCleanup(shutil.rmtree, self.media_root, ignore_errors=True)
        self.addCleanup(media_settings.disable)
        self.product = Product.objects.create(
            name='Everyday Headphones',
            price=179.0,
            list_price=199.0,
            description='A sample product description.',
            offer_text='Limited-time store offer.',
            stock=5,
        )
        EmailVerification.objects.create(
            email='buyer@example.com',
            expires_at=timezone.now() + timedelta(days=365),
            verified_at=timezone.now(),
        )
        postal_patcher = patch(
            'store.views.validate_delivery_location',
            return_value={
                'pincode': '751001',
                'state': 'Odisha',
                'district': 'Khordha',
                'locality': 'Bhubaneswar GPO',
                'delivery': True,
            },
        )
        postal_patcher.start()
        self.addCleanup(postal_patcher.stop)

    def make_product_photos(self, count=3):
        uploads = []
        for index in range(count):
            image_data = BytesIO()
            Image.new('RGB', (4, 4), color=(index * 50, 80, 120)).save(image_data, format='PNG')
            uploads.append(SimpleUploadedFile(
                f'product-photo-{index}.png',
                image_data.getvalue(),
                content_type='image/png',
            ))
        return uploads

    def test_delivery_location_matches_postal_directory(self):
        office = {
            'Name': 'Bhubaneswar GPO',
            'District': 'Khordha',
            'State': 'Odisha',
            'DeliveryStatus': 'Delivery',
        }
        with patch('store.postal.lookup_pincode', return_value=[office]):
            result = validate_delivery_location('751001', 'Bhubaneswar', 'Odisha')
        self.assertEqual(result['district'], 'Khordha')
        self.assertTrue(result['delivery'])

    def test_delivery_location_rejects_mismatched_city_or_state(self):
        office = {
            'Name': 'Bhubaneswar GPO',
            'District': 'Khordha',
            'State': 'Odisha',
            'DeliveryStatus': 'Delivery',
        }
        with patch('store.postal.lookup_pincode', return_value=[office]):
            with self.assertRaises(PostalLookupError):
                validate_delivery_location('751001', 'Cuttack', 'Odisha')

    def test_delivery_location_rejects_non_delivery_post_office(self):
        office = {
            'Name': 'Bhubaneswar GPO',
            'District': 'Khordha',
            'State': 'Odisha',
            'DeliveryStatus': 'Non-Delivery',
        }
        with patch('store.postal.lookup_pincode', return_value=[office]):
            with self.assertRaises(PostalLookupError):
                validate_delivery_location('751001', 'Bhubaneswar', 'Odisha')

    def test_product_card_links_to_product_details(self):
        response = self.client.get(reverse('store'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse('product_detail', args=[self.product.id]))

    def test_product_detail_shows_description_and_offer(self):
        response = self.client.get(reverse('product_detail', args=[self.product.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.product.name)
        self.assertContains(response, self.product.description)
        self.assertContains(response, self.product.offer_text)
        self.assertContains(response, '10% off')
        self.assertContains(response, 'delivery-check')
        self.assertContains(response, reverse('checkout'))

    def test_homepage_groups_products_by_ordered_category(self):
        fashion = Category.objects.create(name='Fashion First', slug='fashion-first', sort_order=1)
        electronics = Category.objects.create(name='Electronics Next', slug='electronics-next', sort_order=2)
        self.product.category = electronics
        self.product.save(update_fields=['category'])
        Product.objects.create(
            name='Canvas Shirt',
            price=50,
            category=fashion,
            description='A categorized fashion listing.',
        )

        response = self.client.get(reverse('store'))

        self.assertEqual(response.status_code, 200)
        sections = response.context['category_sections']
        self.assertEqual([section['name'] for section in sections[:2]], [
            'Fashion First', 'Electronics Next',
        ])
        self.assertContains(response, 'category-shelf')
        self.assertContains(response, 'Canvas Shirt')
        self.assertContains(response, 'Everyday Headphones')

    def test_coupon_discount_is_applied_and_saved_on_checkout(self):
        seller = User.objects.create_user(username='coupon-seller', password='Password!123')
        seller_store = SellerStore.objects.create(
            owner=seller,
            name='Coupon Store',
            description='Everyday products with seller discounts.',
        )
        self.product.seller_store = seller_store
        self.product.save(update_fields=['seller_store'])
        coupon = ProductCoupon.objects.create(
            seller_store=seller_store,
            product=self.product,
            code='SAVE10',
            discount_percent='10.00',
        )
        self.client.cookies['cart'] = json.dumps({str(self.product.id): {'quantity': 1}})

        response = self.client.post(reverse('checkout'), {
            'action': 'apply_coupon',
            'coupon_code': coupon.code,
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Coupon savings:')
        self.assertContains(response, '161.10')

        response = self.client.post(reverse('checkout'), {
            'payment_method': 'cod',
            'name': 'Coupon Buyer',
            'email': 'buyer@example.com',
            'phone': '9876543210',
            'address': '12 Market Road',
            'city': 'Bhubaneswar',
            'state': 'Odisha',
            'zipcode': '751001',
        })

        self.assertEqual(response.status_code, 302, response.content.decode())
        order = Order.objects.get(order_number__startswith='AV-')
        line = order.orderitem_set.get()
        payment = Payment.objects.get(order=order)
        self.assertEqual(order.coupon_code, 'SAVE10')
        self.assertEqual(order.coupon_discount, 17.9)
        self.assertEqual(line.unit_price, 161.1)
        self.assertEqual(line.coupon_code, 'SAVE10')
        self.assertEqual(order.platform_fee, 3.22)
        self.assertEqual(order.platform_fee_gst, 0.58)
        self.assertEqual(order.delivery_fee, 0)
        self.assertEqual(order.get_total_amount, 164.9)
        self.assertEqual(payment.amount, 164.9)

    def test_seller_can_add_at_most_three_coupons_per_product(self):
        seller = User.objects.create_user(username='coupon-owner', password='Password!123')
        category = Category.objects.create(name='Coupon category', slug='coupon-category')
        seller_store = SellerStore.objects.create(
            owner=seller,
            name='Coupon Store',
            description='Everyday products with seller discounts.',
        )
        product = Product.objects.create(
            seller_store=seller_store,
            category=category,
            name='Coupon Product',
            price=100,
        )
        self.client.force_login(seller)
        for code in ('SAVE10', 'SAVE20', 'SAVE30'):
            response = self.client.post(reverse('seller_dashboard'), {
                'action': 'save_coupon',
                'product_id': product.pk,
                'code': code,
                'discount_percent': '10',
            })
            self.assertEqual(response.status_code, 302)

        response = self.client.post(reverse('seller_dashboard'), {
            'action': 'save_coupon',
            'product_id': product.pk,
            'code': 'SAVE40',
            'discount_percent': '10',
        }, follow=True)

        self.assertEqual(ProductCoupon.objects.filter(product=product).count(), 3)
        self.assertContains(response, 'up to three coupon codes')
        detail = self.client.get(reverse('product_detail', args=[product.pk]))
        self.assertContains(detail, 'SAVE10')
        self.assertContains(detail, 'SAVE20')
        self.assertContains(detail, 'SAVE30')

    def test_pwa_service_worker_is_available_at_root_scope(self):
        response = self.client.get(reverse('service_worker'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Service-Worker-Allowed'], '/')
        self.assertContains(response, 'avatar-store-v4')
        self.assertContains(response, 'order-status.js')
        self.assertContains(response, "request.mode === 'navigate'")

    def test_out_of_stock_product_controls_disable_and_reenable_after_restock(self):
        seller = User.objects.create_user(username='stock-seller', password='Password!123')
        SellerStore.objects.create(
            owner=seller,
            name='Stock Store',
            description='A seller store for inventory controls.',
        )
        self.product.seller_store = seller.seller_store
        self.product.stock = 0
        self.product.save(update_fields=['seller_store', 'stock'])

        response = self.client.get(reverse('product_detail', args=[self.product.pk]))
        self.assertContains(response, 'Currently out of stock')
        self.assertContains(response, 'detail-buy update-cart" disabled')
        self.assertContains(response, 'detail-add update-cart" disabled')
        self.assertContains(response, 'The seller must restock this listing')
        self.assertContains(response, '>Out of stock</button>')
        home = self.client.get(reverse('store'))
        self.assertContains(home, 'class="add-to-bag update-cart is-out-of-stock"')

        self.client.force_login(seller)
        dashboard = self.client.get(reverse('seller_dashboard'))
        self.assertContains(dashboard, 'type="number" name="stock"')
        self.assertNotContains(dashboard, 'numbe</label>r')
        response = self.client.post(reverse('seller_dashboard'), {
            'action': 'update_stock',
            'product_id': self.product.pk,
            'stock': '4',
        })
        self.assertEqual(response.status_code, 302)
        response = self.client.get(reverse('product_detail', args=[self.product.pk]))
        self.assertNotContains(response, 'detail-buy update-cart" disabled')
        self.assertNotContains(response, 'detail-add update-cart" disabled')

    def test_seller_product_can_be_added_to_authenticated_cart_with_csrf(self):
        seller = User.objects.create_user(username='cart-seller', password='Password!123')
        seller_store = SellerStore.objects.create(
            owner=seller,
            name='Cart Seller',
            description='Products for authenticated cart tests.',
        )
        self.product.seller_store = seller_store
        self.product.save(update_fields=['seller_store'])
        buyer = User.objects.create_user(
            username='cart-buyer',
            email='cartbuyer@example.com',
            password='Password!123',
        )
        client = Client(enforce_csrf_checks=True)
        client.force_login(buyer)

        client.get(reverse('store'))
        csrf_token = client.cookies['csrftoken'].value
        response = client.post(
            reverse('update_item'),
            data=json.dumps({
                'productId': self.product.pk,
                'action': 'add',
            }),
            content_type='application/json',
            HTTP_X_CSRFTOKEN=csrf_token,
        )

        self.assertEqual(response.status_code, 200, response.content.decode())
        response = client.get(reverse('cart'))
        self.assertContains(response, 'Everyday Headphones')
        self.assertEqual(response.context['order']['get_cart_items'], 1)

    def test_encoded_guest_cart_cookie_loads_seller_products(self):
        seller = User.objects.create_user(username='guest-cart-seller', password='Password!123')
        seller_store = SellerStore.objects.create(
            owner=seller,
            name='Guest Cart Seller',
            description='Products for guest cart tests.',
        )
        self.product.seller_store = seller_store
        self.product.save(update_fields=['seller_store'])
        self.client.cookies['cart'] = quote(json.dumps({
            str(self.product.pk): {'quantity': 2},
        }))

        response = self.client.get(reverse('cart'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Everyday Headphones')
        self.assertEqual(response.context['order']['get_cart_items'], 2)

    def test_unknown_product_returns_not_found(self):
        response = self.client.get(reverse('product_detail', args=[99999]))

        self.assertEqual(response.status_code, 404)

    def test_help_page_has_support_contact_and_delivery_information(self):
        response = self.client.get(reverse('help'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'avatarshop@gmail.com')
        self.assertContains(response, 'Delivery &amp; address')

    def test_category_price_and_variant_filters(self):
        category = Category.objects.create(name='Audio', slug='audio')
        self.product.category = category
        self.product.save(update_fields=['category'])
        ProductVariant.objects.create(product=self.product, name='Color', value='Blue')

        response = self.client.get(reverse('store'), {
            'category': 'audio',
            'min_price': '100',
            'max_price': '200',
            'variant': 'blue',
        })

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.product.name)

    def test_horizontal_filters_only_appear_after_search(self):
        response = self.client.get(reverse('store'))
        self.assertNotContains(response, 'catalog-filters')
        self.assertContains(response, 'Shop product categories')
        self.assertContains(response, 'Electronics')

        response = self.client.get(reverse('store'), {'q': 'Headphones'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'catalog-filters')
        self.assertContains(response, 'Filter search results')
        self.assertContains(response, 'SEARCH RESULTS FOR “Headphones”')

    def test_cod_checkout_creates_order_and_decrements_inventory(self):
        self.client.cookies['cart'] = json.dumps({str(self.product.id): {'quantity': 2}})
        response = self.client.post(reverse('checkout'), {
            'payment_method': 'cod',
            'name': 'Test Buyer',
            'email': 'buyer@example.com',
            'phone': '9876543210',
            'address': '12 Market Road',
            'city': 'Bhubaneswar',
            'state': 'Odisha',
            'zipcode': '751001',
        })

        self.assertEqual(response.status_code, 302)
        order = Order.objects.get(order_number__startswith='AV-')
        self.product.refresh_from_db()
        self.assertEqual(order.payment_method, 'cod')
        self.assertEqual(order.status, 'Placed')
        self.assertEqual(order.payment_status, 'pending')
        self.assertEqual(self.product.stock, 3)
        self.assertEqual(order.get_cart_total, 358.0)
        self.assertEqual(order.get_total_amount, 366.45)
        self.assertEqual(order.gst_amount, 0)
        self.assertEqual(order.platform_fee, 7.16)
        self.assertEqual(order.platform_fee_gst, 1.29)
        self.assertEqual(order.delivery_fee, 0)
        self.assertTrue(Payment.objects.filter(order=order, amount=366.45).exists())
        self.assertTrue(ShippingAddress.objects.filter(order=order, zipcode='751001').exists())

    def test_checkout_calculates_product_gst_platform_fee_and_free_delivery(self):
        self.product.gst_rate = 18
        self.product.save(update_fields=['gst_rate'])
        self.client.cookies['cart'] = json.dumps({str(self.product.id): {'quantity': 1}})
        checkout = self.client.get(reverse('checkout'))
        self.assertContains(checkout, 'GST on products: ₹32.22')
        self.assertContains(checkout, 'Platform fee (2.00%): ₹3.58')
        self.assertContains(checkout, 'GST on platform fee: ₹0.64')
        self.assertContains(checkout, 'Delivery: <strong>FREE</strong>')
        self.assertContains(checkout, 'Total to pay: ₹215.44')

        response = self.client.post(reverse('checkout'), {
            'payment_method': 'cod',
            'name': 'GST Buyer',
            'email': 'buyer@example.com',
            'phone': '9876543210',
            'address': '12 Market Road',
            'city': 'Bhubaneswar',
            'state': 'Odisha',
            'zipcode': '751001',
        })

        self.assertEqual(response.status_code, 302)
        order = Order.objects.get(order_number__startswith='AV-')
        line = order.orderitem_set.get()
        payment = Payment.objects.get(order=order)
        self.assertEqual(order.gst_amount, 32.22)
        self.assertEqual(order.platform_fee, 3.58)
        self.assertEqual(order.platform_fee_gst, 0.64)
        self.assertEqual(order.delivery_fee, 0)
        self.assertEqual(order.get_total_amount, 215.44)
        self.assertEqual(line.gst_amount, 32.22)
        self.assertEqual(payment.amount, 215.44)

    def test_signed_in_customer_can_checkout_without_email_verification(self):
        user = User.objects.create_user(
            username='signedinbuyer',
            email='buyer@example.com',
            password='SafePassword!123',
        )
        Customer.objects.create(
            user=user,
            name='Signed-in Buyer',
            email='buyer@example.com',
            email_verified=False,
            phone='+919876543210',
            phone_e164='+919876543210',
        )
        self.client.force_login(user)
        open_order = Order.objects.create(customer=user.customer, complete=False)
        OrderItem.objects.create(
            order=open_order,
            product=self.product,
            quantity=1,
            unit_price=self.product.price,
        )

        response = self.client.post(reverse('checkout'), {
            'payment_method': 'cod',
            'address': '12 Market Road',
            'city': 'Bhubaneswar',
            'state': 'Odisha',
            'zipcode': '751001',
        })

        self.assertEqual(response.status_code, 302)
        self.assertTrue(Order.objects.filter(customer=user.customer, complete=True).exists())

    def test_checkout_does_not_require_email_verification(self):
        EmailVerification.objects.filter(email='buyer@example.com').delete()
        self.client.cookies['cart'] = json.dumps({str(self.product.id): {'quantity': 1}})
        response = self.client.post(reverse('checkout'), {
            'payment_method': 'cod',
            'name': 'Test Buyer',
            'email': 'buyer@example.com',
            'phone': '9876543210',
            'address': '12 Market Road',
            'city': 'Bhubaneswar',
            'state': 'Odisha',
            'zipcode': '751001',
        })
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Order.objects.filter(order_number__startswith='AV-').exists())

    @override_settings(
        EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
        EMAIL_HOST='smtp.gmail.com',
        EMAIL_HOST_USER='avatarshop@gmail.com',
        EMAIL_HOST_PASSWORD='test-app-password',
        DEFAULT_FROM_EMAIL='avatarshop@gmail.com',
    )
    def test_guest_email_verification_link_confirms_checkout_email(self):
        self.client.cookies['cart'] = json.dumps({str(self.product.id): {'quantity': 1}})
        response = self.client.post(reverse('request_email_verification'), {
            'email': 'newbuyer@example.com',
        })
        self.assertEqual(response.status_code, 302)
        verification = EmailVerification.objects.get(email='newbuyer@example.com')
        response = self.client.get(reverse('verify_email', args=[verification.token]))
        self.assertEqual(response.status_code, 200)
        verification.refresh_from_db()
        self.assertIsNone(verification.verified_at)
        response = self.client.post(reverse('verify_email', args=[verification.token]))
        self.assertEqual(response.status_code, 302)
        response = self.client.get(reverse('checkout'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'newbuyer@example.com')
        self.assertNotContains(response, 'Send email verification link')

    @override_settings(
        EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
        EMAIL_HOST='smtp.gmail.com',
        EMAIL_HOST_USER='avatarshop@gmail.com',
        EMAIL_HOST_PASSWORD='test-app-password',
        DEFAULT_FROM_EMAIL='avatarshop@gmail.com',
    )
    def test_guest_email_otp_verifies_checkout_address(self):
        response = self.client.post(reverse('request_email_verification'), {
            'email': 'otpbuyer@example.com',
        })
        self.assertEqual(response.status_code, 302)
        verification = EmailVerification.objects.get(email='otpbuyer@example.com')
        otp_match = re.search(r'6-digit verification code: ([0-9]{6})', mail.outbox[0].body)
        self.assertIsNotNone(otp_match)

        response = self.client.post(reverse('verify_email_otp'), {
            'email': 'otpbuyer@example.com',
            'otp': otp_match.group(1),
        })
        self.assertEqual(response.status_code, 302)
        verification.refresh_from_db()
        self.assertIsNotNone(verification.verified_at)
        self.assertEqual(self.client.session['verified_checkout_email'], 'otpbuyer@example.com')

    @override_settings(
        EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
        EMAIL_HOST='smtp.gmail.com',
        EMAIL_HOST_USER='avatarshop@gmail.com',
        EMAIL_HOST_PASSWORD='test-app-password',
        DEFAULT_FROM_EMAIL='avatarshop@gmail.com',
    )
    def test_account_email_otp_marks_profile_verified(self):
        user = User.objects.create_user(
            username='emailbuyer',
            email='emailbuyer@example.com',
            password='Password!123',
        )
        customer = Customer.objects.create(
            user=user,
            email='emailbuyer@example.com',
            name='Email Buyer',
        )
        self.client.force_login(user)
        response = self.client.post(reverse('request_email_verification'), {
            'email': 'emailbuyer@example.com',
        })
        self.assertEqual(response.status_code, 302)
        verification = EmailVerification.objects.get(email='emailbuyer@example.com')
        otp_match = re.search(r'6-digit verification code: ([0-9]{6})', mail.outbox[0].body)
        self.assertIsNotNone(otp_match)

        response = self.client.post(reverse('verify_email_otp'), {
            'otp': otp_match.group(1),
        })
        self.assertEqual(response.status_code, 302)
        customer.refresh_from_db()
        self.assertTrue(customer.email_verified)
        verification.refresh_from_db()
        self.assertIsNotNone(verification.verified_at)

    @override_settings(
        EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
        EMAIL_HOST='smtp.gmail.com',
        EMAIL_HOST_USER='avatarshop@gmail.com',
        EMAIL_HOST_PASSWORD='test-app-password',
        DEFAULT_FROM_EMAIL='avatarshop@gmail.com',
    )
    def test_password_reset_email_link_changes_password(self):
        user = User.objects.create_user(
            username='resetbuyer',
            email='resetbuyer@example.com',
            password='OldPassword!123',
        )
        response = self.client.post(reverse('password_reset'), {
            'email': 'resetbuyer@example.com',
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse('password_reset_done'))
        self.assertEqual(len(mail.outbox), 1)
        link_match = re.search(r'(http://testserver\S+)', mail.outbox[0].body)
        self.assertIsNotNone(link_match)

        response = self.client.get(link_match.group(1), follow=True)
        self.assertEqual(response.status_code, 200)
        reset_path = response.request['PATH_INFO']
        response = self.client.post(reset_path, {
            'new_password1': 'NewSafePassword!456',
            'new_password2': 'NewSafePassword!456',
        })
        self.assertEqual(response.status_code, 302)
        user.refresh_from_db()
        self.assertTrue(user.check_password('NewSafePassword!456'))

    @override_settings(EMAIL_HOST='', EMAIL_HOST_USER='', EMAIL_HOST_PASSWORD='')
    def test_password_reset_reports_missing_smtp_configuration(self):
        response = self.client.post(reverse('password_reset'), {
            'email': 'nobody@example.com',
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Password reset email is not configured.')

    @override_settings(MSG91_AUTH_KEY='test-auth-key', MSG91_OTP_TEMPLATE_ID='template-id')
    @patch('store.views.verify_phone_otp')
    @patch('store.views.send_phone_otp')
    def test_phone_otp_login_sends_and_verifies_provider_code(self, send_otp, verify_otp):
        user = User.objects.create_user(username='phonebuyer', password='Password!123')
        customer = Customer.objects.create(
            user=user,
            name='Phone Buyer',
            email='phonebuyer@example.com',
            phone='+919876543210',
            phone_e164='+919876543210',
        )
        response = self.client.post(reverse('phone_login_request'), {
            'phone': '9876543210',
        })
        self.assertRedirects(response, reverse('phone_login_verify'))
        send_otp.assert_called_once_with('+919876543210')
        self.assertTrue(self.client.session['phone_otp_sent'])
        self.client.post(reverse('phone_login_request'), {
            'phone': '9876543210',
        })
        self.assertTrue(self.client.session['phone_otp_sent'])
        send_otp.assert_called_once()

        response = self.client.post(reverse('phone_login_verify'), {'otp': '123456'})
        self.assertRedirects(response, reverse('account'))
        verify_otp.assert_called_once_with('+919876543210', '123456')
        self.assertEqual(int(self.client.session['_auth_user_id']), user.id)
        self.assertEqual(customer.phone_e164, '+919876543210')

    @override_settings(MSG91_AUTH_KEY='test-auth-key', MSG91_OTP_TEMPLATE_ID='template-id')
    @patch('store.views.send_phone_otp')
    def test_phone_otp_request_does_not_send_to_unknown_number(self, send_otp):
        response = self.client.post(reverse('phone_login_request'), {
            'phone': '9876543210',
        })
        self.assertRedirects(response, reverse('phone_login_verify'))
        send_otp.assert_not_called()
        response = self.client.post(reverse('phone_login_verify'), {'otp': '123456'})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('_auth_user_id', self.client.session)

    @override_settings(MSG91_AUTH_KEY='', MSG91_OTP_TEMPLATE_ID='')
    def test_phone_login_requires_local_msg91_credentials(self):
        response = self.client.post(reverse('phone_login_request'), {
            'phone': '9876543210',
        }, follow=True)
        self.assertEqual(response.redirect_chain[-1][0], reverse('phone_login_verify'))
        self.assertContains(response, 'Phone OTP login is not configured.')

    @override_settings(
        MSG91_AUTH_KEY='test-auth-key',
        MSG91_OTP_TEMPLATE_ID='approved-template',
    )
    def test_msg91_send_and_verify_use_provider_otp_api(self):
        with patch('store.sms.urlopen') as urlopen:
            urlopen.return_value.__enter__.return_value.read.return_value = b'{"type":"success"}'
            send_phone_otp('+919876543210')
            send_request = urlopen.call_args.args[0]
            self.assertEqual(
                parse_qs(urlsplit(send_request.full_url).query),
                {'template_id': ['approved-template'], 'mobile': ['919876543210']},
            )
            self.assertEqual(send_request.get_header('Authkey'), 'test-auth-key')

            verify_phone_otp('+919876543210', '123456')
            verify_request = urlopen.call_args.args[0]
            self.assertEqual(verify_request.get_method(), 'GET')
            self.assertEqual(
                parse_qs(urlsplit(verify_request.full_url).query),
                {'mobile': ['919876543210'], 'otp': ['123456']},
            )

    def test_expired_email_verification_link_is_not_accepted(self):
        verification = EmailVerification.objects.create(
            email='expired@example.com',
            expires_at=timezone.now() - timedelta(minutes=1),
        )
        response = self.client.get(reverse('verify_email', args=[verification.token]))
        verification.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertIsNone(verification.verified_at)

    def test_delivery_endpoint_returns_directory_areas(self):
        with patch('store.postal.lookup_pincode', return_value=[{
            'Name': 'Bhubaneswar GPO',
            'District': 'Khordha',
            'State': 'Odisha',
            'DeliveryStatus': 'Delivery',
        }]):
            response = self.client.post(
                reverse('check_delivery'),
                json.dumps({'pincode': '751001'}),
                content_type='application/json',
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['valid'])
        self.assertEqual(response.json()['locations'][0]['district'], 'Khordha')

    def test_checkout_rejects_insufficient_stock_without_creating_order(self):
        self.client.cookies['cart'] = json.dumps({str(self.product.id): {'quantity': 6}})
        response = self.client.post(reverse('checkout'), {
            'payment_method': 'cod',
            'name': 'Test Buyer',
            'email': 'buyer@example.com',
            'phone': '9876543210',
            'address': '12 Market Road',
            'city': 'Bhubaneswar',
            'state': 'Odisha',
            'zipcode': '751001',
        })

        self.assertEqual(response.status_code, 400)
        self.assertFalse(Order.objects.filter(order_number__startswith='AV-').exists())
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 5)

    def test_manual_upi_reference_waits_for_staff_verification(self):
        self.client.cookies['cart'] = json.dumps({str(self.product.id): {'quantity': 1}})
        response = self.client.post(reverse('checkout'), {
            'payment_method': 'upi_manual',
            'name': 'Test Buyer',
            'email': 'buyer@example.com',
            'phone': '9876543210',
            'address': '12 Market Road',
            'city': 'Bhubaneswar',
            'state': 'Odisha',
            'zipcode': '751001',
        })
        order = Order.objects.get(order_number__startswith='AV-')
        payment = Payment.objects.get(order=order)

        self.assertEqual(response.status_code, 302)
        self.client.post(reverse('order_complete', args=[order.public_token]), {'reference': 'UTR123456'})
        payment.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(payment.status, 'pending_review')
        self.assertEqual(order.payment_status, 'pending_review')
        self.assertEqual(order.status, 'Pending Payment')

    def test_variant_checkout_uses_variant_price_and_stock(self):
        variant = ProductVariant.objects.create(
            product=self.product,
            name='Color',
            value='Blue',
            price_adjustment=10,
            stock=2,
        )
        self.client.cookies['cart'] = json.dumps({
            f'{self.product.id}:{variant.id}': {'quantity': 1},
        })
        response = self.client.post(reverse('checkout'), {
            'payment_method': 'cod',
            'name': 'Test Buyer',
            'email': 'buyer@example.com',
            'phone': '9876543210',
            'address': '12 Market Road',
            'city': 'Bhubaneswar',
            'state': 'Odisha',
            'zipcode': '751001',
        })

        self.assertEqual(response.status_code, 302)
        line = OrderItem.objects.get(order__order_number__startswith='AV-')
        variant.refresh_from_db()
        self.assertEqual(line.variant, variant)
        self.assertEqual(line.variant_name, 'Color: Blue')
        self.assertEqual(line.get_total, 189.0)
        self.assertEqual(variant.stock, 1)

    @override_settings(
        PAYU_KEY='merchant-key',
        PAYU_SALT='merchant-salt',
        PAYPAL_CLIENT_ID='paypal-client',
        PAYPAL_CLIENT_SECRET='paypal-secret',
    )
    def test_checkout_shows_configured_payments_and_names_supported_upi_apps(self):
        self.client.cookies['cart'] = json.dumps({str(self.product.id): {'quantity': 1}})

        response = self.client.get(reverse('checkout'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'value="payu"')
        self.assertContains(response, 'value="paypal"')
        self.assertContains(response, 'Google Pay')
        self.assertContains(response, 'PhonePe')
        self.assertContains(response, 'PayPal')

    @override_settings(
        PAYU_KEY='merchant-key',
        PAYU_SALT='merchant-salt',
        PAYU_PAYMENT_URL='https://test.payu.in/_payment',
    )
    def test_payu_checkout_posts_server_calculated_amount(self):
        self.client.cookies['cart'] = json.dumps({str(self.product.id): {'quantity': 1}})
        response = self.client.post(reverse('checkout'), {
            'payment_method': 'payu',
            'name': 'Test Buyer',
            'email': 'buyer@example.com',
            'phone': '9876543210',
            'address': '12 Market Road',
            'city': 'Bhubaneswar',
            'state': 'Odisha',
            'zipcode': '751001',
            'amount': '1.00',
        })

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'https://test.payu.in/_payment')
        self.assertContains(response, '183.22')
        self.assertNotContains(response, 'value="1.00"')
        payment = Payment.objects.get(method='payu')
        self.assertEqual(payment.amount, 183.22)

    @override_settings(
        PAYPAL_CLIENT_ID='paypal-client',
        PAYPAL_CLIENT_SECRET='paypal-secret',
        PAYPAL_API_BASE='https://api-m.sandbox.paypal.com',
    )
    def test_paypal_checkout_creates_provider_order_with_server_total(self):
        self.client.cookies['cart'] = json.dumps({str(self.product.id): {'quantity': 1}})
        provider_order = {
            'id': 'PAYPAL-ORDER-123',
            'links': [{
                'rel': 'approve',
                'href': 'https://www.sandbox.paypal.com/checkoutnow?token=PAYPAL-ORDER-123',
            }],
        }
        checkout_fields = {
            'payment_method': 'paypal',
            'name': 'Test Buyer',
            'email': 'buyer@example.com',
            'phone': '9876543210',
            'address': '12 Market Road',
            'city': 'Bhubaneswar',
            'state': 'Odisha',
            'zipcode': '751001',
            'amount': '1.00',
        }

        with patch('store.views.paypal_api_request', return_value=provider_order) as api_request:
            response = self.client.post(reverse('checkout'), checkout_fields)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], provider_order['links'][0]['href'])
        payment = Payment.objects.get(method='paypal')
        self.assertEqual(payment.amount, 183.22)
        self.assertEqual(payment.gateway_payment_id, 'PAYPAL-ORDER-123')
        payload = api_request.call_args.args[2]
        self.assertEqual(payload['purchase_units'][0]['amount'], {
            'currency_code': 'INR', 'value': '183.22',
        })

    @override_settings(
        PAYPAL_CLIENT_ID='paypal-client',
        PAYPAL_CLIENT_SECRET='paypal-secret',
        PAYPAL_API_BASE='https://api-m.sandbox.paypal.com',
    )
    def test_paypal_setup_error_releases_stock_and_restores_signed_in_cart(self):
        buyer = User.objects.create_user(
            username='paypal-buyer',
            email='buyer@example.com',
            password='Password!123',
        )
        Customer.objects.create(
            user=buyer,
            name='PayPal Buyer',
            email='buyer@example.com',
        )
        open_cart = Order.objects.create(customer=buyer.customer, complete=False)
        OrderItem.objects.create(order=open_cart, product=self.product, quantity=1)
        self.client.force_login(buyer)

        with patch(
            'store.views.paypal_api_request',
            side_effect=PayPalGatewayError('PayPal is unavailable.'),
        ):
            response = self.client.post(reverse('checkout'), {
                'payment_method': 'paypal',
                'email': 'buyer@example.com',
                'phone': '9876543210',
                'address': '12 Market Road',
                'city': 'Bhubaneswar',
                'state': 'Odisha',
                'zipcode': '751001',
            })

        self.assertEqual(response.status_code, 503)
        self.assertContains(response, 'PayPal is unavailable.', status_code=503)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 5)
        open_cart.refresh_from_db()
        self.assertEqual(open_cart.orderitem_set.get().quantity, 1)
        self.assertEqual(Payment.objects.get(method='paypal').status, 'failed')

    @override_settings(
        PAYPAL_CLIENT_ID='paypal-client',
        PAYPAL_CLIENT_SECRET='paypal-secret',
        PAYPAL_API_BASE='https://api-m.sandbox.paypal.com',
    )
    def test_paypal_return_captures_and_verifies_payment_before_marking_paid(self):
        customer = Customer.objects.create(name='Test Buyer', email='buyer@example.com')
        order = Order.objects.create(
            customer=customer,
            complete=True,
            status='Pending Payment',
            payment_method='paypal',
        )
        payment = Payment.objects.create(
            order=order,
            method='paypal',
            amount=179.0,
            transaction_id='paypal-test-transaction',
            gateway_payment_id='PAYPAL-ORDER-123',
        )
        capture_response = {
            'status': 'COMPLETED',
            'purchase_units': [{
                'reference_id': payment.transaction_id,
                'payments': {'captures': [{
                    'id': 'PAYPAL-CAPTURE-456',
                    'status': 'COMPLETED',
                    'amount': {'currency_code': 'INR', 'value': '179.00'},
                }]},
            }],
        }

        with patch('store.views.paypal_api_request', return_value=capture_response) as api_request:
            response = self.client.get(
                reverse('paypal_return', args=[order.public_token]),
                {'token': 'PAYPAL-ORDER-123'},
            )

        self.assertEqual(response.status_code, 302)
        api_request.assert_called_once_with(
            'POST', '/v2/checkout/orders/PAYPAL-ORDER-123/capture', {}
        )
        payment.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(payment.status, 'paid')
        self.assertEqual(payment.customer_reference, 'PAYPAL-CAPTURE-456')
        self.assertEqual(payment.gateway_payment_id, 'PAYPAL-ORDER-123')
        self.assertEqual(order.payment_status, 'paid')
        self.assertEqual(order.status, 'Placed')

    @override_settings(
        PAYPAL_CLIENT_ID='paypal-client',
        PAYPAL_CLIENT_SECRET='paypal-secret',
    )
    def test_paypal_return_rejects_capture_with_wrong_amount(self):
        customer = Customer.objects.create(name='Test Buyer', email='buyer@example.com')
        order = Order.objects.create(
            customer=customer,
            complete=True,
            status='Pending Payment',
            payment_method='paypal',
        )
        payment = Payment.objects.create(
            order=order,
            method='paypal',
            amount=179.0,
            transaction_id='paypal-amount-check',
            gateway_payment_id='PAYPAL-ORDER-789',
        )
        capture_response = {
            'status': 'COMPLETED',
            'purchase_units': [{
                'reference_id': payment.transaction_id,
                'payments': {'captures': [{
                    'id': 'PAYPAL-CAPTURE-WRONG',
                    'status': 'COMPLETED',
                    'amount': {'currency_code': 'INR', 'value': '1.00'},
                }]},
            }],
        }

        with patch('store.views.paypal_api_request', return_value=capture_response):
            response = self.client.get(
                reverse('paypal_return', args=[order.public_token]),
                {'token': 'PAYPAL-ORDER-789'},
            )

        self.assertEqual(response.status_code, 400)
        payment.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(payment.status, 'pending')
        self.assertEqual(order.payment_status, 'pending')

    @override_settings(PAYU_KEY='merchant-key', PAYU_SALT='merchant-salt')
    def test_payu_callback_verifies_hash_and_marks_order_paid(self):
        customer = Customer.objects.create(name='Test Buyer', email='buyer@example.com')
        order = Order.objects.create(customer=customer, complete=True, status='Pending Payment')
        payment = Payment.objects.create(
            order=order,
            method='payu',
            amount=179.0,
            transaction_id='payu-test-transaction',
        )
        values = {
            'status': 'success',
            'txnid': payment.transaction_id,
            'amount': '179.00',
            'productinfo': 'Avatar test order',
            'firstname': 'Test Buyer',
            'email': 'buyer@example.com',
            'key': 'merchant-key',
            'udf1': '',
            'udf2': '',
            'udf3': '',
            'udf4': '',
            'udf5': '',
            'mihpayid': 'gateway-payment-id',
        }
        reverse_fields = [
            values['udf5'], values['udf4'], values['udf3'], values['udf2'],
            values['udf1'], values['email'], values['firstname'], values['productinfo'],
            values['amount'], values['txnid'], values['key'],
        ]
        values['hash'] = hashlib.sha512(
            ('merchant-salt|success|' + '|'.join(reverse_fields)).encode('utf-8')
        ).hexdigest()

        response = self.client.post(reverse('payu_return'), values)

        self.assertEqual(response.status_code, 302)
        payment.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(payment.status, 'paid')
        self.assertEqual(order.payment_status, 'paid')
        self.assertEqual(order.status, 'Placed')

    def test_seller_can_advance_only_owned_fulfillment_and_customer_poll_sees_updates(self):
        seller = User.objects.create_user(username='fulfillment-seller', password='Password!123')
        seller_store = SellerStore.objects.create(
            owner=seller,
            name='Fulfillment Shop',
            description='Seller order update testing.',
        )
        self.product.seller_store = seller_store
        self.product.save(update_fields=['seller_store'])
        self.client.cookies['cart'] = json.dumps({str(self.product.pk): {'quantity': 1}})
        response = self.client.post(reverse('checkout'), {
            'payment_method': 'cod',
            'name': 'Test Buyer',
            'email': 'buyer@example.com',
            'phone': '9876543210',
            'address': '12 Market Road',
            'city': 'Bhubaneswar',
            'state': 'Odisha',
            'zipcode': '751001',
        })
        self.assertEqual(response.status_code, 302)
        order = Order.objects.get(order_number__startswith='AV-')
        fulfillment = OrderFulfillment.objects.get(order=order, seller_store=seller_store)
        self.assertEqual(fulfillment.status, 'Placed')

        self.client.force_login(seller)
        dashboard = self.client.get(reverse('seller_dashboard'))
        self.assertContains(dashboard, 'Orders &amp; fulfillment')
        self.assertContains(dashboard, 'Packed')
        update = {
            'action': 'update_fulfillment',
            'fulfillment_id': fulfillment.pk,
            'shipping_partner': 'Avatar Express',
            'tracking_number': 'TRACK-123',
            'tracking_url': 'https://carrier.example/track/TRACK-123',
        }
        for status in ('Packed', 'Dispatched', 'Shipped', 'Out for Delivery', 'Delivered'):
            response = self.client.post(reverse('seller_dashboard'), {
                **update,
                'status': status,
            })
            self.assertEqual(response.status_code, 302)

        fulfillment.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(fulfillment.status, 'Delivered')
        self.assertEqual(fulfillment.tracking_number, 'TRACK-123')
        self.assertEqual(order.status, 'Delivered')
        self.assertIsNotNone(order.delivered_at)
        status_response = self.client.get(reverse('order_status', args=[order.public_token]))
        self.assertEqual(status_response.status_code, 200)
        self.assertEqual(status_response.json()['fulfillments'][0]['status'], 'Delivered')
        self.assertEqual(status_response.json()['fulfillments'][0]['tracking_number'], 'TRACK-123')

        other_seller = User.objects.create_user(username='other-fulfillment-seller', password='Password!123')
        SellerStore.objects.create(
            owner=other_seller,
            name='Other Shop',
            description='Not the owner of this product.',
        )
        self.client.force_login(other_seller)
        response = self.client.post(reverse('seller_dashboard'), {
            **update,
            'status': 'Delivered',
        })
        self.assertEqual(response.status_code, 404)

    def test_cod_order_invoice_download_returns_pdf_with_signatory(self):
        self.client.cookies['cart'] = json.dumps({str(self.product.pk): {'quantity': 1}})
        response = self.client.post(reverse('checkout'), {
            'payment_method': 'cod',
            'name': 'Invoice Buyer',
            'email': 'buyer@example.com',
            'phone': '9876543210',
            'address': '12 Market Road',
            'city': 'Bhubaneswar',
            'state': 'Odisha',
            'zipcode': '751001',
        })
        self.assertEqual(response.status_code, 302)
        order = Order.objects.get(order_number__startswith='AV-')

        invoice = self.client.get(reverse('download_invoice', args=[order.public_token]))

        self.assertEqual(invoice.status_code, 200)
        self.assertEqual(invoice['Content-Type'], 'application/pdf')
        self.assertIn('Avatar-Invoice-', invoice['Content-Disposition'])
        self.assertTrue(b''.join(invoice.streaming_content).startswith(b'%PDF-'))

    def test_order_cancellation_releases_reserved_inventory(self):
        customer = Customer.objects.create(name='Test Buyer', email='buyer@example.com')
        order = Order.objects.create(
            customer=customer,
            complete=True,
            status='Placed',
            payment_method='cod',
        )
        OrderItem.objects.create(
            order=order,
            product=self.product,
            quantity=2,
            unit_price=179,
            stock_source='product',
        )
        Product.objects.filter(pk=self.product.pk).update(stock=3)

        response = self.client.post(reverse('order_complete', args=[order.public_token]), {
            'action': 'cancel',
            'reason': 'ordered_by_mistake',
        })

        self.assertEqual(response.status_code, 302)
        order.refresh_from_db()
        self.product.refresh_from_db()
        self.assertEqual(order.status, 'Cancelled')
        self.assertEqual(order.cancellation_reason, 'Ordered by mistake')
        self.assertEqual(self.product.stock, 5)
        self.assertTrue(order.inventory_released)

    def test_order_cancellation_requires_a_dropdown_reason_and_other_details(self):
        customer = Customer.objects.create(name='Test Buyer', email='buyer@example.com')
        order = Order.objects.create(
            customer=customer,
            complete=True,
            status='Placed',
            payment_method='cod',
        )
        details = self.client.get(reverse('order_complete', args=[order.public_token]))
        self.assertContains(details, 'Why are you cancelling?')
        self.assertContains(details, 'ordered_by_mistake')

        response = self.client.post(reverse('order_complete', args=[order.public_token]), {
            'action': 'cancel',
            'reason': 'other',
        })
        self.assertEqual(response.status_code, 302)
        order.refresh_from_db()
        self.assertEqual(order.status, 'Placed')

        response = self.client.post(reverse('order_complete', args=[order.public_token]), {
            'action': 'cancel',
            'reason': 'other',
            'reason_detail': 'Found a different item.',
        })
        order.refresh_from_db()
        self.assertEqual(order.status, 'Cancelled')
        self.assertEqual(order.cancellation_reason, 'Other: Found a different item.')

    def test_return_request_requires_recent_delivery(self):
        customer = Customer.objects.create(name='Test Buyer', email='buyer@example.com')
        order = Order.objects.create(
            customer=customer,
            complete=True,
            status='Delivered',
            delivered_at=timezone.now(),
        )
        response = self.client.post(reverse('order_complete', args=[order.public_token]), {
            'action': 'return',
            'reason': 'Item is damaged',
        })

        self.assertEqual(response.status_code, 302)
        order.refresh_from_db()
        self.assertEqual(order.status, 'Return Requested')
        self.assertEqual(order.return_reason, 'Item is damaged')

    @override_settings(PAYU_KEY='merchant-key', PAYU_SALT='merchant-salt')
    def test_payu_success_after_cancellation_does_not_reactivate_order(self):
        customer = Customer.objects.create(name='Test Buyer', email='buyer@example.com')
        order = Order.objects.create(
            customer=customer,
            complete=True,
            status='Cancelled',
            payment_method='payu',
            payment_status='failed',
            inventory_released=True,
        )
        payment = Payment.objects.create(
            order=order,
            method='payu',
            amount=179.0,
            transaction_id='late-payu-transaction',
            status='failed',
        )
        values = {
            'status': 'success',
            'txnid': payment.transaction_id,
            'amount': '179.00',
            'productinfo': 'Avatar test order',
            'firstname': 'Test Buyer',
            'email': 'buyer@example.com',
            'key': 'merchant-key',
            'udf1': '',
            'udf2': '',
            'udf3': '',
            'udf4': '',
            'udf5': '',
            'mihpayid': 'late-gateway-payment',
        }
        reverse_fields = [
            values['udf5'], values['udf4'], values['udf3'], values['udf2'],
            values['udf1'], values['email'], values['firstname'], values['productinfo'],
            values['amount'], values['txnid'], values['key'],
        ]
        values['hash'] = hashlib.sha512(
            ('merchant-salt|success|' + '|'.join(reverse_fields)).encode('utf-8')
        ).hexdigest()

        response = self.client.post(reverse('payu_return'), values)

        self.assertEqual(response.status_code, 302)
        order.refresh_from_db()
        payment.refresh_from_db()
        self.assertEqual(order.status, 'Cancelled')
        self.assertEqual(order.payment_status, 'paid')
        self.assertEqual(payment.status, 'paid')

    def test_registration_collects_profile_details(self):
        response = self.client.post(reverse('register'), {
            'username': 'avatarbuyer',
            'password1': 'SafePassword!123',
            'password2': 'SafePassword!123',
            'first_name': 'Avatar Buyer',
            'email': 'avatarbuyer@example.com',
            'phone': '9876543210',
        })

        self.assertEqual(response.status_code, 302)
        user = User.objects.get(username='avatarbuyer')
        self.assertEqual(user.customer.name, 'Avatar Buyer')
        self.assertEqual(user.customer.phone, '+919876543210')
        self.assertEqual(user.customer.phone_e164, '+919876543210')
        self.assertTrue(user.is_active)
        self.assertEqual(int(self.client.session['_auth_user_id']), user.id)

    def test_registration_and_email_password_login_do_not_require_verification(self):
        response = self.client.post(reverse('register'), {
            'username': 'verifiedbuyer',
            'password1': 'SafePassword!123',
            'password2': 'SafePassword!123',
            'first_name': 'Verified Buyer',
            'email': 'verifiedbuyer@example.com',
            'phone': '',
        })
        self.assertRedirects(response, reverse('account'))
        user = User.objects.get(username='verifiedbuyer')
        self.assertTrue(user.is_active)
        self.assertFalse(EmailVerification.objects.filter(user=user).exists())
        self.assertEqual(int(self.client.session['_auth_user_id']), user.id)
        self.client.post(reverse('logout'))

        response = self.client.post(reverse('login'), {
            'username': 'verifiedbuyer@example.com',
            'password': 'SafePassword!123',
        })
        self.assertRedirects(response, reverse('account'))
        self.assertEqual(int(self.client.session['_auth_user_id']), user.id)

    def test_database_url_configures_neon_postgresql_without_exposing_credentials(self):
        with mock_patch.dict(os.environ, {
            'DATABASE_URL': (
                'postgresql://sample-user:sample-password@db.example.test/store'
                '?sslmode=require&channel_binding=require'
            ),
        }):
            config = database_config()

        self.assertEqual(config['ENGINE'], 'django.db.backends.postgresql')
        self.assertEqual(config['NAME'], 'store')
        self.assertEqual(config['USER'], 'sample-user')
        self.assertEqual(config['HOST'], 'db.example.test')
        self.assertEqual(config['OPTIONS'], {
            'sslmode': 'require',
            'channel_binding': 'require',
        })

    def test_seller_portal_has_its_own_login_and_signup_routes(self):
        login_response = self.client.get(reverse('seller_login'))
        register_response = self.client.get(reverse('seller_register'))
        shopper_login_response = self.client.get(reverse('login'))

        self.assertEqual(login_response.status_code, 200)
        self.assertContains(login_response, 'Avatar Seller')
        self.assertContains(login_response, 'Continue to seller panel')
        self.assertEqual(register_response.status_code, 200)
        self.assertContains(register_response, 'Create seller account')
        self.assertNotContains(login_response, 'Resend verification email')
        self.assertNotContains(register_response, 'Shopping account')
        self.assertContains(shopper_login_response, 'Seller login')

    def test_seller_signup_logs_in_without_email_verification(self):
        response = self.client.post(reverse('seller_register'), {
            'username': 'newdealer',
            'password1': 'SafePassword!123',
            'password2': 'SafePassword!123',
            'first_name': 'New Dealer',
            'email': 'newdealer@example.com',
            'phone': '',
        })
        self.assertRedirects(response, reverse('seller_dashboard'))
        user = User.objects.get(username='newdealer')
        self.assertTrue(user.is_active)
        self.assertFalse(EmailVerification.objects.filter(user=user).exists())
        self.assertEqual(int(self.client.session['_auth_user_id']), user.id)

    def test_account_settings_has_be_a_seller_entry(self):
        response = self.client.get(reverse('account_settings'))
        self.assertEqual(response.status_code, 302)

        user = User.objects.create_user(username='settings-user', password='safe-password')
        self.client.force_login(user)
        response = self.client.get(reverse('account_settings'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Be a seller')
        self.assertContains(response, 'Seller login')
        self.assertContains(response, reverse('seller_login'))
        self.assertContains(response, reverse('seller_register'))

    def test_account_shows_seller_login_and_start_selling_links(self):
        user = User.objects.create_user(username='account-user', password='safe-password')
        self.client.force_login(user)

        response = self.client.get(reverse('account'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Seller login')
        self.assertContains(response, reverse('seller_login'))
        self.assertContains(response, 'Be a seller')
        self.assertContains(response, reverse('seller_register'))

    def test_inactive_legacy_user_can_log_in_with_correct_password(self):
        user = User.objects.create_user(
            username='legacy-inactive',
            email='legacy@example.com',
            password='SafePassword!123',
            is_active=False,
        )

        response = self.client.post(reverse('seller_login'), {
            'username': 'legacy@example.com',
            'password': 'SafePassword!123',
        })

        self.assertRedirects(response, reverse('seller_dashboard'))
        user.refresh_from_db()
        self.assertTrue(user.is_active)
        self.assertEqual(int(self.client.session['_auth_user_id']), user.id)

    def test_analytics_is_staff_only(self):
        anonymous_response = self.client.get(reverse('analytics'))
        self.assertEqual(anonymous_response.status_code, 302)

        staff_user = User.objects.create_user(username='staff', password='staff-password', is_staff=True)
        self.client.force_login(staff_user)
        staff_response = self.client.get(reverse('analytics'))
        self.assertEqual(staff_response.status_code, 200)
        self.assertContains(staff_response, 'Store analytics')

    def test_seller_dashboard_requires_login_and_allows_store_setup(self):
        response = self.client.get(reverse('seller_dashboard'))
        self.assertEqual(response.status_code, 302)

        seller = User.objects.create_user(username='seller', password='seller-password')
        self.client.force_login(seller)
        response = self.client.get(reverse('seller_dashboard'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Open your seller store')

        response = self.client.post(reverse('seller_dashboard'), {
            'action': 'save_store',
            'name': 'Northside Goods',
            'description': 'Useful finds for everyday life.',
        })
        self.assertRedirects(response, reverse('seller_dashboard'))
        self.assertTrue(SellerStore.objects.filter(owner=seller, name='Northside Goods').exists())

    def test_seller_inventory_tabs_and_quick_updates_are_owner_scoped(self):
        owner = User.objects.create_user(username='inventory-owner', password='Password!123')
        owner_store = SellerStore.objects.create(
            owner=owner,
            name='Owner Shop',
            description='Owner inventory for everyday products.',
        )
        other_user = User.objects.create_user(username='inventory-other', password='Password!123')
        other_store = SellerStore.objects.create(
            owner=other_user,
            name='Other Shop',
            description='A different seller account.',
        )
        own_product = Product.objects.create(
            seller_store=owner_store,
            name='Owner Listing',
            price=25,
            stock=8,
            low_stock_threshold=2,
        )
        other_product = Product.objects.create(
            seller_store=other_store,
            name='Private Other Listing',
            price=35,
            stock=4,
        )
        self.client.force_login(owner)

        response = self.client.get(reverse('seller_dashboard'), {'listing_status': 'listed'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Owner Listing')
        self.assertNotContains(response, 'Private Other Listing')
        self.assertEqual(response.context['listing_tabs'][0]['count'], 1)

        response = self.client.post(reverse('seller_dashboard'), {
            'action': 'update_stock',
            'product_id': own_product.pk,
            'stock': '2',
            'listing_status': 'listed',
        })
        self.assertEqual(response.status_code, 302)
        own_product.refresh_from_db()
        self.assertEqual(own_product.stock, 2)
        response = self.client.get(reverse('seller_dashboard'), {'listing_status': 'limited'})
        self.assertContains(response, 'Owner Listing')
        self.assertContains(response, 'Limited stock')

        response = self.client.post(reverse('seller_dashboard'), {
            'action': 'update_stock',
            'product_id': own_product.pk,
            'stock': '0',
            'listing_status': 'limited',
        })
        own_product.refresh_from_db()
        self.assertEqual(own_product.stock, 0)
        response = self.client.get(reverse('seller_dashboard'), {'listing_status': 'out_of_stock'})
        self.assertContains(response, 'Owner Listing')

        response = self.client.post(reverse('seller_dashboard'), {
            'action': 'set_visibility',
            'product_id': own_product.pk,
            'visibility': 'hidden',
            'listing_status': 'out_of_stock',
        })
        own_product.refresh_from_db()
        self.assertFalse(own_product.is_active)
        response = self.client.get(reverse('seller_dashboard'), {'listing_status': 'hidden'})
        self.assertContains(response, 'Owner Listing')
        self.assertContains(response, 'List product')

        self.client.post(reverse('seller_dashboard'), {
            'action': 'set_visibility',
            'product_id': own_product.pk,
            'visibility': 'listed',
            'listing_status': 'hidden',
        })
        self.client.post(reverse('seller_dashboard'), {
            'action': 'update_stock',
            'product_id': own_product.pk,
            'stock': '',
            'listing_status': 'out_of_stock',
        })
        own_product.refresh_from_db()
        self.assertTrue(own_product.is_active)
        self.assertIsNone(own_product.stock)
        response = self.client.get(reverse('seller_dashboard'), {'listing_status': 'listed'})
        self.assertContains(response, 'Owner Listing')

        response = self.client.post(reverse('seller_dashboard'), {
            'action': 'update_stock',
            'product_id': other_product.pk,
            'stock': '0',
        })
        self.assertEqual(response.status_code, 404)
        other_product.refresh_from_db()
        self.assertEqual(other_product.stock, 4)

    def test_seller_can_create_and_edit_only_owned_listings(self):
        seller = User.objects.create_user(username='seller', password='seller-password')
        category = Category.objects.create(name='Accessories', slug='accessories')
        seller_store = SellerStore.objects.create(
            owner=seller,
            name='Northside Goods',
            description='Useful finds for everyday life.',
        )
        self.client.force_login(seller)
        editor = self.client.get(reverse('seller_dashboard'))
        self.assertContains(editor, 'multiple')
        self.assertContains(editor, 'Product photos (minimum 3)')
        response = self.client.post(reverse('seller_dashboard'), {
            'action': 'save_product',
            'name': 'Canvas Day Bag',
            'category': category.pk,
            'price': '420',
            'list_price': '480',
            'description': 'A roomy everyday bag for carrying books, a water bottle and daily essentials.',
            'offer_text': 'Introductory seller price this week.',
            'stock': '8',
            'low_stock_threshold': '2',
            'is_active': 'on',
            'photos': self.make_product_photos(),
        })

        self.assertRedirects(response, reverse('seller_dashboard'))
        product = Product.objects.get(name='Canvas Day Bag')
        self.assertTrue(product.image)
        self.assertEqual(product.gallery_images.count(), 2)
        self.assertEqual(product.seller_store, seller_store)
        self.assertTrue(product.is_active)

        storefront = self.client.get(reverse('store'))
        self.assertContains(storefront, 'Canvas Day Bag')
        self.assertContains(storefront, 'Accessories')
        self.assertContains(storefront, 'A roomy everyday bag for carrying books')
        self.assertContains(storefront, seller_store.name)
        details = self.client.get(reverse('product_detail', args=[product.pk]))
        self.assertEqual(details.content.decode().count('detail-gallery-thumb'), 2)
        self.assertContains(details, '420.00')
        self.assertContains(details, '480.00')
        self.assertContains(details, 'Introductory seller price this week.')
        self.assertContains(details, 'Accessories')

        updated_category = Category.objects.create(name='Travel Gear', slug='travel-gear')
        response = self.client.post(reverse('seller_dashboard'), {
            'action': 'save_product',
            'product_id': product.pk,
            'name': 'Canvas Weekender',
            'category': updated_category.pk,
            'price': '510',
            'list_price': '',
            'description': 'An updated roomy canvas weekender for packing clothes and essentials for short trips.',
            'offer_text': 'Updated product information from the seller.',
            'stock': '6',
            'low_stock_threshold': '2',
            'is_active': 'on',
        })
        self.assertRedirects(response, reverse('seller_dashboard'))
        product.refresh_from_db()
        self.assertEqual(product.name, 'Canvas Weekender')
        self.assertEqual(product.price, 510)
        self.assertEqual(product.category, updated_category)
        self.assertEqual(product.description, 'An updated roomy canvas weekender for packing clothes and essentials for short trips.')
        self.assertEqual(product.offer_text, 'Updated product information from the seller.')

        storefront = self.client.get(reverse('store'), {'category': 'travel-gear'})
        self.assertContains(storefront, 'Canvas Weekender')
        self.assertContains(storefront, '510.00')
        self.assertContains(storefront, 'An updated roomy canvas weekender')
        self.assertContains(storefront, 'Travel Gear')
        self.assertNotContains(storefront, 'Canvas Day Bag')

        other_store = SellerStore.objects.create(
            owner=User.objects.create_user(username='other-seller', password='seller-password'),
            name='Other Goods',
            description='Another shop.',
        )
        other_product = Product.objects.create(
            seller_store=other_store,
            name='Other listing',
            price=10,
        )
        response = self.client.post(reverse('seller_dashboard'), {
            'action': 'edit_product',
            'product_id': other_product.pk,
        })
        self.assertEqual(response.status_code, 404)

    def test_seller_product_listing_requires_three_photos(self):
        seller = User.objects.create_user(username='photo-seller', password='Password!123')
        SellerStore.objects.create(
            owner=seller,
            name='Photo Shop',
            description='A shop requiring clear product photos.',
        )
        category = Category.objects.create(name='Photo Accessories', slug='photo-accessories')
        self.client.force_login(seller)

        response = self.client.post(reverse('seller_dashboard'), {
            'action': 'save_product',
            'name': 'Photo Test Bag',
            'category': category.pk,
            'price': '49.99',
            'description': 'A sufficiently detailed product description for this photo upload test.',
            'stock': '5',
            'is_active': 'on',
            'photos': self.make_product_photos(2),
        })

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'A listing must have at least three product photos.')
        self.assertFalse(Product.objects.filter(name='Photo Test Bag').exists())

    def test_seller_listing_requires_useful_copy_and_finite_price(self):
        seller = User.objects.create_user(username='seller', password='seller-password')
        SellerStore.objects.create(
            owner=seller,
            name='Northside Goods',
            description='Useful finds for everyday life.',
        )
        self.client.force_login(seller)
        response = self.client.post(reverse('seller_dashboard'), {
            'action': 'save_product',
            'name': 'Incomplete listing',
            'price': 'NaN',
            'description': 'Too short.',
            'is_active': 'on',
        })

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Product.objects.filter(name='Incomplete listing').exists())
        self.assertContains(response, 'Enter a number.')
        self.assertContains(response, 'Ensure this value has at least 40 characters')
        self.assertContains(response, 'A listing must have at least three product photos.')

    def test_seller_vault_counts_only_its_paid_and_pending_lines(self):
        seller = User.objects.create_user(username='seller', password='seller-password')
        seller_store = SellerStore.objects.create(
            owner=seller,
            name='Northside Goods',
            description='Useful finds for everyday life.',
        )
        seller_product = Product.objects.create(
            seller_store=seller_store,
            name='Seller product',
            price=20,
        )
        Product.objects.create(name='Unowned product', price=100)
        paid_order = Order.objects.create(
            complete=True,
            status='Placed',
            payment_status='paid',
        )
        OrderItem.objects.create(
            order=paid_order,
            product=seller_product,
            quantity=2,
            unit_price=20,
        )
        pending_order = Order.objects.create(
            complete=True,
            status='Placed',
            payment_status='pending_review',
        )
        OrderItem.objects.create(
            order=pending_order,
            product=seller_product,
            quantity=1,
            unit_price=20,
        )
        cancelled_order = Order.objects.create(
            complete=True,
            status='Cancelled',
            payment_status='paid',
        )
        OrderItem.objects.create(
            order=cancelled_order,
            product=seller_product,
            quantity=1,
            unit_price=20,
        )

        self.client.force_login(seller)
        response = self.client.get(reverse('seller_dashboard'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['paid_sales'], 40)
        self.assertEqual(response.context['awaiting_payment'], 20)
        self.assertContains(response, 'Vault figures are order records')

    def test_inactive_seller_listing_is_hidden_from_storefront(self):
        seller = User.objects.create_user(username='seller', password='seller-password')
        seller_store = SellerStore.objects.create(
            owner=seller,
            name='Northside Goods',
            description='Useful finds for everyday life.',
        )
        product = Product.objects.create(
            seller_store=seller_store,
            name='Hidden product',
            price=20,
            is_active=False,
        )

        self.assertEqual(self.client.get(reverse('store')).status_code, 200)
        self.assertEqual(
            self.client.get(reverse('product_detail', args=[product.pk])).status_code,
            404,
        )
