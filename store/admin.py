from django.contrib import admin
from django.db.models import F
from django.db import transaction
from django.utils import timezone

from .models import (
    Category,
    Customer,
    Order,
    OrderItem,
    Payment,
    Product,
    ProductCoupon,
    ProductVariant,
    SellerStore,
    ShippingAddress,
)
from .utils import release_reserved_inventory


class ProductVariantInline(admin.TabularInline):
    model = ProductVariant
    extra = 1


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
    list_display = ('name', 'seller_store', 'category', 'price', 'stock', 'stock_alert', 'is_active', 'digital')
    list_filter = ('seller_store', 'category', StockLevelFilter, 'is_active', 'digital')
    search_fields = ('name', 'description', 'variants__sku')
    fields = (
        'name', 'seller_store', 'category', 'price', 'list_price', 'cost_price', 'description',
        'offer_text', 'stock', 'low_stock_threshold', 'digital', 'is_active', 'image',
    )
    inlines = (ProductVariantInline,)

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
    readonly_fields = ('product', 'variant_name', 'quantity', 'unit_price', 'unit_cost')
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
        'coupon_code', 'coupon_discount',
    )
    inlines = (OrderItemInline,)
    fieldsets = (
        ('Order', {'fields': ('order_number', 'customer', 'status', 'date_order', 'delivered_at')}),
        ('Payment', {'fields': ('payment_method', 'payment_status', 'transaction_id')}),
        ('Shipping', {'fields': ('shipping_partner', 'tracking_number', 'tracking_url')}),
        ('Customer requests', {'fields': ('cancellation_reason', 'return_reason')}),
        ('Discount', {'fields': ('coupon_code', 'coupon_discount')}),
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
        if obj.status in ('Cancelled', 'Returned') and previous_status != obj.status:
            with transaction.atomic():
                release_reserved_inventory(Order.objects.select_for_update().get(pk=obj.pk))


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ('transaction_id', 'order', 'method', 'amount', 'status', 'created_at')
    list_filter = ('method', 'status', 'created_at')
    search_fields = ('transaction_id', 'gateway_payment_id', 'customer_reference', 'order__order_number')

    def save_model(self, request, obj, form, change):
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


admin.site.register(Customer)
admin.site.register(ShippingAddress)
admin.site.register(SellerStore)


@admin.register(ProductCoupon)
class ProductCouponAdmin(admin.ModelAdmin):
    list_display = ('code', 'product', 'seller_store', 'discount_percent', 'active', 'created_at')
    list_filter = ('active', 'seller_store')
    search_fields = ('code', 'product__name', 'seller_store__name')
