#!/usr/bin/env python3
"""shopxz-japan-launch CLI. All output is concise UTF-8 JSON on stdout.

  profile          generate a profile from a packaged preset
  inspect          read-only snapshot of the currently bound store
  plan             offline plan from profile + snapshot
  apply / resume   journaled writes for a reviewed plan
  verify           read-only verification report (JSON + HTML)
  validate-assets  check packaged assets
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from jplaunch import VERSION  # noqa: E402
from jplaunch.util import LaunchError, dumps, read_json, sha256_file, write_json_atomic, safe_output_file, \
    safe_workdir, SKILL_ROOT  # noqa: E402

LIVE_HELP = "Uses the currently bound fecify-site-manager-v6 session; never saves credentials or switches stores."


def _emit(obj, code=0):
    sys.stdout.write(dumps(obj) + "\n")
    sys.stdout.flush()
    return code


def _manager(args, workdir):
    from jplaunch.adapter import ManagerAdapter, discover_manager_skill, resolve_node
    skill_dir = discover_manager_skill(args.manager_skill, skill_root=SKILL_ROOT)
    node = resolve_node(args.node)
    return ManagerAdapter(skill_dir, node, workdir / ".io", timeout=args.timeout)


def _load(path, what):
    try:
        return read_json(path)
    except (OSError, ValueError) as exc:
        raise LaunchError("INPUT_UNREADABLE", f"cannot read {what}: {exc}")


def cmd_profile(args):
    from jplaunch.profile import build_profile, validate_profile
    out = safe_output_file(args.out)
    if out.exists():
        raise LaunchError("FILE_EXISTS", f"refusing to overwrite {out}")
    profile = build_profile(args.preset)
    report = validate_profile(profile)
    write_json_atomic(out, profile)
    return _emit({"ok": not report["errors"], "profile": str(out), "preset": args.preset, **report})


def cmd_inspect(args):
    from jplaunch.inspector import inspect
    workdir = safe_workdir(args.workdir)
    profile = _load(args.profile, "profile")
    return _emit(inspect(profile, _manager(args, workdir), workdir))


def cmd_plan(args):
    from jplaunch.planner import build_plan
    out = safe_output_file(args.out)
    if out.exists():
        raise LaunchError("FILE_EXISTS", f"refusing to overwrite {out}")
    profile = _load(args.profile, "profile")
    snapshot = _load(args.snapshot, "snapshot")
    plan = build_plan(profile, snapshot, sha256_file(args.snapshot))
    write_json_atomic(out, plan)
    return _emit({"ok": plan["runnable"], "plan": str(out), "fingerprint": plan["fingerprint"],
                  "runnable": plan["runnable"], "selection": plan["selection"], "summary": plan["summary"],
                  "blockers": [{k: b[k] for k in ("scope", "code", "message")} for b in plan["blockers"]],
                  "agent_tasks": len(plan["agent_tasks"])})


def _run_engine(args, mode):
    from jplaunch.engine import Engine
    workdir = safe_workdir(args.workdir)
    plan = _load(args.plan, "plan")
    result = Engine(plan, _manager(args, workdir), workdir).run(mode)
    return _emit(result, 0 if result["ok"] else 1)


def cmd_verify(args):
    from jplaunch.verify import verify
    workdir = safe_workdir(args.workdir)
    plan = _load(args.plan, "plan")
    return _emit(verify(plan, _manager(args, workdir), workdir))


def cmd_validate_assets(args):
    from jplaunch.assets import validate_assets
    result = validate_assets()
    return _emit(result, 0 if result["ok"] else 1)


def _live_args(p):
    p.add_argument("--workdir", required=True, help="artifact directory outside the skill package")
    p.add_argument("--manager-skill", help="fecify-site-manager-v6 directory (else FECIFY_SKILL_DIR, sibling, CODEX_HOME, ~/.codex/skills, ~/.claude/skills)")
    p.add_argument("--node", help="node executable (else NODE_BINARY, PATH)")
    p.add_argument("--timeout", type=int, default=90, help="per-call timeout seconds (default 90)")


def build_parser():
    parser = argparse.ArgumentParser(prog="launch.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=VERSION)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("profile", help="generate a profile from a preset")
    p.add_argument("--preset", required=True, choices=["jp-core", "jp-apparel"])
    p.add_argument("--out", required=True, help="new profile JSON path (outside the skill)")
    p.set_defaults(func=cmd_profile)

    p = sub.add_parser("inspect", help="read-only snapshot of the bound store", description=LIVE_HELP)
    p.add_argument("--profile", required=True)
    _live_args(p)
    p.set_defaults(func=cmd_inspect)

    p = sub.add_parser("plan", help="offline plan from profile + snapshot (no Node/network)")
    p.add_argument("--profile", required=True)
    p.add_argument("--snapshot", required=True)
    p.add_argument("--out", required=True, help="new plan JSON path (outside the skill)")
    p.set_defaults(func=cmd_plan)

    for name, helptext in (("apply", "execute a reviewed runnable plan"),
                           ("resume", "reconcile uncertain operations, then continue")):
        p = sub.add_parser(name, help=helptext, description=LIVE_HELP)
        p.add_argument("--plan", required=True)
        _live_args(p)
        p.set_defaults(func=lambda a, m=name: _run_engine(a, m))

    p = sub.add_parser("verify", help="read-only verification report", description=LIVE_HELP)
    p.add_argument("--plan", required=True)
    _live_args(p)
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("validate-assets", help="validate packaged assets")
    p.set_defaults(func=cmd_validate_assets)
    return parser


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except LaunchError as exc:
        return _emit(exc.to_dict(), 1)


if __name__ == "__main__":
    sys.exit(main())
