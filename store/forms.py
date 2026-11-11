import math

from django import forms
from django.core.validators import MinLengthValidator, MinValueValidator

from .models import Category, Product, SellerStore


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


class SellerProductForm(forms.ModelForm):
    class Meta:
        model = Product
        fields = (
            'name', 'category', 'price', 'list_price', 'description', 'offer_text',
            'stock', 'low_stock_threshold', 'digital', 'is_active', 'image',
        )
        widgets = {
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
        return cleaned_data
