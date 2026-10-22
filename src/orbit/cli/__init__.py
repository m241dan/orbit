"""Command-line interface for orbit."""

import typer

from orbit.cli import chat, maintenance, serve

app = typer.Typer(help="Orbit command-line tool.", no_args_is_help=True)
db = typer.Typer(help="Look after orbit's database of saved sessions.", no_args_is_help=True)
app.add_typer(db, name="db")
token = typer.Typer(help="Look after the token frontends show orbit serve.", no_args_is_help=True)
app.add_typer(token, name="token")
app.command()(chat.chat)
app.command()(serve.serve)
app.command()(maintenance.version)
db.command()(maintenance.backup)
db.command()(maintenance.forget)
token.command()(maintenance.rotate)
