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
from datetime import timedelta
from decimal import Decimal

import pytest
import responses
from django.utils.timezone import now
from django_scopes import scopes_disabled

from pretix.api.models import WebHook, WebHookDelivery
from pretix.base.models import Event, Item, Order, OrderPosition, Organizer, Team, User


@pytest.fixture
def organizer():
    return Organizer.objects.create(name='Dummy', slug='dummy')


@pytest.fixture
def event(organizer):
    event = Event.objects.create(
        organizer=organizer, name='Dummy', slug='dummy',
        date_from=now()
    )
    return event


@pytest.fixture
def webhook(organizer, event):
    wh = organizer.webhooks.create(
        enabled=True,
        target_url='https://google.com',
        all_events=False
    )
    wh.limit_events.add(event)
    wh.listeners.create(action_type='pretix.event.order.placed')
    wh.listeners.create(action_type='pretix.event.order.paid')
    return wh


@pytest.fixture
def admin_user(admin_team):
    u = User.objects.create_user('dummy@dummy.dummy', 'dummy')
    admin_team.members.add(u)
    return u


@pytest.fixture
def admin_team(organizer):
    return Team.objects.create(organizer=organizer, all_organizer_permissions=True, name='Admin team')


@pytest.mark.django_db
def test_list_of_webhooks(event, admin_user, client, webhook):
    client.login(email='dummy@dummy.dummy', password='dummy')
    resp = client.get('/control/organizer/dummy/webhooks')
    assert 'https://google.com' in resp.content.decode()


@pytest.mark.django_db
def test_create_webhook(event, admin_user, admin_team, client):
    client.login(email='dummy@dummy.dummy', password='dummy')
    client.post('/control/organizer/dummy/webhook/add', {
        'target_url': 'https://google.com',
        'enabled': 'on',
        'events': 'pretix.event.order.paid',
        'limit_events': str(event.pk),
    }, follow=True)
    with scopes_disabled():
        w = WebHook.objects.last()
        assert w.target_url == "https://google.com"
        assert w.limit_events.count() == 1
        assert list(w.listeners.values_list('action_type', flat=True)) == ['pretix.event.order.paid']
        assert not w.all_events


@pytest.mark.django_db
def test_update_webhook(event, admin_user, admin_team, webhook, client):
    client.login(email='dummy@dummy.dummy', password='dummy')
    client.post('/control/organizer/dummy/webhook/{}/edit'.format(webhook.pk), {
        'target_url': 'https://google.com',
        'enabled': 'on',
        'events': ['pretix.event.order.paid', 'pretix.event.order.canceled'],
        'limit_events': str(event.pk),
    }, follow=True)
    webhook.refresh_from_db()
    assert webhook.target_url == "https://google.com"
    with scopes_disabled():
        assert webhook.limit_events.count() == 1
        assert list(webhook.listeners.values_list('action_type', flat=True)) == ['pretix.event.order.canceled',
                                                                                 'pretix.event.order.paid']
        assert not webhook.all_events


@pytest.mark.django_db
def test_webhook_logs(event, admin_user, admin_team, webhook, client):
    client.login(email='dummy@dummy.dummy', password='dummy')
    webhook.calls.create(
        webhook=webhook,
        action_type='pretix.event.order.paid',
        target_url=webhook.target_url,
        is_retry=False,
        execution_time=2,
        return_code=0,
        payload='foo',
        response_body='bar'
    )
    resp = client.get('/control/organizer/dummy/webhook/{}/logs'.format(webhook.pk))
    assert 'pretix.event.order.paid' in resp.content.decode()


@pytest.mark.django_db
def test_webhook_logs_show_delivery_states(event, admin_user, admin_team, webhook, order, client):
    client.login(email='dummy@dummy.dummy', password='dummy')
    with scopes_disabled():
        waiting = _waiting_delivery(webhook, order)
        WebHookDelivery.objects.filter(pk=waiting.pk).update(
            state=WebHookDelivery.STATE_GIVEN_UP
        )
        resp = client.get('/control/organizer/dummy/webhook/{}/logs'.format(webhook.pk))
        body = resp.content.decode()
        assert 'Given up' in body
        assert webhook.target_url in body
        assert 'pretix.event.order.paid' in body


@pytest.fixture
def order(event):
    o = Order.objects.create(
        code='FOO', event=event, email='dummy@dummy.test',
        status=Order.STATUS_PENDING, locale='en',
        datetime=now(), expires=now() + timedelta(days=10),
        sales_channel=event.organizer.sales_channels.get(identifier="web"),
        total=Decimal('46.00'),
    )
    ticket = Item.objects.create(event=event, name='Early-bird ticket',
                                 default_price=Decimal('23.00'), admission=True)
    OrderPosition.objects.create(
        order=o, item=ticket, variation=None,
        price=Decimal("23.00"), attendee_name_parts={'full_name': "Peter"}, positionid=1
    )
    return o


def _waiting_delivery(webhook, order):
    le = order.log_action('pretix.event.order.paid', {}, save=False)
    le.save()
    return WebHookDelivery.objects.create(
        webhook=webhook,
        logentry=le,
        action_type='pretix.event.order.paid',
        target_url=webhook.target_url,
        payload=json.dumps({
            'notification_id': le.pk,
            'organizer': 'dummy',
            'event': 'dummy',
            'code': 'FOO',
            'action': 'pretix.event.order.paid',
        }),
        state=WebHookDelivery.STATE_WAITING,
        retry_not_before=now(),
    )


@pytest.mark.django_db
def test_webhook_drop_terminates_pending_deliveries(event, admin_user, admin_team, webhook, order, client):
    client.login(email='dummy@dummy.dummy', password='dummy')
    with scopes_disabled():
        d = _waiting_delivery(webhook, order)
        resp = client.post('/control/organizer/dummy/webhook/{}/logs'.format(webhook.pk), {
            'action': 'drop',
        })
        assert resp.status_code == 302
        d.refresh_from_db()
        assert d.state == WebHookDelivery.STATE_GIVEN_UP
        # The snapshot is retained, only the state changed.
        assert d.target_url == webhook.target_url
        assert json.loads(d.payload)['code'] == 'FOO'


@pytest.mark.django_db
@responses.activate
def test_webhook_expedite_retries_same_delivery(event, admin_user, admin_team, webhook,
                                                order, client,
                                                django_capture_on_commit_callbacks):
    client.login(email='dummy@dummy.dummy', password='dummy')
    responses.add(responses.POST, 'https://google.com', status=200)
    with scopes_disabled():
        d = _waiting_delivery(webhook, order)
        with django_capture_on_commit_callbacks(execute=True):
            resp = client.post('/control/organizer/dummy/webhook/{}/logs'.format(webhook.pk), {
                'action': 'expedite',
            })
        assert resp.status_code == 302
        d.refresh_from_db()
        assert d.state == WebHookDelivery.STATE_SUCCESS
        assert WebHookDelivery.objects.count() == 1
        assert len(responses.calls) == 1
        assert d.calls.count() == 1


@pytest.mark.django_db
def test_webhook_disable_terminates_pending_deliveries(event, admin_user, admin_team, webhook,
                                                       order, client):
    client.login(email='dummy@dummy.dummy', password='dummy')
    with scopes_disabled():
        d = _waiting_delivery(webhook, order)
        resp = client.post('/control/organizer/dummy/webhook/{}/edit'.format(webhook.pk), {
            'target_url': 'https://google.com',
            'events': ['pretix.event.order.paid', 'pretix.event.order.placed'],
            'limit_events': str(event.pk),
        }, follow=True)
        assert resp.status_code == 200
        d.refresh_from_db()
        assert d.state == WebHookDelivery.STATE_DISABLED
