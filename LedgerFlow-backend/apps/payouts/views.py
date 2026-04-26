import logging
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

logger = logging.getLogger(__name__)


def _error(message: str, code: str = "error") -> dict:
    return {"error": {"code": code, "message": message}}


class PayoutCreateView(APIView):
    permission_classes = [AllowAny]  

    def post(self, request):
        idempotency_key = request.headers.get("Idempotency-Key")
        if not idempotency_key:
            return Response(
                _error("Idempotency-Key header is required.", "missing_idempotency_key"),
                status=status.HTTP_400_BAD_REQUEST,
            )

       
        serializer = PayoutRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {"error": {"code": "validation_error", "details": serializer.errors}},
                status=status.HTTP_400_BAD_REQUEST,
            )

        data = serializer.validated_data

        try:
            result = create_payout(
                merchant_id=data["merchant_id"],
                amount_paise=data["amount_paise"],
                bank_account_id=data["bank_account_id"],
                idempotency_key=idempotency_key,
            )
        except MerchantNotFoundError as e:
            logger.warning("Payout rejected — merchant not found: %s", e)
            return Response(
                _error(str(e), "merchant_not_found"),
                status=status.HTTP_404_NOT_FOUND,
            )
        except InsufficientBalanceError as e:
            logger.warning("Payout rejected — insufficient balance: %s", e)
            return Response(
                _error(str(e), "insufficient_balance"),
                status=status.HTTP_400_BAD_REQUEST,
            )

        logger.info(
            "Payout request accepted: payout_id=%s amount=%sp idempotency_key=%s",
            result["payout_id"], result["amount_paise"], idempotency_key,
        )
        return Response(
            PayoutResponseSerializer(result).data,
            status=status.HTTP_201_CREATED,
        )
