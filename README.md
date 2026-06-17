# MyOutboundEngine

An outbound email engine that ingests your offer and a list of prospects, then drafts
personalized, value-led multi-step sequences designed to book conversations. Built to plug
into Instantly or Lemlist for the actual sending.

**Status:** Phase 0 scaffold. The pipeline modules land in subsequent steps.

## What it does

The engine learns your product from files you drop in a folder — not from questions in chat —
and writes a tailored sequence per prospect:

1. Ingest product context from uploads (portfolio, case studies, pricing, finishes, process).
2. Load leads from a CSV (Apollo auto-pull comes later).
3. Score each lead against your ICP and assign a tier.
4. Draft a personalized multi-step sequence per prospect, with A/B variant slots for subjects and CTAs.
5. Export the drafts to a human-review preview so you can judge copy quality before anything sends.

Later phases push approved sequences to Instantly/Lemlist, classify replies, and refine
variants with a bandit to lift the positive-reply rate over time.

## Requirements

- Python 3.12+
- An Anthropic API key

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env   # then add your ANTHROPIC_API_KEY
```

CLI commands (`engine ingest-context`, `engine load-leads`, `engine draft`, `engine export`)
are introduced as their modules are built.

## Project layout

```
MyOutboundEngine/
├── pyproject.toml
├── README.md
├── .env.example
└── src/
    └── outbound/        # engine package
```

## Roadmap

- **Phase 0** — Context ingest, lead ingest, ICP scoring, personalization agent, review export. No sending.
- **Phase 1** — Instantly/Lemlist API integration and LLM reply classification.
- **Phase 2** — Thompson-sampling bandit over variants; budget-gated landing pages and mini-reports.
- **Phase 3** — Apollo auto-pull and a metrics dashboard.
