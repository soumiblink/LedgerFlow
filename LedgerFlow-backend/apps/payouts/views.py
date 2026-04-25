from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import AllowAny

from apps.payouts.serializers import PayoutRequestSerializer, PayoutResponseSerializer
from apps.payouts.services import (
    create_payout,
    InsufficientBalanceError,
    MerchantNotFoundError,
)


class PayoutCreateView(APIView):
    permission_classes = [AllowAny]  # auth will be added in a later step

    def post(self, request):
        # ── Validate Idempotency-Key header ──
        idempotency_key = request.headers.get("Idempotency-Key")
        if not idempotency_key:
            return Response(
                {"error": "Idempotency-Key header is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # ── Validate request body ──
        serializer = PayoutRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        data = serializer.validated_data

        try:
            result = create_payout(
                merchant_id=data["merchant_id"],
                amount_paise=data["amount_paise"],
                bank_account_id=data["bank_account_id"],
                idempotency_key=idempotency_key,
            )
        except MerchantNotFoundError as e:
            return Response({"error": str(e)}, status=status.HTTP_404_NOT_FOUND)
        except InsufficientBalanceError as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(
            PayoutResponseSerializer(result).data,
            status=status.HTTP_201_CREATED,
        )
