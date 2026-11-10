from django.db import migrations


CATEGORY_PRODUCTS = {
    'electronics': ('headphones', 'camera', 'watch', 'air buds', 'speaker', 'mobile'),
    'fashion': ('shoes', 'sneakers', 'cap', 'sun glass', 'shirt', 't-shirt', 'yellow shirt'),
    'home-living': ('plant',),
    'books': ('book', 'mount of olives book'),
    'beauty': ('face wash',),
    'travel': ('travel bag',),
}


def assign_categories(apps, schema_editor):
    Category = apps.get_model('store', 'Category')
    Product = apps.get_model('store', 'Product')
    categories = {category.slug: category for category in Category.objects.all()}
    for slug, product_names in CATEGORY_PRODUCTS.items():
        category = categories.get(slug)
        if category is None:
            continue
        for product_name in product_names:
            Product.objects.filter(name__iexact=product_name).update(category_id=category.pk)


class Migration(migrations.Migration):

    dependencies = [
        ('store', '0010_order_delivered_at'),
    ]

    operations = [
        migrations.RunPython(assign_categories, migrations.RunPython.noop),
    ]
