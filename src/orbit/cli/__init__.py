"""Command-line interface for orbit."""

import typer

from orbit.cli import certs, chat, config, maintenance, serve, status

app = typer.Typer(help="Orbit command-line tool.", no_args_is_help=True)
db = typer.Typer(help="Look after orbit's database of saved sessions.", no_args_is_help=True)
app.add_typer(db, name="db")
token = typer.Typer(help="Look after the token frontends show orbit serve.", no_args_is_help=True)
app.add_typer(token, name="token")
certs_app = typer.Typer(
    help="Make the certificates orbit serve uses for TLS and mutual TLS.", no_args_is_help=True
)
app.add_typer(certs_app, name="certs")
config_app = typer.Typer(
    help="Show the configuration orbit resolves for a workspace.", no_args_is_help=True
)
app.add_typer(config_app, name="config")
app.command()(chat.chat)
app.command()(serve.serve)
app.command()(status.status)
app.command()(maintenance.version)
config_app.command()(config.show)
db.command()(maintenance.backup)
db.command()(maintenance.forget)
token.command()(maintenance.rotate)
certs_app.command()(certs.init)
certs_app.command()(certs.server)
certs_app.command()(certs.client)
certs_app.command("list")(certs.list_certificates)
