from django.contrib import admin, messages
from django.conf import settings
from django.db.models import F
from django.db import transaction
from django.utils import timezone
from django.urls import reverse

from .models import (
    Category,
    Customer,
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
    Refund,
    ReturnRequest,
    SellerStore,
    ShippingAddress,
)
from .notifications import queue_order_update
from .refunds import (
    RefundProviderError,
    confirm_refund_succeeded,
    process_return_refund,
    restock_return_items,
)
from .utils import release_reserved_inventory


class ProductVariantInline(admin.TabularInline):
    model = ProductVariant
    extra = 1


class ProductImageInline(admin.TabularInline):
    model = ProductImage
    extra = 0


class StockLevelFilter(admin.SimpleListFilter):
    title = 'stock level'
    parameter_name = 'stock_level'

    def lookups(self, request, model_admin):
        return (('low', 'Low stock'), ('untracked', 'Not tracked'))

    def queryset(self, request, queryset):
        if self.value() == 'low':
            return queryset.filter(
                stock__isnull=False,
                stock__lte=F('low_stock_threshold'),
            )
        if self.value() == 'untracked':
            return queryset.filter(stock__isnull=True)
        return queryset


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ('name', 'slug', 'sort_order', 'active')
    list_filter = ('active',)
    ordering = ('sort_order', 'name')
    prepopulated_fields = {'slug': ('name',)}
    search_fields = ('name',)


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ('name', 'seller_store', 'category', 'price', 'gst_rate', 'stock', 'stock_alert', 'is_active', 'digital')
    list_filter = ('seller_store', 'category', StockLevelFilter, 'is_active', 'digital')
    search_fields = ('name', 'description', 'variants__sku')
    fields = (
        'name', 'seller_store', 'category', 'price', 'list_price', 'gst_rate', 'cost_price', 'description',
        'offer_text', 'stock', 'low_stock_threshold', 'digital', 'is_active', 'image',
    )
    inlines = (ProductVariantInline, ProductImageInline)

    @admin.display(description='Stock alert', ordering='stock')
    def stock_alert(self, product):
        if product.stock is None:
            return 'Not tracked'
        if product.stock <= product.low_stock_threshold:
            return 'LOW STOCK'
        return 'In stock'


class OrderItemInline(admin.TabularInline):
    model = OrderItem
    extra = 0
    readonly_fields = (
        'product', 'variant_name', 'quantity', 'unit_price', 'unit_cost',
        'coupon_code', 'coupon_discount', 'gst_rate', 'gst_amount',
    )
    can_delete = False


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = (
        'order_number', 'customer', 'date_order', 'status', 'payment_method',
        'payment_status', 'shipping_partner', 'tracking_number',
    )
    list_filter = ('status', 'payment_method', 'payment_status', 'date_order')
    search_fields = ('order_number', 'customer__name', 'customer__email', 'tracking_number')
    readonly_fields = (
        'public_token', 'visitor_key', 'transaction_id', 'date_order', 'delivered_at',
        'coupon_code', 'coupon_discount', 'gst_amount', 'platform_fee',
        'platform_fee_gst', 'platform_fee_rate', 'delivery_fee', 'total_amount',
    )
    inlines = (OrderItemInline,)
    fieldsets = (
        ('Order', {'fields': ('order_number', 'customer', 'status', 'date_order', 'delivered_at')}),
        ('Payment', {'fields': ('payment_method', 'payment_status', 'transaction_id')}),
        ('Shipping', {'fields': ('shipping_partner', 'tracking_number', 'tracking_url')}),
        ('Customer requests', {'fields': ('cancellation_reason', 'return_reason')}),
        ('Discount and charges', {'fields': (
            'coupon_code', 'coupon_discount', 'gst_amount', 'platform_fee',
            'platform_fee_gst', 'platform_fee_rate', 'delivery_fee', 'total_amount',
        )}),
        ('Internal', {'fields': ('public_token', 'visitor_key', 'inventory_released')}),
    )

    def save_model(self, request, obj, form, change):
        previous_status = None
        if change:
            previous_order = Order.objects.get(pk=obj.pk)
            previous_status = previous_order.status
            obj.delivered_at = previous_order.delivered_at
        if obj.status == 'Delivered' and previous_status != 'Delivered':
            obj.delivered_at = timezone.now()
        super().save_model(request, obj, form, change)
        if change and previous_status != obj.status:
            queue_order_update(
                obj,
                f'order:{obj.pk}:admin-status:{timezone.now().strftime("%Y%m%d%H%M%S%f")}',
                obj.status,
                'Store staff updated your order status.',
                f'{settings.SITE_URL}{reverse("order_complete", args=[obj.public_token])}',
            )
        if obj.status in ('Cancelled', 'Returned') and previous_status != obj.status:
            with transaction.atomic():
                release_reserved_inventory(Order.objects.select_for_update().get(pk=obj.pk))


@admin.register(OrderFulfillment)
class OrderFulfillmentAdmin(admin.ModelAdmin):
    list_display = ('order', 'seller_store', 'status', 'shipping_partner', 'tracking_number', 'updated_at')
    list_filter = ('status', 'seller_store', 'updated_at')
    search_fields = ('order__order_number', 'seller_store__name', 'tracking_number')
    readonly_fields = ('updated_at', 'delivered_at')


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ('transaction_id', 'order', 'method', 'amount', 'status', 'created_at')
    list_filter = ('method', 'status', 'created_at')
    search_fields = ('transaction_id', 'gateway_payment_id', 'customer_reference', 'order__order_number')

    def save_model(self, request, obj, form, change):
        previous_status = Payment.objects.get(pk=obj.pk).status if change else None
        super().save_model(request, obj, form, change)
        with transaction.atomic():
            order = Order.objects.select_for_update().get(pk=obj.order_id)
            order.payment_status = obj.status
            if obj.status == 'paid' and order.status == 'Pending Payment':
                order.status = 'Placed'
            elif obj.status == 'failed' and order.status == 'Pending Payment':
                order.status = 'Cancelled'
                release_reserved_inventory(order)
            order.save(update_fields=['payment_status', 'status'])
            if change and previous_status != obj.status:
                queue_order_update(
                    order,
                    f'order:{order.pk}:admin-payment:{timezone.now().strftime("%Y%m%d%H%M%S%f")}',
                    f'Payment {obj.get_status_display().lower()}',
                    'Store staff updated your payment record.',
                    f'{settings.SITE_URL}{reverse("order_complete", args=[order.public_token])}',
                )


@admin.register(OrderEmailNotification)
class OrderEmailNotificationAdmin(admin.ModelAdmin):
    list_display = ('order', 'event_key', 'recipient', 'status', 'attempts', 'created_at', 'sent_at')
    list_filter = ('status', 'created_at')
    search_fields = ('event_key', 'recipient', 'order__order_number')
    readonly_fields = ('order', 'event_key', 'recipient', 'subject', 'body', 'attempts', 'last_error', 'created_at', 'sent_at')


@admin.register(ReturnRequest)
class ReturnRequestAdmin(admin.ModelAdmin):
    list_display = (
        'order', 'customer', 'request_type', 'status', 'reason',
        'inventory_restocked', 'requested_at', 'reviewed_by',
    )
    list_filter = ('request_type', 'status', 'inventory_restocked', 'requested_at')
    search_fields = ('order__order_number', 'customer__name', 'customer__email', 'reason')
    readonly_fields = (
        'order', 'customer', 'request_type', 'reason', 'reviewed_by',
        'reviewed_at', 'inventory_restocked', 'requested_at', 'updated_at',
    )
    actions = (
        'approve_requests', 'reject_requests', 'mark_returns_received',
        'mark_exchanges_complete',
    )

    @admin.action(description='Approve selected return/exchange requests')
    def approve_requests(self, request, queryset):
        count = 0
        for return_request in queryset.filter(status='requested').select_related('order'):
            return_request.status = 'approved'
            return_request.reviewed_by = request.user
            return_request.reviewed_at = timezone.now()
            return_request.save(update_fields=['status', 'reviewed_by', 'reviewed_at', 'updated_at'])
            queue_order_update(
                return_request.order,
                f'order:{return_request.order_id}:return:{return_request.pk}:approved',
                'Return approved',
                'Your return request is approved. Follow the return instructions from the store before sending the item back.',
                f'{settings.SITE_URL}{reverse("order_complete", args=[return_request.order.public_token])}',
            )
            count += 1
        self.message_user(request, f'Approved {count} return request(s).')

    @admin.action(description='Reject selected return/exchange requests')
    def reject_requests(self, request, queryset):
        count = 0
        for return_request in queryset.filter(status__in=('requested', 'approved')).select_related('order'):
            return_request.status = 'rejected'
            return_request.reviewed_by = request.user
            return_request.reviewed_at = timezone.now()
            return_request.save(update_fields=['status', 'reviewed_by', 'reviewed_at', 'updated_at'])
            Order.objects.filter(pk=return_request.order_id).update(status='Delivered')
            queue_order_update(
                return_request.order,
                f'order:{return_request.order_id}:return:{return_request.pk}:rejected',
                'Return request declined',
                'Your return request was reviewed. Contact support if you need help with this decision.',
                f'{settings.SITE_URL}{reverse("order_complete", args=[return_request.order.public_token])}',
            )
            count += 1
        self.message_user(request, f'Rejected {count} return request(s).')

    @admin.action(description='Confirm receipt and inspection of selected returns')
    def mark_returns_received(self, request, queryset):
        count = 0
        for return_request in queryset.filter(status='approved').select_related('order', 'customer'):
            with transaction.atomic():
                locked = ReturnRequest.objects.select_for_update().get(pk=return_request.pk)
                if locked.status != 'approved':
                    continue
                restock_return_items(locked.pk)
                locked.status = 'received'
                update_fields = ['status', 'updated_at']
                if locked.request_type == 'refund':
                    payment = Payment.objects.filter(order=locked.order).first()
                    if payment:
                        Refund.objects.get_or_create(
                            return_request=locked,
                            defaults={
                                'payment': payment,
                                'amount': str(payment.amount),
                            },
                        )
                        locked.status = 'refund_processing'
                        update_fields = ['status', 'updated_at']
                locked.save(update_fields=update_fields)
            queue_order_update(
                locked.order,
                f'order:{locked.order_id}:return:{locked.pk}:received',
                'Return received',
                'The returned item has been received and inspected. Refund processing, if applicable, will be shown separately.',
                f'{settings.SITE_URL}{reverse("order_complete", args=[locked.order.public_token])}',
            )
            count += 1
        self.message_user(request, f'Confirmed receipt for {count} return(s). Refunds are not marked complete until verified.')

    @admin.action(description='Mark selected exchange requests completed')
    def mark_exchanges_complete(self, request, queryset):
        count = 0
        for return_request in queryset.filter(
            request_type='exchange', status__in=('received', 'exchange_processing'),
        ).select_related('order'):
            return_request.status = 'exchanged'
            return_request.reviewed_by = request.user
            return_request.save(update_fields=['status', 'reviewed_by', 'updated_at'])
            Order.objects.filter(pk=return_request.order_id).update(status='Returned')
            queue_order_update(
                return_request.order,
                f'order:{return_request.order_id}:return:{return_request.pk}:exchanged',
                'Exchange completed',
                'Your exchange has been marked complete by the store.',
                f'{settings.SITE_URL}{reverse("order_complete", args=[return_request.order.public_token])}',
            )
            count += 1
        self.message_user(request, f'Marked {count} exchange(s) complete.')


@admin.register(Refund)
class RefundAdmin(admin.ModelAdmin):
    list_display = (
        'return_request', 'payment', 'amount', 'status', 'provider_reference',
        'attempts', 'updated_at',
    )
    list_filter = ('status', 'payment__method', 'created_at')
    search_fields = ('return_request__order__order_number', 'provider_reference', 'payment__transaction_id')
    readonly_fields = (
        'return_request', 'payment', 'amount', 'idempotency_key', 'attempts',
        'last_error', 'created_at', 'updated_at', 'completed_at',
    )
    actions = ('submit_or_retry_refunds', 'reconcile_refunds_as_succeeded')

    @admin.action(description='Submit/retry selected eligible refunds')
    def submit_or_retry_refunds(self, request, queryset):
        counts = {'processing': 0, 'succeeded': 0, 'failed': 0, 'manual_required': 0}
        for refund in queryset.exclude(status='succeeded'):
            try:
                result = process_return_refund(refund.pk)
            except RefundProviderError as error:
                self.message_user(request, f'{refund}: {error}', level=messages.ERROR)
                continue
            counts[result.status] = counts.get(result.status, 0) + 1
            if result.status == 'succeeded':
                self._notify_refund_change(result, 'Refund completed', 'Your refund has been confirmed by the payment provider.')
            elif result.status == 'processing':
                self._notify_refund_change(result, 'Refund processing', 'Your refund request has been submitted and is waiting for payment-provider confirmation.')
        summary = ', '.join(f'{status}: {count}' for status, count in counts.items() if count)
        self.message_user(request, f'Refund processing results — {summary or "no eligible refunds selected"}.')

    @admin.action(description='Reconcile selected refunds as succeeded (verify provider first)')
    def reconcile_refunds_as_succeeded(self, request, queryset):
        count = 0
        for refund in queryset.exclude(status='succeeded'):
            try:
                confirmed = confirm_refund_succeeded(refund.pk)
            except RefundProviderError as error:
                self.message_user(request, f'{refund}: {error}', level=messages.ERROR)
                continue
            self._notify_refund_change(confirmed, 'Refund completed', 'Store staff confirmed the refund with the payment provider.')
            count += 1
        self.message_user(request, f'Reconciled {count} refund(s). Only confirm after verifying the provider record.')

    def _notify_refund_change(self, refund, title, detail):
        order = refund.return_request.order
        queue_order_update(
            order,
            f'order:{order.pk}:refund:{refund.pk}:{refund.status}',
            title,
            detail,
            f'{settings.SITE_URL}{reverse("order_complete", args=[order.public_token])}',
        )


admin.site.register(Customer)
admin.site.register(ShippingAddress)
admin.site.register(SellerStore)


@admin.register(ProductReview)
class ProductReviewAdmin(admin.ModelAdmin):
    list_display = ('product', 'customer', 'rating', 'order', 'created_at')
    list_filter = ('rating', 'created_at')
    search_fields = ('product__name', 'customer__name', 'customer__email', 'title', 'body')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(ProductCoupon)
class ProductCouponAdmin(admin.ModelAdmin):
    list_display = ('code', 'product', 'seller_store', 'discount_percent', 'active', 'created_at')
    list_filter = ('active', 'seller_store')
    search_fields = ('code', 'product__name', 'seller_store__name')
