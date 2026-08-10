"""Stable application entry point for ``uvicorn app.main:app``."""

from app.hijack_extensions import install_hijack_extensions
from app.main_impl import *  # noqa: F403
from app.main_impl import app as _base_app
from app.main_impl import create_app as _base_create_app
from app.product_shell import install_product_shell


def _install_extensions(application):
    return install_hijack_extensions(install_product_shell(application))


def create_app(settings=None):
    return _install_extensions(_base_create_app(settings))


app = _install_extensions(_base_app)
