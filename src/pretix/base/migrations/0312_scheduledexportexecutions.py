# Generated for scheduled export executions

import django.core.serializers.json
import django.db.models.deletion
from django.db import migrations, models

import pretix.base.models.base


class Migration(migrations.Migration):

    dependencies = [
        ('pretixbase', '0311_fix_unshredded_invoices'),
    ]

    operations = [
        migrations.CreateModel(
            name='ScheduledEventExportExecution',
            fields=[
                ('id', models.BigAutoField(primary_key=True, serialize=False)),
                ('status', models.CharField(
                    choices=[
                        ('waiting', 'waiting'),
                        ('rendering', 'rendering'),
                        ('file_ready', 'file ready'),
                        ('mail_queued', 'email queued'),
                        ('failed', 'failed'),
                        ('completed', 'completed'),
                    ],
                    default='waiting',
                    max_length=20,
                )),
                ('scheduled_for', models.DateTimeField(blank=True, null=True)),
                ('manual', models.BooleanField(default=False)),
                ('export_identifier', models.CharField(max_length=190)),
                ('export_form_data', models.JSONField(
                    default=dict,
                    encoder=django.core.serializers.json.DjangoJSONEncoder,
                )),
                ('locale', models.CharField(max_length=250)),
                ('timezone_name', models.CharField(max_length=100)),
                ('mail_additional_recipients', models.TextField(blank=True)),
                ('mail_additional_recipients_cc', models.TextField(blank=True)),
                ('mail_additional_recipients_bcc', models.TextField(blank=True)),
                ('mail_subject', models.CharField(max_length=250)),
                ('mail_template', models.TextField()),
                ('error_code', models.CharField(
                    blank=True,
                    choices=[
                        ('permission', 'Permission denied.'),
                        ('config', 'Invalid export configuration.'),
                        ('empty', 'Your export did not contain any data.'),
                        ('internal', 'Internal error'),
                        ('mail', 'Email could not be sent.'),
                    ],
                    max_length=30,
                    null=True,
                )),
                ('error_message', models.TextField(blank=True, null=True)),
                ('soft_failed', models.BooleanField(default=False)),
                ('created', models.DateTimeField(auto_now_add=True)),
                ('started', models.DateTimeField(blank=True, null=True)),
                ('finished', models.DateTimeField(blank=True, null=True)),
                ('schedule', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='executions',
                    to='pretixbase.scheduledeventexport',
                )),
            ],
            options={
                'ordering': ('-created',),
            },
            bases=(pretix.base.models.base.LoggingMixin, models.Model),
        ),
        migrations.CreateModel(
            name='ScheduledOrganizerExportExecution',
            fields=[
                ('id', models.BigAutoField(primary_key=True, serialize=False)),
                ('status', models.CharField(
                    choices=[
                        ('waiting', 'waiting'),
                        ('rendering', 'rendering'),
                        ('file_ready', 'file ready'),
                        ('mail_queued', 'email queued'),
                        ('failed', 'failed'),
                        ('completed', 'completed'),
                    ],
                    default='waiting',
                    max_length=20,
                )),
                ('scheduled_for', models.DateTimeField(blank=True, null=True)),
                ('manual', models.BooleanField(default=False)),
                ('export_identifier', models.CharField(max_length=190)),
                ('export_form_data', models.JSONField(
                    default=dict,
                    encoder=django.core.serializers.json.DjangoJSONEncoder,
                )),
                ('locale', models.CharField(max_length=250)),
                ('timezone_name', models.CharField(max_length=100)),
                ('mail_additional_recipients', models.TextField(blank=True)),
                ('mail_additional_recipients_cc', models.TextField(blank=True)),
                ('mail_additional_recipients_bcc', models.TextField(blank=True)),
                ('mail_subject', models.CharField(max_length=250)),
                ('mail_template', models.TextField()),
                ('error_code', models.CharField(
                    blank=True,
                    choices=[
                        ('permission', 'Permission denied.'),
                        ('config', 'Invalid export configuration.'),
                        ('empty', 'Your export did not contain any data.'),
                        ('internal', 'Internal error'),
                        ('mail', 'Email could not be sent.'),
                    ],
                    max_length=30,
                    null=True,
                )),
                ('error_message', models.TextField(blank=True, null=True)),
                ('soft_failed', models.BooleanField(default=False)),
                ('created', models.DateTimeField(auto_now_add=True)),
                ('started', models.DateTimeField(blank=True, null=True)),
                ('finished', models.DateTimeField(blank=True, null=True)),
                ('schedule', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='executions',
                    to='pretixbase.scheduledorganizerexport',
                )),
            ],
            options={
                'ordering': ('-created',),
            },
            bases=(pretix.base.models.base.LoggingMixin, models.Model),
        ),
        migrations.AddField(
            model_name='scheduledeventexportexecution',
            name='file',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='event_scheduled_export_executions',
                to='pretixbase.cachedfile',
            ),
        ),
        migrations.AddField(
            model_name='scheduledeventexportexecution',
            name='mail',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='event_scheduled_export_executions',
                to='pretixbase.outgoingmail',
            ),
        ),
        migrations.AddField(
            model_name='scheduledorganizerexportexecution',
            name='file',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='organizer_scheduled_export_executions',
                to='pretixbase.cachedfile',
            ),
        ),
        migrations.AddField(
            model_name='scheduledorganizerexportexecution',
            name='mail',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='organizer_scheduled_export_executions',
                to='pretixbase.outgoingmail',
            ),
        ),
        migrations.AddIndex(
            model_name='scheduledeventexportexecution',
            index=models.Index(fields=['status'], name='sched_event_status_idx'),
        ),
        migrations.AddIndex(
            model_name='scheduledeventexportexecution',
            index=models.Index(fields=['status', 'created'], name='sched_event_status_created_idx'),
        ),
        migrations.AddConstraint(
            model_name='scheduledeventexportexecution',
            constraint=models.UniqueConstraint(
                fields=('schedule', 'scheduled_for'),
                name='scheduled_event_export_slot_unique',
            ),
        ),
        migrations.AddIndex(
            model_name='scheduledorganizerexportexecution',
            index=models.Index(fields=['status'], name='sched_orga_status_idx'),
        ),
        migrations.AddIndex(
            model_name='scheduledorganizerexportexecution',
            index=models.Index(fields=['status', 'created'], name='sched_orga_status_created_idx'),
        ),
        migrations.AddConstraint(
            model_name='scheduledorganizerexportexecution',
            constraint=models.UniqueConstraint(
                fields=('schedule', 'scheduled_for'),
                name='scheduled_organizer_export_slot_unique',
            ),
        ),
    ]
