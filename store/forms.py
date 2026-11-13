import math
from decimal import Decimal

from django import forms
from django.core.validators import MinLengthValidator, MinValueValidator, MaxValueValidator, RegexValidator

from .models import Category, Product, ProductCoupon, SellerStore


class SellerStoreForm(forms.ModelForm):
    class Meta:
        model = SellerStore
        fields = ('name', 'description')
        widgets = {
            'name': forms.TextInput(attrs={'placeholder': 'Your store name'}),
            'description': forms.Textarea(attrs={
                'rows': 3,
                'placeholder': 'Tell shoppers what your store offers.',
            }),
        }


class MultipleImageInput(forms.ClearableFileInput):
    allow_multiple_selected = True

    def __init__(self, attrs=None):
        attrs = dict(attrs or {})
        attrs['multiple'] = True
        super().__init__(attrs)

    def value_from_datadict(self, data, files, name):
        return files.getlist(name) if hasattr(files, 'getlist') else files.get(name)


class MultipleImageField(forms.ImageField):
    widget = MultipleImageInput

    def clean(self, data, initial=None):
        if not data:
            return []
        files = data if isinstance(data, (list, tuple)) else [data]
        clean_image = super().clean
        return [clean_image(image, initial) for image in files]


class SellerProductForm(forms.ModelForm):
    photos = MultipleImageField(
        label='Product photos (minimum 3)',
        required=False,
        help_text='Upload clear product photos. Existing photos count toward the minimum of three.',
    )

    class Meta:
        model = Product
        fields = (
            'name', 'category', 'price', 'list_price', 'gst_rate', 'description', 'offer_text',
            'stock', 'low_stock_threshold', 'digital', 'is_active',
        )
        widgets = {
            'gst_rate': forms.NumberInput(attrs={'min': '0', 'max': '100', 'step': '0.01'}),
            'description': forms.Textarea(attrs={
                'rows': 5,
                'placeholder': 'Describe what it is, what it is made for, and useful details shoppers should know.',
            }),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['category'].queryset = Category.objects.filter(active=True)
        self.fields['category'].required = True
        self.fields['price'].validators.append(MinValueValidator(0.01))
        self.fields['list_price'].validators.append(MinValueValidator(0.01))
        self.fields['list_price'].help_text = (
            'Optional original price. The sale percentage is calculated from this '
            'and your selling price.'
        )
        self.fields['gst_rate'].required = False
        self.fields['gst_rate'].help_text = (
            'Choose the GST rate applicable to this product; verify the correct rate '
            'for your product before publishing.'
        )
        if not self.is_bound:
            self.fields['low_stock_threshold'].initial = 5
        self.fields['name'].required = True
        self.fields['description'].required = True
        self.fields['description'].min_length = 40
        self.fields['description'].validators.append(MinLengthValidator(40))
        self.fields['description'].widget.attrs['minlength'] = '40'
        self.fields['description'].help_text = (
            'Use clear, accurate details. Do not include claims or specifications '
            'you cannot verify.'
        )

    def clean_gst_rate(self):
        return self.cleaned_data.get('gst_rate') or Decimal('0.00')

    def clean(self):
        cleaned_data = super().clean()
        price = cleaned_data.get('price')
        list_price = cleaned_data.get('list_price')
        if price is not None and list_price is not None and list_price < price:
            self.add_error('list_price', 'List price must be at least the selling price.')
        if price is not None and not math.isfinite(price):
            self.add_error('price', 'Enter a finite selling price.')
        if list_price is not None and not math.isfinite(list_price):
            self.add_error('list_price', 'Enter a finite list price.')

        existing_photo_count = self.instance.gallery_images.count() if self.instance.pk else 0
        if self.instance.pk and self.instance.image:
            existing_photo_count += 1
        if existing_photo_count + len(cleaned_data.get('photos') or []) < 3:
            self.add_error('photos', 'A listing must have at least three product photos.')
        return cleaned_data


class ProductCouponForm(forms.ModelForm):
    class Meta:
        model = ProductCoupon
        fields = ('code', 'discount_percent')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['code'].validators.append(RegexValidator(
            regex=r'^[A-Z0-9][A-Z0-9_-]{2,23}$',
            message='Use 3–24 letters, numbers, hyphens or underscores.',
        ))
        self.fields['discount_percent'].validators.extend([
            MinValueValidator(0.01),
            MaxValueValidator(90),
        ])
        self.fields['discount_percent'].help_text = 'Extra discount on this product’s current selling price (up to 90%).'

    def clean_code(self):
        return self.cleaned_data['code'].strip().upper()
