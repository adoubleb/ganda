"""`ganda/<engine>/.env`: parsing, precedence over the real environment, and the Docker default."""
import os

from ganda.core import envfile


def test_parse_handles_comments_quotes_export_and_empty_values():
    text = """
# a comment
ORIENT_URL=http://localhost:2480
export ORIENT_PASSWORD="s3 cret"
ARANGO_IMAGE='arangodb:3.12.11'   
HANA_PORT=443  # the default
EMPTY=
not a pair
=novalue
"""
    assert envfile.parse(text) == {
        "ORIENT_URL": "http://localhost:2480",
        "ORIENT_PASSWORD": "s3 cret",
        "ARANGO_IMAGE": "arangodb:3.12.11",
        "HANA_PORT": "443",
    }


def test_load_never_overwrites_the_real_environment(tmp_path, monkeypatch):
    (tmp_path / "orientdb").mkdir()
    (tmp_path / "orientdb" / ".env").write_text("ORIENT_URL=http://from-file\nORIENT_PASSWORD=pw\n")
    monkeypatch.setenv("ORIENT_URL", "http://from-shell")
    monkeypatch.delenv("ORIENT_PASSWORD", raising=False)
    path = envfile.load("orientdb", package=tmp_path)
    assert path == tmp_path / "orientdb" / ".env"
    assert os.environ["ORIENT_URL"] == "http://from-shell"
    assert os.environ["ORIENT_PASSWORD"] == "pw"
    monkeypatch.delenv("ORIENT_PASSWORD")


def test_an_unset_url_leaves_the_provider_on_docker(tmp_path, monkeypatch):
    (tmp_path / "arangodb").mkdir()
    (tmp_path / "arangodb" / ".env").write_text("# ARANGO_URL=\nARANGO_URL=\n")
    monkeypatch.delenv("ARANGO_URL", raising=False)
    assert envfile.load("arangodb", package=tmp_path) is not None
    assert "ARANGO_URL" not in os.environ


def test_no_file_is_not_an_error(tmp_path):
    assert envfile.load("hana", package=tmp_path) is None
