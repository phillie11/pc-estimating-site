# Siteline web/worker image for AWS ECS Fargate.
# Build from this folder:  docker build --platform linux/amd64 -t siteline .
# infra/scripts/deploy.sh builds, pushes and rolls it out for you.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# The app runs as this unprivileged user, never as root
RUN groupadd --system --gid 10001 siteline \
 && useradd --system --uid 10001 --gid siteline --home-dir /app --no-create-home --shell /usr/sbin/nologin siteline

# mysqlclient is only for PythonAnywhere's MySQL; on AWS the database is PostgreSQL (psycopg)
COPY requirements.txt .
RUN grep -v '^mysqlclient' requirements.txt > /tmp/requirements-aws.txt \
 && pip install -r /tmp/requirements-aws.txt \
 && rm /tmp/requirements-aws.txt

COPY . .

# Static files are collected into the image and served by WhiteNoise.
# The SECRET_KEY here only exists for this one build command; the real one comes from Secrets Manager at run time.
RUN DEBUG=0 SECRET_KEY=collectstatic-only-not-a-secret python manage.py collectstatic --noinput \
 && mkdir -p /app/private_media \
 && chown siteline:siteline /app/private_media

USER siteline
EXPOSE 8000

# The worker and scheduled tasks override this with `python manage.py ...`
CMD ["gunicorn", "--config", "config/gunicorn.conf.py", "config.alb_wsgi:application"]
