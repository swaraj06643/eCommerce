from django.db import models
from django.contrib.auth.models import User
import uuid


class Category(models.Model):
    name = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(max_length=110, unique=True)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ('name',)
        verbose_name_plural = 'categories'

    def __str__(self):
        return self.name


class Customer(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, null=True, blank=True)
    name = models.CharField(max_length=200, null=True)
    email = models.CharField(max_length=200, null=True)
    email_verified = models.BooleanField(default=False)
    phone = models.CharField(max_length=20, null=True, blank=True)
    phone_e164 = models.CharField(max_length=16, blank=True, default='', db_index=True)

    def __str__(self):
        return self.name or self.email or str(self.id)

class Product(models.Model):
    name = models.CharField(max_length=200, null=True)
    price = models.FloatField(max_length=200, null=True)
    list_price = models.FloatField(null=True, blank=True)
    cost_price = models.FloatField(null=True, blank=True)
    description = models.TextField(blank=True, default='')
    offer_text = models.CharField(max_length=255, blank=True, default='')
    category = models.ForeignKey(Category, on_delete=models.SET_NULL, null=True, blank=True)
    stock = models.PositiveIntegerField(null=True, blank=True)
    low_stock_threshold = models.PositiveIntegerField(default=5)
    digital = models.BooleanField(default=False, null=True, blank=True)
    image = models.ImageField(null=True, blank=True)

    def __str__(self):
        return self.name

    @property
    def has_variants(self):
        return any(variant.active for variant in self.variants.all())

    @property
    def imageURL(self):
        try:
            url = self.image.url
        except:
            url = ''
        return url


class ProductVariant(models.Model):
    product = models.ForeignKey(Product, related_name='variants', on_delete=models.CASCADE)
    name = models.CharField(max_length=60, help_text='For example: Size or Color')
    value = models.CharField(max_length=100, help_text='For example: Large or Blue')
    sku = models.CharField(max_length=80, blank=True, default='')
    price_adjustment = models.FloatField(default=0)
    stock = models.PositiveIntegerField(null=True, blank=True)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ('name', 'value')
        unique_together = ('product', 'name', 'value')

    def __str__(self):
        return f'{self.product.name} - {self.name}: {self.value}'

    @property
    def display_value(self):
        return f'{self.name}: {self.value}'


class Order(models.Model):
    STATUS_CHOICES = [
        ('Pending Payment', 'Pending Payment'),
        ('Placed', 'Placed'),
        ('Packed', 'Packed'),
        ('Shipped', 'Shipped'),
        ('Out for Delivery', 'Out for Delivery'),
        ('Delivered', 'Delivered'),
        ('Cancelled', 'Cancelled'),
        ('Return Requested', 'Return Requested'),
        ('Returned', 'Returned'),
    ]
    PAYMENT_METHOD_CHOICES = [
        ('payu', 'PayU'),
        ('upi_manual', 'Manual UPI'),
        ('cod', 'Cash on delivery'),
    ]
    PAYMENT_STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('pending_review', 'Pending verification'),
        ('paid', 'Paid'),
        ('failed', 'Failed'),
        ('refunded', 'Refunded'),
    ]

    customer = models.ForeignKey(Customer, on_delete=models.SET_NULL, null=True, blank=True)
    date_order = models.DateTimeField(auto_now_add=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    complete = models.BooleanField(default=False, null=True, blank=True)
    transaction_id = models.CharField(max_length=200, null=True)
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default='Placed')
    order_number = models.CharField(max_length=40, unique=True, null=True, blank=True)
    payment_method = models.CharField(max_length=20, choices=PAYMENT_METHOD_CHOICES, blank=True)
    payment_status = models.CharField(max_length=20, choices=PAYMENT_STATUS_CHOICES, default='pending')
    public_token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    tracking_number = models.CharField(max_length=100, blank=True)
    tracking_url = models.URLField(blank=True)
    shipping_partner = models.CharField(max_length=100, blank=True)
    cancellation_reason = models.TextField(blank=True)
    return_reason = models.TextField(blank=True)
    visitor_key = models.CharField(max_length=40, blank=True)
    inventory_released = models.BooleanField(default=False)

    def __str__(self):
        return str(self.order_number or self.id)

    @property
    def shipping(self):
        shipping = False
        orderitems = self.orderitem_set.all()
        for i in orderitems:
            if i.product.digital == False:
                shipping = True
        return shipping


    @property
    def get_cart_total(self):
        orderitems = self.orderitem_set.all()
        total = sum([item.get_total for item in orderitems])
        return total

    @property
    def get_cart_items(self):
        orderitems = self.orderitem_set.all()
        total = sum([item.quantity for item in orderitems])
        return total

class OrderItem(models.Model):
    product = models.ForeignKey(Product, on_delete=models.SET_NULL, null=True)
    variant = models.ForeignKey(ProductVariant, on_delete=models.SET_NULL, null=True, blank=True)
    order = models.ForeignKey(Order, on_delete=models.SET_NULL, null=True)
    quantity = models.IntegerField(default=0, null=True, blank=True)
    unit_price = models.FloatField(null=True, blank=True)
    unit_cost = models.FloatField(null=True, blank=True)
    variant_name = models.CharField(max_length=160, blank=True)
    stock_source = models.CharField(max_length=12, default='untracked')
    date_added = models.DateTimeField(auto_now_add=True)

    @property
    def get_total(self):
        price = self.unit_price if self.unit_price is not None else self.product.price
        total = price * self.quantity
        return total


class Payment(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('pending_review', 'Pending verification'),
        ('paid', 'Paid'),
        ('failed', 'Failed'),
        ('refunded', 'Refunded'),
    ]
    order = models.OneToOneField(Order, related_name='payment', on_delete=models.CASCADE)
    method = models.CharField(max_length=20, choices=Order.PAYMENT_METHOD_CHOICES)
    amount = models.FloatField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    transaction_id = models.CharField(max_length=100, unique=True)
    gateway_payment_id = models.CharField(max_length=120, blank=True)
    customer_reference = models.CharField(max_length=120, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'{self.transaction_id} ({self.status})'


class ProductVisit(models.Model):
    product = models.ForeignKey(Product, on_delete=models.CASCADE)
    visitor_key = models.CharField(max_length=40)
    first_seen = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('product', 'visitor_key')


class EmailVerification(models.Model):
    email = models.EmailField()
    user = models.ForeignKey(User, on_delete=models.CASCADE, null=True, blank=True)
    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    verified_at = models.DateTimeField(null=True, blank=True)
    otp_hash = models.CharField(max_length=128, blank=True, default='')
    otp_attempts = models.PositiveSmallIntegerField(default=0)

    def __str__(self):
        return f'{self.email} ({self.expires_at:%Y-%m-%d})'


class PhoneOTPRequest(models.Model):
    phone_e164 = models.CharField(max_length=16, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ('-created_at',)


class ShippingAddress(models.Model):
    customer = models.ForeignKey(Customer, on_delete=models.SET_NULL, null=True, blank=True)
    order = models.ForeignKey(Order, on_delete=models.SET_NULL, null=True)
    address = models.CharField(max_length=200, null=True)
    city = models.CharField(max_length=200, null=True)
    state = models.CharField(max_length=200, null=True)
    zipcode = models.CharField(max_length=200, null=True)
    date_added = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.address