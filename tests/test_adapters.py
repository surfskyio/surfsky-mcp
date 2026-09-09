import pytest
from surfsky import Fingerprint, PremiumProxy
from surfsky_cli import session as cli
from surfsky_cli.out import StaleRef

from surfsky_mcp.output import UsageError, error_result
from surfsky_mcp.tools import session_options

pytestmark = pytest.mark.anyio


def test_session_options_speak_the_cli_option_names():
    kwargs = session_options(
        "premium", "us", None, None, "mobile", "android", "13", "arm", ["image", "font"]
    )
    assert kwargs["proxy"] == PremiumProxy(country="us", type="mobile")
    assert kwargs["fingerprint"] == Fingerprint(
        os="android", os_version="13", os_arch="arm"
    )
    assert kwargs["block_resources"] == frozenset({"image", "font"})
    assert session_options(None, None, None, None, None, None, None, None, None) == {}
    with pytest.raises(UsageError, match="drop them when giving a proxy URL"):
        session_options(
            "socks5://u:p@h:1", "us", None, None, None, None, None, None, None
        )


def test_error_result_maps_usage_before_classify():
    assert error_result(UsageError("x")).structured_content["error"]["code"] == "usage"
    assert error_result(ValueError("x")).structured_content["error"]["code"] == "error"


class Page:
    def __init__(self, matches):
        self.target_id, self.matches = "T1", matches

    async def url(self):
        return "https://e.test/"

    async def count(self, selector):
        return self.matches


async def test_cli_rejects_refs_that_match_more_than_one_element():
    state = {
        "refs": {"1": "#dup > a"},
        "refs_target_id": "T1",
        "refs_url": "https://e.test/",
    }
    live = cli.Attached({}, state, None, Page(2))
    with pytest.raises(StaleRef, match="now matches 2 elements"):
        await cli.resolve_target(live, "@1")
    assert (
        await cli.resolve_target(cli.Attached({}, state, None, Page(1)), "@1")
        == "#dup > a"
    )
