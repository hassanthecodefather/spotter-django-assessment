from django.core.cache import cache
from django.http import JsonResponse
from django.shortcuts import render
from django.views import View

class IndexView(View):
    def get(self, request):
        return render(request, "routing/index.html")


class MapView(View):
    def get(self, request):
        token = request.GET.get("token", "")
        route_data = cache.get(f"map:{token}") if token else None

        if not route_data:
            return JsonResponse(
                {"error": {"code": "MAP_EXPIRED", "message": "re-run the route request"}},
                status=404,
            )

        return render(request, "routing/map.html", {"route_data": route_data})
