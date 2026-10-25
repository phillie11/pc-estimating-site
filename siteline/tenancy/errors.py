from django.shortcuts import render


def not_found(request, exception=None):
    return render(request, 'errors/404.html', status=404)


def forbidden(request, exception=None):
    return render(request, 'errors/403.html', status=403)
