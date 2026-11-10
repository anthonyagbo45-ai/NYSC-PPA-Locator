"""Free source-labelled browsing and private contributor actions."""
from zoneinfo import ZoneInfo

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from accounts.decorators import verified_email_required
from config.security import rate_limit
from . import services
from .forms import GroupForm, GroupSelectionForm, NoticeForm, ResourceFilterForm, ResourceReportForm, positive_id
from .models import CDSGroup, CDSPreference, ResourceReport, ServiceNotice


COMMON_FIELDS = ('title', 'state', 'lga', 'source_type', 'source_url', 'observed_on')
GROUP_FIELDS = COMMON_FIELDS + ('description', 'venue_hint', 'public_contact_url')
NOTICE_FIELDS = COMMON_FIELDS + ('kind', 'body', 'venue_hint', 'group', 'event_at', 'ends_at', 'timezone_name', 'batch_label', 'stage')


def _errors(form, error):
    aliases = {'event_at': 'event_local', 'ends_at': 'ends_local'}
    if hasattr(error, 'message_dict'):
        for name, errors in error.message_dict.items():
            name = aliases.get(name, name)
            for message in errors:
                form.add_error(name if name in form.fields else None, message)
    else:
        form.add_error(None, error)


def _page_query(request):
    values = request.GET.copy()
    values.pop('page', None)
    return values.urlencode()


def _project(resource, user=None):
    return services.safe_group(resource, user=user) if isinstance(resource, CDSGroup) else services.safe_notice(resource, user=user)


def _page(queryset, request):
    page = Paginator(queryset, 20).get_page(request.GET.get('page'))
    page.object_list = [_project(resource, request.user) for resource in page.object_list]
    page.paginator.object_list = page.object_list
    return page


def _query(resource_type):
    if resource_type == 'group':
        return CDSGroup.objects.select_related('state', 'lga')
    if resource_type == 'notice':
        return ServiceNotice.objects.select_related('state', 'lga', 'group')
    raise Http404


def _private_selection(user):
    if not user.is_authenticated:
        return None
    preference = CDSPreference.objects.select_related('group__state', 'group__lga').filter(user=user).first()
    if not preference or not preference.group_id:
        return None
    if preference.group.publication_status != CDSGroup.Publication.APPROVED or preference.group.expires_at <= timezone.now():
        return {'available': False}
    return {'available': True, 'group': services.safe_group(preference.group)}


@require_GET
def home(request, hub=None):
    values = request.GET.copy()
    if hub:
        values['kind'] = hub
    if hub == ServiceNotice.Kind.CLEARANCE:
        values['view'] = 'notices'
    form = ResourceFilterForm(values)
    is_groups = form.is_valid() and form.cleaned_data.get('view') == 'groups'
    rows = services.public_groups() if is_groups else services.public_notices()
    if form.is_valid():
        filters = form.cleaned_data
        for name in ('state', 'lga'):
            if filters.get(name):
                rows = rows.filter(**{name: filters[name]})
        if filters.get('q'):
            content = 'description' if is_groups else 'body'
            rows = rows.filter(Q(title__icontains=filters['q']) | Q(**{content + '__icontains': filters['q']}))
        if filters.get('group'):
            rows = rows.filter(pk=filters['group'].pk) if is_groups else rows.filter(group=filters['group'])
        if not is_groups:
            if filters.get('kind'):
                rows = rows.filter(kind=filters['kind'])
            if filters.get('batch'):
                rows = rows.filter(batch_label__icontains=filters['batch'])
            if filters.get('stage'):
                rows = rows.filter(stage=filters['stage'])
    else:
        rows = rows.none()
    page = _page(rows.order_by('title', 'pk') if is_groups else rows.order_by('-observed_on', '-pk'), request)
    return render(request, 'service_resources/home.html', {
        'form': form, 'resources': page, 'page_obj': page, 'page_query': _page_query(request),
        'is_groups': is_groups, 'hub': hub, 'official_resources': getattr(settings, 'NYSC_RESOURCE_LINKS', []),
        'private_selection': _private_selection(request.user),
    }, status=200 if form.is_valid() else 400)


@require_GET
def cds(request):
    return home(request, hub=ServiceNotice.Kind.CDS)


@require_GET
def clearance(request):
    return home(request, hub=ServiceNotice.Kind.CLEARANCE)


@require_GET
def group_detail(request, pk):
    group = get_object_or_404(_query('group'), pk=pk)
    own = request.user.is_authenticated and group.author_id == request.user.pk
    if group.publication_status != CDSGroup.Publication.APPROVED and not own:
        raise Http404
    notices = _page(services.public_notices().filter(group=group).order_by('-observed_on', '-pk'), request)
    return render(request, 'service_resources/group_detail.html', {
        'resource': services.safe_group(group, user=request.user), 'own': own,
        'private_preview': group.publication_status != CDSGroup.Publication.APPROVED,
        'notices': notices, 'page_obj': notices, 'page_query': _page_query(request),
    })


@require_GET
def notice_detail(request, pk):
    notice = get_object_or_404(_query('notice'), pk=pk)
    own = request.user.is_authenticated and notice.author_id == request.user.pk
    if notice.publication_status != ServiceNotice.Publication.APPROVED and not own:
        raise Http404
    return render(request, 'service_resources/notice_detail.html', {
        'resource': services.safe_notice(notice, user=request.user), 'own': own,
        'private_preview': notice.publication_status != ServiceNotice.Publication.APPROVED,
    })


def _initial(request, resource=None, resource_type='group'):
    if resource is None:
        values = {'state': request.user.state_posted_id, 'lga': request.user.lga_posted_id,
                  'source_type': CDSGroup.Source.MEMBER, 'observed_on': timezone.localdate(),
                  'kind': ServiceNotice.Kind.CDS, 'timezone_name': 'Africa/Lagos'}
        group_id = positive_id(request.GET.get('group'))
        group = services.public_groups().filter(pk=group_id).first() if group_id else None
        if group and resource_type == 'notice':
            values.update(group=group.pk, state=group.state_id, lga=group.lga_id)
        if request.GET.get('kind') in ServiceNotice.Kind.values:
            values['kind'] = request.GET['kind']
        return values
    names = GROUP_FIELDS if resource_type == 'group' else NOTICE_FIELDS
    values = {name: getattr(resource, name) for name in names if name not in ('state', 'lga', 'group', 'event_at', 'ends_at')}
    values.update(state=resource.state_id, lga=resource.lga_id)
    if resource_type == 'notice':
        values['group'] = resource.group_id
        zone = ZoneInfo(resource.timezone_name)
        for name, form_name in [('event_at', 'event_local'), ('ends_at', 'ends_local')]:
            moment = getattr(resource, name)
            values[form_name] = moment.astimezone(zone).strftime('%Y-%m-%dT%H:%M') if moment else ''
    return values


def _mine_redirect(resource_type):
    return redirect(reverse('service_resources:mine') + ('?view=groups' if resource_type == 'group' else ''))


def _contribute(request, resource_type, pk=None):
    resource = get_object_or_404(_query(resource_type), pk=pk, author=request.user) if pk is not None else None
    if resource and resource.publication_status == CDSGroup.Publication.ARCHIVED:
        messages.info(request, 'Archived information is kept as history. Submit a new report if circumstances change.')
        return _mine_redirect(resource_type)
    form_type = GroupForm if resource_type == 'group' else NoticeForm
    form = form_type(request.POST if request.method == 'POST' else None,
                     initial=_initial(request, resource, resource_type))
    if request.method == 'POST' and form.is_valid():
        fields = GROUP_FIELDS if resource_type == 'group' else NOTICE_FIELDS
        cleaned = {name: form.cleaned_data.get(name) for name in fields}
        try:
            if resource_type == 'group':
                services.save_group(user=request.user, cleaned_data=cleaned, group=resource, submit=form.cleaned_data['action'] == 'submit')
            else:
                services.save_notice(user=request.user, cleaned_data=cleaned, notice=resource, submit=form.cleaned_data['action'] == 'submit')
        except ValidationError as error:
            _errors(form, error)
        else:
            messages.success(request, 'Submitted for review.' if form.cleaned_data['action'] == 'submit' else 'Private draft saved.')
            return _mine_redirect(resource_type)
    return render(request, 'service_resources/form.html', {
        'form': form, 'resource_type': resource_type, 'editing': resource is not None,
    }, status=400 if request.method == 'POST' else 200)


@login_required
@verified_email_required
@never_cache
@require_http_methods(['GET', 'POST'])
@rate_limit('service-resource-write', 15)
def create_group(request):
    return _contribute(request, 'group')


@login_required
@verified_email_required
@never_cache
@require_http_methods(['GET', 'POST'])
@rate_limit('service-resource-write', 15)
def edit_group(request, pk):
    return _contribute(request, 'group', pk)


@login_required
@verified_email_required
@never_cache
@require_http_methods(['GET', 'POST'])
@rate_limit('service-resource-write', 15)
def create_notice(request):
    return _contribute(request, 'notice')


@login_required
@verified_email_required
@never_cache
@require_http_methods(['GET', 'POST'])
@rate_limit('service-resource-write', 15)
def edit_notice(request, pk):
    return _contribute(request, 'notice', pk)


@login_required
@never_cache
@require_GET
def mine(request):
    resource_type = 'group' if request.GET.get('view') == 'groups' else 'notice'
    page = _page(_query(resource_type).filter(author=request.user).order_by('-updated_at', '-pk'), request)
    return render(request, 'service_resources/mine.html', {
        'resources': page, 'page_obj': page, 'page_query': _page_query(request), 'resource_type': resource_type,
    })


def _history(request, resource_type, pk):
    resource = get_object_or_404(_query(resource_type), pk=pk, author=request.user)
    revisions = services.owner_revisions(resource=resource, user=request.user)
    for revision in revisions:
        snapshot = revision['snapshot']
        snapshot['publication_label'] = dict(CDSGroup.Publication.choices).get(snapshot.get('publication_status'), 'Status not recorded')
        snapshot['verification_label'] = dict(CDSGroup.Verification.choices).get(snapshot.get('verification_status'), 'Verification not recorded')
        snapshot['source_label'] = dict(CDSGroup.Source.choices).get(snapshot.get('source_type'), 'Source not recorded')
    page = Paginator(revisions, 20).get_page(request.GET.get('page'))
    return render(request, 'service_resources/history.html', {
        'resource': _project(resource, request.user), 'resource_type': resource_type,
        'revisions': page, 'page_obj': page, 'page_query': _page_query(request),
    })


@login_required
@never_cache
@require_GET
def group_history(request, pk):
    return _history(request, 'group', pk)


@login_required
@never_cache
@require_GET
def notice_history(request, pk):
    return _history(request, 'notice', pk)


def _archive(request, resource_type, pk):
    resource = get_object_or_404(_query(resource_type), pk=pk, author=request.user)
    if resource_type == 'group':
        services.archive_group(group=resource, user=request.user)
    else:
        services.archive_notice(notice=resource, user=request.user)
    messages.success(request, 'Contribution withdrawn from public browsing. Your private history is retained.')
    return _mine_redirect(resource_type)


@login_required
@verified_email_required
@never_cache
@require_POST
@rate_limit('service-resource-archive', 20)
def archive_group(request, pk):
    return _archive(request, 'group', pk)


@login_required
@verified_email_required
@never_cache
@require_POST
@rate_limit('service-resource-archive', 20)
def archive_notice(request, pk):
    return _archive(request, 'notice', pk)


@login_required
@never_cache
@require_http_methods(['GET', 'POST'])
@rate_limit('cds-private-preference', 20)
def select_group(request):
    initial = {'group': request.GET.get('group', '')}
    form = GroupSelectionForm(request.POST if request.method == 'POST' else None, initial=initial)
    if request.method == 'POST' and form.is_valid():
        try:
            services.select_group(user=request.user, group=form.cleaned_data.get('group'))
        except ValidationError as error:
            _errors(form, error)
        else:
            messages.success(request, 'Your private CDS preference was saved. It is not an official membership record.')
            return redirect('service_resources:select_group')
    selected = None
    group_id = positive_id(form.data.get('group') if form.is_bound else form.initial.get('group'))
    group = services.public_groups().filter(pk=group_id).first() if group_id else None
    if group:
        selected = services.safe_group(group)
    return render(request, 'service_resources/selection.html', {
        'form': form, 'selected': selected, 'private_selection': _private_selection(request.user),
    }, status=400 if request.method == 'POST' else 200)


def _report(request, resource_type, pk):
    rows = services.public_groups(include_expired=True) if resource_type == 'group' else services.public_notices(include_expired=True)
    resource = get_object_or_404(rows, pk=pk)
    form = ResourceReportForm(request.POST if request.method == 'POST' else None)
    if request.method == 'POST' and form.is_valid():
        try:
            services.save_report(user=request.user, resource=resource, **form.cleaned_data)
        except ValidationError as error:
            _errors(form, error)
        else:
            messages.success(request, 'Your private report is awaiting review. It does not automatically change the resource.')
            return redirect('service_resources:reports')
    return render(request, 'service_resources/report.html', {'form': form, 'resource': _project(resource)}, status=400 if request.method == 'POST' else 200)


@login_required
@never_cache
@require_http_methods(['GET', 'POST'])
@rate_limit('service-resource-report', 10)
def report_group(request, pk):
    return _report(request, 'group', pk)


@login_required
@never_cache
@require_http_methods(['GET', 'POST'])
@rate_limit('service-resource-report', 10)
def report_notice(request, pk):
    return _report(request, 'notice', pk)


@login_required
@never_cache
@require_GET
def reports(request):
    rows = ResourceReport.objects.filter(reporter=request.user).order_by('-created_at', '-pk').values(
        'id', 'category', 'details', 'proposed_correction', 'status', 'created_at',
    )
    page = Paginator(rows, 20).get_page(request.GET.get('page'))
    page.object_list = list(page.object_list)
    for row in page.object_list:
        row.update(category_label=dict(ResourceReport.Category.choices)[row['category']], status_label=dict(ResourceReport.Status.choices)[row['status']])
    page.paginator.object_list = page.object_list
    return render(request, 'service_resources/reports.html', {'reports': page, 'page_obj': page, 'page_query': _page_query(request)})
