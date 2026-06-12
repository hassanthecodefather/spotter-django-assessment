from django.urls import path

from routing import views

urlpatterns = [
    path("route/", views.RouteView.as_view(), name="route"),
    path("health/", views.HealthView.as_view(), name="health"),
    path("locations/", views.LocationsView.as_view(), name="locations"),
]
