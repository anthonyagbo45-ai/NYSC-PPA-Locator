from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q


MODERATOR_PERMISSIONS = {
    'can_moderate_claims', 'can_moderate_reviews',
    'view_ppaclaim', 'view_claimrevision', 'view_review', 'view_reviewrevision',
    'view_reviewchangelog', 'view_placereport', 'change_placereport',
    'view_reportchangelog', 'view_ppa', 'view_placechangelog',
    'can_moderate_submissions', 'view_ppasubmission', 'view_submissionrevision',
}
LOCAL_MODERATOR_PERMISSIONS = {
    'can_moderate_listings', 'can_moderate_listing_reports',
    'view_listing', 'view_listingrevision', 'view_listingreport', 'view_listingreportrevision',
}
RESOURCE_MODERATOR_PERMISSIONS = {
    'can_moderate_resources', 'can_moderate_resource_reports',
    'view_cdsgroup', 'view_servicenotice', 'view_grouprevision', 'view_noticerevision',
    'view_resourcereport', 'view_resourcereportrevision',
}
COMMUNITY_MODERATOR_PERMISSIONS = {
    'can_moderate_community', 'can_moderate_community_reports',
    'view_question', 'view_answer', 'view_questionrevision', 'view_answerrevision',
    'view_communityreport', 'view_reportrevision',
}
ROOM_MODERATOR_PERMISSIONS = {
    'can_manage_rooms', 'can_moderate_room_memberships',
    'can_moderate_room_posts', 'can_moderate_room_reports',
    'view_room', 'view_roomrevision', 'view_roommembership', 'view_roommembershiprevision',
    'view_roompost', 'view_roompostrevision', 'view_roomreport', 'view_roomreportrevision',
}


class Command(BaseCommand):
    help = 'Create least-privilege community roles. Does not grant membership, staff access or verification.'

    @transaction.atomic
    def handle(self, *args, **options):
        permissions = Permission.objects.filter(
            Q(content_type__app_label='ppas', codename__in=MODERATOR_PERMISSIONS)
            | Q(content_type__app_label='local_guides', codename__in=LOCAL_MODERATOR_PERMISSIONS)
            | Q(content_type__app_label='service_resources', codename__in=RESOURCE_MODERATOR_PERMISSIONS)
            | Q(content_type__app_label='community', codename__in=COMMUNITY_MODERATOR_PERMISSIONS)
            | Q(content_type__app_label='local_rooms', codename__in=ROOM_MODERATOR_PERMISSIONS)
        )
        required = {('ppas', name) for name in MODERATOR_PERMISSIONS} | {('local_guides', name) for name in LOCAL_MODERATOR_PERMISSIONS}
        required |= {('service_resources', name) for name in RESOURCE_MODERATOR_PERMISSIONS}
        required |= {('community', name) for name in COMMUNITY_MODERATOR_PERMISSIONS}
        required |= {('local_rooms', name) for name in ROOM_MODERATOR_PERMISSIONS}
        missing = required - set(permissions.values_list('content_type__app_label', 'codename'))
        if missing:
            raise CommandError('Run all migrations first; required moderation permissions are missing: ' + ', '.join(f'{app}.{name}' for app, name in sorted(missing)))
        moderator, _ = Group.objects.get_or_create(name='Moderator')
        moderator.permissions.set(permissions)
        vetted, _ = Group.objects.get_or_create(name='VettedContributor')
        vetted.permissions.clear()
        self.stdout.write(self.style.SUCCESS('Community roles ready. No accounts, staff flags or verification records changed.'))
