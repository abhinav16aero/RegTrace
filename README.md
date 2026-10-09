> [!IMPORTANT]
> ### 🎥 Website Demo Video
>
> Watch the complete walkthrough of the website, including its key features, functionality, and user experience.
>
> **▶️ [Click Here to Watch the Demo](https://drive.google.com/file/d/10aAaaDfFVD1IU1_TXLAQO0uNspPeV9lQ/view?usp=drive_link)**
>
> This demo showcases the website in action and highlights its core capabilities.

# RegTrace

**A regulatory assessment workbench for the ET AI Hackathon.**

RegTrace connects regulatory requirements, bank policies and evidence. It extracts obligations with source citations, checks policy coverage, tests supported CSV evidence, and tracks findings through remediation and reviewer decisions.

## Features

- Document extraction with exact source quotations and readable references.
- Separate applicability, coverage, evidence, test and review outcomes.
- Assigned remediation tasks, corrected-evidence retests and reviewer closure.
- Regulatory version comparison with affected findings and reassessment.
- Cross-regulation analysis, contradiction checks and what-if scenarios.
- Role-based access, tenant isolation and an auditable decision history.

## Run locally

Requires **Python 3.12**, **npm**, **make** and a running **Docker** daemon. Setup installs a project-local Node 22 runtime.

```bash
make demo
```

Open **http://127.0.0.1:8000**. Use that exact address; request-origin checks are enabled. Generated login credentials are stored locally in `.local/demo-users.json`.

## Prepare the demo

Configure a Groq key, then create the fictional Meridian Demo Bank workspace:

```bash
export GROQ_API_KEY="your-groq-api-key"
make stop
.venv/bin/python -I scripts/prepare_presentation.py
.venv/bin/python -I scripts/dev.py start
.venv/bin/python -I scripts/seed_presentation.py
```

Alternatively, store the key in the ignored `.local/secrets.json` file as `{"groq_api":"your-groq-api-key"}`. Seeding calls Groq and resumes saved progress; results depend on the live model.

| Account | Role |
|---|---|
| `maya.analyst` | Explore documents and findings |
| `arjun.operations` | Own corrections and retest evidence |
| `kavya.reviewer` | Review findings and close tasks |
| `boundary.reviewer` | Demonstrate a separate tenant |

Passwords are generated in `.local/demo-users.json`. Sign in and open **Demo guide** to walk through the workflows. The inputs include 40 fictional onboarding records, correction evidence, simulated regulations and a separately attributed RBI excerpt. Prepared results remain distinct from fresh live runs.

## Development

```bash
make build          # Build the frontend
make test           # Unit and PostgreSQL integration tests
make browser-check  # Check the running, seeded demo in Chromium
make stop           # Stop services; retain local data
```

Restart with `.venv/bin/python -I scripts/dev.py start`.

**Stack:** React, TypeScript, FastAPI, PostgreSQL and Groq.

`src/regtrace/` contains the API and workflow services; `web/` the frontend; `db/` the migrations; `scripts/` the local tooling; and `tests/` the automated checks. `config/` and `data/` contain versioned configuration, source documents and reproducible demo/test inputs.

RegTrace is a hackathon prototype. Sample tests do not establish bank-wide compliance, and model interpretations require human review. Local credentials, generated results, build outputs and development notes are excluded from Git.
