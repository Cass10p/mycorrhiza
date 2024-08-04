# Generated by Django 5.0.1 on 2024-08-03 02:52

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('collector', '0027_site_csv_types'),
    ]

    operations = [
        migrations.AddField(
            model_name='profile',
            name='can_merge',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='profile',
            name='expiration',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
