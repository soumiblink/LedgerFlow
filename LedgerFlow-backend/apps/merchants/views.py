from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import AllowAny
from rest_framework import status

from apps.merchants.models import Merchant
from apps.ledger.services import (
    get_merchant_balance,
    get_merchant_held_balance,
    get_available_balance,
)


class MerchantBalanceView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, merchant_id):
        try:
            Merchant.objects.get(id=merchant_id)
        except Merchant.DoesNotExist:
            return Response(
                {"error": {"code": "merchant_not_found", "message": f"Merchant {merchant_id} not found."}},
                status=status.HTTP_404_NOT_FOUND,
            )

        return Response({
            "merchant_id": str(merchant_id),
            "total_balance": get_merchant_balance(merchant_id),
            "held_balance": get_merchant_held_balance(merchant_id),
            "available_balance": get_available_balance(merchant_id),
        })
