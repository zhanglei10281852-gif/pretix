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
import zoneinfo
from datetime import datetime, timedelta

from dateutil.rrule import rrulestr
from dateutil.tz import datetime_exists
from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.utils.timezone import make_aware, now
from django.utils.translation import gettext_lazy as _

from pretix.base.models import LoggedModel
from pretix.base.validators import RRuleValidator, multimail_validate

# How long an execution may stay in a non-terminal state before the periodic scan
# assumes that the scheduler or worker died and dispatches a takeover task.
# Maps a status to (reference timestamp attribute, maximum age).
SCHEDULED_EXPORT_RECOVERY_DELAYS = {
    'waiting': ('created', timedelta(minutes=10)),
    'rendering': ('started', timedelta(hours=1)),
    'file_ready': ('started', timedelta(hours=1, minutes=30)),
}


class AbstractScheduledExport(LoggedModel):
    id = models.BigAutoField(primary_key=True)

    export_identifier = models.CharField(
        max_length=190,
        verbose_name=_("Export"),
    )
    export_form_data = models.JSONField(
        default=dict,
        encoder=DjangoJSONEncoder,
    )

    owner = models.ForeignKey(
        "pretixbase.User",
        on_delete=models.PROTECT,
    )
    locale = models.CharField(
        verbose_name=_('Language'),
        max_length=250
    )

    mail_additional_recipients = models.TextField(
        verbose_name=_('Additional recipients'),
        null=False, blank=True, validators=[multimail_validate],
        help_text=_("You can specify multiple recipients separated by commas.")
    )
    mail_additional_recipients_cc = models.TextField(
        verbose_name=_('Additional recipients (Cc)'),
        null=False, blank=True, validators=[multimail_validate],
        help_text=_("You can specify multiple recipients separated by commas.")
    )
    mail_additional_recipients_bcc = models.TextField(
        verbose_name=_('Additional recipients (Bcc)'),
        null=False, blank=True, validators=[multimail_validate],
        help_text=_("You can specify multiple recipients separated by commas.")
    )
    mail_subject = models.CharField(
        verbose_name=_('Subject'),
        max_length=250
    )
    mail_template = models.TextField(
        verbose_name=_('Message'),
    )

    schedule_rrule = models.TextField(
        null=True, blank=True, validators=[RRuleValidator(enforce_simple=True)]
    )
    schedule_rrule_time = models.TimeField(
        verbose_name=_("Requested start time"),
        help_text=_("The actual start time might be delayed depending on system load."),
    )
    schedule_next_run = models.DateTimeField(null=True, blank=True)

    error_counter = models.IntegerField(default=0)
    error_last_message = models.TextField(null=True, blank=True)

    class Meta:
        abstract = True

    def __str__(self):
        return self.mail_subject

    def compute_next_run(self):
        tz = self.tz
        r = rrulestr(self.schedule_rrule)

        base_dt = now().astimezone(tz).replace(tzinfo=None)
        if now().astimezone(tz).time() < self.schedule_rrule_time:
            base_dt -= timedelta(days=1)

        new_d = r.after(base_dt, inc=False)
        if not new_d:
            self.schedule_next_run = None
            return

        self.schedule_next_run = make_aware(datetime.combine(new_d.date(), self.schedule_rrule_time), tz)
        if not datetime_exists(self.schedule_next_run):
            self.schedule_next_run += timedelta(hours=1)


class ScheduledEventExport(AbstractScheduledExport):
    event = models.ForeignKey(
        "pretixbase.Event", on_delete=models.CASCADE, related_name="scheduled_exports"
    )

    @property
    def tz(self):
        return self.event.timezone


class ScheduledOrganizerExport(AbstractScheduledExport):
    organizer = models.ForeignKey(
        "pretixbase.Organizer", on_delete=models.CASCADE, related_name="scheduled_exports"
    )
    timezone = models.CharField(max_length=100,
                                default=settings.TIME_ZONE,
                                verbose_name=_('Timezone'))

    @property
    def tz(self):
        return zoneinfo.ZoneInfo(self.timezone)


class AbstractScheduledExportExecution(LoggedModel):
    """
    A single, recoverable execution of a scheduled export.

    Executions are identified uniquely by their schedule and the time the slot was
    scheduled for (``scheduled_for``). Manual runs ("Run now") use ``scheduled_for =
    None`` and ``manual = True`` and therefore never collide with a scheduled slot or
    with another manual run.

    All configuration that influences the result (export type, filter configuration,
    language, timezone and recipient settings) is frozen on the execution, so later
    edits of the schedule never affect a pending execution.
    """
    id = models.BigAutoField(primary_key=True)

    STATUS_WAITING = "waiting"
    STATUS_RENDERING = "rendering"
    STATUS_FILE_READY = "file_ready"
    STATUS_MAIL_QUEUED = "mail_queued"
    STATUS_FAILED = "failed"
    STATUS_COMPLETED = "completed"
    STATUS_CHOICES = (
        (STATUS_WAITING, _("waiting")),
        (STATUS_RENDERING, _("rendering")),
        (STATUS_FILE_READY, _("file ready")),
        (STATUS_MAIL_QUEUED, _("email queued")),
        (STATUS_FAILED, _("failed")),
        (STATUS_COMPLETED, _("completed")),
    )
    STATUS_LIST_TERMINAL = {
        STATUS_FAILED,
        STATUS_COMPLETED,
    }
    STATUS_LIST_NON_TERMINAL = {
        STATUS_WAITING,
        STATUS_RENDERING,
        STATUS_FILE_READY,
        STATUS_MAIL_QUEUED,
    }

    ERROR_PERMISSION = "permission"
    ERROR_CONFIG = "config"
    ERROR_EMPTY = "empty"
    ERROR_INTERNAL = "internal"
    ERROR_MAIL = "mail"
    ERROR_CHOICES = (
        (ERROR_PERMISSION, _("Permission denied.")),
        (ERROR_CONFIG, _("Invalid export configuration.")),
        (ERROR_EMPTY, _("Your export did not contain any data.")),
        (ERROR_INTERNAL, _("Internal error")),
        (ERROR_MAIL, _("Email could not be sent.")),
    )

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_WAITING,
    )

    # The time the slot was due at. None for manual runs.
    scheduled_for = models.DateTimeField(null=True, blank=True)
    manual = models.BooleanField(default=False)

    # Frozen configuration
    export_identifier = models.CharField(max_length=190)
    export_form_data = models.JSONField(default=dict, encoder=DjangoJSONEncoder)
    locale = models.CharField(max_length=250)
    timezone_name = models.CharField(max_length=100)
    mail_additional_recipients = models.TextField(null=False, blank=True)
    mail_additional_recipients_cc = models.TextField(null=False, blank=True)
    mail_additional_recipients_bcc = models.TextField(null=False, blank=True)
    mail_subject = models.CharField(max_length=250)
    mail_template = models.TextField()

    error_code = models.CharField(max_length=30, choices=ERROR_CHOICES, null=True, blank=True)
    error_message = models.TextField(null=True, blank=True)
    soft_failed = models.BooleanField(default=False)

    created = models.DateTimeField(auto_now_add=True)
    started = models.DateTimeField(null=True, blank=True)
    finished = models.DateTimeField(null=True, blank=True)

    class Meta:
        abstract = True
        ordering = ("-created",)

    def __str__(self):
        return f"{self.schedule_id}:{self.scheduled_for or 'manual'}:{self.pk}"

    @property
    def tz(self):
        return zoneinfo.ZoneInfo(self.timezone_name)

    @property
    def is_terminal(self):
        return self.status in self.STATUS_LIST_TERMINAL

    def freeze_from_schedule(self, schedule):
        """Copy all execution-relevant configuration from a schedule object."""
        self.export_identifier = schedule.export_identifier
        self.export_form_data = schedule.export_form_data
        self.locale = schedule.locale
        self.timezone_name = str(schedule.tz)
        self.mail_additional_recipients = schedule.mail_additional_recipients
        self.mail_additional_recipients_cc = schedule.mail_additional_recipients_cc
        self.mail_additional_recipients_bcc = schedule.mail_additional_recipients_bcc
        self.mail_subject = schedule.mail_subject
        self.mail_template = schedule.mail_template


class ScheduledEventExportExecution(AbstractScheduledExportExecution):
    schedule = models.ForeignKey(
        ScheduledEventExport, on_delete=models.CASCADE, related_name="executions"
    )
    file = models.ForeignKey(
        "pretixbase.CachedFile",
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name="event_scheduled_export_executions",
    )
    mail = models.ForeignKey(
        "pretixbase.OutgoingMail",
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name="event_scheduled_export_executions",
    )

    class Meta:
        ordering = ("-created",)
        constraints = [
            models.UniqueConstraint(
                fields=["schedule", "scheduled_for"],
                name="scheduled_event_export_slot_unique",
            ),
        ]
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["status", "created"]),
        ]


class ScheduledOrganizerExportExecution(AbstractScheduledExportExecution):
    schedule = models.ForeignKey(
        ScheduledOrganizerExport, on_delete=models.CASCADE, related_name="executions"
    )
    file = models.ForeignKey(
        "pretixbase.CachedFile",
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name="organizer_scheduled_export_executions",
    )
    mail = models.ForeignKey(
        "pretixbase.OutgoingMail",
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name="organizer_scheduled_export_executions",
    )

    class Meta:
        ordering = ("-created",)
        constraints = [
            models.UniqueConstraint(
                fields=["schedule", "scheduled_for"],
                name="scheduled_organizer_export_slot_unique",
            ),
        ]
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["status", "created"]),
        ]
