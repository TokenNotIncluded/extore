import argparse
import getpass
import json
import os
import secrets
import time
import uuid
from importlib import metadata

from argon2 import PasswordHasher

from .config import DATA
from .db import audit, db, init, set_setting, setting
from .models import Product
from .security import digest, token
from .service import issue_cards


class VersionAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        try:
            value = metadata.version("extore")
        except metadata.PackageNotFoundError:
            parser.error("Extore package metadata is unavailable; install the project")
        print(f"extore {value}")
        parser.exit()


def port_number(value):
    try:
        port = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            "port must be an integer from 1 to 65535"
        ) from None
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be an integer from 1 to 65535")
    return port


def host_name(value):
    if not value or any(character.isspace() for character in value):
        raise argparse.ArgumentTypeError("host must be a non-empty address or hostname")
    return value


def add_version(parser):
    parser.add_argument(
        "--version",
        action=VersionAction,
        nargs=0,
        help="show installed package version",
    )


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="extore", description="Extore server and administration"
    )
    add_version(parser)
    commands = parser.add_subparsers(dest="command", required=True)
    descriptions = {
        "init": "set the first login password interactively",
        "bootstrap": "write a generated first login password to the data directory",
        "reset-auth": "recover authentication through server access",
        "integration-key": "generate or rotate the platform card-issuance key",
        "demo": "create a local demonstration product in an empty database",
        "serve": "run the API server in the foreground",
        "worker": "run the fulfillment worker in the foreground",
    }
    for command, description in descriptions.items():
        subparser = commands.add_parser(
            command, help=description, description=description
        )
        add_version(subparser)
        if command == "serve":
            subparser.add_argument("--host", type=host_name, default="127.0.0.1")
            subparser.add_argument("--port", type=port_number, default=8000)
    from .commerce_commands import add_parser as add_commerce_parser
    from .customer_cli import add_parser as add_customer_parser
    from .manage_client import add_parser as add_manage_parser
    from .owner_client import add_parser as add_owner_parser
    from .processor_contribution_commands import add_parser as add_processor_parser
    from .workflow_commands import add_parser as add_workflow_parser

    add_version(add_manage_parser(commands))
    add_version(add_customer_parser(commands))
    add_version(add_owner_parser(commands))
    add_version(add_workflow_parser(commands))
    add_version(add_processor_parser(commands))
    add_version(add_commerce_parser(commands))
    args = parser.parse_args(argv)
    if args.command == "commerce":
        from .commerce_commands import run as commerce_main

        return commerce_main(args)
    if args.command == "processors":
        from .processor_contribution_commands import run as processors_main

        processors_main(args)
        return 0
    if args.command == "workflow":
        from .workflow_commands import run as workflow_main

        workflow_main(args)
        return 0
    if args.command == "customer":
        from .customer_cli import run as customer_main

        return customer_main(args)
    if args.command == "admin":
        from .owner_client import run as owner_main

        return owner_main(args)
    if args.command == "manage":
        from .manage_client import main as manage_main

        return manage_main(args)
    if args.command == "serve":
        import uvicorn

        return uvicorn.run("extore.app:app", host=args.host, port=args.port)
    if args.command == "worker":
        from .worker import main as worker_main

        return worker_main()
    init()
    if args.command in ("init", "bootstrap", "reset-auth"):
        with db() as c:
            exists = (
                setting(c, "bootstrap_password")
                or c.execute(
                    "SELECT count(*) FROM credentials WHERE shop_id IS NULL"
                ).fetchone()[0]
            )
        if args.command in ("init", "bootstrap") and exists:
            raise SystemExit("Already initialized; use reset-auth to recover")
        if (
            args.command == "reset-auth"
            and input("删除超级管理员 Passkey 并撤销其登录会话？输入 RESET: ")
            != "RESET"
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
            from .account_auth import revoke_shop_auth

            revoke_shop_auth(c, None, "session.auth_reset")
            c.execute("DELETE FROM credentials WHERE shop_id IS NULL")
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
            from .shops import default_shop

            pid = str(uuid.uuid4())
            c.execute(
                "INSERT INTO products(id,config,created,shop_id) VALUES (?,?,?,?)",
                (pid, p.model_dump_json(), time.time(), default_shop(c)),
            )
            from .processor_profiles import persist_product_configuration

            values = p.model_dump()
            persist_product_configuration(c, pid, values)
            c.execute(
                "UPDATE products SET config=? WHERE id=?", (json.dumps(values), pid)
            )
            code = issue_cards(c, pid, 1)[0]
        print("演示卡密：", code)


if __name__ == "__main__":
    main()
