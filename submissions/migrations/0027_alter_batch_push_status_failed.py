# Generated for issue #147 - add Batch.PushStatus.FAILED choice.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('submissions', '0026_batch_push_deck_name_batch_push_failed_count_and_more'),
    ]

    operations = [
        migrations.AlterField(
            model_name='batch',
            name='push_status',
            field=models.CharField(blank=True, choices=[('pending', 'Pending'), ('done', 'Done'), ('unreachable', 'Unreachable'), ('failed', 'Failed')], default='', max_length=16),
        ),
    ]
