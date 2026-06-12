from django.urls import include, path

urlpatterns = [
    path("api/v1/", include("routing.urls")),
    path("", include("routing.ui_urls")),
]
