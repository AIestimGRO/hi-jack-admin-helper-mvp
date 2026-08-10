"""Stable application entry point for ``uvicorn app.main:app``."""

from app.main_impl import *  # noqa: F403
from app.main_impl import app as app
from app.product_shell import install_product_shell

install_product_shell(app)
