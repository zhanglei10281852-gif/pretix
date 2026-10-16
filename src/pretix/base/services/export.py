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
import logging
import sys
from datetime import timedelta
from typing import Any, Dict

from celery.exceptions import MaxRetriesExceededError
from django.conf import settings
from django.core.files.base import ContentFile
from django.db import close_old_connections, connection, transaction
from django.dispatch import receiver
from django.utils.timezone import now, override
from django.utils.translation import gettext
from django_scopes import scopes_disabled
from i18nfield.strings import LazyI18nString

from pretix.base.email import get_email_context
from pretix.base.exporter import BaseExporter, OrganizerLevelExportMixin
from pretix.base.i18n import LazyLocaleException, language
from pretix.base.models import (
    CachedFile, Device, Event, Organizer, ScheduledEventExport,
    ScheduledEventExportExecution, ScheduledOrganizerExportExecution,
    TeamAPIToken, User, cachedfile_name,
)
from pretix.base.models.auth import UserWithStaffSession
from pretix.base.models.exports import (
    SCHEDULED_EXPORT_RECOVERY_DELAYS, ScheduledOrganizerExport,
)
from pretix.base.models.mail import OutgoingMail
from pretix.base.services.mail import mail, mail_send_task
from pretix.base.services.tasks import (
    EventTask, OrganizerTask, ProfiledEventTask, ProfiledOrganizerUserTask,
)
from pretix.base.signals import (
    periodic_task, register_data_exporters, register_multievent_data_exporters,
)
from pretix.celery_app import app
from pretix.helpers import OF_SELF, repeatable_reads_transaction
from pretix.helpers.urls import mainreverse_absolute

logger = logging.getLogger(__name__)

# Holds render tracebacks of internal failures settled in eager mode (unit tests).
# When openpyxl write-only rendering fails (e.g. an I/O error), it leaves suspended
# lxml generators behind whose garbage collection would otherwise pollute the test
# session. In production Celery keeps the traceback chain alive while handling the
# retry; we emulate that lifecycle here by delaying the cleanup to interpreter shutdown.
_eager_internal_error_tracebacks = []


class ExportError(LazyLocaleException):
    pass


class ExportEmptyError(ExportError):
    pass


@app.task(base=ProfiledEventTask, throws=(ExportError, ExportEmptyError), bind=True)
def export(self, event: Event, user: User, device: int, token: int, fileid: str, provider: str,
           form_data: Dict[str, Any], staff_session=False) -> None:
    if user:
        user = User.objects.get(pk=user)
    if device:
        device = Device.objects.get(pk=device)
    if token:
        device = TeamAPIToken.objects.get(pk=token)

    def set_progress(val):
        if not self.request.called_directly:
            self.update_state(
                state='PROGRESS',
                meta={'value': val}
            )

    ex = init_event_exporter(
        identifier=provider,
        event=event,
        user=user,
        token=token,
        device=device,
        staff_session=staff_session,
        progress_callback=set_progress,
    )
    if not ex:
        raise ExportError(
            gettext('Export not found or you do not have sufficient permission to perform this export.')
        )

    file = CachedFile.objects.get(id=fileid)
    with language(event.settings.locale, event.settings.region), override(event.settings.timezone):
        if ex.repeatable_read:
            with repeatable_reads_transaction():
                d = ex.render(form_data)
        else:
            d = ex.render(form_data)

        if d is None:
            raise ExportError(
                gettext('Your export did not contain any data.')
            )
        file.filename, file.type, data = d

        close_old_connections()  # This task can run very long, we might need a new DB connection

        f = ContentFile(data)
        file.file.save(cachedfile_name(file, file.filename), f)
    return str(file.pk)


@app.task(base=ProfiledOrganizerUserTask, throws=(ExportError, ExportEmptyError), bind=True)
def multiexport(self, organizer: Organizer, user: User, device: int, token: int, fileid: str, provider: str,
                form_data: Dict[str, Any], staff_session=False) -> None:
    if device:
        device = Device.objects.get(pk=device)
    if token:
        token = TeamAPIToken.objects.get(pk=token)

    def set_progress(val):
        if not self.request.called_directly:
            self.update_state(
                state='PROGRESS',
                meta={'value': val}
            )

    file = CachedFile.objects.get(id=fileid)

    event_qs = organizer.events.all()
    if form_data.get('events') is not None and not form_data.get('all_events'):
        if form_data['events'] and isinstance(form_data['events'][0], str):  # legacy API-created schedules
            event_qs = event_qs.filter(slug__in=form_data.get('events'))
        else:
            event_qs = event_qs.filter(pk__in=form_data.get('events'))

    ex = init_organizer_exporter(
        identifier=provider,
        organizer=organizer,
        user=user,
        token=token,
        device=device,
        staff_session=staff_session,
        progress_callback=set_progress,
        event_qs=event_qs,
    )
    if not ex:
        raise ExportError(
            gettext('Export not found or you do not have sufficient permission to perform this export.')
        )

    if user:
        locale = user.locale
        timezone = user.timezone
        region = None  # todo: add to user?
    else:
        e = ex.events.first()
        if e:
            locale = e.settings.locale
            timezone = e.settings.timezone
            region = e.settings.region
        else:
            locale = organizer.settings.locale or settings.LANGUAGE_CODE
            timezone = organizer.settings.timezone or settings.TIME_ZONE
            region = organizer.settings.region
    with language(locale, region), override(timezone):
        if ex.repeatable_read:
            with repeatable_reads_transaction():
                d = ex.render(form_data)
        else:
            d = ex.render(form_data)
        if d is None:
            raise ExportError(
                gettext('Your export did not contain any data.')
            )
        file.filename, file.type, data = d

        close_old_connections()  # This task can run very long, we might need a new DB connection

        f = ContentFile(data)
        file.file.save(cachedfile_name(file, file.filename), f)
    return str(file.pk)


def init_event_exporter(identifier, **kwargs):
    for ex in init_event_exporters(**kwargs):
        if ex.identifier == identifier:
            return ex
    return None


def init_event_exporters(event, user=None, token=None, device=None, request=None, staff_session=False, **kwargs):
    if not user and not token and not device:
        raise ValueError("No auth source given.")
    perm_holder = device or token or user

    responses = register_data_exporters.send(event)
    for r, response in responses:
        if not response:
            continue

        if issubclass(response, OrganizerLevelExportMixin):
            raise TypeError("Cannot user organizer-level exporter on event level")

        permission_name = response.get_required_event_permission()
        if not perm_holder.has_event_permission(event.organizer, event, permission_name, request) and not staff_session:
            continue

        exporter: BaseExporter = response(
            event=event,
            organizer=event.organizer,
            permission_holder=token or device or (UserWithStaffSession(user) if staff_session else user),
            **kwargs
        )

        if not exporter.available_for_user(user if user and user.is_authenticated else None):
            continue

        yield exporter


def init_organizer_exporter(identifier, **kwargs):
    for ex in init_organizer_exporters(**kwargs):
        if ex.identifier == identifier:
            return ex
    return None


def init_organizer_exporters(
    organizer, user=None, token=None, device=None, request=None, staff_session=False, event_qs=None, **kwargs
):
    if not user and not token and not device:
        raise ValueError("No auth source given.")
    perm_holder = device or token or user

    _event_list_cache = {}
    _has_permission_on_any_team_cache = {}
    _team_cache = None

    responses = register_multievent_data_exporters.send(organizer)
    for r, response in responses:
        if not response:
            continue

        if issubclass(response, OrganizerLevelExportMixin):
            exporter: BaseExporter = response(
                event=Event.objects.none(),
                organizer=organizer,
                permission_holder=token or device or (UserWithStaffSession(user) if staff_session else user),
                **kwargs,
            )

            try:
                if not perm_holder.has_organizer_permission(organizer, response.get_required_organizer_permission(), request) and not staff_session:
                    continue
            except NotImplementedError:
                logger.error(f"Not showing export {response} because get_required_organizer_permission() is not implemented.")
                continue

        else:
            permission_name = response.get_required_event_permission()

            if permission_name not in _event_list_cache:
                if staff_session:
                    events = event_qs.all() if event_qs else organizer.events.all()
                elif event_qs is not None:
                    events = event_qs.filter(
                        pk__in=perm_holder.get_events_with_permission(
                            permission_name, request=request
                        ).filter(
                            organizer=organizer
                        ).values("id")
                    )
                else:
                    events = perm_holder.get_events_with_permission(
                        permission_name, request=request
                    ).filter(
                        organizer=organizer
                    )

                _event_list_cache[permission_name] = events

            if permission_name not in _has_permission_on_any_team_cache:
                # Check if the user has this event permission on any teams they are part of to decide whether to show
                # the export at all.
                # This is different from _event_list_cache[permission_name].exists() for the case of an organizer with
                # zero events in total, or a team with zero events. In these cases, we still want people to be able
                # to see waht exports they'll get once they have events.
                if user:
                    if _team_cache is None:
                        _team_cache = list(user.teams.filter(organizer=organizer))
                    _has_permission_on_any_team_cache[permission_name] = staff_session or any(
                        t.has_event_permission(permission_name) for t in _team_cache
                    )
                elif token:
                    _has_permission_on_any_team_cache[permission_name] = token.team.has_event_permission(permission_name)
                elif device:
                    _has_permission_on_any_team_cache[permission_name] = device.has_event_permission(permission_name)

            if not _has_permission_on_any_team_cache[permission_name] and not staff_session:
                continue

            exporter: BaseExporter = response(
                event=_event_list_cache[permission_name],
                organizer=organizer,
                permission_holder=token or device or (UserWithStaffSession(user) if staff_session else user),
                **kwargs,
            )

        if not exporter.available_for_user(user if user and user.is_authenticated else None):
            continue

        yield exporter


def _locked(execution):
    """Reload an execution row with a row lock, so concurrent workers/takeovers
    can only observe and advance the very same execution."""
    return execution.__class__.objects.select_for_update(of=OF_SELF).get(pk=execution.pk)


def _after_commit(func):
    # In unit tests, transaction.on_commit() never fires because tests run inside a
    # transaction that is rolled back. The locmem email backend is a reliable indicator
    # for this, this mirrors the behaviour in pretix.base.services.mail.
    if 'locmem' in settings.EMAIL_BACKEND:
        func()
    else:
        transaction.on_commit(func)


def dispatch_scheduled_export_task(task, kwargs, queue=None):
    def _publish():
        if queue:
            task.apply_async(kwargs=kwargs, queue=queue)
        else:
            task.apply_async(kwargs=kwargs)

    _after_commit(_publish)


def _enqueue_mail_send(mail_id):
    def _publish():
        mail_send_task.apply_async(kwargs={"outgoing_mail": mail_id})

    _after_commit(_publish)


def create_manual_execution(execution_model, schedule):
    """Create a distinct execution for an admin-triggered "Run now".

    It never occupies or duplicates a pending scheduled slot (``scheduled_for`` is
    None and ``manual`` is True).
    """
    execution = execution_model(
        schedule=schedule,
        scheduled_for=None,
        manual=True,
    )
    execution.freeze_from_schedule(schedule)
    execution.save()
    return execution


def _continue_mail(execution):
    """An OutgoingMail has already been formed for this execution. We must never
    generate another attachment or email, we only continue the existing mail's own
    sending flow and reconcile the execution's state with it."""
    m = execution.mail
    if m is None:
        return
    if m.status in (OutgoingMail.STATUS_SENT, OutgoingMail.STATUS_BOUNCED):
        execution.status = execution.STATUS_COMPLETED
        execution.finished = now()
        execution.save(update_fields=['status', 'finished'])
    elif m.status in (OutgoingMail.STATUS_FAILED, OutgoingMail.STATUS_ABORTED):
        execution.status = execution.STATUS_FAILED
        execution.error_code = execution.ERROR_MAIL
        execution.error_message = m.error
        execution.finished = now()
        execution.save(update_fields=['status', 'error_code', 'error_message', 'finished'])
    elif m.status in (OutgoingMail.STATUS_QUEUED, OutgoingMail.STATUS_AWAITING_RETRY):
        # The email subsystem owns all delivery retries, but make sure a send task
        # actually exists (it is idempotent due to its own locking/state checks).
        _enqueue_mail_send(m.pk)
    # STATUS_INFLIGHT and STATUS_WITHHELD are handled by the mail subsystem.


def _settle_failure(schedule, context, execution, error_code, message, config_url, soft=False):
    """Move an execution to its failed terminal state. Log entry, failure notification
    and error counter are settled exactly once per execution."""
    with transaction.atomic():
        execution = _locked(execution)
        if execution.is_terminal:
            return  # already settled (success or failure)

        execution.status = execution.STATUS_FAILED
        execution.error_code = error_code
        execution.error_message = message
        execution.soft_failed = soft
        execution.finished = now()
        execution.save(update_fields=[
            'status', 'error_code', 'error_message', 'soft_failed', 'finished',
        ])

        if isinstance(context, Event):
            failed_action = 'pretix.event.export.schedule.failed'
        else:
            failed_action = 'pretix.organizer.export.schedule.failed'

        context.log_action(
            failed_action,
            data={
                'id': schedule.id,
                'execution': execution.pk,
                'export_identifier': execution.export_identifier,
                'export_form_data': execution.export_form_data,
                'reason': message,
                'soft': soft,
                'error_code': error_code,
            }
        )
        if schedule.owner.is_active:
            mail(
                email=schedule.owner.email,
                subject=gettext('Scheduled export failed'),
                template='pretixbase/email/export_failed.txt',
                context={
                    'configuration_url': config_url,
                    'reason': message,
                    'soft': soft,
                    'instance': settings.PRETIX_INSTANCE_NAME,
                },
                event=context if isinstance(context, Event) else None,
                organizer=context.organizer if isinstance(context, Event) else context,
                locale=execution.locale,
            )
        if not soft:
            schedule.error_counter += 1
            schedule.error_last_message = message
            schedule.save(update_fields=['error_counter', 'error_last_message'])


def _queue_result_mail(schedule, context, execution, filesize=None):
    """Form the OutgoingMail for a file-ready execution. The mail is created at most
    once per execution, in the same transaction as the state transition."""
    with transaction.atomic():
        execution = _locked(execution)
        if execution.is_terminal:
            return
        if execution.status == execution.STATUS_MAIL_QUEUED:
            _continue_mail(execution)
            return
        if execution.status != execution.STATUS_FILE_READY or not execution.file_id:
            # Rendering was interrupted a second time before the file was committed,
            # or the execution failed concurrently – nothing to send yet.
            return

        schedule.error_counter = 0
        schedule.save(update_fields=['error_counter'])

        to = [r for r in execution.mail_additional_recipients.split(",") if r]
        cc = [r for r in execution.mail_additional_recipients_cc.split(",") if r]
        bcc = [r for r in execution.mail_additional_recipients_bcc.split(",") if r]
        if to:
            # If there is an explicit To, the owner is Cc. Otherwise, the owner is To. Yes, this is
            # purely cosmetical and has policital reasons.
            cc.append(schedule.owner.email)
        else:
            to.append(schedule.owner.email)

        m = mail(
            email=to,
            cc=cc,
            bcc=bcc,
            subject=execution.mail_subject,
            template=LazyI18nString(execution.mail_template),
            context=get_email_context(event=context) if isinstance(context, Event) else {},
            event=context if isinstance(context, Event) else None,
            organizer=context.organizer if isinstance(context, Event) else context,
            locale=execution.locale,
            attach_cached_files=[execution.file],
        )
        execution.mail = m
        execution.status = execution.STATUS_MAIL_QUEUED
        execution.save(update_fields=['mail_id', 'status'])

        if filesize is None:
            try:
                filesize = execution.file.file.size
            except Exception:
                filesize = None

        executed_action = (
            'pretix.event.export.schedule.executed'
            if isinstance(context, Event)
            else 'pretix.organizer.export.schedule.executed'
        )
        context.log_action(
            executed_action,
            data={
                'id': schedule.id,
                'execution': execution.pk,
                'mail': m.pk,
                'export_identifier': execution.export_identifier,
                'export_form_data': execution.export_form_data,
                'result_file_size': filesize,
                'result_file_name': execution.file.file.name,
            }
        )


def _event_exporter_is_registered(event, identifier):
    """Check whether an exporter of this identifier exists for the event at all,
    independent of the running user's permissions."""
    for _receiver, response in register_data_exporters.send(event):
        if (
            response
            and not issubclass(response, OrganizerLevelExportMixin)
            and getattr(response, 'identifier', None) == identifier
        ):
            return True
    return False


def _organizer_exporter_is_registered(organizer, identifier):
    """Check whether an exporter of this identifier exists for the organizer at all,
    independent of the running user's permissions."""
    for _receiver, response in register_multievent_data_exporters.send(organizer):
        if response and getattr(response, 'identifier', None) == identifier:
            return True
    return False


def _run_scheduled_export(task, schedule, context, execution, exporter, exporter_registered,
                          has_permission, config_url):
    """Drive one execution through its state machine.

    Every transition is guarded by a row lock on the execution, so a redelivered task
    or a concurrent worker can only take over the same execution instead of producing
    a second file or a second email:

    * interrupted before the result file was saved: render again,
    * result file saved: reuse the very same CachedFile,
    * OutgoingMail formed: only continue that mail's own send flow.
    """
    with language(execution.locale, context.settings.region), override(execution.tz):
        with transaction.atomic():
            execution = _locked(execution)
            if execution.is_terminal:
                return
            if execution.status == execution.STATUS_MAIL_QUEUED:
                _continue_mail(execution)
                return

            if execution.status == execution.STATUS_FILE_READY:
                need_render = False
            else:
                # waiting or rendering: this worker claims/takes over the render stage
                if not has_permission:
                    _settle_failure(
                        schedule, context, execution,
                        execution.ERROR_PERMISSION, gettext('Permission denied.'), config_url,
                    )
                    return
                if not exporter:
                    # The exporter exists but is not returned for this owner: their
                    # permission to run the export was lost. If it does not exist at
                    # all, the stored configuration is invalid.
                    error_code = (
                        execution.ERROR_PERMISSION
                        if exporter_registered
                        else execution.ERROR_CONFIG
                    )
                    _settle_failure(
                        schedule, context, execution,
                        error_code,
                        gettext('Export type not found or permission denied.'), config_url,
                    )
                    return

                execution.status = execution.STATUS_RENDERING
                execution.started = now()
                if not execution.file_id:
                    # Reserve the CachedFile identity before rendering, so a takeover
                    # always reuses the same file instead of leaving an orphan behind.
                    cf = CachedFile(web_download=False)
                    cf.date = now()
                    cf.expires = now() + timedelta(hours=24)
                    cf.save()
                    execution.file = cf
                execution.save(update_fields=['status', 'started', 'file_id'])
                need_render = True

        if need_render:
            try:
                if exporter.repeatable_read:
                    with repeatable_reads_transaction():
                        d = exporter.render(execution.export_form_data)
                else:
                    d = exporter.render(execution.export_form_data)
                if d is None:
                    raise ExportEmptyError(
                        gettext('Your export did not contain any data.')
                    )
                filename, filetype, data = d
                filesize = len(data)
                if filesize > 20 * 1024 * 1024:  # 20 MB
                    raise ExportError(
                        gettext('Your exported data exceeded the size limit for scheduled exports.')
                    )
            except ExportEmptyError as e:
                _settle_failure(
                    schedule, context, execution,
                    execution.ERROR_EMPTY, str(e), config_url, soft=True,
                )
                return
            except ExportError as e:
                _settle_failure(
                    schedule, context, execution,
                    execution.ERROR_CONFIG, str(e), config_url,
                )
                return
            except Exception:
                # The execution stays in "rendering" and can be taken over or retried.
                # We explicitly pass primitive kwargs because the task base class replaces
                # the event/organizer id in kwargs with a model instance during execution.
                logger.exception("Scheduled export failed.")
                retry_kwargs = {'schedule': schedule.pk, 'execution': execution.pk}
                if isinstance(context, Event):
                    retry_kwargs['event'] = context.pk
                else:
                    retry_kwargs['organizer'] = context.pk
                if getattr(task.request, 'is_eager', False):
                    # In eager mode (unit tests), Celery would re-run the task in-process
                    # in a loop instead of scheduling a delayed retry, so settle directly.
                    _settle_failure(
                        schedule, context, execution,
                        execution.ERROR_INTERNAL, 'Internal Error', config_url,
                    )
                    _eager_internal_error_tracebacks.append(sys.exc_info()[2])
                else:
                    try:
                        task.retry(kwargs=retry_kwargs)
                    except MaxRetriesExceededError:
                        _settle_failure(
                            schedule, context, execution,
                            execution.ERROR_INTERNAL, 'Internal Error', config_url,
                        )
                return

            conn = transaction.get_connection()
            if not conn.in_atomic_block:  # atomic execution only happens during tests or with celery always_eager on
                close_old_connections()  # This task can run very long, we might need a new DB connection

            with transaction.atomic():
                execution = _locked(execution)
                if execution.is_terminal:
                    return
                if execution.status == execution.STATUS_MAIL_QUEUED:
                    _continue_mail(execution)
                    return
                if execution.status != execution.STATUS_FILE_READY:
                    execution.file.filename = filename
                    execution.file.type = filetype
                    f = ContentFile(data)
                    execution.file.file.save(cachedfile_name(execution.file, filename), f)
                    execution.status = execution.STATUS_FILE_READY
                    execution.save(update_fields=['status'])
        else:
            filesize = None

        _queue_result_mail(schedule, context, execution, filesize=filesize)


@app.task(base=OrganizerTask, bind=True, max_retries=5, default_retry_delay=120)
def scheduled_organizer_export(self, organizer: Organizer, schedule: int, execution: int = None) -> None:
    schedule = organizer.scheduled_exports.get(pk=schedule)
    if execution is None:
        # Compatibility with messages queued by pretix versions before executions existed.
        execution = create_manual_execution(ScheduledOrganizerExportExecution, schedule)
    else:
        execution = ScheduledOrganizerExportExecution.objects.get(pk=execution, schedule=schedule)

    event_qs = organizer.events.all()
    if execution.export_form_data.get('events') is not None and not execution.export_form_data.get('all_events'):
        if isinstance(execution.export_form_data['events'][0], str):
            event_qs = event_qs.filter(slug__in=execution.export_form_data.get('events'))
        else:
            event_qs = event_qs.filter(pk__in=execution.export_form_data.get('events'))

    exporter = init_organizer_exporter(
        identifier=execution.export_identifier,
        organizer=organizer,
        user=schedule.owner,
        event_qs=event_qs,
    )
    exporter_registered = exporter is not None or _organizer_exporter_is_registered(
        organizer, execution.export_identifier
    )
    has_permission = schedule.owner.is_active

    _run_scheduled_export(
        self,
        schedule,
        organizer,
        execution,
        exporter,
        exporter_registered,
        has_permission,
        mainreverse_absolute(
            'control:organizer.export',
            kwargs={
                'organizer': organizer.slug,
            }
        ) + f'?identifier={schedule.export_identifier}&scheduled={schedule.pk}',
    )


@app.task(base=EventTask, bind=True, max_retries=5, default_retry_delay=120)
def scheduled_event_export(self, event: Event, schedule: int, execution: int = None) -> None:
    schedule = event.scheduled_exports.get(pk=schedule)
    if execution is None:
        # Compatibility with messages queued by pretix versions before executions existed.
        execution = create_manual_execution(ScheduledEventExportExecution, schedule)
    else:
        execution = ScheduledEventExportExecution.objects.get(pk=execution, schedule=schedule)

    exporter = init_event_exporter(
        identifier=execution.export_identifier,
        event=event,
        user=schedule.owner,
    )
    exporter_registered = exporter is not None or _event_exporter_is_registered(
        event, execution.export_identifier
    )
    has_permission = schedule.owner.is_active

    _run_scheduled_export(
        self,
        schedule,
        event,
        execution,
        exporter,
        exporter_registered,
        has_permission,
        mainreverse_absolute(
            'control:event.orders.export',
            kwargs={
                'event': event.slug,
                'organizer': event.organizer.slug,
            }
        ) + f'?identifier={schedule.export_identifier}&scheduled={schedule.pk}',
    )


def _execution_defaults(execution_model, schedule):
    template = execution_model(schedule=schedule)
    template.freeze_from_schedule(schedule)
    return {
        'export_identifier': template.export_identifier,
        'export_form_data': template.export_form_data,
        'locale': template.locale,
        'timezone_name': template.timezone_name,
        'mail_additional_recipients': template.mail_additional_recipients,
        'mail_additional_recipients_cc': template.mail_additional_recipients_cc,
        'mail_additional_recipients_bcc': template.mail_additional_recipients_bcc,
        'mail_subject': template.mail_subject,
        'mail_template': template.mail_template,
    }


def _dispatch_due_scheduled_exports():
    """Turn every due schedule slot into a unique execution and advance the schedule,
    atomically. Duplicate scanner runs cannot create a second execution for a slot."""
    due_qs = (
        ScheduledEventExport.objects.filter(
            schedule_next_run__lt=now(),
            error_counter__lt=5,
        )
        .select_for_update(
            skip_locked=connection.features.has_select_for_update_skip_locked,
            of=OF_SELF,
        )
    )
    with transaction.atomic():
        for s in due_qs:
            scheduled_for = s.schedule_next_run
            execution, created = ScheduledEventExportExecution.objects.get_or_create(
                schedule=s,
                scheduled_for=scheduled_for,
                defaults=_execution_defaults(ScheduledEventExportExecution, s),
            )
            s.compute_next_run()
            s.save(update_fields=['schedule_next_run'])
            if created:
                dispatch_scheduled_export_task(scheduled_event_export, {
                    'event': s.event_id,
                    'schedule': s.pk,
                    'execution': execution.pk,
                })

    due_qs = (
        ScheduledOrganizerExport.objects.filter(
            schedule_next_run__lt=now(),
            error_counter__lt=5,
        )
        .select_for_update(
            skip_locked=connection.features.has_select_for_update_skip_locked,
            of=OF_SELF,
        )
    )
    with transaction.atomic():
        for s in due_qs:
            scheduled_for = s.schedule_next_run
            execution, created = ScheduledOrganizerExportExecution.objects.get_or_create(
                schedule=s,
                scheduled_for=scheduled_for,
                defaults=_execution_defaults(ScheduledOrganizerExportExecution, s),
            )
            s.compute_next_run()
            s.save(update_fields=['schedule_next_run'])
            if created:
                dispatch_scheduled_export_task(scheduled_organizer_export, {
                    'organizer': s.organizer_id,
                    'schedule': s.pk,
                    'execution': execution.pk,
                })


def _recover_scheduled_export_executions():
    """Take over executions left behind by scheduler or worker restarts:

    * mail_queued executions are reconciled with the actual OutgoingMail state,
    * waiting/rendering/file_ready executions past their grace period are
      re-dispatched; the task's state machine makes the takeover idempotent.
    """
    for execution_model in (ScheduledEventExportExecution, ScheduledOrganizerExportExecution):
        with transaction.atomic():
            for execution in (
                execution_model.objects.filter(status=execution_model.STATUS_MAIL_QUEUED)
                .select_related('mail')
                .order_by('created')[:500]
            ):
                _continue_mail(execution)

        with transaction.atomic():
            qs = (
                execution_model.objects.filter(
                    status__in=tuple(SCHEDULED_EXPORT_RECOVERY_DELAYS.keys())
                )
                .select_related('schedule')
                .select_for_update(
                    skip_locked=connection.features.has_select_for_update_skip_locked,
                    of=OF_SELF,
                )
            )
            for execution in qs:
                timestamp_attr, delay = SCHEDULED_EXPORT_RECOVERY_DELAYS[execution.status]
                reference = getattr(execution, timestamp_attr)
                if not reference or reference > now() - delay:
                    continue
                if execution_model is ScheduledEventExportExecution:
                    kwargs = {
                        'event': execution.schedule.event_id,
                        'schedule': execution.schedule_id,
                        'execution': execution.pk,
                    }
                    task = scheduled_event_export
                else:
                    kwargs = {
                        'organizer': execution.schedule.organizer_id,
                        'schedule': execution.schedule_id,
                        'execution': execution.pk,
                    }
                    task = scheduled_organizer_export
                dispatch_scheduled_export_task(task, kwargs)


@receiver(signal=periodic_task)
@scopes_disabled()
def run_scheduled_exports(sender, **kwargs):
    _recover_scheduled_export_executions()
    _dispatch_due_scheduled_exports()
