"""Atomic community actions and explicit public and owner-only projections."""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Count, Q
from django.http import Http404
from django.utils import timezone

from .models import (
    Answer, AnswerRevision, CommunityBlock, CommunityReport, ContentStatus,
    HelpfulReaction, Question, QuestionFollow, QuestionRevision, ReportRevision,
    validate_public_url,
)


QUESTION_FIELDS = ('title', 'body', 'category', 'state', 'lga', 'ppa', 'group')
ANSWER_FIELDS = ('body', 'source_url')
COMMUNITY_CAUTION = 'Community suggestions, not official NYSC guidance. Confirm decisions with your designated NYSC official.'


def _require_member(user):
    if not getattr(user, 'is_authenticated', False) or not user.is_active:
        raise PermissionDenied('Sign in to use this private community action.')


def _lock_member(user, *, verified=True):
    _require_member(user)
    fresh = get_user_model().objects.select_for_update().filter(pk=user.pk, is_active=True).first()
    if fresh is None or (verified and not fresh.email_verified):
        raise PermissionDenied('Verify your email before posting public community content.')
    return fresh


def _lock_moderator(actor, permission):
    fresh = _lock_member(actor, verified=False)
    if not fresh.has_perm('community.' + permission):
        raise PermissionDenied('You do not have permission for this moderation action.')
    return fresh


def _id(value):
    return getattr(value, 'pk', value)


def interaction_blocked(a, b):
    """A private block prevents interaction in either direction across communities."""
    a, b = _id(a), _id(b)
    if not a or not b or a == b:
        return False
    return CommunityBlock.objects.filter(Q(user_id=a, blocked_user_id=b) | Q(user_id=b, blocked_user_id=a)).exists()


def blocked_user_ids(user):
    _require_member(user)
    rows = CommunityBlock.objects.filter(Q(user=user) | Q(blocked_user=user)).values_list('user_id', 'blocked_user_id')
    return {other if member == user.pk else member for member, other in rows}


def _check_interaction(user, *authors):
    for author_id in set(authors):
        if author_id and interaction_blocked(user.pk, author_id):
            raise PermissionDenied('This community interaction is unavailable.')


def _check_version(content, expected_version):
    if expected_version is not None and content.version != expected_version:
        raise ValidationError('This contribution changed since you opened it. Reload it before saving.')


def _check_write_allowance(user, *, creating=None):
    now = timezone.now()
    if creating:
        maximum = 20 if creating is Question else 50
        if creating.objects.filter(author=user, created_at__gte=now-timedelta(days=1)).count() >= maximum:
            raise ValidationError('Your daily community contribution allowance has been reached.')
        recent = (Question.objects.filter(author=user, created_at__gte=now-timedelta(minutes=5)).count()
                  + Answer.objects.filter(author=user, created_at__gte=now-timedelta(minutes=5)).count())
        if recent >= 5:
            raise ValidationError('Please wait before adding another community contribution.')
    recent_edits = (QuestionRevision.objects.filter(actor=user, created_at__gte=now-timedelta(minutes=5)).count()
                    + AnswerRevision.objects.filter(actor=user, created_at__gte=now-timedelta(minutes=5)).count())
    if recent_edits >= 30:
        raise ValidationError('Please wait before making more community changes.')


def _snapshot(content):
    # This deliberately omits author IDs, moderator identities and private reasons.
    fields = QUESTION_FIELDS if isinstance(content, Question) else ANSWER_FIELDS
    result = {}
    for name in (*fields, 'status', 'version'):
        field = content._meta.get_field(name)
        value = getattr(content, field.attname)
        result[field.attname] = value.isoformat() if hasattr(value, 'isoformat') else value
    if isinstance(content, Question):
        result['resolved'] = content.resolved
    return result


def _log(content, actor, reason):
    model, field = (QuestionRevision, 'question') if isinstance(content, Question) else (AnswerRevision, 'answer')
    model.objects.create(**{field: content}, actor=actor, snapshot=_snapshot(content), reason=reason)


def _question_locked(question):
    row = Question.objects.select_for_update().filter(pk=_id(question)).first()
    if row is None:
        raise Http404('Question unavailable.')
    return row


def _answer_locked(answer):
    # Parent-first locking gives every answer writer the same lock order.
    parent_id = Answer.objects.filter(pk=_id(answer)).values_list('question_id', flat=True).first()
    if parent_id is None:
        raise Http404('Answer unavailable.')
    parent = _question_locked(parent_id)
    row = Answer.objects.select_for_update().get(pk=_id(answer))
    row.question = parent
    return row


def _assert_owner(content, user):
    if content.author_id != user.pk:
        raise PermissionDenied('Only the author can change this contribution.')


def _assert_public_question(question):
    if question.status != ContentStatus.VISIBLE:
        raise Http404('Question unavailable.')


def _assert_public_answer(answer):
    _assert_public_question(answer.question)
    if answer.status != ContentStatus.VISIBLE:
        raise Http404('Answer unavailable.')


@transaction.atomic
def save_question(*, user, cleaned_data, question=None, expected_version=None):
    user = _lock_member(user)
    created = question is None
    question = Question(author=user) if created else _question_locked(question)
    if not created:
        _assert_owner(question, user)
        _check_version(question, expected_version)
        if question.status == ContentStatus.ARCHIVED:
            raise ValidationError('Archived questions are retained as history.')
    _check_write_allowance(user, creating=Question if created else None)
    for name in QUESTION_FIELDS:
        if name in cleaned_data:
            setattr(question, name, cleaned_data[name])
    question.title = ' '.join(question.title.split())
    question.body = question.body.strip()
    duplicates = Question.objects.filter(author=user, title__iexact=question.title, lga_id=question.lga_id).exclude(status=ContentStatus.ARCHIVED)
    if question.pk:
        duplicates = duplicates.exclude(pk=question.pk)
    if duplicates.exists():
        raise ValidationError('You already asked this question in this area. Edit that question instead.')
    # Recheck linked sources while holding their rows, so a moderation change
    # cannot race a new public reference. Source content is never copied.
    if question.ppa_id:
        from ppas.models import PPA
        PPA.objects.select_for_update().filter(pk=question.ppa_id).exists()
    if question.group_id:
        from service_resources.models import CDSGroup
        CDSGroup.objects.select_for_update().filter(pk=question.group_id).exists()
    if not created:
        question.version += 1
    question.full_clean()
    question.save()
    _log(question, user, 'Question created.' if created else 'Author edited the question.')
    return question


@transaction.atomic
def save_answer(*, user, question, cleaned_data, answer=None, expected_version=None):
    user = _lock_member(user)
    created = answer is None
    if created:
        question = _question_locked(question)
        _assert_public_question(question)
        _check_interaction(user, question.author_id)
        answer = Answer(question=question, author=user)
    else:
        answer = _answer_locked(answer)
        if answer.question_id != _id(question):
            raise PermissionDenied('This answer does not belong to this question.')
        _assert_owner(answer, user)
        _check_version(answer, expected_version)
        if answer.status == ContentStatus.ARCHIVED:
            raise ValidationError('Archived answers are retained as history.')
        _check_interaction(user, answer.question.author_id)
    _check_write_allowance(user, creating=Answer if created else None)
    for name in ANSWER_FIELDS:
        if name in cleaned_data:
            setattr(answer, name, cleaned_data[name])
    answer.body = answer.body.strip()
    answer.source_url = answer.source_url.strip()
    if not created:
        answer.version += 1
    answer.full_clean()
    answer.save()
    _log(answer, user, 'Answer created.' if created else 'Author edited the answer.')
    if created:
        # Durable activity is recorded in the same transaction. Dispatch is a
        # separate opt-in in-app workflow; no external messages are sent here.
        from notifications.community_services import record_answer_activity
        record_answer_activity(answer)
    return answer


@transaction.atomic
def archive_question(*, user, question):
    user = _lock_member(user, verified=False)
    question = _question_locked(question)
    _assert_owner(question, user)
    if question.status != ContentStatus.ARCHIVED:
        _check_write_allowance(user)
        question.status = ContentStatus.ARCHIVED
        question.version += 1
        question.save(update_fields=['status', 'version', 'updated_at'])
        _log(question, user, 'Author archived the question; answers remain retained.')
    return question


@transaction.atomic
def archive_answer(*, user, answer):
    user = _lock_member(user, verified=False)
    answer = _answer_locked(answer)
    _assert_owner(answer, user)
    if answer.status != ContentStatus.ARCHIVED:
        _check_write_allowance(user)
        answer.status = ContentStatus.ARCHIVED
        answer.version += 1
        answer.save(update_fields=['status', 'version', 'updated_at'])
        _log(answer, user, 'Author archived the answer.')
    return answer


@transaction.atomic
def set_question_resolved(*, user, question, resolved):
    user = _lock_member(user, verified=False)
    question = _question_locked(question)
    _assert_owner(question, user)
    if question.status == ContentStatus.ARCHIVED:
        raise ValidationError('Archived questions are retained as history.')
    if question.resolved != bool(resolved):
        _check_write_allowance(user)
        question.resolved = bool(resolved)
        question.version += 1
        question.save(update_fields=['resolved', 'version', 'updated_at'])
        _log(question, user, 'Author changed the resolved marker.')
    return question


def _moderate(content, actor, status, reason):
    reason = reason.strip()
    if not reason or len(reason) > 1000:
        raise ValidationError('Give a moderation reason of at most 1000 characters.')
    if status not in (ContentStatus.VISIBLE, ContentStatus.HIDDEN):
        raise ValidationError('Moderators can hide or restore a contribution. Authors control archiving.')
    if content.status == ContentStatus.ARCHIVED:
        raise ValidationError('An author-archived contribution cannot be restored by moderation.')
    content.status = status
    content.version += 1
    content.save(update_fields=['status', 'version', 'updated_at'])
    _log(content, actor, reason)
    return content


@transaction.atomic
def moderate_question(*, question, actor, status, reason):
    actor = _lock_moderator(actor, 'can_moderate_community')
    return _moderate(_question_locked(question), actor, status, reason)


@transaction.atomic
def moderate_answer(*, answer, actor, status, reason):
    actor = _lock_moderator(actor, 'can_moderate_community')
    return _moderate(_answer_locked(answer), actor, status, reason)


def public_questions():
    return Question.objects.filter(status=ContentStatus.VISIBLE).select_related('state', 'lga', 'ppa', 'group', 'author').annotate(
        answer_count=Count('answers', filter=Q(answers__status=ContentStatus.VISIBLE), distinct=True),
        helpful_count=Count('answers__helpful_reactions', filter=Q(answers__status=ContentStatus.VISIBLE), distinct=True),
    )


def public_answers(question):
    return Answer.objects.filter(question_id=_id(question), status=ContentStatus.VISIBLE, question__status=ContentStatus.VISIBLE).select_related('author', 'question').annotate(helpful_count=Count('helpful_reactions', distinct=True))


def get_public_question(pk):
    question = public_questions().filter(pk=pk).first()
    if question is None:
        raise Http404('Question unavailable.')
    return question


def get_public_answer(pk):
    answer = Answer.objects.filter(pk=pk, status=ContentStatus.VISIBLE, question__status=ContentStatus.VISIBLE).select_related('question', 'author').annotate(helpful_count=Count('helpful_reactions', distinct=True)).first()
    if answer is None:
        raise Http404('Answer unavailable.')
    return answer


def _question_projection(question, user=None):
    from ppas.models import PPA
    from service_resources.models import CDSGroup
    ppa = question.ppa if question.ppa_id else None
    if ppa and (ppa.status in (PPA.Status.CLOSED, PPA.Status.DISPUTED) or ppa.location_status == PPA.LocationStatus.MISMATCH or ppa.state_id != question.state_id or ppa.lga_id != question.lga_id):
        ppa = None
    group = question.group if question.group_id else None
    if group and (group.publication_status != CDSGroup.Publication.APPROVED or group.expires_at <= timezone.now() or group.state_id != question.state_id or group.lga_id != question.lga_id):
        group = None
    return dict(
        id=question.pk, title=question.title, body=question.body, category=question.category,
        category_label=question.get_category_display(), state_id=question.state_id, state_name=question.state.name,
        lga_id=question.lga_id, lga_name=question.lga.name, ppa_id=ppa.pk if ppa else None, ppa_name=ppa.name if ppa else '',
        group_id=group.pk if group else None, group_title=group.title if group else '',
        status=question.status, status_label=question.get_status_display(), resolved=question.resolved,
        version=question.version, created_at=question.created_at, updated_at=question.updated_at,
        author_name=question.author.public_name if question.author else 'Former member',
        is_owner=bool(user and user.is_authenticated and question.author_id == user.pk),
        answer_count=getattr(question, 'answer_count', 0), community_caution=COMMUNITY_CAUTION,
        helpful_count=getattr(question, 'helpful_count', 0),
    )


def safe_public_question(question, *, user=None):
    _assert_public_question(question)
    return _question_projection(question, user)


def safe_owner_question(question, *, user):
    _require_member(user)
    _assert_owner(question, user)
    return _question_projection(question, user)


def _answer_projection(answer, user=None):
    source_url = answer.source_url
    try:
        validate_public_url(source_url)
    except ValidationError:
        source_url = ''
    return dict(
        id=answer.pk, question_id=answer.question_id, body=answer.body, source_url=source_url,
        status=answer.status, status_label=answer.get_status_display(), version=answer.version,
        created_at=answer.created_at, updated_at=answer.updated_at,
        author_name=answer.author.public_name if answer.author else 'Former member',
        is_owner=bool(user and user.is_authenticated and answer.author_id == user.pk),
        helpful_count=getattr(answer, 'helpful_count', 0), community_caution=COMMUNITY_CAUTION,
    )


def safe_public_answer(answer, *, user=None):
    _assert_public_answer(answer)
    return _answer_projection(answer, user)


def safe_owner_answer(answer, *, user):
    _require_member(user)
    _assert_owner(answer, user)
    return _answer_projection(answer, user)


def owner_questions(user):
    _require_member(user)
    return Question.objects.filter(author=user).select_related('state', 'lga', 'ppa', 'group', 'author').annotate(answer_count=Count('answers', filter=Q(answers__status=ContentStatus.VISIBLE), distinct=True))


def owner_answers(user):
    _require_member(user)
    return Answer.objects.filter(author=user).select_related('question', 'author').annotate(helpful_count=Count('helpful_reactions', distinct=True)).order_by('-created_at', '-pk')


def owner_revisions(*, user, question=None, answer=None):
    _require_member(user)
    if bool(question) == bool(answer):
        raise ValidationError('Choose exactly one contribution history.')
    model, field, content_model, content = ((QuestionRevision, 'question', Question, question) if question is not None else (AnswerRevision, 'answer', Answer, answer))
    if not content_model.objects.filter(pk=_id(content), author_id=user.pk).exists():
        raise PermissionDenied('Only the author can read this history.')
    return model.objects.filter(**{field + '_id': _id(content)}).values('id', 'snapshot', 'created_at')


@transaction.atomic
def set_helpful(*, user, answer, helpful=True):
    user = _lock_member(user, verified=False)
    answer = _answer_locked(answer)
    _assert_public_answer(answer)
    _check_interaction(user, answer.author_id, answer.question.author_id)
    if helpful and answer.author_id == user.pk:
        raise ValidationError('You cannot mark your own answer helpful.')
    if helpful:
        reaction, _ = HelpfulReaction.objects.get_or_create(user=user, answer=answer)
        return reaction
    HelpfulReaction.objects.filter(user=user, answer=answer).delete()
    return None


@transaction.atomic
def set_follow(*, user, question, following=True, enabled=False):
    user = _lock_member(user, verified=False)
    question = _question_locked(question)
    if not following:
        QuestionFollow.objects.filter(user=user, question=question).delete()
        return None
    _assert_public_question(question)
    _check_interaction(user, question.author_id)
    follow, created = QuestionFollow.objects.get_or_create(user=user, question=question)
    enabled = bool(enabled)
    if enabled != follow.enabled or (enabled and follow.enabled_at is None):
        follow.enabled_at = timezone.now() if enabled else None
    follow.enabled = enabled
    follow.save(update_fields=['enabled', 'enabled_at', 'updated_at'])
    return follow


@transaction.atomic
def set_block(*, user, blocked_user, blocked=True):
    user = _lock_member(user, verified=False)
    target = get_user_model().objects.filter(pk=_id(blocked_user), is_active=True).first()
    if target is None:
        raise Http404('Member unavailable.')
    if user.pk == target.pk:
        raise ValidationError('You cannot block yourself.')
    if blocked:
        record, _ = CommunityBlock.objects.get_or_create(user=user, blocked_user=target)
        return record
    CommunityBlock.objects.filter(user=user, blocked_user=target).delete()
    return None


@transaction.atomic
def save_report(*, user, category, details, private_evidence='', question=None, answer=None):
    user = _lock_member(user, verified=False)
    if bool(question) == bool(answer):
        raise ValidationError('Report exactly one question or answer.')
    if question is not None:
        question = _question_locked(question)
        _assert_public_question(question)
    else:
        answer = _answer_locked(answer)
        _assert_public_answer(answer)
    if CommunityReport.objects.filter(reporter=user, created_at__gte=timezone.now()-timedelta(days=1)).count() >= 20:
        raise ValidationError('Your daily community report allowance has been reached.')
    report = CommunityReport(reporter=user, question=question, answer=answer, category=category, details=details.strip(), private_evidence=private_evidence.strip())
    report.full_clean()
    report.save()
    return report


@transaction.atomic
def moderate_report(*, report, actor, status, moderator_note):
    actor = _lock_moderator(actor, 'can_moderate_community_reports')
    report = CommunityReport.objects.select_for_update().get(pk=report.pk)
    note = moderator_note.strip()
    if not note:
        raise ValidationError('Explain how this report was reviewed.')
    previous = report.status
    report.status, report.moderator_note = status, note
    report.moderated_by, report.moderated_at = actor, timezone.now()
    report.full_clean()
    report.save(update_fields=['status', 'moderator_note', 'moderated_by', 'moderated_at', 'updated_at'])
    ReportRevision.objects.create(report=report, actor=actor, previous_status=previous, new_status=status, moderator_note=note)
    return report


def owner_reports(user):
    _require_member(user)
    return CommunityReport.objects.filter(reporter=user)


def safe_owner_report(report, *, user):
    _require_member(user)
    if report.reporter_id != user.pk:
        raise PermissionDenied('Only the reporter can read this report history.')
    # No current target title/body and no private reviewer notes or identities.
    return dict(id=report.pk, question_id=report.question_id, answer_id=report.answer_id,
                target_label='Question' if report.question_id else 'Answer',
                category=report.category, category_label=report.get_category_display(), details=report.details,
                private_evidence=report.private_evidence, status=report.status, status_label=report.get_status_display(),
                created_at=report.created_at, updated_at=report.updated_at)


def owner_report_revisions(*, report, user):
    _require_member(user)
    if not CommunityReport.objects.filter(pk=_id(report), reporter_id=user.pk).exists():
        raise PermissionDenied('Only the reporter can read this report history.')
    return ReportRevision.objects.filter(report_id=_id(report)).values('id', 'previous_status', 'new_status', 'created_at')
