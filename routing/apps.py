import logging

from django.apps import AppConfig

logger = logging.getLogger(__name__)

_preloaded = False


class RoutingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "routing"

    def ready(self):
        global _preloaded
        if _preloaded:
            return
        _preloaded = True
        try:
            from routing.services import geocoder, corridor  # noqa: F401 - triggers module-level preload
        except Exception as exc:
            logger.warning("Preload failed (migrations not yet applied?): %s", exc)
