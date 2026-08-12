"""Stable application entry point for ``uvicorn app.main:app``."""

from starlette.middleware.sessions import SessionMiddleware

from app.account_security import install_account_security
from app.admin_account_lifecycle import install_admin_account_lifecycle
from app.admin_telegram_unlink import install_admin_telegram_unlink
from app.db import init_db
from app.hijack_extensions import install_hijack_extensions
from app.hijack_rating_baseline import install_hijack_rating_baseline
from app.hijack_rating_paging import install_hijack_rating_paging
from app.hijack_rating_relink import install_hijack_rating_relink
from app.legal_registration import install_legal_registration
from app.main_impl import *  # noqa: F403
from app.main_impl import app as _base_app
from app.main_impl import create_app as _base_create_app
from app.member_account_management import install_member_account_management
from app.prelaunch_economy_compat import install_prelaunch_economy_compat
from app.prelaunch_experience import install_prelaunch_experience
from app.prelaunch_profile_privacy import install_prelaunch_profile_privacy
from app.prelaunch_profile_sharing import install_prelaunch_profile_sharing
from app.product_shell import install_product_shell
from app.profile_experience import install_profile_experience
from app.public_rating_consent_policy import install_public_rating_consent_policy
from app.pwa import install_pwa
from app.registration_flow_hotfix import install_registration_flow_hotfix
from app.security_journal import install_security_journal


def _session_middleware_outermost(application):
    """Keep request.session available to extension middleware."""
    for index, middleware in enumerate(application.user_middleware):
        if middleware.cls is SessionMiddleware:
            if index:
                application.user_middleware.insert(
                    0, application.user_middleware.pop(index)
                )
            application.middleware_stack = None
            break
    return application


def _install_extensions(application):
    application = install_product_shell(application)
    application = install_pwa(application)
    application = install_account_security(application)
    application = install_member_account_management(application)
    application = install_admin_account_lifecycle(application)
    application = install_admin_telegram_unlink(application)
    application = install_security_journal(application)
    # The base lifespan initializes the DB at startup, while extensions are
    # installed during app import. init_db is additive/idempotent, so expose the
    # base tables before the legal extension adds its own foreign keys/triggers.
    init_db(application.state.settings.db_path)
    application = install_legal_registration(application)
    application = install_registration_flow_hotfix(application)
    application = install_public_rating_consent_policy(application)
    application = install_hijack_extensions(application)
    application = install_hijack_rating_relink(application)
    application = install_hijack_rating_baseline(application)
    application = install_hijack_rating_paging(application)
    application = install_profile_experience(application)
    application = install_prelaunch_experience(application)
    application = install_prelaunch_economy_compat(application)
    application = install_prelaunch_profile_sharing(application)
    application = install_prelaunch_profile_privacy(application)
    return _session_middleware_outermost(application)


def create_app(settings=None):
    return _install_extensions(_base_create_app(settings))


app = _install_extensions(_base_app)
