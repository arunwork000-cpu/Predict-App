# Sports Prediction

A points-based sports prediction game built with Django. Users predict the
winner of a match before its deadline; the admin enters the result and each
prediction scores +10 (correct) or -5 (incorrect).

## Local development

```
python -m venv .venv
.venv/bin/pip install -r requirements.txt      # Windows: .venv\Scripts\pip
python manage.py migrate
python manage.py runserver
```

Local development uses SQLite (`db.sqlite3`) and needs no configuration. To
override anything locally, copy `.env.example` to `.env`.

## Production runtime

| | |
|---|---|
| OS | Linux |
| Python | 3.13 (see `.python-version`) |
| WSGI server | Gunicorn 23 |
| Gunicorn entrypoint | `config.wsgi:application` |

Set `DATABASE_URL` (PostgreSQL) and the other variables listed in
`.env.example`. Deploy-time steps: install `requirements.txt`, run
`python manage.py collectstatic --noinput`, run `python manage.py migrate`,
then start `gunicorn config.wsgi:application`.
