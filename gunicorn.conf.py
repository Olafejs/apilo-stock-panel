import os


bind = f"{os.getenv('APP_HOST', '127.0.0.1')}:{os.getenv('APP_PORT', '5080')}"
worker_class = "gthread"
workers = 1
threads = max(1, int(os.getenv("APP_THREADS", "4")))
timeout = max(30, int(os.getenv("APP_TIMEOUT", "120")))
graceful_timeout = max(30, int(os.getenv("APP_GRACEFUL_TIMEOUT", "30")))
keepalive = 5
accesslog = "-"
errorlog = "-"
loglevel = os.getenv("APP_LOG_LEVEL", "info")


def post_worker_init(worker):
    if os.getenv("BACKGROUND_REFRESH_ENABLED", "1") != "1":
        worker.log.info("Background refresh disabled for this runtime.")
        return
    from app import start_background_refresh

    started = start_background_refresh(debug_mode=False)
    if started:
        worker.log.info("Background refresh started in gunicorn worker pid=%s", worker.pid)
