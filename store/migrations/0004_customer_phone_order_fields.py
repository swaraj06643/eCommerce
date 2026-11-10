from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('store', '0003_product_details'),
    ]

    operations = [
        migrations.AddField(
            model_name='customer',
            name='phone',
            field=models.CharField(blank=True, max_length=20, null=True),
        ),
        migrations.AddField(
            model_name='order',
            name='order_number',
            field=models.CharField(blank=True, max_length=40, null=True, unique=True),
        ),
        migrations.AddField(
            model_name='order',
            name='status',
            field=models.CharField(
                choices=[
                    ('Placed', 'Placed'),
                    ('Packed', 'Packed'),
                    ('Shipped', 'Shipped'),
                    ('Out for Delivery', 'Out for Delivery'),
                    ('Delivered', 'Delivered'),
                ],
                default='Placed',
                max_length=30,
            ),
        ),
    ]
