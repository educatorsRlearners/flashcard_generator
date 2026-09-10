from django.db import migrations


def backfill(apps, schema_editor):
    """Create one BatchRequest per existing SubmittedURL, linking it to its
    originating batch, so request history is complete from day one."""
    SubmittedURL = apps.get_model("submissions", "SubmittedURL")
    BatchRequest = apps.get_model("submissions", "BatchRequest")
    for submitted_url in SubmittedURL.objects.all().iterator():
        BatchRequest.objects.get_or_create(
            batch_id=submitted_url.batch_id, submitted_url=submitted_url
        )


def unbackfill(apps, schema_editor):
    """Reverse: drop the backfilled links. All BatchRequest rows are removed;
    0004 recreates an empty table on re-apply."""
    BatchRequest = apps.get_model("submissions", "BatchRequest")
    BatchRequest.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ("submissions", "0004_batchrequest"),
    ]

    operations = [
        migrations.RunPython(backfill, unbackfill),
    ]
