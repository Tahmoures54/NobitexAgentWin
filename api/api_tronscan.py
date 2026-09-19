# source/api/api_tronscan.py
"""
TronScan API Client.

Endpoints:
    - get_transaction(tx_hash)
    - get_account_info(address)
    - get_token_transfers(address, ...)
    - get_trc20_transfers(address, ...)
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from api.api_base import ApiBaseClient

logger = logging.getLogger(__name__)


class TronscanClient(ApiBaseClient):
    """
    Client برای TronScan API.

    Note:
        نیاز به API key ندارد ولی rate limit دارد.
        برای تأیید تراکنش USDT از core.user_status استفاده کنید.
    """

    BASE_URL = "https://apilist.tronscan.org/api"

    def __init__(self, api_key: Optional[str] = None):
        super().__init__(
            api_key=api_key,
            auth_method="none",
            timeout=15.0,
            max_retries=3,
        )

    def get_transaction(self, tx_hash: str) -> Optional[Dict[str, Any]]:
        """
        اطلاعات یک تراکنش را از TronScan می‌گیرد.

        Parameters
        ----------
        tx_hash:
            هش تراکنش.

        Returns
        -------
        dict اطلاعات تراکنش یا None.
        """
        if not tx_hash or not tx_hash.strip():
            raise ValueError("tx_hash cannot be empty.")

        return self._request(
            f"{self.BASE_URL}/transaction-info",
            params={"hash": tx_hash.strip()},
        )

    def get_account_info(self, address: str) -> Optional[Dict[str, Any]]:
        """
        اطلاعات یک آدرس Tron را می‌گیرد.

        Parameters
        ----------
        address:
            آدرس Tron (شروع با T).
        """
        if not address or not address.strip():
            raise ValueError("address cannot be empty.")

        return self._request(
            f"{self.BASE_URL}/account",
            params={"address": address.strip()},
        )

    def get_token_transfers(
        self,
        address: str,
        limit:  int = 20,
        start:  int = 0,
        token:  Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        لیست انتقال‌های token یک آدرس.

        Parameters
        ----------
        address:
            آدرس Tron.
        limit:
            تعداد نتایج (max 50).
        start:
            offset برای pagination.
        token:
            فیلتر بر اساس contract address (اختیاری).
        """
        if not address or not address.strip():
            raise ValueError("address cannot be empty.")

        params: Dict[str, Any] = {
            "address": address.strip(),
            "limit":   min(limit, 50),
            "start":   max(start, 0),
        }
        if token:
            params["contract_address"] = token

        return self._request(
            f"{self.BASE_URL}/token_trc20/transfers",
            params=params,
        )

    def get_trc20_transfers(
        self,
        address:          str,
        contract_address: Optional[str] = None,
        limit:            int           = 20,
        start:            int           = 0,
    ) -> Optional[Dict[str, Any]]:
        """
        انتقال‌های TRC20 یک آدرس.

        Parameters
        ----------
        address:
            آدرس Tron.
        contract_address:
            آدرس قرارداد TRC20 برای فیلتر.
        """
        if not address or not address.strip():
            raise ValueError("address cannot be empty.")

        params: Dict[str, Any] = {
            "address": address.strip(),
            "limit":   min(limit, 50),
            "start":   max(start, 0),
        }
        if contract_address:
            params["contract_address"] = contract_address

        return self._request(
            f"{self.BASE_URL}/token_trc20/transfers",
            params=params,
        )

    def get_account_tokens(
        self,
        address: str,
        token_type: int = 20,
    ) -> Optional[Dict[str, Any]]:
        """
        لیست token های یک آدرس.

        Parameters
        ----------
        token_type:
            10 = TRC10, 20 = TRC20
        """
        if not address or not address.strip():
            raise ValueError("address cannot be empty.")

        return self._request(
            f"{self.BASE_URL}/account/tokens",
            params={
                "address": address.strip(),
                "token_id": "",
                "show": 0,
                "sortType": 0,
                "sortBy": 0,
                "limit": 200,
            },
        )

    def get_block(self, block_number: int) -> Optional[Dict[str, Any]]:
        """
        اطلاعات یک بلاک.

        Parameters
        ----------
        block_number:
            شماره بلاک.
        """
        if block_number < 0:
            raise ValueError("block_number must be non-negative.")

        return self._request(
            f"{self.BASE_URL}/block",
            params={"number": block_number},
        )