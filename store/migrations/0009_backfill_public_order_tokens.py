import uuid

from django.db import migrations, models


def backfill_public_tokens(apps, schema_editor):
    Order = apps.get_model('store', 'Order')
    for order in Order.objects.filter(public_token__isnull=True).iterator():
        Order.objects.filter(pk=order.pk).update(public_token=uuid.uuid4())


class Migration(migrations.Migration):

    dependencies = [
        ('store', '0008_alter_productvariant_sku'),
    ]

    operations = [
        migrations.RunPython(backfill_public_tokens, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='order',
            name='public_token',
            field=models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
        ),
    ]
