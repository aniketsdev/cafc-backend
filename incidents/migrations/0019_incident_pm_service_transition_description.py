from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("incidents", "0018_pm_review_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="incident",
            name="pm_service_transition_description",
            field=models.TextField(blank=True, null=True),
        ),
    ]
