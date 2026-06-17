"""The ``engine`` command-line interface.

Commands are added as their modules are built. Right now: ``ingest-context``.
"""

from __future__ import annotations

import typer

from .config import load_settings
from .context.ingest import ingest, read_context_files

app = typer.Typer(
    help="MyOutboundEngine - personalized outbound sequence generation.",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def _main() -> None:
    """Keep the app in multi-command mode so subcommand names are stable."""


@app.command(name="ingest-context")
def ingest_context(
    config: str = typer.Option("config.toml", help="Path to config.toml."),
) -> None:
    """Read your uploaded context files and distill them into the offer brief."""
    settings = load_settings(config)
    settings.paths.ensure()

    docs = read_context_files(settings.paths.context_dir)
    if not docs:
        typer.secho(
            f"No context files found in {settings.paths.context_dir}.\n"
            "Drop your portfolio, case studies, pricing, and process docs there "
            "(.pdf, .docx, .md, .txt) and run this again.",
            fg=typer.colors.YELLOW,
        )
        raise typer.Exit(code=1)

    typer.echo(f"Reading {len(docs)} file(s): {', '.join(d.filename for d in docs)}")
    try:
        offer = ingest(settings, docs=docs)
    except RuntimeError as exc:  # missing API key
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(code=1)
    except Exception as exc:  # parsing / model errors — surface cleanly, don't traceback
        typer.secho(f"Distillation failed: {exc}", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    typer.secho("Offer brief distilled:", fg=typer.colors.GREEN)
    typer.echo(f"  name:         {offer.name or '(unspecified)'}")
    typer.echo(f"  summary:      {offer.summary}")
    typer.echo(f"  value props:  {len(offer.value_props)} | proof points: {len(offer.proof_points)}")
    typer.echo(f"  saved to:     {settings.paths.data_dir / 'offer_brief.json'}")


if __name__ == "__main__":
    app()
