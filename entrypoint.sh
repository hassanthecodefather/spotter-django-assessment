#!/bin/sh
set -e


python manage.py migrate --noinput
python manage.py createcachetable
python manage.py collectstatic --noinput
python manage.py load_stations --skip-if-loaded

exec gunicorn fuelroutes.wsgi \
  --bind 0.0.0.0:8000 \
  --workers 2 \
  --timeout 30 \
  --worker-tmp-dir /tmp \
  --access-logfile - \
  --error-logfile -
