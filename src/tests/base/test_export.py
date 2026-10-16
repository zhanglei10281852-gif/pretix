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
from datetime import datetime, time, timedelta, timezone

import pytest
from django.core import mail as djmail
from django.core.files.base import ContentFile
from django.utils.timezone import now
from django_scopes import scope
from freezegun import freeze_time

from pretix.base.models import (
    CachedFile, Event, LogEntry, Organizer, OutgoingMail,
    ScheduledEventExport, ScheduledEventExportExecution,
    ScheduledOrganizerExport, ScheduledOrganizerExportExecution, User,
    cachedfile_name,
)
from pretix.base.services.cleanup import clean_cached_files
from pretix.base.services.export import (
    _recover_scheduled_export_executions, create_manual_execution,
    run_scheduled_exports, scheduled_event_export,
)


@pytest.fixture(scope='function')
def event():
    o = Organizer.objects.create(name='Dummy', slug='dummy')
    event = Event.objects.create(
        organizer=o, name='Dummy', slug='dummy',
        date_from=datetime(2023, 1, 19, 2, 30, 0, tzinfo=timezone.utc),
        plugins='pretix.plugins.banktransfer'
    )
    o.settings.timezone = "Europe/Berlin"
    with scope(organizer=o):
        yield event


@pytest.fixture
def team(event):
    return event.organizer.teams.create(all_events=True, all_event_permissions=True)


@pytest.fixture
def user(team):
    user = User.objects.create_user('dummy@dummy.dummy', 'dummy')
    team.members.add(user)
    return user


@pytest.mark.django_db
@freeze_time("2023-01-18 03:00:00+01:00")
def test_event_run_sets_new_time(event, user):
    s = ScheduledEventExport(event=event, owner=user)
    s.schedule_rrule = "DTSTART:20230118T000000\nRRULE:FREQ=DAILY;INTERVAL=1;WKST=MO"
    s.schedule_rrule_time = time(2, 30, 0)
    s.schedule_next_run = now() - timedelta(minutes=5)
    s.save()

    run_scheduled_exports(None)
    s.refresh_from_db()
    assert s.schedule_next_run == datetime(2023, 1, 19, 2, 30, 0, tzinfo=event.timezone)


@pytest.mark.django_db
@freeze_time("2023-01-18 03:00:00+01:00")
def test_event_not_run_when_failed_5_times(event, user):
    s = ScheduledEventExport(event=event, owner=user)
    s.schedule_rrule = "DTSTART:20230118T000000\nRRULE:FREQ=DAILY;INTERVAL=1;WKST=MO"
    s.schedule_rrule_time = time(2, 30, 0)
    s.schedule_next_run = datetime(2023, 1, 18, 2, 30, 0, tzinfo=event.timezone)
    s.error_counter = 5
    s.save()

    run_scheduled_exports(None)
    s.refresh_from_db()
    assert s.schedule_next_run == datetime(2023, 1, 18, 2, 30, 0, tzinfo=event.timezone)


@pytest.mark.django_db
@freeze_time("2023-01-18 03:00:00+01:00")
def test_event_fail_invalid_config(event, user):
    djmail.outbox = []
    s = ScheduledEventExport(event=event, owner=user)
    s.export_identifier = " invalid "
    s.schedule_rrule = "DTSTART:20230118T000000\nRRULE:FREQ=DAILY;INTERVAL=1;WKST=MO"
    s.schedule_rrule_time = time(2, 30, 0)
    s.schedule_next_run = now() - timedelta(minutes=5)
    s.error_counter = 0
    s.save()

    run_scheduled_exports(None)
    s.refresh_from_db()
    assert s.schedule_next_run > now()
    assert s.error_counter == 1
    assert len(djmail.outbox) == 1
    assert djmail.outbox[0].subject == "Scheduled export failed"
    assert "Reason: Export type not found" in djmail.outbox[0].body
    assert djmail.outbox[0].to == [user.email]


@pytest.mark.django_db
@freeze_time("2023-01-18 03:00:00+01:00")
def test_event_fail_user_inactive(event, user):
    djmail.outbox = []
    s = ScheduledEventExport(event=event, owner=user)
    s.export_identifier = "orderlist"
    s.schedule_rrule = "DTSTART:20230118T000000\nRRULE:FREQ=DAILY;INTERVAL=1;WKST=MO"
    s.schedule_rrule_time = time(2, 30, 0)
    s.schedule_next_run = now() - timedelta(minutes=5)
    s.error_counter = 0
    s.save()

    user.is_active = False
    user.save()

    run_scheduled_exports(None)
    s.refresh_from_db()
    assert s.schedule_next_run > now()
    assert s.error_counter == 1
    assert len(djmail.outbox) == 0  # no mails sent to inactive user


@pytest.mark.django_db
@freeze_time("2023-01-18 03:00:00+01:00")
def test_event_fail_user_no_permission(event, user, team):
    djmail.outbox = []
    s = ScheduledEventExport(event=event, owner=user)
    s.export_identifier = "orderlist"
    s.schedule_rrule = "DTSTART:20230118T000000\nRRULE:FREQ=DAILY;INTERVAL=1;WKST=MO"
    s.schedule_rrule_time = time(2, 30, 0)
    s.schedule_next_run = now() - timedelta(minutes=5)
    s.error_counter = 0
    s.save()

    team.all_event_permissions = False
    team.limit_event_permissions = {"event.vouchers:read": True}
    team.save()

    run_scheduled_exports(None)
    s.refresh_from_db()
    assert s.schedule_next_run > now()
    assert s.error_counter == 1
    assert len(djmail.outbox) == 1
    assert djmail.outbox[0].subject == "Scheduled export failed"
    assert "Reason: Export type not found or permission denied." in djmail.outbox[0].body
    assert djmail.outbox[0].to == [user.email]


@pytest.mark.django_db(transaction=True)
@freeze_time("2023-01-18 03:00:00+01:00")
def test_event_ok(event, user, team):
    djmail.outbox = []
    s = ScheduledEventExport(event=event, owner=user)
    s.export_identifier = "orderlist"
    s.export_form_data = {"_format": "xlsx", "paid_only": False}
    s.mail_additional_recipients = "boss@example.org,boss@example.net"
    s.mail_additional_recipients_cc = "assistant@example.net"
    s.mail_additional_recipients_bcc = "archive@example.net"
    s.mail_subject = "Report 1"
    s.mail_template = "Here is the report."
    s.schedule_rrule = "DTSTART:20230118T000000\nRRULE:FREQ=DAILY;INTERVAL=1;WKST=MO"
    s.schedule_rrule_time = time(2, 30, 0)
    s.schedule_next_run = now() - timedelta(minutes=5)
    s.error_counter = 1
    s.save()

    run_scheduled_exports(None)
    s.refresh_from_db()
    assert s.schedule_next_run > now()
    assert s.error_counter == 0
    assert len(djmail.outbox) == 1
    assert djmail.outbox[0].subject == "Report 1"
    assert "Here is the report." in djmail.outbox[0].body
    assert djmail.outbox[0].to == ["boss@example.org", "boss@example.net"]
    assert djmail.outbox[0].cc == ["assistant@example.net", user.email]
    assert djmail.outbox[0].bcc == ["archive@example.net"]
    assert len(djmail.outbox[0].attachments) == 1
    assert djmail.outbox[0].attachments[0][0] == "dummy_orders.xlsx"


@pytest.mark.django_db
@freeze_time("2023-01-18 03:00:00+01:00")
def test_organizer_run_sets_new_time(event, user):
    s = ScheduledOrganizerExport(organizer=event.organizer, owner=user, timezone="Europe/Berlin")
    s.schedule_rrule = "DTSTART:20230118T000000\nRRULE:FREQ=DAILY;INTERVAL=1;WKST=MO"
    s.schedule_rrule_time = time(2, 30, 0)
    s.schedule_next_run = now() - timedelta(minutes=5)
    s.save()

    run_scheduled_exports(None)
    s.refresh_from_db()
    assert s.schedule_next_run == datetime(2023, 1, 19, 2, 30, 0, tzinfo=event.timezone)


@pytest.mark.django_db
@freeze_time("2023-01-18 03:00:00+01:00")
def test_organizer_not_run_when_failed_5_times(event, user):
    s = ScheduledOrganizerExport(organizer=event.organizer, owner=user)
    s.schedule_rrule = "DTSTART:20230118T000000\nRRULE:FREQ=DAILY;INTERVAL=1;WKST=MO"
    s.schedule_rrule_time = time(2, 30, 0)
    s.schedule_next_run = datetime(2023, 1, 18, 2, 30, 0, tzinfo=event.timezone)
    s.error_counter = 5
    s.save()

    run_scheduled_exports(None)
    s.refresh_from_db()
    assert s.schedule_next_run == datetime(2023, 1, 18, 2, 30, 0, tzinfo=event.timezone)


@pytest.mark.django_db
@freeze_time("2023-01-18 03:00:00+01:00")
def test_organizer_fail_invalid_config(event, user):
    djmail.outbox = []
    s = ScheduledOrganizerExport(organizer=event.organizer, owner=user)
    s.export_identifier = " invalid "
    s.schedule_rrule = "DTSTART:20230118T000000\nRRULE:FREQ=DAILY;INTERVAL=1;WKST=MO"
    s.schedule_rrule_time = time(2, 30, 0)
    s.schedule_next_run = now() - timedelta(minutes=5)
    s.error_counter = 0
    s.save()

    run_scheduled_exports(None)
    s.refresh_from_db()
    assert s.schedule_next_run > now()
    assert s.error_counter == 1
    assert len(djmail.outbox) == 1
    assert djmail.outbox[0].subject == "Scheduled export failed"
    assert "Reason: Export type not found" in djmail.outbox[0].body
    assert djmail.outbox[0].to == [user.email]


@pytest.mark.django_db
@freeze_time("2023-01-18 03:00:00+01:00")
def test_organizer_fail_user_inactive(event, user):
    djmail.outbox = []
    s = ScheduledOrganizerExport(organizer=event.organizer, owner=user)
    s.export_identifier = "orderlist"
    s.schedule_rrule = "DTSTART:20230118T000000\nRRULE:FREQ=DAILY;INTERVAL=1;WKST=MO"
    s.schedule_rrule_time = time(2, 30, 0)
    s.schedule_next_run = now() - timedelta(minutes=5)
    s.error_counter = 0
    s.save()

    user.is_active = False
    user.save()

    run_scheduled_exports(None)
    s.refresh_from_db()
    assert s.schedule_next_run > now()
    assert s.error_counter == 1
    assert len(djmail.outbox) == 0  # no mails sent to inactive user


@pytest.mark.django_db
@freeze_time("2023-01-18 03:00:00+01:00")
def test_organizer_fail_user_does_not_have_specific_permission(event, user, team):
    djmail.outbox = []
    s = ScheduledOrganizerExport(organizer=event.organizer, owner=user)
    s.export_identifier = "customerlist"
    s.schedule_rrule = "DTSTART:20230118T000000\nRRULE:FREQ=DAILY;INTERVAL=1;WKST=MO"
    s.schedule_rrule_time = time(2, 30, 0)
    s.schedule_next_run = now() - timedelta(minutes=5)
    s.error_counter = 0
    s.save()

    team.all_event_permissions = False
    team.limit_event_permissions = {"organizer.giftcards:write": True}
    team.save()

    run_scheduled_exports(None)
    s.refresh_from_db()
    assert s.schedule_next_run > now()
    assert s.error_counter == 1
    assert len(djmail.outbox) == 1
    assert djmail.outbox[0].subject == "Scheduled export failed"
    assert "Reason: Export type not found or permission denied." in djmail.outbox[0].body
    assert djmail.outbox[0].to == [user.email]


@pytest.mark.django_db(transaction=True)
@freeze_time("2023-01-18 03:00:00+01:00")
def test_organizer_limited_to_events(event, user, team):
    djmail.outbox = []
    s = ScheduledOrganizerExport(organizer=event.organizer, owner=user)
    s.export_identifier = "eventdata"
    s.export_form_data = {"_format": "default", "all_events": True}
    s.mail_subject = "Report 1"
    s.mail_template = "Here is the report."
    s.schedule_rrule = "DTSTART:20230118T000000\nRRULE:FREQ=DAILY;INTERVAL=1;WKST=MO"
    s.schedule_rrule_time = time(2, 30, 0)
    s.schedule_next_run = now() - timedelta(minutes=5)
    s.error_counter = 0
    s.save()

    event2 = Event.objects.create(
        organizer=event.organizer, name='Dummy', slug='dummy2',
        date_from=datetime(2023, 1, 19, 2, 30, 0, tzinfo=timezone.utc),
        plugins='pretix.plugins.banktransfer'
    )
    team.all_events = False
    team.save()
    team.limit_events.add(event2)

    run_scheduled_exports(None)
    s.refresh_from_db()
    assert s.schedule_next_run > now()
    assert s.error_counter == 0
    assert len(djmail.outbox) == 1
    assert djmail.outbox[0].subject == "Report 1"
    assert "Here is the report." in djmail.outbox[0].body
    assert djmail.outbox[0].to == [user.email]
    assert len(djmail.outbox[0].attachments) == 1
    assert djmail.outbox[0].attachments[0][0] == "dummy_events.csv"
    assert len(djmail.outbox[0].attachments[0][1].splitlines()) == 2


@pytest.mark.django_db(transaction=True)
@freeze_time("2023-01-18 03:00:00+01:00")
def test_organizer_ok(event, user, team):
    djmail.outbox = []
    s = ScheduledOrganizerExport(organizer=event.organizer, owner=user)
    s.export_identifier = "eventdata"
    s.export_form_data = {"_format": "default", "all_events": True}
    s.mail_additional_recipients = "boss@example.org,boss@example.net"
    s.mail_additional_recipients_cc = "assistant@example.net"
    s.mail_additional_recipients_bcc = "archive@example.net"
    s.mail_subject = "Report 1"
    s.mail_template = "Here is the report."
    s.schedule_rrule = "DTSTART:20230118T000000\nRRULE:FREQ=DAILY;INTERVAL=1;WKST=MO"
    s.schedule_rrule_time = time(2, 30, 0)
    s.schedule_next_run = now() - timedelta(minutes=5)
    s.error_counter = 1
    s.save()

    Event.objects.create(
        organizer=event.organizer, name='Dummy', slug='dummy2',
        date_from=datetime(2023, 1, 19, 2, 30, 0, tzinfo=timezone.utc),
        plugins='pretix.plugins.banktransfer'
    )

    run_scheduled_exports(None)
    s.refresh_from_db()
    assert s.schedule_next_run > now()
    assert s.error_counter == 0
    assert len(djmail.outbox) == 1
    assert djmail.outbox[0].subject == "Report 1"
    assert "Here is the report." in djmail.outbox[0].body
    assert djmail.outbox[0].to == ["boss@example.org", "boss@example.net"]
    assert djmail.outbox[0].cc == ["assistant@example.net", user.email]
    assert djmail.outbox[0].bcc == ["archive@example.net"]
    assert len(djmail.outbox[0].attachments) == 1
    assert djmail.outbox[0].attachments[0][0] == "dummy_events.csv"
    assert len(djmail.outbox[0].attachments[0][1].splitlines()) == 3


# ---------------------------------------------------------------------------
# Recoverable, idempotent scheduled export executions
# ---------------------------------------------------------------------------


def _make_event_schedule(event, user, **kwargs):
    s = ScheduledEventExport(event=event, owner=user)
    s.export_identifier = kwargs.get('identifier', 'orderlist')
    s.export_form_data = kwargs.get('form_data', {'_format': 'orders:default'})
    s.mail_subject = kwargs.get('subject', 'Report 1')
    s.mail_template = kwargs.get('template', 'Here is the report.')
    s.mail_additional_recipients = kwargs.get('to', '')
    s.schedule_rrule = "DTSTART:20230118T000000\nRRULE:FREQ=DAILY;INTERVAL=1;WKST=MO"
    s.schedule_rrule_time = time(2, 30, 0)
    s.schedule_next_run = now() - timedelta(minutes=5)
    s.save()
    return s


def _run_event_task(event, schedule, execution):
    scheduled_event_export.apply(kwargs={
        'event': event.pk,
        'schedule': schedule.pk,
        'execution': execution.pk,
    })


@pytest.mark.django_db(transaction=True)
@freeze_time("2023-01-18 03:00:00+01:00")
def test_event_scan_creates_unique_execution(event, user):
    djmail.outbox = []
    s = _make_event_schedule(event, user)
    due = s.schedule_next_run

    run_scheduled_exports(None)
    s.refresh_from_db()

    executions = ScheduledEventExportExecution.objects.filter(schedule=s)
    assert executions.count() == 1
    ex = executions.get()
    assert ex.status in (ex.STATUS_MAIL_QUEUED, ex.STATUS_COMPLETED)
    assert ex.scheduled_for == due
    assert ex.manual is False
    # configuration is frozen on the execution
    assert ex.export_identifier == 'orderlist'
    assert ex.export_form_data == {'_format': 'orders:default'}
    assert ex.timezone_name == 'Europe/Berlin'
    assert ex.mail_subject == 'Report 1'
    assert ex.file is not None
    assert ex.mail is not None
    assert s.schedule_next_run == datetime(2023, 1, 19, 2, 30, 0, tzinfo=event.timezone)
    assert len(djmail.outbox) == 1


@pytest.mark.django_db(transaction=True)
@freeze_time("2023-01-18 03:00:00+01:00")
def test_event_scan_does_not_duplicate_existing_slot_execution(event, user):
    djmail.outbox = []
    s = _make_event_schedule(event, user)
    due = s.schedule_next_run

    run_scheduled_exports(None)
    # Simulate a repeated scan where the next_run advancement was lost but the
    # execution was committed: the same slot must not be executed twice.
    ScheduledEventExport.objects.filter(pk=s.pk).update(schedule_next_run=due)
    run_scheduled_exports(None)

    assert ScheduledEventExportExecution.objects.filter(schedule=s).count() == 1
    assert len(djmail.outbox) == 1
    assert ScheduledEventExportExecution.objects.filter(
        schedule=s, mail__isnull=False
    ).count() == 1


@pytest.mark.django_db(transaction=True)
@freeze_time("2023-01-18 03:00:00+01:00")
def test_event_execution_config_is_frozen(event, user):
    djmail.outbox = []
    s = _make_event_schedule(event, user, subject='Frozen subject', to='boss@example.org')

    ex = ScheduledEventExportExecution(schedule=s, scheduled_for=s.schedule_next_run)
    ex.freeze_from_schedule(s)
    ex.save()

    # Change the schedule after the slot was created
    s.mail_subject = 'Changed subject'
    s.mail_additional_recipients = 'someone-else@example.org'
    s.save()

    _run_event_task(event, s, ex)
    ex.refresh_from_db()

    assert len(djmail.outbox) == 1
    assert djmail.outbox[0].subject == 'Frozen subject'
    assert djmail.outbox[0].to == ['boss@example.org']
    assert ex.mail is not None


@pytest.mark.django_db(transaction=True)
@freeze_time("2023-01-18 03:00:00+01:00")
def test_event_manual_execution_is_distinct(event, user):
    djmail.outbox = []
    s = _make_event_schedule(event, user)

    manual_ex = create_manual_execution(ScheduledEventExportExecution, s)
    assert manual_ex.scheduled_for is None
    assert manual_ex.manual is True

    # A second manual run can coexist and does not collide with the first or with slots
    manual_ex_2 = create_manual_execution(ScheduledEventExportExecution, s)
    assert manual_ex_2.pk != manual_ex.pk

    _run_event_task(event, s, manual_ex)

    # The scheduled slot is still due and untouched
    assert ScheduledEventExport.objects.get(pk=s.pk).schedule_next_run < now()
    assert ScheduledEventExportExecution.objects.filter(
        schedule=s, scheduled_for__isnull=False
    ).count() == 0
    manual_ex.refresh_from_db()
    assert manual_ex.status in (manual_ex.STATUS_MAIL_QUEUED, manual_ex.STATUS_COMPLETED)
    assert len(djmail.outbox) == 1


@pytest.mark.django_db(transaction=True)
@freeze_time("2023-01-18 03:00:00+01:00")
def test_event_takeover_rendering_reuses_cachedfile(event, user):
    djmail.outbox = []
    s = _make_event_schedule(event, user)

    cf = CachedFile(web_download=False)
    cf.date = now()
    cf.expires = now() + timedelta(hours=24)
    cf.save()

    ex = ScheduledEventExportExecution(schedule=s, scheduled_for=s.schedule_next_run)
    ex.freeze_from_schedule(s)
    ex.status = ex.STATUS_RENDERING
    ex.started = now()
    ex.file = cf
    ex.save()

    _run_event_task(event, s, ex)
    ex.refresh_from_db()
    cf.refresh_from_db()

    # The same CachedFile identity was reused and now holds the result
    assert ex.file_id == cf.pk
    assert bool(cf.file)
    assert cf.filename == 'dummy_orders.csv'
    assert ex.status in (ex.STATUS_MAIL_QUEUED, ex.STATUS_COMPLETED)
    assert ex.mail is not None
    assert len(djmail.outbox) == 1
    assert OutgoingMail.objects.filter(should_attach_cached_files=cf).count() == 1


@pytest.mark.django_db(transaction=True)
@freeze_time("2023-01-18 03:00:00+01:00")
def test_event_takeover_file_ready_does_not_render_or_mail_twice(event, user):
    djmail.outbox = []
    s = _make_event_schedule(event, user)

    cf = CachedFile(web_download=False, filename='dummy_orders.csv', type='text/csv')
    cf.date = now()
    cf.expires = now() + timedelta(hours=24)
    cf.save()
    cf.file.save(cachedfile_name(cf, 'dummy_orders.csv'), ContentFile(b'Code,Status\nA,paid\n'))

    ex = ScheduledEventExportExecution(schedule=s, scheduled_for=s.schedule_next_run)
    ex.freeze_from_schedule(s)
    ex.status = ex.STATUS_FILE_READY
    ex.file = cf
    ex.save()

    # Two redelivered tasks / concurrent workers for the same execution
    _run_event_task(event, s, ex)
    _run_event_task(event, s, ex)
    ex.refresh_from_db()

    assert ex.file_id == cf.pk
    assert ex.status == ex.STATUS_COMPLETED
    assert OutgoingMail.objects.filter(pk=ex.mail_id).count() == 1
    assert ScheduledEventExportExecution.objects.filter(
        schedule=s, scheduled_for=s.schedule_next_run
    ).count() == 1
    assert len(djmail.outbox) == 1
    assert djmail.outbox[0].attachments[0][1] == 'Code,Status\nA,paid\n'
    # the success log was written exactly once
    success_logs = LogEntry.objects.filter(
        event=event, action_type='pretix.event.export.schedule.executed',
    )
    assert sum(1 for l in success_logs if (l.parsed_data or {}).get('execution') == ex.pk) == 1


@pytest.mark.django_db(transaction=True)
@freeze_time("2023-01-18 03:00:00+01:00")
def test_event_takeover_mail_queued_only_continues_existing_mail(event, user):
    djmail.outbox = []
    s = _make_event_schedule(event, user)

    cf = CachedFile(web_download=False, filename='dummy_orders.csv', type='text/csv')
    cf.date = now()
    cf.expires = now() + timedelta(hours=24)
    cf.save()
    cf.file.save(cachedfile_name(cf, 'dummy_orders.csv'), ContentFile(b'Code,Status\n'))

    ex = ScheduledEventExportExecution(schedule=s, scheduled_for=s.schedule_next_run)
    ex.freeze_from_schedule(s)
    ex.status = ex.STATUS_FILE_READY
    ex.file = cf
    ex.save()

    _run_event_task(event, s, ex)
    ex.refresh_from_db()
    m = ex.mail
    assert m is not None
    mail_count_before = OutgoingMail.objects.count()

    # A redelivery after the OutgoingMail was formed must never create another one
    _run_event_task(event, s, ex)
    ex.refresh_from_db()
    assert ex.status == ex.STATUS_COMPLETED
    assert OutgoingMail.objects.count() == mail_count_before
    assert ex.mail_id == m.pk
    assert len(djmail.outbox) == 1


@pytest.mark.django_db(transaction=True)
@freeze_time("2023-01-18 03:00:00+01:00")
def test_event_failure_settled_once_with_reason(event, user):
    djmail.outbox = []
    s = _make_event_schedule(event, user, identifier=' invalid ')

    ex = ScheduledEventExportExecution(schedule=s, scheduled_for=s.schedule_next_run)
    ex.freeze_from_schedule(s)
    ex.save()

    _run_event_task(event, s, ex)
    _run_event_task(event, s, ex)  # redelivery must not settle twice
    ex.refresh_from_db()
    s.refresh_from_db()

    assert ex.status == ex.STATUS_FAILED
    assert ex.error_code == ex.ERROR_CONFIG
    assert ex.error_message
    assert ex.soft_failed is False
    assert s.error_counter == 1
    assert len(djmail.outbox) == 1
    assert "Export type not found" in djmail.outbox[0].body
    failure_logs = LogEntry.objects.filter(
        event=event, action_type='pretix.event.export.schedule.failed',
    )
    assert sum(1 for l in failure_logs if (l.parsed_data or {}).get('execution') == ex.pk) == 1


@pytest.mark.django_db(transaction=True)
@freeze_time("2023-01-18 03:00:00+01:00")
def test_event_permission_failure_has_distinct_reason(event, user, team):
    s = _make_event_schedule(event, user, identifier='orderlist')
    team.all_event_permissions = False
    team.limit_event_permissions = {"event.vouchers:read": True}
    team.save()

    ex = ScheduledEventExportExecution(schedule=s, scheduled_for=s.schedule_next_run)
    ex.freeze_from_schedule(s)
    ex.save()

    _run_event_task(event, s, ex)
    ex.refresh_from_db()
    s.refresh_from_db()

    assert ex.status == ex.STATUS_FAILED
    assert ex.error_code == ex.ERROR_PERMISSION
    assert s.error_counter == 1


@pytest.mark.django_db(transaction=True)
@freeze_time("2023-01-18 03:00:00+01:00")
def test_event_empty_result_is_soft_failure(event, user):
    djmail.outbox = []
    # No invoices exist, so the invoice exporter renders an empty result.
    s = _make_event_schedule(event, user, identifier='invoices', form_data={'_format': 'zip'})

    ex = ScheduledEventExportExecution(schedule=s, scheduled_for=s.schedule_next_run)
    ex.freeze_from_schedule(s)
    ex.save()

    _run_event_task(event, s, ex)
    ex.refresh_from_db()
    s.refresh_from_db()

    assert ex.status == ex.STATUS_FAILED
    assert ex.error_code == ex.ERROR_EMPTY
    assert ex.soft_failed is True
    assert s.error_counter == 0  # soft failures do not count towards the pause threshold
    assert len(djmail.outbox) == 1  # the owner is still notified once


@pytest.mark.django_db(transaction=True)
@freeze_time("2023-01-18 03:00:00+01:00")
def test_event_paused_schedule_does_not_dispatch_new_slots(event, user):
    s = _make_event_schedule(event, user)
    s.error_counter = 5
    s.save(update_fields=['error_counter'])

    run_scheduled_exports(None)
    s.refresh_from_db()
    assert s.schedule_next_run < now()  # still due, but no execution created
    assert ScheduledEventExportExecution.objects.filter(schedule=s).count() == 0


@pytest.mark.django_db(transaction=True)
@freeze_time("2023-01-18 03:00:00+01:00")
def test_event_recovery_takes_over_stale_waiting_execution(event, user):
    djmail.outbox = []
    s = _make_event_schedule(event, user)
    ex = ScheduledEventExportExecution(schedule=s, scheduled_for=s.schedule_next_run)
    ex.freeze_from_schedule(s)
    ex.save()
    # Simulate the scheduler dying after committing the execution, before publishing
    ScheduledEventExportExecution.objects.filter(pk=ex.pk).update(
        created=now() - timedelta(minutes=30),
    )

    _recover_scheduled_export_executions()
    ex.refresh_from_db()

    assert ex.status in (ex.STATUS_MAIL_QUEUED, ex.STATUS_COMPLETED)
    assert ex.mail is not None
    assert len(djmail.outbox) == 1


@pytest.mark.django_db(transaction=True)
@freeze_time("2023-01-18 03:00:00+01:00")
def test_event_successful_execution_is_not_resend_after_fix(event, user):
    djmail.outbox = []
    s = _make_event_schedule(event, user)
    ex = ScheduledEventExportExecution(schedule=s, scheduled_for=s.schedule_next_run)
    ex.freeze_from_schedule(s)
    ex.save()

    _run_event_task(event, s, ex)
    # The execution succeeded; later recovery runs must not resend it
    _recover_scheduled_export_executions()
    _recover_scheduled_export_executions()

    assert len(djmail.outbox) == 1
    assert ScheduledEventExportExecution.objects.filter(
        schedule=s, mail__isnull=False
    ).count() == 1


@pytest.mark.django_db(transaction=True)
@freeze_time("2023-01-18 03:00:00+01:00")
def test_event_non_terminal_files_are_not_cleaned(event, user):
    s = _make_event_schedule(event, user)
    cf = CachedFile(web_download=False, filename='dummy_orders.csv', type='text/csv')
    cf.date = now() - timedelta(hours=25)
    cf.expires = now() - timedelta(hours=1)
    cf.save()

    ex = ScheduledEventExportExecution(schedule=s, scheduled_for=s.schedule_next_run)
    ex.freeze_from_schedule(s)
    ex.status = ex.STATUS_RENDERING
    ex.started = now()
    ex.file = cf
    ex.save()

    clean_cached_files(None)
    assert CachedFile.objects.filter(pk=cf.pk).exists()

    # Terminal executions fall back to the ordinary retention rules again
    ScheduledEventExportExecution.objects.filter(pk=ex.pk).update(status=ex.STATUS_FAILED)
    clean_cached_files(None)
    assert not CachedFile.objects.filter(pk=cf.pk).exists()
