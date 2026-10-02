"""Run Django's development server with a temporary self-signed TLS certificate."""

import shutil
import ssl
import subprocess
import tempfile
from pathlib import Path

from django.core.management.base import CommandError
from django.core.management.commands.runserver import Command as RunserverCommand
from django.core.servers.basehttp import WSGIServer, WSGIRequestHandler


class TLSRequestHandler(WSGIRequestHandler):
    def get_environ(self):
        environ = super().get_environ()
        environ["wsgi.url_scheme"] = "https"
        return environ


class TLSServer(WSGIServer):
    """Wrap Django's bound development-server socket in TLS."""

    RequestHandlerClass = TLSRequestHandler

    def __init__(self, *args, certfile, keyfile, **kwargs):
        super().__init__(*args, **kwargs)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certfile=certfile, keyfile=keyfile)
        self.socket = context.wrap_socket(self.socket, server_side=True)


class Command(RunserverCommand):
    help = "Start the development server with temporary self-signed HTTPS."

    def inner_run(self, *args, **options):
        openssl = shutil.which("openssl")
        if not openssl:
            raise CommandError(
                "The runhttps command requires OpenSSL. Install it or use runserver over HTTP."
            )

        with tempfile.TemporaryDirectory(prefix="goldi-dev-tls-") as temp_dir:
            certfile = Path(temp_dir) / "cert.pem"
            keyfile = Path(temp_dir) / "key.pem"
            try:
                subprocess.run(
                    [
                        openssl,
                        "req",
                        "-x509",
                        "-newkey",
                        "rsa:2048",
                        "-keyout",
                        str(keyfile),
                        "-out",
                        str(certfile),
                        "-days",
                        "1",
                        "-nodes",
                        "-subj",
                        "/CN=localhost",
                        "-addext",
                        "subjectAltName=DNS:localhost,IP:127.0.0.1,IP:::1",
                    ],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except subprocess.CalledProcessError as exc:
                raise CommandError("OpenSSL could not create a development TLS certificate.") from exc

            class DevelopmentTLSServer(TLSServer):
                def __init__(self, *server_args, **server_kwargs):
                    super().__init__(
                        *server_args,
                        certfile=certfile,
                        keyfile=keyfile,
                        **server_kwargs,
                    )

            self.server_cls = DevelopmentTLSServer
            self.stdout.write(
                self.style.WARNING(
                    "Using a temporary self-signed certificate; your browser will show a trust warning."
                )
            )
            super().inner_run(*args, **options)
