"""What ``orbit certs client --bundle`` puts in a client's bundle: the CA, the client's pair and
the ``[remotes.<name>]`` lines that connect with them once unpacked on the client."""

import tomllib

import pytest

from orbit.agent.domain.client_bundle import BundledFile, bundle_url, client_bundle, is_bundle_url

CA = "-----BEGIN CERTIFICATE-----\nca\n-----END CERTIFICATE-----\n"
CERTIFICATE = "-----BEGIN CERTIFICATE-----\nlaptop\n-----END CERTIFICATE-----\n"
KEY = "-----BEGIN PRIVATE KEY-----\nlaptop\n-----END PRIVATE KEY-----\n"


def _remote(files: tuple[BundledFile, ...]) -> str:
    [remote] = [file for file in files if file.name == "remote.toml"]
    return remote.text


def test_a_bundle_holds_the_ca_the_clients_pair_and_its_remote_the_key_alone_private() -> None:
    files = client_bundle("laptop", CA, CERTIFICATE, KEY, "wss://box:7433")

    assert files[:3] == (
        BundledFile("ca.pem", CA),
        BundledFile("laptop.pem", CERTIFICATE),
        BundledFile("laptop-key.pem", KEY, private=True),
    )
    assert [file.name for file in files[3:]] == ["remote.toml"]
    assert not files[3].private


def test_the_remote_connects_to_the_url_with_the_files_where_the_bundle_is_unpacked() -> None:
    files = client_bundle("laptop", CA, CERTIFICATE, KEY, "wss://box:7433")

    assert tomllib.loads(_remote(files)) == {
        "remotes": {
            "laptop": {
                "url": "wss://box:7433",
                "ca": "~/.config/orbit/certs/laptop/ca.pem",
                "cert": "~/.config/orbit/certs/laptop/laptop.pem",
                "key": "~/.config/orbit/certs/laptop/laptop-key.pem",
            }
        }
    }


def test_the_remote_says_where_its_lines_go_and_how_to_connect() -> None:
    files = client_bundle("phone", CA, CERTIFICATE, KEY, "wss://box:7433")

    assert _remote(files) == (
        "# Add these lines to ~/.config/orbit/config.toml, then connect with orbit chat"
        " --remote phone.\n"
        "[remotes.phone]\n"
        'url = "wss://box:7433"\n'
        'ca = "~/.config/orbit/certs/phone/ca.pem"\n'
        'cert = "~/.config/orbit/certs/phone/phone.pem"\n'
        'key = "~/.config/orbit/certs/phone/phone-key.pem"\n'
    )


def test_a_url_toml_has_to_escape_is_read_back_as_given() -> None:
    url = 'wss://box:7433/"quoted"\\path'
    files = client_bundle("laptop", CA, CERTIFICATE, KEY, url)

    assert tomllib.loads(_remote(files))["remotes"]["laptop"]["url"] == url


def test_the_default_url_is_wss_to_the_server_name_on_the_port() -> None:
    assert bundle_url("orbit.example.com", 8443) == "wss://orbit.example.com:8443"


def test_the_default_url_puts_an_ipv6_address_in_brackets() -> None:
    assert bundle_url("::1", 7433) == "wss://[::1]:7433"


@pytest.mark.parametrize("url", ["wss://orbit.example.com:9000", "wss://[::1]:7433", "wss://box"])
def test_a_wss_url_can_go_in_a_bundle(url: str) -> None:
    assert is_bundle_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "orbit.example.com:9000",
        "https://orbit.example.com",
        "ws://orbit.example.com:7433",
        "WSS://orbit.example.com:9000",
        " wss://orbit.example.com:9000",
        "",
    ],
)
def test_a_url_without_tls_or_a_scheme_cant_go_in_a_bundle(url: str) -> None:
    assert not is_bundle_url(url)
