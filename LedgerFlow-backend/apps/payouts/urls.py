from django.urls import path
from .views import PayoutCreateView

urlpatterns = [
    path("", PayoutCreateView.as_view(), name="payout-create"),
]
