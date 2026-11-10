"""Public community reading, safe projections and author-scoped mutations."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import Http404, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from accounts.decorators import verified_email_required
from config.security import rate_limit
from ppas.models import PPA
from service_resources.models import CDSGroup
from . import services
from .forms import AnswerForm, BlockForm, BooleanActionForm, FollowForm, QuestionFilterForm, QuestionForm, ReportForm, positive_id
from .models import Answer, HelpfulReaction, CommunityBlock, CommunityReport, Question, QuestionFollow


QUESTION_FIELDS = ('title', 'body', 'category', 'state', 'lga', 'ppa', 'group')
ANSWER_FIELDS = ('body', 'source_url')


def _errors(form, error):
    if hasattr(error, 'message_dict'):
        for field, errors in error.message_dict.items():
            for message in errors:
                form.add_error(field if field in form.fields else None, message)
    else:
        form.add_error(None, error)


def _query(request):
    query = request.GET.copy()
    query.pop('page', None)
    return query.urlencode()


def _page(queryset, request, project, per_page=20):
    page = Paginator(queryset, per_page).get_page(request.GET.get('page'))
    page.object_list = [project(item) for item in page.object_list]
    # Do not leave a raw model path reachable through the paginator context.
    page.paginator.object_list = page.object_list
    return page


def _global_notifications(user):
    if not user.is_authenticated:
        return False
    from notifications.models import NotificationPreference
    return NotificationPreference.objects.filter(user=user, community_replies=True).exists()


def _blocked_ids(user, author_ids):
    if not user.is_authenticated:
        return set()
    author_ids = {value for value in author_ids if value}
    rows = CommunityBlock.objects.filter(Q(user=user, blocked_user_id__in=author_ids) | Q(blocked_user=user, user_id__in=author_ids)).values_list('user_id', 'blocked_user_id')
    return {other if member == user.pk else member for member, other in rows}


def _public_question(item, request, blocked=None):
    result = services.safe_public_question(item, user=request.user)
    result['interaction_blocked'] = item.author_id in (blocked or set())
    return result


def _owner_answer(item, user):
    result = services.safe_owner_answer(item, user=user)
    result['question_visible'] = item.question.status == 'visible'
    return result


def _initial(request, question=None):
    if question is not None:
        values = {name: getattr(question, name) for name in QUESTION_FIELDS if name not in ('state', 'lga', 'ppa', 'group')}
        values.update(state=question.state_id, lga=question.lga_id, ppa=question.ppa_id, group=question.group_id, version=question.version)
        return values
    values = {}
    ppa_id, group_id = positive_id(request.GET.get('ppa')), positive_id(request.GET.get('group'))
    context = PPA.objects.filter(pk=ppa_id).exclude(status__in=['closed', 'disputed']).exclude(location_status='mismatch').first() if ppa_id else None
    if context:
        values.update(state=context.state_id, lga=context.lga_id, ppa=context.pk)
    elif group_id:
        context = CDSGroup.objects.filter(pk=group_id, publication_status='approved', expires_at__gt=timezone.now()).first()
        if context:
            values.update(state=context.state_id, lga=context.lga_id, group=context.pk)
    if not values and request.user.is_authenticated:
        values.update(state=request.user.state_posted_id, lga=request.user.lga_posted_id)
    return values


@require_GET
def home(request):
    data = request.GET.copy()
    for name, model in (('ppa', PPA), ('group', CDSGroup)):
        selected_id = positive_id(data.get(name))
        choices = model.objects.filter(publication_status='approved', expires_at__gt=timezone.now()) if name == 'group' else model.objects.exclude(status__in=['closed', 'disputed']).exclude(location_status='mismatch')
        obj = choices.filter(pk=selected_id).only('state_id', 'lga_id').first() if selected_id else None
        if obj:
            if not data.get('state'):
                data['state'] = obj.state_id
            if not data.get('lga'):
                data['lga'] = obj.lga_id
    form = QuestionFilterForm(data)
    queryset = services.public_questions()
    valid = form.is_valid()
    if valid:
        filters = form.cleaned_data
        for field in ('category', 'state', 'lga', 'ppa', 'group'):
            if filters.get(field):
                queryset = queryset.filter(**{field: filters[field]})
        if filters.get('q'):
            queryset = queryset.filter(Q(title__icontains=filters['q']) | Q(body__icontains=filters['q']))
        sort = filters.get('sort') or 'newest'
        if sort == 'unanswered':
            queryset = queryset.filter(answer_count=0)
        elif sort == 'resolved':
            queryset = queryset.filter(resolved=True)
        queryset = queryset.order_by('-helpful_count', '-created_at', '-pk') if sort == 'helpful' else queryset.order_by('-created_at', '-pk')
    else:
        queryset = queryset.none()
    page = _page(queryset, request, lambda item: services.safe_public_question(item, user=request.user))
    return render(request, 'community/home.html', {'form': form, 'questions': page, 'page_obj': page, 'page_query': _query(request)}, status=200 if valid else 400)


@require_GET
def question_detail(request, pk):
    question = services.get_public_question(pk)
    page = Paginator(services.public_answers(question).order_by('created_at', 'pk'), 20).get_page(request.GET.get('page'))
    answer_rows = list(page.object_list)
    blocked = _blocked_ids(request.user, [question.author_id, *[answer.author_id for answer in answer_rows]])
    votes = set(HelpfulReaction.objects.filter(user=request.user, answer_id__in=[answer.pk for answer in answer_rows]).values_list('answer_id', flat=True)) if request.user.is_authenticated else set()
    safe_answers = []
    for answer in answer_rows:
        safe = services.safe_public_answer(answer, user=request.user)
        safe.update(has_voted=answer.pk in votes, interaction_blocked=answer.author_id in blocked or question.author_id in blocked)
        safe_answers.append(safe)
    page.object_list = safe_answers
    page.paginator.object_list = safe_answers
    follow = QuestionFollow.objects.filter(user=request.user, question=question).values('enabled').first() if request.user.is_authenticated else None
    return render(request, 'community/question_detail.html', {
        'question': _public_question(question, request, blocked), 'answers': page, 'page_obj': page, 'page_query': _query(request),
        'following': follow is not None, 'follow_enabled': bool(follow and follow['enabled']), 'global_notifications_enabled': _global_notifications(request.user),
    })


@login_required
@verified_email_required
@never_cache
@require_http_methods(['GET', 'POST'])
@rate_limit('community-contribute', 15)
def question_add(request, pk=None):
    question = get_object_or_404(Question, pk=pk, author=request.user) if pk is not None else None
    if question and question.status == 'archived':
        messages.info(request, 'Archived questions are retained in your history. Ask a new question if needed.')
        return redirect('community:my_activity')
    form = QuestionForm(request.POST if request.method == 'POST' else None, initial=_initial(request, question))
    if question:
        form.fields['version'].required = True
    if request.method == 'POST' and request.POST.get('action') == 'refresh':
        form = QuestionForm(initial=request.POST.dict())
        if question:
            form.fields['version'].required = True
        return render(request, 'community/form.html', {'form': form, 'editing': question is not None, 'content_type': 'question'})
    if request.method == 'POST' and form.is_valid():
        try:
            result = services.save_question(user=request.user, cleaned_data={key: form.cleaned_data[key] for key in QUESTION_FIELDS}, question=question, expected_version=form.cleaned_data.get('version'))
        except ValidationError as error:
            _errors(form, error)
        else:
            messages.success(request, 'Your question was saved. Hidden questions remain hidden until moderator review.' if question and question.status == 'hidden' else 'Your question was published.')
            return redirect('community:question_detail', pk=result.pk) if result.status == 'visible' else redirect('community:my_activity')
    return render(request, 'community/form.html', {'form': form, 'editing': question is not None, 'content_type': 'question'}, status=400 if request.method == 'POST' else 200)


@login_required
@verified_email_required
@never_cache
@require_http_methods(['GET', 'POST'])
@rate_limit('community-contribute', 15)
def answer_add(request, pk=None, answer_pk=None):
    answer = get_object_or_404(Answer.objects.select_related('question'), pk=answer_pk, author=request.user) if answer_pk is not None else None
    question = answer.question if answer else services.get_public_question(pk)
    if answer and answer.status == 'archived':
        messages.info(request, 'Archived answers are retained in your history.')
        return redirect('community:my_activity')
    if question.status != 'visible' and not answer:
        raise Http404
    if services.interaction_blocked(request.user, question.author) or (answer and services.interaction_blocked(request.user, answer.author)):
        messages.error(request, 'A member block prevents this interaction.')
        return redirect('community:question_detail', pk=question.pk) if question.status == 'visible' else redirect('community:my_activity')
    initial = {'body': answer.body, 'source_url': answer.source_url, 'version': answer.version} if answer else {}
    form = AnswerForm(request.POST if request.method == 'POST' else None, initial=initial)
    if answer:
        form.fields['version'].required = True
    if request.method == 'POST' and form.is_valid():
        try:
            services.save_answer(user=request.user, question=question, answer=answer, cleaned_data={key: form.cleaned_data[key] for key in ANSWER_FIELDS}, expected_version=form.cleaned_data.get('version'))
        except ValidationError as error:
            _errors(form, error)
        else:
            messages.success(request, 'Your answer was saved. A hidden answer remains hidden until moderator review.' if answer and answer.status == 'hidden' else 'Your answer was published.')
            return redirect('community:question_detail', pk=question.pk) if question.status == 'visible' else redirect('community:my_activity')
    return render(request, 'community/form.html', {'form': form, 'editing': answer is not None, 'content_type': 'answer', 'question': services.safe_public_question(question, user=request.user) if question.status == 'visible' else None}, status=400 if request.method == 'POST' else 200)


@login_required
@require_POST
@rate_limit('community-contribute', 15)
def question_archive(request, pk):
    question = get_object_or_404(Question, pk=pk, author=request.user)
    try:
        services.archive_question(user=request.user, question=question)
    except ValidationError as error:
        messages.error(request, '; '.join(error.messages))
    else:
        messages.success(request, 'Question archived. It is no longer public.')
    return redirect('community:my_activity')


@login_required
@require_POST
@rate_limit('community-contribute', 15)
def answer_archive(request, pk):
    answer = get_object_or_404(Answer, pk=pk, author=request.user)
    try:
        services.archive_answer(user=request.user, answer=answer)
    except ValidationError as error:
        messages.error(request, '; '.join(error.messages))
    else:
        messages.success(request, 'Answer archived. It is no longer public.')
    return redirect('community:my_activity')


@login_required
@verified_email_required
@require_POST
@rate_limit('community-contribute', 15)
def question_resolve(request, pk):
    question = get_object_or_404(Question, pk=pk, author=request.user)
    form = BooleanActionForm(request.POST)
    if not form.is_valid():
        return HttpResponseBadRequest('Choose a valid resolved state.')
    try:
        services.set_question_resolved(user=request.user, question=question, resolved=form.cleaned_data['value'] == '1')
    except ValidationError as error:
        messages.error(request, '; '.join(error.messages))
    return redirect('community:question_detail', pk=pk) if question.status == 'visible' else redirect('community:my_activity')


@login_required
@verified_email_required
@require_POST
@rate_limit('community-vote', 30)
def vote(request, pk):
    answer = services.get_public_answer(pk)
    form = BooleanActionForm(request.POST)
    if not form.is_valid():
        return HttpResponseBadRequest('Choose a valid helpful action.')
    try:
        services.set_helpful(user=request.user, answer=answer, helpful=form.cleaned_data['value'] == '1')
    except ValidationError as error:
        messages.error(request, '; '.join(error.messages))
    return redirect('community:question_detail', pk=answer.question_id)


@login_required
@require_POST
@rate_limit('community-follow', 30)
def follow(request, pk):
    question = services.get_public_question(pk)
    form = FollowForm(request.POST)
    if not form.is_valid():
        return HttpResponseBadRequest('Choose a valid follow action.')
    try:
        services.set_follow(user=request.user, question=question, following=form.cleaned_data['action'] == 'follow', enabled=form.cleaned_data['enabled'])
    except ValidationError as error:
        messages.error(request, '; '.join(error.messages))
    else:
        messages.success(request, 'Your private follow preference was saved. Community notifications are paused in your reminder preferences.' if form.cleaned_data['enabled'] and not _global_notifications(request.user) else 'Your private follow preference was saved.')
    return redirect('community:question_detail', pk=pk)


@login_required
@never_cache
@require_GET
def my_activity(request):
    kind = request.GET.get('kind', 'questions')
    if kind not in ('questions', 'answers', 'following'):
        return HttpResponseBadRequest('Choose questions, answers or following.')
    if kind == 'answers':
        queryset = services.owner_answers(request.user)
        project = lambda item: _owner_answer(item, request.user)
    elif kind == 'following':
        queryset = services.public_questions().filter(pk__in=QuestionFollow.objects.filter(user=request.user).values('question_id'))
        project = lambda item: services.safe_public_question(item, user=request.user)
    else:
        queryset = services.owner_questions(request.user)
        project = lambda item: services.safe_owner_question(item, user=request.user)
    page = _page(queryset.order_by('-updated_at', '-pk'), request, project)
    return render(request, 'community/activity.html', {'items': page, 'kind': kind, 'page_obj': page, 'page_query': _query(request)})


@login_required
@never_cache
@require_GET
def history(request, pk, content_type):
    model = Question if content_type == 'question' else Answer
    item = get_object_or_404(model, pk=pk, author=request.user)
    kwargs = {content_type: item, 'user': request.user}
    rows = services.owner_revisions(**kwargs)
    page = _page(rows, request, lambda revision: {key: revision[key] for key in ('snapshot', 'created_at')})
    safe = services.safe_owner_question(item, user=request.user) if content_type == 'question' else services.safe_owner_answer(item, user=request.user)
    return render(request, 'community/history.html', {'item': safe, 'content_type': content_type, 'revisions': page, 'page_obj': page, 'page_query': _query(request)})


@login_required
@never_cache
@require_http_methods(['GET', 'POST'])
@rate_limit('community-report', 10)
def report(request, pk, content_type):
    item = services.get_public_question(pk) if content_type == 'question' else services.get_public_answer(pk)
    form = ReportForm(request.POST if request.method == 'POST' else None)
    if request.method == 'POST' and form.is_valid():
        try:
            services.save_report(user=request.user, **{content_type: item}, **form.cleaned_data)
        except ValidationError as error:
            _errors(form, error)
        else:
            messages.success(request, 'Your private report is awaiting moderator review.')
            return redirect('community:reports')
    safe = services.safe_public_question(item, user=request.user) if content_type == 'question' else services.safe_public_answer(item, user=request.user)
    return render(request, 'community/report.html', {'form': form, 'item': safe, 'content_type': content_type}, status=400 if request.method == 'POST' else 200)


@login_required
@never_cache
@require_GET
def reports(request):
    page = _page(services.owner_reports(request.user), request, lambda item: services.safe_owner_report(item, user=request.user))
    return render(request, 'community/reports.html', {'reports': page, 'page_obj': page, 'page_query': _query(request)})


@login_required
@never_cache
@require_GET
def report_history(request, pk):
    report = get_object_or_404(CommunityReport, pk=pk, reporter=request.user)
    page = _page(services.owner_report_revisions(report=report, user=request.user), request, lambda row: {key: row[key] for key in ('previous_status', 'new_status', 'created_at')})
    return render(request, 'community/report_history.html', {'report': services.safe_owner_report(report, user=request.user), 'revisions': page, 'page_obj': page, 'page_query': _query(request)})


@login_required
@never_cache
@require_GET
def blocks(request):
    rows = CommunityBlock.objects.filter(user=request.user).select_related('blocked_user').order_by('-created_at', '-pk')
    page = _page(rows, request, lambda item: {'id': item.pk, 'author_name': item.blocked_user.public_name, 'created_at': item.created_at})
    return render(request, 'community/blocks.html', {'blocks': page, 'page_obj': page, 'page_query': _query(request)})


@login_required
@require_POST
@rate_limit('community-block', 15)
def block(request):
    form = BlockForm(request.POST)
    if not form.is_valid():
        return HttpResponseBadRequest('Choose a member from visible community content.')
    kind, target_id = form.cleaned_data['target_type'], form.cleaned_data['target_id']
    if kind == 'room_post':
        from local_rooms.models import RoomPost
        from local_rooms.services import can_read_room, room_posts
        target = get_object_or_404(RoomPost.objects.select_related('room'), pk=target_id)
        if not can_read_room(target.room, request.user):
            raise Http404('This community contribution is unavailable.')
        item = get_object_or_404(room_posts(room=target.room, user=request.user), pk=target_id)
    else:
        item = services.get_public_question(target_id) if kind == 'question' else services.get_public_answer(target_id)
    try:
        services.set_block(user=request.user, blocked_user=item.author, blocked=True)
    except ValidationError as error:
        messages.error(request, '; '.join(error.messages))
    else:
        messages.success(request, 'Member blocked for interactions and notifications. Public pages remain readable, including as a guest.')
    return redirect('community:blocks')


@login_required
@require_POST
@rate_limit('community-block', 15)
def unblock(request, pk):
    entry = get_object_or_404(CommunityBlock.objects.select_related('blocked_user'), pk=pk, user=request.user)
    services.set_block(user=request.user, blocked_user=entry.blocked_user, blocked=False)
    messages.success(request, 'Block removed. Notification preferences remain under your control.')
    return redirect('community:blocks')
