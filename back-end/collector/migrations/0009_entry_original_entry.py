# Generated by Django 4.2.7 on 2023-12-30 08:37

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('collector', '0008_alter_exclusion_user_spreadsheetupload_profile'),
    ]

    operations = [
        migrations.AddField(
            model_name='entry',
            name='original_entry',
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='translations', to='collector.entry'),
        ),
    ]
