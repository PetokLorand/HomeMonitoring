"""Command-line entry point: `acmonitor <command>`."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time

from .config import load_config


def _ctx(args):
    from .controller import Controller
    from .drivers import make_driver
    from .store import Store

    cfg = load_config(args.config)
    return cfg, Controller(cfg, Store(cfg.monitor.db_path), make_driver(cfg))


def cmd_serve(args) -> None:
    from .web import serve

    cfg, ctl = _ctx(args)
    serve(ctl, cfg.server.host, cfg.server.port)


def cmd_homewhiz_appliances(args) -> None:
    from .drivers import homewhiz as hw

    cfg = load_config(args.config)
    creds = hw.login(cfg.homewhiz.username, cfg.homewhiz.password)
    apps = hw.list_appliances(creds)
    if not apps:
        print("No appliances are linked to this HomeWhiz account.")
        return
    for a in apps:
        tag = "  <- Wi-Fi air conditioner" if hw.is_cloud_ac(a) else ""
        print(
            f"{a.get('applianceId')}  {a.get('name')!r}  model={a.get('model')}  "
            f"type={a.get('applianceType')}  connectivity={a.get('connectivity')}  "
            f"home={a.get('homeName')!r}{tag}"
        )


def cmd_status(args) -> None:
    cfg, ctl = _ctx(args)
    d = ctl.driver
    if d.live_state:
        print(f"Connecting via {d.kind}…")
        deadline = time.time() + args.wait
        while time.time() < deadline and d.read_state() is None:
            d.poll()
            time.sleep(1)
        print(d.describe())
        print(json.dumps(d.read_state(), indent=2))
        d.stop()
    else:
        print(json.dumps(ctl.state(), indent=2, default=str))


def cmd_discover(args) -> None:
    try:
        import broadlink
    except ImportError:
        raise ValueError("Install the IR extra first: pip install 'acmonitor[ir]'") from None

    devs = broadlink.discover(timeout=args.timeout)
    if not devs:
        print("No Broadlink devices found. Is the blaster on the same network/VLAN?")
    for dev in devs:
        print(f"{dev.host[0]}  {dev.model}  type={dev.type}  mac={dev.mac.hex(':')}")


def cmd_preset(args) -> None:
    cfg, ctl = _ctx(args)
    p = ctl.store.save_preset(
        args.name, args.label or "", None, args.off, args.mode, args.temp, args.fan
    )
    print(f"Saved preset {p.name}: {p.describe()}")


def cmd_learn(args) -> None:
    cfg, ctl = _ctx(args)
    if ctl.store.get_preset(args.name) is None:
        ctl.store.save_preset(args.name, args.label or args.name, None, args.off, args.mode, args.temp, args.fan)
    print(f"Point your remote at the blaster and press the button for '{args.name}' "
          f"(waiting {cfg.device.learn_timeout_s}s)…")
    p = ctl.learn(args.name)
    print(f"Learned {p.name}: {p.describe()}")


def cmd_apply(args) -> None:
    cfg, ctl = _ctx(args)
    if ctl.driver.live_state:
        ctl.driver.poll()
        deadline = time.time() + 20
        while time.time() < deadline and ctl.driver.read_state() is None:
            time.sleep(1)
    ctl.apply(args.name, "manual", args.off_after)
    print(f"Sent {args.name}")
    ctl.driver.stop()


def cmd_export(args) -> None:
    _, ctl = _ctx(args)
    json.dump(ctl.store.export_presets(), sys.stdout, indent=2)
    print()


def cmd_import(args) -> None:
    _, ctl = _ctx(args)
    with open(args.file) as fh:
        print(f"Imported {ctl.store.import_presets(json.load(fh))} presets")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="acmonitor", description=__doc__)
    ap.add_argument("-c", "--config", default="config.toml", help="config file (default: config.toml)")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("serve", help="run the scheduler and web dashboard").set_defaults(fn=cmd_serve)
    sub.add_parser(
        "homewhiz-appliances", help="list appliances linked to your HomeWhiz account"
    ).set_defaults(fn=cmd_homewhiz_appliances)
    p = sub.add_parser("status", help="print the AC's current state")
    p.add_argument("--wait", type=int, default=30, help="seconds to wait for live state")
    p.set_defaults(fn=cmd_status)
    p = sub.add_parser("discover", help="find Broadlink IR blasters on the LAN")
    p.add_argument("--timeout", type=int, default=5)
    p.set_defaults(fn=cmd_discover)

    def preset_args(p):
        p.add_argument("name", help="short id, e.g. cool24")
        p.add_argument("--label")
        p.add_argument("--off", action="store_true", help="this preset turns the AC off")
        p.add_argument("--mode", choices=["cool", "heat", "dry", "fan", "auto"])
        p.add_argument("--temp", type=float)
        p.add_argument("--fan")

    p = sub.add_parser("preset", help="create/update a preset from settings")
    preset_args(p)
    p.set_defaults(fn=cmd_preset)
    p = sub.add_parser("learn", help="learn the IR code for a preset (IR drivers)")
    preset_args(p)
    p.set_defaults(fn=cmd_learn)
    p = sub.add_parser("apply", help="apply a preset now")
    p.add_argument("name")
    p.add_argument("--off-after", type=float, metavar="MIN", help="turn off again after MIN minutes")
    p.set_defaults(fn=cmd_apply)
    sub.add_parser("export-presets", help="print presets (incl. IR codes) as JSON").set_defaults(fn=cmd_export)
    p = sub.add_parser("import-presets", help="load presets from a JSON export")
    p.add_argument("file")
    p.set_defaults(fn=cmd_import)

    args = ap.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    from .drivers import DriverError

    try:
        args.fn(args)
    except (DriverError, FileNotFoundError, ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
