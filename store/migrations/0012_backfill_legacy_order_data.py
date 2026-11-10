from django.db import migrations, models


def backfill_legacy_order_data(apps, schema_editor):
    Order = apps.get_model('store', 'Order')
    OrderItem = apps.get_model('store', 'OrderItem')
    Order.objects.filter(
        status='Delivered',
        delivered_at__isnull=True,
    ).update(delivered_at=models.F('date_order'))
    for item in OrderItem.objects.filter(unit_price__isnull=True).select_related('product').iterator():
        if item.product_id and item.product.price is not None:
            OrderItem.objects.filter(pk=item.pk).update(unit_price=item.product.price)


class Migration(migrations.Migration):

    dependencies = [
        ('store', '0011_assign_existing_product_categories'),
    ]

    operations = [
        migrations.RunPython(backfill_legacy_order_data, migrations.RunPython.noop),
    ]
