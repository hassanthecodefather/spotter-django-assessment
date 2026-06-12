from django.urls import path

from routing import ui_views

urlpatterns = [
    path("", ui_views.IndexView.as_view(), name="index"),
    path("map/", ui_views.MapView.as_view(), name="map"),
]
