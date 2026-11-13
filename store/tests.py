import hashlib
import json
import os
import re
import shutil
from datetime import timedelta
from decimal import Decimal
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
    OrderEmailNotification,
    OrderFulfillment,
    OrderItem,
    Payment,
    Product,
    ProductCoupon,
    ProductImage,
    ProductReview,
    ProductVariant,
    ProductVisit,
    Refund,
    ReturnRequest,
    SavedAddress,
    SellerStore,
    ShippingAddress,
    SponsoredCampaign,
    SuperCoinEntry,
    WishlistItem,
)
from .postal import PostalLookupError, validate_delivery_location
from .sms import send_phone_otp, verify_phone_otp
from .notifications import dispatch_order_email, queue_order_update
from .refunds import (
    RefundProviderError,
    confirm_refund_succeeded,
    process_return_refund,
    restock_return_items,
)
from .rewards import coin_balance, grant_order_rewards
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

    def test_product_page_exposes_canonical_and_product_structured_data(self):
        response = self.client.get(reverse('product_detail', args=[self.product.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'rel="canonical"')
        self.assertContains(response, 'property="og:type" content="product"')
        self.assertContains(response, 'application/ld+json')
        self.assertContains(response, '"@type":"Product"')
        self.assertContains(response, '"priceCurrency":"INR"')
        self.assertContains(response, 'https://schema.org/InStock')

    def test_robots_and_sitemap_expose_public_catalog_only(self):
        robots = self.client.get(reverse('robots_txt'))
        sitemap = self.client.get(reverse('sitemap'))

        self.assertEqual(robots.status_code, 200)
        self.assertContains(robots, 'Disallow: /checkout/')
        self.assertContains(robots, '/sitemap.xml')
        self.assertEqual(sitemap.status_code, 200)
        self.assertContains(sitemap, reverse('product_detail', args=[self.product.pk]))
        self.assertContains(sitemap, 'http://testserver/')

    def test_theme_controls_and_credit_are_present_in_both_portals(self):
        shop = self.client.get(reverse('store'))
        seller = self.client.get(reverse('seller_login'))

        self.assertContains(shop, 'data-theme-toggle')
        self.assertContains(seller, 'data-theme-toggle')
        self.assertContains(shop, 'theme.js?v=7')
        self.assertContains(seller, 'theme.js?v=7')
        self.assertContains(shop, 'Subhasish Rath and a heart ❤️ from Odisha')
        self.assertContains(seller, 'Subhasish Rath and a heart ❤️ from Odisha')

    def test_delivered_customer_can_submit_and_update_verified_product_review(self):
        user = User.objects.create_user(
            username='review-buyer',
            password='Password!123',
            first_name='Asha',
        )
        customer = Customer.objects.create(
            user=user,
            name='Asha Buyer',
            email='asha@example.com',
        )
        order = Order.objects.create(
            customer=customer,
            status='Delivered',
            delivered_at=timezone.now(),
        )
        OrderItem.objects.create(
            order=order,
            product=self.product,
            quantity=1,
            unit_price=self.product.price,
        )
        self.client.force_login(user)

        response = self.client.post(
            reverse('product_detail', args=[self.product.id]),
            {'rating': '5', 'title': 'Great sound', 'body': 'Comfortable and clear for daily listening.'},
        )

        self.assertRedirects(
            response,
            f'{reverse("product_detail", args=[self.product.id])}#reviews',
            fetch_redirect_response=False,
        )
        review = ProductReview.objects.get(product=self.product, customer=customer)
        self.assertEqual(review.order, order)
        self.assertEqual(review.rating, 5)

        response = self.client.get(reverse('product_detail', args=[self.product.id]))
        self.assertContains(response, '5.0')
        self.assertContains(response, 'Verified purchase')
        self.assertContains(response, 'Asha')

        self.client.post(
            reverse('product_detail', args=[self.product.id]),
            {'rating': '4', 'title': 'Still great', 'body': 'Comfortable and clear after using it every day.'},
        )
        self.assertEqual(ProductReview.objects.filter(product=self.product, customer=customer).count(), 1)
        review.refresh_from_db()
        self.assertEqual(review.rating, 4)
        self.assertEqual(review.title, 'Still great')

    def test_review_requires_a_delivered_order_for_the_product(self):
        user = User.objects.create_user(username='not-delivered', password='Password!123')
        customer = Customer.objects.create(user=user, name='Buyer', email='buyer@example.com')
        order = Order.objects.create(customer=customer, status='Placed')
        OrderItem.objects.create(
            order=order,
            product=self.product,
            quantity=1,
            unit_price=self.product.price,
        )
        self.client.force_login(user)

        response = self.client.post(
            reverse('product_detail', args=[self.product.id]),
            {'rating': '5', 'title': 'Nice item', 'body': 'This should not be accepted before delivery.'},
        )

        self.assertRedirects(
            response,
            f'{reverse("product_detail", args=[self.product.id])}#reviews',
            fetch_redirect_response=False,
        )
        self.assertFalse(ProductReview.objects.exists())

    def test_seller_cannot_review_own_listing(self):
        seller = User.objects.create_user(username='review-seller', password='Password!123')
        seller_store = SellerStore.objects.create(
            owner=seller,
            name='Seller Store',
            description='A seller store for review permissions.',
        )
        self.product.seller_store = seller_store
        self.product.save(update_fields=['seller_store'])
        customer = Customer.objects.create(user=seller, name='Seller', email='seller@example.com')
        order = Order.objects.create(
            customer=customer,
            status='Delivered',
            delivered_at=timezone.now(),
        )
        OrderItem.objects.create(order=order, product=self.product, quantity=1, unit_price=self.product.price)
        self.client.force_login(seller)

        response = self.client.get(reverse('product_detail', args=[self.product.id]))

        self.assertFalse(response.context['can_review'])
        self.assertContains(response, 'Seller accounts cannot review their own listings')

    def test_popular_sort_uses_sales_saves_and_product_visits(self):
        popular_product = Product.objects.create(
            name='Zebra Popular Item',
            price=100,
            description='A product that ranks highly from actual shopper activity.',
        )
        order_customer = Customer.objects.create(name='Recent Buyer', email='recent@example.com')
        order = Order.objects.create(
            customer=order_customer,
            status='Delivered',
            complete=True,
            delivered_at=timezone.now(),
        )
        OrderItem.objects.create(order=order, product=popular_product, quantity=3, unit_price=100)
        ProductVisit.objects.create(product=popular_product, visitor_key='popular-visitor')

        response = self.client.get(reverse('store'))

        listed_products = list(response.context['products'])
        self.assertEqual(listed_products[0], popular_product)
        self.assertEqual(listed_products[0].sold_units, 3)
        self.assertEqual(listed_products[0].product_visits, 1)

    @patch('store.notifications.send_mail', return_value=1)
    def test_order_emails_are_deduplicated_and_dispatch_after_commit(self, send_mail_mock):
        customer = Customer.objects.create(name='Email Buyer', email='email-buyer@example.com')
        order = Order.objects.create(customer=customer, complete=True, order_number='AV-EMAIL-1')

        with self.captureOnCommitCallbacks(execute=True):
            notification = queue_order_update(
                order,
                'order:email-test:placed',
                'Placed',
                'Your order was received.',
                'https://shop.example/order/',
            )

        notification.refresh_from_db()
        self.assertEqual(notification.status, 'sent')
        self.assertEqual(notification.attempts, 1)
        send_mail_mock.assert_called_once()

        with self.captureOnCommitCallbacks(execute=True):
            queue_order_update(
                order,
                'order:email-test:placed',
                'Placed',
                'Duplicate should not send.',
                'https://shop.example/order/',
            )
        send_mail_mock.assert_called_once()

    @patch('store.notifications.send_mail', side_effect=OSError('SMTP unavailable'))
    def test_failed_order_email_can_be_retried(self, send_mail_mock):
        customer = Customer.objects.create(name='Email Buyer', email='retry@example.com')
        order = Order.objects.create(customer=customer, complete=True, order_number='AV-EMAIL-2')

        with self.captureOnCommitCallbacks(execute=True):
            notification = queue_order_update(
                order,
                'order:email-test:retry',
                'Placed',
                'Your order was received.',
                'https://shop.example/order/',
            )
        notification.refresh_from_db()
        self.assertEqual(notification.status, 'failed')
        self.assertEqual(notification.attempts, 1)
        self.assertIn('SMTP unavailable', notification.last_error)

        with patch('store.notifications.send_mail', return_value=1):
            self.assertTrue(dispatch_order_email(notification.pk))
        notification.refresh_from_db()
        self.assertEqual(notification.status, 'sent')
        self.assertEqual(notification.attempts, 2)

    def test_brand_can_be_searched_and_filtered(self):
        branded_product = Product.objects.create(
            name='Everyday Trainer',
            brand='North Star',
            price=120,
            description='A comfortable trainer made for everyday movement.',
        )

        search_response = self.client.get(reverse('store'), {'q': 'North Star'})
        self.assertContains(search_response, branded_product.name)

        filter_response = self.client.get(reverse('store'), {
            'q': 'Everyday',
            'brand': 'north star',
        })
        self.assertEqual(filter_response.status_code, 200)
        self.assertEqual(list(filter_response.context['products']), [branded_product])
        self.assertEqual(filter_response.context['selected_brand'], 'North Star')
        self.assertContains(filter_response, 'Everyday Trainer')
        brand_response = self.client.get(reverse('store'), {'brand': 'North Star'})
        self.assertContains(brand_response, 'SHOP BY BRAND')
        self.assertContains(brand_response, 'North Star')

    def test_wishlist_is_account_private_and_can_be_toggled(self):
        user = User.objects.create_user(username='wishlist-owner', password='Password!123')
        self.client.force_login(user)

        response = self.client.post(
            reverse('wishlist_toggle'),
            {'product_id': str(self.product.id), 'next': reverse('product_detail', args=[self.product.id])},
        )

        self.assertRedirects(
            response,
            reverse('product_detail', args=[self.product.id]),
            fetch_redirect_response=False,
        )
        customer = Customer.objects.get(user=user)
        self.assertTrue(WishlistItem.objects.filter(customer=customer, product=self.product).exists())
        response = self.client.get(reverse('wishlist'))
        self.assertContains(response, self.product.name)

        other_user = User.objects.create_user(username='other-wishlist-user', password='Password!123')
        other_client = Client()
        other_client.force_login(other_user)
        self.assertNotContains(other_client.get(reverse('wishlist')), self.product.name)

        response = self.client.post(
            reverse('wishlist_toggle'),
            {'product_id': str(self.product.id), 'next': reverse('wishlist')},
        )
        self.assertRedirects(response, reverse('wishlist'), fetch_redirect_response=False)
        self.assertFalse(WishlistItem.objects.filter(customer=customer, product=self.product).exists())

    def test_wishlist_toggle_rejects_external_redirect_and_invalid_product_id(self):
        user = User.objects.create_user(username='safe-wishlist-user', password='Password!123')
        self.client.force_login(user)

        response = self.client.post(
            reverse('wishlist_toggle'),
            {'product_id': str(self.product.id), 'next': 'https://attacker.example/'},
        )
        self.assertRedirects(response, reverse('wishlist'), fetch_redirect_response=False)

        response = self.client.post(
            reverse('wishlist_toggle'),
            {'product_id': 'not-an-id', 'next': reverse('wishlist')},
        )
        self.assertEqual(response.status_code, 404)

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
        self.assertContains(response, 'avatar-store-v7')
        self.assertContains(response, 'order-status.js')
        self.assertContains(response, 'theme.js')
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

    def test_super_coins_are_awarded_once_only_after_delivery_and_payment(self):
        referrer = Customer.objects.create(name='Referrer', email='referrer@example.com')
        buyer_user = User.objects.create_user(
            username='referred-buyer', email='referred@example.com', password='Password!123',
        )
        customer = Customer.objects.create(
            user=buyer_user,
            name='Referred Buyer',
            email='referred@example.com',
            referred_by=referrer,
        )
        order = Order.objects.create(
            customer=customer,
            complete=True,
            status='Delivered',
            payment_status='pending',
            total_amount=1000.75,
        )
        payment = Payment.objects.create(
            order=order,
            method='cod',
            amount=1000.75,
            transaction_id='coins-delivered-order',
            status='pending',
        )
        self.assertEqual(grant_order_rewards(order), 0)
        payment.status = 'paid'
        payment.save(update_fields=['status', 'updated_at'])
        order.payment_status = 'paid'
        order.save(update_fields=['payment_status'])

        with override_settings(REFERRAL_REWARD_COINS=25):
            self.assertEqual(grant_order_rewards(order), 1000)
            self.assertEqual(grant_order_rewards(order), 0)
            second_order = Order.objects.create(
                customer=customer, complete=True, status='Delivered', payment_status='paid',
            )
            Payment.objects.create(
                order=second_order, method='cod', amount=150,
                transaction_id='coins-second-order', status='paid',
            )
            self.assertEqual(grant_order_rewards(second_order), 150)

        self.assertEqual(coin_balance(customer), 1150)
        self.assertEqual(coin_balance(referrer), 25)
        self.assertEqual(SuperCoinEntry.objects.filter(customer=customer, entry_type='earned').count(), 2)
        self.assertEqual(SuperCoinEntry.objects.filter(customer=referrer, entry_type='referral').count(), 1)

    def test_super_coin_redemption_is_server_calculated_and_restored_on_cancel(self):
        user = User.objects.create_user(
            username='coin-buyer', email='coins@example.com', password='Password!123',
        )
        customer = Customer.objects.create(
            user=user, name='Coin Buyer', email='coins@example.com', phone='+919876543210',
        )
        SuperCoinEntry.objects.create(
            customer=customer, delta=200, entry_type='earned', event_key='seed:coin-balance',
        )
        self.client.force_login(user)
        open_order = Order.objects.create(customer=customer, complete=False)
        OrderItem.objects.create(order=open_order, product=self.product, quantity=2, unit_price=179)
        response = self.client.post(reverse('checkout'), {
            'payment_method': 'cod',
            'coins_to_redeem': '200',
            'phone': '9876543210',
            'address': '12 Market Road',
            'city': 'Bhubaneswar',
            'state': 'Odisha',
            'zipcode': '751001',
        })

        self.assertEqual(response.status_code, 302)
        order = Order.objects.get(customer=customer, complete=True)
        payment = Payment.objects.get(order=order)
        self.assertEqual(order.coins_redeemed, 200)
        self.assertEqual(order.coin_discount, 20)
        self.assertEqual(order.get_total_amount, 346.45)
        self.assertEqual(payment.amount, order.get_total_amount)
        self.assertEqual(coin_balance(customer), 0)

        response = self.client.post(reverse('order_action', args=[order.pk]), {
            'action': 'cancel', 'reason': 'changed_mind',
        })
        self.assertEqual(response.status_code, 302)
        order.refresh_from_db()
        self.assertEqual(order.status, 'Cancelled')
        self.assertEqual(coin_balance(customer), 200)
        self.assertEqual(SuperCoinEntry.objects.filter(order=order, entry_type='restored').count(), 1)

    def test_checkout_rejects_non_hundred_coin_redemption(self):
        user = User.objects.create_user(
            username='invalid-coin-buyer', email='invalid-coins@example.com', password='Password!123',
        )
        customer = Customer.objects.create(user=user, name='Buyer', email=user.email)
        self.client.force_login(user)
        open_order = Order.objects.create(customer=customer, complete=False)
        OrderItem.objects.create(order=open_order, product=self.product, quantity=1, unit_price=179)
        response = self.client.post(reverse('checkout'), {
            'payment_method': 'cod', 'coins_to_redeem': '50',
            'address': '12 Market Road', 'city': 'Bhubaneswar',
            'state': 'Odisha', 'zipcode': '751001', 'phone': '9876543210',
        })
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, 'multiples of 100', status_code=400)
        self.assertFalse(Order.objects.filter(customer=customer, complete=True).exists())

    def test_registration_applies_referral_code_and_account_shows_rewards_tray(self):
        referrer_user = User.objects.create_user(
            username='referring-friend', email='friend@example.com', password='Password!123',
        )
        referrer = Customer.objects.create(
            user=referrer_user, name='Referrer', email='friend@example.com',
        )
        response = self.client.post(reverse('register'), {
            'username': 'invited-buyer',
            'password1': 'SafePassword!123',
            'password2': 'SafePassword!123',
            'first_name': 'Invited Buyer',
            'email': 'invited@example.com',
            'phone': '',
            'referral_code': referrer.referral_code,
        })
        self.assertRedirects(response, reverse('account'))
        invited = Customer.objects.get(user__username='invited-buyer')
        self.assertEqual(invited.referred_by, referrer)
        self.assertContains(self.client.get(reverse('account')), 'Super Coins')
        self.assertContains(self.client.get(reverse('account')), 'Refer a friend')
        self.assertContains(self.client.get(reverse('account')), invited.referral_code)

    def test_sponsored_campaign_is_labeled_and_counts_impressions_and_clicks(self):
        campaign = SponsoredCampaign.objects.create(
            product=self.product, title='Featured headphones', active=True,
        )
        response = self.client.get(reverse('store'))
        self.assertContains(response, 'Sponsored finds')
        self.assertContains(response, 'Featured headphones')
        campaign.refresh_from_db()
        self.assertEqual(campaign.impressions, 1)
        response = self.client.get(reverse('sponsored_campaign_click', args=[campaign.pk]))
        self.assertRedirects(response, reverse('product_detail', args=[self.product.pk]))
        campaign.refresh_from_db()
        self.assertEqual(campaign.clicks, 1)

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

    def test_signed_in_checkout_uses_only_the_customers_saved_address(self):
        user = User.objects.create_user(
            username='address-book-buyer',
            email='address-book@example.com',
            password='Password!123',
        )
        customer = Customer.objects.create(
            user=user,
            name='Address Book Buyer',
            email='address-book@example.com',
            phone='9876543210',
        )
        saved_address = SavedAddress.objects.create(
            customer=customer,
            label='Home',
            recipient_name='Address Book Buyer',
            phone='9876543210',
            address='12 Market Road',
            city='Bhubaneswar',
            state='Odisha',
            zipcode='751001',
            is_default=True,
        )
        open_order = Order.objects.create(customer=customer, complete=False)
        OrderItem.objects.create(
            order=open_order,
            product=self.product,
            quantity=1,
            unit_price=self.product.price,
        )
        self.client.force_login(user)

        checkout_page = self.client.get(reverse('checkout'))
        self.assertContains(checkout_page, 'Use a saved address')
        self.assertContains(checkout_page, 'Home — 12 Market Road, Bhubaneswar (default)')

        response = self.client.post(reverse('checkout'), {
            'payment_method': 'cod',
            'saved_address_id': saved_address.pk,
            'phone': '',
            'address': '999 Attacker Road',
            'city': 'Cuttack',
            'state': 'Bihar',
            'zipcode': '111111',
        })

        self.assertEqual(response.status_code, 302)
        placed_order = Order.objects.get(customer=customer, complete=True)
        shipping_address = ShippingAddress.objects.get(order=placed_order)
        self.assertEqual(shipping_address.address, saved_address.address)
        self.assertEqual(shipping_address.city, saved_address.city)
        self.assertEqual(shipping_address.state, saved_address.state)
        self.assertEqual(shipping_address.zipcode, saved_address.zipcode)

    def test_checkout_can_save_and_manage_an_account_address(self):
        user = User.objects.create_user(
            username='save-address-buyer',
            email='save-address@example.com',
            password='Password!123',
        )
        customer = Customer.objects.create(
            user=user,
            name='Save Address Buyer',
            email='save-address@example.com',
        )
        open_order = Order.objects.create(customer=customer, complete=False)
        OrderItem.objects.create(
            order=open_order,
            product=self.product,
            quantity=1,
            unit_price=self.product.price,
        )
        self.client.force_login(user)

        response = self.client.post(reverse('checkout'), {
            'payment_method': 'cod',
            'phone': '9876543210',
            'address': '12 Market Road',
            'city': 'Bhubaneswar',
            'state': 'Odisha',
            'zipcode': '751001',
            'save_address': 'on',
            'address_label': 'Office',
        })

        self.assertEqual(response.status_code, 302)
        saved_address = SavedAddress.objects.get(customer=customer)
        self.assertEqual(saved_address.label, 'Office')
        self.assertTrue(saved_address.is_default)
        self.assertContains(self.client.get(reverse('account')), 'Office')

        response = self.client.post(reverse('saved_address_action', args=[saved_address.pk]), {
            'action': 'delete',
        })
        self.assertRedirects(response, reverse('account'))
        self.assertFalse(SavedAddress.objects.filter(pk=saved_address.pk).exists())

    def test_checkout_rejects_another_customers_saved_address(self):
        buyer = User.objects.create_user(
            username='address-owner-check',
            email='owner-check@example.com',
            password='Password!123',
        )
        customer = Customer.objects.create(user=buyer, email='owner-check@example.com')
        other_user = User.objects.create_user(username='other-address-owner', password='Password!123')
        other_customer = Customer.objects.create(user=other_user, email='other@example.com')
        foreign_address = SavedAddress.objects.create(
            customer=other_customer,
            label='Private',
            phone='9876543210',
            address='12 Market Road',
            city='Bhubaneswar',
            state='Odisha',
            zipcode='751001',
        )
        open_order = Order.objects.create(customer=customer, complete=False)
        OrderItem.objects.create(order=open_order, product=self.product, quantity=1)
        self.client.force_login(buyer)

        response = self.client.post(reverse('checkout'), {
            'payment_method': 'cod',
            'saved_address_id': foreign_address.pk,
        })

        self.assertEqual(response.status_code, 400)
        self.assertContains(response, 'That saved address is not available on your account.', status_code=400)
        self.assertFalse(Order.objects.filter(customer=customer, complete=True).exists())

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
        response = self.client.post(reverse('seller_dashboard'), {
            **update,
            'confirm_delivered': '1',
        })
        self.assertEqual(response.status_code, 302)
        fulfillment.refresh_from_db()
        self.assertEqual(fulfillment.status, 'Placed')

        for status in ('Packed', 'Dispatched', 'Shipped', 'Out for Delivery'):
            response = self.client.post(reverse('seller_dashboard'), {
                **update,
                'status': status,
            })
            self.assertEqual(response.status_code, 302)

        dashboard = self.client.get(reverse('seller_dashboard'))
        self.assertContains(dashboard, 'Confirm successfully delivered')
        response = self.client.post(reverse('seller_dashboard'), {
            **update,
            'confirm_delivered': '1',
        })
        self.assertEqual(response.status_code, 302)
        self.assertContains(self.client.get(reverse('seller_dashboard')), 'can now leave a verified review')

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

        buyer = User.objects.create_user(
            username='delivered-review-buyer',
            email='buyer@example.com',
            password='Password!123',
        )
        order.customer.user = buyer
        order.customer.save(update_fields=['user'])
        self.client.force_login(buyer)
        review_page = self.client.get(reverse('product_detail', args=[self.product.pk]))
        self.assertContains(review_page, 'Review this product')
        review_response = self.client.post(reverse('product_detail', args=[self.product.pk]), {
            'rating': '5',
            'title': 'Delivered successfully',
            'body': 'The order arrived safely and the delivery update was accurate.',
        })
        self.assertEqual(review_response.status_code, 302)
        self.assertTrue(ProductReview.objects.filter(order=order, product=self.product).exists())

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


class ReturnRefundTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='return-owner', email='return-owner@example.com', password='Password!123',
        )
        self.customer = Customer.objects.create(
            user=self.user, name='Return Owner', email='return-owner@example.com',
        )
        self.product = Product.objects.create(name='Return Test Item', price=179, stock=0)

    def make_return(self, method='paypal', payment_status='paid'):
        order = Order.objects.create(
            customer=self.customer,
            complete=True,
            status='Return Requested',
            payment_method=method,
            payment_status=payment_status,
        )
        OrderItem.objects.create(
            order=order,
            product=self.product,
            quantity=2,
            unit_price=Decimal('179.00'),
            stock_source='product',
        )
        payment = Payment.objects.create(
            order=order,
            method=method,
            amount=179.0,
            status=payment_status,
            transaction_id=f'refund-{method}-{order.pk}',
            gateway_payment_id='PAYU-GATEWAY-1' if method == 'payu' else 'PAYPAL-ORDER-1',
            customer_reference='PAYPAL-CAPTURE-1' if method == 'paypal' else '',
        )
        return_request = ReturnRequest.objects.create(
            order=order,
            customer=self.customer,
            request_type='refund',
            status='refund_processing',
            reason='Item arrived damaged',
        )
        refund = Refund.objects.create(
            return_request=return_request,
            payment=payment,
            amount=Decimal('179.00'),
        )
        return order, payment, return_request, refund

    def test_full_refund_reverses_earned_coins_and_restores_spent_coins(self):
        order, payment, return_request, refund = self.make_return()
        order.coins_redeemed = 100
        order.coin_discount = 10
        order.save(update_fields=['coins_redeemed', 'coin_discount'])
        SuperCoinEntry.objects.create(
            customer=self.customer,
            order=order,
            delta=179,
            entry_type='earned',
            event_key=f'order:{order.pk}:coins:earned',
        )
        SuperCoinEntry.objects.create(
            customer=self.customer,
            order=order,
            delta=-100,
            entry_type='redeemed',
            event_key=f'order:{order.pk}:coins:redeemed',
        )

        confirmed = confirm_refund_succeeded(refund.pk, 'verified-refund-reference')

        order.refresh_from_db()
        self.assertEqual(confirmed.status, 'succeeded')
        self.assertEqual(order.status, 'Returned')
        self.assertEqual(coin_balance(self.customer), 0)
        self.assertTrue(SuperCoinEntry.objects.filter(
            event_key=f'order:{order.pk}:coins:earned:reversed', delta=-179,
        ).exists())
        self.assertTrue(SuperCoinEntry.objects.filter(
            event_key=f'order:{order.pk}:coins:restored', delta=100,
        ).exists())

    def test_return_action_is_owner_scoped_and_requires_delivery_window(self):
        recent_order = Order.objects.create(
            customer=self.customer,
            complete=True,
            status='Delivered',
            delivered_at=timezone.now(),
        )
        other_user = User.objects.create_user(username='not-return-owner', password='Password!123')
        self.client.force_login(other_user)
        response = self.client.post(reverse('order_action', args=[recent_order.pk]), {
            'action': 'return', 'request_type': 'refund', 'reason': 'Damaged',
        })
        self.assertEqual(response.status_code, 404)
        self.assertFalse(ReturnRequest.objects.filter(order=recent_order).exists())

        old_order = Order.objects.create(
            customer=self.customer,
            complete=True,
            status='Delivered',
            delivered_at=timezone.now() - timedelta(days=8),
        )
        self.client.force_login(self.user)
        response = self.client.post(reverse('order_action', args=[old_order.pk]), {
            'action': 'return', 'request_type': 'refund', 'reason': 'Damaged',
        })
        self.assertEqual(response.status_code, 302)
        old_order.refresh_from_db()
        self.assertEqual(old_order.status, 'Delivered')
        self.assertFalse(ReturnRequest.objects.filter(order=old_order).exists())

        response = self.client.post(reverse('order_action', args=[recent_order.pk]), {
            'action': 'return', 'request_type': 'refund', 'reason': 'Damaged',
        })
        self.assertEqual(response.status_code, 302)
        recent_order.refresh_from_db()
        self.assertEqual(recent_order.status, 'Return Requested')
        self.assertEqual(ReturnRequest.objects.get(order=recent_order).customer, self.customer)

    def test_return_stock_is_restocked_only_once(self):
        _, _, return_request, _ = self.make_return(method='cod', payment_status='pending')

        self.assertTrue(restock_return_items(return_request.pk))
        self.assertFalse(restock_return_items(return_request.pk))
        self.product.refresh_from_db()
        return_request.refresh_from_db()
        self.assertEqual(self.product.stock, 2)
        self.assertTrue(return_request.inventory_restocked)

    def test_admin_approves_and_receives_returns_before_creating_refund(self):
        order = Order.objects.create(
            customer=self.customer,
            complete=True,
            status='Delivered',
            delivered_at=timezone.now(),
            payment_method='paypal',
            payment_status='paid',
        )
        OrderItem.objects.create(
            order=order, product=self.product, quantity=1, unit_price=179,
            stock_source='product',
        )
        payment = Payment.objects.create(
            order=order,
            method='paypal',
            amount=179,
            status='paid',
            transaction_id='admin-return-payment',
            customer_reference='PAYPAL-CAPTURE-ADMIN',
        )
        return_request = ReturnRequest.objects.create(
            order=order,
            customer=self.customer,
            request_type='refund',
            status='requested',
            reason='Item arrived damaged',
        )
        staff = User.objects.create_user(
            username='return-admin', password='Password!123', is_staff=True, is_superuser=True,
        )
        self.client.force_login(staff)
        admin_url = reverse('admin:store_returnrequest_changelist')

        response = self.client.post(admin_url, {
            'action': 'approve_requests',
            '_selected_action': [return_request.pk],
        })
        self.assertEqual(response.status_code, 302)
        return_request.refresh_from_db()
        self.assertEqual(return_request.status, 'approved')
        self.assertEqual(return_request.reviewed_by, staff)

        response = self.client.post(admin_url, {
            'action': 'mark_returns_received',
            '_selected_action': [return_request.pk],
        })
        self.assertEqual(response.status_code, 302)
        return_request.refresh_from_db()
        self.product.refresh_from_db()
        refund = Refund.objects.get(return_request=return_request)
        self.assertEqual(return_request.status, 'refund_processing')
        self.assertTrue(return_request.inventory_restocked)
        self.assertEqual(self.product.stock, 1)
        self.assertEqual(refund.payment, payment)
        self.assertEqual(refund.status, 'pending')

    @override_settings(
        PAYPAL_CLIENT_ID='paypal-client',
        PAYPAL_CLIENT_SECRET='paypal-secret',
        PAYPAL_API_BASE='https://api-m.sandbox.paypal.com',
    )
    def test_paypal_completed_refund_updates_payment_and_return(self):
        order, payment, return_request, refund = self.make_return()

        with patch('store.refunds._paypal_request', return_value={
            'id': 'PAYPAL-REFUND-1', 'status': 'COMPLETED',
        }) as provider:
            result = process_return_refund(refund.pk)

        provider.assert_called_once()
        self.assertEqual(provider.call_args.args[1], '/v2/payments/captures/PAYPAL-CAPTURE-1/refund')
        self.assertEqual(provider.call_args.kwargs['request_id'], str(refund.idempotency_key))
        payment.refresh_from_db()
        order.refresh_from_db()
        return_request.refresh_from_db()
        self.assertEqual(result.status, 'succeeded')
        self.assertEqual(result.provider_reference, 'PAYPAL-REFUND-1')
        self.assertEqual(payment.status, 'refunded')
        self.assertEqual(order.status, 'Returned')
        self.assertEqual(return_request.status, 'refunded')

    @override_settings(
        PAYPAL_CLIENT_ID='paypal-client',
        PAYPAL_CLIENT_SECRET='paypal-secret',
    )
    def test_paypal_pending_refund_stays_processing_for_reconciliation(self):
        _, _, return_request, refund = self.make_return()
        with patch('store.refunds._paypal_request', return_value={
            'id': 'PAYPAL-REFUND-PENDING', 'status': 'PENDING',
        }):
            result = process_return_refund(refund.pk)

        self.assertEqual(result.status, 'processing')
        self.assertEqual(result.provider_reference, 'PAYPAL-REFUND-PENDING')
        return_request.refresh_from_db()
        self.assertEqual(return_request.status, 'refund_processing')

    @override_settings(
        PAYPAL_CLIENT_ID='paypal-client',
        PAYPAL_CLIENT_SECRET='paypal-secret',
    )
    def test_paypal_refund_provider_failure_is_retryable(self):
        _, _, _, refund = self.make_return()
        with patch(
            'store.refunds._paypal_request',
            side_effect=RefundProviderError('Provider temporarily unavailable.'),
        ):
            result = process_return_refund(refund.pk)

        self.assertEqual(result.status, 'failed')
        self.assertIn('temporarily unavailable', result.last_error)
        self.assertEqual(result.attempts, 1)

        with patch('store.refunds._paypal_request', return_value={
            'id': 'PAYPAL-REFUND-FAILED', 'status': 'FAILED',
        }):
            result = process_return_refund(refund.pk)
        self.assertEqual(result.status, 'failed')
        self.assertIn('did not confirm', result.last_error)
        self.assertEqual(result.attempts, 2)

    @override_settings(
        PAYU_KEY='merchant-key',
        PAYU_SALT='merchant-salt',
        PAYU_REFUND_API_URL='https://test.payu.in/merchant/postservice.php?form=2',
    )
    def test_payu_accepted_refund_remains_processing_until_reconciled(self):
        _, _, return_request, refund = self.make_return(method='payu')
        response = mock_patch('store.refunds.urlopen')
        urlopen = response.start()
        self.addCleanup(response.stop)
        urlopen.return_value.__enter__.return_value.read.return_value = b'{"status":1,"request_id":"PAYU-REFUND-1"}'

        result = process_return_refund(refund.pk)

        self.assertEqual(result.status, 'processing')
        self.assertEqual(result.provider_reference, 'PAYU-REFUND-1')
        self.assertIn('queued', result.last_error.lower())
        return_request.refresh_from_db()
        self.assertEqual(return_request.status, 'refund_processing')
        self.assertEqual(urlopen.call_args.args[0].full_url, 'https://test.payu.in/merchant/postservice.php?form=2')

    def test_manual_payment_refund_requires_staff_reconciliation(self):
        _, _, _, refund = self.make_return(method='upi_manual')

        result = process_return_refund(refund.pk)

        self.assertEqual(result.status, 'manual_required')
        self.assertIn('manual refund', result.last_error)
