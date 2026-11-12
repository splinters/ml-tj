"""
Минимальная защита демо-стенда: HTTP Basic Auth на все маршруты.

Параметры - только через переменные окружения (в .env или в оболочке):
    DEMO_USER      логин (по умолчанию "demo")
    DEMO_PASSWORD  пароль; обязателен
    DEMO_AUTH=off  явное отключение защиты (только для локальной отладки)

Без DEMO_PASSWORD и без DEMO_AUTH=off приложение не стартует: защита не
должна отключаться из-за забытой переменной.

Что делает модуль:
- проверка логина/пароля за постоянное время (hmac.compare_digest);
- ограничение перебора: после MAX_FAILS неудач с одного IP за WINDOW_SEC
  секунд - ответ 429 (счётчик в памяти процесса, сбрасывается при рестарте);
- заголовки: nosniff, SAMEORIGIN, no-referrer, Cache-Control: no-store
  для /export*.

Что модуль не делает: Basic Auth передаёт пароль в base64 (не шифрует).
Вне localhost стенд нужно отдавать по HTTPS (reverse proxy: nginx/Caddy)
или через VPN. Роли пользователей, сессии и журнал входов не реализованы.
"""
import hmac
import os
import time
from collections import defaultdict, deque

from flask import Response, request

MAX_FAILS = 10
WINDOW_SEC = 300
_fails = defaultdict(deque)  # ip -> времена неудачных попыток


def _client_ip():
    return request.remote_addr or "unknown"


def _too_many_fails(ip):
    now = time.time()
    q = _fails[ip]
    while q and now - q[0] > WINDOW_SEC:
        q.popleft()
    return len(q) >= MAX_FAILS


def _unauthorized():
    return Response(
        "Требуется авторизация.\n", 401,
        {"WWW-Authenticate": 'Basic realm="ML TJ demo", charset="UTF-8"'},
    )


def _credentials_ok(user, password, expected_user, expected_password):
    # оба сравнения выполняются всегда, без раннего выхода
    u_ok = hmac.compare_digest(user.encode("utf-8"), expected_user.encode("utf-8"))
    p_ok = hmac.compare_digest(password.encode("utf-8"), expected_password.encode("utf-8"))
    return u_ok and p_ok


def init_auth(app):
    expected_user = os.getenv("DEMO_USER", "demo")
    expected_password = os.getenv("DEMO_PASSWORD", "")
    auth_off = os.getenv("DEMO_AUTH", "on").lower() == "off"

    if not expected_password and not auth_off:
        raise RuntimeError(
            "Не задан DEMO_PASSWORD. Задайте пароль (export DEMO_PASSWORD=...) "
            "или явно отключите защиту: DEMO_AUTH=off (только локальная отладка)."
        )

    app.config["AUTH_ENABLED"] = not auth_off

    if auth_off:
        app.logger.warning("DEMO_AUTH=off: защита отключена, не использовать вне localhost")
    else:
        @app.before_request
        def _guard():
            if request.endpoint == "healthz":
                return None
            ip = _client_ip()
            if _too_many_fails(ip):
                return Response("Слишком много попыток. Повторите позже.\n", 429,
                                {"Retry-After": str(WINDOW_SEC)})
            auth = request.authorization
            if auth is None or auth.type != "basic":
                return _unauthorized()
            if not _credentials_ok(auth.username or "", auth.password or "",
                                   expected_user, expected_password):
                _fails[ip].append(time.time())
                return _unauthorized()
            return None

    @app.after_request
    def _headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        # выгрузки не должны оседать в кэше браузера/прокси; карта и API
        # (hydrology.geojson ~34 МБ) кэшируются как обычно
        if request.path.startswith("/export"):
            resp.headers.setdefault("Cache-Control", "no-store")
        return resp

    @app.route("/healthz")
    def healthz():
        return "ok", 200
