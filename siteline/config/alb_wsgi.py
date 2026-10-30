"""
WSGI entry point on AWS: gunicorn in ECS Fargate behind the Application Load Balancer.

It is config/wsgi.py plus two adjustments the load balancer needs. PythonAnywhere
keeps using config/wsgi.py and is unaffected.

1. Health checks. The ALB calls /health/ on each task's private IP over plain HTTP,
   with that IP as the Host header. Django would reject it (ALLOWED_HOSTS) or
   redirect it to HTTPS, and the ALB would mark every task unhealthy. For that one
   path only, the request is presented as arriving on the real domain over HTTPS,
   so it still runs through Django and proves the app is up.

2. Real client IP addresses. Every request reaches gunicorn from the ALB, so
   REMOTE_ADDR is the ALB's address, and django-axes (which locks by email + IP)
   would treat every visitor as one IP. The ALB appends the connecting client's
   address as the LAST entry of X-Forwarded-For. Only that entry is trustworthy
   (anything before it came from the client), and it is only trustworthy because
   the security group lets nothing but the ALB reach the tasks.
"""
import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

_django_app = get_wsgi_application()

from django.conf import settings  # noqa: E402  (settings are loaded by the line above)

_HEALTH_HOST = next((h for h in settings.ALLOWED_HOSTS if h and h[0] not in '.*'), 'localhost')


def application(environ, start_response):
    if environ.get('PATH_INFO') == '/health/':
        environ['HTTP_HOST'] = _HEALTH_HOST
        environ['HTTP_X_FORWARDED_PROTO'] = 'https'
    forwarded_for = environ.get('HTTP_X_FORWARDED_FOR', '')
    if forwarded_for:
        environ['REMOTE_ADDR'] = forwarded_for.split(',')[-1].strip()
    return _django_app(environ, start_response)
