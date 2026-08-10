"""Stable application entry point for ``uvicorn app.main:app``."""

from app.main_impl import *  # noqa: F403
from app.main_impl import app as _base_app
from app.main_impl import create_app as _base_create_app
from app.product_shell import install_product_shell


def create_app(settings=None):
    return install_product_shell(_base_create_app(settings))


app = install_product_shell(_base_app)
