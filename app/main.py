"""Stable application entry point for ``uvicorn app.main:app``."""

from app.hijack_extensions import install_hijack_extensions
from app.hijack_rating_baseline import install_hijack_rating_baseline
from app.hijack_rating_paging import install_hijack_rating_paging
from app.hijack_rating_relink import install_hijack_rating_relink
from app.main_impl import *  # noqa: F403
from app.main_impl import app as _base_app
from app.main_impl import create_app as _base_create_app
from app.product_shell import install_product_shell
from app.profile_experience import install_profile_experience


def _install_extensions(application):
    application = install_product_shell(application)
    application = install_hijack_extensions(application)
    application = install_hijack_rating_relink(application)
    application = install_hijack_rating_baseline(application)
    application = install_hijack_rating_paging(application)
    return install_profile_experience(application)


def create_app(settings=None):
    return _install_extensions(_base_create_app(settings))


app = _install_extensions(_base_app)
