# Deliverables

PDF documents and diagrams produced for the challenge hand-over. The PDFs in `pdf/` are the
deliverable; everything else here is their source.

| document | source | contents |
|----------|--------|----------|
| `pdf/01-architecture-and-design.pdf` | `01-architecture.md` | request flow, code structure, design decisions, security, packaging, AWS infrastructure, scaling, tests |
| `pdf/02-usage-guide.pdf` | `02-usage-guide.md` | prerequisites, quick start, token verification, Make targets, configuration, behaviour, tests, troubleshooting |
| `pdf/03-aws-deployment-access-and-review.pdf` | `03-ec2-access-and-review.md` | how to reach the EC2 deployment over SSM and a step-by-step review checklist |
| `pdf/04-drmtoday-solution-design.pdf` | `../drmtoday-solution-design.md` | the solution-design part of the challenge |

Diagrams: `../architecture.drawio` (two pages, editable in draw.io / diagrams.net) rendered to
`../img/application-architecture.png` and `../img/aws-deployment.png`.

## Building

```bash
make docs                 # all PDFs with generic placeholders for the AWS values -> pdf/ (committed)
DOCS_LIVE=1 make docs     # real stack/instance values from the AWS CLI -> pdf-live/ (git-ignored)
make diagrams             # only re-export the draw.io pages to PNG
DOCS_ARGS="--only 02" make docs
```

`pdf-live/` is kept out of git because it names the AWS account and resource IDs; share those
files directly with the reviewers who have access to the account.

Requirements: Google Chrome (headless, renders the PDFs), draw.io desktop (optional, exports the
diagrams; the PNGs are committed so the build works without it), and the Python packages
`markdown`, `pygments`, `pypdf`, `reportlab` (installed by `make docs`).

Pipeline: Markdown → HTML (`python-markdown`, `style.css`) → PDF (Chrome `--print-to-pdf`, A4,
landscape pages for the diagrams) → footer with document name, date and `Page i of n`
(`pypdf` + `reportlab`). Document 04 embeds Mermaid diagrams, rendered in Chrome from the CDN
at build time, so building it needs network access.

Placeholders such as `{{INSTANCE_ID}}` in the Markdown sources are replaced at build time, from
the AWS account with `--live` or with generic `<...>` values otherwise.
