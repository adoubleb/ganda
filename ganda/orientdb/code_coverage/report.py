"""Line / branch / method coverage of OrientDB from a campaign's JaCoCo execution data.

`ganda orientdb run --coverage variant ...` leaves `campaign_reports/<tag>-coverage.exec`
(everything) and `campaign_reports/<tag>-coverage/<generator>.exec` (per query generator).
This script analyzes them against EVERY class of the image's `orientdb-*.jar`s (so the
denominator is the engine, not the classes the campaign loaded) through
`jacoco/CoverageReport.java` (next to this file), launched as a single-file source program over
org.jacoco.core / org.jacoco.report / ASM from `~/.m2` -- no build, no download.

    python3 -m ganda.orientdb.code_coverage.report report campaign_reports/cov-coverage.exec --out coverage/cov
    python3 -m ganda.orientdb.code_coverage.report report a.exec b.exec --sources ~/src/orientdb/core/src/main/java
    python3 -m ganda.orientdb.code_coverage.report diff campaign_reports/cov-coverage/ir_select.exec \\
        --base campaign_reports/cov-coverage/ir_traversal.exec  # what ir_select reaches that ir_traversal never does

`report` prints a per-jar and a per-package table and, with `--out`, writes an HTML
report, `coverage.csv` and `coverage.xml`. Without `--sources` the HTML stops at the
method level (the image ships no sources; `orientdb-core-<v>-sources.jar` from Maven
Central works as a `--sources` argument as it is).
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys

from ganda.orientdb.engine.connection import DEFAULT_IMAGE
from ganda.orientdb.code_coverage.collector import AGENT_EXCLUDES, JACOCO_VERSION

HERE = os.path.dirname(os.path.abspath(__file__))
# the jars copied out of the image and the HTML reports land under the working directory
ROOT = os.getcwd()

ASM_VERSION = "9.8"  # what org.jacoco.core 0.8.13 is built against
M2 = os.path.expanduser("~/.m2/repository")
CACHE = os.path.join(ROOT, "coverage")


def classpath() -> str:
    jars = [
        f"{M2}/org/jacoco/org.jacoco.core/{JACOCO_VERSION}/org.jacoco.core-{JACOCO_VERSION}.jar",
        f"{M2}/org/jacoco/org.jacoco.report/{JACOCO_VERSION}/org.jacoco.report-{JACOCO_VERSION}.jar",
    ] + [f"{M2}/org/ow2/asm/{a}/{ASM_VERSION}/{a}-{ASM_VERSION}.jar" for a in ("asm", "asm-commons", "asm-tree")]
    missing = [j for j in jars if not os.path.isfile(j)]
    if missing:
        sys.exit("missing from ~/.m2 (mvn dependency:get -Dartifact=org.jacoco:org.jacoco.report:%s fetches them):\n  %s" % (JACOCO_VERSION, "\n  ".join(missing)))
    return os.pathsep.join(jars)


def image_lib(image: str) -> str:
    """The image's /orientdb/lib, copied out once per image id."""
    proc = subprocess.run(["docker", "image", "inspect", "-f", "{{.Id}}", image], capture_output=True, text=True)
    if proc.returncode != 0:
        # the containerd image store answers "No such image" to a tag of another
        # platform's image (amd64 on an arm64 host); the image list still names it
        listed = subprocess.run(["docker", "images", "-q", "--no-trunc", image], capture_output=True, text=True)
        if listed.returncode != 0 or not listed.stdout.strip():
            sys.exit(f"docker image inspect {image} failed: {proc.stderr.strip()}")
        proc = listed
    lib = os.path.join(CACHE, "lib-" + proc.stdout.strip().splitlines()[0].split(":")[-1][:12])
    if os.path.isdir(lib):
        return lib
    cid = subprocess.run(["docker", "create", image], capture_output=True, text=True, check=True).stdout.strip()
    try:
        tmp = lib + ".tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(CACHE, exist_ok=True)
        subprocess.run(["docker", "cp", "-q", f"{cid}:/orientdb/lib", tmp], check=True)
        os.rename(tmp, lib)
    finally:
        subprocess.run(["docker", "rm", cid], capture_output=True)
    return lib


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("command", choices=["report", "diff"])
    ap.add_argument("exec", nargs="+", help="execution data (.exec); several are merged")
    ap.add_argument("--base", nargs="+", default=[], help="diff: the execution data to subtract")
    ap.add_argument("--out", help="report: the directory for html/, coverage.csv, coverage.xml")
    ap.add_argument("--sources", nargs="+", default=[], help="report: source directories or -sources.jar files")
    ap.add_argument("--title", default="OrientDB")
    ap.add_argument("--top", type=int, default=40, help="diff: how many methods to list")
    ap.add_argument("--image", default=DEFAULT_IMAGE)
    ap.add_argument("--lib", help="a directory of orientdb-*.jar instead of the image's")
    args = ap.parse_args(argv)
    if args.command == "diff" and not args.base:
        ap.error("diff needs --base")
    lib = args.lib or image_lib(args.image)
    cmd = ["java", "-cp", classpath(), os.path.join(HERE, "jacoco", "CoverageReport.java"), args.command, "--lib", lib,
           "--exclude", *AGENT_EXCLUDES.split(":"), "--exec", *args.exec]
    if args.command == "report":
        cmd += ["--title", args.title]
        if args.out:
            cmd += ["--out", args.out]
        if args.sources:
            cmd += ["--sources", *args.sources]
    else:
        cmd += ["--base", *args.base, "--top", str(args.top)]
    return subprocess.run(cmd).returncode


if __name__ == "__main__":
    sys.exit(main())
