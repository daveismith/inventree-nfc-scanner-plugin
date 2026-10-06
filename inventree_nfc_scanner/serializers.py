"""Serializers for the panel's API."""

from rest_framework import serializers

from .models import Job


class JobSerializer(serializers.ModelSerializer):
    """A job, as the panel follows it."""

    scanner = serializers.SerializerMethodField()
    created_by_name = serializers.SerializerMethodField()

    class Meta:
        """Meta options."""

        model = Job
        fields = [
            'id', 'kind', 'location', 'scanner', 'created_by', 'created_by_name', 'created_at', 'updated_at',
            'finished_at', 'state', 'overwrite', 'timeout_s', 'uid', 'tag_type', 'protected', 'error',
            'error_detail', 'existing_text', 'existing_uri',
        ]
        read_only_fields = fields

    def get_scanner(self, job):
        """The machine's id and name, or null for a USB job."""
        if job.machine is None:
            return None
        return {'id': str(job.machine.pk), 'name': job.machine.name}

    def get_created_by_name(self, job):
        """Who asked."""
        return job.created_by.username if job.created_by else None


class JobCreateSerializer(serializers.Serializer):
    """What the panel sends to queue a job for a network scanner."""

    location = serializers.IntegerField()
    scanner = serializers.UUIDField()
    kind = serializers.ChoiceField(choices=Job.Kind.choices, default=Job.Kind.PROGRAM)
    overwrite = serializers.BooleanField(default=False)


class UsbJobSerializer(serializers.Serializer):
    """The outcome of a job the browser did over USB, for the history."""

    kind = serializers.ChoiceField(choices=Job.Kind.choices, default=Job.Kind.PROGRAM)
    state = serializers.ChoiceField(choices=[Job.State.DONE, Job.State.FAILED, Job.State.CANCELLED])
    overwrite = serializers.BooleanField(default=False)
    uid = serializers.CharField(max_length=20, required=False, allow_blank=True, default='')
    tag_type = serializers.CharField(max_length=12, required=False, allow_blank=True, default='')
    protected = serializers.BooleanField(required=False, allow_null=True, default=None)
    error = serializers.CharField(max_length=32, required=False, allow_blank=True, default='')
    error_detail = serializers.CharField(max_length=200, required=False, allow_blank=True, default='')
