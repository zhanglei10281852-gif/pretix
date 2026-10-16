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
from datetime import timedelta
from decimal import Decimal

import pytest
import responses
from django.utils.timezone import now
from django_scopes import scopes_disabled

from pretix.api.models import WebHookCall, WebHookDelivery
from pretix.api.signals import cleanup_webhook_logs
from pretix.api import webhooks as whmodule
from pretix.api.webhooks import (
    RETRY_INTERVALS, manually_retry_all_calls, schedule_webhook_retries_on_celery,
    send_webhook,
)
from pretix.base.models import Event, Item, Order, OrderPosition, Organizer


@pytest.fixture
def organizer():
    return Organizer.objects.create(name='Dummy', slug='dummy')


@pytest.fixture
def event(organizer):
    return Event.objects.create(
        organizer=organizer, name='Dummy', slug='dummy',
        date_from=now()
    )


@pytest.fixture
def webhook(organizer, event):
    wh = organizer.webhooks.create(
        enabled=True,
        target_url='https://old.example.com/hook',
        all_events=False
    )
    wh.limit_events.add(event)
    wh.listeners.create(action_type='pretix.event.order.placed')
    wh.listeners.create(action_type='pretix.event.order.paid')
    return wh


@pytest.fixture
def order(event):
    o = Order.objects.create(
        code='FOO', event=event, email='dummy@dummy.test',
        status=Order.STATUS_PENDING, locale='en',
        datetime=now(), expires=now() + timedelta(days=10),
        sales_channel=event.organizer.sales_channels.get(identifier="web"),
        total=Decimal('46.00'),
    )
    tr19 = event.tax_rules.create(rate=Decimal('19.00'))
    ticket = Item.objects.create(event=event, name='Early-bird ticket', tax_rule=tr19,
                                 default_price=Decimal('23.00'), admission=True)
    OrderPosition.objects.create(
        order=o, item=ticket, variation=None,
        price=Decimal("23.00"), attendee_name_parts={'full_name': "Peter"}, positionid=1
    )
    return o


def force_str(v):
    return v.decode() if isinstance(v, bytes) else str(v)


def _logentry(order, action='pretix.event.order.paid'):
    # Builds a saved LogEntry without triggering the webhook fanout.
    le = order.log_action(action, {}, save=False)
    le.save()
    return le


def _waiting_delivery(webhook, logentry, payload=None, retry_count=0):
    if payload is None:
        payload = {
            'notification_id': logentry.pk,
            'organizer': 'dummy',
            'event': 'dummy',
            'code': 'FOO',
            'action': logentry.action_type,
        }
    return WebHookDelivery.objects.create(
        webhook=webhook,
        logentry=logentry,
        action_type=logentry.action_type,
        target_url=webhook.target_url,
        payload=json.dumps(payload),
        state=WebHookDelivery.STATE_WAITING,
        retry_count=retry_count,
        retry_not_before=now(),
    )


@pytest.fixture
def no_celery_phase_retry(mocker):
    # Make "publish the next short-interval attempt" a no-op so that tests observe exactly one
    # attempt and can drive the next one themselves.
    mocker.patch.object(whmodule, '_publish_delivery_attempt', return_value=True)


@pytest.mark.django_db
@responses.activate
def test_delivery_frozen_on_enqueue_and_success(event, order, webhook,
                                                django_capture_on_commit_callbacks):
    responses.add(responses.POST, 'https://old.example.com/hook', status=200)
    with django_capture_on_commit_callbacks(execute=True):
        le = order.log_action('pretix.event.order.paid', {})

    with scopes_disabled():
        assert WebHookDelivery.objects.count() == 1
        d = WebHookDelivery.objects.get()
        assert d.state == WebHookDelivery.STATE_SUCCESS
        assert d.retry_count == 0
        assert d.target_url == 'https://old.example.com/hook'
        assert d.action_type == 'pretix.event.order.paid'
        assert json.loads(d.payload) == {
            'notification_id': le.pk,
            'organizer': 'dummy',
            'event': 'dummy',
            'code': 'FOO',
            'action': 'pretix.event.order.paid',
        }
        calls = list(d.calls.all())
        assert len(calls) == 1
        assert calls[0].success
        assert calls[0].target_url == d.target_url
        assert calls[0].payload == d.payload
        assert not calls[0].is_retry


@pytest.mark.django_db
@responses.activate
def test_config_change_does_not_rewrite_waiting_delivery(event, order, webhook,
                                                         django_capture_on_commit_callbacks,
                                                         no_celery_phase_retry):
    responses.add(responses.POST, 'https://old.example.com/hook', status=500)
    with django_capture_on_commit_callbacks(execute=True):
        order.log_action('pretix.event.order.paid', {})

    with scopes_disabled():
        d = WebHookDelivery.objects.get()
        assert d.state == WebHookDelivery.STATE_WAITING
        frozen_payload = d.payload
        delivery_id = d.pk

        # The operator points the webhook to a new integration endpoint.
        webhook.target_url = 'https://new.example.org/integration'
        webhook.save()

        d.refresh_from_db()
        assert d.pk == delivery_id
        assert d.state == WebHookDelivery.STATE_WAITING
        assert d.target_url == 'https://old.example.com/hook'
        assert d.payload == frozen_payload

        # Only the frozen old address answers. Hitting the new address would raise a
        # ConnectionError and send the delivery back into waiting, so success here proves the
        # retry was not rewritten to the new address.
        responses.reset()
        responses.add(responses.POST, 'https://old.example.com/hook', status=200)
        send_webhook.run(d.pk)

        d.refresh_from_db()
        assert d.state == WebHookDelivery.STATE_SUCCESS
        assert len(responses.calls) == 1
        assert force_str(responses.calls[0].request.url) == 'https://old.example.com/hook'
        assert force_str(responses.calls[0].request.body) == frozen_payload
        for call in d.calls.all():
            assert call.target_url == 'https://old.example.com/hook'
            assert call.payload == frozen_payload


@pytest.mark.django_db
@responses.activate
def test_payload_not_changed_by_later_business_object_change(event, order, webhook,
                                                             no_celery_phase_retry):
    le = _logentry(order)
    with scopes_disabled():
        d = _waiting_delivery(webhook, le)
        frozen = d.payload

        # Business object changes after the notification was first queued.
        order.email = 'changed@example.com'
        order.status = Order.STATUS_PAID
        order.save()

        responses.add(responses.POST, 'https://old.example.com/hook', status=200)
        send_webhook.run(d.pk)

        d.refresh_from_db()
        assert d.state == WebHookDelivery.STATE_SUCCESS
        assert d.payload == frozen
        assert force_str(responses.calls[0].request.body) == frozen
        assert json.loads(force_str(responses.calls[0].request.body))['code'] == 'FOO'


@pytest.mark.django_db
@responses.activate
def test_new_notification_after_config_change_uses_new_configuration(event, order, webhook,
                                                                     django_capture_on_commit_callbacks,
                                                                     mocker):
    # Prevent the eager celery retry chain of the first failure, but only while the first
    # notification is being fanned out.
    mocker.patch.object(whmodule, '_publish_delivery_attempt', return_value=True)
    responses.add(responses.POST, 'https://old.example.com/hook', status=500)
    with django_capture_on_commit_callbacks(execute=True):
        le1 = order.log_action('pretix.event.order.paid', {})

    with scopes_disabled():
        first = WebHookDelivery.objects.get(logentry_id=le1.pk)
        webhook.target_url = 'https://new.example.org/integration'
        webhook.save()

    mocker.stopall()
    responses.reset()
    responses.add(responses.POST, 'https://new.example.org/integration', status=200)
    with django_capture_on_commit_callbacks(execute=True):
        le2 = order.log_action('pretix.event.order.placed', {})

    with scopes_disabled():
        deliveries = list(WebHookDelivery.objects.order_by('id'))
        assert len(deliveries) == 2
        assert deliveries[0].pk == first.pk
        assert deliveries[0].target_url == 'https://old.example.com/hook'
        assert deliveries[0].state == WebHookDelivery.STATE_WAITING
        assert deliveries[1].logentry_id == le2.pk
        assert deliveries[1].target_url == 'https://new.example.org/integration'
        assert deliveries[1].state == WebHookDelivery.STATE_SUCCESS
        assert len(responses.calls) == 1
        assert force_str(responses.calls[0].request.url) == 'https://new.example.org/integration'


@pytest.mark.django_db
@responses.activate
def test_duplicate_task_after_success_does_not_send_again(event, order, webhook, django_capture_on_commit_callbacks):
    responses.add(responses.POST, 'https://old.example.com/hook', status=200)
    with django_capture_on_commit_callbacks(execute=True):
        order.log_action('pretix.event.order.paid', {})

    with scopes_disabled():
        d = WebHookDelivery.objects.get()
        # Simulate a worker redelivery / duplicated scan after the terminal state was reached.
        send_webhook.run(d.pk)
        send_webhook.apply(args=(d.pk,))
        assert d.calls.count() == 1
        assert len(responses.calls) == 1


@pytest.mark.django_db
@responses.activate
def test_running_delivery_cannot_get_a_second_active_copy(event, order, webhook):
    le = _logentry(order)
    with scopes_disabled():
        d = _waiting_delivery(webhook, le)
        WebHookDelivery.objects.filter(pk=d.pk).update(
            state=WebHookDelivery.STATE_RUNNING, locked_at=now()
        )
        # A concurrent task must back off and not perform an HTTP request.
        assert send_webhook.run(d.pk) == 'obsolete'
        assert len(responses.calls) == 0
        d.refresh_from_db()
        assert d.state == WebHookDelivery.STATE_RUNNING
        assert d.calls.count() == 0


@pytest.mark.django_db
@responses.activate
def test_stale_running_delivery_is_reclaimed(event, order, webhook):
    le = _logentry(order)
    with scopes_disabled():
        d = _waiting_delivery(webhook, le)
        WebHookDelivery.objects.filter(pk=d.pk).update(
            state=WebHookDelivery.STATE_RUNNING,
            locked_at=now() - timedelta(minutes=20),
        )
        responses.add(responses.POST, 'https://old.example.com/hook', status=200)
        send_webhook.run(d.pk)
        d.refresh_from_db()
        assert d.state == WebHookDelivery.STATE_SUCCESS
        assert d.calls.count() == 1


@pytest.mark.django_db
@responses.activate
def test_backoff_written_to_same_delivery(event, order, webhook, no_celery_phase_retry):
    le = _logentry(order)
    with scopes_disabled():
        d = _waiting_delivery(webhook, le)
        responses.add(responses.POST, 'https://old.example.com/hook', status=500)
        send_webhook.run(d.pk)

        d.refresh_from_db()
        assert d.state == WebHookDelivery.STATE_WAITING
        assert d.retry_count == 1
        assert d.retry_not_before > now()
        assert d.calls.count() == 1
        assert d.calls.get().is_retry is False

        # Second attempt is a retry, still the same delivery.
        responses.add(responses.POST, 'https://old.example.com/hook', status=500)
        WebHookDelivery.objects.filter(pk=d.pk).update(retry_not_before=now())
        send_webhook.run(d.pk)
        d.refresh_from_db()
        assert d.retry_count == 2
        assert WebHookDelivery.objects.count() == 1
        assert d.calls.count() == 2
        assert d.calls.filter(is_retry=False).count() == 1
        assert d.calls.filter(is_retry=True).count() == 1


@pytest.mark.django_db
@responses.activate
def test_database_phase_retry_persisted_without_task(event, order, webhook, mocker):
    le = _logentry(order)
    with scopes_disabled():
        d = _waiting_delivery(webhook, le, retry_count=3)  # next interval is 300s -> DB phase
        publish = mocker.patch.object(whmodule, '_publish_delivery_attempt')
        responses.add(responses.POST, 'https://old.example.com/hook', status=500)
        send_webhook.run(d.pk)
        d.refresh_from_db()
        assert d.state == WebHookDelivery.STATE_WAITING
        assert d.retry_count == 4
        assert d.scheduled_at is None
        assert d.retry_not_before >= now() + timedelta(seconds=290)
        publish.assert_not_called()


@pytest.mark.django_db
@responses.activate
def test_retry_schedule_exhausted_gives_up(event, order, webhook):
    le = _logentry(order)
    with scopes_disabled():
        d = _waiting_delivery(webhook, le, retry_count=len(RETRY_INTERVALS))
        responses.add(responses.POST, 'https://old.example.com/hook', status=500)
        result = send_webhook.run(d.pk)
        assert result == WebHookDelivery.STATE_GIVEN_UP
        d.refresh_from_db()
        assert d.state == WebHookDelivery.STATE_GIVEN_UP
        # A terminal delivery is never attempted again.
        assert send_webhook.run(d.pk) == 'obsolete'
        assert len(responses.calls) == 1


@pytest.mark.django_db
@responses.activate
def test_network_error_enters_backoff(event, order, webhook, no_celery_phase_retry):
    le = _logentry(order)
    with scopes_disabled():
        d = _waiting_delivery(webhook, le)
        # No response registered -> ConnectionError
        result = send_webhook.run(d.pk)
        assert result == WebHookDelivery.STATE_WAITING
        d.refresh_from_db()
        assert d.state == WebHookDelivery.STATE_WAITING
        assert d.retry_count == 1
        call = d.calls.get()
        assert call.return_code == 0
        assert not call.success
        assert call.response_body


@pytest.mark.django_db
@responses.activate
def test_410_disables_webhook_and_terminates_pending(event, order, webhook):
    le1 = _logentry(order)
    le2 = _logentry(order, action='pretix.event.order.placed')
    with scopes_disabled():
        d1 = _waiting_delivery(webhook, le1)
        d2 = _waiting_delivery(webhook, le2)
        responses.add(responses.POST, 'https://old.example.com/hook', status=410)
        result = send_webhook.run(d1.pk)
        assert result == WebHookDelivery.STATE_DISABLED
        webhook.refresh_from_db()
        assert not webhook.enabled
        d1.refresh_from_db()
        d2.refresh_from_db()
        assert d1.state == WebHookDelivery.STATE_DISABLED
        assert d2.state == WebHookDelivery.STATE_DISABLED
        # No further attempts happen, even when explicitly scheduled.
        assert send_webhook.run(d1.pk) == 'obsolete'
        assert len(responses.calls) == 1


@pytest.mark.django_db
def test_disabling_webhook_terminates_all_pending(event, order, webhook):
    le1 = _logentry(order)
    le2 = _logentry(order, action='pretix.event.order.placed')
    with scopes_disabled():
        _waiting_delivery(webhook, le1)
        d2 = _waiting_delivery(webhook, le2)
        WebHookDelivery.objects.filter(pk=d2.pk).update(
            state=WebHookDelivery.STATE_RUNNING, locked_at=now()
        )

        webhook.enabled = False
        webhook.save(update_fields=['enabled'])

        assert set(WebHookDelivery.objects.values_list('state', flat=True)) == {
            WebHookDelivery.STATE_DISABLED
        }


@pytest.mark.django_db
@responses.activate
def test_periodic_scan_publishes_due_waiting(event, order, webhook):
    le = _logentry(order)
    with scopes_disabled():
        d = _waiting_delivery(webhook, le)
        WebHookDelivery.objects.filter(pk=d.pk).update(scheduled_at=None)
        responses.add(responses.POST, 'https://old.example.com/hook', status=200)
        schedule_webhook_retries_on_celery(sender=None)
        d.refresh_from_db()
        assert d.state == WebHookDelivery.STATE_SUCCESS
        assert len(responses.calls) == 1


@pytest.mark.django_db
@responses.activate
def test_periodic_scan_respects_future_schedule_and_marker(event, order, webhook):
    le = _logentry(order)
    with scopes_disabled():
        d = _waiting_delivery(webhook, le)
        # Not due yet.
        WebHookDelivery.objects.filter(pk=d.pk).update(
            scheduled_at=None, retry_not_before=now() + timedelta(hours=1)
        )
        schedule_webhook_retries_on_celery(sender=None)
        assert len(responses.calls) == 0

        # Due but a task was published only recently.
        WebHookDelivery.objects.filter(pk=d.pk).update(
            retry_not_before=now(), scheduled_at=now() - timedelta(minutes=1)
        )
        schedule_webhook_retries_on_celery(sender=None)
        assert len(responses.calls) == 0

        # Marker older than the grace period -> republish (e.g. task was lost).
        WebHookDelivery.objects.filter(pk=d.pk).update(
            scheduled_at=now() - timedelta(minutes=20)
        )
        responses.add(responses.POST, 'https://old.example.com/hook', status=200)
        schedule_webhook_retries_on_celery(sender=None)
        d.refresh_from_db()
        assert d.state == WebHookDelivery.STATE_SUCCESS
        assert len(responses.calls) == 1


@pytest.mark.django_db
@responses.activate
def test_periodic_scan_reclaims_stale_running(event, order, webhook):
    le = _logentry(order)
    with scopes_disabled():
        d = _waiting_delivery(webhook, le)
        WebHookDelivery.objects.filter(pk=d.pk).update(
            state=WebHookDelivery.STATE_RUNNING,
            locked_at=now() - timedelta(minutes=20),
        )
        responses.add(responses.POST, 'https://old.example.com/hook', status=200)
        schedule_webhook_retries_on_celery(sender=None)
        d.refresh_from_db()
        assert d.state == WebHookDelivery.STATE_SUCCESS


@pytest.mark.django_db
@responses.activate
def test_publish_failure_leaves_delivery_recoverable(event, order, webhook, mocker,
                                                     django_capture_on_commit_callbacks):
    from pretix.api.webhooks import send_webhook as sw

    calls = {'n': 0}

    def flaky_apply_async(*args, **kwargs):
        calls['n'] += 1
        if calls['n'] == 1:
            raise OSError('broker is down')

    mocker.patch.object(sw, 'apply_async', side_effect=flaky_apply_async)
    responses.add(responses.POST, 'https://old.example.com/hook', status=200)

    with django_capture_on_commit_callbacks(execute=True):
        order.log_action('pretix.event.order.paid', {})

    with scopes_disabled():
        d = WebHookDelivery.objects.get()
        assert d.state == WebHookDelivery.STATE_WAITING
        assert d.scheduled_at is None
        assert len(responses.calls) == 0

        # Stop mocking: the periodic scan recovers the delivery on its next run.
        mocker.stopall()
        responses.add(responses.POST, 'https://old.example.com/hook', status=200)
        schedule_webhook_retries_on_celery(sender=None)
        d.refresh_from_db()
        assert d.state == WebHookDelivery.STATE_SUCCESS
        assert len(responses.calls) == 1


@pytest.mark.django_db
@responses.activate
def test_manual_retry_now_continues_same_delivery(event, order, webhook):
    le = _logentry(order)
    with scopes_disabled():
        d = _waiting_delivery(webhook, le, retry_count=2)
        responses.add(responses.POST, 'https://old.example.com/hook', status=200)
        manually_retry_all_calls.run(webhook.pk)
        d.refresh_from_db()
        assert d.state == WebHookDelivery.STATE_SUCCESS
        assert WebHookDelivery.objects.count() == 1
        call = d.calls.get()
        assert call.is_retry
        assert call.target_url == 'https://old.example.com/hook'
        assert call.payload == d.payload


@pytest.mark.django_db
def test_admin_drop_explicitly_terminates_pending(event, order, webhook):
    le1 = _logentry(order)
    le2 = _logentry(order, action='pretix.event.order.placed')
    with scopes_disabled():
        d1 = _waiting_delivery(webhook, le1)
        d2 = _waiting_delivery(webhook, le2)
        webhook.deliveries.filter(
            state__in=(WebHookDelivery.STATE_WAITING, WebHookDelivery.STATE_RUNNING)
        ).update(
            state=WebHookDelivery.STATE_GIVEN_UP,
            scheduled_at=None,
            locked_at=None,
        )
        assert set(WebHookDelivery.objects.values_list('state', flat=True)) == {
            WebHookDelivery.STATE_GIVEN_UP
        }
        assert send_webhook.run(d1.pk) == 'obsolete'
        assert send_webhook.run(d2.pk) == 'obsolete'


@pytest.mark.django_db
def test_cleanup_keeps_snapshots_within_retry_window(event, order, webhook):
    le1 = _logentry(order)
    le2 = _logentry(order, action='pretix.event.order.placed')
    with scopes_disabled():
        waiting = _waiting_delivery(webhook, le1)
        success = _waiting_delivery(webhook, le2)
        WebHookDelivery.objects.filter(pk=waiting.pk).update(
            created=now() - timedelta(days=40), updated=now() - timedelta(days=40)
        )
        WebHookDelivery.objects.filter(pk=success.pk).update(
            state=WebHookDelivery.STATE_SUCCESS,
            created=now() - timedelta(days=40), updated=now() - timedelta(days=40),
        )
        # History for the successful delivery is recent and must survive its delivery cleanup.
        WebHookCall.objects.create(
            webhook=webhook, delivery=success,
            action_type=success.action_type, target_url=success.target_url,
            payload=success.payload, response_body='', success=True, return_code=200,
        )

        cleanup_webhook_logs(sender=None)

        waiting.refresh_from_db()
        assert waiting.state == WebHookDelivery.STATE_WAITING
        assert not WebHookDelivery.objects.filter(pk=success.pk).exists()
        call = WebHookCall.objects.get()
        assert call.delivery_id is None
        assert call.payload == success.payload
