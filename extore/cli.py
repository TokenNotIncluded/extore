import argparse
import getpass
import os
import secrets
import time
import uuid

from argon2 import PasswordHasher

from .config import DATA
from .db import audit, db, init, set_setting, setting
from .models import Product
from .security import digest, token
from .service import issue_cards


def main():
    parser = argparse.ArgumentParser(description="Extore server administration")
    parser.add_argument(
        "command",
        choices=["init", "bootstrap", "reset-auth", "integration-key", "demo"],
    )
    args = parser.parse_args()
    init()
    if args.command in ("init", "bootstrap", "reset-auth"):
        with db() as c:
            exists = (
                setting(c, "bootstrap_password")
                or c.execute("SELECT count(*) FROM credentials").fetchone()[0]
            )
        if args.command in ("init", "bootstrap") and exists:
            raise SystemExit("Already initialized; use reset-auth to recover")
        if (
            args.command == "reset-auth"
            and input("删除所有 Passkey 并撤销全部登录会话？输入 RESET: ") != "RESET"
        ):
            raise SystemExit("Cancelled")
        if args.command == "bootstrap":
            password = secrets.token_urlsafe(32)
            fd = os.open(
                DATA / "bootstrap-password.txt",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(fd, "w") as f:
                f.write(password + "\n")
        else:
            password = getpass.getpass("设置首次登录密码（至少 12 字符）: ")
            if len(password) < 12 or password != getpass.getpass("再次输入: "):
                raise SystemExit("Password too short or does not match")
        with db() as c:
            c.execute("DELETE FROM credentials")
            from .link_access import revoke_all_sessions

            revoke_all_sessions(c, "ssh", "session.auth_reset")
            c.execute("DELETE FROM challenges")
            set_setting(c, "bootstrap_password", PasswordHasher().hash(password))
            audit(c, "ssh", "auth." + args.command, "owner")
        if args.command != "bootstrap":
            (DATA / "bootstrap-password.txt").unlink(missing_ok=True)
        print("密码登录已启用。登录后注册 Passkey，密码会立即失效。")
        if args.command == "bootstrap":
            print(
                "首次密码保存在数据目录 bootstrap-password.txt，仅服务器用户可读；不会输出到日志。"
            )
    elif args.command == "integration-key":
        value = token()
        with db() as c:
            set_setting(c, "integration_key", digest(value))
            audit(c, "ssh", "integration.rotate", "platform")
        print("仅显示一次的平台制卡密钥：", value)
    elif args.command == "demo":
        with db() as c:
            if c.execute("SELECT 1 FROM products LIMIT 1").fetchone():
                raise SystemExit("Demo only works on an empty database")
            p = Product(
                name="示例 · 欢迎函",
                description="本地演示商品。提交昵称后自动生成一封欢迎函，不会操作外部平台。",
                public=True,
                mode="script",
                processor_id="personalized_text",
                processor_config={"template": "你好，$name！\n你的欢迎函已准备好。"},
            )
            pid = str(uuid.uuid4())
            c.execute(
                "INSERT INTO products VALUES (?,?,?)",
                (pid, p.model_dump_json(), time.time()),
            )
            code = issue_cards(c, pid, 1)[0]
        print("演示卡密：", code)


if __name__ == "__main__":
    main()
