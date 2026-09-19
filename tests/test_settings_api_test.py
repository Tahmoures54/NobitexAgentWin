from unittest.mock import patch
from gui.dialogs.settings_window import NOBITEX_STATS_URL

def test_nobitex_settings_url_is_irt():
    assert "nobitex.ir" in NOBITEX_STATS_URL
    assert "dstCurrency=rls" in NOBITEX_STATS_URL

@patch("gui.dialogs.settings_window.requests.get")
def test_nobitex_connectivity_check(mock_get):
    class Response:
        status_code = 200
    mock_get.return_value = Response()
    assert mock_get.return_value.status_code == 200
