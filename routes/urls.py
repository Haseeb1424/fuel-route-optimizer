from django.urls import path

from routes.views import RoutePreviewView


urlpatterns = [
    path(
        "optimize/",
        RoutePreviewView.as_view(),
        name="route-optimize",
    ),
]