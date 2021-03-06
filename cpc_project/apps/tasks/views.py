from datetime import date
from itertools import chain
from operator import attrgetter


from django.shortcuts import render_to_response, get_object_or_404
from django.http import HttpResponse
from django.http import HttpResponseRedirect
from django.template import RequestContext
from django.core.urlresolvers import reverse
from django.core.exceptions import ImproperlyConfigured
from django.db.models import get_app
from django.db.models import Q

from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import Group
from django.contrib.auth.models import User
from django.contrib.contenttypes.models import ContentType

from tagging.models import Tag

from tasks.models import (Task, Nudge)

from tasks.forms import TaskForm, EditTaskForm, SearchTaskForm

from tasks.workflow import (REVERSE_STATE_CHOICES, STATE_ID_LIST, STATE_CHOICES,
                            RESOLUTION_CHOICES, STATE_CHOICES_DICT,
                            RESOLUTION_CHOICES_DICT)

from tasks.workflow import export_state_transitions as ext

try:
    notification = get_app('notification')
except ImproperlyConfigured:
    notification = None


def tasks(request, group_slug=None, template_name="tasks/task_list.html"):
    group = None # get_object_or_404(Project, slug=slug)

    # @@@ if group.deleted:
    # @@@     raise Http404

    is_member = True # @@@ groups.has_member(request.user)

    group_by = request.GET.get("group_by")


    if group:
        tasks = group.tasks.all() # @@@ assumes GR
    else:
        tasks = Task.objects.filter(object_id__isnull=True)

    # exclude states
    hide_state  = request.GET.get("hide_state")
    if hide_state:
        for exclude in hide_state.split(','):
            if exclude in STATE_ID_LIST:
                tasks = tasks.exclude(state__exact=exclude)

            state = REVERSE_STATE_CHOICES.get(exclude, None)
            if state:
                tasks = tasks.exclude(state__exact=state)


    state_displays = []
    for state in STATE_CHOICES:
        state_displays.append(dict(id=state[0], description=state[1]))

    return render_to_response(template_name, {
        "group": group,
        "tasks": tasks,
        "group_by": group_by,
        "is_member": is_member,
        "hide_state": hide_state,
        "state_displays": state_displays,
    }, context_instance=RequestContext(request))


def add_task(request, group_slug=None, form_class=TaskForm, template_name="tasks/add.html"):
    group = None # get_object_or_404(Project, slug=slug)

    # @@@ if group.deleted:
    # @@@     raise Http404

    if group:
        notify_list = group.member_users.all().exclude(id__exact=request.user.id) # @@@
    else:
        notify_list = User.objects.all().exclude(id__exact=request.user.id)

    is_member = True # @@@ groups.has_member(request.user)

    search_form = SearchTaskForm()
    search_results = []
    if request.method == "POST":
        action = request.POST.get('action', None)
        if action == 'search':
            search_form = SearchTaskForm(request.POST)
            search = request.POST.get('search', None)
            task_form = form_class(group=group)
            if search:
                qset = (
                    Q(summary__contains=search)|
                    Q(detail__contains=search)
                    )
                search_results = Task.objects.filter(qset).distinct()


        if request.user.is_authenticated() and not action:
            task_form = form_class(group, request.POST)
            if task_form.is_valid():
                task = task_form.save(commit=False)
                task.creator = request.user
                task.group = group
                # @@@ we should check that assignee is really a member
                task.save()
                task.save_history()
                request.user.message_set.create(message="added task '%s'" % task.summary)
                if notification:
                    notification.send(notify_list, "tasks_new", {"creator": request.user, "task": task, "group": group})
                if request.POST.has_key('add-another-task'):
                    return HttpResponseRedirect(reverse('task_add'))
                return HttpResponseRedirect(reverse("task_list"))
    else:
        task_form = form_class(group=group)

    return render_to_response(template_name, {
        "group": group,
        "is_member": is_member,
        "task_form": task_form,
        "search_form": search_form,
        "search_results":search_results
    }, context_instance=RequestContext(request))

@login_required
def nudge(request, id):
    """ Called when a user nudges a ticket """

    task = get_object_or_404(Task, id=id)
    task_url = task.get_absolute_url()

    nudged = Nudge.objects.filter(task__exact=task,nudger__exact=request.user)
    if nudged:
        # you've already nudged this task.
        nudge = nudged[0]
        nudge.delete()
        message = "You've removed your nudge from this task"
        request.user.message_set.create(message=message)
        return HttpResponseRedirect(task_url)


    nudge = Nudge(nudger = request.user, task = task)
    nudge.save()

    count = Nudge.objects.filter(task__exact=task).count()

    # send the message to the user
    message = "%s has been nudged about this task" % task.assignee
    request.user.message_set.create(message=message)

    # send out the nudge notification
    if notification:
        notify_list = [task.assignee]
        notification.send(notify_list, "tasks_nudge", {"nudger": request.user, "task": task, "count": count})

    return HttpResponseRedirect(task_url)

def task(request, id, template_name="tasks/task.html"):
    task = get_object_or_404(Task, id=id)
    group = task.group

    # @@@ if group.deleted:
    # @@@     raise Http404

    if group:
        notify_list = group.member_users.all().exclude(id__exact=request.user.id) # @@@
    else:
        notify_list = User.objects.all().exclude(id__exact=request.user.id)

    is_member = request.user.is_authenticated() # @@@ groups.has_member(request.user)

    if is_member and request.method == "POST":
        form = EditTaskForm(request.user, request.POST, instance=task)
        if form.is_valid():
            task = form.save()
            task.save_history(change_owner=request.user)
            if task.assignee == request.user:
                task.denudge()
            if "status" in form.changed_data:
                request.user.message_set.create(message="updated your status on the task")
                if notification:
                    notification.send(notify_list, "tasks_status", {"user": request.user, "task": task, "group": group})
            if "state" in form.changed_data:
                request.user.message_set.create(message="task marked %s" % task.get_state_display())
                if notification:
                    notification.send(notify_list, "tasks_change", {"user": request.user, "task": task, "group": group, "new_state": task.get_state_display()})
            if "assignee" in form.changed_data:
                request.user.message_set.create(message="assigned task to '%s'" % task.assignee)
                if notification:
                    notification.send(notify_list, "tasks_assignment", {"user": request.user, "task": task, "assignee": task.assignee, "group": group})
            if "tags" in form.changed_data:
                request.user.message_set.create(message="updated tags on the task")
                if notification:
                    notification.send(notify_list, "tasks_tags", {"user": request.user, "task": task, "group": group})
            form = EditTaskForm(request.user, instance=task)
    else:
        form = EditTaskForm(request.user, instance=task)

    # The NUDGE dictionary
    nudge = {}
    nudge['nudgeable'] = False

    # get the count of nudges so assignee can see general level of interest.
    nudge['count'] = Nudge.objects.filter(task__exact=task).count()

    # get the nudge if you are not the assignee otherwise just a None
    if is_member and request.user != task.assignee and task.assignee:
        nudge['nudgeable'] = True
        try:
            nudge['nudge'] = Nudge.objects.filter(nudger__exact=request.user, task__exact=task)[0]
        except IndexError:
            nudge['nudge'] = None

    # get the nudge history
    nudge['history'] = Nudge.objects.filter(task__exact=task)

    return render_to_response(template_name, {
        "nudge": nudge,
        "task": task,
        "is_member": is_member,
        "form": form,
    }, context_instance=RequestContext(request))


@login_required
def user_tasks(request, username, template_name="tasks/user_tasks.html"):
    other_user = get_object_or_404(User, username=username)
    assigned_tasks = other_user.assigned_tasks.all().order_by("state", "-modified") # @@@ filter(project__deleted=False)
    created_tasks = other_user.created_tasks.all().order_by("state", "-modified") # @@@ filter(project__deleted=False)

    # get the list of your tasks that have been nudged
    nudged_tasks =[x for x in other_user.assigned_tasks.all().order_by('-modified') if x.task_nudge.all()]

    url = reverse("tasks_mini_list")

    bookmarklet = """javascript:(
            function() {
                url = '%s';
                window.open(url, 'tasklist', 'height=500, width=250, title=no, location=no, scrollbars=yes, menubars=no, navigation=no, statusbar=no, directories=no, resizable=yes, status=no, toolbar=no, menuBar=no');
            }
        )()""" % url

    return render_to_response(template_name, {
        "assigned_tasks": assigned_tasks,
        "created_tasks": created_tasks,
        "nudged_tasks": nudged_tasks,
        "other_user": other_user,
        "bookmarklet": bookmarklet
    }, context_instance=RequestContext(request))


@login_required
def mini_list(request, template_name="tasks/mini_list.html"):
    assigned_tasks = request.user.assigned_tasks.all().exclude(state="2").exclude(state="3").order_by("state", "-modified") # @@@ filter(project__deleted=False)
    return render_to_response(template_name, {
        "assigned_tasks": assigned_tasks,
    }, context_instance=RequestContext(request))


def focus(request, field, value, group_slug=None, template_name="tasks/focus.html"):
    group = None # get_object_or_404(Project, slug=slug)

    # @@@ if group.deleted:
    # @@@     raise Http404

    is_member = True # @@@ groups.has_member(request.user)

    group_by = request.GET.get("group_by")

    if group:
        qs = group.tasks.all()
    else:
        qs = Task.objects.filter(object_id__isnull=True)

    if field == "modified":
        try:
            # @@@ this seems hackish and brittle but I couldn't work out another way
            year, month, day = value.split("-")
            # have to int month and day in case zero-padded
            tasks = qs.filter(modified__year=int(year), modified__month=int(month), modified__day=int(day))
        except:
            tasks = Task.objects.none() # @@@ or throw 404?
    elif field == "state":
        tasks = qs.filter(state=Task.REVERSE_STATE_CHOICES[value])
    elif field == "assignee":
        if value == "unassigned": # @@@ this means can't have a username 'unassigned':
            tasks = qs.filter(assignee__isnull=True)
        else:
            try:
                assignee = User.objects.get(username=value)
                tasks = qs.filter(assignee=assignee)
            except User.DoesNotExist:
                tasks = Task.objects.none() # @@@ or throw 404?
    elif field == "tag":
        try:
            # @@@ is there a better way?
            task_type = ContentType.objects.get_for_model(Task)
            tasks = Task.objects.filter(id__in=Tag.objects.get(name=value).items.filter(content_type=task_type).values_list("object_id", flat=True))
            # @@@ still need to filter on group if group not None
        except Tag.DoesNotExist:
            tasks = Task.objects.none() # @@@ or throw 404?
    else:
        tasks = qs

    return render_to_response(template_name, {
        "group": group,
        "tasks": tasks,
        "field": field,
        "value": value,
        "group_by": group_by,
        "is_member": is_member,
    }, context_instance=RequestContext(request))


def tasks_history(request, id, template_name="tasks/task_history.html"):
    task = get_object_or_404(Task, id=id)
    task_history = task.history_task.all().order_by('-modified')
    nudge_history = task.task_nudge.all().order_by('-modified')

    result_list = sorted(
        chain(task_history, nudge_history),
        key=attrgetter('modified')
        )
    result_list.reverse()


    for change in task_history:
        change.humanized_state = STATE_CHOICES_DICT.get(change.state, None)
        change.humanized_resolution = RESOLUTION_CHOICES_DICT.get(change.resolution, None)


    return render_to_response(template_name, {
        "task": task,
        "task_history": result_list,
        "nudge_history":nudge_history

    }, context_instance=RequestContext(request))

def export_state_transitions(request):
    export = ext()
    return HttpResponse(export,mimetype='text/csv')