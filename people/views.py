from django.contrib import messages
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import Group
from django.contrib.auth.decorators import login_required, user_passes_test
from django.shortcuts import redirect, render

from .forms import PersonRecordForm
from .models import PersonRecord


def is_admin_role(user):
    return user.is_authenticated and (
        user.is_superuser or user.groups.filter(name="admins").exists()
    )


def is_user_role(user):
    return user.is_authenticated and user.groups.filter(name="users").exists()


@login_required
def home(request):
    can_add = is_admin_role(request.user)
    can_fetch = is_user_role(request.user)
    show_data = request.method == "POST" and "fetch_data" in request.POST and can_fetch
    records = PersonRecord.objects.all() if show_data else []

    context = {
        "can_add": can_add,
        "can_fetch": can_fetch,
        "show_data": show_data,
        "records": records,
    }
    return render(request, "people/home.html", context)


@login_required
@user_passes_test(is_admin_role)
def add_record(request):
    if request.method == "POST":
        form = PersonRecordForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "Запись добавлена.")
            return redirect("add_record")
    else:
        form = PersonRecordForm()

    return render(request, "people/add_record.html", {"form": form})


def register(request):
    if request.user.is_authenticated:
        return redirect("home")

    if request.method == "POST":
        form = UserCreationForm(request.POST)
        if form.is_valid():
            user = form.save()
            users_group, _ = Group.objects.get_or_create(name="users")
            user.groups.add(users_group)
            messages.success(
                request,
                "Пользователь зарегистрирован. Войдите в систему.",
            )
            return redirect("login")
    else:
        form = UserCreationForm()

    return render(request, "registration/register.html", {"form": form})
