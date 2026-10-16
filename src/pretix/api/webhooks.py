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
# this file, see <https://pretix.eu/about/en/license>.
#
# This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied
# warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU Affero General Public License for more
# details.
#
# You should have received a copy of the GNU Affero General Public License along with this program.  If not, see
# <https://www.gnu.org/licenses/>.
#
import json
import logging
import time
from collections import OrderedDict
from datetime import timedelta

import requests
from django.db import DatabaseError, connection, transaction
from django.db.models import Exists, OuterRef, Q
from django.dispatch import receiver
from django.utils.timezone import now
from django.utils.translation import gettext_lazy as _, pgettext_lazy
from django_scopes import scope, scopes_disabled
from requests import RequestException

from pretix.api.models import (
    WebHook, WebHookCall, WebHookDelivery, WebHookEventListener,
)
from pretix.api.signals import register_webhook_events
from pretix.base.models import LogEntry
from pretix.base.services.tasks import ProfiledTask, TransactionAwareTask
from pretix.base.signals import periodic_task
from pretix.celery_app import app
from pretix.helpers import OF_SELF
from pretix.helpers.celery import get_task_priority

logger = logging.getLogger(__name__)
_ALL_EVENTS = None


class WebhookEvent:
    def __init__(self):
        pass

    def __repr__(self):
        return '<WebhookEvent: {}>'.format(self.action_type)

    @property
    def action_type(self) -> str:
        """
        The action_type string that this notification handles, for example
        ``"pretix.event.order.paid"``. Only one notification type should be registered
        per action type.
        """
        raise NotImplementedError()  # NOQA

    @property
    def verbose_name(self) -> str:
        """
        A human-readable name of this notification type.
        """
        raise NotImplementedError()  # NOQA

    def build_payload(self, logentry: LogEntry) -> dict:
        """
        This is the main function that you should override. It is supposed to turn a log entry
        object into a dictionary that can be used as the webhook payload.
        """
        raise NotImplementedError()  # NOQA

    @property
    def help_text(self) -> str:
        """
        A human-readable description
        """
        return ""


def get_all_webhook_events():
    global _ALL_EVENTS

    if _ALL_EVENTS:
        return _ALL_EVENTS

    types = OrderedDict()
    for recv, ret in register_webhook_events.send(None):
        if isinstance(ret, (list, tuple)):
            for r in ret:
                types[r.action_type] = r
        else:
            types[ret.action_type] = ret
    _ALL_EVENTS = types
    return types


class ParametrizedWebhookEvent(WebhookEvent):
    def __init__(self, action_type, verbose_name, help_text=""):
        self._action_type = action_type
        self._verbose_name = verbose_name
        self._help_text = help_text
        super().__init__()

    @property
    def action_type(self):
        return self._action_type

    @property
    def verbose_name(self):
        return self._verbose_name

    @property
    def help_text(self):
        return self._help_text


class ParametrizedOrderWebhookEvent(ParametrizedWebhookEvent):
    def build_payload(self, logentry: LogEntry):
        order = logentry.content_object
        if not order:
            return None

        return {
            'notification_id': logentry.pk,
            'organizer': order.event.organizer.slug,
            'event': order.event.slug,
            'code': order.code,
            'action': logentry.action_type,
        }


class DeletedOrderWebhookEvent(ParametrizedWebhookEvent):
    def build_payload(self, logentry: LogEntry):
        return {
            'notification_id': logentry.pk,
            'organizer': logentry.organizer.slug,
            'event': logentry.event.slug,
            'code': logentry.parsed_data.get("code"),
            'action': logentry.action_type,
        }


class ParametrizedEventWebhookEvent(ParametrizedWebhookEvent):

    def build_payload(self, logentry: LogEntry):
        if logentry.action_type == 'pretix.event.deleted':
            organizer = logentry.content_object
            return {
                'notification_id': logentry.pk,
                'organizer': organizer.slug,
                'event': logentry.parsed_data.get('slug'),
                'action': logentry.action_type,
            }

        event = logentry.content_object
        if not event:
            return None

        return {
            'notification_id': logentry.pk,
            'organizer': event.organizer.slug,
            'event': event.slug,
            'action': logentry.action_type,
        }


class ParametrizedGiftcardWebhookEvent(ParametrizedWebhookEvent):
    def build_payload(self, logentry: LogEntry):
        giftcard = logentry.content_object
        if not giftcard:
            return None

        return {
            'notification_id': logentry.pk,
            'issuer_id': logentry.organizer_id,
            'issuer_slug': logentry.organizer.slug,
            'giftcard': giftcard.pk,
            'action': logentry.action_type,
        }


class ParametrizedGiftcardTransactionWebhookEvent(ParametrizedWebhookEvent):
    def build_payload(self, logentry: LogEntry):
        giftcard = logentry.content_object
        if not giftcard:
            return None

        return {
            'notification_id': logentry.pk,
            'issuer_id': logentry.organizer_id,
            'issuer_slug': logentry.organizer.slug,
            'acceptor_id': logentry.parsed_data.get('acceptor_id'),
            'acceptor_slug': logentry.parsed_data.get('acceptor_slug'),
            'giftcard': giftcard.pk,
            'action': logentry.action_type,
        }


class ParametrizedVoucherWebhookEvent(ParametrizedWebhookEvent):

    def build_payload(self, logentry: LogEntry):
        # do not use content_object, this is also called in deletion
        return {
            'notification_id': logentry.pk,
            'organizer': logentry.event.organizer.slug,
            'event': logentry.event.slug,
            'voucher': logentry.object_id,
            'action': logentry.action_type,
        }


class ParametrizedSubEventWebhookEvent(ParametrizedWebhookEvent):

    def build_payload(self, logentry: LogEntry):
        # do not use content_object, this is also called in deletion
        return {
            'notification_id': logentry.pk,
            'organizer': logentry.event.organizer.slug,
            'event': logentry.event.slug,
            'subevent': logentry.object_id,
            'action': logentry.action_type,
        }


class ParametrizedItemWebhookEvent(ParametrizedWebhookEvent):

    def build_payload(self, logentry: LogEntry):
        # do not use content_object, this is also called in deletion
        return {
            'notification_id': logentry.pk,
            'organizer': logentry.event.organizer.slug,
            'event': logentry.event.slug,
            'item': logentry.object_id,
            'action': logentry.action_type,
        }


class ParametrizedOrderPositionCheckinWebhookEvent(ParametrizedOrderWebhookEvent):

    def build_payload(self, logentry: LogEntry):
        d = super().build_payload(logentry)
        if d is None:
            return None
        d['orderposition_id'] = logentry.parsed_data.get('position')
        d['orderposition_positionid'] = logentry.parsed_data.get('positionid')
        d['checkin_list'] = logentry.parsed_data.get('list')
        d['type'] = logentry.parsed_data.get('type')
        d['first_checkin'] = logentry.parsed_data.get('first_checkin')
        return d


class ParametrizedWaitingListEntryWebhookEvent(ParametrizedWebhookEvent):

    def build_payload(self, logentry: LogEntry):
        # do not use content_object, this is also called in deletion
        return {
            'notification_id': logentry.pk,
            'organizer': logentry.event.organizer.slug,
            'event': logentry.event.slug,
            'waitinglistentry': logentry.object_id,
            'action': logentry.action_type,
        }


class ParametrizedCustomerWebhookEvent(ParametrizedWebhookEvent):

    def build_payload(self, logentry: LogEntry):
        customer = logentry.content_object
        if not customer:
            return None

        return {
            'notification_id': logentry.pk,
            'organizer': customer.organizer.slug,
            'customer': customer.identifier,
            'action': logentry.action_type,
        }


@receiver(register_webhook_events, dispatch_uid="base_register_default_webhook_events")
def register_default_webhook_events(sender, **kwargs):
    return (
        ParametrizedOrderWebhookEvent(
            'pretix.event.order.placed',
            _('New order placed'),
        ),
        ParametrizedOrderWebhookEvent(
            'pretix.event.order.placed.require_approval',
            _('New order requires approval'),
        ),
        ParametrizedOrderWebhookEvent(
            'pretix.event.order.paid',
            _('Order marked as paid'),
        ),
        ParametrizedOrderWebhookEvent(
            'pretix.event.order.canceled',
            _('Order canceled'),
        ),
        ParametrizedOrderWebhookEvent(
            'pretix.event.order.reactivated',
            _('Order reactivated'),
        ),
        ParametrizedOrderWebhookEvent(
            'pretix.event.order.expired',
            _('Order expired'),
        ),
        ParametrizedOrderWebhookEvent(
            'pretix.event.order.expirychanged',
            _('Order expiry date changed'),
        ),
        ParametrizedOrderWebhookEvent(
            'pretix.event.order.modified',
            _('Order information changed'),
        ),
        ParametrizedOrderWebhookEvent(
            'pretix.event.order.contact.changed',
            _('Order contact address changed'),
        ),
        ParametrizedOrderWebhookEvent(
            'pretix.event.order.changed.*',
            _('Order changed'),
        ),
        ParametrizedOrderWebhookEvent(
            'pretix.event.order.refund.created',
            _('Refund of payment created'),
        ),
        ParametrizedOrderWebhookEvent(
            'pretix.event.order.refund.created.externally',
            _('External refund of payment'),
        ),
        ParametrizedOrderWebhookEvent(
            'pretix.event.order.refund.requested',
            _('Refund of payment requested by customer'),
        ),
        ParametrizedOrderWebhookEvent(
            'pretix.event.order.refund.done',
            _('Refund of payment completed'),
        ),
        ParametrizedOrderWebhookEvent(
            'pretix.event.order.refund.canceled',
            _('Refund of payment canceled'),
        ),
        ParametrizedOrderWebhookEvent(
            'pretix.event.order.refund.failed',
            _('Refund of payment failed'),
        ),
        ParametrizedOrderWebhookEvent(
            'pretix.event.order.payment.confirmed',
            _('Payment confirmed'),
        ),
        ParametrizedOrderWebhookEvent(
            'pretix.event.order.approved',
            _('Order approved'),
        ),
        ParametrizedOrderWebhookEvent(
            'pretix.event.order.denied',
            _('Order denied'),
        ),
        DeletedOrderWebhookEvent(
            'pretix.event.order.deleted',
            _('Order deleted'),
        ),
        ParametrizedOrderPositionCheckinWebhookEvent(
            'pretix.event.checkin',
            _('Ticket checked in'),
        ),
        ParametrizedOrderPositionCheckinWebhookEvent(
            'pretix.event.checkin.reverted',
            _('Ticket check-in reverted'),
        ),
        ParametrizedEventWebhookEvent(
            'pretix.event.added',
            _('Event created'),
        ),
        ParametrizedEventWebhookEvent(
            'pretix.event.changed',
            _('Event details changed'),
        ),
        ParametrizedEventWebhookEvent(
            'pretix.event.deleted',
            _('Event deleted'),
        ),
        ParametrizedSubEventWebhookEvent(
            'pretix.subevent.added',
            pgettext_lazy('subevent', 'Event series date added'),
        ),
        ParametrizedSubEventWebhookEvent(
            'pretix.subevent.changed',
            pgettext_lazy('subevent', 'Event series date changed'),
        ),
        ParametrizedSubEventWebhookEvent(
            'pretix.subevent.deleted',
            pgettext_lazy('subevent', 'Event series date deleted'),
        ),
        ParametrizedItemWebhookEvent(
            'pretix.event.item.*',
            _('Product changed'),
            _('This includes product added or deleted and changes to nested objects like '
              'variations or bundles.'),
        ),
        ParametrizedItemWebhookEvent(
            'pretix.event.quota.*',
            _('Quota changed'),
            _('This includes related events like creation, deletion, opening or closing of quotas. '
              'No webhook is sent for changes to the resulting availability.'),
        ),
        ParametrizedEventWebhookEvent(
            'pretix.event.live.activated',
            _('Shop taken live'),
        ),
        ParametrizedEventWebhookEvent(
            'pretix.event.live.deactivated',
            _('Shop taken offline'),
        ),
        ParametrizedEventWebhookEvent(
            'pretix.event.testmode.activated',
            _('Test-Mode of shop has been activated'),
        ),
        ParametrizedEventWebhookEvent(
            'pretix.event.testmode.deactivated',
            _('Test-Mode of shop has been deactivated'),
        ),
        ParametrizedWaitingListEntryWebhookEvent(
            'pretix.event.orders.waitinglist.added',
            _('Waiting list entry added'),
        ),
        ParametrizedWaitingListEntryWebhookEvent(
            'pretix.event.orders.waitinglist.changed',
            _('Waiting list entry changed'),
        ),
        ParametrizedWaitingListEntryWebhookEvent(
            'pretix.event.orders.waitinglist.deleted',
            _('Waiting list entry deleted'),
        ),
        ParametrizedWaitingListEntryWebhookEvent(
            'pretix.event.orders.waitinglist.voucher_assigned',
            _('Waiting list entry received voucher'),
        ),
        ParametrizedVoucherWebhookEvent(
            'pretix.voucher.added',
            _('Voucher added'),
        ),
        ParametrizedVoucherWebhookEvent(
            'pretix.voucher.changed',
            _('Voucher changed'),
            _('Only includes explicit changes to the voucher, not e.g. an increase of the number of redemptions.')
        ),
        ParametrizedVoucherWebhookEvent(
            'pretix.voucher.deleted',
            _('Voucher deleted'),
        ),
        ParametrizedCustomerWebhookEvent(
            'pretix.customer.created',
            _('Customer account created'),
        ),
        ParametrizedCustomerWebhookEvent(
            'pretix.customer.changed',
            _('Customer account changed'),
        ),
        ParametrizedCustomerWebhookEvent(
            'pretix.customer.anonymized',
            _('Customer account anonymized'),
        ),
        ParametrizedGiftcardWebhookEvent(
            'pretix.giftcards.created',
            _('Gift card added'),
        ),
        ParametrizedGiftcardWebhookEvent(
            'pretix.giftcards.modified',
            _('Gift card modified'),
        ),
        ParametrizedGiftcardTransactionWebhookEvent(
            'pretix.giftcards.transaction.*',
            _('Gift card used in transaction'),
        )
    )


#: Retry intervals in seconds. Added up, these are approximately 3 days, as documented.
RETRY_INTERVALS = (
    5,  # + 5 seconds
    30,  # + 30 seconds
    60,  # + 1 minute
    300,  # + 5 minutes
    1200,  # + 20 minutes
    3600,  # + 60 minutes
    14400,  # + 4 hours
    21600,  # + 6 hours
    43200,  # + 12 hours
    43200,  # + 24 hours
    86400,  # + 24 hours
)
#: Intervals below this threshold are scheduled as celery tasks directly, everything above is
#: persisted in the database and picked up by the periodic scan.
RETRY_CELERY_CUTOFF = 300
#: A waiting delivery that recently got a celery task published is not picked up again by scans
#: until this grace period elapsed. Must be longer than any normal broker/queue delay.
REPUBLISH_GRACE = timedelta(minutes=15)
#: An attempt (HTTP timeout is 30 seconds) which has been in the running state longer than this
#: is assumed to belong to a crashed worker or a lost task and may be reclaimed by another task
#: or the periodic scan.
STALE_ATTEMPT_TIMEOUT = timedelta(minutes=10)
#: Maximum number of deliveries a single periodic scan publishes, to keep batches bounded.
SCAN_BATCH_LIMIT = 500


def _delivery_log(level, delivery, message, *args):
    logger.log(
        level,
        "webhook delivery #%s (webhook=%s, event=%s, target=%s): " + message,
        delivery.id, delivery.webhook_id, delivery.action_type, delivery.target_url, *args,
        extra={
            'webhook_delivery_id': delivery.id,
            'webhook_id': delivery.webhook_id,
            'webhook_target_url': delivery.target_url,
            'webhook_action_type': delivery.action_type,
        }
    )


def _publish_delivery_attempt(delivery_id: int, organizer_id: int, countdown: int = None) -> bool:
    """
    Publishes a celery task for a delivery. If publishing fails, the delivery stays in a
    recoverable state and will be picked up by the next periodic scan.
    """
    kwargs = {
        'args': (delivery_id,),
        'priority': get_task_priority("notifications", organizer_id),
    }
    if countdown:
        kwargs['countdown'] = countdown
    try:
        send_webhook.apply_async(**kwargs)
    except Exception:
        logger.exception(
            "webhook delivery #%s: publishing the attempt task failed, leaving the delivery "
            "in a recoverable state for the periodic scan",
            delivery_id,
        )
        WebHookDelivery.objects.filter(
            pk=delivery_id, state=WebHookDelivery.STATE_WAITING
        ).update(scheduled_at=None)
        return False
    return True


@app.task(base=TransactionAwareTask, max_retries=9, default_retry_delay=900, acks_late=True)
def notify_webhooks(logentry_ids: list):
    if not isinstance(logentry_ids, list):
        logentry_ids = [logentry_ids]
    qs = LogEntry.all.select_related(
        'event', 'event__organizer', 'organizer'
    ).order_by(
        'action_type', 'organizer_id', 'event_id',
    ).filter(id__in=logentry_ids)
    _org, _at, _ev, webhooks = None, None, None, None
    for logentry in qs:
        if not logentry.organizer:
            break  # We need to know the organizer

        notification_type = logentry.webhook_type

        if not notification_type:
            break  # Ignore, no webhooks for this event type

        if _org != logentry.organizer or _at != logentry.action_type or _ev != logentry.event_id or webhooks is None:
            _org = logentry.organizer
            _at = logentry.action_type

            # All webhooks that registered for this notification
            event_listener = WebHookEventListener.objects.filter(
                webhook=OuterRef('pk'),
                action_type=notification_type.action_type
            )
            webhooks = WebHook.objects.annotate(has_el=Exists(event_listener)).filter(
                organizer=logentry.organizer,
                has_el=True,
                enabled=True
            )
            if logentry.event_id:
                webhooks = webhooks.filter(
                    Q(all_events=True) | Q(limit_events__pk=logentry.event_id)
                )

        # The payload is built once, when the notification is first queued, and the target URL
        # and event type are frozen alongside it. Later changes to the business object, the
        # webhook URL or the subscription scope never touch already-created deliveries.
        with scope(organizer=logentry.organizer):
            webhook_list = list(webhooks)
            if not webhook_list:
                continue
            payload = notification_type.build_payload(logentry)
            if payload is None:
                # Content object deleted?
                continue
            payload_json = json.dumps(payload)

            queued = []
            for wh in webhook_list:
                with transaction.atomic():
                    delivery, created = WebHookDelivery.objects.get_or_create(
                        webhook_id=wh.pk,
                        logentry_id=logentry.id,
                        defaults=dict(
                            action_type=notification_type.action_type,
                            target_url=wh.target_url,
                            payload=payload_json,
                            scheduled_at=now(),
                        ),
                    )
                    if created:
                        queued.append((delivery.id, wh.organizer_id))
                        _delivery_log(
                            logging.INFO, delivery,
                            "queued for logentry #%s, frozen JSON snapshot with %s bytes",
                            logentry.id, len(payload_json),
                        )

        for delivery_id, organizer_id in queued:
            _publish_delivery_attempt(delivery_id, organizer_id)


def _claim_delivery(delivery_id: int):
    """
    Claims a delivery for an attempt. Returns the delivery on success and ``None`` if the
    delivery is already in a terminal state, if another worker currently holds an active
    attempt, or if this row is locked by a concurrent scanner/worker.
    """
    d = WebHookDelivery.objects.select_related('webhook').select_for_update(
        skip_locked=connection.features.has_select_for_update_skip_locked,
        of=OF_SELF,
    ).filter(pk=delivery_id).first()
    if d is None:
        logger.info(
            "webhook delivery #%s: skipping attempt, the delivery is locked by another process",
            delivery_id,
        )
        return None
    if d.state in WebHookDelivery.TERMINAL_STATES:
        _delivery_log(logging.INFO, d, "skipping attempt, delivery is already %s", d.state)
        return None
    if d.state == WebHookDelivery.STATE_RUNNING:
        if d.locked_at and d.locked_at > now() - STALE_ATTEMPT_TIMEOUT:
            _delivery_log(
                logging.INFO, d,
                "skipping duplicate task, an attempt is already active since %s",
                d.locked_at,
            )
            return None
        _delivery_log(
            logging.WARNING, d,
            "reclaiming an attempt that has been running since %s, assuming the previous "
            "worker died", d.locked_at,
        )
    d.state = WebHookDelivery.STATE_RUNNING
    d.locked_at = now()
    d.scheduled_at = None
    d.save(update_fields=['state', 'locked_at', 'scheduled_at', 'updated'])
    return d


def _finish_delivery(delivery_id: int, return_code: int, response_body: str, execution_time: float) -> dict:
    """
    Records the HTTP attempt in the call history and moves the delivery to its next state.
    Runs while holding a row lock on the delivery, so concurrent retries, scans, worker
    restarts and administrator actions cannot create two active copies or record more than
    one final state.
    """
    with transaction.atomic():
        d = WebHookDelivery.objects.select_related('webhook').select_for_update(
            skip_locked=connection.features.has_select_for_update_skip_locked,
            of=OF_SELF,
        ).get(pk=delivery_id)

        attempt = d.retry_count + 1
        success = 200 <= return_code <= 299
        WebHookCall.objects.create(
            webhook_id=d.webhook_id,
            delivery=d,
            action_type=d.action_type,
            target_url=d.target_url,
            is_retry=d.retry_count > 0,
            execution_time=execution_time,
            return_code=return_code,
            payload=d.payload,
            response_body=response_body[:1024 * 1024],
            success=success,
        )

        # The delivery may have been terminated (endpoint disabled or given up manually) while
        # the HTTP request was in flight. Record the attempt, but never resurrect it.
        if d.state != WebHookDelivery.STATE_RUNNING:
            _delivery_log(
                logging.INFO, d,
                "attempt #%s returned HTTP %s after %.2fs, but the delivery had already been "
                "terminated as %s", attempt, return_code, execution_time, d.state,
            )
            return {'state': d.state}

        d.locked_at = None
        if success:
            d.state = WebHookDelivery.STATE_SUCCESS
            d.save(update_fields=['state', 'locked_at', 'updated'])
            _delivery_log(
                logging.INFO, d,
                "attempt #%s succeeded with HTTP %s after %.2fs, delivery finished: success",
                attempt, return_code, execution_time,
            )
            return {'state': WebHookDelivery.STATE_SUCCESS}

        if return_code == 410:
            d.state = WebHookDelivery.STATE_DISABLED
            d.save(update_fields=['state', 'locked_at', 'updated'])
            # This also terminates all other still-pending deliveries of this webhook.
            webhook = d.webhook
            webhook.enabled = False
            webhook.save(update_fields=['enabled'])
            _delivery_log(
                logging.WARNING, d,
                "attempt #%s got HTTP 410 after %.2fs, webhook disabled, delivery finished: "
                "terminated (endpoint disabled)", attempt, execution_time,
            )
            return {'state': WebHookDelivery.STATE_DISABLED}

        if d.retry_count >= len(RETRY_INTERVALS):
            d.state = WebHookDelivery.STATE_GIVEN_UP
            d.save(update_fields=['state', 'locked_at', 'updated'])
            _delivery_log(
                logging.WARNING, d,
                "attempt #%s failed with HTTP %s after %.2fs, retry schedule exhausted, "
                "delivery finished: given up", attempt, return_code, execution_time,
            )
            return {'state': WebHookDelivery.STATE_GIVEN_UP}

        interval = RETRY_INTERVALS[d.retry_count]
        next_not_before = now() + timedelta(seconds=interval)
        d.retry_count += 1
        d.state = WebHookDelivery.STATE_WAITING
        d.retry_not_before = next_not_before
        if interval < RETRY_CELERY_CUTOFF:
            # Publish a celery task directly; mark the delivery as scheduled so a concurrent
            # scan does not publish a second task.
            d.scheduled_at = now()
            via_celery = True
        else:
            # Persist the delayed retry and let the periodic scan publish when it is due.
            d.scheduled_at = None
            via_celery = False
        d.save(update_fields=['state', 'retry_count', 'retry_not_before', 'locked_at',
                              'scheduled_at', 'updated'])
        _delivery_log(
            logging.INFO, d,
            "attempt #%s failed with HTTP %s after %.2fs, waiting until %s for attempt #%s "
            "(%s)",
            attempt, return_code, execution_time, next_not_before, d.retry_count + 1,
            'celery' if via_celery else 'database',
        )
        return {
            'state': WebHookDelivery.STATE_WAITING,
            'via_celery': via_celery,
            'interval': interval,
        }


@app.task(base=ProfiledTask, bind=True, max_retries=5, default_retry_delay=60, acks_late=True,
          autoretry_for=(DatabaseError,),)
def send_webhook(self, delivery_id: int):
    """
    Performs one attempt of a previously queued webhook delivery.

    A :class:`WebHookDelivery` row is the single source of truth for a delivery: its target
    URL, event type and JSON request body are frozen when the notification is first queued and
    never change afterwards, even if the webhook configuration or the related business objects
    change.

    Retry and crash semantics:

    1. We retry for up to three days with approximately exponentially increasing intervals,
       see ``RETRY_INTERVALS``.

    2. ``acks_late=True`` prevents completely lost tasks when a worker crashes. Any duplicate
       task (worker redelivery, overlapping periodic scans, concurrent administrator actions)
       converges through a row-level claim: only one attempt can be active at a time, and a
       delivery already in a terminal state is never executed again.

    3. Because celery/redis can not properly handle long-running ``acks_late`` tasks beyond
       ``visibility_timeout``, delays of 5 minutes and more are persisted on the delivery row
       and published by the periodic scan; shorter delays are celery tasks. The periodic scan
       also reclaims deliveries left in the running state by a crashed worker, as well as
       waiting deliveries whose celery task could never be published.
    """
    with scopes_disabled():
        delivery = WebHookDelivery.objects.select_related('webhook').get(pk=delivery_id)

    with scope(organizer=delivery.webhook.organizer):
        with transaction.atomic():
            d = _claim_delivery(delivery_id)
        if d is None:
            return 'obsolete'

        if not d.webhook.enabled:
            # Safety net, disabling a webhook normally already terminates all pending
            # deliveries via WebHook.save().
            with transaction.atomic():
                WebHookDelivery.objects.filter(
                    pk=delivery_id, state__in=WebHookDelivery.ACTIVE_STATES
                ).update(state=WebHookDelivery.STATE_DISABLED, scheduled_at=None, locked_at=None)
            _delivery_log(logging.INFO, d, "webhook is disabled, delivery finished: terminated")
            return 'gone'

        _delivery_log(logging.INFO, d, "attempt #%s started, sending frozen JSON snapshot",
                      d.retry_count + 1)

        t = time.time()
        try:
            resp = requests.post(
                d.target_url,
                data=d.payload.encode('utf-8'),
                headers={'Content-Type': 'application/json'},
                allow_redirects=False,
                timeout=30,
            )
        except RequestException as e:
            execution_time = time.time() - t
            _delivery_log(
                logging.WARNING, d,
                "attempt #%s failed with a network error after %.2fs: %s",
                d.retry_count + 1, execution_time, str(e),
            )
            result = _finish_delivery(delivery_id, 0, str(e), execution_time)
        else:
            execution_time = time.time() - t
            result = _finish_delivery(delivery_id, resp.status_code, resp.text, execution_time)

        if result.get('state') == WebHookDelivery.STATE_WAITING and result.get('via_celery'):
            _publish_delivery_attempt(delivery_id, d.webhook.organizer_id,
                                      countdown=result['interval'])

        return result['state']


def _mark_waiting_for_publish(queryset, set_due_now=False):
    """
    Locks waiting deliveries and marks them as scheduled so a concurrent scan or administrator
    action can not publish a second task for them. Returns ``(delivery_id, organizer_id)``
    tuples for which a task must be published after the transaction committed.
    """
    t = now()
    due = []
    with transaction.atomic():
        for d in queryset.select_related('webhook').select_for_update(
            skip_locked=connection.features.has_select_for_update_skip_locked,
            of=OF_SELF,
        ):
            d.scheduled_at = t
            update_fields = ['scheduled_at', 'updated']
            if set_due_now:
                d.retry_not_before = t
                update_fields.append('retry_not_before')
            d.save(update_fields=update_fields)
            due.append((d.id, d.webhook.organizer_id))
    return due


@app.task(base=TransactionAwareTask)
def manually_retry_all_calls(webhook_id: int):
    with scopes_disabled():
        webhook = WebHook.objects.get(id=webhook_id)
    with scope(organizer=webhook.organizer):
        # Only waiting deliveries are affected; deliveries currently being attempted keep their
        # active attempt and end up waiting again if that attempt fails. All of these continue
        # the same frozen delivery, they are not turned into new requests.
        due = _mark_waiting_for_publish(
            webhook.deliveries.filter(state=WebHookDelivery.STATE_WAITING),
            set_due_now=True,
        )
        logger.info(
            "webhook #%s: administrator requested immediate retry of %s waiting deliveries",
            webhook_id, len(due),
        )
    for delivery_id, organizer_id in due:
        _publish_delivery_attempt(delivery_id, organizer_id)


@receiver(signal=periodic_task, dispatch_uid='pretixapi_schedule_webhook_retries_on_celery')
@scopes_disabled()
def schedule_webhook_retries_on_celery(sender, **kwargs):
    t = now()
    stale_cutoff = t - STALE_ATTEMPT_TIMEOUT
    republish_cutoff = t - REPUBLISH_GRACE

    # Pick up waiting deliveries that are due and either never got a task published or lost it
    # longer ago than the republish grace, as well as deliveries stuck in the running state
    # because a worker died mid-attempt (or the task was lost entirely).
    waiting_due = Q(
        state=WebHookDelivery.STATE_WAITING,
        retry_not_before__lte=t,
    ) & (
        Q(scheduled_at__isnull=True) | Q(scheduled_at__lte=republish_cutoff)
    )
    stale_running = Q(
        state=WebHookDelivery.STATE_RUNNING,
        locked_at__lte=stale_cutoff,
    )
    queryset = WebHookDelivery.objects.filter(waiting_due | stale_running)

    due = []
    with transaction.atomic():
        deliveries = list(
            queryset.select_related('webhook').select_for_update(
                skip_locked=connection.features.has_select_for_update_skip_locked,
                of=OF_SELF,
            )[:SCAN_BATCH_LIMIT]
        )
        for d in deliveries:
            if d.state == WebHookDelivery.STATE_WAITING:
                d.scheduled_at = t
                d.save(update_fields=['scheduled_at', 'updated'])
            # Stale running deliveries are not modified: the published task will either claim
            # the stale attempt or back off if another task got there first.
            due.append((d.id, d.webhook.organizer_id))

    if due:
        logger.info("webhook periodic scan: publishing %s due/stale delivery attempts", len(due))
    for delivery_id, organizer_id in due:
        _publish_delivery_attempt(delivery_id, organizer_id)
