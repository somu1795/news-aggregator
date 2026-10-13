#!/usr/bin/env python3
"""
Reports pinned versions that have newer releases: the images and tools pinned
in docker-compose.yml and the Dockerfiles, the browser identity in
app/config.py, and the Python packages in the app and tagger images. Reads
the pins from those files, so there's no list here to keep in sync. Changes
nothing: it writes a report for a person to act on (see MAINTENANCE.md).

    scripts/check-updates.py            # Report to stdout and logs/updates.txt
    scripts/check-updates.py --no-pip   # Skip the Python packages (they take a minute)

Run weekly from cron. Releases newer than 14 days are shown but marked "new":
wait for them to settle before upgrading.
"""

import datetime
import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
REPORT = REPO / "logs" / "updates.txt"
SETTLE_DAYS = 14


def read(path):
    return (REPO / path).read_text()


def pinned(path, pattern):
    match = re.search(pattern, read(path))
    return match.group(1) if match else None


def fetch_json(url):
    request = urllib.request.Request(url, headers={"User-Agent": "news-aggregator-update-check", "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def github_latest(repo):
    """(version, date) of a GitHub repository's latest release."""
    release = fetch_json(f"https://api.github.com/repos/{repo}/releases/latest")
    return release["tag_name"], release["published_at"][:10]


def github_latest_tag(repo):
    tags = fetch_json(f"https://api.github.com/repos/{repo}/tags?per_page=1")
    return tags[0]["name"], None


def endoflife_latest(product, cycle):
    """(latest patch version, its date) of a release cycle, from endoflife.date."""
    for entry in fetch_json(f"https://endoflife.date/api/{product}.json"):
        if str(entry["cycle"]) == cycle:
            return entry["latest"], entry.get("latestReleaseDate")
    return None, None


def endoflife_newest(product):
    entry = fetch_json(f"https://endoflife.date/api/{product}.json")[0]
    return entry["latest"], entry.get("latestReleaseDate")


def chrome_stable():
    versions = fetch_json("https://versionhistory.googleapis.com/v1/chrome/platforms/win/channels/stable/versions?pageSize=1")
    return versions["versions"][0]["version"].split(".")[0], None


def bare(version):
    return (version or "").lstrip("v")


CHECKS = [
    # name, pinned version, where it's pinned, how to find the latest
    ("cloudflared", lambda: pinned("docker-compose.yml", r"cloudflare/cloudflared:([\w.]+)"), "docker-compose.yml",
     lambda: github_latest("cloudflare/cloudflared")),
    ("FlareSolverr", lambda: pinned("docker-compose.yml", r"flaresolverr/flaresolverr:(v?[\w.]+)"), "docker-compose.yml",
     lambda: github_latest("FlareSolverr/FlareSolverr")),
    ("Byparr", lambda: pinned("docker-compose.yml", r"thephaseless/byparr:([\w.]+)"), "docker-compose.yml",
     lambda: github_latest("ThePhaseless/Byparr")),
    ("Redis", lambda: pinned("docker-compose.yml", r"image: redis:([\d.]+)"), "docker-compose.yml",
     lambda: github_latest("redis/redis")),
    ("Caddy", lambda: pinned("caddy/Dockerfile", r"FROM caddy:([\d.]+)"), "caddy/Dockerfile",
     lambda: github_latest("caddyserver/caddy")),
    ("Python (app image)", lambda: pinned("app/Dockerfile", r"FROM python:([\d.]+)"), "app/Dockerfile",
     lambda: endoflife_latest("python", ".".join(pinned("app/Dockerfile", r"FROM python:([\d.]+)").split(".")[:2]))),
    ("Go (Smokescreen build)", lambda: pinned("smokescreen/Dockerfile", r"FROM golang:([\d.]+)"), "smokescreen/Dockerfile",
     lambda: endoflife_latest("go", ".".join(pinned("smokescreen/Dockerfile", r"FROM golang:([\d.]+)").split(".")[:2]))),
    ("Alpine (Smokescreen image)", lambda: pinned("smokescreen/Dockerfile", r"FROM alpine:([\d.]+)"), "smokescreen/Dockerfile",
     lambda: endoflife_newest("alpine")),
    ("Smokescreen", lambda: pinned("smokescreen/Dockerfile", r"ARG VERSION=(v?[\w.]+)"), "smokescreen/Dockerfile",
     lambda: github_latest_tag("stripe/smokescreen")),
    ("Intel NPU driver", lambda: pinned("tagger/Dockerfile", r"linux-npu-driver/releases/download/(v[\d.]+)/"), "tagger/Dockerfile",
     lambda: github_latest("intel/linux-npu-driver")),
    ("Browser identity (Chrome major version)", lambda: pinned("app/config.py", r"Chrome/(\d+)\."), "app/config.py BROWSER_USER_AGENT",
     chrome_stable),
]


def outdated_packages(image, pip):
    """[(package, pinned, latest)] in an image, via pip inside it (needs the network)."""
    result = subprocess.run(["docker", "run", "--rm", "--entrypoint", pip, image, "list", "--outdated", "--format=json",
                             "--disable-pip-version-check"], capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "pip failed")
    return [(p["name"], p["version"], p["latest_version"]) for p in json.loads(result.stdout or "[]")]


def main():
    today = datetime.date.today()
    lines = [f"Update check {today.isoformat()} (pins read from {REPO})", ""]
    behind = 0
    for name, get_pinned, where, get_latest in CHECKS:
        try:
            current = get_pinned()
            latest, date = get_latest()
        except Exception as e:
            lines.append(f"  ?  {name}: couldn't check ({type(e).__name__}: {e})")
            continue
        if bare(latest) == bare(current):
            lines.append(f"  ok {name}: {current}")
            continue
        if name.startswith("Browser identity") and int(latest) - int(current) <= 2:
            lines.append(f"  ok {name}: {current} (Chrome stable is {latest})")
            continue
        age = (today - datetime.date.fromisoformat(date)).days if date else None
        note = f", released {date}" + (" - new, let it settle" if age is not None and age < SETTLE_DAYS else "") if date else ""
        lines.append(f"  >> {name}: pinned {current}, latest {latest}{note}  [{where}]")
        behind += 1

    if "--no-pip" not in sys.argv:
        for label, image, pip in (("app", "news-aggregator-app", "pip"), ("tagger", "news-aggregator-tagger", "/venv/bin/pip")):
            try:
                packages = outdated_packages(image, pip)
            except Exception as e:
                lines.append(f"  ?  Python packages ({label}): couldn't check ({e})")
                continue
            if packages:
                behind += 1
                shown = ", ".join(f"{n} {a}->{b}" for n, a, b in packages[:12]) + (" ..." if len(packages) > 12 else "")
                lines.append(f"  >> Python packages ({label} image): {len(packages)} outdated: {shown}")
            else:
                lines.append(f"  ok Python packages ({label} image)")

    lines += ["", f"{behind} item(s) behind." if behind else "Everything is up to date."]
    report = "\n".join(lines)
    print(report)
    REPORT.parent.mkdir(exist_ok=True)
    REPORT.write_text(report + "\n")


if __name__ == "__main__":
    main()
