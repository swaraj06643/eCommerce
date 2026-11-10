from django.db import migrations, models


PRODUCT_DESCRIPTIONS = {
    'headphones': 'An audio essential for your everyday listening setup.',
    'mount of olives book': 'An engaging title to add to your reading list.',
    'camera': 'Capture moments and make photography part of your everyday adventures.',
    'watch': 'A practical timepiece to finish your daily look.',
    'shoes': 'A versatile footwear pick for day-to-day outfits.',
    'plant': 'Add a touch of green to your home or workspace.',
    'air buds': 'A compact audio pick for music and podcasts on the move.',
    'cap': 'An easy-to-style accessory for everyday outfits.',
    'sun glass': 'A practical style accessory for sunny days.',
    'face wash': 'A skincare essential to add to your personal care routine.',
    'speaker': 'An audio accessory for enjoying music at home.',
    'mobile': 'A mobile device for your everyday communication and entertainment.',
    'shirt': 'An everyday wardrobe staple for easy, casual styling.',
    't-shirt': 'An everyday wardrobe staple for easy, casual styling.',
    'travel bag': 'A handy companion for organizing essentials on trips.',
    'sneakers': 'A versatile footwear pick for day-to-day outfits.',
    'yellow shirt': 'An everyday wardrobe staple for easy, casual styling.',
    'book': 'An engaging title to add to your reading list.',
}


def add_existing_product_descriptions(apps, schema_editor):
    Product = apps.get_model('store', 'Product')
    for product in Product.objects.filter(description=''):
        product.description = PRODUCT_DESCRIPTIONS.get(
            (product.name or '').lower(),
            'A useful pick for your everyday essentials.',
        )
        product.save(update_fields=['description'])


class Migration(migrations.Migration):

    dependencies = [
        ('store', '0002_product_image'),
    ]

    operations = [
        migrations.AddField(
            model_name='product',
            name='list_price',
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='product',
            name='description',
            field=models.TextField(blank=True, default=''),
        ),
        migrations.AddField(
            model_name='product',
            name='offer_text',
            field=models.CharField(blank=True, default='', max_length=255),
        ),
        migrations.RunPython(add_existing_product_descriptions, migrations.RunPython.noop),
    ]
