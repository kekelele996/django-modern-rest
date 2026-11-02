from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name='UserProfile',
            fields=[
                (
                    'id',
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name='ID',
                    ),
                ),
                ('name', models.CharField(max_length=100)),
                ('email', models.EmailField(max_length=254, unique=True)),
                ('avatar', models.JSONField(default=dict)),
                ('contact', models.JSONField(default=dict)),
                (
                    'role',
                    models.JSONField(default=dict, null=True),
                ),
                ('tags', models.JSONField(default=list)),
                ('attributes', models.JSONField(default=dict)),
                (
                    'internal_note',
                    models.TextField(blank=True, default=''),
                ),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
        ),
    ]
