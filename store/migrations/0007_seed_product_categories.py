from django.db import migrations


CATEGORY_PRODUCTS = {
    'electronics': ('headphones', 'camera', 'watch', 'air buds', 'speaker', 'mobile'),
    'fashion': ('shoes', 'sneakers', 'cap', 'sun glass', 'shirt', 't-shirt', 'yellow shirt'),
    'home & living': ('plant',),
    'books': ('book', 'mount of olives book'),
    'beauty': ('face wash',),
    'travel': ('travel bag',),
}


def seed_categories(apps, schema_editor):
    Category = apps.get_model('store', 'Category')
    Product = apps.get_model('store', 'Product')
    for name, product_names in CATEGORY_PRODUCTS.items():
        category, _ = Category.objects.get_or_create(
            slug=name.replace(' & ', '-').replace(' ', '-'),
            defaults={'name': name.title(), 'active': True},
        )
        for product_name in product_names:
            Product.objects.filter(name__iexact=product_name).update(category_id=category.pk)


class Migration(migrations.Migration):

    dependencies = [
        ('store', '0006_auto_20261110_1842'),
    ]

    operations = [
        migrations.RunPython(seed_categories, migrations.RunPython.noop),
    ]
