import os
import pwd
import sys


RUNTIME_PATHS = (
    "/app/data",
    "/app/logs",
    "/app/static/thumbs",
)


def ensure_runtime_permissions(uid, gid):
    for base_path in RUNTIME_PATHS:
        os.makedirs(base_path, exist_ok=True)
        for root, dirnames, filenames in os.walk(base_path):
            os.chown(root, uid, gid)
            for dirname in dirnames:
                os.chown(os.path.join(root, dirname), uid, gid)
            for filename in filenames:
                os.chown(os.path.join(root, filename), uid, gid)


def main():
    cmd = sys.argv[1:] or ["gunicorn", "-c", "gunicorn.conf.py", "app:app"]
    if os.geteuid() != 0:
        os.execvp(cmd[0], cmd)

    app_user = pwd.getpwnam("app")
    ensure_runtime_permissions(app_user.pw_uid, app_user.pw_gid)
    os.environ["HOME"] = app_user.pw_dir
    os.setgid(app_user.pw_gid)
    os.setuid(app_user.pw_uid)
    os.execvp(cmd[0], cmd)


if __name__ == "__main__":
    main()
