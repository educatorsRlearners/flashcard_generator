from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("submissions", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="Batch",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "ordering": ["-created_at", "-id"],
            },
        ),
        migrations.AddField(
            model_name="submittedurl",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending", "Pending"),
                    ("ok", "OK"),
                    ("failed", "Failed"),
                ],
                default="pending",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="submittedurl",
            name="failure_reason",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="submittedurl",
            name="batch",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="urls",
                to="submissions.batch",
            ),
        ),
        migrations.AlterField(
            model_name="submittedurl",
            name="batch",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="urls",
                to="submissions.batch",
            ),
        ),
    ]
