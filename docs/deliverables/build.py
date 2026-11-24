#!/usr/bin/env python3
"""Build the PDF deliverables from the Markdown sources in this directory.

Pipeline:  Markdown ──python-markdown──▶ HTML (+ style.css) ──headless Chrome──▶ PDF ──pypdf/reportlab──▶ footer + page numbers

Also renders the draw.io diagram pages to PNG (docs/img/) so the documents can embed them.

Usage:
    python docs/deliverables/build.py                 # all documents, generic placeholders -> pdf/ (committed)
    python docs/deliverables/build.py --live          # real values from the AWS account -> pdf-live/ (git-ignored)
    python docs/deliverables/build.py --only 02       # one document (prefix match)
    python docs/deliverables/build.py --no-diagrams   # skip the draw.io export

Requires: python packages markdown, pygments, pypdf, reportlab; Google Chrome; draw.io desktop (optional).
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import io
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import markdown
from pygments.formatters import HtmlFormatter
from pypdf import PdfReader, PdfWriter
from reportlab.lib.colors import HexColor
from reportlab.pdfgen import canvas

HERE = Path(__file__).resolve().parent
DOCS = HERE.parent
ROOT = DOCS.parent
BUILD = HERE / "build"
IMG = DOCS / "img"
DRAWIO_FILE = DOCS / "architecture.drawio"

PROJECT = "jwt-proxy · Castlabs Solutions Engineer coding challenge"

CHROME_CANDIDATES = [
    os.environ.get("CHROME_BIN", ""),
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    shutil.which("google-chrome") or "",
    shutil.which("chromium") or "",
    shutil.which("chromium-browser") or "",
]
DRAWIO_CANDIDATES = [
    os.environ.get("DRAWIO_BIN", ""),
    "/Applications/draw.io.app/Contents/MacOS/draw.io",
    shutil.which("drawio") or "",
]

# draw.io pages (1-based) -> PNG file
DIAGRAMS = {
    1: IMG / "application-architecture.png",
    2: IMG / "aws-deployment.png",
}


@dataclass
class Doc:
    number: str
    slug: str
    title: str
    subtitle: str
    source: Path
    footer: str
    mermaid: bool = False

    @property
    def pdf_name(self) -> str:
        return f"{self.number}-{self.slug}.pdf"


DOCUMENTS = [
    Doc("01", "architecture-and-design", "Architecture and design",
        "How the proxy is built, why it is built that way, how it is packaged and deployed",
        HERE / "01-architecture.md", "Architecture and design"),
    Doc("02", "usage-guide", "Usage guide",
        "Running, configuring, testing and operating the proxy locally with Docker Compose",
        HERE / "02-usage-guide.md", "Usage guide"),
    Doc("03", "aws-deployment-access-and-review", "AWS deployment: access and review guide",
        "Reaching the EC2 deployment through SSM and verifying the solution end to end",
        HERE / "03-ec2-access-and-review.md", "AWS deployment: access and review"),
    Doc("04", "drmtoday-solution-design", "Solution design: DRMtoday migration proposal",
        "Customer issue analysis, target architecture, advantages, migration plan and timeline",
        DOCS / "drmtoday-solution-design.md", "DRMtoday solution design", mermaid=True),
]


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def first_existing(paths: list[str]) -> str | None:
    for p in paths:
        if p and Path(p).exists():
            return p
    return None


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw)


def git(*args: str) -> str:
    try:
        return run(["git", "-C", str(ROOT), *args]).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""


def aws(*args: str) -> str:
    """Run an AWS CLI command and return stdout, or '' on any failure."""
    try:
        return run(["aws", *args, "--output", "json"], env={**os.environ, "AWS_PAGER": ""}).stdout
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        print(f"  warning: aws {' '.join(args)} failed: {detail.strip()[:200]}", file=sys.stderr)
        return ""


# --------------------------------------------------------------------------- #
# deployment values (placeholders in the Markdown: {{NAME}})
# --------------------------------------------------------------------------- #
GENERIC_VALUES = {
    "ACCOUNT_ID": "<account-id>",
    "REGION": "<region>",
    "PROFILE": "<aws-profile>",
    "STACK_NAME": "jwt-proxy",
    "INSTANCE_ID": "<instance-id>",
    "INSTANCE_TYPE": "t4g.micro",
    "PRIVATE_IP": "<private-ip>",
    "SUBNET_ID": "<private-subnet-id>",
    "AVAILABILITY_ZONE": "<availability-zone>",
    "NETWORK_MODE": "private-nat",
    "NAT_GATEWAY_ID": "<nat-gateway-id>",
    "SOURCE": "<s3://bucket/key or git url>",
    "SOURCE_COMMIT": "<commit>",
    "LAUNCHED_AT": "<launch time>",
    "STACK_STATUS": "<stack status>",
    "STACK_ID": "<stack-id>",
}


def live_values(stack_name: str) -> dict[str, str]:
    """Fill the placeholders from the AWS account the CLI is currently pointed at."""
    values = dict(GENERIC_VALUES)
    values["STACK_NAME"] = stack_name
    values["PROFILE"] = os.environ.get("AWS_PROFILE", "default")
    region = ""
    try:
        region = run(["aws", "configure", "get", "region"]).stdout.strip()
    except subprocess.CalledProcessError:
        pass
    values["REGION"] = region or os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))

    ident = aws("sts", "get-caller-identity")
    if ident:
        values["ACCOUNT_ID"] = json.loads(ident)["Account"]

    stacks = aws("cloudformation", "describe-stacks", "--stack-name", stack_name)
    if not stacks:
        print("  warning: stack not found; keeping generic placeholders", file=sys.stderr)
        return values
    stack = json.loads(stacks)["Stacks"][0]
    outputs = {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}
    params = {p["ParameterKey"]: p.get("ParameterValue", "") for p in stack.get("Parameters", [])}
    values["STACK_STATUS"] = stack["StackStatus"]
    values["STACK_ID"] = stack["StackId"]
    values["INSTANCE_ID"] = outputs.get("InstanceId", values["INSTANCE_ID"])
    values["NETWORK_MODE"] = outputs.get("NetworkMode", params.get("NetworkMode", values["NETWORK_MODE"]))
    values["INSTANCE_TYPE"] = params.get("InstanceType", values["INSTANCE_TYPE"])
    if params.get("RepoUrl"):
        values["SOURCE"] = f"{params['RepoUrl']} @ {params.get('GitRef', 'main')}"
        values["SOURCE_COMMIT"] = params.get("GitRef", "")
    elif params.get("SourceBucket"):
        values["SOURCE"] = f"s3://{params['SourceBucket']}/{params.get('SourceKey', '')}"
        values["SOURCE_COMMIT"] = Path(params.get("SourceKey", "")).stem

    if values["INSTANCE_ID"].startswith("i-"):
        inst = aws("ec2", "describe-instances", "--instance-ids", values["INSTANCE_ID"])
        if inst:
            i = json.loads(inst)["Reservations"][0]["Instances"][0]
            values["PRIVATE_IP"] = i.get("PrivateIpAddress", values["PRIVATE_IP"])
            values["SUBNET_ID"] = i.get("SubnetId", values["SUBNET_ID"])
            values["AVAILABILITY_ZONE"] = i.get("Placement", {}).get("AvailabilityZone", values["AVAILABILITY_ZONE"])
            values["LAUNCHED_AT"] = i.get("LaunchTime", values["LAUNCHED_AT"])
            values["INSTANCE_TYPE"] = i.get("InstanceType", values["INSTANCE_TYPE"])
    res = aws("cloudformation", "describe-stack-resources", "--stack-name", stack_name)
    if res:
        for r in json.loads(res)["StackResources"]:
            if r["ResourceType"] == "AWS::EC2::NatGateway":
                values["NAT_GATEWAY_ID"] = r["PhysicalResourceId"]
    return values


def fill(text: str, values: dict[str, str]) -> str:
    missing = set(re.findall(r"{{\s*([A-Z_]+)\s*}}", text)) - set(values)
    if missing:
        raise SystemExit(f"unknown placeholders: {sorted(missing)}")
    return re.sub(r"{{\s*([A-Z_]+)\s*}}", lambda m: values[m.group(1)], text)


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #
MERMAID_RE = re.compile(r"```mermaid\s*\n(.*?)```", re.S)


def preprocess_markdown(text: str) -> tuple[str, bool]:
    """Turn ```mermaid fences into <pre class="mermaid"> blocks; return (text, used_mermaid)."""
    used = bool(MERMAID_RE.search(text))
    text = MERMAID_RE.sub(lambda m: f'\n<pre class="mermaid">\n{html.escape(m.group(1))}</pre>\n', text)
    return text, used


def render_html(doc: Doc, body_md: str, meta: dict[str, str]) -> str:
    body_md, used_mermaid = preprocess_markdown(body_md)
    # Drop the source's own H1: the title block replaces it.
    body_md = re.sub(r"\A(\s*#\s[^\n]*\n)", "", body_md)
    md = markdown.Markdown(
        extensions=["tables", "fenced_code", "codehilite", "toc", "attr_list", "md_in_html", "sane_lists", "def_list"],
        extension_configs={
            "codehilite": {"guess_lang": False, "noclasses": False},
            "toc": {"toc_depth": "1-2", "title": ""},
        },
    )
    body_html = md.convert(body_md)
    toc_html = md.toc if "<li>" in md.toc else ""
    css = (HERE / "style.css").read_text() + "\n" + HtmlFormatter(style="friendly").get_style_defs(".codehilite")
    meta_rows = "\n".join(f"<tr><td>{html.escape(k)}</td><td>{v}</td></tr>" for k, v in meta.items())
    mermaid = (
        '<script type="module">'
        "import mermaid from 'https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs';"
        "mermaid.initialize({startOnLoad: false, theme: 'neutral', securityLevel: 'loose'});"
        "await mermaid.run(); document.documentElement.dataset.mermaidDone = '1';"
        "</script>"
        if used_mermaid else ""
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{html.escape(doc.title)}</title>
<base href="file://{HERE}/">
<style>{css}</style>
</head>
<body>
<div class="title-block">
  <p class="project">{html.escape(PROJECT)}</p>
  <h1>{html.escape(doc.title)}</h1>
  <p class="subtitle">{html.escape(doc.subtitle)}</p>
  <table class="meta">{meta_rows}</table>
</div>
{f'<div class="toc"><div class="toc-title">Contents</div>{toc_html}</div>' if toc_html else ''}
{body_html}
{mermaid}
</body>
</html>
"""


def chrome_pdf(chrome: str, html_path: Path, pdf_path: Path, wait_ms: int) -> None:
    cmd = [
        chrome, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
        "--hide-scrollbars", "--no-pdf-header-footer", f"--virtual-time-budget={wait_ms}",
        f"--print-to-pdf={pdf_path}", html_path.resolve().as_uri(),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not pdf_path.exists():
        raise SystemExit(f"Chrome failed for {html_path.name}:\n{proc.stderr[-2000:]}")


def add_footer(pdf_path: Path, label: str) -> int:
    """Stamp 'label · page i of n' at the bottom of every page. Returns the page count."""
    writer = PdfWriter(clone_from=str(pdf_path))
    total = len(writer.pages)
    for index, page in enumerate(writer.pages, start=1):
        width = float(page.mediabox.width)
        buf = io.BytesIO()
        c = canvas.Canvas(buf, pagesize=(width, float(page.mediabox.height)))
        c.setFont("Helvetica", 7.5)
        c.setFillColor(HexColor("#6b7280"))
        c.drawString(15 / 25.4 * 72, 10 / 25.4 * 72, label)
        c.drawRightString(width - 15 / 25.4 * 72, 10 / 25.4 * 72, f"Page {index} of {total}")
        c.save()
        buf.seek(0)
        page.merge_page(PdfReader(buf).pages[0])
    writer.add_metadata({"/Title": label, "/Producer": "jwt-proxy docs build (Chrome + pypdf)"})
    with open(pdf_path, "wb") as fh:
        writer.write(fh)
    return total


def export_diagrams(drawio: str | None) -> None:
    if not drawio:
        print("draw.io not found: keeping the existing PNGs in docs/img/", file=sys.stderr)
        return
    IMG.mkdir(exist_ok=True)
    for page, png in DIAGRAMS.items():
        subprocess.run(
            [drawio, "--export", "--format", "png", "--page-index", str(page), "--scale", "2", "--border", "16",
             "--output", str(png), str(DRAWIO_FILE)],
            check=True, capture_output=True, text=True,
        )
        print(f"  diagram page {page} -> {png.relative_to(ROOT)}")


# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--live", action="store_true", help="fill deployment values from the current AWS account")
    ap.add_argument("--stack-name", default=os.environ.get("STACK_NAME", "jwt-proxy"))
    ap.add_argument("--only", help="build only the documents whose number or slug starts with this")
    ap.add_argument("--no-diagrams", action="store_true", help="do not re-export the draw.io pages")
    ap.add_argument("--author", default=git("log", "-1", "--format=%an") or "velartur")
    ap.add_argument("--wait-ms", type=int, default=8000, help="virtual time budget for Chrome (JS such as Mermaid)")
    ap.add_argument("--out", type=Path, default=None,
                    help="output directory (default: pdf/, or pdf-live/ with --live so account details stay out of git)")
    args = ap.parse_args()
    out_dir = args.out or (HERE / ("pdf-live" if args.live else "pdf"))

    chrome = first_existing(CHROME_CANDIDATES)
    if not chrome:
        raise SystemExit("Google Chrome (or Chromium/Edge) is required; set CHROME_BIN if it is somewhere unusual")

    if not args.no_diagrams:
        print("Rendering diagrams")
        export_diagrams(first_existing(DRAWIO_CANDIDATES))

    values = live_values(args.stack_name) if args.live else dict(GENERIC_VALUES)
    if args.live:
        print(f"Deployment values: account {values['ACCOUNT_ID']}, stack {values['STACK_NAME']} "
              f"({values['STACK_STATUS']}), instance {values['INSTANCE_ID']}")

    commit = git("rev-parse", "--short", "HEAD") or "n/a"
    dirty = " (uncommitted changes)" if git("status", "--porcelain") else ""
    repo = re.sub(r"\.git$", "", git("remote", "get-url", "origin")) or "n/a"
    today = dt.date.today().isoformat()

    out_dir.mkdir(exist_ok=True)
    BUILD.mkdir(exist_ok=True)
    for doc in DOCUMENTS:
        if args.only and not (doc.number.startswith(args.only) or doc.slug.startswith(args.only)):
            continue
        print(f"Building {doc.pdf_name}")
        source = fill(doc.source.read_text(), values)
        meta = {
            "Document": f"{doc.number} · {doc.title}",
            "Author": html.escape(args.author),
            "Date": today,
            "Repository": f'<a href="{html.escape(repo)}">{html.escape(repo)}</a>',
            "Revision": f"<code>{html.escape(commit)}</code>{dirty}",
        }
        html_path = BUILD / f"{doc.number}-{doc.slug}.html"
        html_path.write_text(render_html(doc, source, meta))
        pdf_path = out_dir / doc.pdf_name
        chrome_pdf(chrome, html_path, pdf_path, args.wait_ms if doc.mermaid else 1500)
        pages = add_footer(pdf_path, f"{PROJECT} · {doc.footer} · {today}")
        print(f"  -> {pdf_path.relative_to(ROOT)} ({pages} pages, {pdf_path.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
