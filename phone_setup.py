"""One-shot setup for an Android phone (Termux) or any Linux box.

    python phone_setup.py

Installs the package, asks for your HomeWhiz login, writes config.toml,
checks the AC is reachable, and starts the dashboard. Safe to run again.
"""

from __future__ import annotations

import getpass
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "config.toml"


def say(msg: str = "") -> None:
    print(msg, flush=True)


def step(n: int, msg: str) -> None:
    say(f"\n=== {n}. {msg} ===")


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    answer = input(f"{prompt}{suffix}: ").strip()
    return answer or default


def yes(prompt: str, default: bool = True) -> bool:
    answer = input(f"{prompt} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
    return default if not answer else answer.startswith("y")


def detect_timezone() -> str:
    try:  # Android
        tz = subprocess.run(["getprop", "persist.sys.timezone"], capture_output=True, text=True).stdout.strip()
        if tz:
            return tz
    except OSError:
        pass
    try:  # Linux
        link = os.readlink("/etc/localtime")
        if "zoneinfo/" in link:
            return link.split("zoneinfo/", 1)[1]
    except OSError:
        pass
    return ""


def toml_str(s: str) -> str:
    return json.dumps(s)  # a JSON string is a valid TOML basic string


def acmonitor(*args: str) -> int:
    exe = shutil.which("acmonitor") or "acmonitor"
    return subprocess.run([exe, "-c", str(CONFIG), *args]).returncode


def write_config(email: str, password: str, tz: str) -> None:
    CONFIG.write_text(
        "# Written by phone_setup.py. See config.example.toml for all options.\n"
        "[device]\n"
        'backend = "homewhiz"\n\n'
        "[homewhiz]\n"
        f"username = {toml_str(email)}\n"
        f"password = {toml_str(password)}\n\n"
        "[server]\n"
        'host = "0.0.0.0"\n'
        "port = 8080\n\n"
        "[monitor]\n"
        'db_path = "acmonitor.db"\n'
        f"timezone = {toml_str(tz)}\n"
        "rated_power_kw = 1.0\n"
    )
    CONFIG.chmod(0o600)


def main() -> None:
    os.chdir(HERE)
    say("AC Monitor setup")

    step(1, "Installing (takes a minute the first time)")
    rc = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-e", ".", "tzdata"]).returncode
    if rc != 0:
        say("Install failed. Check the phone is online and run this again.")
        sys.exit(1)
    say("Installed.")

    step(2, "Your HomeWhiz login")
    if CONFIG.exists() and yes("A config.toml already exists. Keep it?"):
        say("Keeping the existing config.")
    else:
        email = ""
        while "@" not in email:
            email = ask("HomeWhiz e-mail")
        password = ""
        while not password:
            password = getpass.getpass("HomeWhiz password (hidden while typing): ")
        tz = ask("Time zone", detect_timezone() or "Europe/Istanbul")
        write_config(email, password, tz)
        say("Saved config.toml (readable only by Termux).")

    step(3, "Looking for your AC in the HomeWhiz account")
    if acmonitor("homewhiz-appliances") != 0:
        say("\nCould not log in or list appliances. Check the e-mail/password and run this again.")
        say("(Send the error above to Claude if it is not a password problem.)")
        sys.exit(1)

    step(4, "Reading the AC's live state (up to 45 s)")
    acmonitor("status", "--wait", "45")
    say("\nCompare the values above with the AC's display (power, mode, set temperature).")

    step(5, "Dashboard")
    say(f"Next time, start it with:  cd {HERE} && acmonitor serve")
    if not yes("Start the dashboard now?"):
        return
    if shutil.which("termux-wake-lock"):
        subprocess.run(["termux-wake-lock"])
        say("Wake lock on, so Android won't put the server to sleep.")
    say("Open http://localhost:8080 in your phone's browser. Press Ctrl+C here to stop.\n")
    exe = shutil.which("acmonitor") or "acmonitor"
    os.execvp(exe, [exe, "-c", str(CONFIG), "serve"])


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        say("\nStopped.")
