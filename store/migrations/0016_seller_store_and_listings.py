from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


PRODUCT_DESCRIPTIONS = {
    'Headphones': (
        'Settle in with a favourite playlist, podcast or audiobook. These headphones '
        'are an easy everyday choice for listening at home, at work or while travelling.'
    ),
    'Mount of Olives Book': (
        'A thoughtful book to keep on your shelf or take along for a quiet reading '
        'break. Check the book cover and edition details for its specific contents.'
    ),
    'Camera': (
        'Keep a camera close for family gatherings, weekend outings and the small '
        'moments worth remembering. Check the product packaging for model-specific features.'
    ),
    'Watch': (
        'A practical wristwatch for keeping track of time through busy days, with a '
        'simple look that works for everyday wear.'
    ),
    'Shoes': (
        'A pair of everyday shoes for errands, commuting and casual plans. Check the '
        'available size options and fit details before ordering.'
    ),
    'Plant': (
        'Bring a little greenery to a desk, shelf or bright corner at home. Review the '
        'listing or packaging for the plant variety and care guidance.'
    ),
    'Air Buds': (
        'A compact option for listening to music or podcasts on the go. Check the '
        'packaging for model details and included accessories.'
    ),
    'Cap': (
        'An easy-to-wear cap for casual outfits and time outdoors. Check the listing '
        'for colour, size and material details.'
    ),
    'Sun Glass': (
        'A simple finishing touch for sunny-day outfits. Check the product details for '
        'lens and frame information before choosing.'
    ),
    'Face Wash': (
        'A face wash to use as part of a daily cleansing routine. Check the label for '
        'ingredients and directions, especially if your skin is sensitive.'
    ),
    'Speaker': (
        'Enjoy music, podcasts or radio around your home with this speaker. Check the '
        'product packaging for its connectivity and power details.'
    ),
    'Mobile': (
        'A mobile phone for everyday communication, browsing and entertainment. Review '
        'the product packaging for model, storage and network details.'
    ),
    'Shirt': (
        'A straightforward shirt for workdays, outings and easy everyday styling. Check '
        'the size options and product details for fit and fabric information.'
    ),
    'T-shirt': (
        'An easy everyday T-shirt for relaxed outfits, layering or time at home. Check '
        'the size options and listing details for fit and fabric information.'
    ),
    'Travel Bag': (
        'Keep travel essentials together for a weekend away or daily commute. Check the '
        'product details for capacity, compartments and dimensions.'
    ),
    'Sneakers': (
        'Casual sneakers for everyday outfits, errands and relaxed plans. Check the '
        'available size options and fit details before ordering.'
    ),
    'Yellow Shirt': (
        'A yellow shirt to bring a brighter note to everyday outfits. Check the size '
        'options and product details for fit and fabric information.'
    ),
    'Book': (
        'A good companion for a quiet evening, a commute or a growing home library. '
        "Check the cover and edition details for the book's specific contents."
    ),
}


def improve_existing_descriptions(apps, schema_editor):
    Product = apps.get_model('store', 'Product')
    for name, description in PRODUCT_DESCRIPTIONS.items():
        Product.objects.filter(name__iexact=name).update(description=description)


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('store', '0015_auto_20261110_1944'),
    ]

    operations = [
        migrations.CreateModel(
            name='SellerStore',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=120)),
                ('description', models.TextField()),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('owner', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='seller_store', to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.AddField(
            model_name='product',
            name='is_active',
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name='product',
            name='seller_store',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='products', to='store.sellerstore'),
        ),
        migrations.RunPython(improve_existing_descriptions, migrations.RunPython.noop),
    ]
