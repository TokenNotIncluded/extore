"""Offline CLI entry for contributing reviewed processors through GitHub PRs."""

import json
import sys
from pathlib import Path

from .processor_contributions import (
    LANGUAGES,
    PROPOSAL_LIMITS,
    contribution_contract,
    contribution_prompt,
    validate_proposal,
)


def add_parser(commands):
    parent = commands.add_parser(
        "processors", help="offline product-processor developer tools"
    )
    children = parent.add_subparsers(dest="processor_command", required=True)
    contribute = children.add_parser(
        "contribute", help="prepare links, steps and an AI prompt for a reviewed PR"
    )
    contribute.add_argument(
        "--language", default="zh-CN", metavar="{zh-CN,en}", help="prompt language"
    )
    for field, limit in PROPOSAL_LIMITS.items():
        contribute.add_argument(
            "--" + field,
            default="",
            help=f"proposal data only; at most {limit} characters",
        )
    contribute.add_argument(
        "--output", type=Path, help="create a private JSON file; never overwrite"
    )
    return parent


def _emit(args, result):
    if args.output is None:
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return
    from .manage_commands import OutputFile

    with OutputFile(args, "processor-contribution") as output:
        status = output.write(result)
    print(json.dumps(status, ensure_ascii=False, separators=(",", ":")))


def run(args):
    from .manage_client import ManageError

    try:
        if args.language not in LANGUAGES:
            raise ValueError("invalid contribution language")
        proposal = validate_proposal(
            {key: getattr(args, key) for key in PROPOSAL_LIMITS}
        )
        result = {
            "ok": True,
            "contribution": contribution_contract(),
            "language": args.language,
            "proposal": proposal,
            "prompt": contribution_prompt(args.language, proposal),
        }
    except (ValueError, UnicodeError):
        code = "invalid_input"
    else:
        try:
            _emit(args, result)
            return result
        except ManageError as error:
            code = "output_exists" if error.code == "output_exists" else "unsafe_output"
        except OSError:
            code = "unsafe_output"
    messages = {
        "invalid_input": "Use zh-CN or en and bounded text proposal fields without invalid characters.",
        "output_exists": "Output already exists; choose a new file.",
        "unsafe_output": "Cannot create a private contribution output file.",
    }
    print(
        json.dumps({"ok": False, "error": code, "message": messages[code]}),
        file=sys.stderr,
    )
    raise SystemExit(2)
