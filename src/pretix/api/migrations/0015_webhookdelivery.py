#
# This file is part of pretix (Community Edition).
#
# Copyright (C) 2014-2020  Raphael Michel and contributors
# Copyright (C) 2020-today pretix GmbH and contributors
#
# This program is free software: you can redistribute it and/or modify it under the terms of the GNU Affero General
# Public License as published by the Free Software Foundation in version 3 of the License.
#
# ADDITIONAL TERMS APPLY: Pursuant to Section 7 of the GNU Affero General Public License, additional terms are
# applicable granting you additional permissions and placing additional restrictions on your usage of this software.
# Please refer to the pretix LICENSE file to obtain the full terms applicable to this work. If you did not receive
# this file, see <https://www.gnu.org/licenses/>.
#
import json

import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


def copy_pending_retries(apps, schema_editor):
    # Best-effort migration of pending retries into the new stable delivery table, freezing a
    # payload snapshot exactly like new deliveries do. Rows whose payload can no longer be
    # built (e.g. content object gone or plugin uninstalled) are dropped, as they would be
    # treated as obsolete at runtime anyway.
    import logging

    from django_scopes import scopes_disabled

    from pretix.api.webhooks import get_all_webhook_events

    WebHookDelivery = apps.get_model('pretixapi', 'WebHookDelivery')
    WebHookCallRetry = apps.get_model('pretixapi', 'WebHookCallRetry')
    logger = logging.getLogger(__name__)

    event_types = None
    with scopes_disabled():
        for old in WebHookCallRetry.objects.select_related(
            'webhook', 'logentry__event__organizer', 'logentry__organizer'
        ).iterator():
            if event_types is None:
                event_types = get_all_webhook_events()
            event_type = event_types.get(old.action_type)
            payload = None
            if event_type is not None:
                try:
                    payload = event_type.build_payload(old.logentry)
                except Exception:
                    logger.exception(
                        "Could not freeze payload for pending webhook retry (logentry=%s)",
                        old.logentry_id,
                    )
                    payload = None
            if payload is None:
                continue
            WebHookDelivery.objects.create(
                webhook_id=old.webhook_id,
                logentry_id=old.logentry_id,
                action_type=old.action_type,
                target_url=old.webhook.target_url,
                payload=json.dumps(payload),
                state='waiting',
                retry_count=old.retry_count,
                retry_not_before=old.retry_not_before,
            )


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('pretixbase', '0311_fix_unshredded_invoices'),
        ('pretixapi', '0014_alter_webhook_target_url_and_more'),
    ]

    operations = [
        migrations.CreateModel(
            name='WebHookDelivery',
            fields=[
                ('id', models.BigAutoField(primary_key=True, serialize=False)),
                ('action_type', models.CharField(max_length=255)),
                ('target_url', models.URLField(max_length=1024)),
                ('payload', models.TextField()),
                ('state', models.CharField(
                    choices=[
                        ('waiting', 'Waiting for an attempt'),
                        ('running', 'Attempt in progress'),
                        ('success', 'Successfully delivered'),
                        ('given_up', 'Given up'),
                        ('disabled', 'Terminated because the endpoint was disabled'),
                    ],
                    default='waiting', max_length=16,
                )),
                ('retry_count', models.PositiveIntegerField(default=0)),
                ('retry_not_before', models.DateTimeField(default=django.utils.timezone.now)),
                ('scheduled_at', models.DateTimeField(blank=True, null=True)),
                ('locked_at', models.DateTimeField(blank=True, null=True)),
                ('created', models.DateTimeField(auto_now_add=True)),
                ('updated', models.DateTimeField(auto_now=True)),
                ('logentry', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='webhook_deliveries', to='pretixbase.logentry',
                )),
                ('webhook', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='deliveries', to='pretixapi.webhook',
                )),
            ],
            options={
                'ordering': ('id',),
                'unique_together': {('webhook', 'logentry')},
                'indexes': [
                    models.Index(fields=['state', 'retry_not_before'],
                                 name='pretixapi_w_state_b28dd1_idx'),
                ],
            },
        ),
        migrations.AddField(
            model_name='webhookcall',
            name='delivery',
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='calls', to='pretixapi.webhookdelivery',
            ),
        ),
        migrations.RunPython(copy_pending_retries, noop),
        migrations.DeleteModel(
            name='WebHookCallRetry',
        ),
    ]
