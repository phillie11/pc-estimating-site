"""gunicorn settings for the Docker image (AWS ECS Fargate). Each value can be overridden with an env var."""
import os

bind = f"0.0.0.0:{os.environ.get('PORT', '8000')}"

# Sized for a 0.5 vCPU / 1 GB Fargate task: 2 processes x 4 threads.
workers = int(os.environ.get('GUNICORN_WORKERS', '2'))
threads = int(os.environ.get('GUNICORN_THREADS', '4'))
# Workers' heartbeat files, in memory: the gunicorn docs' advice for containers, where /tmp is on the
# overlay disk and a slow write gets workers killed. Nothing secret is written here.
worker_tmp_dir = '/dev/shm'  # nosec B108

timeout = int(os.environ.get('GUNICORN_TIMEOUT', '60'))
graceful_timeout = 30
# Longer than the ALB's 60-second idle timeout, so gunicorn never closes a connection the ALB is about to reuse (502s)
keepalive = 75

# Restart each process now and then so a slow memory leak can never fill the task
max_requests = 1000
max_requests_jitter = 100

accesslog = '-'
errorlog = '-'
loglevel = os.environ.get('GUNICORN_LOG_LEVEL', 'info')
# Client IP is the last X-Forwarded-For entry (added by the ALB); see config/alb_wsgi.py
access_log_format = '%({x-forwarded-for}i)s "%(r)s" %(s)s %(b)s %(M)sms "%(a)s"'
